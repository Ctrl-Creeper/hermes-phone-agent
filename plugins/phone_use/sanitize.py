"""Input sanitization and safety checks for phone_use commands.

This is the security boundary between the LLM's tool calls and the ADB
subprocess. Every string that touches `adb shell` must pass through here.
"""

from __future__ import annotations

import re
from typing import Optional


# Characters that have special meaning in an Android shell context.
# We reject arguments containing these rather than trying to escape them —
# escaping is error-prone and a single miss is a command injection.
_SHELL_META_CHARS = re.compile(r'[;|&`$(){}\\<>\n\r\x00]')

# Patterns that must never appear in typed text or shell commands.
# Even if the agent is instructed by prompt injection to type these,
# they are blocked before reaching ADB.
_BLOCKED_SHELL_PATTERNS = [
    re.compile(r"rm\s+-r", re.IGNORECASE),
    re.compile(r"rm\s+/", re.IGNORECASE),
    re.compile(r"mkfs\b", re.IGNORECASE),
    re.compile(r"dd\s+if=", re.IGNORECASE),
    re.compile(r"reboot", re.IGNORECASE),
    re.compile(r"shutdown", re.IGNORECASE),
    re.compile(r"su\s*$", re.IGNORECASE),
    re.compile(r"\bsu\s+-", re.IGNORECASE),
    re.compile(r"chmod\s+777", re.IGNORECASE),
    re.compile(r"pm\s+uninstall\s+-k\s+--user\s+0\s+com\.android", re.IGNORECASE),
    re.compile(r"settings\s+put\s+global\s+adb_enabled\s+0", re.IGNORECASE),
    re.compile(r"am\s+broadcast.*MASTER_CLEAR", re.IGNORECASE),
    re.compile(r"wipe\s+data", re.IGNORECASE),
    re.compile(r"curl\s+[^|]*\|\s*sh", re.IGNORECASE),
    re.compile(r"wget\s+[^|]*\|\s*sh", re.IGNORECASE),
    # Prevent ADB escape to host
    re.compile(r"adb\s+forward", re.IGNORECASE),
    re.compile(r"adb\s+reverse", re.IGNORECASE),
]

# Allowed keycode names (uppercase). Anything not on this list is rejected.
ALLOWED_KEYCODES = frozenset({
    "BACK", "HOME", "ENTER", "TAB", "SPACE", "DEL", "FORWARD_DEL",
    "DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT", "DPAD_CENTER",
    "VOLUME_UP", "VOLUME_DOWN", "VOLUME_MUTE",
    "POWER", "APP_SWITCH", "MENU", "SEARCH",
    "MOVE_HOME", "MOVE_END", "PAGE_UP", "PAGE_DOWN",
    "ESCAPE", "CAMERA", "NOTIFICATION",
    "MEDIA_PLAY_PAUSE", "MEDIA_STOP", "MEDIA_NEXT", "MEDIA_PREVIOUS",
    # Number keys
    "0", "1", "2", "3", "4", "5", "6", "7", "8", "9",
})

# Maximum text length the agent can type in one call.
MAX_TYPE_LENGTH = 500

# Maximum shell command length.
MAX_SHELL_LENGTH = 200

# Package name must match Android conventions.
_PACKAGE_PATTERN = re.compile(r'^[a-zA-Z][a-zA-Z0-9_]*(\.[a-zA-Z][a-zA-Z0-9_]*)+$')

# Activity name: package-qualified or shorthand.
_ACTIVITY_PATTERN = re.compile(r'^[a-zA-Z][a-zA-Z0-9_.]*$')


def sanitize_shell_arg(value: str, field_name: str = "argument") -> str:
    """Validate a string before it's used as an ADB shell argument.

    Raises ValueError with a descriptive message if the value contains
    shell metacharacters or blocked patterns. Returns the value unchanged
    if safe.
    """
    if _SHELL_META_CHARS.search(value):
        raise ValueError(
            f"{field_name} contains shell metacharacter: "
            f"{_SHELL_META_CHARS.search(value).group()!r}"
        )
    for pat in _BLOCKED_SHELL_PATTERNS:
        if pat.search(value):
            raise ValueError(f"{field_name} matches blocked pattern: {pat.pattern!r}")
    return value


def validate_text_input(text: str) -> str:
    """Validate text before typing via ADB input."""
    if len(text) > MAX_TYPE_LENGTH:
        raise ValueError(
            f"text too long ({len(text)} chars, max {MAX_TYPE_LENGTH}). "
            f"Split into multiple type calls."
        )
    if '\x00' in text:
        raise ValueError("text contains null byte")
    for pat in _BLOCKED_SHELL_PATTERNS:
        if pat.search(text):
            raise ValueError(f"text matches blocked pattern: {pat.pattern!r}")
    return text


def validate_shell_command(command: str) -> str:
    """Validate a raw shell command before execution."""
    if len(command) > MAX_SHELL_LENGTH:
        raise ValueError(
            f"shell command too long ({len(command)} chars, max {MAX_SHELL_LENGTH})"
        )
    if '\x00' in command:
        raise ValueError("command contains null byte")
    for pat in _BLOCKED_SHELL_PATTERNS:
        if pat.search(command):
            raise ValueError(f"command matches blocked pattern: {pat.pattern!r}")
    return command


def validate_keycode(keycode: str) -> str:
    """Validate a keycode against the allowlist. Returns the normalized name."""
    normalized = keycode.upper().removeprefix("KEYCODE_")
    if normalized not in ALLOWED_KEYCODES:
        raise ValueError(
            f"keycode {keycode!r} not in allowlist. "
            f"Allowed: {sorted(ALLOWED_KEYCODES)}"
        )
    return f"KEYCODE_{normalized}"


def validate_package_name(package: str) -> str:
    """Validate an Android package name."""
    if not _PACKAGE_PATTERN.match(package):
        raise ValueError(
            f"invalid Android package name: {package!r}. "
            f"Expected format: com.example.app"
        )
    sanitize_shell_arg(package, "package")
    return package


def validate_activity_name(activity: str) -> str:
    """Validate an Android activity class name."""
    if not _ACTIVITY_PATTERN.match(activity):
        raise ValueError(f"invalid activity name: {activity!r}")
    sanitize_shell_arg(activity, "activity")
    return activity


def validate_coordinate(
    x: Optional[int],
    y: Optional[int],
    screen_width: int,
    screen_height: int,
) -> None:
    """Validate touch coordinates are within screen bounds."""
    if x is not None and y is not None:
        if not (0 <= x <= screen_width and 0 <= y <= screen_height):
            raise ValueError(
                f"coordinate ({x}, {y}) out of screen bounds "
                f"({screen_width}x{screen_height})"
            )


def validate_apk_path(path: str) -> str:
    """Validate an APK path — must be a local file, no traversal."""
    if not path.endswith(".apk"):
        raise ValueError("path must end with .apk")
    if ".." in path:
        raise ValueError("path traversal (..) not allowed")
    sanitize_shell_arg(path, "apk_path")
    return path
