"""An a11y dump must not cost TalkBack Compose's reading order (live, opt-in).

Reading the Compose delegate's semantics snapshot without a service clears its
invalidation flag, and Compose recomputes its traversal order only when that flag is
set; the agent must put it back (ComposeTraversal.restore). This dumps the A11yProbe
traversal screen with no service, turns TalkBack on over the same static screen, and
dumps again: the second dump is Compose's own (service) order and must still be right.

It turns TalkBack on for a few seconds and restores the previous settings, so it runs
only when asked::

    cd host && ANDROID_SERIAL=emulator-5556 INSPECTOR_WIDGET_TALKBACK_TESTS=1 \\
        .venv/bin/python -m pytest tests/test_device_compose_order_under_talkback.py -m device
"""

from __future__ import annotations

import os
import time

import pytest

pytestmark = pytest.mark.device

SERIAL = (os.environ.get("ANDROID_SERIAL") or "").strip() or None
PACKAGE = "com.oberkfell.a11yprobe"
TALKBACK = "com.google.android.marvin.talkback/com.google.android.marvin.talkback.TalkBackService"
ORDER = ["1. First", "2. Second", "3. Third", "1. First (reordered)", "2. Second (reordered)",
         "3. Third (reordered)", "3. Third (bad)", "2. Second (bad)", "1. First (bad)"]


def _order(session):
    from inspector_widget import a11y
    d = a11y.a11y_to_dict(session.dump_a11y(root_id=0, include_extras=True))
    return [e.get("speak") for e in d["focus_order"] if e.get("speak") in set(ORDER)], \
        d.get("diagnostics", "")


def test_talkback_gets_compose_order_after_a_dump():
    if not SERIAL or os.environ.get("INSPECTOR_WIDGET_TALKBACK_TESTS") != "1":
        pytest.skip("set ANDROID_SERIAL and INSPECTOR_WIDGET_TALKBACK_TESTS=1 (turns TalkBack on)")
    import inspector_widget
    from inspector_widget import adb

    def sh(cmd):
        return adb.shell(SERIAL, cmd, check=False).strip()

    if "talkback" not in sh("pm list packages com.google.android.marvin.talkback"):
        pytest.skip("TalkBack is not installed on this device")
    prev = (sh("settings get secure enabled_accessibility_services"),
            sh("settings get secure accessibility_enabled"))
    sh("settings put secure enabled_accessibility_services null")
    sh("settings put secure accessibility_enabled 0")
    time.sleep(2)
    sh(f"am force-stop {PACKAGE}")
    sh(f"am start -W -n {PACKAGE}/.MainActivity --es scenario traversal")
    time.sleep(2)
    session = inspector_widget.attach(SERIAL, PACKAGE)
    try:
        before, diag = _order(session)
        assert before == ORDER and "a11y-services=off" in diag, (before, diag)
        sh(f"settings put secure enabled_accessibility_services {TALKBACK}")
        sh("settings put secure accessibility_enabled 1")
        time.sleep(6)
        after, diag = _order(session)
        assert "compose-traversal service=" in diag, diag
        assert after == ORDER, f"TalkBack lost Compose's reading order after a dump: {after}"
    finally:
        sh("settings put secure enabled_accessibility_services "
           + (prev[0] if prev[0] not in ("", "null") else "null"))
        sh("settings put secure accessibility_enabled " + (prev[1] or "0"))
        try:
            session.detach()
        except Exception:
            pass
        sh(f"am force-stop {PACKAGE}")
