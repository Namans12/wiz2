package com.wizlight.app

/** A requested buffer format is not proof of the producer's frame dataspace. */
internal object CaptureFormatPolicy {
    /** HDR reader must deliver a frame this soon after it started, else fall back to SDR. */
    const val HDR_NO_FRAME_TIMEOUT_MS = 1500L

    /** After switching to SDR, a frame must arrive this soon, else the display is rebuilt. */
    const val SDR_NO_FRAME_TIMEOUT_MS = 1000L

    enum class WatchdogAction { NONE, SWITCH_TO_SDR, REBUILD_DISPLAY }

    /** Why the HDR (FP16) reader is or is not attempted. Pure, so the gating is testable. */
    enum class HdrGate(val reason: String?) {
        NOT_REQUESTED(null),
        ALREADY_FELL_BACK(null),
        SDK_TOO_OLD("needs Android 14"),
        DISPLAY_NOT_HDR("display reports no HDR"),
        ATTEMPT(null),
    }

    const val MIN_HDR_SDK = 34

    fun hdrGate(requested: Boolean, alreadyFellBack: Boolean, sdk: Int, displayHdr: Boolean, bravia: Boolean): HdrGate = when {
        !requested -> HdrGate.NOT_REQUESTED
        alreadyFellBack -> HdrGate.ALREADY_FELL_BACK
        sdk < MIN_HDR_SDK -> HdrGate.SDK_TOO_OLD
        !displayHdr && !bravia -> HdrGate.DISPLAY_NOT_HDR
        else -> HdrGate.ATTEMPT
    }

    /**
     * Diagnostics label for the plain 8-bit buffer. Plain "rgba8888" means HDR capture was never
     * asked for; once it was asked for and could not be used the label says so (and why).
     */
    fun sdrBufferLabel(hdrRequestFailed: Boolean, reason: String?): String = when {
        !hdrRequestFailed -> "rgba8888"
        reason.isNullOrBlank() -> "rgba8888 (HDR unavailable)"
        else -> "rgba8888 (HDR unavailable: $reason)"
    }

    /** After a grid-affecting setting changed, stale replays are held back this long for a fresh frame. */
    const val STALE_REPLAY_GRACE_MS = 1000L

    /**
     * May the stored grids be replayed on a static screen? Never when the active mode needs an area
     * grid they lack; and, right after a sample-size / letterbox / mode change, not until a fresh frame
     * had [STALE_REPLAY_GRACE_MS] to arrive (the service nudges the display to produce one).
     */
    fun replayAllowed(
        freshNeededSinceMs: Long, nowMs: Long, zones: Boolean, gridKeys: Set<String>, areas: Set<String>,
    ): Boolean {
        if (zones && !gridKeys.containsAll(areas)) return false
        return freshNeededSinceMs == 0L || nowMs - freshNeededSinceMs >= STALE_REPLAY_GRACE_MS
    }

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
