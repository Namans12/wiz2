package com.wizlight.app

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Drives the real [ColorSyncEngine] frame pipeline (fake clock, packets captured instead of a
 * socket) and shows that each setting reaches the engine and changes its output or behaviour,
 * and that Single / Regions mean what the UI says.
 */
class EngineSettingsTest {
    private val n = 12

    private class Rig(configure: ColorSyncEngine.() -> Unit = {}) {
        val engine = ColorSyncEngine()
        var now = 10_000L
        val batches = mutableListOf<Map<String, String>>()
        var accept = true

        init {
            engine.clock = { now }
            engine.batchSink = { payloads ->
                if (accept) batches.add(payloads.mapValues { String(it.value) })
                accept
            }
            engine.configure()
            engine.reset() // reset() seeds currentFps etc. from the settings above
        }

        fun frame(grids: Map<String, IntArray>, size: Int, stepMs: Long = 100L): Boolean {
            now += stepMs
            return engine.submitFrame(grids, size)
        }
    }

    private fun argb(r: Int, g: Int, b: Int) = (0xFF shl 24) or (r shl 16) or (g shl 8) or b
    private fun solid(r: Int, g: Int, b: Int) = IntArray(n * n) { argb(r, g, b) }
    private fun single(r: Int, g: Int, b: Int) = mapOf("all" to solid(r, g, b))
    private fun bulb(ip: String, region: String) = ColorSyncEngine.BulbTarget(ip, region)
    private val twoLamps = listOf(bulb("10.0.0.1", "left"), bulb("10.0.0.2", "right"))

    private fun Rig.singleColourOutput(grids: Map<String, IntArray> = single(200, 120, 80)): Map<String, String> {
        frame(grids, n)
        return batches.last()
    }

    // ── 1. Single vs Regions ──────────────────────────────────────────────────────────

    /** Left half red, right half blue: the whole-frame colour is a blend of both. */
    private fun splitFrame() = IntArray(n * n) { if (it % n < n / 2) argb(220, 30, 30) else argb(30, 30, 220) }

    @Test fun singleModeGivesEveryLampTheSameColourWhateverItsArea() {
        val rig = Rig { effectiveMode = "single"; bulbTargets = twoLamps }
        val out = rig.singleColourOutput(mapOf("all" to splitFrame()))
        assertEquals(2, out.size)
        assertEquals("both lamps share one packet in Single mode", out["10.0.0.1"], out["10.0.0.2"])
        assertEquals(setOf("all"), rig.engine.lastTargetColors.keys)
    }

    @Test fun singleModeIgnoresAreaAssignmentsEntirely() {
        fun run(a: String, b: String): Map<String, String> {
            val rig = Rig { effectiveMode = "single"; bulbTargets = listOf(bulb("10.0.0.1", a), bulb("10.0.0.2", b)) }
            return rig.singleColourOutput(mapOf("all" to splitFrame()))
        }
        val leftRight = run("left", "right")
        assertEquals(leftRight, run("right", "left"))
        assertEquals(leftRight, run("center", "center"))
        assertEquals(leftRight, run("top", "bottom"))
    }

    @Test fun regionsModeGivesEachLampItsOwnAreaColour() {
        val grids = mapOf(
            "all" to splitFrame(),
            "left" to solid(220, 30, 30),
            "right" to solid(30, 30, 220),
        )
        val rig = Rig { effectiveMode = "zones"; bulbTargets = twoLamps }
        val out = rig.singleColourOutput(grids)
        assertNotEquals(out["10.0.0.1"], out["10.0.0.2"])
        assertEquals(setOf("left", "right"), rig.engine.lastTargetColors.keys)
        val left = rig.engine.lastTargetColors.getValue("left")
        val right = rig.engine.lastTargetColors.getValue("right")
        assertTrue("left lamp follows the red side", left.r > left.b)
        assertTrue("right lamp follows the blue side", right.b > right.r)

        // Assignments are honoured in Regions: swap the areas and the packets swap.
        val swapped = Rig {
            effectiveMode = "zones"
            bulbTargets = listOf(bulb("10.0.0.1", "right"), bulb("10.0.0.2", "left"))
        }.singleColourOutput(grids)
        assertEquals(out["10.0.0.1"], swapped["10.0.0.2"])
        assertEquals(out["10.0.0.2"], swapped["10.0.0.1"])
    }

