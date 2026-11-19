package com.wizlight.app

import org.junit.Assert.*
import org.junit.Test

class PerLightRefreshTest {
    @Test fun oneBusyLightDoesNotDelayAnotherLightsRefresh() {
        val policy = PerLightRefresh()
        policy.markQueued("left", 100)
        policy.markQueued("right", 100)
        policy.markQueued("left", 1200)
        assertFalse(policy.due("left", 1600))
        assertTrue(policy.due("right", 1600))
        policy.remove("right")
        assertTrue(policy.due("right", 1601))
        policy.clear()
        assertTrue(policy.due("left", 1601))
    }
}
