package com.wizlight.app

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class CaptureFormatPolicyTest {
    @Test fun requiresBothFp16StrideAndActualPqMetadata() {
        val pq = 42
        assertTrue(CaptureFormatPolicy.isFp16Pq(8, pq, pq))
        assertFalse(CaptureFormatPolicy.isFp16Pq(4, pq, pq))
        assertFalse(CaptureFormatPolicy.isFp16Pq(8, 0, pq))
        assertFalse(CaptureFormatPolicy.isFp16Pq(8, 99, pq))
    }
}
