package com.wizlight.app

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.assertNull
import org.junit.Test

class CaptureFormatPolicyTest {
    @Test fun hdrAcquisitionFailureFallsBackBeforeAnyImageExists() {
        var fallback = false
        val image = CaptureFormatPolicy.acquire<Any>(true, { throw UnsupportedOperationException("format") }) {
            fallback = true
        }
        assertNull(image)
        assertTrue(fallback)
    }

    @Test(expected = IllegalStateException::class)
    fun sdrAcquisitionFailureIsNotMisreportedAsHdrFallback() {
        CaptureFormatPolicy.acquire<Any>(false, { throw IllegalStateException("closed") }, {})
    }
    @Test fun requiresBothFp16StrideAndActualPqMetadata() {
        val pq = 42
        assertTrue(CaptureFormatPolicy.isFp16Pq(8, pq, pq))
        assertFalse(CaptureFormatPolicy.isFp16Pq(4, pq, pq))
        assertFalse(CaptureFormatPolicy.isFp16Pq(8, 0, pq))
        assertFalse(CaptureFormatPolicy.isFp16Pq(8, 99, pq))
    }
}
