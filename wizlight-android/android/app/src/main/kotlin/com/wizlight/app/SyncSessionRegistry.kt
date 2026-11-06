package com.wizlight.app

/** One native output owner, including starts that have not reached their Activity/Service yet. */
object SyncSessionRegistry {
    private var id = ""
    private var backend = "screen"
    private var state = "stopped"
    private var error: String? = null
    private var stopOwner: (() -> Unit)? = null

    @Synchronized fun snapshot(): Map<String, Any?> = mapOf(
        "sessionId" to id, "backend" to backend, "state" to state, "error" to error,
    )

    @Synchronized fun begin(sessionId: String, kind: String): Boolean {
        if (state in setOf("starting", "running", "stopping")) return false
        id = sessionId
        backend = kind
        state = "starting"
        error = null
        stopOwner = null
        publish()
        return true
    }

    @Synchronized fun attach(sessionId: String, stop: () -> Unit): Boolean {
        if (sessionId != id || state != "starting") return false
        stopOwner = stop
        return true
    }

    @Synchronized fun running(sessionId: String) {
        if (sessionId != id || state != "starting") return
        state = "running"
        publish()
    }

    @Synchronized fun stop(sessionId: String) {
        if (sessionId != id || state !in setOf("starting", "running")) return
        state = "stopping"
        publish()
        val owner = stopOwner
        if (owner == null) finish(sessionId) else owner()
    }

    @Synchronized fun finish(sessionId: String, failure: String? = null) {
        if (sessionId != id || state in setOf("stopped", "error")) return
        state = if (failure == null) "stopped" else "error"
        error = failure
        stopOwner = null
        publish()
    }

    private fun publish() = SyncBridge.post("onSessionState", snapshot())
}
