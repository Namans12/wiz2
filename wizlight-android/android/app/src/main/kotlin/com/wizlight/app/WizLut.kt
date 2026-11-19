package com.wizlight.app

import kotlin.math.min
import kotlin.math.roundToInt

/**
 * A baked 33^3 colour table plus 256-entry gray ramp (see scripts/export_lut_for_android.py),
 * interpolated exactly like the desktop BulbGamutMapper.map_rgb. Pure JVM, so unit tests
 * can load the shipped assets directly.
 *
 * map() returns r,g,b,c,w (always 5 values): 4-channel tables carry r,g,b,w, so c is 0.
 */
internal class WizLut private constructor(
    val grid: Int,
    val channels: Int,
    private val lut: IntArray,
    private val gray: IntArray,
) {
    fun map(r: Int, g: Int, b: Int): IntArray {
        val ch = channels
        val gmax = grid - 1
        val fr = r / 255.0 * gmax
        val fg = g / 255.0 * gmax
        val fb = b / 255.0 * gmax
        val ri0 = fr.toInt(); val gi0 = fg.toInt(); val bi0 = fb.toInt()
        val ri1 = min(ri0 + 1, gmax); val gi1 = min(gi0 + 1, gmax); val bi1 = min(bi0 + 1, gmax)
        val tr = fr - ri0; val tg = fg - gi0; val tb = fb - bi0

        fun idx(ri: Int, gi: Int, bi: Int) = ((ri * grid + gi) * grid + bi) * ch
        val i000 = idx(ri0, gi0, bi0); val i001 = idx(ri0, gi0, bi1)
        val i010 = idx(ri0, gi1, bi0); val i011 = idx(ri0, gi1, bi1)
        val i100 = idx(ri1, gi0, bi0); val i101 = idx(ri1, gi0, bi1)
        val i110 = idx(ri1, gi1, bi0); val i111 = idx(ri1, gi1, bi1)

        val out = DoubleArray(ch)
        for (k in 0 until ch) {
            val c00 = lut[i000 + k] + (lut[i001 + k] - lut[i000 + k]) * tb
            val c01 = lut[i010 + k] + (lut[i011 + k] - lut[i010 + k]) * tb
            val c10 = lut[i100 + k] + (lut[i101 + k] - lut[i100 + k]) * tb
            val c11 = lut[i110 + k] + (lut[i111 + k] - lut[i110 + k]) * tb
            val c0 = c00 + (c01 - c00) * tg
            val c1 = c10 + (c11 - c10) * tg
            out[k] = c0 + (c1 - c0) * tr
        }

        // Near-neutral inputs blend toward the exact-gray ramp (desktop parity).
        val maxc = maxOf(r, g, b)
        if (maxc > 0) {
            val sat = (maxc - minOf(r, g, b)).toDouble() / maxc
            if (sat < 0.12) {
                val t = min(1.0, (0.12 - sat) / 0.08)
                val blend = t * t * (3.0 - 2.0 * t)
                val luma = (r * 0.2126 + g * 0.7152 + b * 0.0722).roundToInt().coerceIn(0, 255)
                for (k in 0 until ch) out[k] += (gray[luma * ch + k] - out[k]) * blend
            }
        }
        val v = IntArray(ch) { Math.rint(out[it]).toInt().coerceIn(0, 255) }
        return if (ch >= 5) intArrayOf(v[0], v[1], v[2], v[3], v[4])
        else intArrayOf(v[0], v[1], v[2], 0, v[3])
    }

    companion object {
        fun parse(bytes: ByteArray): WizLut? {
            if (bytes.size < 14 || bytes[0].toInt() != 'W'.code || bytes[1].toInt() != 'Z'.code) return null
            var off = 6
            fun i32(): Int {
                val v = (bytes[off].toInt() and 0xFF) or
                    ((bytes[off + 1].toInt() and 0xFF) shl 8) or
                    ((bytes[off + 2].toInt() and 0xFF) shl 16) or
                    ((bytes[off + 3].toInt() and 0xFF) shl 24)
                off += 4
                return v
            }
            val grid = i32()
            val channels = i32()
            if (grid < 2 || channels !in 4..5) return null
            val lutSize = grid * grid * grid * channels
            if (bytes.size < off + lutSize + 4) return null
            val lutArr = IntArray(lutSize) { bytes[off + it].toInt() and 0xFF }
            off += lutSize
            val grayN = i32()
            if (grayN != 256 || bytes.size < off + grayN * channels) return null
            val grayArr = IntArray(grayN * channels) { bytes[off + it].toInt() and 0xFF }
            return WizLut(grid, channels, lutArr, grayArr)
        }
    }
}
