package com.hermes.phoneagent

import org.json.JSONObject
import java.util.concurrent.CopyOnWriteArrayList

/**
 * Simple in-process event bus. Services post events here;
 * EventSocketService reads and forwards them to the host.
 *
 * SECURITY: Events are only forwarded if a valid session token
 * has been set via TokenReceiver. Without a token, the socket
 * service does not authenticate and events are dropped.
 */
object EventBus {

    interface Listener {
        fun onEvent(event: JSONObject)
    }

    private val listeners = CopyOnWriteArrayList<Listener>()

    @Volatile
    var sessionToken: String? = null
        private set

    fun setToken(token: String) {
        sessionToken = token
    }

    fun clearToken() {
        sessionToken = null
    }

    fun register(listener: Listener) {
        listeners.add(listener)
    }

    fun unregister(listener: Listener) {
        listeners.remove(listener)
    }

    fun post(event: JSONObject) {
        // Drop events if no active session.
        if (sessionToken == null) return
        for (listener in listeners) {
            try {
                listener.onEvent(event)
            } catch (e: Exception) {
                // Don't let one listener crash others.
            }
        }
    }
}
