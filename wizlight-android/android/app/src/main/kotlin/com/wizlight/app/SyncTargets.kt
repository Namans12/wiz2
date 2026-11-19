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
