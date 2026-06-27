"""Phone policy engine — governs what the agent may do autonomously.

Loads a YAML config that defines per-package and per-event-type rules.
Two enforcement points use this:
  1. phone_events adapter: decides whether to forward/summarize/drop events
  2. phone_use tool: blocks actions that violate the policy

The config file is searched in order:
  1. $PHONE_POLICY_PATH (explicit override)
  2. ./.hermes/phone-policy.yaml (project-local)
  3. ~/.hermes/phone-policy.yaml (user-global)
  4. Built-in conservative defaults (no auto, everything reports)
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional, Set

logger = logging.getLogger(__name__)

# ── Behavior levels ────────────────────────────────────────────────

BEHAVIOR_AUTO = "auto"
BEHAVIOR_REPORT = "report"
BEHAVIOR_IGNORE = "ignore"
_VALID_BEHAVIORS = frozenset({BEHAVIOR_AUTO, BEHAVIOR_REPORT, BEHAVIOR_IGNORE})

ALL_PHONE_ACTIONS = frozenset({
    "tap", "double_tap", "long_press", "swipe",
    "type", "clear_text", "set_text", "keyevent",
    "launch_app", "stop_app", "install_apk", "shell",
    "capture", "wait", "list_apps", "current_app", "device_info",
})


# ── Data classes ───────────────────────────────────────────────────

@dataclass(frozen=True)
class AppProfile:
    """A named group of apps with shared policy."""
    name: str
    packages: List[str] = field(default_factory=list)
    on_event: str = BEHAVIOR_REPORT
    blocked_actions: FrozenSet[str] = field(default_factory=frozenset)
    allowed_actions: FrozenSet[str] = field(default_factory=frozenset)
    notes: str = ""


@dataclass(frozen=True)
class EventRule:
    """A fine-grained rule matched against specific event patterns."""
    package: str = ""
    package_regex: Optional[re.Pattern] = field(default=None, compare=False, repr=False)
    event_type: str = ""
    title_regex: Optional[re.Pattern] = field(default=None, compare=False, repr=False)
    body_regex: Optional[re.Pattern] = field(default=None, compare=False, repr=False)
    behavior: str = BEHAVIOR_REPORT
    blocked_actions: FrozenSet[str] = field(default_factory=frozenset)
    allowed_actions: FrozenSet[str] = field(default_factory=frozenset)
    notes: str = ""
    priority: int = 0


@dataclass
class PolicyDecision:
    """Result of evaluating an event or action against the policy."""
    behavior: str = BEHAVIOR_REPORT
    blocked_actions: FrozenSet[str] = field(default_factory=frozenset)
    allowed_actions: FrozenSet[str] = field(default_factory=frozenset)
    notes: str = ""
    source: str = "default"

    @property
    def is_auto(self) -> bool:
        return self.behavior == BEHAVIOR_AUTO

    @property
    def is_report(self) -> bool:
        return self.behavior == BEHAVIOR_REPORT

    @property
    def is_ignore(self) -> bool:
        return self.behavior == BEHAVIOR_IGNORE

    def action_allowed(self, action: str) -> bool:
        if action in self.blocked_actions:
            return False
        if self.allowed_actions and action not in self.allowed_actions:
            return False
        return True


# ── Policy class ───────────────────────────────────────────────────

class PhonePolicy:
    """Loaded policy config with two-tier matching: event_rules then app_profiles."""

    def __init__(
        self,
        default_behavior: str = BEHAVIOR_REPORT,
        global_restrict: FrozenSet[str] = frozenset(),
        app_profiles: Optional[List[AppProfile]] = None,
        event_rules: Optional[List[EventRule]] = None,
    ):
        self.default_behavior = default_behavior
        self.global_restrict = global_restrict
        self.app_profiles: List[AppProfile] = app_profiles or []
        self.event_rules: List[EventRule] = sorted(
            event_rules or [], key=lambda r: r.priority, reverse=True,
        )
        self._package_to_profile: Dict[str, AppProfile] = {}
        for profile in self.app_profiles:
            for pkg in profile.packages:
                self._package_to_profile[pkg] = profile

    def evaluate_event(
        self,
        package: str = "",
        event_type: str = "",
        title: str = "",
        body: str = "",
    ) -> PolicyDecision:
        """Match an event against rules and profiles, return the decision."""
        # Tier 1: event_rules (fine-grained, priority-ordered)
        for rule in self.event_rules:
            if self._event_rule_matches(rule, package, event_type, title, body):
                return PolicyDecision(
                    behavior=rule.behavior,
                    blocked_actions=rule.blocked_actions | self.global_restrict,
                    allowed_actions=rule.allowed_actions,
                    notes=rule.notes,
                    source=f"event_rule(pkg={rule.package!r}, event={rule.event_type!r}, p={rule.priority})",
                )

        # Tier 2: app_profiles (package-based)
        profile = self._find_profile(package)
        if profile is not None:
            return PolicyDecision(
                behavior=profile.on_event,
                blocked_actions=profile.blocked_actions | self.global_restrict,
                allowed_actions=profile.allowed_actions,
                notes=profile.notes,
                source=f"app_profile({profile.name!r})",
            )

        # Tier 3: default
        return PolicyDecision(
            behavior=self.default_behavior,
            blocked_actions=self.global_restrict,
            source="default",
        )

    def check_action(
        self, action: str, target_package: str = "",
    ) -> PolicyDecision:
        """Check if a phone_use action is allowed for a given package."""
        # Global restrict always applies.
        if action in self.global_restrict:
            return PolicyDecision(
                behavior=BEHAVIOR_REPORT,
                blocked_actions=self.global_restrict,
                notes=f"'{action}' is globally restricted by policy",
                source="global_restrict",
            )

        # Check app profile for the target package.
        profile = self._find_profile(target_package)
        if profile is not None:
            decision = PolicyDecision(
                behavior=profile.on_event,
                blocked_actions=profile.blocked_actions | self.global_restrict,
                allowed_actions=profile.allowed_actions,
                notes=profile.notes,
                source=f"app_profile({profile.name!r})",
            )
            if not decision.action_allowed(action):
                decision.notes = (
                    f"'{action}' on '{target_package}' blocked by "
                    f"profile '{profile.name}'"
                )
            return decision

        return PolicyDecision(
            behavior=self.default_behavior,
            blocked_actions=self.global_restrict,
            source="default",
        )

    def _find_profile(self, package: str) -> Optional[AppProfile]:
        """Find the app profile for a package (exact match, then glob)."""
        if package in self._package_to_profile:
            return self._package_to_profile[package]
        for profile in self.app_profiles:
            for pat in profile.packages:
                if "*" in pat or "?" in pat:
                    if fnmatch.fnmatch(package, pat):
                        return profile
        return None

    @staticmethod
    def _event_rule_matches(
        rule: EventRule,
        package: str,
        event_type: str,
        title: str,
        body: str,
    ) -> bool:
        # Package match
        if rule.package:
            if rule.package_regex:
                if not rule.package_regex.match(package):
                    return False
            elif rule.package != package:
                return False
        # Event type match
        if rule.event_type and rule.event_type != event_type:
            return False
        # Title regex match
        if rule.title_regex and not rule.title_regex.search(title):
            return False
        # Body regex match
        if rule.body_regex and not rule.body_regex.search(body):
            return False
        return True


# ── Config parsing ─────────────────────────────────────────────────

def _compile_glob(pattern: str) -> Optional[re.Pattern]:
    """Convert a glob-style package pattern to a regex."""
    if "*" not in pattern and "?" not in pattern:
        return None
    regex = fnmatch.translate(pattern)
    try:
        return re.compile(regex)
    except re.error:
        return None


def _compile_regex(pattern: str, label: str) -> Optional[re.Pattern]:
    try:
        return re.compile(pattern)
    except re.error as e:
        logger.warning("invalid regex in %s: %s", label, e)
        return None


def _parse_config(raw: Dict[str, Any]) -> PhonePolicy:
    default = raw.get("default_behavior", BEHAVIOR_REPORT)
    if default not in _VALID_BEHAVIORS:
        logger.warning("invalid default_behavior %r, using 'report'", default)
        default = BEHAVIOR_REPORT

    global_restrict = frozenset(
        a for a in raw.get("global_restrict", []) if a in ALL_PHONE_ACTIONS
    )

    # Parse app_profiles
    profiles: List[AppProfile] = []
    for name, entry in (raw.get("app_profiles") or {}).items():
        on_event = entry.get("on_event", BEHAVIOR_REPORT)
        if on_event not in _VALID_BEHAVIORS:
            logger.warning("profile %r: invalid on_event %r, using 'report'", name, on_event)
            on_event = BEHAVIOR_REPORT
        profiles.append(AppProfile(
            name=name,
            packages=entry.get("packages", []),
            on_event=on_event,
            blocked_actions=frozenset(entry.get("blocked_actions", [])),
            allowed_actions=frozenset(entry.get("allowed_actions", [])),
            notes=entry.get("notes", "").strip(),
        ))

    # Parse event_rules
    rules: List[EventRule] = []
    for i, entry in enumerate(raw.get("event_rules") or raw.get("rules") or []):
        match = entry.get("match", {})
        pkg = match.get("package", "")
        pkg_regex = _compile_glob(pkg) if pkg else None

        event = match.get("event", "")
        title_rx = _compile_regex(match["title_regex"], f"rule {i} title_regex") if match.get("title_regex") else None
        body_rx = _compile_regex(match["body_regex"], f"rule {i} body_regex") if match.get("body_regex") else None

        behavior = entry.get("behavior", BEHAVIOR_REPORT)
        if behavior not in _VALID_BEHAVIORS:
            logger.warning("event_rule %d: invalid behavior %r, skipping", i, behavior)
            continue

        rules.append(EventRule(
            package=pkg,
            package_regex=pkg_regex,
            event_type=event,
            title_regex=title_rx,
            body_regex=body_rx,
            behavior=behavior,
            blocked_actions=frozenset(entry.get("blocked_actions", entry.get("restrict", []))),
            allowed_actions=frozenset(entry.get("allowed_actions", entry.get("allow", []))),
            notes=entry.get("notes", entry.get("summary", "")).strip(),
            priority=int(entry.get("priority", 0)),
        ))

    return PhonePolicy(
        default_behavior=default,
        global_restrict=global_restrict,
        app_profiles=profiles,
        event_rules=rules,
    )


# ── Config loading (cached, hot-reload) ───────────────────────────

_policy_lock = threading.Lock()
_cached_policy: Optional[PhonePolicy] = None
_cached_mtime: float = 0.0
_cached_path: Optional[str] = None


def _find_config_path() -> Optional[str]:
    explicit = os.environ.get("PHONE_POLICY_PATH")
    if explicit and os.path.isfile(explicit):
        return explicit
    candidates = [
        os.path.join(os.getcwd(), ".hermes", "phone-policy.yaml"),
        os.path.expanduser("~/.hermes/phone-policy.yaml"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def load_policy(force_reload: bool = False) -> PhonePolicy:
    """Load and cache the phone policy. Auto-reloads when the file changes."""
    global _cached_policy, _cached_mtime, _cached_path

    with _policy_lock:
        path = _find_config_path()

        if path is None:
            if _cached_policy is None or force_reload:
                logger.info("no phone-policy.yaml found, using conservative defaults")
                _cached_policy = PhonePolicy(default_behavior=BEHAVIOR_REPORT)
                _cached_path = None
                _cached_mtime = 0.0
            return _cached_policy

        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0.0

        if (
            not force_reload
            and _cached_policy is not None
            and _cached_path == path
            and _cached_mtime == mtime
        ):
            return _cached_policy

        try:
            import yaml
        except ImportError:
            logger.warning(
                "PyYAML not installed — cannot load phone-policy.yaml. "
                "Install it: pip install pyyaml"
            )
            if _cached_policy is None:
                _cached_policy = PhonePolicy(default_behavior=BEHAVIOR_REPORT)
            return _cached_policy

        try:
            with open(path, "r") as f:
                raw = yaml.safe_load(f) or {}
            _cached_policy = _parse_config(raw)
            _cached_path = path
            _cached_mtime = mtime
            logger.info(
                "loaded phone policy from %s (%d profiles, %d event rules, default=%s)",
                path, len(_cached_policy.app_profiles),
                len(_cached_policy.event_rules), _cached_policy.default_behavior,
            )
        except Exception as e:
            logger.error("failed to parse phone-policy.yaml: %s", e)
            if _cached_policy is None:
                _cached_policy = PhonePolicy(default_behavior=BEHAVIOR_REPORT)

        return _cached_policy


def get_policy() -> PhonePolicy:
    """Get the current policy (cached, auto-reloading)."""
    return load_policy()
