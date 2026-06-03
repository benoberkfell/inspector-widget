# CLAUDE.md

This file orients Claude Code in this repository. The full contributor guide is **`AGENTS.md`** —
read it. It is imported below so its contents load with this file.

@AGENTS.md

## Non-negotiables (do not get these wrong)

1. **Two names, on purpose.** "Inspector Widget" is user-facing; "viewspector"/"ViewSpector" is
   the Android-internal codename. Never rename the codename internals (`com.oberkfell.viewspector`,
   `libviewspector.so`, `VWSPCT01`, socket `viewspector_<pid>`, logcat `TAG="ViewSpector"`, the
   `adb logcat -s ViewSpector` hint, protobuf package `viewspector.proto`). See `AGENTS.md §1`.

2. **Offline-green ≠ works.** Host logic mostly runs only against a device. After any change to
   `cli.py`, `mcp_server.py`, or the `inspector_widget` modules: run `host/tests/test_symbol_parity.py`,
   then **live-verify on `emulator-5554`** (launch `com.oberkfell.a11yprobe`, run the path you
   touched). This is how the recurring "ships fine, dies on device" bugs get caught. See `AGENTS.md §6`.

3. **CLI ↔ MCP parity.** A capability reachable one way but not the other is a bug. Add both.

## Fast commands

```bash
./scripts/build.sh                                            # on-device artifacts -> build-out/
./scripts/test.sh                                             # device-free pytest
host/mcp_server.py --self-check                               # proto status + 15 tools
adb shell am start -n com.oberkfell.a11yprobe/.MainActivity   # launch the a11y test app for live checks
```

## Constraints for this project

- **Local-only.** Do not publish, do not push to any remote, do not set up credentials or GitHub.
  Commit locally when asked; that is the boundary.
