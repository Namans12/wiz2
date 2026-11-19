package com.wizlight.app

import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.util.concurrent.Executors
import java.util.concurrent.ThreadFactory
import java.util.concurrent.ConcurrentHashMap

/** Number of white LEDs a bulb reports (`nowc` in getModelConfig). */
internal enum class WhiteLayout(val whiteChannels: Int) {
    NONE(0), SINGLE(1), DUAL(2);

    companion object {
        fun of(count: Int): WhiteLayout = when (count) { 0 -> NONE; 2 -> DUAL; else -> SINGLE }
    }
}

internal object ModelConfigParser {
    private val NOWC = Regex("\"nowc\"\\s*:\\s*([^,}\\s]+)")

    /**
     * Mirrors desktop parse_wiz_profile_hints for the white-LED count: a valid reply
     * with a missing, non-numeric or out-of-range `nowc` means the default of 1;
     * a reply without a `result` object (error, unrelated datagram) is no answer -> null.
     */
    fun parseWhiteLayout(response: String?): WhiteLayout? {
        if (response == null || !response.contains("\"result\"")) return null
        val raw = NOWC.find(response)?.groupValues?.get(1) ?: return WhiteLayout.SINGLE
        val value = raw.toDoubleOrNull() ?: return WhiteLayout.SINGLE
        val whole = value.toInt()
        return if (whole in 0..2) WhiteLayout.of(whole) else WhiteLayout.SINGLE
    }

    const val REQUEST = "{\"method\":\"getModelConfig\",\"params\":{}}"
}

/**
 * Asks each target bulb for its model config once per session (off the capture
 * thread) and caches the white-LED count per IP. [layoutOf] never blocks: it returns
 * the cached answer, or null (keep the heuristic table) while unknown. Failed
 * probes are retried after [retryAfterMs] so a bulb that was asleep still gets
 * its proper table.
 */
internal class BulbProfileProbe(
    private val query: (String) -> String? = ::queryOverUdp,
    private val clock: () -> Long = System::currentTimeMillis,
    private val retryAfterMs: Long = 5000L,
    private val attempts: Int = 2,
    executor: java.util.concurrent.ExecutorService? = null,
) : AutoCloseable {
    private val results = ConcurrentHashMap<String, WhiteLayout>()
    private val inFlight = ConcurrentHashMap<String, Long>()   // ip -> started/failed at
    private val failedAt = ConcurrentHashMap<String, Long>()
    private val pool = executor ?: Executors.newSingleThreadExecutor(ThreadFactory {
        Thread(it, "WizProfileProbe").apply { isDaemon = true }
    })

    fun layoutOf(ip: String): WhiteLayout? {
        results[ip]?.let { return it }
        val now = clock()
        val failed = failedAt[ip]
        if (failed != null && now - failed < retryAfterMs) return null
        if (inFlight.putIfAbsent(ip, now) == null) {
            try {
                pool.execute { probe(ip) }
            } catch (_: Exception) {
                inFlight.remove(ip)
            }
        }
        return null
    }

    private fun probe(ip: String) {
        try {
            var layout: WhiteLayout? = null
            for (attempt in 0 until attempts) {
                layout = ModelConfigParser.parseWhiteLayout(try { query(ip) } catch (_: Exception) { null })
                if (layout != null) break
            }
            if (layout != null) results[ip] = layout else failedAt[ip] = clock()
        } finally {
            inFlight.remove(ip)
        }
    }

    override fun close() { pool.shutdownNow() }

    companion object {
        const val TIMEOUT_MS = 400

        fun queryOverUdp(ip: String): String? {
            DatagramSocket().use { socket ->
                socket.soTimeout = TIMEOUT_MS
                val request = ModelConfigParser.REQUEST.toByteArray(Charsets.UTF_8)
                socket.send(DatagramPacket(request, request.size, InetAddress.getByName(ip), 38899))
                val buffer = ByteArray(2048)
                val reply = DatagramPacket(buffer, buffer.size)
                return try {
                    socket.receive(reply)
                    String(reply.data, 0, reply.length, Charsets.UTF_8)
                } catch (_: java.net.SocketTimeoutException) {
                    null
                }
            }
        }
    }
}
