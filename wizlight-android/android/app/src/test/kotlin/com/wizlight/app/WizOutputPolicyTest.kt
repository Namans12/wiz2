package com.wizlight.app

import org.junit.Assert.*
import org.junit.Test

class WizOutputPolicyTest {
    @Test fun followsSceneAndKeepsHueSeparateFromBrightness() {
        assertArrayEquals(intArrayOf(255, 128, 0), WizOutputPolicy.normalize(intArrayOf(100, 50, 0)))
        assertEquals(255, DimmingPolicy().value(1.0, 20, 255, true))
        assertEquals(165, DimmingPolicy().value(0.5, 20, 255, true))
        assertEquals(26, DimmingPolicy().value(0.02, 20, 8, true))
        assertEquals(255, DimmingPolicy().value(0.0, 20, 0, false))
    }
    @Test fun packetIncludesWhitesAndGlobalDimmingButOffDoesNot() {
        val packet = WizOutputPolicy.packet(intArrayOf(8, 16, 24, 32, 40), 102)
        assertTrue(packet.contains("\"c\":32,\"w\":40,\"dimming\":40"))
        val off = WizOutputPolicy.packet(IntArray(5), 102)
        assertTrue(off.contains("\"state\":false"))
        assertFalse(off.contains("dimming"))
    }
    @Test fun dimmingSmoothingResetsBetweenSessions() {
        val dim = DimmingPolicy()
        dim.value(1.0, 20, 255, true)
        assertTrue(dim.value(.1, 20, 255, true) > DimmingPolicy().value(.1, 20, 255, true))
        dim.reset()
        assertEquals(DimmingPolicy().value(.1, 20, 255, true), dim.value(.1, 20, 255, true))
    }
}
