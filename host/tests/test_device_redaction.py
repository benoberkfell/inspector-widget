"""Password text never leaves the agent: the paths the A11yProbe goldens don't reach (live).

``test_device_a11y_golden.py`` greps the default dumps (semantics, View tree, a11y) for
A11yProbe's password secrets. Two more paths can carry a password field's text:

* the Compose SLOT TABLE (``enable_inspection``): an app's own wrapper composable takes the
  secret as a String parameter (Thunderbird's ``PasswordInput(password = ...)``,
  ``TextFieldOutlinedPassword(value = ...)``) and builds the transformation inside, so the
  wrapper is not a password field by its own parameters. A11yProbe's ``password_field``
  scenario has two such wrappers (Scenarios.kt ``PasswordWrapper`` /
  ``PasswordValueWrapper``).
* the accessibility EVENT TAP: a text change in a password field sends its text in the
  event (the character just typed in a masked field, the whole text of a visible-password
  one). That needs an accessibility service, so it turns TalkBack on for a few seconds and
  restores the settings, and runs only when asked.

::

    scripts/build.sh && scripts/install-a11yprobe.sh "$ANDROID_SERIAL" --no-launch
    cd host && ANDROID_SERIAL=emulator-5554 .venv/bin/python -m pytest \\
        tests/test_device_redaction.py -q -m device
    # + the event tap (turns TalkBack on):
    cd host && ANDROID_SERIAL=emulator-5554 INSPECTOR_WIDGET_TALKBACK_TESTS=1 \\
        .venv/bin/python -m pytest tests/test_device_redaction.py -q -m device
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Iterator, List

import pytest

pytestmark = pytest.mark.device

SERIAL = (os.environ.get("ANDROID_SERIAL") or "").strip() or None
PACKAGE = "com.oberkfell.a11yprobe"
TALKBACK = "com.google.android.marvin.talkback/com.google.android.marvin.talkback.TalkBackService"
MASK = "•"

# Scenarios.kt (password_field) and ViewScenarioActivity.kt (section 9).
COMPOSE_SECRETS = {"PasswordWrapper": "hunter2-wrapped-secret",
                   "PasswordValueWrapper": "hunter2-wrapped-value-secret"}
COMPOSE_FIELD_SECRET = "hunter2-compose-secret"
VIEW_FIELDS = {"passwordField": "hunter2-view-secret",
               "visiblePasswordField": "hunter2-visible-secret"}
TYPED = "qzx"  # typed into the View fields with the tap installed


def _sh(cmd: str) -> str:
    from inspector_widget import adb
    return adb.shell(SERIAL, cmd, check=False).strip()


@pytest.fixture(scope="module")
def device():
    if not SERIAL:
        pytest.skip("set ANDROID_SERIAL to run the device redaction checks")
    from inspector_widget import adb
    if PACKAGE not in adb.list_debuggable_packages(SERIAL):
        pytest.skip(f"{PACKAGE} is not installed on {SERIAL}")
    yield SERIAL
    _sh(f"am force-stop {PACKAGE}")


def _attach():
    import inspector_widget
    deadline, last = time.time() + 30, None
    while time.time() < deadline:
        try:
            return inspector_widget.attach(SERIAL, PACKAGE)
        except Exception as e:  # the process may still be starting
            last = e
            time.sleep(1.0)
    raise AssertionError(f"could not attach to {PACKAGE} on {SERIAL}: {last}")


def _nodes(node: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    yield node
    for c in node.get("children") or []:
        yield from _nodes(c)


def _compose(session, semantics: bool) -> Dict[str, Any]:
    from inspector_widget import strings as st
    return st.dump_compose_to_dict(session.dump_compose(
        include_semantics=semantics, include_slot_table=True, enable_inspection=True))


def _slot_nodes(data: Dict[str, Any], name: str) -> List[Dict[str, Any]]:
    return [n for w in data.get("windows") or [] if w.get("root")
            for n in _nodes(w["root"]) if n.get("name") == name]


def test_slot_table_masks_a_password_passed_through_a_wrapper(device):
    _sh(f"am force-stop {PACKAGE}")
    _sh(f"am start -W -n {PACKAGE}/.MainActivity --es scenario password_field")
    session = _attach()
    try:
        data: Dict[str, Any] = {}
        deadline = time.time() + 20
        while time.time() < deadline:  # the hot reload fills the slot table on the next frame
            data = _compose(session, semantics=True)
            if all(_slot_nodes(data, name) for name in COMPOSE_SECRETS):
                break
            time.sleep(1.0)
        # Slot table only: no Password semantics node lends its content as a secret, so the
        # value wrapper's parameter is masked by its name and the password field below it.
        slot_only = _compose(session, semantics=False)
    finally:
        session.detach()
    for label, dump in (("semantics+slot", data), ("slot only", slot_only)):
        text = json.dumps(dump)
        leaked = [s for s in (COMPOSE_FIELD_SECRET, *COMPOSE_SECRETS.values()) if s in text]
        assert not leaked, f"{label}: password text in the Compose dump: {leaked}"
        for name, secret in COMPOSE_SECRETS.items():
            nodes = _slot_nodes(dump, name)
            assert nodes, f"{label}: no slot-table node {name} (diagnostics: {dump.get('diagnostics')})"
            attrs = nodes[0].get("attrs") or {}
            assert attrs.get("password") == MASK * len(secret), f"{label}: {name} attrs {attrs}"


def _view_id(session, name: str) -> int:
    from inspector_widget import strings as st
    tree = st.dump_tree_to_dict(session.dump_tree())
    stack = list(tree.get("roots") or [])
    while stack:
        n = stack.pop()
        if str(n.get("view_id_name") or "").endswith(name):
            return int(n["id"])
        stack.extend(n.get("children") or [])
    raise AssertionError(f"no View {name} in the tree")


def test_event_tap_masks_a_password_fields_text(device):
    if os.environ.get("INSPECTOR_WIDGET_TALKBACK_TESTS") != "1":
        pytest.skip("set INSPECTOR_WIDGET_TALKBACK_TESTS=1 (turns TalkBack on)")
    from inspector_widget import a11y
    from inspector_widget.talkback import device as tb
    if tb.talkback_version(SERIAL) is None:
        pytest.skip("TalkBack is not installed on this device")
    _sh(f"am force-stop {PACKAGE}")
    _sh(f"am start -W -n {PACKAGE}/.ViewScenarioActivity")
    session = _attach()
    events: List[Dict[str, Any]] = []
    try:
        # Snapshots the settings, dismisses the permission dialog TalkBack raises on every
        # start (it would take the typed keys) and brings the app back to the front.
        tb.enable(SERIAL, PACKAGE)
        seq = session.a11y_focus().seq  # installs the tap
        for name in VIEW_FIELDS:
            act = session.a11y_act(host_view_id=_view_id(session, name), action="focus")
            assert act.performed, f"could not focus {name}: {act.error}"
            _sh("input keyevent KEYCODE_MOVE_END")
            for ch in TYPED:  # one key at a time: the masked field shows each for a moment
                _sh(f"input text {ch}")
                time.sleep(0.3)
            time.sleep(0.5)
            resp = session.a11y_focus(after_seq=seq)
            seq = resp.seq
            events += a11y.a11y_focus_to_dict(resp)["events"]
    finally:
        try:
            tb.restore(SERIAL)
        finally:
            try:
                session.detach()
            except Exception:
                pass
    changed = [e for e in events if e["type"] == "VIEW_TEXT_CHANGED"]
    assert len(changed) >= len(VIEW_FIELDS) * len(TYPED), f"text-change events missing: {events}"
    texts = [e.get("text") or "" for e in changed]
    for text in texts:
        assert text and set(text) == {MASK}, f"password event text sent unmasked: {texts}"
    focused = [e.get("text") for e in events if e["type"] == "VIEW_FOCUSED" and e.get("text")]
    assert focused and all(set(t) == {MASK} for t in focused), f"focus event text: {focused}"
    blob = json.dumps(events)
    leaked = [s for s in (*VIEW_FIELDS.values(), TYPED) if s in blob]
    assert not leaked, f"password text in the recorded events: {leaked}"