    @Test fun lampAreasAreKeptWhenSwitchingModes() {
        val lamps = twoLamps
        assertEquals("single", resolveSyncMode("single", lamps))
        assertEquals("zones", resolveSyncMode("zones", lamps))
        // Regions with every lamp on one area is the same as Single.
        assertEquals("single", resolveSyncMode("zones", listOf(bulb("a", "left"), bulb("b", "left"))))
        assertEquals("single", resolveSyncMode("zones", listOf(bulb("a", "left"))))
    }

    @Test fun bulbColoursForHelperSemantics() {
        val red = ColorSyncEngine.RgbColor(255, 0, 0)
        val blue = ColorSyncEngine.RgbColor(0, 0, 255)
        val avg = { colors: List<ColorSyncEngine.RgbColor> -> colors.first() }
        val single = bulbColorsFor(false, twoLamps, mapOf("all" to red), avg)
        assertEquals(mapOf("10.0.0.1" to red, "10.0.0.2" to red), single)
        val zones = bulbColorsFor(true, twoLamps, mapOf("left" to red, "right" to blue), avg)
        assertEquals(mapOf("10.0.0.1" to red, "10.0.0.2" to blue), zones)
    }

    @Test fun twoLampsOnDifferentBulbTablesCanLegitimatelyDiffer() {
        // Same colour, different table (bulb type / probe pending): payloads may differ even in
        // Single mode. The pure mapping still hands both lamps the same *colour*.
        val colours = bulbColorsFor(false, twoLamps, mapOf("all" to ColorSyncEngine.RgbColor(10, 20, 30)), { it.first() })
        assertEquals(colours["10.0.0.1"], colours["10.0.0.2"])
    }

    // ── 2. Settings reach the engine and change behaviour ─────────────────────────────

    @Test fun maxFpsSetsTheFrameRateCeiling() {
        fun fps(max: Int): Int {
            val rig = Rig { bulbTargets = twoLamps; adaptiveFps = false; maxFps = max }
            rig.frame(single(100, 100, 100), n)
            return rig.engine.currentFps
        }
        assertEquals(12, fps(12))
        assertEquals(30, fps(30))
    }

    @Test fun minFpsSetsTheStaticSceneFloorWhenAdaptive() {
        fun fps(min: Int): Int {
            val rig = Rig { bulbTargets = twoLamps; adaptiveFps = true; minFps = min; maxFps = 30 }
            repeat(3) { rig.frame(single(100, 100, 100), n) }  // static scene -> no motion
            return rig.engine.currentFps
        }
        assertEquals(6, fps(6))
        assertEquals(15, fps(15))
    }

    @Test fun adaptiveFpsTogglesBetweenFloorAndCeiling() {
        fun fps(adaptive: Boolean): Int {
            val rig = Rig { bulbTargets = twoLamps; adaptiveFps = adaptive; minFps = 8; maxFps = 24 }
            repeat(3) { rig.frame(single(100, 100, 100), n) }
            return rig.engine.currentFps
        }
        assertEquals(8, fps(true))
        assertEquals(24, fps(false))
    }

    @Test fun smoothingChangesHowFastLampsFollowAChange() {
        fun run(smoothing: Double): Pair<Double, ColorSyncEngine.RgbColor> {
            val rig = Rig { bulbTargets = twoLamps; this.smoothing = smoothing; predictiveSmoothing = false }
            rig.frame(single(120, 60, 60), n)
            rig.frame(single(150, 60, 60), n)
            return rig.engine.lastSmoothingFactor to rig.engine.currentOutputColors.getValue("10.0.0.1")
        }
        val (fastFactor, fastColour) = run(0.05)
        val (slowFactor, slowColour) = run(0.85)
        assertTrue("more smoothing -> smaller per-frame step ($fastFactor vs $slowFactor)", slowFactor < fastFactor)
        assertNotEquals(fastColour, slowColour)
    }

    @Test fun colourBoostChangesTheOutputColour() {
        fun colour(boost: Double): ColorSyncEngine.RgbColor {
            val rig = Rig { bulbTargets = twoLamps; colorBoost = boost }
            rig.frame(single(200, 120, 80), n)
            return rig.engine.lastTargetColors.getValue("all")
        }
        val flat = colour(1.0)
        val boosted = colour(1.8)
        assertNotEquals(flat, boosted)
        assertTrue("boost raises saturation", boosted.r - boosted.b > flat.r - flat.b)
    }

