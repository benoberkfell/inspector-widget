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
* an R8-OBFUSCATED app (A11yProbe's r8 build type, ``com.oberkfell.a11yprobe.r8``): Compose's
  classes are renamed, so the agent cannot tell a visible-password field from any other text
  field. It FAILS CLOSED: every editable Compose node's text is masked there, in the dumps,
  the focus reader and the event tap, and the diagnostics say so (``redaction_unverified``,
  ``redaction_masked``). Labels stay readable, and in the debug build a plain text field's
  text is not masked at all (no over-masking in a normal app). Skipped when the r8 build is
  not installed.

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
    # + the obfuscated app:
    scripts/install-a11yprobe.sh "$ANDROID_SERIAL" --r8 --no-launch
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
R8_PACKAGE = PACKAGE + ".r8"  # the r8 build type: R8-minified and obfuscated, debuggable

# Scenarios.kt (password_field) and ViewScenarioActivity.kt (section 9).
COMPOSE_SECRETS = {"PasswordWrapper": "hunter2-wrapped-secret",
                   "PasswordValueWrapper": "hunter2-wrapped-value-secret"}
COMPOSE_FIELD_SECRET = "hunter2-compose-secret"
COMPOSE_VISIBLE_SECRET = "hunter2-visible-compose"
COMPOSE_VISIBLE_TAG = "bad_password"
VIEW_FIELDS = {"passwordField": "hunter2-view-secret",
               "visiblePasswordField": "hunter2-visible-secret"}
TYPED = "qzx"  # typed into the fields with the tap installed
PLAIN_TEXT = "name@example.com"  # set on form_field's plain text field (Scenarios.kt)
PLAIN_TAG = "good_form_field"
VISIBLE_LABEL = "Password (shown)"  # the visible-password field's label: never masked

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


@pytest.fixture(scope="module")
def r8_device():
    if not SERIAL:
        pytest.skip("set ANDROID_SERIAL to run the device redaction checks")
    from inspector_widget import adb
    if R8_PACKAGE not in adb.list_debuggable_packages(SERIAL):
        pytest.skip(f"{R8_PACKAGE} is not installed on {SERIAL} "
                    "(scripts/install-a11yprobe.sh <serial> --r8)")
    yield SERIAL
    _sh(f"am force-stop {R8_PACKAGE}")


def _launch(activity: str, scenario: Optional[str] = None, package: str = PACKAGE) -> None:
    """Start ``activity`` (".MainActivity", ...: A11yProbe's classes keep their names in every
    build, the r8 one included, whose package has a suffix)."""
    _sh(f"am force-stop {package}")
    extra = f" --es scenario {scenario}" if scenario else ""
    _sh(f"am start -W -n {package}/{PACKAGE}{activity}{extra}")


def _attach(package: str = PACKAGE):
    import inspector_widget
    deadline, last = time.time() + 30, None
    while time.time() < deadline:
        try:
            return inspector_widget.attach(SERIAL, package)
        except Exception as e:  # the process may still be starting
            last = e
            time.sleep(1.0)
    raise AssertionError(f"could not attach to {package} on {SERIAL}: {last}")


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
def _talkback(steps: List[str], package: str = PACKAGE) -> Iterator[str]:
    """TalkBack on for the block (settings snapshotted first, restored after); yields the
    app's activity from before, to bring back when something covers it."""
    from inspector_widget.talkback import device as tb
    top_before = tb.top_activity(SERIAL) or ""
    try:
        # Dismisses the permission dialog TalkBack raises on every start (it would take the
        # typed keys) and brings the app back to the front.
        out = tb.enable(SERIAL, package)
        steps.append(f"talkback on: changed={out.get('changed')} dismissed={out.get('dismissed')} "
                     f"refronted={out.get('refronted')}")
        yield top_before
    finally:
        tb.restore(SERIAL)


def _front(top_before: str, steps: List[str], package: str = PACKAGE) -> None:
    """Put the app back on top when something covers it: BACK out of TalkBack's tutorial or
    permission dialog (which can come seconds after TalkBack started), else re-front it (a
    closed activity comes back as a new one: callers resolve their target again)."""
    from inspector_widget.talkback import device as tb
    top = tb.top_activity(SERIAL)
    if top and top.startswith(package + "/"):
        return
    dismissed = tb.dismiss_talkback_activities(SERIAL, top_before)
    out = tb.ensure_foreground(SERIAL, package, top_before)
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
               steps: List[str], package: str = PACKAGE
               ) -> Tuple[List[Dict[str, Any]], int, List[Source]]:
    """Give the field ``resolve()`` names input focus and type TYPED into it one key at a
    time (a masked field shows each character for a moment); returns the events recorded
    meanwhile, the next seq, and the field's (host_view_id, virtual_id) per attempt, the last
    one current. Tries twice when the field never took input focus or no text change came
    from it; the field is resolved again each time (its activity may have been recreated)."""
    from inspector_widget import a11y
    events: List[Dict[str, Any]] = []
    sources: List[Source] = []
    for attempt in (1, 2):
        _front(top_before, steps, package)
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
                    session, lambda name=name: (_view_id(session, name), -1), name, seq,
                    top_before, steps)
                events += got
    except Exception as e:  # a step that failed outright: report it with what was seen
        raise AssertionError(report([f"{type(e).__name__}: {e}"], events, steps)) from e
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


