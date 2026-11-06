package com.wizlight.app

import android.app.Activity
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.graphics.Color
import android.graphics.PixelFormat
import android.graphics.Rect
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.Image
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Handler
import android.os.HandlerThread
import android.os.IBinder
import android.os.Looper
import android.os.SystemClock
import android.util.DisplayMetrics
import android.view.WindowManager
import androidx.core.app.NotificationCompat
import org.json.JSONArray
import java.util.ArrayDeque
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToInt

/**
 * Screen-capture front-end: mirrors the display via MediaProjection, samples each
 * frame into an NxN ARGB grid, and hands it to [ColorSyncEngine] (the shared color
 * pipeline). All color math, gamut/HDR mapping, smoothing and bulb-driving live in
 * the engine — this class only owns capture + sampling + the status bridge.
 */
class ScreenCaptureService : Service() {

    companion object {
        const val ACTION_START = "com.wizlight.app.START_CAPTURE"
        const val ACTION_STOP = "com.wizlight.app.STOP_CAPTURE"

        const val EXTRA_BULBS_JSON = "bulbsJson"
        const val EXTRA_MODE = "mode"
        const val EXTRA_MIN_FPS = "minFps"
        const val EXTRA_MAX_FPS = "maxFps"
        const val EXTRA_SMOOTHING = "smoothing"
        const val EXTRA_SAMPLE_SIZE = "sampleSize"
        const val EXTRA_COLOR_BOOST = "colorBoost"
        const val EXTRA_EDGE_WEIGHT = "edgeWeight"
        const val EXTRA_COLOR_ALGORITHM = "colorAlgorithm"
        const val EXTRA_MIN_BRIGHTNESS = "minBrightness"
        const val EXTRA_MIN_COLOR_DELTA = "minColorDelta"
        const val EXTRA_ADAPTIVE_FPS = "adaptiveFps"
        const val EXTRA_PREDICTIVE_SMOOTHING = "predictiveSmoothing"
        const val EXTRA_IGNORE_LETTERBOX = "ignoreLetterbox"
        const val EXTRA_SOURCE_COLOR_SPACE = "sourceColorSpace"
        const val EXTRA_HDR_TONE_MAPPING = "hdrToneMapping"
        const val EXTRA_HDR_CAPTURE = "hdrCapture"
        const val EXTRA_SYNC_PROFILE = "syncProfile"
        const val EXTRA_BRIGHTNESS_FOLLOW = "brightnessFollow"

        private const val NOTIFICATION_ID = 1001
        private const val CHANNEL_ID = "wizlight_capture"

        private var mediaProjectionResultCode: Int = Activity.RESULT_CANCELED
        private var mediaProjectionData: Intent? = null

        fun setMediaProjectionIntent(resultCode: Int, data: Intent) {
            mediaProjectionResultCode = resultCode
            mediaProjectionData = data
        }

        fun hasProjectionPermission(): Boolean {
            return mediaProjectionResultCode == Activity.RESULT_OK && mediaProjectionData != null
        }
    }

    private data class ImageFrame(
        val buffer: java.nio.ByteBuffer,
        val sourceWidth: Int,
        val sourceHeight: Int,
        val rowStride: Int,
        val pixelStride: Int,
        val left: Int,
        val top: Int,
        val width: Int,
        val height: Int,
    )

    private val engine = ColorSyncEngine()

    private var mediaProjection: MediaProjection? = null
    private var virtualDisplay: VirtualDisplay? = null
    private var imageReader: ImageReader? = null
    private var captureThread: HandlerThread? = null
    private var captureHandler: Handler? = null

    private var screenWidth = 0
    private var screenHeight = 0
    private var screenDensity = 0

    private var bulbTargets: List<ColorSyncEngine.BulbTarget> = emptyList()
    private var requestedMode = "single"
    private var ignoreLetterbox = true
    private var sampleSize = 56
    private var requestedColorSpace = "auto"
    private var requestedHdrToneMapping = false
    private var requestedHdrCapture = false
    private var hdrCaptureActive = false
    private var hdrReaderActive = false
    private var captureBufferFormat = "rgba8888"