    @Test fun edgeWeightShiftsTheColourTowardTheScreenEdges() {
        // Red border, blue middle: more edge weight must pull the extracted colour toward red.
        val frame = IntArray(n * n) {
            val x = it % n; val y = it / n
            val edge = x < 2 || y < 2 || x >= n - 2 || y >= n - 2
            if (edge) argb(210, 40, 40) else argb(40, 40, 210)
        }
        fun colour(weight: Double): ColorSyncEngine.RgbColor {
            val rig = Rig { bulbTargets = twoLamps; edgeWeight = weight; colorAlgorithm = "weighted"; colorBoost = 1.0 }
            rig.frame(mapOf("all" to frame), n)
            return rig.engine.lastTargetColors.getValue("all")
        }
        val low = colour(1.0)
        val high = colour(2.5)
        assertNotEquals(low, high)
        assertTrue("edge weight pulls toward the red border", high.r - high.b > low.r - low.b)
    }

    @Test fun algorithmSettingChangesTheExtractedColour() {
        // Mostly dull grey with a small saturated patch: extractors disagree.
        val frame = IntArray(n * n) { if (it < 16) argb(240, 20, 20) else argb(90, 90, 100) }
        fun colour(algorithm: String): ColorSyncEngine.RgbColor {
            val rig = Rig { bulbTargets = twoLamps; colorAlgorithm = algorithm; colorBoost = 1.0 }
            rig.frame(mapOf("all" to frame), n)
            return rig.engine.lastTargetColors.getValue("all")
        }
        val results = listOf("auto", "weighted", "histogram", "kmeans").associateWith(::colour)
        assertTrue("at least two algorithms differ: $results", results.values.toSet().size > 1)
        assertNotEquals(results["weighted"], results["histogram"])
    }

    @Test fun algorithmNamesAreNormalised() {
        val engine = ColorSyncEngine()
        for (name in listOf("auto", "weighted", "kmeans", "histogram")) assertEquals(name, engine.normalizeAlgorithm(name))
        assertEquals("auto", engine.normalizeAlgorithm("nonsense"))
    }

    @Test fun minBrightnessIsTheDarkSceneFloor() {
        fun brightness(floor: Int): Int {
            val rig = Rig { bulbTargets = twoLamps; minBrightness = floor }
            rig.frame(single(70, 25, 25), n)
            return rig.engine.outputBrightness
        }
        assertTrue(brightness(60) > brightness(10))
    }

    @Test fun followBrightnessOffPinsBrightnessAtFull() {
        fun brightness(follow: Boolean): Int {
            val rig = Rig { bulbTargets = twoLamps; brightnessFollow = follow }
            rig.frame(single(70, 25, 25), n)
            return rig.engine.outputBrightness
        }
        assertEquals(255, brightness(false))
        assertTrue(brightness(true) < 255)
    }

    @Test fun minColourDeltaGatesWhichChangesAreSent() {
        fun resent(delta: Int): Boolean {
            val rig = Rig {
                bulbTargets = twoLamps; minColorDelta = delta; smoothing = 0.05
                predictiveSmoothing = false; adaptiveFps = false; brightnessFollow = false
            }
            assertTrue(rig.frame(single(180, 90, 90), n))
            return rig.frame(single(200, 90, 90), n)  // modest change, no scene cut
        }
        assertTrue("small delta sends the change", resent(2))
        assertFalse("large delta suppresses it", resent(48))
    }

    @Test fun minColourDeltaFeedsTheThresholdTheDiagnosticsShow() {
        fun threshold(delta: Int, adaptive: Boolean): Int {
            val rig = Rig { bulbTargets = twoLamps; minColorDelta = delta; adaptiveFps = adaptive }
            rig.frame(single(100, 100, 100), n)
            return rig.engine.lastDeltaThreshold
        }
        assertEquals(8, threshold(8, adaptive = false))
        assertEquals(20, threshold(20, adaptive = false))
        // Adaptive adds up to a profile-dependent slack on static scenes (6 generic, 5 wide-gamut HDR):
        // delta 8 on the user's tablet reads as 13.
        assertEquals(14, threshold(8, adaptive = true))
    }