def _visible_password_event_problems(resolve: Callable[[Any], Source], package: str, label: str,
                                     dumps: Dict[str, Any], steps: List[str]
                                     ) -> Tuple[List[str], List[Dict[str, Any]], List[Source]]:
    """Accessibility focus on the visible-password field ``resolve()`` names, then type into it
    with TalkBack on: the focus reader's node text and every event of the field must be masked.
    Fills ``dumps`` (the focus read, then the a11y tree after typing); returns the problems,
    the events and the field's identities."""
    from inspector_widget import a11y
    events: List[Dict[str, Any]] = []
    problems: List[str] = []
    sources: List[Source] = []
    session = _attach(package)
    try:
        with _talkback(steps, package) as top_before:
            seq = session.a11y_focus().seq  # installs the tap
            _front(top_before, steps, package)
            source = resolve(session)
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
            got, seq, typed_into = _type_into(session, lambda: resolve(session), label,
                                              focus["seq"], top_before, steps, package)
            sources += typed_into
            events += focus["events"] + got
            dumps["a11y"] = a11y.a11y_to_dict(session.dump_a11y())
            dumps["compose"] = _compose(session, semantics=True, slot=False)
    except Exception as e:  # a step that failed outright: report it with what was seen
        raise AssertionError(report([f"{type(e).__name__}: {e}"], events, steps)) from e
    finally:
        _detach(session)
    problems += field_problems(events, sources, label)
    problems += [f"password text sent in plain text: {x}"
                 for x in leaks((COMPOSE_VISIBLE_SECRET, TYPED), events=events, **dumps)]
    after = _a11y_node(dumps.get("a11y") or {}, sources[-1])
    if after is None:
        problems.append(f"no a11y node {sources[-1]} after typing")
    elif not is_masked(after.get("text")) or len(after["text"]) <= len(COMPOSE_VISIBLE_SECRET):
        problems.append(f"a11y text after typing: {after.get('text')!r}, want more than "
                        f"{len(COMPOSE_VISIBLE_SECRET)} dots")
    return problems, events, sources


def test_event_tap_masks_a_compose_visible_password(device):
    """The Compose visible-password field: its events carry no isPassword and its node no
    input type, so the tap asks its SemanticsNode. Also the focus reader's node (the lite
    snapshot) and the dumps after typing."""
    _require_talkback()
    _launch(".MainActivity", "password_field")
    steps: List[str] = []

    def resolve(session) -> Source:
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

    problems, events, _ = _visible_password_event_problems(
        resolve, PACKAGE, COMPOSE_VISIBLE_TAG, {}, steps)
    assert not problems, report(problems, events, steps)


# --------------------------------------------------------------------------- #
# Fail closed: the R8-obfuscated app, and no over-masking in a normal one
# --------------------------------------------------------------------------- #
def _tokens(diagnostics: Optional[str], prefix: str) -> List[str]:
    return [t.strip() for t in (diagnostics or "").split(";") if t.strip().startswith(prefix)]


