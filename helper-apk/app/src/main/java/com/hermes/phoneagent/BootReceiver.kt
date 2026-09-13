package com.hermes.phoneagent

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log

/**
 * Records boot completion. The EventSocketService is started on demand by
 * TokenReceiver when the host creates an authenticated session.
 */
class BootReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED) {
            Log.i(TAG, "Boot completed — helper ready for a host session")
        }
    }

    companion object {
        private const val TAG = "HermesBoot"
    }
}
