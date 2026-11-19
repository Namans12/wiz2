package com.wizlight.app

import android.app.Activity
import android.os.Bundle
import android.widget.Toast

/** No-UI launcher entry for Android TV: stops the running sync session and finishes at once. */
class StopSyncActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Goes through the registry (same process) so Dart sees "stopping" first.
        if (!SyncSessionRegistry.stopCurrent()) {
            Toast.makeText(applicationContext, "Sync is not running", Toast.LENGTH_SHORT).show()
        }
        finish()
    }
}
