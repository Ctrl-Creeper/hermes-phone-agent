# Shared phone stack and downstreams

`hermes-phone-agent` is the source of shared phone control and the Android helper.
Changes flow from here to **both** downstreams through reviewed changes:

| Repository | Shared code location | Host-owned behavior | Current sync mechanism |
| --- | --- | --- | --- |
| [phone-mcp-server](https://github.com/Ctrl-Creeper/phone-mcp-server) | `phone_control/` | MCP/HTTP tools and server lifecycle | Automatic sync PR, with scheduled fallback |
| [Neko phone_workflows](https://github.com/Ctrl-Creeper/n.e.k.o_plugin_phone_workflows) | `upstream/phone_core/` | Bounded workflows, reply preview and one-time confirmation token | Manual `scripts/sync_from_hermes.py`; no automatic sync workflow yet |

## What is shared

ADB/Appium backends, OCR, sanitization, policy primitives, deterministic WeChat
flows and the helper protocol are reusable. The MCP mapping and helper release
identity are in [`sync/phone-control-manifest.json`](sync/phone-control-manifest.json).
Neko currently has its own fixed file list and pinned APK version in its sync
script; it does **not** yet consume that manifest. Do not describe the manifest
as an enforced contract for Neko until its adapter is implemented and tested.

Use the same helper package, version, asset name and SHA-256 in all repositories.
Prefer the upstream GitHub Release asset; never rebuild a different APK under
the same release identity. If a downstream bundles it, verify the identical
hash. Helper protocol changes require compatible host adapters on both sides.

## What does not sync automatically

Hermes `phone_use/tool.py`, `phone_events`, gateway approvals, turn ownership and
durable receipt integration are host-specific. Neko and MCP do not receive these
capabilities simply by copying `wechat.py`. Preserve Neko's preview/confirmation
contract unless its behavior is explicitly being changed. Device ownership in
one host does not arbitrate access by another running host.

## Checklist for a shared change

1. Inspect both downstreams' `UPSTREAM` lock/metadata and local changes first.
   A downstream may contain fixes newer than the published upstream base; do not
   silently discard them during synchronization.
2. Implement the shared behavior here with backward-compatible APIs and tests.
   Keep framework integration in the respective host wrappers.
3. In isolated downstream checkouts run
   `python3 scripts/sync_from_hermes.py --source <upstream-checkout>` and inspect
   the entire generated diff. Run `--check` when supported, plus the downstream
   test suite. Neko also has plugin-market verification; see its README.
4. Record which revision was tested, which host integration works, and any
   unverified release/runtime checks in the PR. A green upstream suite alone is
   not a compatibility check.
5. Deliver downstream updates through PRs. Do not merge, publish helper releases
   or update the user's running installation without task authorization.

This document describes the current mechanism, not a promise that both sync
pipelines already exist. Update it when the implementation changes.
