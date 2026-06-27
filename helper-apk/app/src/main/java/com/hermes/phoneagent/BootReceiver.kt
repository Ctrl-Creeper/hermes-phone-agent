package com.hermes.phoneagent

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log

/**
 * Restarts the EventSocketService after device reboot.
 * The service won't forward events until a new session token
 * is set via TokenReceiver, so there's no security risk from
 * auto-starting.
 */
class BootReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED) {
            Log.i(TAG, "Boot completed — starting EventSocketService")
            val serviceIntent = Intent(context, EventSocketService::class.java)
            context.startForegroundService(serviceIntent)
        }
    }

    companion object {
        private const val TAG = "HermesBoot"
    }
}
