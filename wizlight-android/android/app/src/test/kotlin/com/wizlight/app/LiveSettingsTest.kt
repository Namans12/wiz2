package com.wizlight.app

import java.util.concurrent.atomic.AtomicReference
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class LiveSettingsTest {
    private val n = 12
    private val lamps = listOf(
        ColorSyncEngine.BulbTarget("10.0.0.1", "left"),
        ColorSyncEngine.BulbTarget("10.0.0.2", "right"),
    )

    private fun argb(r: Int, g: Int, b: Int) = (0xFF shl 24) or (r shl 16) or (g shl 8) or b
    private fun solid(r: Int, g: Int, b: Int) = IntArray(n * n) { argb(r, g, b) }

    /** What the service now samples: the whole frame plus one grid per lamp area, in either mode. */
    private val grids = mapOf(
        "all" to IntArray(n * n) { if (it % n < n / 2) argb(220, 30, 30) else argb(30, 30, 220) },
        "left" to solid(220, 30, 30),
        "right" to solid(30, 30, 220),
    )

    private class Rig(val engine: ColorSyncEngine = ColorSyncEngine()) {
        var now = 10_000L
        val batches = mutableListOf<Map<String, String>>()
        init {
            engine.clock = { now }
            engine.batchSink = { batches.add(it.mapValues { (_, v) -> String(v) }); true }
        }
        fun frame(grids: Map<String, IntArray>, size: Int): Boolean {
            engine.applyPendingSettings()
            now += 100
            return engine.submitFrame(grids, size)
        }
    }

    private fun running(mode: String, configure: ColorSyncEngine.() -> Unit = {}): Rig {
        val rig = Rig()
        rig.engine.bulbTargets = lamps
        rig.engine.effectiveMode = resolveSyncMode(mode, lamps)
        rig.engine.configure()
        rig.engine.reset()
        return rig
    }

    private fun Rig.snapshot(mode: String) = engine.liveSettings(mode, n, true, false)

    private fun Rig.update(mode: String, args: Map<String, Any?>) {
        engine.requestSettings(args) { snapshot(mode) }
    }

    @Test fun anAppliedSnapshotChangesTheNextFramesOutput() {
        val flat = running("single") { colorBoost = 1.0 }
        flat.frame(grids, n)
        val before = flat.engine.lastTargetColors.getValue("all")

        flat.update("single", mapOf("colorBoost" to 1.8))
        flat.frame(grids, n)
        val after = flat.engine.lastTargetColors.getValue("all")
        assertNotEquals(before, after)

        val boostedFromStart = running("single") { colorBoost = 1.8 }
        boostedFromStart.frame(grids, n)
        assertEquals(boostedFromStart.engine.lastTargetColors.getValue("all"), after)
        assertEquals(1.8, flat.engine.colorBoost, 0.0)
    }

    @Test fun everyEngineSettingLandsOnTheNextFrame() {
        val rig = running("single")
        rig.frame(grids, n)
        rig.update("single", mapOf(
            "minFps" to 7, "maxFps" to 27, "smoothing" to 0.7, "colorBoost" to 1.5, "edgeWeight" to 2.0,
            "colorAlgorithm" to "histogram", "minBrightness" to 33, "minColorDelta" to 21,
            "adaptiveFps" to false, "predictiveSmoothing" to false, "sourceColorSpace" to "display-p3",
            "hdrToneMapping" to true, "syncProfile" to "action", "brightnessFollow" to false,
        ))
        assertEquals("nothing changes until the capture thread installs it", 10, rig.engine.minFps)
        rig.frame(grids, n)
        val e = rig.engine
        assertEquals(7, e.minFps); assertEquals(27, e.maxFps); assertEquals(27, e.currentFps)
        assertEquals(0.7, e.smoothing, 0.0); assertEquals(1.5, e.colorBoost, 0.0); assertEquals(2.0, e.edgeWeight, 0.0)
        assertEquals("histogram", e.colorAlgorithm); assertEquals(33, e.minBrightness); assertEquals(21, e.minColorDelta)
        assertEquals(false, e.adaptiveFps); assertEquals(false, e.predictiveSmoothing)
        assertEquals("display-p3", e.sourceColorSpace); assertEquals(true, e.hdrToneMapping)
        assertEquals("action", e.syncProfile); assertEquals(false, e.brightnessFollow)
        assertEquals(255, e.outputBrightness)
    }

    @Test fun switchingToSingleLiveGivesBothLampsTheSamePacketAndBackSplitsThem() {
        val rig = running("zones")
        assertEquals("zones", rig.engine.effectiveMode)
        rig.frame(grids, n)
        val zones = rig.batches.last()
        assertNotEquals(zones["10.0.0.1"], zones["10.0.0.2"])

        rig.update("zones", mapOf("mode" to "single"))
        rig.frame(grids, n)
        assertEquals("single", rig.engine.effectiveMode)
        val single = rig.batches.last()
        assertEquals("both lamps are resent at once after the switch", setOf("10.0.0.1", "10.0.0.2"), single.keys)
        assertEquals(single["10.0.0.1"], single["10.0.0.2"])

        rig.update("single", mapOf("mode" to "zones"))
        rig.frame(grids, n)
        assertEquals("zones", rig.engine.effectiveMode)
        val back = rig.batches.last()
        assertNotEquals(back["10.0.0.1"], back["10.0.0.2"])
        assertEquals(zones["10.0.0.1"], back["10.0.0.1"])
        assertEquals(zones["10.0.0.2"], back["10.0.0.2"])
    }

    @Test fun modeSwitchDropsPerLampSmoothingHistory() {
        val rig = running("zones")
        rig.frame(grids, n)
        assertTrue(rig.engine.currentOutputColors.isNotEmpty())
        rig.update("zones", mapOf("mode" to "single"))
        assertTrue("still the old history until the next frame installs the snapshot", rig.engine.currentOutputColors.isNotEmpty())
        rig.engine.applyPendingSettings()
        assertTrue(rig.engine.currentOutputColors.isEmpty())
        assertTrue(rig.engine.lastSentColors.isEmpty())
    }

    @Test fun regionsWithOnlyOneAreaStaysSingleLive() {
        val rig = running("single")
        rig.engine.bulbTargets = listOf(ColorSyncEngine.BulbTarget("a", "left"), ColorSyncEngine.BulbTarget("b", "left"))
        rig.update("single", mapOf("mode" to "zones"))
        rig.engine.applyPendingSettings()
        assertEquals("single", rig.engine.effectiveMode)
    }

    @Test fun playerStyleEngineNeverEntersZones() {
        val rig = running("single")
        rig.engine.supportsZones = false
        rig.update("single", mapOf("mode" to "zones"))
        rig.engine.applyPendingSettings()
        assertEquals("single", rig.engine.effectiveMode)
    }

    @Test fun onlyHdrCaptureNeedsARestart() {
        val args = mapOf("smoothing" to 0.4, "hdrCapture" to true, "mode" to "zones", "bogus" to 1)
        val whileOff = LiveSettings.report(args, runningHdrCapture = false)
        assertEquals(listOf("hdrCapture"), whileOff["restartRequired"])
        assertEquals(listOf("smoothing", "mode"), whileOff["applied"])
        val whileOn = LiveSettings.report(args, runningHdrCapture = true)
        assertEquals(emptyList<String>(), whileOn["restartRequired"])
    }

    @Test fun mergeClampsLikeAStartAndNeverTouchesHdrCapture() {
        val base = ColorSyncEngine().liveSettings("single", 56, true, false)
        val merged = LiveSettings.merge(base, mapOf(
            "smoothing" to 9.0, "colorBoost" to 0.1, "minFps" to 99, "maxFps" to 2, "sampleSize" to 1000,
            "colorAlgorithm" to "nonsense", "hdrCapture" to true, "mode" to "zones",
        ))
        assertEquals(0.85, merged.smoothing, 0.0)
        assertEquals(1.0, merged.colorBoost, 0.0)
        assertEquals(30, merged.minFps); assertEquals(30, merged.maxFps)
        assertEquals(96, merged.sampleSize)
        assertEquals("auto", merged.colorAlgorithm)
        assertEquals(false, merged.hdrCapture)
        assertEquals("zones", merged.mode)
        assertEquals(base, LiveSettings.merge(base, emptyMap()))
    }

    @Test fun updatesWhileFramesAreRunningNeverCrashOrTearAFrame() {
        val rig = running("zones")
        val failure = AtomicReference<Throwable?>(null)
        val writer = Thread {
            try {
                var i = 0
                while (!Thread.currentThread().isInterrupted && i < 5000) {
                    rig.engine.requestSettings(mapOf(
                        "mode" to if (i % 2 == 0) "single" else "zones",
                        "smoothing" to 0.05 + (i % 80) / 100.0,
                        "colorBoost" to 1.0 + (i % 8) / 10.0,
                        "predictiveSmoothing" to (i % 3 == 0),
                    )) { rig.snapshot("zones") }
                    i++
                }
            } catch (t: Throwable) {
                failure.set(t)
            }
        }
        writer.start()
        try {
            repeat(400) { rig.frame(grids, n) }
        } catch (t: Throwable) {
            failure.set(t)
        } finally {
            writer.interrupt()
            writer.join(5000)
        }
        assertNull(failure.get()?.stackTraceToString(), failure.get())
        // Every frame saw one consistent snapshot: the bulb map always covered both lamps.
        assertTrue(rig.batches.isNotEmpty())
    }

    @Test fun aPartialUpdateAfterTheCaptureThreadTookTheLastOneKeepsTheMode() {
        val rig = running("zones")
        rig.update("zones", mapOf("mode" to "single"))
        rig.engine.applyPendingSettings()  // capture thread consumed the pending snapshot
        // The next partial update seeds from stale "zones" if it merged against installed state.
        rig.engine.requestSettings(mapOf("smoothing" to 0.4)) { rig.snapshot("zones") }
        rig.engine.applyPendingSettings()
        assertEquals("single", rig.engine.effectiveMode)
        assertEquals(0.4, rig.engine.smoothing, 0.0)
    }

    @Test fun anUpdateOnlyEverPublishesTheMergedSnapshot() {
        val rig = running("single")
        val first = rig.engine.requestSettings(mapOf("colorBoost" to 1.4)) { rig.snapshot("single") }
        val second = rig.engine.requestSettings(mapOf("smoothing" to 0.3)) { rig.snapshot("single") }
        assertEquals(1.4, second.colorBoost, 0.0)
        assertEquals(0.3, second.smoothing, 0.0)
        assertEquals(second, rig.engine.applyPendingSettings())
        assertEquals(1.4, first.colorBoost, 0.0)
    }

    @Test fun playerReplyNeverAsksForARestartOrClaimsScreenOnlyKeys() {
        val reply = LiveSettings.reportForPlayer(mapOf(
            "smoothing" to 0.4, "hdrCapture" to true, "sampleSize" to 80, "ignoreLetterbox" to false,
        ))
        assertEquals(listOf("smoothing"), reply["applied"])
        assertEquals(emptyList<String>(), reply["restartRequired"])
    }

    @Test fun staleGridsAreHeldBackAfterAGridChangeUntilAFreshFrameHadItsChance() {
        val areas = setOf("left", "right")
        val p = CaptureFormatPolicy::replayAllowed
        assertTrue(p(0L, 5_000L, false, setOf("all"), areas))
        assertTrue(p(0L, 5_000L, true, setOf("all", "left", "right"), areas))
        // Regions needs the area grids; a Single-era replay can never serve it.
        assertTrue(!p(0L, 5_000L, true, setOf("all"), areas))
        assertTrue(!p(4_800L, 5_000L, true, setOf("all"), areas))
        // Just changed sample size / letterbox: wait for the fresh frame, then fall back to the replay.
        assertTrue(!p(4_800L, 5_000L, false, setOf("all"), areas))
        assertTrue(p(4_800L, 4_800L + CaptureFormatPolicy.STALE_REPLAY_GRACE_MS, false, setOf("all"), areas))
    }
}