    @Volatile private var isRunning = false
    private var cleanupStarted = false
    private var lastError: String? = null
    private var sessionId = ""
    private var sessionFailure: String? = null
    private var latestGrids: Map<String, IntArray>? = null
    private var lastStatusUpdateAt = 0L
    private var cachedContentBounds: Rect? = null
    private var cachedContentBoundsAtFrame = -1
    private val frameTimesMs = ArrayDeque<Long>()
    private var framesProcessed = 0
    private var replayedFrames = 0
    private var lastFreshFrameAtMs = 0L

    private val mainHandler = Handler(Looper.getMainLooper())
    private val mediaProjectionCallback =
        object : MediaProjection.Callback() {
            override fun onStop() {
                if (isRunning) {
                    stopCapture(stopProjection = false)
                    stopSelf()
                }
            }
        }

    private val captureRunnable = object : Runnable {
        override fun run() {
            if (!isRunning) {
                return
            }
            val startedAt = SystemClock.elapsedRealtime()
            try {
                captureFrame()
            } catch (error: Exception) {
                lastError = error.message ?: error.toString()
                maybePostStatus()
            }
            val elapsed = SystemClock.elapsedRealtime() - startedAt
            frameTimesMs.addLast(elapsed)
            while (frameTimesMs.size > 30) {
                frameTimesMs.removeFirst()
            }
            val frameDelay = max(5L, (1000L / max(engine.currentFps, 1)) - elapsed)
            captureHandler?.postDelayed(this, frameDelay)
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()

        val windowManager = getSystemService(Context.WINDOW_SERVICE) as WindowManager
        val metrics = DisplayMetrics()
        @Suppress("DEPRECATION")
        windowManager.defaultDisplay.getRealMetrics(metrics)

        screenWidth = max(360, metrics.widthPixels / 4)
        screenHeight = max(240, metrics.heightPixels / 4)
        screenDensity = metrics.densityDpi
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_START -> {
                sessionId = intent.getStringExtra("sessionId") ?: ""
                if (!SyncSessionRegistry.attach(sessionId) { mainHandler.post { stopCapture(); stopSelf() } }) {
                    stopSelf()
                    return START_NOT_STICKY
                }
                try {
                bulbTargets = parseBulbs(intent.getStringExtra(EXTRA_BULBS_JSON) ?: "[]")
                requestedMode = intent.getStringExtra(EXTRA_MODE) ?: "single"
                engine.minFps = intent.getIntExtra(EXTRA_MIN_FPS, 10).coerceIn(4, 30)
                engine.maxFps = intent.getIntExtra(EXTRA_MAX_FPS, 24).coerceIn(engine.minFps, 30)
                engine.currentFps = engine.maxFps
                engine.smoothing = intent.getDoubleExtra(EXTRA_SMOOTHING, 0.2).coerceIn(0.05, 0.85)
                sampleSize = intent.getIntExtra(EXTRA_SAMPLE_SIZE, 56).coerceIn(24, 96)
                engine.colorBoost = intent.getDoubleExtra(EXTRA_COLOR_BOOST, 1.18).coerceIn(1.0, 1.8)
                engine.edgeWeight = intent.getDoubleExtra(EXTRA_EDGE_WEIGHT, 1.5).coerceIn(1.0, 2.5)
                engine.colorAlgorithm = engine.normalizeAlgorithm(intent.getStringExtra(EXTRA_COLOR_ALGORITHM))
                engine.minBrightness = intent.getIntExtra(EXTRA_MIN_BRIGHTNESS, 20).coerceIn(0, 80)
                engine.minColorDelta = intent.getIntExtra(EXTRA_MIN_COLOR_DELTA, 8).coerceIn(2, 48)
                engine.adaptiveFps = intent.getBooleanExtra(EXTRA_ADAPTIVE_FPS, true)
                engine.predictiveSmoothing = intent.getBooleanExtra(EXTRA_PREDICTIVE_SMOOTHING, true)
                ignoreLetterbox = intent.getBooleanExtra(EXTRA_IGNORE_LETTERBOX, true)
                requestedColorSpace = intent.getStringExtra(EXTRA_SOURCE_COLOR_SPACE) ?: "auto"
                requestedHdrToneMapping = intent.getBooleanExtra(EXTRA_HDR_TONE_MAPPING, false)
                requestedHdrCapture = intent.getBooleanExtra(EXTRA_HDR_CAPTURE, false)
                engine.syncProfile = engine.normalizeSyncProfile(intent.getStringExtra(EXTRA_SYNC_PROFILE))
                engine.brightnessFollow = intent.getBooleanExtra(EXTRA_BRIGHTNESS_FOLLOW, true)
                engine.bulbTargets = bulbTargets
                engine.effectiveMode = resolveEffectiveMode(requestedMode, bulbTargets)
                startCapture()
                } catch (error: Exception) {
                    lastError = error.message ?: "Capture startup failed"
                    sessionFailure = lastError
                    stopCapture()
                    stopSelf()
                }
            }

            ACTION_STOP -> {
                stopCapture()
                stopSelf()
            }
        }
        return START_NOT_STICKY
    }

