package com.hermes.phoneagent

import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import android.util.Log
import org.json.JSONObject

/**
 * Listens for notifications and posts structured events to EventBus.
 *
 * SECURITY:
 * - We extract only: package, title, text, timestamp.
 * - We do NOT extract: icons, actions, extras, PendingIntents.
 * - Notification text is capped at 200 chars to limit data exposure.
 * - The host-side redaction layer strips OTPs and sensitive patterns
 *   before the text reaches the LLM.
 */
class PhoneNotificationListener : NotificationListenerService() {

    override fun onNotificationPosted(sbn: StatusBarNotification) {
        try {
            val notification = sbn.notification ?: return
            val extras = notification.extras ?: return

            val title = extras.getCharSequence("android.title")?.toString() ?: ""
            val text = extras.getCharSequence("android.text")?.toString() ?: ""

            // Skip empty notifications and system noise.
            if (title.isBlank() && text.isBlank()) return
            if (sbn.packageName == packageName) return  // Don't loop on our own notifications.

            val event = JSONObject().apply {
                put("type", "notification")
                put("package", sbn.packageName)
                put("title", title.take(MAX_FIELD_LENGTH))
                put("body", text.take(MAX_FIELD_LENGTH))
                put("timestamp", sbn.postTime / 1000.0)
            }
            EventBus.post(event)
        } catch (e: Exception) {
            Log.e(TAG, "Error processing notification", e)
        }
    }

    override fun onNotificationRemoved(sbn: StatusBarNotification) {
        // We don't track removals — the agent cares about arrivals.
    }

    companion object {
        private const val TAG = "HermesNotif"
        private const val MAX_FIELD_LENGTH = 200
    }
}
