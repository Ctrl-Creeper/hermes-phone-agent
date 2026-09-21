#!/usr/bin/env python3
"""Validate the published helper APK contract before notifying phone-mcp-server."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "sync" / "phone-control-manifest.json"
GRADLE_PATH = ROOT / "helper-apk" / "app" / "build.gradle.kts"
EXPECTED_REPOSITORY = "Ctrl-Creeper/hermes-phone-agent"
EXPECTED_PACKAGE = "com.hermes.phoneagent"


def _require_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _gradle_value(name: str) -> str:
    match = re.search(rf"{name}\s*=\s*\"?([^\"\s]+)\"?", GRADLE_PATH.read_text())
    if not match:
        raise ValueError(f"{name} was not found in {GRADLE_PATH.relative_to(ROOT)}")
    return match.group(1)


def _release_digest(tag: str, asset_name: str) -> str:
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(
        f"https://api.github.com/repos/{EXPECTED_REPOSITORY}/releases/tags/{quote(tag)}",
        headers=headers,
    )
    with urlopen(request, timeout=20) as response:  # nosec B310: fixed GitHub API host
        release = json.load(response)
    for asset in release.get("assets", []):
        if asset.get("name") == asset_name:
            digest = asset.get("digest", "")
            if isinstance(digest, str) and digest.startswith("sha256:"):
                return digest.removeprefix("sha256:")
            raise ValueError(f"release asset {asset_name} has no SHA-256 digest")
    raise ValueError(f"release {tag} does not contain {asset_name}")


def validate(skip_release: bool = False) -> None:
    manifest = json.loads(MANIFEST_PATH.read_text())
    if manifest.get("schema_version") != 1:
        raise ValueError("unsupported manifest schema")
    if manifest.get("source_repository") != EXPECTED_REPOSITORY:
        raise ValueError("manifest source_repository is incorrect")

    helper = manifest.get("helper")
    if not isinstance(helper, dict):
        raise ValueError("manifest helper must be an object")
    version = _require_string(helper.get("version"), "helper.version")
    version_code = helper.get("version_code")
    release_tag = _require_string(helper.get("release_tag"), "helper.release_tag")
    asset = _require_string(helper.get("asset"), "helper.asset")
    digest = _require_string(helper.get("sha256"), "helper.sha256")

    if helper.get("package") != EXPECTED_PACKAGE:
        raise ValueError("helper package is incorrect")
    if helper.get("transport_contract") != "helper-socket-hmac-v1":
        raise ValueError("helper transport contract is incorrect")
    if not isinstance(version_code, int) or version_code <= 0:
        raise ValueError("helper.version_code must be a positive integer")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("helper.sha256 must be a lowercase SHA-256 digest")

    if version != _gradle_value("versionName"):
        raise ValueError("manifest helper.version does not match Gradle versionName")
    if str(version_code) != _gradle_value("versionCode"):
        raise ValueError("manifest helper.version_code does not match Gradle versionCode")
    if release_tag != f"v{version}" or asset != f"hermes-phone-agent-v{version}.apk":
        raise ValueError("helper release tag or asset name does not match the version")

    core_files = manifest.get("core_files")
    if not isinstance(core_files, list) or not core_files:
        raise ValueError("manifest core_files must be a non-empty list")
    for entry in core_files:
        if not isinstance(entry, dict):
            raise ValueError("manifest core_files entries must be objects")
        source = _require_string(entry.get("source"), "core file source")
        if not (ROOT / source).is_file():
            raise ValueError(f"manifest source does not exist: {source}")

    if not skip_release and _release_digest(release_tag, asset) != digest:
        raise ValueError("manifest helper.sha256 does not match the GitHub Release asset")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-release", action="store_true", help="skip GitHub Release digest validation")
    args = parser.parse_args()
    try:
        validate(skip_release=args.skip_release)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"manifest validation failed: {exc}", file=sys.stderr)
        return 1
    print("phone control manifest is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
