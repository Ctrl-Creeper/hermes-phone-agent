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
BUILT_APK_PATH="${SCRIPT_DIR}/helper-apk/app/build/outputs/apk/debug/app-debug.apk"
RELEASE_APK_PATH="${SCRIPT_DIR}/helper-apk/releases/hermes-phone-agent-v0.2.2.apk"
if [[ -f "$BUILT_APK_PATH" ]]; then
    APK_PATH="$BUILT_APK_PATH"
else
    APK_PATH="$RELEASE_APK_PATH"
fi

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
    echo "     cp app/build/outputs/apk/debug/app-debug.apk releases/hermes-phone-agent-v0.2.2.apk"
    exit 1
fi
$ADB_CMD install -r "$APK_PATH"
echo "   Installed."
# PackageManager may clear listener grants asynchronously just after an APK
# update. Wait for that bookkeeping before rewriting the authoritative lists.
sleep 1

# Grant NotificationListener permission through NotificationManager. Writing
# enabled_notification_listeners directly can leave the secure setting updated
# while NotificationManager still considers the listener unapproved.
echo "3. Granting NotificationListener permission..."
LISTENER="${HELPER_PACKAGE}/${HELPER_PACKAGE}.PhoneNotificationListener"
$ADB_CMD shell cmd notification allow_listener "$LISTENER"
echo "   NotificationListener granted."

# Grant AccessibilityService permission
echo "4. Granting AccessibilityService permission..."
A11Y_SERVICE="${HELPER_PACKAGE}/${HELPER_PACKAGE}.PhoneAccessibilityService"
CURRENT_A11Y=$($ADB_CMD shell settings get secure enabled_accessibility_services 2>/dev/null || true)
NEW_A11Y=""
if [[ -n "$CURRENT_A11Y" && "$CURRENT_A11Y" != "null" ]]; then
    IFS=':' read -r -a CURRENT_A11Y_SERVICES <<< "$CURRENT_A11Y"
    for COMPONENT in "${CURRENT_A11Y_SERVICES[@]}"; do
        [[ "$COMPONENT" == "$A11Y_SERVICE" ]] && continue
        NEW_A11Y="${NEW_A11Y:+${NEW_A11Y}:}${COMPONENT}"
    done
fi
$ADB_CMD shell settings put secure enabled_accessibility_services "${NEW_A11Y:+${NEW_A11Y}:}${A11Y_SERVICE}"
$ADB_CMD shell settings put secure accessibility_enabled 1
echo "   AccessibilityService granted."

# The socket service is exported only behind Android's signature-level
# WRITE_SECURE_SETTINGS permission, which ADB shell holds and ordinary apps do
# not. Hermes starts it with a per-session token on first connection.
echo "5. Socket service will start on the first Hermes session."

# Build the optional macOS Vision OCR helper used when an app exposes an empty
# accessibility hierarchy (notably some WeChat builds).
if [[ "$(uname -s)" == "Darwin" ]] && command -v swiftc &>/dev/null; then
    echo "6. Installing macOS host OCR helper..."
    mkdir -p "${HOME}/.hermes/bin"
    swiftc "${SCRIPT_DIR}/plugins/phone_use/native/phone_ocr.swift" \
        -o "${HOME}/.hermes/bin/phone-ocr" \
        -framework Vision -framework ImageIO -framework CoreGraphics
    chmod 700 "${HOME}/.hermes/bin/phone-ocr"
    echo "   Host OCR installed."
else
    echo "6. Host OCR skipped (requires macOS and swiftc)."
fi

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Install hermes plugins:"
echo "     hermes plugins install <your-repo>/plugins/phone_use"
echo "     hermes plugins install <your-repo>/plugins/phone_events"
echo "  2. Run hermes and start using phone_use tool."
