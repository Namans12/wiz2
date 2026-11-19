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
    /**
     * Exact port of desktop _trim_physical_orange (screen_sync_v2.py): adds green around
     * orange, tapering toward red, yellow and pastels. Applied after boost/adaptation,
     * before normalize-to-full-drive.
     */
    fun trimPhysicalOrange(rgb: IntArray): IntArray {
        val r = rgb[0]; val g = rgb[1]; val b = rgb[2]
        if (r < 8 || !(0 < g && g < r) || b >= r * 0.14) return rgb.copyOf()
        val hueWeight = maxOf(0.0, 1.0 - Math.abs(g.toDouble() / r - 0.5) / 0.17)
        val weight = hueWeight * (1.0 - b / (0.14 * r))
        return intArrayOf(r, Math.rint(g + r * 0.08 * weight).toInt(), b)
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
