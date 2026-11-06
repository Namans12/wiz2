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
    // HDR capture has already been converted to sRGB before grid sampling.
    var capturedPixelsAreSrgb = false
    var deviceProfile = DeviceProfile.GENERIC
    var bulbTargets: List<BulbTarget> = emptyList()
    var syncProfile = "auto"        // "auto" | "cinema" | "action"
    var brightnessFollow = true     // bulb brightness tracks scene luma

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
    private var prevLumaHist: DoubleArray? = null
    private val motionHistory = ArrayDeque<Double>()  // 5-frame moving average (desktop parity)
    private var lastSubmitAtMs = 0L
    var lastSceneCut = false
        private set
    var cutsDetected = 0
        private set
    var sceneNeutral = 0.0
        private set
    var sceneLuma = 0.0
        private set
    var captureLooksBlack = false
        private set
    private var blackSinceMs = 0L
    private var currentBrightnessFactor = 1.0
    private val dimmingPolicy = DimmingPolicy()
    var outputBrightness = 255
        private set
    private val lastSentBrightness = mutableMapOf<String, Int>()
    @Volatile var lastSendError: String? = null
        private set
    @Volatile var datagramsSent = 0
        private set
    @Volatile var sendErrors = 0
        private set
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
    private val refreshPolicy = PerLightRefresh()
    private var lastSendAt: Long? = null
    private val sendIntervalsMs = ArrayDeque<Long>()

    val currentOutputColors = mutableMapOf<String, RgbColor>()
    val lastSentColors = mutableMapOf<String, RgbColor>()
    val lastTargetColors = mutableMapOf<String, RgbColor>()
    private val predictionTargets = mutableMapOf<String, RgbColor>()
    private val packetCache = object : LinkedHashMap<String, ByteArray>(128, 0.75f, true) {
        override fun removeEldestEntry(eldest: MutableMap.MutableEntry<String, ByteArray>?): Boolean {
            return size > 4096
        }
    }
    private var udpSocket: DatagramSocket? = null
    private var outputSender: LatestWizSender? = null

    // Shared 33^3 RGBCW LUT baked from the desktop mapper (assets/wiz_rgbcw_lut_v1.bin).
    // When loaded, mapViaLut() interpolates it identically to desktop map_rgb so a
    // color produces the SAME bulb payload on TV and desktop. Falls back to the
    // hue-basis projection if the asset is missing.
    private var lut: IntArray? = null
    private var lutGray: IntArray? = null
    private var lutGrid = 33
    private var lutChannels = 5

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


    // 8-bit sRGB -> linear-light decode table. Extractor means accumulate in
    // linear light (light adds linearly; gamma-space means darken mixes) while
    // the scene-analysis weights stay in gamma space where they were tuned.
    private val srgb8ToLinear = DoubleArray(256) { srgbDecode(it / 255.0) }

    private fun linearMeanToSrgb(
        sumR: Double,
        sumG: Double,
        sumB: Double,
        totalWeight: Double,
    ): RgbColor {
        if (totalWeight <= 0.0) {
            return RgbColor(128, 128, 128)
        }
        return RgbColor(
            (srgbEncode((sumR / totalWeight).coerceIn(0.0, 1.0)) * 255.0).roundToInt().coerceIn(0, 255),
            (srgbEncode((sumG / totalWeight).coerceIn(0.0, 1.0)) * 255.0).roundToInt().coerceIn(0, 255),
            (srgbEncode((sumB / totalWeight).coerceIn(0.0, 1.0)) * 255.0).roundToInt().coerceIn(0, 255),
        )
    }

    // ── Lifecycle ──────────────────────────────────────────────────────────────
    fun start() {
        reset()
        val socket = DatagramSocket().apply { sendBufferSize = 262144 }
        udpSocket = socket
        outputSender = LatestWizSender(
            transmit = { ip, payload ->
                val address = InetAddress.getByName(ip)
                socket.send(DatagramPacket(payload, payload.size, address, WIZ_PORT))
            },
            onResult = { error ->
                if (error == null) {
                    datagramsSent += 1
                    lastSendError = null
                } else {
                    sendErrors += 1
                    lastSendError = error.message ?: error.toString()
                }
            },
        )
    }

    fun reset() {
        previousMotionSample = null
        previousSingleColor = null
        prevLumaHist = null
        motionHistory.clear()
        lastSubmitAtMs = 0L
        lastSceneCut = false
        cutsDetected = 0
        sceneNeutral = 0.0
        sceneLuma = 0.0
        captureLooksBlack = false
        blackSinceMs = 0L
        currentBrightnessFactor = 1.0
        dimmingPolicy.reset()
        outputBrightness = 255
        lastSentBrightness.clear()
        lastSendError = null
        datagramsSent = 0
        sendErrors = 0
        lastMotionScore = 0.0
        refreshPolicy.clear()
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
        packetCache.clear()
    }

    fun close() {
        // Closing the socket first ensures a blocked sender cannot transmit
        // after the session has stopped, even if joining the worker times out.
        udpSocket?.close()
        outputSender?.close()
        outputSender = null
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
        loadLut(context)

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

        // CAPTURE-SPACE BINDING (fixes DV/HDR "too bright / too dim / wrong hue"):
        // every pixel grid this engine receives is ALREADY sRGB —
        //  * MediaProjection RGBA_8888 virtual displays are composited to sRGB
        //    by SurfaceFlinger regardless of how wide the panel is, and the OS
        //    has already tone-mapped HDR content into that buffer;
        //  * the FP16 BT.2020-PQ path converts PQ→sRGB inside pixelAtFp16Pq
        //    (per-HDR-type curve applied there, where the real HDR pixels are);
        //  * the USB player's TextureView bitmaps are SDR sRGB.
        // Applying the panel's rec2020 matrix + a tone curve on top of that was
        // double-processing. Auto therefore binds to the buffer (sRGB, no tone
        // map); an explicit user selection is still honored for exotic setups.
        // Panel detection results stay in the stats and still drive the device
        // profile (Bravia cool bias, OLED true-black, FP16 curve choice).
        if (requestedColorSpace == "auto") {
            sourceColorSpace = "srgb"
            hdrToneMapping = false
        } else {
            sourceColorSpace = requestedColorSpace
            hdrToneMapping = requestedHdrToneMapping
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

    fun normalizeSyncProfile(raw: String?): String {
        return when (raw?.lowercase()) {
            "auto", "cinema", "action" -> raw.lowercase()
            else -> "auto"
        }
    }

    // ── Main per-frame entry ─────────────────────────────────────────────────────
    /**
     * @param grids region -> NxN ARGB grid. Single mode uses key "all"; zones uses
     *              "left"/"right". @return true if any bulb update was queued.
     */
    fun submitFrame(grids: Map<String, IntArray>, size: Int): Boolean {
        if (grids.isEmpty()) return false
        val nowMs = SystemClock.elapsedRealtime()
        val dtSeconds = if (lastSubmitAtMs == 0L) {
            1.0 / max(1, currentFps)
        } else {
            (nowMs - lastSubmitAtMs).coerceIn(5L, 500L) / 1000.0
        }
        lastSubmitAtMs = nowMs

        val primary = grids["all"] ?: grids["left"] ?: grids.values.first()
        val motionScore = computeMotionScore(primary)
        val sceneCut = lastSceneCut
        lastMotionScore = motionScore
        updateAdaptiveFps(motionScore)
        if (sceneCut) {
            // A cut invalidates palette memory and velocity history: snap.
            previousSingleColor = null
            predictionTargets.clear()
        }

        // Scene-level content adaptation (B&W / dim-scene handling): average
        // saturation drives neutral detection, average luma drives brightness
        // follow. Neutral scenes are pulled to true white (the WiZ mapper
        // routes low-saturation colors to the white LEDs); black turns off.
        // Scene stats must reflect the WHOLE frame, not just one region: in zone
        // mode grids = {left,right}, so averaging only the primary (left) region
        // would drive neutral / brightness-follow / black-off off half the screen.
        val statGrids = grids["all"]?.let { listOf(it) } ?: grids.values.toList()
        var satSum = 0.0
        var lumaSum = 0.0
        var valueSum = 0.0
        var pixelCount = 0
        for (grid in statGrids) {
            for (pixel in grid) {
                val r = Color.red(pixel)
                val g = Color.green(pixel)
                val b = Color.blue(pixel)
                satSum += rgbSaturation(r, g, b)
                lumaSum += rgbLuma(r, g, b)
                valueSum += max(r, max(g, b)) / 255.0
                pixelCount += 1
            }
        }
        val n = max(1, pixelCount).toDouble()
        val avgSat = satSum / n
        val avgLuma = lumaSum / n
        val avgValue = valueSum / n
        val tNeutral = ((0.06 - avgSat) / 0.06).coerceIn(0.0, 1.0)
        sceneNeutral = tNeutral * tNeutral * (3.0 - 2.0 * tNeutral)
        sceneLuma = avgLuma
        // Brightness-follow uses VALUE (avg max-channel), not luma: luma's low
        // blue/red weights wrongly dim full-intensity saturated colors.
        currentBrightnessFactor = Math.pow((avgValue / 0.32).coerceIn(0.0, 1.0), 0.8)

        // Black handling: turning the bulb off requires the black to PERSIST
        // (~0.7s) so brief fades don't flicker the power state — and because
        // DRM apps / HDMI inputs capture as permanent black, we surface a
        // "capture looks black" flag after 3s so the UI can explain that the
        // content is not capturable instead of silently going dark.
        val isBlackFrame = avgLuma < 0.015 && sceneNeutral > 0.7
        if (isBlackFrame) {
            if (blackSinceMs == 0L) blackSinceMs = nowMs
        } else {
            blackSinceMs = 0L
        }
        captureLooksBlack = isBlackFrame && (nowMs - blackSinceMs) > 3000L
        if (isBlackFrame && (!brightnessFollow || nowMs - blackSinceMs < 700L)) return false
        val extractedTargets: Map<String, RgbColor> =
            if (brightnessFollow && isBlackFrame && (nowMs - blackSinceMs) >= 700L) {
                // Sustained true black: payload of zeros -> state:false (off).
                grids.keys.associateWith { RgbColor(0, 0, 0) }
            } else {
                extractTargetColors(grids, size, motionScore)
            }
        val peak = extractedTargets.values.maxOfOrNull { max(it.r, max(it.g, it.b)) } ?: 0
        if (!brightnessFollow && peak < 24) return false
        outputBrightness = dimmingPolicy.value(avgValue, minBrightness, peak, brightnessFollow)
        val targetColors = extractedTargets.mapValues { (_, color) ->
            val rgb = WizOutputPolicy.normalize(intArrayOf(color.r, color.g, color.b))
            RgbColor(rgb[0], rgb[1], rgb[2])
        }
        lastTargetColors.clear()
        lastTargetColors.putAll(targetColors)

        val smoothingFactor = if (sceneCut) 1.0 else targetSmoothing(motionScore, dtSeconds)
        lastSmoothingFactor = smoothingFactor
        val sendThreshold = sendDeltaThreshold(motionScore)
        lastDeltaThreshold = sendThreshold
        val mappedBulbColors = buildBulbColorMap(targetColors)
        var sentAnyUpdate = false
        val pendingColors = linkedMapOf<String, RgbColor>()
        val pendingPayloads = linkedMapOf<String, ByteArray>()

        for ((ip, targetColor) in mappedBulbColors) {
            val current = currentOutputColors[ip]
            val predictedTarget = if (sceneCut) {
                predictionTargets[ip] = targetColor
                targetColor
            } else {
                applyPredictiveTarget(ip, targetColor, motionScore)
            }
            val smoothed = if (current == null || smoothingFactor >= 1.0) {
                predictedTarget
            } else {
                smoothColor(current, predictedTarget, smoothingFactor)
            }
            currentOutputColors[ip] = smoothed

            val lastSent = lastSentColors[ip]
            val needsForcedRefresh = lastSent != null && refreshPolicy.due(ip, nowMs)
            if (
                lastSent == null ||
                lastSentBrightness[ip] != outputBrightness ||
                sceneCut ||
                perceptualDelta(smoothed, lastSent) >= sendThreshold.toDouble() ||
                needsForcedRefresh
            ) {
                pendingColors[ip] = smoothed
                pendingPayloads[ip] = makeColorPayload(smoothed)
            }
        }
        if (pendingPayloads.isNotEmpty() && outputSender?.offerBatch(pendingPayloads) == true) {
            for ((ip, color) in pendingColors) {
                lastSentColors[ip] = color
                lastSentBrightness[ip] = outputBrightness
                refreshPolicy.markQueued(ip, nowMs)
            }
            sentAnyUpdate = true
        }
        val staleIps = currentOutputColors.keys - mappedBulbColors.keys
        for (ip in staleIps) {
            currentOutputColors.remove(ip)
            lastSentColors.remove(ip)
            predictionTargets.remove(ip)
            lastSentBrightness.remove(ip)
            refreshPolicy.remove(ip)
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
            "outputBrightness" to outputBrightness,
            // updatesSent is retained for older clients and counts queued groups.
            "updatesQueued" to updatesSent,
            "datagramsSent" to datagramsSent,
            "sendErrors" to sendErrors,
            "sendError" to lastSendError,
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
            "sceneCut" to lastSceneCut,
            "cutsDetected" to cutsDetected,
            "sceneNeutral" to sceneNeutral,
            "sceneLuma" to sceneLuma,
            "syncProfile" to syncProfile,
            "brightnessFollow" to brightnessFollow,
            "captureLooksBlack" to captureLooksBlack,
            "targetColors" to lastTargetColors.mapValues { listOf(it.value.r, it.value.g, it.value.b) },
            "outputColors" to currentOutputColors.mapValues { listOf(it.value.r, it.value.g, it.value.b) },
        )
    }

    // ── Motion + scene-cut detection ─────────────────────────────────────────────
    private fun computeMotionScore(pixels: IntArray): Double {
        val current = FloatArray(pixels.size)
        for (index in pixels.indices) {
            current[index] = colorLuma(pixels[index]).toFloat()
        }

        // Luma histogram for cut detection. A cut must move BOTH the histogram
        // (panning barely does) AND the instantaneous diff (near-solid fades
        // hop bins but diff stays tiny). Hard cuts move both by 10x.
        val hist = DoubleArray(32)
        for (v in current) {
            hist[(v * 31.999f).toInt().coerceIn(0, 31)] += 1.0
        }
        val n = max(1, current.size).toDouble()
        for (i in hist.indices) hist[i] /= n
        val prevHist = prevLumaHist
        var histTv = 0.0
        if (prevHist != null) {
            var s = 0.0
            for (i in hist.indices) s += abs(hist[i] - prevHist[i])
            histTv = 0.5 * s
        }
        prevLumaHist = hist

        val previous = previousMotionSample
        previousMotionSample = current
        if (previous == null || previous.size != current.size) {
            lastSceneCut = false
            return 0.0
        }
        var total = 0.0
        for (index in current.indices) {
            total += abs(current[index] - previous[index])
        }
        val diff = min(1.0, total / current.size.toDouble())
        // Scene cut uses the INSTANTANEOUS diff (a cut is a one-frame spike);
        // the returned motion score is a 5-frame moving average so adaptive
        // FPS / smoothing / delta don't oscillate every frame (desktop parity).
        lastSceneCut = histTv > 0.30 && diff > 0.10
        if (lastSceneCut) cutsDetected += 1
        motionHistory.addLast(diff)
        while (motionHistory.size > 5) motionHistory.removeFirst()
        return motionHistory.average()
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
            // No hold in action profile (kills responsiveness) or on neutral
            // scenes (holding tints B&W content with the previous color).
            val skipHold = syncProfile == "action" || sceneNeutral > 0.5
            val held = applyCinematicPaletteHold(
                enhanced,
                if (skipHold) null else previousSingleColor,
                motionScore,
            )
            previousSingleColor = held
            mapOf("all" to held)
        } else {
            previousSingleColor = null
            val targets = mutableMapOf<String, RgbColor>()
            for ((region, grid) in grids) {
                if (region == "all") continue
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
        var accentWeight = 0.10 + sceneEnergy * 0.22
        if (sceneEnergy < 0.35) {
            accentWeight *= 0.55
        }
        var reference = blendColors(weighted, accent, accentWeight)

        // Perceptual palette match (desktop extract_dominant_auto parity).
        val candidates = paletteCandidates(sample, topK = 6) { amb, sat, _ ->
            amb * (0.65 + sat * 1.35)
        }
        val balanceScore = paletteBalanceScore(candidates)
        val weightedSaturation = colorSaturation(weighted)
        if (balanceScore > 0.32 && weightedSaturation < 0.22) {
            val neutralPull = min(0.88, balanceScore * (0.55 + max(0.0, 0.22 - weightedSaturation) * 2.4))
            reference = blendColors(reference, weighted, neutralPull)
        }
        val matched = selectPaletteMatch(reference, candidates, preferVivid = sceneEnergy > 0.3)

        var paletteWeight = 0.12 + sceneEnergy * 0.28
        if (sceneEnergy < 0.2) {
            paletteWeight *= 0.45
        }
        if (balanceScore > 0.32 && weightedSaturation < 0.22) {
            paletteWeight *= max(0.15, 1.0 - balanceScore * 1.25)
        }
        return blendColors(reference, matched, paletteWeight)
    }

    private fun extractCinematicSingleColor(sample: SampledFrame): RgbColor {
        val weighted = extractWeightedColor(sample)
        val saturationMean = averageSaturation(sample)
        val vividRatio = vividRatio(sample, saturationThreshold = 0.32, lumaThreshold = 0.08)
        val darkness = averageLuma(sample)
        if (darkness < 0.03 && saturationMean < 0.04) {
            return weighted
        }
        val accent = extractVibrantAccent(sample)
        val paletteAnchor = extractPaletteAnchor(sample)

        var accentWeight = 0.18 + min(0.30, saturationMean * 0.5 + vividRatio * 0.22)
        if (saturationMean < 0.08 && darkness > 0.18) {
            accentWeight *= 0.8
        }
        var reference = blendColors(weighted, accent, accentWeight)
        reference = blendColors(reference, paletteAnchor, 0.08 + min(0.18, vividRatio * 0.24))

        // Perceptual palette match: quantize the sample into a small weighted
        // palette, then pull the ambient reference toward the closest prominent,
        // vivid palette entry (desktop extract_cinematic_single_color parity).
        val candidates = paletteCandidates(sample, topK = 7) { amb, sat, luma ->
            amb * (0.7 + sat * 1.8 + (0.6 - abs(luma - 0.42)).coerceIn(0.0, 0.6) * 0.3)
        }
        val balanceScore = paletteBalanceScore(candidates)
        val weightedSaturation = colorSaturation(weighted)
        if (balanceScore > 0.3 && weightedSaturation < 0.22) {
            val neutralPull = min(0.9, balanceScore * (0.6 + max(0.0, 0.22 - weightedSaturation) * 2.6))
            reference = blendColors(reference, weighted, neutralPull)
        }
        val matched = selectPaletteMatch(reference, candidates, preferVivid = true)

        var paletteWeight = 0.20 + min(0.34, saturationMean * 0.6 + vividRatio * 0.3)
        if (saturationMean < 0.12) {
            paletteWeight *= 0.55
        }
        if (balanceScore > 0.3 && weightedSaturation < 0.22) {
            paletteWeight *= max(0.12, 1.0 - balanceScore * 1.35)
        }
        return blendColors(reference, matched, paletteWeight)
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
                totalR += srgb8ToLinear[r] * weight
                totalG += srgb8ToLinear[g] * weight
                totalB += srgb8ToLinear[b] * weight
                totalWeight += weight
            }
        }
        return linearMeanToSrgb(totalR, totalG, totalB, totalWeight)
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
            totalR += srgb8ToLinear[Color.red(pixel)]
            totalG += srgb8ToLinear[Color.green(pixel)]
            totalB += srgb8ToLinear[Color.blue(pixel)]
            count += 1
        }
        if (count == 0) {
            return extractWeightedColor(sample)
        }
        return linearMeanToSrgb(totalR, totalG, totalB, count.toDouble())
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
            totalR += srgb8ToLinear[Color.red(pixel)]
            totalG += srgb8ToLinear[Color.green(pixel)]
            totalB += srgb8ToLinear[Color.blue(pixel)]
            count += 1
        }
        if (count == 0) {
            return extractVibrantAccent(sample)
        }
        return linearMeanToSrgb(totalR, totalG, totalB, count.toDouble())
    }

    private data class PaletteEntry(val color: RgbColor, val weight: Double)

    /**
     * Build a small weighted palette from quantized sampled pixels, ranked by
     * weight (desktop _palette_candidates parity). [paletteWeight] maps
     * (ambientWeight, saturation, luma) -> the per-pixel contribution so each
     * extractor can bias the palette differently. Bins are 32-wide (8 levels /
     * channel), matching the desktop bin_size=32 / levels=8 quantization.
     */
    private fun paletteCandidates(
        sample: SampledFrame,
        topK: Int,
        paletteWeight: (ambient: Double, saturation: Double, luma: Double) -> Double,
    ): List<PaletteEntry> {
        val binSize = 32
        val border = max(1, min(sample.width, sample.height) / 6)
        val counts = HashMap<Int, Double>()
        val sumR = HashMap<Int, Double>()
        val sumG = HashMap<Int, Double>()
        val sumB = HashMap<Int, Double>()
        for (y in 0 until sample.height) {
            for (x in 0 until sample.width) {
                val pixel = sample.pixels[y * sample.width + x]
                val r = Color.red(pixel)
                val g = Color.green(pixel)
                val b = Color.blue(pixel)
                val sat = rgbSaturation(r, g, b)
                val luma = rgbLuma(r, g, b)
                var ambient = 0.15 + sat * 2.2 + luma * 0.5
                if (x < border || y < border || x >= sample.width - border || y >= sample.height - border) {
                    ambient *= edgeWeight
                }
                val w = paletteWeight(ambient, sat, luma)
                if (w <= 0.0) continue
                val qr = (r / binSize).coerceIn(0, 7)
                val qg = (g / binSize).coerceIn(0, 7)
                val qb = (b / binSize).coerceIn(0, 7)
                val key = (qr shl 6) or (qg shl 3) or qb
                counts[key] = (counts[key] ?: 0.0) + w
                sumR[key] = (sumR[key] ?: 0.0) + r * w
                sumG[key] = (sumG[key] ?: 0.0) + g * w
                sumB[key] = (sumB[key] ?: 0.0) + b * w
            }
        }
        return counts.entries
            .sortedByDescending { it.value }
            .take(topK)
            .mapNotNull { entry ->
                val w = entry.value
                if (w <= 0.0) return@mapNotNull null
                // Truncate (toInt), NOT round — desktop uses int(np.clip(...)); a
                // 1-LSB difference here would shift the downstream match/balance scores.
                PaletteEntry(
                    RgbColor(
                        (sumR[entry.key]!! / w).toInt().coerceIn(0, 255),
                        (sumG[entry.key]!! / w).toInt().coerceIn(0, 255),
                        (sumB[entry.key]!! / w).toInt().coerceIn(0, 255),
                    ),
                    w,
                )
            }
    }

    /** Choose the closest palette entry by perceptual distance + prominence + vividness. */
    private fun selectPaletteMatch(
        reference: RgbColor,
        candidates: List<PaletteEntry>,
        preferVivid: Boolean,
    ): RgbColor {
        if (candidates.isEmpty()) return reference
        val maxWeight = candidates.maxOf { it.weight }.let { if (it <= 0.0) 1.0 else it }
        val referenceLuma = colorLuma(reference)
        var bestColor = reference
        var bestScore: Double? = null
        for (candidate in candidates) {
            val prominence = candidate.weight / maxWeight
            val saturation = colorSaturation(candidate.color)
            val luma = colorLuma(candidate.color)
            var score = perceptualColorDistance(candidate.color, reference)
            score -= prominence * 52.0
            score -= saturation * (if (preferVivid) 78.0 else 42.0)
            score += abs(luma - referenceLuma) * 24.0
            if (referenceLuma > 0.08 && luma < 0.02) {
                score += 18.0
            }
            if (bestScore == null || score < bestScore!!) {
                bestScore = score
                bestColor = candidate.color
            }
        }
        return bestColor
    }

    /** Estimate whether a frame holds several equally strong, distinct colors (0..1). */
    private fun paletteBalanceScore(candidates: List<PaletteEntry>): Double {
        if (candidates.size < 3) return 0.0
        val top = candidates.take(3)
        val w0 = top[0].weight
        if (w0 <= 0.0) return 0.0
        val prominence = (top[1].weight / w0 + top[2].weight / w0) / 2.0
        var separationSum = 0.0
        var pairs = 0
        for (i in 0 until 3) {
            for (j in i + 1 until 3) {
                separationSum += perceptualColorDistance(top[i].color, top[j].color)
                pairs += 1
            }
        }
        val separation = separationSum / pairs
        return min(1.0, prominence * min(1.0, separation / 170.0))
    }

    /** Redmean perceptual distance (desktop perceptual_color_distance parity). */
    private fun perceptualColorDistance(a: RgbColor, b: RgbColor): Double {
        val rMean = (a.r + b.r) / 2.0
        val dr = (a.r - b.r).toDouble()
        val dg = (a.g - b.g).toDouble()
        val db = (a.b - b.b).toDouble()
        return sqrt(
            (2.0 + rMean / 256.0) * dr * dr +
                4.0 * dg * dg +
                (2.0 + (255.0 - rMean) / 256.0) * db * db,
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
        if (capturedPixelsAreSrgb) return color
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
        // Neutral scenes suppress the color boost (boost amplifies faint tints
        // in B&W film) and are desaturated toward true gray so the WiZ mapper
        // routes them to the white LEDs. Brightness-follow scales dim scenes
        // down instead of pinning them at the minimum-brightness floor.
        val neutral = sceneNeutral
        // Saturation-aware boost (desktop adaptive_color_boost parity): damp the
        // boost on low-saturation colors so already-vivid frames aren't
        // over-saturated, THEN suppress further on neutral/B&W scenes.
        val chromaPeak = max(color.r, max(color.g, color.b))
        val sat = if (chromaPeak <= 0) 0.0 else (chromaPeak - min(color.r, min(color.g, color.b))).toDouble() / chromaPeak
        val strength = min(0.45, sat * 0.7)
        val adaptiveBoost = 1.0 + (colorBoost - 1.0) * strength
        val boostEff = 1.0 + (adaptiveBoost - 1.0) * (1.0 - neutral)
        lastEffectiveColorBoost = boostEff
        val mean = (color.r + color.g + color.b) / 3.0
        var red = mean + (color.r - mean) * boostEff
        var green = mean + (color.g - mean) * boostEff
        var blue = mean + (color.b - mean) * boostEff

        if (neutral > 0.0) {
            val gray = red * 0.2126 + green * 0.7152 + blue * 0.0722
            red += (gray - red) * neutral
            green += (gray - green) * neutral
            blue += (gray - blue) * neutral
        }
        return RgbColor(
            red.roundToInt().coerceIn(0, 255),
            green.roundToInt().coerceIn(0, 255),
            blue.roundToInt().coerceIn(0, 255),
        )
    }

    /**
     * Keep some prior hue in calm, low-saturation scenes without lagging on cuts.
     * Mirrors desktop apply_cinematic_palette_hold EXACTLY — no frame cap. The
     * hold is a bounded feedback blend (strength <= 0.4) against the previous
     * output, so it converges/releases smoothly on its own as the scene changes.
     * The old device-specific frame cap force-released after N frames, producing
     * a visible hue "pop" on long calm scenes that desktop never had.
     */
    private fun applyCinematicPaletteHold(target: RgbColor, previous: RgbColor?, motionScore: Double): RgbColor {
        previous ?: return target
        val targetSaturation = colorSaturation(target)
        val previousSaturation = colorSaturation(previous)
        val targetLuma = colorLuma(target)
        if (targetLuma < 0.035) return target
        if (motionScore > 0.045) return target
        if (previousSaturation < 0.12) return target
        if (targetSaturation >= previousSaturation * 0.92) return target
        var holdStrength = min(0.34, max(0.0, (previousSaturation - targetSaturation) * 0.85))
        holdStrength *= max(0.0, 1.0 - min(1.0, motionScore / 0.03))
        if (targetSaturation < 0.12 && targetLuma < 0.55) {
            holdStrength = min(0.4, holdStrength + 0.08)
        }
        if (holdStrength <= 0.01) return target
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
        val normalizedMotion = min(1.0, motionScore / 0.06)  // desktop: mt*4 = 0.06
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
        // Perceptually-uniform Oklab lerp (gamma-RGB lerp grays out between
        // hues) with a hard snap once within ~0.4 JND so transitions actually
        // finish instead of trailing an asymptotic EMA tail.
        if (factor >= 1.0) return target
        val out = lerpOklab(current, target, factor)
        return if (perceptualDelta(out, target) < 1.6) target else out
    }

    // ── Oklab perceptual space (Björn Ottosson; parity with desktop color_science) ──
    private fun srgbToOklab(color: RgbColor): DoubleArray {
        val r = srgbDecode(color.r / 255.0)
        val g = srgbDecode(color.g / 255.0)
        val b = srgbDecode(color.b / 255.0)
        val l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b)
        val m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b)
        val s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b)
        return doubleArrayOf(
            0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
            1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
            0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s,
        )
    }

    private fun oklabToSrgb(lab: DoubleArray): RgbColor {
        val lc = lab[0] + 0.3963377774 * lab[1] + 0.2158037573 * lab[2]
        val mc = lab[0] - 0.1055613458 * lab[1] - 0.0638541728 * lab[2]
        val sc = lab[0] - 0.0894841775 * lab[1] - 1.2914855480 * lab[2]
        val l = lc * lc * lc
        val m = mc * mc * mc
        val s = sc * sc * sc
        val r = 4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s
        val g = -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s
        val b = -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s
        return RgbColor(
            (srgbEncode(r.coerceIn(0.0, 1.0)) * 255.0).roundToInt().coerceIn(0, 255),
            (srgbEncode(g.coerceIn(0.0, 1.0)) * 255.0).roundToInt().coerceIn(0, 255),
            (srgbEncode(b.coerceIn(0.0, 1.0)) * 255.0).roundToInt().coerceIn(0, 255),
        )
    }

    private fun lerpOklab(current: RgbColor, target: RgbColor, t: Double): RgbColor {
        val tt = t.coerceIn(0.0, 1.0)
        if (tt >= 1.0) return target
        if (tt <= 0.0) return current
        val a = srgbToOklab(current)
        val b = srgbToOklab(target)
        return oklabToSrgb(
            doubleArrayOf(
                a[0] + (b[0] - a[0]) * tt,
                a[1] + (b[1] - a[1]) * tt,
                a[2] + (b[2] - a[2]) * tt,
            ),
        )
    }

    /** Oklab distance on the legacy delta scale (black->white = 400, JND ~8). */
    private fun perceptualDelta(a: RgbColor, b: RgbColor): Double {
        val la = srgbToOklab(a)
        val lb = srgbToOklab(b)
        val d0 = la[0] - lb[0]
        val d1 = la[1] - lb[1]
        val d2 = la[2] - lb[2]
        return sqrt(d0 * d0 + d1 * d1 + d2 * d2) * 400.0
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

    private fun targetSmoothing(motionScore: Double, dtSeconds: Double): Double {
        // Frame-rate-independent settling model (parity with desktop V2):
        // the smoothing setting maps to a settle window (0.2 -> 160 ms) where
        // the output reaches ~95% of a new target regardless of adaptive FPS.
        var settle = max(0.03, smoothing * 0.8)
        // Divisor matches desktop: motion_threshold(0.015) * 3 = 0.045.
        val normalized = min(1.0, motionScore / 0.045)
        settle *= 1.0 - 0.55 * normalized
        // Sync profile: cinema favors long calm transitions; action favors
        // near-instant response for games / fast content.
        when (syncProfile) {
            "cinema" -> settle *= 1.7
            "action" -> settle *= 0.45
        }
        val dt = dtSeconds.coerceIn(0.005, 0.5)
        if (settle <= dt) return 1.0
        return (1.0 - exp(-3.0 * dt / settle)).coerceIn(0.05, 1.0)
    }

    private fun sendDeltaThreshold(motionScore: Double): Int {
        if (!adaptiveFps) {
            return minColorDelta
        }
        val motionFactor = min(1.0, motionScore / 0.06)  // desktop: mt*4 = 0.06
        val cap = when (syncProfile) {
            "cinema" -> 8.0
            "action" -> 2.0
            else -> when (deviceProfile) {
                DeviceProfile.BRAVIA -> 4.0
                DeviceProfile.WIDE_GAMUT_HDR -> 5.0
                DeviceProfile.GENERIC -> 6.0
            }
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

    private fun loadLut(context: Context) {
        if (lut != null) return  // already loaded
        try {
            val bytes = context.assets.open("wiz_rgbcw_lut_v1.bin").use { it.readBytes() }
            // magic(6) grid(i32) channels(i32) lut(bytes) grayN(i32) gray(bytes)
            var off = 6
            fun i32(): Int {
                val v = (bytes[off].toInt() and 0xFF) or
                    ((bytes[off + 1].toInt() and 0xFF) shl 8) or
                    ((bytes[off + 2].toInt() and 0xFF) shl 16) or
                    ((bytes[off + 3].toInt() and 0xFF) shl 24)
                off += 4
                return v
            }
            if (bytes.size < 14 ||
                bytes[0].toInt() != 'W'.code || bytes[1].toInt() != 'Z'.code
            ) {
                return
            }
            val grid = i32()
            val channels = i32()
            val lutSize = grid * grid * grid * channels
            val lutArr = IntArray(lutSize) { bytes[off + it].toInt() and 0xFF }
            off += lutSize
            val grayN = i32()
            val grayArr = IntArray(grayN * channels) { bytes[off + it].toInt() and 0xFF }
            lutGrid = grid
            lutChannels = channels
            lut = lutArr
            lutGray = grayArr
        } catch (_: Throwable) {
            lut = null
            lutGray = null
        }
    }

    /** Trilinear RGBCW lookup identical to desktop map_rgb (LUT + gray ramp). */
    private fun mapViaLut(color: RgbColor): WizPilotColor {
        val lutArr = lut ?: return mapToWizPilotColor(color)
        val ch = lutChannels
        val gmax = lutGrid - 1
        val fr = color.r / 255.0 * gmax
        val fg = color.g / 255.0 * gmax
        val fb = color.b / 255.0 * gmax
        val ri0 = fr.toInt(); val gi0 = fg.toInt(); val bi0 = fb.toInt()
        val ri1 = min(ri0 + 1, gmax); val gi1 = min(gi0 + 1, gmax); val bi1 = min(bi0 + 1, gmax)
        val tr = fr - ri0; val tg = fg - gi0; val tb = fb - bi0

        fun idx(ri: Int, gi: Int, bi: Int) = ((ri * lutGrid + gi) * lutGrid + bi) * ch
        val i000 = idx(ri0, gi0, bi0); val i001 = idx(ri0, gi0, bi1)
        val i010 = idx(ri0, gi1, bi0); val i011 = idx(ri0, gi1, bi1)
        val i100 = idx(ri1, gi0, bi0); val i101 = idx(ri1, gi0, bi1)
        val i110 = idx(ri1, gi1, bi0); val i111 = idx(ri1, gi1, bi1)

        val out = DoubleArray(ch)
        for (k in 0 until ch) {
            val c00 = lutArr[i000 + k] + (lutArr[i001 + k] - lutArr[i000 + k]) * tb
            val c01 = lutArr[i010 + k] + (lutArr[i011 + k] - lutArr[i010 + k]) * tb
            val c10 = lutArr[i100 + k] + (lutArr[i101 + k] - lutArr[i100 + k]) * tb
            val c11 = lutArr[i110 + k] + (lutArr[i111 + k] - lutArr[i110 + k]) * tb
            val c0 = c00 + (c01 - c00) * tg
            val c1 = c10 + (c11 - c10) * tg
            out[k] = c0 + (c1 - c0) * tr
        }

        // Near-neutral inputs blend toward the exact-gray ramp (desktop parity).
        val maxc = max(color.r, max(color.g, color.b))
        val grayArr = lutGray
        if (maxc > 0 && grayArr != null) {
            val sat = (maxc - min(color.r, min(color.g, color.b))).toDouble() / maxc
            if (sat < 0.12) {
                val t = min(1.0, (0.12 - sat) / 0.08)
                val blend = t * t * (3.0 - 2.0 * t)
                val luma = (color.r * 0.2126 + color.g * 0.7152 + color.b * 0.0722)
                    .roundToInt().coerceIn(0, 255)
                for (k in 0 until ch) {
                    out[k] += (grayArr[luma * ch + k] - out[k]) * blend
                }
            }
        }

        val pr = out[0].roundToInt().coerceIn(0, 255)
        val pg = out[1].roundToInt().coerceIn(0, 255)
        val pb = out[2].roundToInt().coerceIn(0, 255)
        var cold = if (ch >= 5) out[3].roundToInt().coerceIn(0, 255) else 0
        var warm = if (ch >= 5) out[4].roundToInt().coerceIn(0, 255) else if (ch == 4) out[3].roundToInt().coerceIn(0, 255) else 0

        // Per-device cool bias applied post-hoc (single generic LUT serves all).
        val bias = when (deviceProfile) {
            DeviceProfile.BRAVIA -> 0.10
            DeviceProfile.WIDE_GAMUT_HDR -> 0.05
            DeviceProfile.GENERIC -> 0.0
        }
        if (bias != 0.0) {
            val total = cold + warm
            if (total > 0) {
                cold = (cold + total * bias).roundToInt().coerceIn(0, total)
                warm = total - cold
            }
        }
        return WizPilotColor(RgbColor(pr, pg, pb), cold, warm)
    }

    private fun makeColorPayload(color: RgbColor): ByteArray {
        val mapped = mapViaLut(color)
        val channels = intArrayOf(mapped.rgb.r, mapped.rgb.g, mapped.rgb.b, mapped.coldWhite, mapped.warmWhite)
        val cacheKey = channels.joinToString(":") + ":$outputBrightness"
        val payload = packetCache.getOrPut(cacheKey) {
            WizOutputPolicy.packet(channels, outputBrightness).toByteArray(StandardCharsets.UTF_8)
        }
        return payload
    }

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
