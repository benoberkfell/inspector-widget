"""Password text never leaves the agent: the paths the A11yProbe goldens don't reach (live).

``test_device_a11y_golden.py`` greps the default dumps (semantics, View tree, a11y) for
A11yProbe's password secrets. More paths can carry a password field's text:

* the Compose SLOT TABLE (``enable_inspection``): an app's own wrapper composable takes the
  secret as a String parameter (Thunderbird's ``PasswordInput(password = ...)``,
  ``TextFieldOutlinedPassword(value = ...)``) and builds the transformation inside, so the
  wrapper is not a password field by its own parameters. A11yProbe's ``password_field``
  scenario has two such wrappers (Scenarios.kt ``PasswordWrapper`` /
  ``PasswordValueWrapper``).
* a Compose VISIBLE-PASSWORD field (``password_field``'s BAD field, tagged ``bad_password``:
  a password keyboard, no ``PasswordVisualTransformation``) has no Password semantics and
  no password input type, so nothing on its node says password; the agent knows it by its
  keyboard (``ComposeInspector.isPasswordNode``) in every dump, the integrated view included.
* the accessibility EVENT TAP: a text change in a password field sends its text in the
  event (the character just typed in a masked field, the whole text of a visible-password
  one, View or Compose). That needs an accessibility service, so those checks turn TalkBack
  on for a few seconds each, restore the settings, and run only when asked.

Every check says which condition failed, with the recorded event texts and the steps taken
(``redaction_checks.py``). Typing waits for the field to hold input focus first, BACKs out of
TalkBack's POST_NOTIFICATIONS dialog (raised on every TalkBack start, at times seconds late)
when it covers the app, and tries a second time when no text change arrived.

::

    scripts/build.sh && scripts/install-a11yprobe.sh "$ANDROID_SERIAL" --no-launch
    cd host && ANDROID_SERIAL=emulator-5554 .venv/bin/python -m pytest \\
        tests/test_device_redaction.py -q -m device
    # + the event tap (turns TalkBack on):
    cd host && ANDROID_SERIAL=emulator-5554 INSPECTOR_WIDGET_TALKBACK_TESTS=1 \\
        .venv/bin/python -m pytest tests/test_device_redaction.py -q -m device
"""

from __future__ import annotations

import contextlib
import os
import time
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import pytest

from redaction_checks import (FOCUSED, MASK, TEXT_CHANGED, Source, field_problems, from_source,
                              is_masked, leaks, of_type, report)

pytestmark = pytest.mark.device

SERIAL = (os.environ.get("ANDROID_SERIAL") or "").strip() or None
PACKAGE = "com.oberkfell.a11yprobe"

# Scenarios.kt (password_field) and ViewScenarioActivity.kt (section 9).
COMPOSE_SECRETS = {"PasswordWrapper": "hunter2-wrapped-secret",
                   "PasswordValueWrapper": "hunter2-wrapped-value-secret"}
COMPOSE_FIELD_SECRET = "hunter2-compose-secret"
COMPOSE_VISIBLE_SECRET = "hunter2-visible-compose"
COMPOSE_VISIBLE_TAG = "bad_password"
VIEW_FIELDS = {"passwordField": "hunter2-view-secret",
               "visiblePasswordField": "hunter2-visible-secret"}
TYPED = "qzx"  # typed into the fields with the tap installed

INPUT_FOCUS_WAIT_S = 3.0
EVENTS_WAIT_S = 3.0


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


def _launch(activity: str, scenario: Optional[str] = None) -> None:
    _sh(f"am force-stop {PACKAGE}")
    extra = f" --es scenario {scenario}" if scenario else ""
    _sh(f"am start -W -n {PACKAGE}/{activity}{extra}")


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


def _detach(session) -> None:
    try:
        session.detach()
    except Exception:
        pass


