package com.wizlight.app

/** Pure per-region helpers for zone mode (desktop _process_region parity). */
internal object ZoneOutput {
    /**
     * Grids that get their own stats and dimming. Capture always supplies "all" next to the
     * region grids in zone mode; "all" stays for scene-wide diagnostics only.
     */
    fun regionGrids(zones: Boolean, grids: Map<String, IntArray>): Map<String, IntArray> =
        if (zones) grids.filterKeys { it != "all" } else emptyMap()

    /** (average saturation, average value) of an ARGB grid, as the engine's scene stats. */
    fun gridStats(grid: IntArray): Pair<Double, Double> {
        var sat = 0.0
        var value = 0.0
        for (pixel in grid) {
            val r = (pixel shr 16) and 0xFF; val g = (pixel shr 8) and 0xFF; val b = pixel and 0xFF
            val mx = maxOf(r, g, b)
            if (mx > 0) sat += (mx - minOf(r, g, b)).toDouble() / mx
            value += mx / 255.0
        }
        val n = maxOf(1, grid.size).toDouble()
        return sat / n to value / n
    }

    /** Desktop _neutral_strength: smoothstep of how far average saturation sits under 0.06. */
    fun neutralStrength(avgSaturation: Double): Double {
        val t = ((0.06 - avgSaturation) / 0.06).coerceIn(0.0, 1.0)
        return t * t * (3.0 - 2.0 * t)
    }
}

/** One dimming smoother per region, so a dark zone never rides a bright zone's brightness. */
internal class ZoneDimming {
    private val policies = mutableMapOf<String, DimmingPolicy>()

    fun reset() = policies.clear()

    /** region -> (average value 0..1, peak channel of the region's colour). */
    fun update(regions: Map<String, Pair<Double, Int>>, floor: Int, follow: Boolean): Map<String, Int> =
        regions.mapValues { (region, stats) ->
            policies.getOrPut(region) { DimmingPolicy() }.value(stats.first, floor, stats.second, follow)
        }

    companion object {
        /** Dimming for one light, falling back to [fallback] for lights without a region result. */
        fun forLight(ip: String, targets: List<ColorSyncEngine.BulbTarget>, perRegion: Map<String, Int>, fallback: Int): Int {
            if (perRegion.isEmpty()) return fallback
            return perRegion[targets.firstOrNull { it.ip == ip }?.region] ?: fallback
        }
    }
}
