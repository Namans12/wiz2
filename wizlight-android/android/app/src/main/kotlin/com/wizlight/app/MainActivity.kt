package com.wizlight.app

import android.app.Activity
import android.content.Context
import android.content.ClipData
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import io.flutter.plugin.common.MethodCall
import android.media.projection.MediaProjectionManager
import android.os.Build
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
    private var pendingMediaUri: String? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        handleViewIntent(intent, notifyDart = false)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleViewIntent(intent, notifyDart = true)
    }

    /** Remember a video opened from a file browser ("Open with WizLight"). */
    private fun handleViewIntent(intent: Intent?, notifyDart: Boolean) {
        if (intent == null || intent.action != Intent.ACTION_VIEW) return
        val uri = intent.data ?: return
        val type = intent.type ?: contentResolver.getType(uri)
        if (type != null && !type.startsWith("video/") && type != "application/octet-stream") return
        try {
            contentResolver.takePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION)
        } catch (_: Exception) {
            // Not every provider offers persistable grants; the activity grant still works while we run.
        }
        pendingMediaUri = uri.toString()
        // Mark consumed so a recreation does not re-deliver the same VIEW intent.
        intent.action = Intent.ACTION_MAIN
        if (notifyDart) methodChannel?.invokeMethod("onPendingMediaUri", null)
    }

    override fun cleanUpFlutterEngine(flutterEngine: FlutterEngine) {
        SyncBridge.detach(methodChannel)
        methodChannel?.setMethodCallHandler(null)
        methodChannel = null
        super.cleanUpFlutterEngine(flutterEngine)
    }

    override fun onDestroy() {
        SyncBridge.detach(methodChannel)
        super.onDestroy()
    }

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)

        methodChannel = MethodChannel(flutterEngine.dartExecutor.binaryMessenger, channelName)
        SyncBridge.channel = methodChannel
        methodChannel?.setMethodCallHandler { call, result ->
            when (call.method) {
                "requestPermission" -> requestScreenCapturePermission(result)
                "startCapture" -> startScreenCapture(call, result)
                "stopCapture" -> stopScreenCapture(call, result)
                "updateTargets" -> {
                    SyncSessionRegistry.updateTargets(
                        call.argument<String>("sessionId") ?: "", readSyncTargets(call),
                    ) { accepted -> result.success(accepted) }
                }
                "updateSettings" -> {
                    @Suppress("UNCHECKED_CAST")
                    val args = (call.arguments as? Map<String, Any?>) ?: emptyMap()
                    val report = SyncSessionRegistry.updateSettings(args["sessionId"] as? String ?: "", args)
                    result.success(report ?: mapOf("accepted" to false))
                }
                "pickMedia" -> pickMedia(result)
                "takePendingMediaUri" -> {
                    val uri = pendingMediaUri
                    pendingMediaUri = null
                    result.success(uri)
                }
                "isTelevision" -> result.success(isTelevisionDevice(this))
                "playMedia" -> playMedia(call, result)
                "stopMedia" -> {
                    SyncSessionRegistry.stop(call.argument<String>("sessionId") ?: "")
                    result.success(true)
                }
                "querySession" -> result.success(SyncSessionRegistry.snapshot())
                else -> result.notImplemented()
            }
        }
    }

    private fun requestScreenCapturePermission(result: MethodChannel.Result) {
        if (pendingResult != null) {
            result.error("busy", "A capture permission request is already open", null)
            return
        }
        pendingResult = result
        val projectionManager =
            getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
        startActivityForResult(projectionManager.createScreenCaptureIntent(), requestMediaProjection)
    }

    private fun startScreenCapture(call: io.flutter.plugin.common.MethodCall, result: MethodChannel.Result) {
        if (readSyncTargets(call).isEmpty() || !ScreenCaptureService.hasProjectionPermission()) {
            result.success(false)
            return
        }
        val sessionId = call.argument<String>("sessionId") ?: ""
        if (sessionId.isBlank() || !SyncSessionRegistry.begin(sessionId, "screen")) {
            result.success(false)
            return
        }
        val intent = Intent(this, ScreenCaptureService::class.java).apply {
            action = ScreenCaptureService.ACTION_START
            putCommonSyncExtras(call)
        }
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) startForegroundService(intent)
            else startService(intent)
            result.success(true)
        } catch (error: Exception) {
            SyncSessionRegistry.finish(sessionId, error.message ?: "Could not start capture")
            result.success(false)
        }
    }

    private fun stopScreenCapture(call: io.flutter.plugin.common.MethodCall, result: MethodChannel.Result) {
        SyncSessionRegistry.stop(call.argument<String>("sessionId") ?: "")
        result.success(true)
    }

    private fun pickMedia(result: MethodChannel.Result) {
        if (pendingPickResult != null) {
            result.error("busy", "A media picker is already open", null)
            return
        }
        pendingPickResult = result
        val open = Intent(Intent.ACTION_OPEN_DOCUMENT).apply {
            addCategory(Intent.CATEGORY_OPENABLE)
            type = "video/*"
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
        // Sony TVs often have no DocumentsUI: fall back to GET_CONTENT, then report NO_PICKER.
        val get = Intent(Intent.ACTION_GET_CONTENT).apply {
            addCategory(Intent.CATEGORY_OPENABLE)
            type = "video/*"
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
        for (candidate in listOf(open, get)) {
            try {
                startActivityForResult(candidate, requestPickMedia)
                return
            } catch (_: Exception) {
            }
        }
        pendingPickResult = null
        result.error("NO_PICKER", "No file picker is available on this device", null)
    }

    private fun playMedia(call: io.flutter.plugin.common.MethodCall, result: MethodChannel.Result) {
        val uri = call.argument<String>("mediaUri")
        if (uri.isNullOrBlank() || readSyncTargets(call).isEmpty()) {
            result.success(false)
            return
        }
        val sessionId = call.argument<String>("sessionId") ?: ""
        if (sessionId.isBlank() || !SyncSessionRegistry.begin(sessionId, "usb")) {
            result.success(false)
            return
        }
        val intent = Intent(this, PlayerActivity::class.java).apply {
            putExtra(PlayerActivity.EXTRA_MEDIA_URI, uri)
            val parsed = Uri.parse(uri)
            if (parsed.scheme == "content") {
                // Forward our read grant (picked or "Open with") to the player activity.
                data = parsed
                clipData = ClipData.newRawUri("media", parsed)
                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            }
            putCommonSyncExtras(call)
        }
        try {
            startActivity(intent)
            result.success(true)
        } catch (error: Exception) {
            SyncSessionRegistry.finish(sessionId, error.message ?: "Could not open player")
            result.success(false)
        }
    }

    private fun readSyncTargets(call: io.flutter.plugin.common.MethodCall): List<ColorSyncEngine.BulbTarget> =
        try {
            parseSyncTargets(call.argument<String>("bulbsJson") ?: "[]")
        } catch (_: org.json.JSONException) {
            emptyList()
        }

    private fun Intent.putCommonSyncExtras(call: io.flutter.plugin.common.MethodCall) {
        putExtra("sessionId", call.argument<String>("sessionId") ?: "")
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