    private fun startCapture() {
        if (isRunning) {
            return
        }
        val projectionData = mediaProjectionData ?: run {
            sessionFailure = "Capture permission is missing"
            stopCapture()
            stopSelf()
            return
        }

        engine.configureAutoFromDisplay(this, requestedColorSpace, requestedHdrToneMapping)

        startForeground(NOTIFICATION_ID, createNotification())

        try {
            val projectionManager =
                getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
            mediaProjection = projectionManager.getMediaProjection(mediaProjectionResultCode, projectionData)
            mediaProjection?.registerCallback(mediaProjectionCallback, mainHandler)

            imageReader = createImageReaderWithHdrFallback()

            virtualDisplay = mediaProjection?.createVirtualDisplay(
                "WizLightCapture",
                screenWidth,
                screenHeight,
                screenDensity,
                DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
                imageReader?.surface,
                null,
                mainHandler,
            )
        } catch (error: Exception) {
            // Projection setup failures (revoked consent, SecurityException on
            // newer Android) used to kill the service silently — surface them.
            lastError = "Projection setup failed: ${error.message ?: error}"
            sessionFailure = lastError
            stopCapture()
            SyncBridge.post(
                "onSyncUpdate",
                hashMapOf<String, Any?>(
                    "lastError" to lastError,
                    "captureBackend" to "mediaprojection",
                ),
            )
            stopSelf()
            return
        }

        mediaProjectionData = null
        engine.start()
        latestGrids = null
        lastError = null
        lastStatusUpdateAt = 0L
        cachedContentBounds = null
        cachedContentBoundsAtFrame = -1
        frameTimesMs.clear()
        framesProcessed = 0
        replayedFrames = 0
        lastFreshFrameAtMs = 0L

        captureThread = HandlerThread("WizLightCaptureThread").also { it.start() }
        captureHandler = Handler(captureThread!!.looper)
        isRunning = true
        captureHandler?.post(captureRunnable)
        SyncSessionRegistry.running(sessionId)
    }

    private fun createImageReaderWithHdrFallback(): ImageReader {
        val sdrReader = {
            captureBufferFormat = "rgba8888"
            hdrCaptureActive = false
            hdrReaderActive = false
            engine.capturedPixelsAreSrgb = false
            ImageReader.newInstance(screenWidth, screenHeight, PixelFormat.RGBA_8888, 2)
        }
        if (!requestedHdrCapture) return sdrReader()
        if (Build.VERSION.SDK_INT < 34) return sdrReader()
        if (!engine.detectedHdrSupport && engine.deviceProfile != ColorSyncEngine.DeviceProfile.BRAVIA) {
            return sdrReader()
        }
        return try {
            val builderClass = Class.forName("android.media.ImageReader\$Builder")
            val hwBufferClass = Class.forName("android.hardware.HardwareBuffer")
            val dataSpaceClass = Class.forName("android.hardware.DataSpace")
            val rgbaFp16 = hwBufferClass.getField("RGBA_FP16").getInt(null)
            val bt2020Pq = dataSpaceClass.getField("DATASPACE_BT2020_PQ").getInt(null)
            val builderCtor = builderClass.getConstructor(Int::class.javaPrimitiveType, Int::class.javaPrimitiveType)
            val builder = builderCtor.newInstance(screenWidth, screenHeight)
            builderClass.getMethod("setMaxImages", Int::class.javaPrimitiveType).invoke(builder, 2)
            builderClass.getMethod("setDefaultHardwareBufferFormat", Int::class.javaPrimitiveType)
                .invoke(builder, rgbaFp16)
            builderClass.getMethod("setDefaultDataSpace", Int::class.javaPrimitiveType)
                .invoke(builder, bt2020Pq)
            val reader = builderClass.getMethod("build").invoke(builder) as ImageReader
            captureBufferFormat = "rgba_fp16 (awaiting frame metadata)"
            hdrReaderActive = true
            hdrCaptureActive = false
            reader
        } catch (_: Throwable) {
            sdrReader()
        }
    }

