package com.wizlight.app

import org.json.JSONArray

/** Lights that were in the running session and are not in the new target list. */
internal fun removedTargetIps(
    old: List<ColorSyncEngine.BulbTarget>,
    new: List<ColorSyncEngine.BulbTarget>,
): List<String> {
    val keep = new.map { it.ip }.toSet()
    return old.map { it.ip }.distinct().filter { it !in keep }
}

internal fun parseSyncTargets(rawJson: String): List<ColorSyncEngine.BulbTarget> {
    val array = JSONArray(rawJson)
    return (0 until array.length()).mapNotNull { index ->
        val item = array.optJSONObject(index) ?: return@mapNotNull null
        val ip = item.optString("ip")
        if (ip.isBlank() || !item.optBoolean("screenSyncEnabled", true)) return@mapNotNull null
        ColorSyncEngine.BulbTarget(ip, RegionLayout.normalize(item.optString("region")))
    }.distinctBy { it.ip }
}

/**
 * Which colour each light receives.
 * Single mode (or any frame carrying the whole-screen "all" colour): every light gets that one
 * colour and its per-light area is ignored. Regions mode: each light gets its own area's colour
 * (average of all areas for a light whose area was not sampled).
 */
internal fun bulbColorsFor(
    zones: Boolean,
    bulbs: List<ColorSyncEngine.BulbTarget>,
    targetColors: Map<String, ColorSyncEngine.RgbColor>,
    average: (List<ColorSyncEngine.RgbColor>) -> ColorSyncEngine.RgbColor,
): Map<String, ColorSyncEngine.RgbColor> {
    if (bulbs.isEmpty() || targetColors.isEmpty()) return emptyMap()
    if (!zones || targetColors.containsKey("all")) {
        val single = targetColors["all"] ?: average(targetColors.values.toList())
        return bulbs.associate { it.ip to single }
    }
    val fallback = average(targetColors.values.toList())
    return bulbs.associate { bulb -> bulb.ip to (targetColors[bulb.region] ?: fallback) }
}

/**
 * Requested mode -> effective engine mode. "single" always means one whole-screen colour for
 * every light. "zones" needs two lights on two different areas, otherwise it is the same thing
 * as single and runs as single.
 */
internal fun resolveSyncMode(mode: String, bulbs: List<ColorSyncEngine.BulbTarget>): String {
    if (mode != "zones") return "single"
    val regions = bulbs.map { it.region }.toSet()
    return if (regions.size >= 2 && bulbs.size >= 2) "zones" else "single"
}