def _nodes(node: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    yield node
    for c in node.get("children") or []:
        yield from _nodes(c)


def _compose(session, semantics: bool, slot: bool = True) -> Dict[str, Any]:
    from inspector_widget import strings as st
    return st.dump_compose_to_dict(session.dump_compose(
        include_semantics=semantics, include_slot_table=slot, enable_inspection=slot))


def _slot_nodes(data: Dict[str, Any], name: str) -> List[Dict[str, Any]]:
    return [n for w in data.get("windows") or [] if w.get("root")
            for n in _nodes(w["root"]) if n.get("name") == name]


def _compose_field(data: Dict[str, Any], tag: str) -> Tuple[int, Dict[str, Any]]:
    """(AndroidComposeView id, semantics node) of the node whose TestTag is ``tag``."""
    for w in data.get("windows") or []:
        for n in _nodes(w.get("root") or {}):
            if (n.get("attrs") or {}).get("TestTag") == tag:
                return w["view_id"], n
    raise AssertionError(f"no Compose semantics node tagged {tag} "
                         f"(diagnostics: {data.get('diagnostics')})")


def _a11y_node(dump: Dict[str, Any], source: Source) -> Optional[Dict[str, Any]]:
    for w in dump.get("windows") or []:
        for n in _nodes(w.get("root") or {}):
            if (n.get("host_view_id"), n.get("virtual_id", -1)) == source:
                return n
    return None


def test_slot_table_masks_a_password_passed_through_a_wrapper(device):
    _launch(".MainActivity", "password_field")
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
        _detach(session)
    secrets = (COMPOSE_FIELD_SECRET, COMPOSE_VISIBLE_SECRET, *COMPOSE_SECRETS.values())
    for label, dump in (("semantics+slot", data), ("slot only", slot_only)):
        leaked = leaks(secrets, **{label: dump})
        assert not leaked, f"password text in the Compose dump: {leaked}"
        for name, secret in COMPOSE_SECRETS.items():
            nodes = _slot_nodes(dump, name)
            assert nodes, f"{label}: no slot-table node {name} (diagnostics: {dump.get('diagnostics')})"
            attrs = nodes[0].get("attrs") or {}
            assert attrs.get("password") == MASK * len(secret), f"{label}: {name} attrs {attrs}"


def test_compose_visible_password_is_masked_in_every_dump(device):
    """The BAD field of ``password_field`` shows its password: no Password semantics, no
    password input type. Its text is masked in the semantics dump, the a11y tree, the View
    tree and properties, and the integrated view and node dossier."""
    from inspector_widget import a11y, adb, correlate, strings as st
    _launch(".MainActivity", "password_field")
    session = _attach()
    try:
        # Semantics only: no hot reload, so the semantics id stays valid for the other dumps.
        compose = _compose(session, semantics=True, slot=False)
        acv, node = _compose_field(compose, COMPOSE_VISIBLE_TAG)
        source = (acv, int(node["id"]))
        dumps: Dict[str, Any] = {
            "compose": compose,
            "a11y": a11y.a11y_to_dict(session.dump_a11y()),
            "tree": st.dump_tree_to_dict(session.dump_tree(include_properties=True)),
            "properties": st.get_properties_to_dict(session.get_properties(acv)),
            "inspect": correlate.inspect_tree(session, include_properties=True),
        }
        dumps["inspect_node"] = correlate.inspect_node(
            session, node_key=f"compose:{acv}:{source[1]}", include_image=False, lint=True,
            density=adb.display_density(SERIAL), font_scale=adb.font_scale(SERIAL))
    finally:
        _detach(session)
    leaked = leaks((COMPOSE_VISIBLE_SECRET, COMPOSE_FIELD_SECRET), **dumps)
    assert not leaked, f"Compose password text sent in plain text: {leaked}"
    attrs = node.get("attrs") or {}
    want = MASK * len(COMPOSE_VISIBLE_SECRET)
    assert "Password" not in attrs, f"{COMPOSE_VISIBLE_TAG} is not the visible-password case: {attrs}"
    assert attrs.get("EditableText") == want, f"semantics EditableText not masked: {attrs}"
    if "InputText" in attrs:
        assert attrs["InputText"] == want, f"semantics InputText not masked: {attrs}"
    a = _a11y_node(dumps["a11y"], source)
    assert a is not None, f"no a11y node {source} for {COMPOSE_VISIBLE_TAG}"
    assert "password" not in (a.get("flags") or []), f"the a11y node says password itself: {a}"
    assert a.get("text") == want, f"a11y text not masked: {a.get('text')!r}"
    assert dumps["inspect_node"] is not None, f"no dossier for compose:{acv}:{source[1]}"


# --------------------------------------------------------------------------- #
# The event tap (TalkBack on)
# --------------------------------------------------------------------------- #
def _require_talkback() -> None:
    if os.environ.get("INSPECTOR_WIDGET_TALKBACK_TESTS") != "1":
        pytest.skip("set INSPECTOR_WIDGET_TALKBACK_TESTS=1 (turns TalkBack on)")
    from inspector_widget.talkback import device as tb
    if tb.talkback_version(SERIAL) is None:
        pytest.skip("TalkBack is not installed on this device")


@contextlib.contextmanager
def _talkback(steps: List[str]) -> Iterator[str]:
    """TalkBack on for the block (settings snapshotted first, restored after); yields the
    app's activity from before, to bring back when something covers it."""
    from inspector_widget.talkback import device as tb
    top_before = tb.top_activity(SERIAL) or ""
    try:
        # Dismisses the permission dialog TalkBack raises on every start (it would take the
        # typed keys) and brings the app back to the front.
        out = tb.enable(SERIAL, PACKAGE)
        steps.append(f"talkback on: changed={out.get('changed')} dismissed={out.get('dismissed')} "
                     f"refronted={out.get('refronted')}")
        yield top_before
    finally:
        tb.restore(SERIAL)


def _front(top_before: str, steps: List[str]) -> None:
    """Put the app back on top when something covers it: BACK out of TalkBack's tutorial or
    permission dialog (which can come seconds after TalkBack started), else re-front it (a
    closed activity comes back as a new one: callers resolve their target again)."""
    from inspector_widget.talkback import device as tb
    top = tb.top_activity(SERIAL)
    if top and top.startswith(PACKAGE + "/"):
        return
    dismissed = tb.dismiss_talkback_activities(SERIAL, top_before)
    out = tb.ensure_foreground(SERIAL, PACKAGE, top_before)
    steps.append(f"app not on top ({top}): dismissed={dismissed} {out or ''}".rstrip())


def _has_input_focus(session, source: Source) -> bool:
    from inspector_widget import a11y
    n = _a11y_node(a11y.a11y_to_dict(session.dump_a11y(include_extras=False)), source)
    return n is not None and "focused" in (n.get("flags") or [])


def _await(pred, timeout_s: float, interval_s: float = 0.25) -> bool:
    deadline = time.monotonic() + timeout_s
    while True:
        if pred():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval_s)


