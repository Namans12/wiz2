package com.wizlight.app

import android.app.UiModeManager
import android.content.Context
import android.content.res.Configuration

/** True on Android TV / Google TV (television UI mode or leanback feature). */
fun isTelevisionDevice(context: Context): Boolean {
    val ui = context.getSystemService(Context.UI_MODE_SERVICE) as? UiModeManager
    if (ui?.currentModeType == Configuration.UI_MODE_TYPE_TELEVISION) return true
    return context.packageManager.hasSystemFeature("android.software.leanback")
}
