package com.wizlight.app

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class PhysicalTrimTest {
    // Expected values generated with src.features.screen_sync_v2._trim_physical_orange.
    private val cases = listOf(
        Pair(intArrayOf(255,128,0), intArrayOf(255,148,0)), Pair(intArrayOf(255,140,5), intArrayOf(255,152,5)),
        Pair(intArrayOf(200,100,10), intArrayOf(200,110,10)), Pair(intArrayOf(200,90,0), intArrayOf(200,101,0)),
        Pair(intArrayOf(255,0,0), intArrayOf(255,0,0)), Pair(intArrayOf(255,255,0), intArrayOf(255,255,0)),
        Pair(intArrayOf(255,128,36), intArrayOf(255,128,36)), Pair(intArrayOf(255,128,35), intArrayOf(255,128,35)),
        Pair(intArrayOf(7,3,0), intArrayOf(7,3,0)), Pair(intArrayOf(100,50,0), intArrayOf(100,58,0)),
        Pair(intArrayOf(180,95,12), intArrayOf(180,101,12)), Pair(intArrayOf(250,200,10), intArrayOf(250,200,10)),
        Pair(intArrayOf(128,64,17), intArrayOf(128,65,17)), Pair(intArrayOf(255,110,20), intArrayOf(255,115,20)),
        Pair(intArrayOf(60,30,2), intArrayOf(60,34,2)), Pair(intArrayOf(200,110,28), intArrayOf(200,110,28)),
        Pair(intArrayOf(255,150,0), intArrayOf(255,160,0)), Pair(intArrayOf(255,80,0), intArrayOf(255,80,0)),
        Pair(intArrayOf(8,4,0), intArrayOf(8,5,0)), Pair(intArrayOf(0,0,0), intArrayOf(0,0,0)),
        Pair(intArrayOf(200,200,0), intArrayOf(200,200,0)), Pair(intArrayOf(150,60,3), intArrayOf(150,64,3)),
        Pair(intArrayOf(255,200,100), intArrayOf(255,200,100)), Pair(intArrayOf(10,5,1), intArrayOf(10,5,1)),
    )

    @Test fun matchesDesktopTrimOutputs() {
        for ((input, expected) in cases) {
            assertArrayEquals(input.toList().toString(), expected, WizOutputPolicy.trimPhysicalOrange(input))
        }
    }

    @Test fun doesNotMutateInput() {
        val input = intArrayOf(255, 128, 0)
        WizOutputPolicy.trimPhysicalOrange(input)
        assertArrayEquals(intArrayOf(255, 128, 0), input)
    }

    @Test fun singleWhitePacketCarriesColdZeroLikeDesktopFmt4() {
        // The RGBW table output is r,g,b,0,w; desktop _FMT_4 sends "c":0,"w":<4th value>.
        val lut = WizLut.parse(java.io.File("src/main/assets/wiz_rgbw_physical_lut_v1.bin").readBytes())!!
        val v = lut.map(255, 255, 255)
        val packet = WizOutputPolicy.packet(v, 255)
        assertTrue(packet, packet.contains("\"c\":0,\"w\":${v[4]}"))
        assertTrue(packet, v[4] > 0)
    }
}
