package com.wizlight.app

import kotlin.math.pow
import kotlin.math.roundToInt

/** Pure output math, independently testable without Android devices or sockets. */
class DimmingPolicy {
    private var previous: Double? = null
    fun reset() { previous = null }
    fun value(avgValue: Double, floor: Int, peak: Int, follow: Boolean): Int {
        if (!follow) { previous = null; return 255 }
        val floorFraction = floor.coerceIn(10, 60) / 255.0
        var raw = 255.0 * (floorFraction + (1.0 - floorFraction) * avgValue.coerceIn(0.0, 1.0).pow(0.7))
        if (peak < 48) raw *= peak.coerceAtLeast(0) / 48.0
        val smoothed = previous?.let { it + (raw - it) * 0.35 } ?: raw
        previous = smoothed
        return (Math.rint(smoothed / 5.0).toInt() * 5).coerceIn(26, 255)
    }
}

object WizOutputPolicy {
    fun normalize(rgb: IntArray): IntArray {
        val peak = rgb.maxOrNull() ?: 0
        if (peak < 8 || peak >= 255) return rgb.copyOf()
        return IntArray(rgb.size) { Math.rint(rgb[it] * 255.0 / peak).toInt().coerceIn(0, 255) }
    }
    val OFF_PACKET: ByteArray = "{\"method\":\"setPilot\",\"params\":{\"state\":false}}".toByteArray(Charsets.UTF_8)
    fun packet(channels: IntArray, brightness: Int): String {
        require(channels.size == 5)
        require(channels.all { it in 0..255 })
        val on = channels.any { it > 0 }
        val dim = Math.rint(brightness / 255.0 * 100).toInt().coerceIn(10, 100)
        val dimField = if (on) ",\"dimming\":$dim" else ""
        return "{\"method\":\"setPilot\",\"params\":{\"state\":$on,\"r\":${channels[0]},\"g\":${channels[1]},\"b\":${channels[2]},\"c\":${channels[3]},\"w\":${channels[4]}$dimField}}"
    }
}
