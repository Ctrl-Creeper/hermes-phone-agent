package com.hermes.phoneagent

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log

/**
 * Receives the session authentication token from the host via:
 *   adb shell am broadcast -a com.hermes.phoneagent.SET_TOKEN \
 *     -n com.hermes.phoneagent/.TokenReceiver --es token <hex>
 *
 * SECURITY:
 * - Receiver remains private. Current hosts start the protected socket
 *   service directly; this receiver is retained for same-UID compatibility.
 * - Token is stored in memory only (EventBus.sessionToken),
 *   never persisted to disk.
 * - Token is validated on every socket connection handshake.
 */
class TokenReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        val token = intent.getStringExtra("token")
        if (token.isNullOrBlank()) {
            Log.w(TAG, "SET_TOKEN broadcast with empty token — ignored")
            return
        }
        if (token.length < 32) {
            Log.w(TAG, "SET_TOKEN token too short (${token.length} chars) — ignored")
            return
        }
        EventBus.setToken(token)
        // The host sends this broadcast for each session. Starting the
        // service here keeps the service private (exported=false) while
        // still allowing ADB-driven sessions to start it on demand.
        val serviceIntent = Intent(context, EventSocketService::class.java)
        context.startForegroundService(serviceIntent)
        Log.i(TAG, "Session token set (${token.length} chars)")
    }

    companion object {
        private const val TAG = "HermesToken"
    }
}
