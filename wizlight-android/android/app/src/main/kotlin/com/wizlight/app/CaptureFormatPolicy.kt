package com.wizlight.app

/** A requested buffer format is not proof of the producer's frame dataspace. */
internal object CaptureFormatPolicy {
    /** HDR reader must deliver a frame this soon after it started, else fall back to SDR. */
    const val HDR_NO_FRAME_TIMEOUT_MS = 1500L

    /** After switching to SDR, a frame must arrive this soon, else the display is rebuilt. */
    const val SDR_NO_FRAME_TIMEOUT_MS = 1000L

    enum class WatchdogAction { NONE, SWITCH_TO_SDR, REBUILD_DISPLAY }

    fun isFp16Pq(pixelStride: Int, actualDataSpace: Int, bt2020Pq: Int): Boolean =
        pixelStride >= 8 && actualDataSpace == bt2020Pq

    /**
     * Pure timing decision for the "no frames ever arrive" failure mode.
     * Times are elapsedRealtime ms; [lastFrameAtMs] == 0 means no frame since the last
     * reader switch. [hdrStartedAtMs]/[sdrSwitchedAtMs] == 0 mean "not applicable".
     * The rebuild step happens at most once ([displayRebuilt]), so this cannot loop.
     */
    fun watchdog(
        hdrReaderActive: Boolean,
        hdrStartedAtMs: Long,
        sdrSwitchedAtMs: Long,
        displayRebuilt: Boolean,
        lastFrameAtMs: Long,
        nowMs: Long,
    ): WatchdogAction {
        if (hdrReaderActive) {
            val frameSeen = lastFrameAtMs > 0L && lastFrameAtMs >= hdrStartedAtMs
            return if (hdrStartedAtMs > 0L && !frameSeen && nowMs - hdrStartedAtMs >= HDR_NO_FRAME_TIMEOUT_MS) {
                WatchdogAction.SWITCH_TO_SDR
            } else WatchdogAction.NONE
        }
        val frameSeen = lastFrameAtMs > 0L && lastFrameAtMs >= sdrSwitchedAtMs
        return if (sdrSwitchedAtMs > 0L && !displayRebuilt && !frameSeen &&
            nowMs - sdrSwitchedAtMs >= SDR_NO_FRAME_TIMEOUT_MS
        ) WatchdogAction.REBUILD_DISPLAY else WatchdogAction.NONE
    }

    /**
     * Runs [read] against an HDR frame (plane/buffer/metadata access). Any exception counts
     * as an HDR failure: [fallback] runs once and null is returned so the caller drops the frame.
     */
    fun <T> guardHdrRead(read: () -> T?, fallback: () -> Unit): T? = try {
        read()
    } catch (_: Exception) {
        fallback()
        null
    }

    fun <T> acquire(hdrReader: Boolean, read: () -> T?, fallback: () -> Unit): T? = try {
        read()
    } catch (error: RuntimeException) {
        if (!hdrReader) throw error
        fallback()
        null
    }
}