    private fun captureFrame() {
        val image = imageReader?.acquireLatestImage() ?: run {
            // Static screens produce no new frames (VirtualDisplay only pushes
            // on change) — keep the diagnostics heartbeat alive regardless so
            // the UI doesn't look dead.
            latestGrids?.let {
                replayedFrames += 1
                engine.submitFrame(it, sampleSize)
            }
            maybePostStatus()
            return
        }
        lastFreshFrameAtMs = SystemClock.elapsedRealtime()
        var fallbackToSdr = false
        try {
            if (hdrReaderActive) {
                val plane = image.planes.firstOrNull()
                val actualDataSpace = if (Build.VERSION.SDK_INT >= 33) image.dataSpace else 0
                val pq = if (Build.VERSION.SDK_INT >= 33) android.hardware.DataSpace.DATASPACE_BT2020_PQ else -1
                if (plane == null || !CaptureFormatPolicy.isFp16Pq(plane.pixelStride, actualDataSpace, pq)) {
                    fallbackToSdr = true
                    return
                }
                hdrCaptureActive = true
                engine.capturedPixelsAreSrgb = true
                captureBufferFormat = "rgba_fp16_bt2020_pq"
            }
            val frame = imageToFrame(image) ?: return
            val contentFrame = cropContentArea(frame, ignoreLetterbox)

            val grids = if (engine.effectiveMode == "single") {
                mapOf("all" to sampleGrid(contentFrame, sampleSize))
            } else {
                val map = mutableMapOf("all" to sampleGrid(contentFrame, sampleSize))
                for (region in bulbTargets.map { it.region }.toSet()) {
                    map[region] = sampleGrid(cropRelativeRegion(contentFrame, region), sampleSize)
                }
                map
            }

            latestGrids = grids
            engine.submitFrame(grids, sampleSize)
            framesProcessed += 1
            if (engine.updatesSent > 0) {
                lastError = null
            }
            maybePostStatus()
        } catch (error: Exception) {
            lastError = error.message ?: error.toString()
            maybePostStatus()
        } finally {
            image.close()
            if (fallbackToSdr) switchToSdrCapture()
        }
    }

    private fun switchToSdrCapture() {
        val previous = imageReader ?: return
        var replacement: ImageReader? = null
        try {
            val display = virtualDisplay ?: throw IllegalStateException("Virtual display unavailable")
            replacement = ImageReader.newInstance(screenWidth, screenHeight, PixelFormat.RGBA_8888, 2)
            display.surface = replacement.surface
            imageReader = replacement
            hdrReaderActive = false
            hdrCaptureActive = false
            engine.capturedPixelsAreSrgb = false
            captureBufferFormat = "rgba8888 (HDR format unavailable)"
            latestGrids = null
            cachedContentBounds = null
            previous.close()
            maybePostStatus()
        } catch (error: Exception) {
            replacement?.close()
            hdrReaderActive = false
            hdrCaptureActive = false
            lastError = "HDR fallback failed: ${error.message ?: error}"
            sessionFailure = lastError
            stopCapture()
            stopSelf()
        }
    }

    private fun parseBulbs(rawJson: String): List<ColorSyncEngine.BulbTarget> {
        val bulbs = mutableListOf<ColorSyncEngine.BulbTarget>()
        val array = JSONArray(rawJson)
        for (index in 0 until array.length()) {
            val item = array.optJSONObject(index) ?: continue
            val ip = item.optString("ip")
            if (ip.isBlank()) continue
            val region = RegionLayout.normalize(item.optString("region"))
            bulbs.add(ColorSyncEngine.BulbTarget(ip = ip, region = region))
        }
        return bulbs
    }

    private fun resolveEffectiveMode(mode: String, bulbs: List<ColorSyncEngine.BulbTarget>): String {
        if (mode != "zones") return "single"
        val regions = bulbs.map { it.region }.toSet()
        return if (regions.size >= 2 && bulbs.size >= 2) "zones" else "single"
    }