def _type_into(session, resolve: Callable[[], Source], label: str, seq: int, top_before: str,
               steps: List[str]) -> Tuple[List[Dict[str, Any]], int, List[Source]]:
    """Give the field ``resolve()`` names input focus and type TYPED into it one key at a
    time (a masked field shows each character for a moment); returns the events recorded
    meanwhile, the next seq, and the field's (host_view_id, virtual_id) per attempt, the last
    one current. Tries twice when the field never took input focus or no text change came
    from it; the field is resolved again each time (its activity may have been recreated)."""
    from inspector_widget import a11y
    events: List[Dict[str, Any]] = []
    sources: List[Source] = []
    for attempt in (1, 2):
        _front(top_before, steps)
        source = resolve()
        sources.append(source)
        act = session.a11y_act(host_view_id=source[0], virtual_id=source[1], action="focus")
        focused = act.performed and _await(lambda: _has_input_focus(session, source),
                                           INPUT_FOCUS_WAIT_S)
        steps.append(f"{label} #{attempt}: focus performed={act.performed}"
                     f"{' error=' + act.error if act.error else ''} input_focus={focused}")
        if focused:
            _sh("input keyevent KEYCODE_MOVE_END")
            for ch in TYPED:
                _sh(f"input text {ch}")
                time.sleep(0.3)
        got: List[Dict[str, Any]] = []

        def arrived() -> bool:
            nonlocal seq
            out = a11y.a11y_focus_to_dict(session.a11y_focus(after_seq=seq))
            got.extend(out["events"])
            seq = out["seq"]
            return len(of_type(from_source(got, source), TEXT_CHANGED)) >= len(TYPED)

        _await(arrived, EVENTS_WAIT_S if focused else 0.0, 0.5)
        events += got
        n = len(of_type(from_source(got, source), TEXT_CHANGED))
        steps.append(f"{label} #{attempt} {source}: {n} VIEW_TEXT_CHANGED of {len(got)} event(s)")
        if n:
            break
    return events, seq, sources


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
    _require_talkback()
    _launch(".ViewScenarioActivity")
    session = _attach()
    events: List[Dict[str, Any]] = []
    steps: List[str] = []
    sources: Dict[str, List[Source]] = {}
    try:
        with _talkback(steps) as top_before:
            seq = session.a11y_focus().seq  # installs the tap
            for name in VIEW_FIELDS:
                got, seq, sources[name] = _type_into(
                    session, lambda: (_view_id(session, name), -1), name, seq, top_before, steps)
                events += got
    finally:
        _detach(session)
    problems: List[str] = []
    for name, seen in sources.items():
        problems += field_problems(events, seen, name)
    focused = [e for e in of_type(events, FOCUSED) if e.get("text")
               and any(from_source([e], s) for seen in sources.values() for s in seen)]
    if not focused:
        problems.append("no VIEW_FOCUSED event with text from a password field")
    problems += [f"VIEW_FOCUSED text not masked: {e['text']!r}" for e in focused
                 if not is_masked(e["text"])]
    problems += [f"password text in the recorded events: {x}"
                 for x in leaks((*VIEW_FIELDS.values(), TYPED), events=events)]
    assert not problems, report(problems, events, steps)


