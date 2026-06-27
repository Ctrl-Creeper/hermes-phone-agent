#!/usr/bin/env bash
#
# Setup script for hermes-phone-agent.
#
# Installs the helper APK on the connected emulator and grants
# the required permissions (NotificationListener, AccessibilityService).
#
# Usage: ./setup.sh [--serial <device_serial>]
#
# SECURITY: This script only grants permissions to the helper APK.
# It does not modify system settings, install certificates, or
# enable developer options beyond what's already required for ADB.

set -euo pipefail

HELPER_PACKAGE="com.hermes.phoneagent"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APK_PATH="${SCRIPT_DIR}/helper-apk/releases/phone-agent-helper.apk"

# Parse args
ADB_SERIAL=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --serial|-s)
            ADB_SERIAL="$2"
            shift 2
            ;;
        *)
            echo "Usage: $0 [--serial <device_serial>]"
            exit 1
            ;;
    esac
done

ADB_CMD="adb"
if [[ -n "$ADB_SERIAL" ]]; then
    ADB_CMD="adb -s $ADB_SERIAL"
fi

echo "=== hermes-phone-agent setup ==="
echo ""

# Check ADB
if ! command -v adb &>/dev/null; then
    echo "ERROR: adb not found. Install Android SDK platform-tools."
    exit 1
fi

# Check device
echo "1. Checking device connection..."
DEVICE_STATE=$($ADB_CMD get-state 2>/dev/null || true)
if [[ "$DEVICE_STATE" != "device" ]]; then
    echo "ERROR: No device connected (state: ${DEVICE_STATE:-none})."
    echo "Start an emulator: emulator -avd <name>"
    exit 1
fi
echo "   Device connected."

# Install APK
echo "2. Installing helper APK..."
if [[ ! -f "$APK_PATH" ]]; then
    echo "   APK not found at: $APK_PATH"
    echo "   Build it first:"
    echo "     cd helper-apk && ./gradlew :app:assembleDebug"
    echo "     cp app/build/outputs/apk/debug/app-debug.apk releases/phone-agent-helper.apk"
    exit 1
fi
$ADB_CMD install -r "$APK_PATH"
echo "   Installed."

# Grant NotificationListener permission
echo "3. Granting NotificationListener permission..."
CURRENT=$($ADB_CMD shell settings get secure enabled_notification_listeners 2>/dev/null || true)
LISTENER="${HELPER_PACKAGE}/${HELPER_PACKAGE}.PhoneNotificationListener"
if [[ "$CURRENT" != *"$LISTENER"* ]]; then
    if [[ -n "$CURRENT" && "$CURRENT" != "null" ]]; then
        NEW="${CURRENT}:${LISTENER}"
    else
        NEW="$LISTENER"
    fi
    $ADB_CMD shell settings put secure enabled_notification_listeners "$NEW"
fi
echo "   NotificationListener granted."

# Grant AccessibilityService permission
echo "4. Granting AccessibilityService permission..."
A11Y_SERVICE="${HELPER_PACKAGE}/${HELPER_PACKAGE}.PhoneAccessibilityService"
CURRENT_A11Y=$($ADB_CMD shell settings get secure enabled_accessibility_services 2>/dev/null || true)
if [[ "$CURRENT_A11Y" != *"$A11Y_SERVICE"* ]]; then
    if [[ -n "$CURRENT_A11Y" && "$CURRENT_A11Y" != "null" ]]; then
        NEW_A11Y="${CURRENT_A11Y}:${A11Y_SERVICE}"
    else
        NEW_A11Y="$A11Y_SERVICE"
    fi
    $ADB_CMD shell settings put secure enabled_accessibility_services "$NEW_A11Y"
    $ADB_CMD shell settings put secure accessibility_enabled 1
fi
echo "   AccessibilityService granted."

# Start the socket service
echo "5. Starting EventSocketService..."
$ADB_CMD shell am startservice \
    -n "${HELPER_PACKAGE}/.EventSocketService" \
    --user 0 2>/dev/null || \
$ADB_CMD shell am start-foreground-service \
    -n "${HELPER_PACKAGE}/.EventSocketService" 2>/dev/null || true
echo "   Service started."

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Install hermes plugins:"
echo "     hermes plugins install <your-repo>/plugins/phone_use"
echo "     hermes plugins install <your-repo>/plugins/phone_events"
echo "  2. Run hermes and start using phone_use tool."