    private fun imageToFrame(image: Image): ImageFrame? {
        val plane = image.planes.firstOrNull() ?: return null
        return ImageFrame(
            buffer = plane.buffer.duplicate().order(java.nio.ByteOrder.LITTLE_ENDIAN),
            sourceWidth = image.width,
            sourceHeight = image.height,
            rowStride = plane.rowStride,
            pixelStride = plane.pixelStride,
            left = 0,
            top = 0,
            width = image.width,
            height = image.height,
        )
    }

    private fun cropContentArea(source: ImageFrame, shouldCrop: Boolean): ImageFrame {
        if (!shouldCrop) return source
        val shouldRefreshBounds =
            cachedContentBounds == null || framesProcessed - cachedContentBoundsAtFrame >= 12
        val bounds = if (shouldRefreshBounds) {
            detectContentBounds(source).also {
                cachedContentBounds = it
                cachedContentBoundsAtFrame = framesProcessed
            }
        } else {
            cachedContentBounds ?: detectContentBounds(source)
        }
        return if (
            bounds.left == source.left &&
            bounds.top == source.top &&
            bounds.width() == source.width &&
            bounds.height() == source.height
        ) {
            source
        } else {
            source.copy(
                left = bounds.left,
                top = bounds.top,
                width = bounds.width(),
                height = bounds.height(),
            )
        }
    }

    private fun detectContentBounds(source: ImageFrame): Rect {
        val size = 64
        val sample = sampleGrid(source, size)
        val rowActivity = DoubleArray(size)
        val colActivity = DoubleArray(size)
        for (y in 0 until size) {
            var rowActive = 0
            for (x in 0 until size) {
                val luma = engine.colorLuma(sample[y * size + x])
                if (luma * 255.0 > 16.0) {
                    rowActive += 1
                    colActivity[x] += 1.0
                }
            }
            rowActivity[y] = rowActive.toDouble() / size.toDouble()
        }
        val activeRows = rowActivity.indices.filter { rowActivity[it] > 0.02 }
        val activeCols = colActivity.indices.filter { colActivity[it] / size.toDouble() > 0.02 }
        if (activeRows.isEmpty() || activeCols.isEmpty()) {
            return Rect(source.left, source.top, source.left + source.width, source.top + source.height)
        }
        val top = activeRows.first()
        val bottom = activeRows.last() + 1
        val left = activeCols.first()
        val right = activeCols.last() + 1
        if ((bottom - top) < size * 0.45 || (right - left) < size * 0.45) {
            return Rect(source.left, source.top, source.left + source.width, source.top + source.height)
        }
        val mappedLeft = source.left + (left * source.width.toDouble() / size.toDouble()).roundToInt()
        val mappedTop = source.top + (top * source.height.toDouble() / size.toDouble()).roundToInt()
        val mappedRight = source.left + (right * source.width.toDouble() / size.toDouble()).roundToInt()
        val mappedBottom = source.top + (bottom * source.height.toDouble() / size.toDouble()).roundToInt()
        return Rect(
            mappedLeft.coerceIn(source.left, source.left + source.width - 1),
            mappedTop.coerceIn(source.top, source.top + source.height - 1),
            mappedRight.coerceIn(source.left + 1, source.left + source.width),
            mappedBottom.coerceIn(source.top + 1, source.top + source.height),
        )
    }

    private fun cropRelativeRegion(source: ImageFrame, region: String): ImageFrame {
        val bounds = RegionLayout.bounds(region)
        val rect = Rect(
            source.left + (source.width * bounds[0]).roundToInt(),
            source.top + (source.height * bounds[1]).roundToInt(),
            source.left + (source.width * bounds[2]).roundToInt(),
            source.top + (source.height * bounds[3]).roundToInt(),
        )
        val left = rect.left.coerceIn(source.left, source.left + source.width - 1)
        val top = rect.top.coerceIn(source.top, source.top + source.height - 1)
        val right = rect.right.coerceIn(left + 1, source.left + source.width)
        val bottom = rect.bottom.coerceIn(top + 1, source.top + source.height)
        return source.copy(left = left, top = top, width = right - left, height = bottom - top)
    }

