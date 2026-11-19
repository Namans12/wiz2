package com.wizlight.app

import java.io.File
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class WizLutTest {
    private val assets = File("src/main/assets")
    private val tables = listOf(
        "wiz_rgbcw_lut_v1.bin" to 5,
        "wiz_rgbcw_physical_lut_v1.bin" to 5,
        "wiz_rgbw_physical_lut_v1.bin" to 4,
        "wiz_rgb_physical_lut_v1.bin" to 4,
    )

    private fun load(name: String): WizLut = WizLut.parse(File(assets, name).readBytes())!!

    @Test fun everyShippedTableLoadsWithItsChannelCount() {
        for ((name, channels) in tables) assertEquals(name, channels, load(name).channels)
    }

    @Test fun tablesMatchPythonMapperAtExportedSamples() {
        val lines = File("src/test/resources/lut_expected_v1.txt").readLines().filter { it.isNotBlank() }
        assertEquals(tables.size * 20, lines.size)
        val loaded = tables.associate { it.first to load(it.first) }
        var worst = 0
        for (line in lines) {
            val (head, tail) = line.split(" -> ")
            val parts = head.split(" ")
            val lut = loaded.getValue(parts[0])
            val expected = tail.split(" ").map { it.toInt() }
            val actual = lut.map(parts[1].toInt(), parts[2].toInt(), parts[3].toInt())
            // map() always returns r,g,b,c,w; four-channel tables carry r,g,b,w.
            val comparable = if (expected.size == 5) actual.toList()
            else listOf(actual[0], actual[1], actual[2], actual[4])
            for (k in expected.indices) {
                val diff = Math.abs(expected[k] - comparable[k])
                worst = maxOf(worst, diff)
                assertTrue("$line -> ${actual.toList()}", diff <= 1)  // rint vs numpy round at .5 only
            }
        }
        println("max channel error vs python: $worst")
    }

    @Test fun singleWhiteTableNeverDrivesTheColdChannel() {
        val lut = load("wiz_rgbw_physical_lut_v1.bin")
        for (r in 0..255 step 17) for (g in 0..255 step 17) for (b in 0..255 step 17) {
            assertEquals(0, lut.map(r, g, b)[3])
        }
    }

    @Test fun rgbOnlyTableNeverDrivesAnyWhite() {
        val lut = load("wiz_rgb_physical_lut_v1.bin")
        for (r in 0..255 step 51) for (g in 0..255 step 51) for (b in 0..255 step 51) {
            val v = lut.map(r, g, b)
            assertEquals(0, v[3]); assertEquals(0, v[4])
        }
    }

    @Test fun rejectsTruncatedOrForeignData() {
        assertNull(WizLut.parse(ByteArray(4)))
        assertNull(WizLut.parse(File(assets, "wiz_rgbw_physical_lut_v1.bin").readBytes().copyOf(100)))
        assertNotNull(WizLut.parse(File(assets, "wiz_rgbw_physical_lut_v1.bin").readBytes()))
    }
}
