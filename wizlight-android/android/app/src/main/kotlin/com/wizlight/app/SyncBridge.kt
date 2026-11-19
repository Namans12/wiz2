package com.wizlight.app

import android.os.Handler
import android.os.Looper
import io.flutter.plugin.common.MethodChannel

/**
 * Shared holder for the Flutter MethodChannel so both the screen-capture service
 * and the USB media player can post "onSyncUpdate" diagnostics to the same Dart
 * listener.
 *
 * Every attach, detach and post is serialised on the main thread (a single FIFO queue), and the
 * destination channel is resolved at delivery time. The latest session-state snapshot is always
 * retained; if it could not be delivered because no channel was attached, it is replayed on the
 * next attach, before any later post is processed.
 */
object SyncBridge {
    private const val SESSION_STATE = "onSessionState"

    private val mainHandler = Handler(Looper.getMainLooper())

    // Main-thread state only.
    private var current: MethodChannel? = null
    private var latestSession: Any? = null
    private var sessionUndelivered = false

    @Volatile
    private var channelView: MethodChannel? = null

    var channel: MethodChannel?
        get() = channelView
        set(value) {
            channelView = value
            mainHandler.post { attach(value) }
        }

    /** Detach only if [owner] is still the attached channel (a newer activity may have replaced it). */
    fun detach(owner: MethodChannel?) {
        if (owner == null) return
        mainHandler.post {
            if (current === owner) current = null
            if (channelView === owner) channelView = null
        }
    }

    fun post(method: String, payload: Any?) {
        mainHandler.post { deliver(method, payload) }
    }

    private fun attach(value: MethodChannel?) {
        current = value
        if (value != null && sessionUndelivered) {
            sessionUndelivered = false
            value.invokeMethod(SESSION_STATE, latestSession)
        }
    }

    private fun deliver(method: String, payload: Any?) {
        val isSession = method == SESSION_STATE
        if (isSession) latestSession = payload
        val ch = current
        if (ch == null) {
            if (isSession) sessionUndelivered = true
            return
        }
        if (isSession) sessionUndelivered = false
        ch.invokeMethod(method, payload)
    }
}
