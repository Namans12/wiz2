package com.wizlight.app

import org.json.JSONArray

internal fun parseSyncTargets(rawJson: String): List<ColorSyncEngine.BulbTarget> {
    val array = JSONArray(rawJson)
    return (0 until array.length()).mapNotNull { index ->
        val item = array.optJSONObject(index) ?: return@mapNotNull null
        val ip = item.optString("ip")
        if (ip.isBlank() || !item.optBoolean("screenSyncEnabled", true)) return@mapNotNull null
        ColorSyncEngine.BulbTarget(ip, RegionLayout.normalize(item.optString("region")))
    }.distinctBy { it.ip }
}
