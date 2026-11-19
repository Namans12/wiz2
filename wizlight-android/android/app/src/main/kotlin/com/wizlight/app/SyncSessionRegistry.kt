package com.wizlight.app

/** One native output owner, including starts that have not reached their Activity/Service yet. */
object SyncSessionRegistry {
    private var id = ""
    private var backend = "screen"
    private var state = "stopped"
    private var error: String? = null
    private var stopOwner: (() -> Unit)? = null
    private var settingsOwner: ((Map<String, Any?>) -> Map<String, Any?>)? = null
    private var updateOwner: ((List<ColorSyncEngine.BulbTarget>, (Boolean) -> Unit) -> Unit)? = null

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

    @Synchronized fun attach(
        sessionId: String,
        update: (List<ColorSyncEngine.BulbTarget>, (Boolean) -> Unit) -> Unit,
        stop: () -> Unit,
    ): Boolean {
        if (sessionId != id || state != "starting") return false
        stopOwner = stop
        updateOwner = update
        return true
    }

    @Synchronized fun updateTargets(
        sessionId: String,
        targets: List<ColorSyncEngine.BulbTarget>,
        complete: (Boolean) -> Unit,
    ) {
        val owner = updateOwner
        if (sessionId != id || state != "running" || owner == null || targets.isEmpty()) {
            complete(false)
            return
        }
        owner(targets, complete)
    }

    @Synchronized fun attachSettings(sessionId: String, owner: (Map<String, Any?>) -> Map<String, Any?>) {
        if (sessionId == id) settingsOwner = owner
    }

    /** Applies settings to the running session; null when no session of that id is running. */
    @Synchronized fun updateSettings(sessionId: String, args: Map<String, Any?>): Map<String, Any?>? {
        val owner = settingsOwner
        if (sessionId != id || state != "running" || owner == null) return null
        return owner(args)
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

    @Synchronized fun isActive(): Boolean = state in setOf("starting", "running")

    /** Stops whichever session is starting/running; false when nothing was active. */
    @Synchronized fun stopCurrent(): Boolean {
        if (!isActive()) return false
        stop(id)
        return true
    }

    @Synchronized fun finish(sessionId: String, failure: String? = null) {
        if (sessionId != id || state in setOf("stopped", "error")) return
        state = if (failure == null) "stopped" else "error"
        error = failure
        stopOwner = null
        updateOwner = null
        settingsOwner = null
        publish()
    }

    private fun publish() = SyncBridge.post("onSessionState", snapshot())
}
