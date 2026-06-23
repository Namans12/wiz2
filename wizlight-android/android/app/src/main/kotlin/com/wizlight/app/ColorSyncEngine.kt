package com.wizlight.app

import android.content.Context
import android.graphics.Color
import android.hardware.display.DisplayManager
import android.os.Build
import android.os.SystemClock
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.nio.charset.StandardCharsets
import java.util.ArrayDeque
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.exp
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToInt
import kotlin.math.sin
import kotlin.math.sqrt

/**
 * Canonical color pipeline shared by ALL capture front-ends:
 *   - ScreenCaptureService (MediaProjection screen capture)
 *   - PlayerActivity        (ExoPlayer USB media playback)
 *
 * A front-end samples its frame into an NxN grid of ARGB ints and calls
 * [submitFrame]; the engine handles motion scoring, color extraction, gamut /
 * HDR mapping, the CCT-based cold/warm white split, device-profile tuning,
 * smoothing, the WiZ basis projection, and the UDP send. This is the single
 * source of truth for the color math — do not duplicate it in a front-end.
 */
class ColorSyncEngine {

    enum class DeviceProfile { GENERIC, BRAVIA, WIDE_GAMUT_HDR }

    data class RgbColor(val r: Int, val g: Int, val b: Int)

    data class WizPilotColor(val rgb: RgbColor, val coldWhite: Int, val warmWhite: Int)

    data class BulbTarget(val ip: String, val region: String)

    private data class SampledFrame(val pixels: IntArray, val width: Int, val height: Int)

    // ── Configurable params ──────────────────────────────────────────────────
    var effectiveMode = "single"
    var adaptiveFps = true
    var minFps = 10
    var maxFps = 24
    var currentFps = 24
    var smoothing = 0.2
    var colorBoost = 1.18
    var colorAlgorithm = "auto"
    var minBrightness = 20
    var minColorDelta = 8
    var edgeWeight = 1.5
    var predictiveSmoothing = true
    var predictionWeight = 0.3
    var sourceColorSpace = "srgb"
    var hdrToneMapping = false
    var deviceProfile = DeviceProfile.GENERIC
    var bulbTargets: List<BulbTarget> = emptyList()

    // ── Detection results (populated by configureAutoFromDisplay) ─────────────
    var requestedColorSpace = "auto"
    var requestedHdrToneMapping = false
    var detectedDisplayGamut = "srgb"
    var detectedHdrSupport = false
    var supportedHdrTypes: Set<String> = emptySet()
    var primaryHdrType: String = "none"

    // ── Runtime state ─────────────────────────────────────────────────────────
    private var previousMotionSample: FloatArray? = null
    private var previousSingleColor: RgbColor? = null
    private var paletteHoldFramesCount = 0
    var lastMotionScore = 0.0
        private set
    var lastSmoothingFactor = 0.2
        private set
    var lastEffectiveColorBoost = 1.18
        private set
    var lastDeltaThreshold = 8
        private set
    var updatesSent = 0
        private set
    private var lastForcedSendAt = 0L
    private var lastSendAt: Long? = null
    private val sendIntervalsMs = ArrayDeque<Long>()

    val currentOutputColors = mutableMapOf<String, RgbColor>()
    val lastSentColors = mutableMapOf<String, RgbColor>()
    val lastTargetColors = mutableMapOf<String, RgbColor>()
    private val predictionTargets = mutableMapOf<String, RgbColor>()
    private val addressCache = mutableMapOf<String, InetAddress>()
    private val packetCache = object : LinkedHashMap<String, ByteArray>(128, 0.75f, true) {
        override fun removeEldestEntry(eldest: MutableMap.MutableEntry<String, ByteArray>?): Boolean {
            return size > 4096
        }
    }
    private var udpSocket: DatagramSocket? = null

    private val wizBasis = arrayOf(
        doubleArrayOf(cos(0.0), sin(0.0)),
        doubleArrayOf(cos((2.0 * PI) / 3.0), sin((2.0 * PI) / 3.0)),
        doubleArrayOf(cos((4.0 * PI) / 3.0), sin((4.0 * PI) / 3.0)),
    )
    private val DISPLAY_P3_TO_SRGB = arrayOf(
        doubleArrayOf(1.2249401, -0.2249404, 0.0),
        doubleArrayOf(-0.0420569, 1.0420571, 0.0),
        doubleArrayOf(-0.0196376, -0.0786361, 1.0982735),
    )
    private val REC2020_TO_SRGB = arrayOf(
        doubleArrayOf(1.6602270, -0.5875478, -0.0728387),
        doubleArrayOf(-0.1245535, 1.1329261, -0.0083495),
        doubleArrayOf(-0.0181551, -0.1006030, 1.1189980),
    )

    companion object {
        private const val WIZ_PORT = 38899
    }

    // ── Lifecycle ──────────────────────────────────────────────────────────────
    fun start() {
        udpSocket = DatagramSocket().apply { sendBufferSize = 262144 }
        reset()
    }

    fun reset() {
        previousMotionSample = null
        previousSingleColor = null
        paletteHoldFramesCount = 0
        lastMotionScore = 0.0
        lastForcedSendAt = 0L
        lastSendAt = null
        lastEffectiveColorBoost = colorBoost
        lastDeltaThreshold = minColorDelta
        updatesSent = 0
        currentFps = maxFps
        sendIntervalsMs.clear()
        currentOutputColors.clear()
        lastSentColors.clear()
        lastTargetColors.clear()
        predictionTargets.clear()
        addressCache.clear()
        packetCache.clear()
    }

    fun close() {
        udpSocket?.close()
        udpSocket = null
    }

    // ── Detection (shared by both front-ends) ───────────────────────────────────
    fun detectDeviceProfileFromBuild() {
        val manufacturer = Build.MANUFACTURER.lowercase()
        val brand = Build.BRAND.lowercase()
        val model = Build.MODEL.lowercase()
        val product = Build.PRODUCT.lowercase()
        val sonyLike = manufacturer.contains("sony") || brand.contains("sony")
        val braviaLike =
            model.contains("bravia") ||
                product.contains("bravia") ||
                model.startsWith("xr-") ||
                model.startsWith("k-") ||
                product.startsWith("bravia") ||
                product.contains("googletv")
        deviceProfile = if (sonyLike && braviaLike) DeviceProfile.BRAVIA else DeviceProfile.GENERIC
    }