    private fun sampleGrid(source: ImageFrame, size: Int): IntArray {
        val pixels = IntArray(size * size)
        val maxX = max(1, source.width - 1)
        val maxY = max(1, source.height - 1)
        for (y in 0 until size) {
            val sourceY = ((y * maxY).toDouble() / max(1, size - 1)).roundToInt()
            for (x in 0 until size) {
                val sourceX = ((x * maxX).toDouble() / max(1, size - 1)).roundToInt()
                pixels[y * size + x] = pixelAt(source, sourceX, sourceY)
            }
        }
        return pixels
    }

    private fun pixelAt(source: ImageFrame, x: Int, y: Int): Int {
        val absoluteX = (source.left + x).coerceIn(0, source.sourceWidth - 1)
        val absoluteY = (source.top + y).coerceIn(0, source.sourceHeight - 1)
        val offset = absoluteY * source.rowStride + absoluteX * source.pixelStride
        val buffer = source.buffer
        if (hdrCaptureActive && source.pixelStride >= 8) {
            if (offset + 5 >= buffer.limit()) return Color.argb(255, 0, 0, 0)
            return engine.fp16PqPixelToSrgbArgb(
                buffer.getShort(offset),
                buffer.getShort(offset + 2),
                buffer.getShort(offset + 4),
            )
        }
        val red = buffer.get(offset).toInt() and 0xFF
        val green = buffer.get(offset + 1).toInt() and 0xFF
        val blue = buffer.get(offset + 2).toInt() and 0xFF
        val alpha = if (offset + 3 < buffer.limit()) buffer.get(offset + 3).toInt() and 0xFF else 0xFF
        return Color.argb(alpha, red, green, blue)
    }

    private fun maybePostStatus() {
        val now = SystemClock.elapsedRealtime()
        if (now - lastStatusUpdateAt < 350L) {
            return
        }
        lastStatusUpdateAt = now

        val frameTimeMs = if (frameTimesMs.isEmpty()) 0.0 else frameTimesMs.average()
        val payload = HashMap<String, Any?>(engine.statusExtras())
        payload["frameTimeMs"] = frameTimeMs
        payload["framesProcessed"] = framesProcessed
        payload["replayedFrames"] = replayedFrames
        payload["freshFrameAgeMs"] = if (lastFreshFrameAtMs == 0L) null else now - lastFreshFrameAtMs
        payload["captureBackend"] = "mediaprojection"
        payload["hdrCaptureActive"] = hdrCaptureActive
        payload["captureBufferFormat"] = captureBufferFormat
        payload["lastError"] = lastError

        SyncBridge.post("onSyncUpdate", payload)
    }

    @Synchronized
    private fun stopCapture(stopProjection: Boolean = true) {
        if (cleanupStarted) return
        cleanupStarted = true
        isRunning = false
        val worker = captureThread
        val handler = captureHandler
        handler?.removeCallbacksAndMessages(null)
        val cleanup = Runnable {
            fun release(action: () -> Unit) {
                try { action() } catch (error: Exception) {
                    if (sessionFailure == null) sessionFailure = error.message ?: "Capture cleanup failed"
                }
            }
            release { virtualDisplay?.release() }
            virtualDisplay = null
            release { imageReader?.close() }
            imageReader = null
            release { mediaProjection?.unregisterCallback(mediaProjectionCallback) }
            if (stopProjection) release { mediaProjection?.stop() }
            mediaProjection = null
            mediaProjectionData = null
            latestGrids = null
            release { engine.close() }
            SyncSessionRegistry.finish(sessionId, sessionFailure)
        }
        if (worker != null && worker.isAlive && Thread.currentThread() !== worker && handler != null) {
            handler.post(cleanup)
            worker.quitSafely()
        } else {
            cleanup.run()
            worker?.quitSafely()
        }
        captureThread = null
        captureHandler = null
        stopForeground(STOP_FOREGROUND_REMOVE)
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                "Screen Capture",
                NotificationManager.IMPORTANCE_LOW,
            ).apply {
                description = "WizLight tablet screen sync"
            }
            getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
        }
    }

    private fun createNotification(): Notification {
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("WizLight Tablet Sync")
            .setContentText("Driving WiZ bulbs from tablet screen color")
            .setSmallIcon(android.R.drawable.ic_menu_view)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setOngoing(true)
            .build()
    }

    override fun onDestroy() {
        stopCapture()
        super.onDestroy()
    }
}