    @Test fun adaptiveFpsAlsoWidensTheDeltaOnStaticScenes() {
        fun threshold(adaptive: Boolean): Int {
            val rig = Rig { bulbTargets = twoLamps; minColorDelta = 8; adaptiveFps = adaptive }
            rig.frame(single(100, 100, 100), n)
            return rig.engine.lastDeltaThreshold
        }
        assertTrue(threshold(true) > threshold(false))
    }

    @Test fun wideGamutProfileShowsThirteenOnAStaticSceneAtDelta8() {
        val rig = Rig {
            bulbTargets = twoLamps; minColorDelta = 8; adaptiveFps = true
            deviceProfile = ColorSyncEngine.DeviceProfile.WIDE_GAMUT_HDR
        }
        rig.frame(single(100, 100, 100), n)
        assertEquals(13, rig.engine.lastDeltaThreshold)  // 8 + round(5)
    }

    @Test fun predictiveSmoothingLeadsTheTargetOnSteadyChange() {
        fun colour(predictive: Boolean): ColorSyncEngine.RgbColor {
            val rig = Rig { bulbTargets = twoLamps; predictiveSmoothing = predictive; smoothing = 0.05; adaptiveFps = false }
            rig.frame(single(100, 60, 60), n)
            rig.frame(single(120, 60, 60), n)
            return rig.engine.currentOutputColors.getValue("10.0.0.1")
        }
        assertNotEquals(colour(true), colour(false))
    }

    @Test fun syncProfileChangesSmoothingAndThreshold() {
        fun run(profile: String): Pair<Double, Int> {
            val rig = Rig { bulbTargets = twoLamps; syncProfile = profile; smoothing = 0.5 }
            rig.frame(single(120, 60, 60), n)
            rig.frame(single(140, 60, 60), n)
            return rig.engine.lastSmoothingFactor to rig.engine.lastDeltaThreshold
        }
        val auto = run("auto")
        val cinema = run("cinema")
        val action = run("action")
        assertTrue("action responds faster than auto", action.first > auto.first)
        assertTrue("cinema settles slower than auto", cinema.first < auto.first)
        assertTrue("action sends smaller changes", action.second < auto.second)
        assertTrue("cinema holds back more", cinema.second > auto.second)
    }

    @Test fun syncProfileNamesAreNormalised() {
        val engine = ColorSyncEngine()
        assertEquals("cinema", engine.normalizeSyncProfile("CINEMA"))
        assertEquals("action", engine.normalizeSyncProfile("action"))
        assertEquals("auto", engine.normalizeSyncProfile("nope"))
    }

    @Test fun explicitSourceColourSpaceChangesTheColour() {
        fun colour(space: String): ColorSyncEngine.RgbColor {
            val rig = Rig {
                bulbTargets = twoLamps; colorBoost = 1.0
                requestedColorSpace = space; requestedHdrToneMapping = false; applyRequestedColorSpace()
            }
            rig.frame(single(40, 200, 90), n)
            return rig.engine.lastTargetColors.getValue("all")
        }
        val srgb = colour("srgb")
        assertNotEquals(srgb, colour("display-p3"))
        assertNotEquals(srgb, colour("rec2020"))
    }

    @Test fun autoSourceColourSpaceForcesToneMappingOff() {
        val engine = ColorSyncEngine()
        engine.requestedColorSpace = "auto"
        engine.requestedHdrToneMapping = true
        engine.applyRequestedColorSpace()
        assertEquals("srgb", engine.sourceColorSpace)
        assertFalse("the user's diagnostics: HDR tone map false even with the switch on", engine.hdrToneMapping)
    }

    @Test fun explicitSourceColourSpaceHonoursTheToneMappingSwitch() {
        val engine = ColorSyncEngine()
        engine.requestedColorSpace = "rec2020"
        engine.requestedHdrToneMapping = true
        engine.applyRequestedColorSpace()
        assertEquals("rec2020", engine.sourceColorSpace)
        assertTrue(engine.hdrToneMapping)
    }

    @Test fun hdrToneMappingChangesTheColourWhenAColourSpaceIsChosen() {
        fun colour(tone: Boolean): ColorSyncEngine.RgbColor {
            val rig = Rig {
                bulbTargets = twoLamps; colorBoost = 1.0
                requestedColorSpace = "rec2020"; requestedHdrToneMapping = tone; applyRequestedColorSpace()
            }
            rig.frame(single(230, 200, 150), n)
            return rig.engine.lastTargetColors.getValue("all")
        }
        assertNotEquals(colour(false), colour(true))
    }

