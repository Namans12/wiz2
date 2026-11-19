package com.wizlight.app

import android.app.PendingIntent
import android.content.Intent
import android.os.Build
import android.service.quicksettings.Tile
import android.service.quicksettings.TileService

/** Quick Settings tile: active while syncing; tap stops, or opens the app when stopped. */
class StopSyncTileService : TileService() {
    override fun onStartListening() {
        super.onStartListening()
        refresh()
    }

    override fun onClick() {
        super.onClick()
        if (SyncSessionRegistry.stopCurrent()) {
            refresh(forceInactive = true)
            return
        }
        val launch = Intent(this, MainActivity::class.java).apply {
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
        }
        if (Build.VERSION.SDK_INT >= 34) {
            val pending = PendingIntent.getActivity(
                this, 0, launch, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
            )
            startActivityAndCollapse(pending)
        } else {
            @Suppress("DEPRECATION")
            startActivityAndCollapse(launch)
        }
    }

    private fun refresh(forceInactive: Boolean = false) {
        val tile = qsTile ?: return
        val active = !forceInactive && SyncSessionRegistry.isActive()
        tile.state = if (active) Tile.STATE_ACTIVE else Tile.STATE_INACTIVE
        tile.label = "WizLight sync"
        if (Build.VERSION.SDK_INT >= 29) tile.subtitle = if (active) "Tap to stop" else "Tap to open"
        tile.updateTile()
    }
}