def test_event_tap_masks_a_compose_visible_password(device):
    """The Compose visible-password field: its events carry no isPassword and its node no
    input type, so the tap asks its SemanticsNode. Also the focus reader's node (the lite
    snapshot) and the dumps after typing."""
    _require_talkback()
    from inspector_widget import a11y
    _launch(".MainActivity", "password_field")
    session = _attach()
    events: List[Dict[str, Any]] = []
    steps: List[str] = []
    problems: List[str] = []
    dumps: Dict[str, Any] = {}
    sources: List[Source] = []

    def resolve() -> Source:
        """The field now; starts the scenario again when it is gone (a closed MainActivity
        comes back as the scenario list)."""
        try:
            acv, node = _compose_field(_compose(session, semantics=True, slot=False),
                                       COMPOSE_VISIBLE_TAG)
        except AssertionError:
            steps.append("password_field is not on screen: starting it again")
            _sh(f"am start -W -n {PACKAGE}/.MainActivity --es scenario password_field")
            acv, node = _compose_field(_compose(session, semantics=True, slot=False),
                                       COMPOSE_VISIBLE_TAG)
        return acv, int(node["id"])

    try:
        with _talkback(steps) as top_before:
            seq = session.a11y_focus().seq  # installs the tap
            _front(top_before, steps)
            source = resolve()
            sources.append(source)
            act = session.a11y_act(host_view_id=source[0], virtual_id=source[1],
                                   action="accessibility_focus")
            steps.append(f"accessibility_focus performed={act.performed}"
                         f"{' error=' + act.error if act.error else ''}")
            focus = a11y.a11y_focus_to_dict(session.a11y_focus(after_seq=seq))
            dumps["a11y_focus"] = focus
            f = focus.get("a11y") or {}
            text = (f.get("node") or {}).get("text")
            if (f.get("host_view_id"), f.get("virtual_id")) != source:
                problems.append(f"accessibility focus is not on the field: {f.get('node_key')}")
            elif not is_masked(text):
                problems.append(f"focus reader's node text not masked: {text!r}")
            got, seq, typed_into = _type_into(session, resolve, COMPOSE_VISIBLE_TAG,
                                              focus["seq"], top_before, steps)
            sources += typed_into
            events += focus["events"] + got
            dumps["a11y"] = a11y.a11y_to_dict(session.dump_a11y())
            dumps["compose"] = _compose(session, semantics=True, slot=False)
    finally:
        _detach(session)
    problems += field_problems(events, sources, COMPOSE_VISIBLE_TAG)
    problems += [f"password text sent in plain text: {x}"
                 for x in leaks((COMPOSE_VISIBLE_SECRET, TYPED), events=events, **dumps)]
    after = _a11y_node(dumps.get("a11y") or {}, sources[-1])
    if after is None:
        problems.append(f"no a11y node {sources[-1]} after typing")
    elif not is_masked(after.get("text")) or len(after["text"]) <= len(COMPOSE_VISIBLE_SECRET):
        problems.append(f"a11y text after typing: {after.get('text')!r}, want more than "
                        f"{len(COMPOSE_VISIBLE_SECRET)} dots")
    assert not problems, report(problems, events, steps)
