package com.hermes.phoneagent

import android.app.Notification
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import android.util.Log
import org.json.JSONObject

/**
 * Listens for notifications and posts structured events to EventBus.
 *
 * SECURITY:
 * - We extract only: package, application label, title, text, timestamp.
 * - We do NOT extract: icons, actions, extras, PendingIntents.
 * - Report-only events are delivered directly to the configured human channel.
 * - The host-side redaction layer protects content that reaches the LLM.
 */
class PhoneNotificationListener : NotificationListenerService() {

    override fun onNotificationPosted(sbn: StatusBarNotification) {
        try {
            val notification = sbn.notification ?: return
            val extras = notification.extras ?: return

            val title = extras.getCharSequence(Notification.EXTRA_TITLE)?.toString() ?: ""
            val text = (
                extras.getCharSequence(Notification.EXTRA_BIG_TEXT)
                    ?: extras.getCharSequence(Notification.EXTRA_TEXT)
            )?.toString() ?: ""
            val appName = try {
                val appInfo = packageManager.getApplicationInfo(sbn.packageName, 0)
                packageManager.getApplicationLabel(appInfo).toString()
            } catch (_: Exception) {
                sbn.packageName
            }

            // Skip empty notifications and system noise.
            if (title.isBlank() && text.isBlank()) return
            if (sbn.packageName == packageName) return  // Don't loop on our own notifications.

            val event = JSONObject().apply {
                put("type", "notification")
                put("package", sbn.packageName)
                put("notification_key", sbn.key)
                put("app_name", appName)
                put("title", title)
                put("body", text)
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
    }
}
