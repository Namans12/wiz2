package com.wizlight.app

import android.app.Activity
import android.graphics.Bitmap
import android.net.Uri
import android.os.Bundle
import android.os.Handler
import android.os.HandlerThread
import android.os.Looper
import android.os.SystemClock
import android.view.TextureView
import android.view.WindowManager
import androidx.media3.common.MediaItem
import androidx.media3.exoplayer.ExoPlayer
import org.json.JSONArray

/**
 * USB media player front-end. Plays a local (non-DRM) media URI with ExoPlayer,
 * renders to a TextureView, samples decoded frames via getBitmap, and feeds them
 * to the shared [ColorSyncEngine] — no MediaProjection, no secure-surface risk,
 * pristine frames. Identical color behavior to screen-capture mode.
 */
class PlayerActivity : Activity() {

    companion object {
        const val EXTRA_MEDIA_URI = "mediaUri"
        private const val GRID = 64

        @Volatile
        private var active: PlayerActivity? = null

        /** Called from MainActivity (Flutter "stopMedia") to close the player. */
        fun stopExternally() {
            active?.let { it.runOnUiThread { it.finish() } }
        }
    }

    private val engine = ColorSyncEngine()
    private var player: ExoPlayer? = null
    private lateinit var textureView: TextureView

    private val sampleBitmap: Bitmap = Bitmap.createBitmap(GRID, GRID, Bitmap.Config.ARGB_8888)
    private val samplePixels = IntArray(GRID * GRID)

    private val mainHandler = Handler(Looper.getMainLooper())
    private var sampleThread: HandlerThread? = null
    private var sampleHandler: Handler? = null

    private var isSyncing = false
    private var lastStatusAt = 0L
    private var lastError: String? = null

    private val frameTick = object : Runnable {
        override fun run() {
            if (!isSyncing) return
            captureAndSubmit()
            val delay = Math.max(15L, (1000L / Math.max(engine.currentFps, 1)).toLong())
            mainHandler.postDelayed(this, delay)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        textureView = TextureView(this)
        setContentView(textureView)

        configureEngineFromIntent()

        sampleThread = HandlerThread("WizPlayerSync").also { it.start() }
        sampleHandler = Handler(sampleThread!!.looper)

        val uriString = intent.getStringExtra(EXTRA_MEDIA_URI)
        if (uriString.isNullOrBlank()) {
            finish()
            return
        }

        engine.start()

        val exo = ExoPlayer.Builder(this).build()
        exo.setVideoTextureView(textureView)
        exo.setMediaItem(MediaItem.fromUri(Uri.parse(uriString)))
        exo.prepare()
        exo.playWhenReady = true
        player = exo

        active = this
        isSyncing = true
        mainHandler.postDelayed(frameTick, 250L)
    }

    private fun configureEngineFromIntent() {
        engine.minFps = intent.getIntExtra(ScreenCaptureService.EXTRA_MIN_FPS, 10).coerceIn(4, 30)
        engine.maxFps = intent.getIntExtra(ScreenCaptureService.EXTRA_MAX_FPS, 24).coerceIn(engine.minFps, 30)
        engine.currentFps = engine.maxFps
        engine.smoothing = intent.getDoubleExtra(ScreenCaptureService.EXTRA_SMOOTHING, 0.2).coerceIn(0.05, 0.85)
        engine.colorBoost = intent.getDoubleExtra(ScreenCaptureService.EXTRA_COLOR_BOOST, 1.18).coerceIn(1.0, 1.8)
        engine.edgeWeight = intent.getDoubleExtra(ScreenCaptureService.EXTRA_EDGE_WEIGHT, 1.5).coerceIn(1.0, 2.5)
        engine.colorAlgorithm = engine.normalizeAlgorithm(intent.getStringExtra(ScreenCaptureService.EXTRA_COLOR_ALGORITHM))
        engine.minBrightness = intent.getIntExtra(ScreenCaptureService.EXTRA_MIN_BRIGHTNESS, 20).coerceIn(0, 80)
        engine.minColorDelta = intent.getIntExtra(ScreenCaptureService.EXTRA_MIN_COLOR_DELTA, 8).coerceIn(2, 48)
        engine.adaptiveFps = intent.getBooleanExtra(ScreenCaptureService.EXTRA_ADAPTIVE_FPS, true)
        engine.predictiveSmoothing = intent.getBooleanExtra(ScreenCaptureService.EXTRA_PREDICTIVE_SMOOTHING, true)
        engine.syncProfile = engine.normalizeSyncProfile(
            intent.getStringExtra(ScreenCaptureService.EXTRA_SYNC_PROFILE),
        )
        engine.brightnessFollow = intent.getBooleanExtra(ScreenCaptureService.EXTRA_BRIGHTNESS_FOLLOW, true)
        engine.bulbTargets = parseBulbs(intent.getStringExtra(ScreenCaptureService.EXTRA_BULBS_JSON) ?: "[]")
        engine.effectiveMode = "single"
        engine.configureAutoFromDisplay(
            this,
            intent.getStringExtra(ScreenCaptureService.EXTRA_SOURCE_COLOR_SPACE) ?: "auto",
            intent.getBooleanExtra(ScreenCaptureService.EXTRA_HDR_TONE_MAPPING, false),
        )
    }

    private fun captureAndSubmit() {
        val view = textureView
        if (!view.isAvailable) return
        try {
            view.getBitmap(sampleBitmap)
        } catch (_: Throwable) {
            return
        }
        sampleBitmap.getPixels(samplePixels, 0, GRID, 0, 0, GRID, GRID)
        val grid = samplePixels.copyOf()
        sampleHandler?.post {
            try {
                engine.submitFrame(mapOf("all" to grid), GRID)
                if (engine.updatesSent > 0) lastError = null
            } catch (error: Exception) {
                lastError = error.message ?: error.toString()
            }
            maybePostStatus()
        }
    }

    private fun maybePostStatus() {
        val now = SystemClock.elapsedRealtime()
        if (now - lastStatusAt < 350L) return
        lastStatusAt = now
        val payload = HashMap<String, Any?>(engine.statusExtras())
        payload["captureBackend"] = "usb_media_player"
        payload["hdrCaptureActive"] = false
        payload["captureBufferFormat"] = "rgba8888"
        payload["frameTimeMs"] = 0.0
        payload["lastError"] = lastError
        SyncBridge.post("onSyncUpdate", payload)
    }

    private fun parseBulbs(rawJson: String): List<ColorSyncEngine.BulbTarget> {
        val bulbs = mutableListOf<ColorSyncEngine.BulbTarget>()
        val array = JSONArray(rawJson)
        for (index in 0 until array.length()) {
            val item = array.optJSONObject(index) ?: continue
            val ip = item.optString("ip")
            if (ip.isBlank()) continue
            val region = if (item.optString("region") == "right") "right" else "left"
            bulbs.add(ColorSyncEngine.BulbTarget(ip = ip, region = region))
        }
        return bulbs
    }

    override fun onPause() {
        super.onPause()
        player?.pause()
    }

    override fun onResume() {
        super.onResume()
        if (isSyncing) {
            player?.play()
        }
    }

    override fun onDestroy() {
        isSyncing = false
        mainHandler.removeCallbacks(frameTick)
        if (active === this) active = null
        player?.release()
        player = null
        sampleThread?.quitSafely()
        sampleThread = null
        sampleHandler = null
        engine.close()
        super.onDestroy()
    }
}