    /**
     * Inspect the primary display's gamut + HDR capabilities and resolve the
     * effective [sourceColorSpace] / [hdrToneMapping] / [deviceProfile] when the
     * caller requested "auto". Safe to call on any thread.
     */
    fun configureAutoFromDisplay(
        context: Context,
        requestedColorSpaceRaw: String?,
        requestedHdrTone: Boolean,
    ) {
        requestedColorSpace = normalizeColorSpace(requestedColorSpaceRaw)
        requestedHdrToneMapping = requestedHdrTone
        detectDeviceProfileFromBuild()

        detectedDisplayGamut = "srgb"
        detectedHdrSupport = false
        supportedHdrTypes = emptySet()
        primaryHdrType = "none"
        try {
            val displayManager = context.getSystemService(Context.DISPLAY_SERVICE) as? DisplayManager
            val display = displayManager?.getDisplay(android.view.Display.DEFAULT_DISPLAY)
            if (display != null) {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
                    detectedHdrSupport = display.isHdr
                    val caps = display.hdrCapabilities
                    if (caps != null) {
                        val types = mutableSetOf<String>()
                        for (type in caps.supportedHdrTypes) {
                            when (type) {
                                1 -> types.add("dolby_vision")
                                2 -> types.add("hdr10")
                                3 -> types.add("hlg")
                                4 -> types.add("hdr10_plus")
                            }
                        }
                        supportedHdrTypes = types
                        primaryHdrType = when {
                            "dolby_vision" in types -> "dolby_vision"
                            "hdr10_plus" in types -> "hdr10_plus"
                            "hdr10" in types -> "hdr10"
                            "hlg" in types -> "hlg"
                            else -> "none"
                        }
                    }
                }
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    val wide = display.preferredWideGamutColorSpace
                    val wideName = wide?.name?.lowercase() ?: ""
                    detectedDisplayGamut = when {
                        wideName.contains("bt2020") || wideName.contains("bt.2020") ||
                            wideName.contains("rec.2020") || wideName.contains("rec2020") -> "rec2020"
                        wideName.contains("p3") -> "display-p3"
                        display.isWideColorGamut -> "display-p3"
                        else -> "srgb"
                    }
                }
                if (detectedHdrSupport && detectedDisplayGamut == "srgb") {
                    detectedDisplayGamut = "rec2020"
                }
                if (deviceProfile == DeviceProfile.BRAVIA && detectedDisplayGamut == "srgb") {
                    detectedDisplayGamut = "rec2020"
                    detectedHdrSupport = true
                }
            }
        } catch (_: Throwable) {
        }

        if (deviceProfile == DeviceProfile.GENERIC && (detectedHdrSupport || detectedDisplayGamut != "srgb")) {
            deviceProfile = DeviceProfile.WIDE_GAMUT_HDR
        }

        sourceColorSpace = if (requestedColorSpace == "auto") detectedDisplayGamut else requestedColorSpace
        hdrToneMapping = requestedHdrToneMapping || (requestedColorSpace == "auto" && detectedHdrSupport)
        if (deviceProfile == DeviceProfile.BRAVIA && requestedColorSpace == "auto") {
            hdrToneMapping = true
        }
    }

    fun normalizeColorSpace(raw: String?): String {
        return when (raw?.lowercase()?.replace("_", "-")) {
            "p3", "display-p3" -> "display-p3"
            "bt2020", "bt.2020", "rec-2020", "rec2020" -> "rec2020"
            "srgb" -> "srgb"
            "auto", null, "" -> "auto"
            else -> "auto"
        }
    }

    fun normalizeAlgorithm(raw: String?): String {
        return when (raw?.lowercase()) {
            "auto", "weighted", "kmeans", "histogram" -> raw.lowercase()
            else -> "auto"
        }
    }

    // ── Main per-frame entry ─────────────────────────────────────────────────────
    /**
     * @param grids region -> NxN ARGB grid. Single mode uses key "all"; zones uses
     *              "left"/"right". @return true if any bulb update was sent.
     */
    fun submitFrame(grids: Map<String, IntArray>, size: Int): Boolean {
        if (grids.isEmpty()) return false
        val primary = grids["all"] ?: grids["left"] ?: grids.values.first()
        val motionScore = computeMotionScore(primary)
        lastMotionScore = motionScore
        updateAdaptiveFps(motionScore)

        val targetColors = extractTargetColors(grids, size, motionScore)
        lastTargetColors.clear()
        lastTargetColors.putAll(targetColors)

        val smoothingFactor = targetSmoothing(motionScore)
        lastSmoothingFactor = smoothingFactor
        val sendThreshold = sendDeltaThreshold(motionScore)
        lastDeltaThreshold = sendThreshold
        val mappedBulbColors = buildBulbColorMap(targetColors)
        var sentAnyUpdate = false

        val nowMs = SystemClock.elapsedRealtime()
        val forceRefreshDue = nowMs - lastForcedSendAt >= 1500L
        for ((ip, targetColor) in mappedBulbColors) {
            val current = currentOutputColors[ip]
            val predictedTarget = applyPredictiveTarget(ip, targetColor, motionScore)
            val smoothed = if (current == null) {
                predictedTarget
            } else {
                smoothColor(current, predictedTarget, smoothingFactor)
            }
            currentOutputColors[ip] = smoothed

            val lastSent = lastSentColors[ip]
            val needsForcedRefresh = lastSent != null &&
                forceRefreshDue &&
                colorDistance(smoothed, lastSent) >= max(2, minColorDelta / 2)
            if (
                lastSent == null ||
                colorDistance(smoothed, lastSent) >= sendThreshold ||
                needsForcedRefresh
            ) {
                sendColor(ip, smoothed)
                lastSentColors[ip] = smoothed
                sentAnyUpdate = true
            }
        }
        if (sentAnyUpdate || lastForcedSendAt == 0L) {
            lastForcedSendAt = nowMs
        }

        val staleIps = currentOutputColors.keys - mappedBulbColors.keys
        for (ip in staleIps) {
            currentOutputColors.remove(ip)
            lastSentColors.remove(ip)
            predictionTargets.remove(ip)
        }

        if (sentAnyUpdate) {
            lastSendAt?.let {
                sendIntervalsMs.addLast(nowMs - it)
                while (sendIntervalsMs.size > 30) {
                    sendIntervalsMs.removeFirst()
                }
            }
            lastSendAt = nowMs
            updatesSent += 1
        }
        return sentAnyUpdate
    }

    fun sendRateHz(): Double {
        val intervalAverage = if (sendIntervalsMs.isEmpty()) 0.0 else sendIntervalsMs.average()
        return if (intervalAverage > 0.0) 1000.0 / intervalAverage else 0.0
    }

    /** Color-pipeline status fields; merged by the front-end with capture-specific stats. */
    fun statusExtras(): Map<String, Any?> {
        return mapOf(
            "mode" to effectiveMode,
            "requestedFps" to maxFps,
            "currentFps" to currentFps,
            "sendRateHz" to sendRateHz(),
            "motionScore" to lastMotionScore,
            "smoothing" to lastSmoothingFactor,
            "smoothingFactor" to lastSmoothingFactor,
            "effectiveColorBoost" to lastEffectiveColorBoost,
            "minBrightness" to minBrightness,
            "deltaThreshold" to lastDeltaThreshold,
            "colorAlgorithm" to colorAlgorithm,
            "sourceColorSpace" to sourceColorSpace,
            "hdrToneMapping" to hdrToneMapping,
            "requestedColorSpace" to requestedColorSpace,
            "detectedDisplayGamut" to detectedDisplayGamut,
            "detectedHdrSupport" to detectedHdrSupport,
            "supportedHdrTypes" to supportedHdrTypes.toList(),
            "primaryHdrType" to primaryHdrType,
            "deviceProfile" to deviceProfile.name.lowercase(),
            "predictiveSmoothing" to predictiveSmoothing,
            "updatesSent" to updatesSent,
            "targetColors" to lastTargetColors.mapValues { listOf(it.value.r, it.value.g, it.value.b) },
            "outputColors" to currentOutputColors.mapValues { listOf(it.value.r, it.value.g, it.value.b) },
        )
    }

    // ── Motion ───────────────────────────────────────────────────────────────────
    private fun computeMotionScore(pixels: IntArray): Double {
        val current = FloatArray(pixels.size)
        for (index in pixels.indices) {
            current[index] = colorLuma(pixels[index]).toFloat()
        }
        val previous = previousMotionSample
        previousMotionSample = current
        if (previous == null || previous.size != current.size) {
            return 0.0
        }
        var total = 0.0
        for (index in current.indices) {
            total += abs(current[index] - previous[index])
        }
        return min(1.0, total / current.size.toDouble())
    }

    // ── Extraction ─────────────────────────────────────────────────────────────
    private fun extractTargetColors(
        grids: Map<String, IntArray>,
        size: Int,
        motionScore: Double,
    ): Map<String, RgbColor> {
        return if (effectiveMode == "single") {
            val sampled = SampledFrame(grids["all"] ?: grids.values.first(), size, size)
            val extracted = if (colorAlgorithm == "auto") {
                extractCinematicSingleColor(sampled)
            } else {
                extractColor(sampled)
            }
            val enhanced = enhanceColor(mapSourceColorToSrgb(extracted))
            val held = applyCinematicPaletteHold(enhanced, previousSingleColor, motionScore)
            previousSingleColor = held
            mapOf("all" to held)
        } else {
            previousSingleColor = null
            val targets = mutableMapOf<String, RgbColor>()
            for ((region, grid) in grids) {
                val sampled = SampledFrame(grid, size, size)
                targets[region] = enhanceColor(mapSourceColorToSrgb(extractColor(sampled)))
            }
            targets
        }
    }

    private fun extractColor(sample: SampledFrame): RgbColor {
        return when (colorAlgorithm) {
            "weighted" -> extractWeightedColor(sample)
            "histogram" -> extractHistogramColor(sample)
            "kmeans" -> extractKMeansColor(sample)
            else -> extractBalancedAutoColor(sample)
        }
    }

    private fun extractHistogramColor(sample: SampledFrame): RgbColor {
        val binSize = 32
        val bins = HashMap<Int, Int>()
        val sums = HashMap<Int, IntArray>()
        for (pixel in sample.pixels) {
            val r = Color.red(pixel)
            val g = Color.green(pixel)
            val b = Color.blue(pixel)
            val qr = (r / binSize).coerceIn(0, 7)
            val qg = (g / binSize).coerceIn(0, 7)
            val qb = (b / binSize).coerceIn(0, 7)
            val key = (qr shl 6) or (qg shl 3) or qb
            bins[key] = (bins[key] ?: 0) + 1
            val sum = sums.getOrPut(key) { intArrayOf(0, 0, 0) }
            sum[0] += r
            sum[1] += g
            sum[2] += b
        }
        val dominant = bins.maxByOrNull { it.value }?.key ?: return extractWeightedColor(sample)
        val total = max(1, bins[dominant] ?: 1)
        val sum = sums[dominant] ?: intArrayOf(128, 128, 128)
        return RgbColor(sum[0] / total, sum[1] / total, sum[2] / total)
    }

    private fun extractKMeansColor(sample: SampledFrame): RgbColor {
        if (sample.pixels.isEmpty()) {
            return RgbColor(128, 128, 128)
        }
        val clusterCount = 3
        val centroids = Array(clusterCount) { index ->
            val pixel = sample.pixels[(index * sample.pixels.size / clusterCount).coerceIn(0, sample.pixels.size - 1)]
            floatArrayOf(Color.red(pixel).toFloat(), Color.green(pixel).toFloat(), Color.blue(pixel).toFloat())
        }
        repeat(5) {
            val sums = Array(clusterCount) { floatArrayOf(0f, 0f, 0f) }
            val counts = IntArray(clusterCount)
            for (pixel in sample.pixels) {
                val r = Color.red(pixel).toFloat()
                val g = Color.green(pixel).toFloat()
                val b = Color.blue(pixel).toFloat()
                var bestIndex = 0
                var bestDistance = Double.MAX_VALUE
                for (index in centroids.indices) {
                    val dr = r - centroids[index][0]
                    val dg = g - centroids[index][1]
                    val db = b - centroids[index][2]
                    val distance = (dr * dr + dg * dg + db * db).toDouble()
                    if (distance < bestDistance) {
                        bestDistance = distance
                        bestIndex = index
                    }
                }
                sums[bestIndex][0] += r
                sums[bestIndex][1] += g
                sums[bestIndex][2] += b
                counts[bestIndex] += 1
            }
            for (index in centroids.indices) {
                if (counts[index] == 0) continue
                centroids[index][0] = sums[index][0] / counts[index]
                centroids[index][1] = sums[index][1] / counts[index]
                centroids[index][2] = sums[index][2] / counts[index]
            }
        }
        var dominantIndex = 0
        var dominantScore = -1.0
        for (index in centroids.indices) {
            val saturation = rgbSaturation(
                centroids[index][0].roundToInt(),
                centroids[index][1].roundToInt(),
                centroids[index][2].roundToInt(),
            )
            val luma = rgbLuma(
                centroids[index][0].roundToInt(),
                centroids[index][1].roundToInt(),
                centroids[index][2].roundToInt(),
            )
            val score = saturation * 0.75 + luma * 0.25
            if (score > dominantScore) {
                dominantScore = score
                dominantIndex = index
            }
        }
        val chosen = centroids[dominantIndex]
        return RgbColor(
            chosen[0].roundToInt().coerceIn(0, 255),
            chosen[1].roundToInt().coerceIn(0, 255),
            chosen[2].roundToInt().coerceIn(0, 255),
        )
    }

    private fun extractBalancedAutoColor(sample: SampledFrame): RgbColor {
        val weighted = extractWeightedColor(sample)
        val accent = extractVibrantAccent(sample)
        val saturationMean = averageSaturation(sample)
        val vividRatio = vividRatio(sample, saturationThreshold = 0.35, lumaThreshold = 0.08)
        val sceneEnergy = min(1.0, vividRatio * 2.2 + saturationMean * 1.8)
        var accentWeight = 0.10 + sceneEnergy * 0.25
        if (sceneEnergy < 0.35) {
            accentWeight *= 0.5
        }
        return blendColors(weighted, accent, accentWeight)
    }

    private fun extractCinematicSingleColor(sample: SampledFrame): RgbColor {
        val weighted = extractWeightedColor(sample)
        val accent = extractVibrantAccent(sample)
        val palette = extractPaletteAnchor(sample)
        val saturationMean = averageSaturation(sample)
        val vividRatio = vividRatio(sample, saturationThreshold = 0.32, lumaThreshold = 0.08)
        val averageLuma = averageLuma(sample)
        if (averageLuma < 0.03 && saturationMean < 0.04) {
            return weighted
        }
        var accentWeight = 0.18 + min(0.34, saturationMean * 0.55 + vividRatio * 0.22)
        var paletteWeight = 0.10 + min(0.26, vividRatio * 0.35 + max(0.0, saturationMean - 0.12) * 0.5)
        if (saturationMean < 0.12) {
            paletteWeight *= 0.5
        }
        if (saturationMean < 0.08 && averageLuma > 0.18) {
            accentWeight *= 0.75
        }
        return blendColors(blendColors(weighted, accent, accentWeight), palette, paletteWeight)
    }

    private fun extractWeightedColor(sample: SampledFrame): RgbColor {
        val border = max(1, min(sample.width, sample.height) / 6)
        var totalR = 0.0
        var totalG = 0.0
        var totalB = 0.0
        var totalWeight = 0.0
        for (y in 0 until sample.height) {
            for (x in 0 until sample.width) {
                val pixel = sample.pixels[y * sample.width + x]
                val r = Color.red(pixel)
                val g = Color.green(pixel)
                val b = Color.blue(pixel)
                val saturation = rgbSaturation(r, g, b)
                val luma = rgbLuma(r, g, b)
                var weight = 0.15 + saturation * 2.2 + luma * 0.5
                if (x < border || y < border || x >= sample.width - border || y >= sample.height - border) {
                    weight *= edgeWeight
                }
                totalR += r * weight
                totalG += g * weight
                totalB += b * weight
                totalWeight += weight
            }
        }
        if (totalWeight <= 0.0) {
            return RgbColor(128, 128, 128)
        }
        return RgbColor(
            (totalR / totalWeight).roundToInt().coerceIn(0, 255),
            (totalG / totalWeight).roundToInt().coerceIn(0, 255),
            (totalB / totalWeight).roundToInt().coerceIn(0, 255),
        )
    }

    private fun extractVibrantAccent(sample: SampledFrame): RgbColor {
        val energies = DoubleArray(sample.pixels.size)
        for (index in sample.pixels.indices) {
            val pixel = sample.pixels[index]
            val saturation = rgbSaturation(Color.red(pixel), Color.green(pixel), Color.blue(pixel))
            val luma = colorLuma(pixel)
            energies[index] = saturation * 0.8 + luma * 0.2
        }
        val sorted = energies.sorted()
        val thresholdIndex = max(0, (sorted.size * 0.8).toInt() - 1)
        val threshold = sorted[thresholdIndex]
        var totalR = 0.0
        var totalG = 0.0
        var totalB = 0.0
        var count = 0
        for (index in sample.pixels.indices) {
            if (energies[index] < threshold) continue
            val pixel = sample.pixels[index]
            totalR += Color.red(pixel)
            totalG += Color.green(pixel)
            totalB += Color.blue(pixel)
            count += 1
        }
        if (count == 0) {
            return extractWeightedColor(sample)
        }
        return RgbColor(
            (totalR / count).roundToInt().coerceIn(0, 255),
            (totalG / count).roundToInt().coerceIn(0, 255),
            (totalB / count).roundToInt().coerceIn(0, 255),
        )
    }

    private fun extractPaletteAnchor(sample: SampledFrame): RgbColor {
        val energies = DoubleArray(sample.pixels.size)
        for (index in sample.pixels.indices) {
            val pixel = sample.pixels[index]
            val saturation = rgbSaturation(Color.red(pixel), Color.green(pixel), Color.blue(pixel))
            val luma = colorLuma(pixel)
            energies[index] = saturation * 0.9 + max(0.0, 0.65 - abs(luma - 0.42)) * 0.1
        }
        val sorted = energies.sorted()
        val thresholdIndex = max(0, (sorted.size * 0.9).toInt() - 1)
        val threshold = sorted[thresholdIndex]
        var totalR = 0.0
        var totalG = 0.0
        var totalB = 0.0
        var count = 0
        for (index in sample.pixels.indices) {
            val pixel = sample.pixels[index]
            if (energies[index] < threshold || colorLuma(pixel) <= 0.05) continue
            totalR += Color.red(pixel)
            totalG += Color.green(pixel)
            totalB += Color.blue(pixel)
            count += 1
        }
        if (count == 0) {
            return extractVibrantAccent(sample)
        }
        return RgbColor(
            (totalR / count).roundToInt().coerceIn(0, 255),
            (totalG / count).roundToInt().coerceIn(0, 255),
            (totalB / count).roundToInt().coerceIn(0, 255),
        )
    }

    private fun averageSaturation(sample: SampledFrame): Double {
        var total = 0.0
        for (pixel in sample.pixels) {
            total += rgbSaturation(Color.red(pixel), Color.green(pixel), Color.blue(pixel))
        }
        return total / sample.pixels.size.toDouble()
    }

    private fun averageLuma(sample: SampledFrame): Double {
        var total = 0.0
        for (pixel in sample.pixels) {
            total += colorLuma(pixel)
        }
        return total / sample.pixels.size.toDouble()
    }

    private fun vividRatio(sample: SampledFrame, saturationThreshold: Double, lumaThreshold: Double): Double {
        var count = 0
        for (pixel in sample.pixels) {
            val r = Color.red(pixel)
            val g = Color.green(pixel)
            val b = Color.blue(pixel)
            if (rgbSaturation(r, g, b) > saturationThreshold && rgbLuma(r, g, b) > lumaThreshold) {
                count += 1
            }
        }
        return count.toDouble() / sample.pixels.size.toDouble()
    }

    private fun buildBulbColorMap(targetColors: Map<String, RgbColor>): Map<String, RgbColor> {
        if (bulbTargets.isEmpty() || targetColors.isEmpty()) {
            return emptyMap()
        }
        if (effectiveMode != "zones" || targetColors.containsKey("all")) {
            val single = targetColors["all"] ?: averageColors(targetColors.values.toList())
            return bulbTargets.associate { it.ip to single }
        }
        val fallback = averageColors(targetColors.values.toList())
        return bulbTargets.associate { bulb -> bulb.ip to (targetColors[bulb.region] ?: fallback) }
    }

    // ── Color transforms ───────────────────────────────────────────────────────
    private fun blendColors(base: RgbColor, accent: RgbColor, weight: Double): RgbColor {
        val accentWeight = weight.coerceIn(0.0, 1.0)
        val baseWeight = 1.0 - accentWeight
        return RgbColor(
            (base.r * baseWeight + accent.r * accentWeight).roundToInt().coerceIn(0, 255),
            (base.g * baseWeight + accent.g * accentWeight).roundToInt().coerceIn(0, 255),
            (base.b * baseWeight + accent.b * accentWeight).roundToInt().coerceIn(0, 255),
        )
    }

    private fun mapSourceColorToSrgb(color: RgbColor): RgbColor {
        if (sourceColorSpace == "srgb" && !hdrToneMapping) {
            return color
        }
        val linear = doubleArrayOf(
            srgbDecode(color.r / 255.0),
            srgbDecode(color.g / 255.0),
            srgbDecode(color.b / 255.0),
        )
        val converted = when (sourceColorSpace) {
            "display-p3" -> multiplyColorMatrix(DISPLAY_P3_TO_SRGB, linear)
            "rec2020" -> multiplyColorMatrix(REC2020_TO_SRGB, linear)
            else -> linear
        }
        val compressed = compressToSrgb(converted)
        val mapped = if (hdrToneMapping) toneMapByHdrType(compressed) else compressed
        return RgbColor(
            (srgbEncode(mapped[0]) * 255.0).roundToInt().coerceIn(0, 255),
            (srgbEncode(mapped[1]) * 255.0).roundToInt().coerceIn(0, 255),
            (srgbEncode(mapped[2]) * 255.0).roundToInt().coerceIn(0, 255),
        )
    }

    private fun multiplyColorMatrix(matrix: Array<DoubleArray>, color: DoubleArray): DoubleArray {
        return doubleArrayOf(
            matrix[0][0] * color[0] + matrix[0][1] * color[1] + matrix[0][2] * color[2],
            matrix[1][0] * color[0] + matrix[1][1] * color[1] + matrix[1][2] * color[2],
            matrix[2][0] * color[0] + matrix[2][1] * color[1] + matrix[2][2] * color[2],
        )
    }

    private fun compressToSrgb(color: DoubleArray): DoubleArray {
        return DoubleArray(3) { index ->
            val value = max(0.0, color[index])
            if (value <= 0.82) {
                value
            } else {
                0.82 + 0.18 * (1.0 - exp(-((value - 0.82) / 0.18) * 2.5))
            }.coerceIn(0.0, 1.0)
        }
    }

    private fun acesToneMap(color: DoubleArray): DoubleArray {
        return DoubleArray(3) { index ->
            val x = max(0.0, color[index]) * 4.5
            ((x * (2.51 * x + 0.03)) / (x * (2.43 * x + 0.59) + 0.14)).coerceIn(0.0, 1.0)
        }
    }

    private fun hableToneMap(color: DoubleArray): DoubleArray {
        val a = 0.15; val b = 0.50; val c = 0.10; val d = 0.20
        val e = 0.02; val f = 0.30; val w = 11.2
        fun curve(x: Double): Double {
            val xx = max(0.0, x) * 2.0
            return ((xx * (a * xx + c * b) + d * e) / (xx * (a * xx + b) + d * f) - e / f)
        }
        val whiteScale = 1.0 / curve(w)
        return DoubleArray(3) { (curve(color[it]) * whiteScale).coerceIn(0.0, 1.0) }
    }

    private fun hlgToneMap(color: DoubleArray): DoubleArray {
        val a = 0.17883277; val b = 0.28466892; val c = 0.55991073
        return DoubleArray(3) { index ->
            val v = max(0.0, color[index])
            val linear = if (v <= 0.5) {
                (v * v) / 3.0
            } else {
                (exp((v - c) / a) + b) / 12.0
            }
            val gamma = 1.2
            Math.pow(linear.coerceAtMost(1.0), 1.0 / gamma).coerceIn(0.0, 1.0)
        }
    }

    private fun toneMapByHdrType(color: DoubleArray): DoubleArray {
        return when (primaryHdrType) {
            "hlg" -> hlgToneMap(color)
            "dolby_vision", "hdr10_plus" -> hableToneMap(color)
            "hdr10" -> acesToneMap(color)
            else -> acesToneMap(color)
        }
    }

    private fun srgbDecode(value: Double): Double {
        val v = value.coerceIn(0.0, 1.0)
        return if (v <= 0.04045) v / 12.92 else Math.pow((v + 0.055) / 1.055, 2.4)
    }

    private fun srgbEncode(value: Double): Double {
        val v = value.coerceIn(0.0, 1.0)
        return if (v <= 0.0031308) v * 12.92 else 1.055 * Math.pow(v, 1.0 / 2.4) - 0.055
    }

    private fun enhanceColor(color: RgbColor): RgbColor {
        lastEffectiveColorBoost = colorBoost
        val mean = (color.r + color.g + color.b) / 3.0
        var red = mean + (color.r - mean) * colorBoost
        var green = mean + (color.g - mean) * colorBoost
        var blue = mean + (color.b - mean) * colorBoost

        val trueBlack = color.r <= 2 && color.g <= 2 && color.b <= 2
        val oledLike = deviceProfile == DeviceProfile.BRAVIA || deviceProfile == DeviceProfile.WIDE_GAMUT_HDR
        val effectiveFloor = when {
            trueBlack -> 0.0
            oledLike -> minBrightness * 0.35
            else -> minBrightness.toDouble()
        }
        val peak = max(1.0, max(red, max(green, blue)))
        if (peak < effectiveFloor) {
            val scale = effectiveFloor / peak
            red *= scale
            green *= scale
            blue *= scale
        }
        return RgbColor(
            red.roundToInt().coerceIn(0, 255),
            green.roundToInt().coerceIn(0, 255),
            blue.roundToInt().coerceIn(0, 255),
        )
    }

    private fun applyCinematicPaletteHold(target: RgbColor, previous: RgbColor?, motionScore: Double): RgbColor {
        previous ?: run {
            paletteHoldFramesCount = 0
            return target
        }
        val targetSaturation = colorSaturation(target)
        val previousSaturation = colorSaturation(previous)
        val targetLuma = colorLuma(target)
        val maxHoldFrames = when (deviceProfile) {
            DeviceProfile.BRAVIA -> 18
            DeviceProfile.WIDE_GAMUT_HDR -> 24
            DeviceProfile.GENERIC -> 36
        }
        if (paletteHoldFramesCount >= maxHoldFrames) {
            paletteHoldFramesCount = 0
            return target
        }
        if (targetLuma < 0.035) {
            paletteHoldFramesCount = 0
            return target
        }
        if (motionScore > 0.045) {
            paletteHoldFramesCount = 0
            return target
        }
        if (previousSaturation < 0.12) {
            paletteHoldFramesCount = 0
            return target
        }
        if (targetSaturation >= previousSaturation * 0.92) {
            paletteHoldFramesCount = 0
            return target
        }
        var holdStrength = min(0.34, max(0.0, (previousSaturation - targetSaturation) * 0.85))
        holdStrength *= max(0.0, 1.0 - min(1.0, motionScore / 0.03))
        if (targetSaturation < 0.12 && targetLuma < 0.55) {
            holdStrength = min(0.4, holdStrength + 0.08)
        }
        if (holdStrength <= 0.01) {
            paletteHoldFramesCount = 0
            return target
        }
        paletteHoldFramesCount += 1
        return blendColors(target, previous, holdStrength)
    }

    private fun applyPredictiveTarget(ip: String, target: RgbColor, motionScore: Double): RgbColor {
        if (!predictiveSmoothing) {
            predictionTargets[ip] = target
            return target
        }
        val previous = predictionTargets[ip]
        predictionTargets[ip] = target
        if (previous == null) {
            return target
        }
        val normalizedMotion = min(1.0, motionScore / 0.05)
        val weight = predictionWeight * (1.0 - normalizedMotion)
        if (weight <= 0.0) {
            return target
        }
        val predicted = RgbColor(
            (target.r + (target.r - previous.r) * weight).roundToInt().coerceIn(0, 255),
            (target.g + (target.g - previous.g) * weight).roundToInt().coerceIn(0, 255),
            (target.b + (target.b - previous.b) * weight).roundToInt().coerceIn(0, 255),
        )
        return blendColors(target, predicted, weight)
    }

    private fun smoothColor(current: RgbColor, target: RgbColor, factor: Double): RgbColor {
        return RgbColor(
            (current.r + (target.r - current.r) * factor).roundToInt().coerceIn(0, 255),
            (current.g + (target.g - current.g) * factor).roundToInt().coerceIn(0, 255),
            (current.b + (target.b - current.b) * factor).roundToInt().coerceIn(0, 255),
        )
    }

    private fun averageColors(colors: List<RgbColor>): RgbColor {
        if (colors.isEmpty()) {
            return RgbColor(128, 128, 128)
        }
        var totalR = 0
        var totalG = 0
        var totalB = 0
        for (color in colors) {
            totalR += color.r
            totalG += color.g
            totalB += color.b
        }
        return RgbColor(totalR / colors.size, totalG / colors.size, totalB / colors.size)
    }

    private fun colorDistance(first: RgbColor, second: RgbColor): Int {
        return abs(first.r - second.r) + abs(first.g - second.g) + abs(first.b - second.b)
    }

    private fun colorSaturation(color: RgbColor): Double = rgbSaturation(color.r, color.g, color.b)

    fun colorLuma(color: RgbColor): Double = rgbLuma(color.r, color.g, color.b)

    fun colorLuma(pixel: Int): Double = rgbLuma(Color.red(pixel), Color.green(pixel), Color.blue(pixel))

    private fun rgbSaturation(r: Int, g: Int, b: Int): Double {
        val maxChannel = max(r, max(g, b)) / 255.0
        val minChannel = min(r, min(g, b)) / 255.0
        return maxChannel - minChannel
    }

    private fun rgbLuma(r: Int, g: Int, b: Int): Double {
        return (r * 0.2126 + g * 0.7152 + b * 0.0722) / 255.0
    }

    // ── Adaptive cadence ─────────────────────────────────────────────────────────
    private fun updateAdaptiveFps(motionScore: Double) {
        if (!adaptiveFps) {
            currentFps = maxFps
            return
        }
        val normalized = min(1.0, motionScore / 0.05)
        currentFps = (minFps + (maxFps - minFps) * normalized).roundToInt().coerceIn(minFps, maxFps)
    }

    private fun targetSmoothing(motionScore: Double): Double {
        val normalized = min(1.0, motionScore / 0.03)
        var factor = smoothing * (0.72 + normalized * 0.9)
        if (effectiveMode == "single") {
            factor *= 0.95 + normalized * 0.2
        }
        return factor.coerceIn(0.06, 0.9)
    }

    private fun sendDeltaThreshold(motionScore: Double): Int {
        if (!adaptiveFps) {
            return minColorDelta
        }
        val motionFactor = min(1.0, motionScore / 0.05)
        val cap = when (deviceProfile) {
            DeviceProfile.BRAVIA -> 4.0
            DeviceProfile.WIDE_GAMUT_HDR -> 5.0
            DeviceProfile.GENERIC -> 6.0
        }
        val staticExtra = ((1.0 - motionFactor) * cap).roundToInt()
        return max(minColorDelta, minColorDelta + staticExtra)
    }

    // ── WiZ basis projection + CCT cold/warm split ──────────────────────────────
    private fun estimateCCT(r: Double, g: Double, b: Double): Double {
        val rl = srgbDecode(r)
        val gl = srgbDecode(g)
        val bl = srgbDecode(b)
        val X = 0.4124564 * rl + 0.3575761 * gl + 0.1804375 * bl
        val Y = 0.2126729 * rl + 0.7151522 * gl + 0.0721750 * bl
        val Z = 0.0193339 * rl + 0.1191920 * gl + 0.9503041 * bl
        val sum = X + Y + Z
        if (sum <= 1.0e-6) return 5500.0
        val x = X / sum
        val y = Y / sum
        val denom = 0.1858 - y
        if (abs(denom) < 1.0e-6) return 5500.0
        val n = (x - 0.3320) / denom
        val cct = 437.0 * n * n * n + 3601.0 * n * n + 6861.0 * n + 5517.0
        return cct.coerceIn(1500.0, 15000.0)
    }

    private fun computeCoolMix(r: Double, g: Double, b: Double): Double {
        val maxC = max(r, max(g, b))
        if (maxC < 1.0e-3) return 0.5
        val cct = estimateCCT(r, g, b)
        var mix = ((cct - 2700.0) / (6500.0 - 2700.0)).coerceIn(0.0, 1.0)
        if (deviceProfile == DeviceProfile.BRAVIA) {
            mix = (mix + 0.10).coerceIn(0.0, 1.0)
        } else if (deviceProfile == DeviceProfile.WIDE_GAMUT_HDR) {
            mix = (mix + 0.05).coerceIn(0.0, 1.0)
        }
        return mix
    }

    private fun mapToWizPilotColor(color: RgbColor): WizPilotColor {
        val red = color.r / 255.0
        val green = color.g / 255.0
        val blue = color.b / 255.0

        var hueX = wizBasis[0][0] * red + wizBasis[1][0] * green + wizBasis[2][0] * blue
        var hueY = wizBasis[0][1] * red + wizBasis[1][1] * green + wizBasis[2][1] * blue

        val epsilon = 1.0e-5
        val saturation = sqrt(hueX * hueX + hueY * hueY)
        if (saturation > epsilon) {
            hueX /= saturation
            hueY /= saturation
        }

        val rgb = DoubleArray(3)
        if (saturation > epsilon) {
            val maxAngle = cos((2.0 * PI / 3.0) - epsilon)
            val mask = IntArray(3) { index ->
                if (hueX * wizBasis[index][0] + hueY * wizBasis[index][1] > maxAngle) 1 else 0
            }
            val activeCount = mask.sum()
            if (activeCount <= 1) {
                if (activeCount == 0) {
                    val channels = listOf(red, green, blue)
                    val maxChannelIndex = channels.indices.maxByOrNull { channels[it] } ?: 0
                    rgb[maxChannelIndex] = 1.0
                } else {
                    rgb[0] = mask[0].toDouble()
                    rgb[1] = mask[1].toDouble()
                    rgb[2] = mask[2].toDouble()
                }
            } else {
                val active = mask.indices.filter { mask[it] == 1 }
                val first = wizBasis[active.first()]
                val second = wizBasis[active.last()]
                val abX = second[1]
                val abY = -second[0]
                val coeff0 = (hueX * abX + hueY * abY) / (first[0] * abX + first[1] * abY)
                val intersectionX = hueX - first[0] * coeff0
                val intersectionY = hueY - first[1] * coeff0
                val coeff1 = intersectionX * second[0] + intersectionY * second[1]
                val maxCoeff = max(coeff0, coeff1).coerceAtLeast(epsilon)
                val scaled0 = coeff0 / maxCoeff
                val scaled1 = coeff1 / maxCoeff
                var activeIndex = 0
                for (index in 0..2) {
                    if (mask[index] == 1) {
                        rgb[index] = min(1.0, if (activeIndex == 0) scaled0 else scaled1)
                        activeIndex += 1
                    }
                }
            }
        }

        val inputLuma = (red * 0.2126 + green * 0.7152 + blue * 0.0722).coerceIn(0.0, 1.0)
        val saturationGate = when (deviceProfile) {
            DeviceProfile.BRAVIA -> 0.08
            DeviceProfile.WIDE_GAMUT_HDR -> 0.09
            DeviceProfile.GENERIC -> 0.10
        }
        val whiteFraction = if (saturation < saturationGate && inputLuma > 0.05) {
            ((saturationGate - saturation) / saturationGate).coerceIn(0.0, 1.0)
        } else {
            0.0
        }
        if (whiteFraction > 0.0) {
            val scale = (saturation / saturationGate).coerceIn(0.0, 1.0)
            rgb[0] *= scale
            rgb[1] *= scale
            rgb[2] *= scale
        }
        val whiteLevel = (whiteFraction * inputLuma * 150.0).roundToInt().coerceIn(0, 150)
        val coolMix = computeCoolMix(red, green, blue)
        val coldWhite = (whiteLevel * coolMix).roundToInt().coerceIn(0, 150)
        val warmWhite = (whiteLevel - coldWhite).coerceIn(0, 150)

        return WizPilotColor(
            rgb = RgbColor(
                (rgb[0] * 255.0).roundToInt().coerceIn(0, 255),
                (rgb[1] * 255.0).roundToInt().coerceIn(0, 255),
                (rgb[2] * 255.0).roundToInt().coerceIn(0, 255),
            ),
            coldWhite = coldWhite,
            warmWhite = warmWhite,
        )
    }

    private fun sendColor(ip: String, color: RgbColor) {
        val mapped = mapToWizPilotColor(color)
        val isOn = mapped.rgb.r > 0 || mapped.rgb.g > 0 || mapped.rgb.b > 0 ||
            mapped.coldWhite > 0 || mapped.warmWhite > 0
        val cacheKey =
            "${if (isOn) 1 else 0}:${mapped.rgb.r}:${mapped.rgb.g}:${mapped.rgb.b}:${mapped.coldWhite}:${mapped.warmWhite}"
        val payload = packetCache.getOrPut(cacheKey) {
            (
                """{"method":"setPilot","params":{"state":$isOn,""" +
                    """"r":${mapped.rgb.r},"g":${mapped.rgb.g},"b":${mapped.rgb.b},""" +
                    """"c":${mapped.coldWhite},"w":${mapped.warmWhite}}}"""
                ).toByteArray(StandardCharsets.UTF_8)
        }
        val address = addressCache.getOrPut(ip) { InetAddress.getByName(ip) }
        val packet = DatagramPacket(payload, payload.size, address, WIZ_PORT)
        udpSocket?.send(packet)
    }

    // ── HDR FP16/PQ pixel decode (used by MediaProjection FP16 capture) ──────────
    fun fp16PqPixelToSrgbArgb(rH: Short, gH: Short, bH: Short): Int {
        val r = halfToFloat(rH).toDouble().coerceIn(0.0, 1.0)
        val g = halfToFloat(gH).toDouble().coerceIn(0.0, 1.0)
        val b = halfToFloat(bH).toDouble().coerceIn(0.0, 1.0)
        val tone = hdrReinhardScale(doubleArrayOf(pqEotf(r), pqEotf(g), pqEotf(b)))
        val sR = REC2020_TO_SRGB[0][0] * tone[0] + REC2020_TO_SRGB[0][1] * tone[1] + REC2020_TO_SRGB[0][2] * tone[2]
        val sG = REC2020_TO_SRGB[1][0] * tone[0] + REC2020_TO_SRGB[1][1] * tone[1] + REC2020_TO_SRGB[1][2] * tone[2]
        val sB = REC2020_TO_SRGB[2][0] * tone[0] + REC2020_TO_SRGB[2][1] * tone[1] + REC2020_TO_SRGB[2][2] * tone[2]
        val r8 = (srgbEncode(max(0.0, sR).coerceAtMost(1.0)) * 255.0).roundToInt().coerceIn(0, 255)
        val g8 = (srgbEncode(max(0.0, sG).coerceAtMost(1.0)) * 255.0).roundToInt().coerceIn(0, 255)
        val b8 = (srgbEncode(max(0.0, sB).coerceAtMost(1.0)) * 255.0).roundToInt().coerceIn(0, 255)
        return Color.argb(255, r8, g8, b8)
    }

    private fun hdrReinhardScale(linearNitsRatio: DoubleArray): DoubleArray {
        val whitePoint = when (primaryHdrType) {
            "dolby_vision", "hdr10_plus", "hdr10" -> 4.0
            "hlg" -> 3.0
            else -> 3.5
        }
        val w2 = whitePoint * whitePoint
        return DoubleArray(3) { idx ->
            val x = max(0.0, linearNitsRatio[idx])
            ((x * (1.0 + x / w2)) / (1.0 + x)).coerceIn(0.0, 1.0)
        }
    }

    private fun halfToFloat(h: Short): Float {
        val bits = h.toInt() and 0xFFFF
        val sign = (bits ushr 15) and 0x1
        val exp = (bits ushr 10) and 0x1F
        val mant = bits and 0x3FF
        val f = when (exp) {
            0 -> if (mant == 0) 0f else mant * Math.pow(2.0, -24.0).toFloat()
            31 -> if (mant == 0) Float.POSITIVE_INFINITY else Float.NaN
            else -> ((mant + 1024) * Math.pow(2.0, (exp - 25).toDouble())).toFloat()
        }
        return if (sign == 1) -f else f
    }

    private fun pqEotf(v: Double): Double {
        val m1 = 0.1593017578125
        val m2 = 78.84375
        val c1 = 0.8359375
        val c2 = 18.8515625
        val c3 = 18.6875
        val ep = Math.pow(v.coerceIn(0.0, 1.0), 1.0 / m2)
        val num = max(0.0, ep - c1)
        val den = c2 - c3 * ep
        if (den <= 1.0e-9) return 0.0
        val l = Math.pow(num / den, 1.0 / m1)
        return (l * 100.0).coerceIn(0.0, 100.0)
    }
}