def _a11y_nodes(dump: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    for w in dump.get("windows") or []:
        yield from _nodes(w.get("root") or {})


def _obfuscated_visible_field(session) -> Source:
    """password_field's visible-password field in the r8 build, from the a11y tree: Compose
    semantics (and so test tags) are out of reach there, and it is the scenario's only
    editable node that does not say password."""
    from inspector_widget import a11y
    fields = [n for n in _a11y_nodes(a11y.a11y_to_dict(session.dump_a11y(include_extras=False)))
              if "editable" in (n.get("flags") or []) and "password" not in (n.get("flags") or [])]
    if len(fields) != 1:
        raise AssertionError(f"want one editable non-password node in password_field, got "
                             f"{[n.get('node_key') for n in fields]}")
    return fields[0]["host_view_id"], fields[0].get("virtual_id", -1)


def _listed(token: str, source: Source) -> bool:
    """Whether a redaction_masked token names ``source`` (compose:<acv>:<id>)."""
    return f"compose:{source[0]}:{source[1]}" in token


def test_obfuscated_compose_never_sends_a_visible_password(r8_device):
    """The r8 build: Compose's classes are renamed, so nothing says the visible-password field
    is one. Its text is masked anyway, in every dump and in the focus reader's nodes (the
    input-focus subtree: without TalkBack the AndroidComposeView, whose subtree holds the
    field), the diagnostics name it and the ComposeView, and the labels around it stay
    readable."""
    from inspector_widget import a11y, adb, correlate, strings as st
    _launch(".MainActivity", "password_field", R8_PACKAGE)
    session = _attach(R8_PACKAGE)
    dumps: Dict[str, Any] = {}
    try:
        source = _obfuscated_visible_field(session)
        dumps["compose"] = _compose(session, semantics=True)
        dumps["a11y"] = a11y.a11y_to_dict(session.dump_a11y())
        dumps["tree"] = st.dump_tree_to_dict(session.dump_tree(include_properties=True))
        dumps["properties"] = st.get_properties_to_dict(session.get_properties(source[0]))
        dumps["inspect"] = correlate.inspect_tree(session, include_properties=True)
        dumps["inspect_node"] = correlate.inspect_node(
            session, node_key=f"virtual:{source[0]}:{source[1]}", include_image=False, lint=True,
            density=adb.display_density(SERIAL), font_scale=adb.font_scale(SERIAL))
        dumps["focus"] = a11y.a11y_focus_to_dict(
            session.a11y_focus(include_input_focus=True, subtree_depth=16))
    finally:
        _detach(session)
    leaked = leaks((COMPOSE_VISIBLE_SECRET, COMPOSE_FIELD_SECRET, *COMPOSE_SECRETS.values()), **dumps)
    assert not leaked, f"password text sent from the obfuscated app: {leaked}"

    compose_diag = dumps["compose"].get("diagnostics")
    assert _tokens(compose_diag, "compose_obfuscated"), f"not obfuscated: {compose_diag}"
    assert any(f"view#{source[0]}" in t for t in _tokens(compose_diag, "redaction_unverified")), \
        f"compose diagnostics: {compose_diag}"
    a11y_diag = dumps["a11y"].get("diagnostics")
    assert any(f"view#{source[0]}" in t for t in _tokens(a11y_diag, "redaction_unverified")), \
        f"a11y diagnostics: {a11y_diag}"
    assert any(_listed(t, source) for t in _tokens(a11y_diag, "redaction_masked")), \
        f"a11y diagnostics do not list the field: {a11y_diag}"
    assert dumps["inspect"]["summary"].get("redaction", {}).get("a11y"), dumps["inspect"]["summary"]
    assert dumps["inspect_node"].get("redaction"), "the dossier does not explain the dots"

    node = _a11y_node(dumps["a11y"], source)
    assert node is not None and node.get("text") == MASK * len(COMPOSE_VISIBLE_SECRET), \
        f"a11y text of the field: {node and node.get('text')!r}"
    texts = {n.get("text") for n in _a11y_nodes(dumps["a11y"])}
    assert VISIBLE_LABEL in texts and "Password field" in texts, \
        f"labels must stay readable, got {sorted(t for t in texts if t)}"
    inp = [n for n in _nodes((dumps["focus"].get("input") or {}).get("node") or {})
           if (n.get("host_view_id"), n.get("virtual_id", -1)) == source]
    assert inp, "the field is not in the input-focus subtree"
    assert inp[0].get("text") == MASK * len(COMPOSE_VISIBLE_SECRET), \
        f"focus reader's text of the field: {inp[0].get('text')!r}"
    assert any(_listed(t, source) for t in _tokens(dumps["focus"].get("diagnostics"),
                                                   "redaction_masked")), dumps["focus"].get("diagnostics")


@pytest.mark.parametrize("obfuscated", [False, True], ids=["debug", "r8"])
def test_a_plain_text_field_is_masked_only_when_obfuscated(obfuscated, request):
    """No over-masking: in the debug build a plain text field's text is read as is, with no
    redaction token anywhere. In the r8 build it is masked (nothing can say it is not a
    visible-password field) and listed."""
    from inspector_widget import a11y, strings as st
    package = R8_PACKAGE if obfuscated else PACKAGE
    request.getfixturevalue("r8_device" if obfuscated else "device")
    _launch(".MainActivity", "form_field", package)
    session = _attach(package)
    dump: Dict[str, Any] = {}
    try:
        if obfuscated:  # no test tags: either of the two plain fields
            dump = a11y.a11y_to_dict(session.dump_a11y(include_extras=False))
            fields = [n for n in _a11y_nodes(dump) if "editable" in (n.get("flags") or [])]
            assert len(fields) == 2, f"form_field has two text fields: {[n.get('node_key') for n in fields]}"
            source = (fields[0]["host_view_id"], fields[0].get("virtual_id", -1))
        else:
            acv, field = _compose_field(_compose(session, semantics=True, slot=False), PLAIN_TAG)
            source = (acv, int(field["id"]))
        act = a11y.a11y_act_to_dict(session.a11y_act(
            host_view_id=source[0], virtual_id=source[1], action="set_text",
            args={"ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE": PLAIN_TEXT}))
        assert act.get("performed"), f"set_text: {act}"

        def updated() -> bool:  # the field recomposes with its new text on a later frame
            nonlocal dump
            dump = a11y.a11y_to_dict(session.dump_a11y())
            return bool((_a11y_node(dump, source) or {}).get("text"))

        _await(updated, 3.0)
        compose = _compose(session, semantics=True, slot=False)
    finally:
        _detach(session)
    node = _a11y_node(dump, source)
    assert node is not None, f"no a11y node {source} after set_text"
    diags = {"a11y": dump.get("diagnostics"), "compose": compose.get("diagnostics")}
    if not obfuscated:
        assert node.get("text") == PLAIN_TEXT, f"a plain text field was masked: {node.get('text')!r}"
        _, field = _compose_field(compose, PLAIN_TAG)
        assert (field.get("attrs") or {}).get("EditableText") == PLAIN_TEXT, field.get("attrs")
        noise = {k: _tokens(v, "redaction") for k, v in diags.items() if _tokens(v, "redaction")}
        assert not noise, f"redaction tokens in a normal app: {noise}"
    else:
        assert node.get("text") == MASK * len(PLAIN_TEXT), f"a11y text: {node.get('text')!r}"
        assert any(_listed(t, source) for t in _tokens(diags["a11y"], "redaction_masked")), diags
        assert PLAIN_TEXT not in str(dump)


def test_event_tap_masks_an_obfuscated_compose_visible_password(r8_device):
    """The r8 build with TalkBack on: the visible-password field's text events come from a
    Compose node whose status cannot be determined; they are masked, and so is the focus
    reader's node."""
    _require_talkback()
    _launch(".MainActivity", "password_field", R8_PACKAGE)
    steps: List[str] = []
    dumps: Dict[str, Any] = {}

    def resolve(session) -> Source:
        try:
            return _obfuscated_visible_field(session)
        except AssertionError:
            steps.append("password_field is not on screen: starting it again")
            _sh(f"am start -W -n {R8_PACKAGE}/{PACKAGE}.MainActivity --es scenario password_field")
            return _obfuscated_visible_field(session)

    problems, events, sources = _visible_password_event_problems(
        resolve, R8_PACKAGE, "obfuscated " + COMPOSE_VISIBLE_TAG, dumps, steps)
    masked = _tokens((dumps.get("a11y_focus") or {}).get("diagnostics"), "redaction_masked")
    if not any(_listed(t, sources[0]) for t in masked):
        problems.append(f"the focus read does not list the field as masked: {masked}")
    assert not problems, report(problems, events, steps)