    @Test fun hdrAwareCaptureOutputBypassesTheSecondConversion() {
        // HDR capture converts PQ -> sRGB itself; the engine must then not convert again.
        fun colour(captured: Boolean): ColorSyncEngine.RgbColor {
            val rig = Rig {
                bulbTargets = twoLamps; colorBoost = 1.0
                requestedColorSpace = "rec2020"; requestedHdrToneMapping = true; applyRequestedColorSpace()
                capturedPixelsAreSrgb = captured
            }
            rig.frame(single(230, 200, 150), n)
            return rig.engine.lastTargetColors.getValue("all")
        }
        assertNotEquals(colour(false), colour(true))
    }

    // ── 3. HDR capture reader gating / reporting ──────────────────────────────────────

    @Test fun hdrGateAttemptsOnlyWhenRequestedOnAndroid14WithAnHdrDisplay() {
        val g = CaptureFormatPolicy::hdrGate
        assertEquals(CaptureFormatPolicy.HdrGate.NOT_REQUESTED, g(false, false, 34, true, false))
        assertEquals(CaptureFormatPolicy.HdrGate.ALREADY_FELL_BACK, g(true, true, 34, true, false))
        assertEquals(CaptureFormatPolicy.HdrGate.SDK_TOO_OLD, g(true, false, 33, true, false))
        assertEquals(CaptureFormatPolicy.HdrGate.DISPLAY_NOT_HDR, g(true, false, 34, false, false))
        assertEquals(CaptureFormatPolicy.HdrGate.ATTEMPT, g(true, false, 34, true, false))
        assertEquals(CaptureFormatPolicy.HdrGate.ATTEMPT, g(true, false, 35, false, true))
    }

    @Test fun skippedOrRejectedHdrReaderIsNeverReportedAsPlainRgba8888() {
        assertEquals("rgba8888", CaptureFormatPolicy.sdrBufferLabel(false, null))
        assertEquals("rgba8888 (HDR unavailable)", CaptureFormatPolicy.sdrBufferLabel(true, null))
        assertEquals(
            "rgba8888 (HDR unavailable: needs Android 14)",
            CaptureFormatPolicy.sdrBufferLabel(true, CaptureFormatPolicy.HdrGate.SDK_TOO_OLD.reason),
        )
    }

    // ── native argument keys: Dart settings -> Intent extras ───────────────────────────

    @Test fun extraKeysMatchTheKeysDartSends() {
        val dartKeys = setOf(
            "mode", "minFps", "maxFps", "smoothing", "sampleSize", "colorBoost", "edgeWeight",
            "colorAlgorithm", "minBrightness", "minColorDelta", "adaptiveFps", "predictiveSmoothing",
            "ignoreLetterbox", "sourceColorSpace", "hdrToneMapping", "hdrCapture", "syncProfile",
            "brightnessFollow",
        )
        val extras = setOf(
            ScreenCaptureService.EXTRA_MODE, ScreenCaptureService.EXTRA_MIN_FPS, ScreenCaptureService.EXTRA_MAX_FPS,
            ScreenCaptureService.EXTRA_SMOOTHING, ScreenCaptureService.EXTRA_SAMPLE_SIZE,
            ScreenCaptureService.EXTRA_COLOR_BOOST, ScreenCaptureService.EXTRA_EDGE_WEIGHT,
            ScreenCaptureService.EXTRA_COLOR_ALGORITHM, ScreenCaptureService.EXTRA_MIN_BRIGHTNESS,
            ScreenCaptureService.EXTRA_MIN_COLOR_DELTA, ScreenCaptureService.EXTRA_ADAPTIVE_FPS,
            ScreenCaptureService.EXTRA_PREDICTIVE_SMOOTHING, ScreenCaptureService.EXTRA_IGNORE_LETTERBOX,
            ScreenCaptureService.EXTRA_SOURCE_COLOR_SPACE, ScreenCaptureService.EXTRA_HDR_TONE_MAPPING,
            ScreenCaptureService.EXTRA_HDR_CAPTURE, ScreenCaptureService.EXTRA_SYNC_PROFILE,
            ScreenCaptureService.EXTRA_BRIGHTNESS_FOLLOW,
        )
        assertEquals(dartKeys, extras)
    }
}
