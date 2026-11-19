package com.wizlight.app

/**
 * Immutable snapshot of every user setting that can change while a session runs. The main thread
 * builds a new snapshot and hands it to the engine; the capture thread installs it between frames,
 * so a frame never sees half of an update.
 */
internal data class LiveSettings(
    val mode: String,
    val minFps: Int,
    val maxFps: Int,
    val smoothing: Double,
    val sampleSize: Int,
    val colorBoost: Double,
    val edgeWeight: Double,
    val colorAlgorithm: String,
    val minBrightness: Int,
    val minColorDelta: Int,
    val adaptiveFps: Boolean,
    val predictiveSmoothing: Boolean,
    val ignoreLetterbox: Boolean,
    val sourceColorSpace: String,
    val hdrToneMapping: Boolean,
    val hdrCapture: Boolean,
    val syncProfile: String,
    val brightnessFollow: Boolean,
) {
    companion object {
        /** Keys accepted by updateSettings. Same names Dart sends at start. */
        val KEYS = setOf(
            "mode", "minFps", "maxFps", "smoothing", "sampleSize", "colorBoost", "edgeWeight",
            "colorAlgorithm", "minBrightness", "minColorDelta", "adaptiveFps", "predictiveSmoothing",
            "ignoreLetterbox", "sourceColorSpace", "hdrToneMapping", "hdrCapture", "syncProfile",
            "brightnessFollow",
        )

        /** Settings that cannot change without rebuilding the ImageReader (a restart). */
        val RESTART_KEYS = setOf("hdrCapture")

        /**
         * [base] overridden by the keys present in [args], clamped to the same ranges as a start.
         * hdrCapture is never changed here: it needs a restart.
         */
        fun merge(base: LiveSettings, args: Map<String, Any?>): LiveSettings {
            fun int(key: String, old: Int, lo: Int, hi: Int) =
                (args[key] as? Number)?.toInt()?.coerceIn(lo, hi) ?: old
            fun dbl(key: String, old: Double, lo: Double, hi: Double) =
                (args[key] as? Number)?.toDouble()?.takeIf { it.isFinite() }?.coerceIn(lo, hi) ?: old
            fun flag(key: String, old: Boolean) = args[key] as? Boolean ?: old
            val helper = ColorSyncEngine()
            val minFps = int("minFps", base.minFps, 4, 30)
            return base.copy(
                mode = if (args["mode"] == "zones") "zones" else if (args["mode"] == "single") "single" else base.mode,
                minFps = minFps,
                maxFps = int("maxFps", base.maxFps, 4, 30).coerceAtLeast(minFps),
                smoothing = dbl("smoothing", base.smoothing, 0.05, 0.85),
                sampleSize = int("sampleSize", base.sampleSize, 24, 96),
                colorBoost = dbl("colorBoost", base.colorBoost, 1.0, 1.8),
                edgeWeight = dbl("edgeWeight", base.edgeWeight, 1.0, 2.5),
                colorAlgorithm = (args["colorAlgorithm"] as? String)?.let(helper::normalizeAlgorithm) ?: base.colorAlgorithm,
                minBrightness = int("minBrightness", base.minBrightness, 0, 80),
                minColorDelta = int("minColorDelta", base.minColorDelta, 2, 48),
                adaptiveFps = flag("adaptiveFps", base.adaptiveFps),
                predictiveSmoothing = flag("predictiveSmoothing", base.predictiveSmoothing),
                ignoreLetterbox = flag("ignoreLetterbox", base.ignoreLetterbox),
                sourceColorSpace = (args["sourceColorSpace"] as? String)?.let(helper::normalizeColorSpace) ?: base.sourceColorSpace,
                hdrToneMapping = flag("hdrToneMapping", base.hdrToneMapping),
                syncProfile = (args["syncProfile"] as? String)?.let(helper::normalizeSyncProfile) ?: base.syncProfile,
                brightnessFollow = flag("brightnessFollow", base.brightnessFollow),
            )
        }

        /** Keys that mean nothing to the USB player: it has no screen reader, letterbox or sample grid. */
        val PLAYER_IGNORED_KEYS = setOf("hdrCapture", "sampleSize", "ignoreLetterbox")

        /** Player reply: screen-only keys are neither applied nor ever "restart required". */
        fun reportForPlayer(args: Map<String, Any?>): Map<String, Any?> {
            val sent = args.keys.filter { it in KEYS && it !in PLAYER_IGNORED_KEYS }
            return mapOf("accepted" to true, "applied" to sent, "restartRequired" to emptyList<String>())
        }

        /** What the channel returns: which of the sent keys were applied live and which need a restart. */
        fun report(args: Map<String, Any?>, runningHdrCapture: Boolean): Map<String, Any?> {
            val sent = args.keys.filter { it in KEYS }
            val restart = sent.filter { it in RESTART_KEYS && args[it] != runningHdrCapture }
            return mapOf(
                "accepted" to true,
                "applied" to sent.filter { it !in RESTART_KEYS },
                "restartRequired" to restart,
            )
        }
    }
}
