package com.hermes.phoneagent

import android.accessibilityservice.AccessibilityService
import android.util.Log
import android.view.accessibility.AccessibilityEvent
import org.json.JSONObject

/**
 * Observes UI changes and posts events to EventBus.
 *
 * SECURITY:
 * - canRetrieveWindowContent is FALSE in the XML config — we cannot
 *   read arbitrary UI content, only event metadata.
 * - We extract: event type, package name, class name, content description.
 * - We do NOT extract: full view hierarchy, text field contents,
 *   passwords, or other sensitive UI data.
 * - Events are throttled to avoid flooding (200ms notification timeout
 *   in XML config).
 */
class PhoneAccessibilityService : AccessibilityService() {

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        event ?: return

        try {
            val eventType = when (event.eventType) {
                AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED -> "window_state_changed"
                AccessibilityEvent.TYPE_WINDOW_CONTENT_CHANGED -> "window_content_changed"
                AccessibilityEvent.TYPE_NOTIFICATION_STATE_CHANGED -> "notification_state_changed"
                else -> return  // Ignore event types we didn't subscribe to.
            }

            val packageName = event.packageName?.toString() ?: return
            if (packageName == this.packageName) return  // Skip our own events.

            // For window_content_changed, only forward if it's meaningful
            // (e.g., a new dialog, not every scroll tick).
            if (event.eventType == AccessibilityEvent.TYPE_WINDOW_CONTENT_CHANGED) {
                val className = event.className?.toString() ?: ""
                val isInteresting = className.contains("Dialog", ignoreCase = true)
                        || className.contains("Toast", ignoreCase = true)
                        || className.contains("PopupWindow", ignoreCase = true)
                        || className.contains("AlertDialog", ignoreCase = true)
                if (!isInteresting) return
            }

            val desc = event.contentDescription?.toString()?.take(MAX_DESC_LENGTH) ?: ""

            val json = JSONObject().apply {
                put("type", "ui_change")
                put("package", packageName)
                put("event", eventType)
                put("class", event.className?.toString() ?: "")
                put("body", desc)
                put("timestamp", System.currentTimeMillis() / 1000.0)
            }
            EventBus.post(json)
        } catch (e: Exception) {
            Log.e(TAG, "Error processing accessibility event", e)
        }
    }

    override fun onInterrupt() {
        Log.w(TAG, "Accessibility service interrupted")
    }

    companion object {
        private const val TAG = "HermesA11y"
        private const val MAX_DESC_LENGTH = 200
    }
}
