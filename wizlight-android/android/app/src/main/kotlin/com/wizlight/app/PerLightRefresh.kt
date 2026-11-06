package com.wizlight.app

/** Periodic retries belong to each light, even when other lights keep changing. */
internal class PerLightRefresh(private val intervalMs: Long = 1500L) {
    private val queuedAt = mutableMapOf<String, Long>()

    fun due(ip: String, nowMs: Long): Boolean =
        queuedAt[ip]?.let { nowMs - it >= intervalMs } ?: true

    fun markQueued(ip: String, nowMs: Long) { queuedAt[ip] = nowMs }
    fun remove(ip: String) { queuedAt.remove(ip) }
    fun clear() { queuedAt.clear() }
}
