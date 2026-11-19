package com.wizlight.app

/** One pending payload per device; network work never blocks frame analysis. */
internal class LatestWizSender(
    private val transmit: (String, ByteArray) -> Unit,
    private val onResult: (Exception?) -> Unit,
    private val retryGapMs: Long = 3L,
) : AutoCloseable {
    private val lock = Object()
    private val transmitLock = Any()
    private val pending = LinkedHashMap<String, ByteArray>()
    private val lastSent = mutableMapOf<String, ByteArray>()
    private var closed = false
    private var generation = 0L
    private val worker = Thread({ run() }, "WizLatestSender").apply {
        isDaemon = true
        start()
    }

    fun offerBatch(updates: Map<String, ByteArray>): Boolean = synchronized(lock) {
        if (closed) return false
        for ((ip, payload) in updates) pending[ip] = payload.copyOf()
        lock.notifyAll()
        true
    }

    fun offer(ip: String, payload: ByteArray): Boolean = offerBatch(mapOf(ip to payload))

    /** Drop queued packets and finish any transmission that already started. */
    fun invalidate() = synchronized(transmitLock) {
        synchronized(lock) {
            generation++
            pending.clear()
        }
    }

    /** Drop everything queued, then queue [updates] so nothing stale can follow them. */
    fun invalidateThenOffer(updates: Map<String, ByteArray>): Boolean {
        invalidate()
        return offerBatch(updates)
    }

    private fun run() {
        try {
            while (true) {
                val (batch, version) = synchronized(lock) {
                    while (!closed && pending.isEmpty()) lock.wait()
                    if (closed) return
                    LinkedHashMap(pending).also { pending.clear() } to generation
                }
                val retries = mutableListOf<Pair<String, ByteArray>>()
                // Deliver first packets to every light before retrying any one.
                for ((ip, payload) in batch) {
                    val changed = lastSent[ip]?.contentEquals(payload) != true
                    if (sendCurrent(ip, payload, version)) {
                        lastSent[ip] = payload
                        if (changed) retries.add(ip to payload)
                    }
                }
                repeat(2) {
                    if (retries.isNotEmpty()) Thread.sleep(retryGapMs)
                    for ((ip, payload) in retries) {
                        sendCurrent(ip, payload, version)
                    }
                }
            }
        } catch (_: InterruptedException) {
            Thread.currentThread().interrupt()
        }
    }

    private fun sendCurrent(ip: String, payload: ByteArray, version: Long): Boolean = synchronized(transmitLock) {
        val obsolete = synchronized(lock) { closed || generation != version || pending.containsKey(ip) }
        !obsolete && send(ip, payload)
    }

    private fun send(ip: String, payload: ByteArray): Boolean {
        val error = try {
            transmit(ip, payload)
            null
        } catch (failure: Exception) {
            failure
        }
        // A diagnostic callback must never kill the sender worker.
        try { onResult(error) } catch (_: Exception) { }
        return error == null
    }

    override fun close() {
        synchronized(lock) {
            if (closed) return
            closed = true
            pending.clear()
            lock.notifyAll()
        }
        worker.interrupt()
        if (Thread.currentThread() !== worker) worker.join(1000)
    }
}
