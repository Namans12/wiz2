package com.wizlight.app

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ZoneOutputTest {
    @Test fun neutralStrengthIsDesktopSmoothstep() {
        assertEquals(1.0, ZoneOutput.neutralStrength(0.0), 1e-12)
        assertEquals(0.0, ZoneOutput.neutralStrength(0.06), 1e-12)
        assertEquals(0.0, ZoneOutput.neutralStrength(0.5), 1e-12)
        assertEquals(0.5, ZoneOutput.neutralStrength(0.03), 1e-12)
        // a grey region stays neutral even when the other region is vivid
        assertTrue(ZoneOutput.neutralStrength(0.01) > 0.9)
    }

    @Test fun eachRegionDimsWithItsOwnPicture() {
        val dimming = ZoneDimming()
        val out = dimming.update(mapOf("left" to (0.05 to 255), "right" to (1.0 to 255)), 20, true)
        assertTrue(out.getValue("left") < out.getValue("right"))
        assertEquals(255, out.getValue("right"))
        // independent smoothing state per region
        val next = dimming.update(mapOf("left" to (1.0 to 255), "right" to (1.0 to 255)), 20, true)
        assertTrue(next.getValue("left") < 255)
        assertEquals(255, next.getValue("right"))
    }

    @Test fun lightsGetTheirRegionsDimming() {
        val targets = listOf(ColorSyncEngine.BulbTarget("a", "left"), ColorSyncEngine.BulbTarget("b", "right"))
        val per = mapOf("left" to 60, "right" to 200)
        assertEquals(60, ZoneDimming.forLight("a", targets, per, 255))
        assertEquals(200, ZoneDimming.forLight("b", targets, per, 255))
        assertEquals(255, ZoneDimming.forLight("c", targets, per, 255))
        assertEquals(255, ZoneDimming.forLight("a", targets, emptyMap(), 255))
    }

    @Test fun mixedBulbsCanHaveDifferentPacketsFromTheirOwnTables() {
        val rgbw = WizLut.parse(java.io.File("src/main/assets/wiz_rgbw_physical_lut_v1.bin").readBytes())!!
        val rgbcw = WizLut.parse(java.io.File("src/main/assets/wiz_rgbcw_physical_lut_v1.bin").readBytes())!!
        val single = WizOutputPolicy.packet(rgbw.map(255, 200, 160), 120)
        val dual = WizOutputPolicy.packet(rgbcw.map(255, 200, 160), 200)
        assertTrue(single, single.contains("\"c\":0,"))
        assertTrue(single, single.contains("\"dimming\":47"))
        assertTrue(dual, dual.contains("\"dimming\":78"))
        assertTrue(single != dual)
    }

    @Test fun realCaptureGridsWithAllStillGetPerRegionDimmingAndNeutral() {
        fun grid(argb: Int) = IntArray(64) { argb }
        val dark = grid(0xFF0A0A0A.toInt())          // dark neutral left
        val bright = grid(0xFFFF4020.toInt())        // bright saturated right
        val grids = mapOf("all" to grid(0xFF808080.toInt()), "left" to dark, "right" to bright)
        assertTrue(ZoneOutput.regionGrids(false, grids).isEmpty())
        val regions = ZoneOutput.regionGrids(true, grids)
        assertEquals(setOf("left", "right"), regions.keys)
        val stats = regions.mapValues { ZoneOutput.gridStats(it.value) }
        assertTrue(ZoneOutput.neutralStrength(stats.getValue("left").first) > 0.9)
        assertEquals(0.0, ZoneOutput.neutralStrength(stats.getValue("right").first), 1e-12)
        val per = ZoneDimming().update(
            stats.mapValues { it.value.second to 255 }, 20, true,
        )
        val targets = listOf(ColorSyncEngine.BulbTarget("10.0.0.1", "left"), ColorSyncEngine.BulbTarget("10.0.0.2", "right"))
        val l = ZoneDimming.forLight("10.0.0.1", targets, per, 255)
        val r = ZoneDimming.forLight("10.0.0.2", targets, per, 255)
        assertTrue("left $l right $r", l < r)
    }
}
