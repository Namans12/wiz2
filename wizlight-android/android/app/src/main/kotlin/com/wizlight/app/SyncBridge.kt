package com.wizlight.app

import android.os.Handler
import android.os.Looper
import io.flutter.plugin.common.MethodChannel

/**
 * Shared holder for the Flutter MethodChannel so both the screen-capture service
 * and the USB media player can post "onSyncUpdate" diagnostics to the same Dart
 * listener. Always invokes on the main thread.
 */
object SyncBridge {
    @Volatile
    var channel: MethodChannel? = null

    private val mainHandler = Handler(Looper.getMainLooper())

    fun post(method: String, payload: Any?) {
        val ch = channel ?: return
        mainHandler.post { ch.invokeMethod(method, payload) }
    }
}
