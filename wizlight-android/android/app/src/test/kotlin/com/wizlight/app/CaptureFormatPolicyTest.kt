package com.wizlight.app

import org.junit.Assert.assertEquals
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
    @Test fun guardHdrReadTurnsAnyExceptionIntoOneFallback() {
        var fallbacks = 0
        val r = CaptureFormatPolicy.guardHdrRead<Boolean>({ throw IllegalStateException("buffer") }) { fallbacks++ }
        assertNull(r)
        assertEquals(1, fallbacks)
        assertEquals(true, CaptureFormatPolicy.guardHdrRead({ true }) { fallbacks++ })
        assertEquals(1, fallbacks)
    }

    private fun wd(hdr: Boolean, hdrAt: Long = 0, sdrAt: Long = 0, rebuilt: Boolean = false, last: Long = 0, now: Long) =
        CaptureFormatPolicy.watchdog(hdr, hdrAt, sdrAt, rebuilt, last, now)

    @Test fun hdrReaderWithoutFrameSwitchesToSdrAfter1500ms() {
        assertEquals(CaptureFormatPolicy.WatchdogAction.NONE, wd(true, hdrAt = 1000, now = 2499))
        assertEquals(CaptureFormatPolicy.WatchdogAction.SWITCH_TO_SDR, wd(true, hdrAt = 1000, now = 2500))
    }

    @Test fun hdrReaderThatDeliveredAFrameIsLeftAlone() {
        assertEquals(CaptureFormatPolicy.WatchdogAction.NONE, wd(true, hdrAt = 1000, last = 1200, now = 9000))
    }

    @Test fun sdrSwitchWithoutFrameRebuildsDisplayAfter1000msOnlyOnce() {
        assertEquals(CaptureFormatPolicy.WatchdogAction.NONE, wd(false, sdrAt = 5000, now = 5999))
        assertEquals(CaptureFormatPolicy.WatchdogAction.REBUILD_DISPLAY, wd(false, sdrAt = 5000, now = 6000))
        assertEquals(CaptureFormatPolicy.WatchdogAction.NONE, wd(false, sdrAt = 5000, rebuilt = true, now = 60000))
    }

    @Test fun sdrFrameAfterSwitchCancelsRebuildAndPlainSdrNeverTriggers() {
        assertEquals(CaptureFormatPolicy.WatchdogAction.NONE, wd(false, sdrAt = 5000, last = 5300, now = 9000))
        assertEquals(CaptureFormatPolicy.WatchdogAction.NONE, wd(false, now = 9000))
    }
}
