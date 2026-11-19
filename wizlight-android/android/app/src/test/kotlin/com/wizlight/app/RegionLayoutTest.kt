package com.wizlight.app

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Test

class RegionLayoutTest {
    @Test fun providesDistinctAreasAndSafeFallback() {
        assertArrayEquals(doubleArrayOf(0.08, 0.0, 0.92, 0.22), RegionLayout.bounds("top"), 0.00001)
        assertArrayEquals(doubleArrayOf(0.08, 0.78, 0.92, 1.0), RegionLayout.bounds("bottom"), 0.00001)
        assertArrayEquals(RegionLayout.bounds("left"), RegionLayout.bounds("unknown"), 0.00001)
        assertEquals("center", RegionLayout.normalize("center"))
    }
}
