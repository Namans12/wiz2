package com.wizlight.app

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.media.projection.MediaProjectionManager
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel

class MainActivity : FlutterActivity() {
    private val channelName = "com.wizlight.app/screen_capture"
    private val requestMediaProjection = 1001
    private val requestPickMedia = 1002

    private var methodChannel: MethodChannel? = null
    private var pendingResult: MethodChannel.Result? = null
    private var pendingPickResult: MethodChannel.Result? = null

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)

        methodChannel = MethodChannel(flutterEngine.dartExecutor.binaryMessenger, channelName)
        SyncBridge.channel = methodChannel
        methodChannel?.setMethodCallHandler { call, result ->
            when (call.method) {
                "requestPermission" -> requestScreenCapturePermission(result)
                "startCapture" -> startScreenCapture(call, result)
                "stopCapture" -> stopScreenCapture(result)
                "pickMedia" -> pickMedia(result)
                "playMedia" -> playMedia(call, result)
                "stopMedia" -> {
                    PlayerActivity.stopExternally()
                    result.success(true)
                }
                else -> result.notImplemented()
            }
        }
    }

    private fun requestScreenCapturePermission(result: MethodChannel.Result) {
        pendingResult = result
        val projectionManager =
            getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
        startActivityForResult(projectionManager.createScreenCaptureIntent(), requestMediaProjection)
    }

    private fun startScreenCapture(call: io.flutter.plugin.common.MethodCall, result: MethodChannel.Result) {
        if (ScreenCaptureService.hasProjectionPermission().not()) {
            result.success(false)
            return
        }
        val intent = Intent(this, ScreenCaptureService::class.java).apply {
            action = ScreenCaptureService.ACTION_START
            putCommonSyncExtras(call)
        }
        startForegroundService(intent)
        result.success(true)
    }

    private fun stopScreenCapture(result: MethodChannel.Result) {
        val intent = Intent(this, ScreenCaptureService::class.java).apply {
            action = ScreenCaptureService.ACTION_STOP
        }
        stopService(intent)
        result.success(true)
    }

    private fun pickMedia(result: MethodChannel.Result) {
        pendingPickResult = result
        val intent = Intent(Intent.ACTION_OPEN_DOCUMENT).apply {
            addCategory(Intent.CATEGORY_OPENABLE)
            type = "video/*"
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
        try {
            startActivityForResult(intent, requestPickMedia)
        } catch (error: Exception) {
            pendingPickResult = null
            result.success(null)
        }
    }

    private fun playMedia(call: io.flutter.plugin.common.MethodCall, result: MethodChannel.Result) {
        val uri = call.argument<String>("mediaUri")
        if (uri.isNullOrBlank()) {
            result.success(false)
            return
        }
        val intent = Intent(this, PlayerActivity::class.java).apply {
            putExtra(PlayerActivity.EXTRA_MEDIA_URI, uri)
            putCommonSyncExtras(call)
        }
        startActivity(intent)
        result.success(true)
    }

    private fun Intent.putCommonSyncExtras(call: io.flutter.plugin.common.MethodCall) {
        putExtra(ScreenCaptureService.EXTRA_BULBS_JSON, call.argument<String>("bulbsJson") ?: "[]")
        putExtra(ScreenCaptureService.EXTRA_MODE, call.argument<String>("mode") ?: "single")
        putExtra(ScreenCaptureService.EXTRA_MIN_FPS, call.argument<Int>("minFps") ?: 10)
        putExtra(ScreenCaptureService.EXTRA_MAX_FPS, call.argument<Int>("maxFps") ?: 24)
        putExtra(ScreenCaptureService.EXTRA_SMOOTHING, call.argument<Double>("smoothing") ?: 0.2)
        putExtra(ScreenCaptureService.EXTRA_SAMPLE_SIZE, call.argument<Int>("sampleSize") ?: 56)
        putExtra(ScreenCaptureService.EXTRA_COLOR_BOOST, call.argument<Double>("colorBoost") ?: 1.18)
        putExtra(ScreenCaptureService.EXTRA_EDGE_WEIGHT, call.argument<Double>("edgeWeight") ?: 1.5)
        putExtra(ScreenCaptureService.EXTRA_COLOR_ALGORITHM, call.argument<String>("colorAlgorithm") ?: "auto")
        putExtra(ScreenCaptureService.EXTRA_MIN_BRIGHTNESS, call.argument<Int>("minBrightness") ?: 20)
        putExtra(ScreenCaptureService.EXTRA_MIN_COLOR_DELTA, call.argument<Int>("minColorDelta") ?: 8)
        putExtra(ScreenCaptureService.EXTRA_ADAPTIVE_FPS, call.argument<Boolean>("adaptiveFps") ?: true)
        putExtra(ScreenCaptureService.EXTRA_PREDICTIVE_SMOOTHING, call.argument<Boolean>("predictiveSmoothing") ?: true)
        putExtra(ScreenCaptureService.EXTRA_IGNORE_LETTERBOX, call.argument<Boolean>("ignoreLetterbox") ?: true)
        putExtra(ScreenCaptureService.EXTRA_SOURCE_COLOR_SPACE, call.argument<String>("sourceColorSpace") ?: "auto")
        putExtra(ScreenCaptureService.EXTRA_HDR_TONE_MAPPING, call.argument<Boolean>("hdrToneMapping") ?: false)
        putExtra(ScreenCaptureService.EXTRA_HDR_CAPTURE, call.argument<Boolean>("hdrCapture") ?: false)
        putExtra(ScreenCaptureService.EXTRA_SYNC_PROFILE, call.argument<String>("syncProfile") ?: "auto")
        putExtra(ScreenCaptureService.EXTRA_BRIGHTNESS_FOLLOW, call.argument<Boolean>("brightnessFollow") ?: true)
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        when (requestCode) {
            requestMediaProjection -> {
                if (resultCode == Activity.RESULT_OK && data != null) {
                    ScreenCaptureService.setMediaProjectionIntent(resultCode, data)
                    pendingResult?.success(true)
                } else {
                    pendingResult?.success(false)
                }
                pendingResult = null
            }
            requestPickMedia -> {
                val uri = data?.data
                if (resultCode == Activity.RESULT_OK && uri != null) {
                    try {
                        contentResolver.takePersistableUriPermission(
                            uri,
                            Intent.FLAG_GRANT_READ_URI_PERMISSION,
                        )
                    } catch (_: Exception) {
                    }
                    pendingPickResult?.success(uri.toString())
                } else {
                    pendingPickResult?.success(null)
                }
                pendingPickResult = null
            }
        }
    }
}
