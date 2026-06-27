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
 * - Receiver is not exported (android:exported="false") — only
 *   processes with the same UID or root (ADB) can send to it.
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
        Log.i(TAG, "Session token set (${token.length} chars)")
    }

    companion object {
        private const val TAG = "HermesToken"
    }
}
