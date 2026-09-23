# Working on the phone projects

Read [DOWNSTREAMS.md](DOWNSTREAMS.md) before changing shared phone code or the
Android helper. There are **two direct downstreams**, not just MCP:

- `Ctrl-Creeper/phone-mcp-server`
- `Ctrl-Creeper/n.e.k.o_plugin_phone_workflows`

Hermes is the upstream source of shared ADB/Appium, OCR, WeChat flows and the
helper APK. Keep shared modules independent of Hermes gateway imports. Preserve
optional arguments and existing standalone callers when adding Hermes features.
Host adapters, policy/confirmation flows and task lifecycles need separate
integration; copying shared files does not make a feature work in every host.

For changes to shared code, record compatibility checks for **both** downstreams
in the PR, or explicitly list what remains unverified. Use isolated checkouts and
each downstream's sync script; preserve downstream fixes and inspect the diff.
Do not overwrite a checkout used by another agent, change live gateway settings,
deploy, merge, accept real friend requests or send test messages as a side effect
of code verification.

For WeChat fixes, inspect the existing workflow and reproduce the failing screen
transition before changing it. Test adjacent controls, ambiguous identities,
intermediate screens and positive completion evidence. Missing buttons alone do
not prove an operation succeeded. Keep personal screen data out of fixtures.

Run `PYTHONPATH=. python3 -m pytest -q tests` and `git diff --check`.
