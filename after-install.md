# hermes-phone-agent installed

## Quick start

1. **Start an Android emulator** (if not already running):
   ```
   emulator -avd <your_avd_name>
   ```

2. **Run the setup script** to install the helper APK and grant permissions:
   ```
   cd ~/.hermes/plugins/phone-use
   ../../phone-agent-helper/setup.sh
   ```

3. **Enable the plugins**:
   ```
   hermes plugins enable phone-use
   hermes plugins enable phone-events
   ```

4. **Start hermes** and try:
   ```
   > Take a screenshot of the phone
   > Open Settings
   > What notifications are on the phone right now?
   ```

## Security notes

- All ADB commands use safe argument lists (no shell injection possible)
- Phone content is treated as untrusted data — the agent won't follow
  instructions found in notification text or UI labels
- `install_apk` and `shell` always require your explicit approval
- OTPs and sensitive patterns are redacted before reaching the LLM
- The helper APK socket uses per-session authentication tokens

See SECURITY.md for the full threat model.
