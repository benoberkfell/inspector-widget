"""Offline side of the password-redaction checks (the masking itself runs on the device).

* ``redaction_checks`` decides pass / fail for ``test_device_redaction.py``: pinned here on
  synthetic events, and on events carried through the fake agent's wire, so a masked text
  arrives as the dots the agent sent and an unmasked one is reported with its text.
* The agent's text paths agree on what a Compose password field is: the semantics dump, the
  a11y tree and the event tap all ask ``ComposeInspector.isPasswordNode`` (checked in the
  Kotlin source, like ``test_wire_depth``), and A11yProbe carries the visible-password
  secret the device checks grep for.
"""

from __future__ import annotations

import re
import socket
from pathlib import Path

import pytest

import fakeagent
import redaction_checks as rc
from inspector_widget import a11y
from inspector_widget.client import Client

REPO = Path(__file__).resolve().parents[2]
PAYLOAD_DIR = REPO / "agent" / "src" / "main" / "kotlin" / "com" / "oberkfell" / "viewspector" / "agent" / "payload"
SCENARIOS_KT = (REPO / "testapps" / "a11yprobe" / "app" / "src" / "main" / "kotlin" / "com" / "oberkfell"
                / "a11yprobe" / "Scenarios.kt")

TYPE_VIEW_FOCUSED = 0x00000008
TYPE_VIEW_TEXT_CHANGED = 0x00000010
TYPE_VIEW_TEXT_SELECTION_CHANGED = 0x00002000
ACV = "androidx.compose.ui.platform.AndroidComposeView"
FIELD = (1006, 59)
SECRET = "hunter2-visible-compose"


def _ev(type_, source=FIELD, text=None, seq=1, **kw):
    return {"seq": seq, "type": type_, "host_view_id": source[0], "virtual_id": source[1],
            "node_key": f"compose:{source[0]}:{source[1]}", "text": text, **kw}


# --------------------------------------------------------------------------- #
# The checks
# --------------------------------------------------------------------------- #
def test_is_masked_wants_only_dots():
    assert rc.is_masked(rc.MASK * 3)
    assert not rc.is_masked("")
    assert not rc.is_masked(None)
    assert not rc.is_masked(rc.MASK * 2 + "x")  # the character a masked field shows briefly


def test_leaks_names_the_dump_and_the_secret_anywhere_in_it():
    dumps = {"a11y": {"windows": [{"root": {"text": SECRET + "qzx"}}]},
             "compose": {"attrs": {SECRET: "key, not value"}},
             "clean": {"text": rc.MASK * len(SECRET)}}
    assert rc.leaks([SECRET, "absent", ""], **dumps) == [f"a11y: {SECRET!r}", f"compose: {SECRET!r}"]


def test_event_problems_pass_masked_events_and_ignore_other_sources():
    events = [_ev(rc.TEXT_CHANGED, text=rc.MASK * 24, seq=1),
              _ev("VIEW_TEXT_SELECTION_CHANGED", text=rc.MASK * 24, seq=2),
              _ev(rc.TEXT_CHANGED, source=(1006, 7), text="a plain field", seq=3)]
    assert rc.event_problems(events, FIELD, "bad_password") == []


def test_event_problems_name_the_condition_and_the_text():
    events = [_ev(rc.TEXT_CHANGED, text=SECRET + "q", seq=4),
              _ev("WINDOW_STATE_CHANGED", text=rc.MASK, pane_title="pane " + SECRET, seq=5)]
    problems = rc.event_problems(events, FIELD, "bad_password")
    assert problems == [
        f"bad_password: VIEW_TEXT_CHANGED seq=4 text not masked: {SECRET + 'q'!r}",
        f"bad_password: WINDOW_STATE_CHANGED seq=5 pane_title not masked: {'pane ' + SECRET!r}",
    ]
    missing = rc.event_problems([_ev(rc.FOCUSED, text=rc.MASK)], FIELD, "bad_password")
    assert missing == ["bad_password: 0 VIEW_TEXT_CHANGED event(s) from (1006, 59), want >= 1"]


def test_field_problems_follow_a_field_recreated_between_attempts():
    old, new = (1006, 59), (2044, 61)
    events = [_ev(rc.FOCUSED, source=old, text=rc.MASK, seq=1),
              _ev(rc.TEXT_CHANGED, source=new, text=rc.MASK * 3, seq=2)]
    assert rc.field_problems(events, [old, new, new], "f") == []
    leaked = events + [_ev(rc.TEXT_CHANGED, source=old, text="qz", seq=3)]
    assert rc.field_problems(leaked, [old, new], "f") == ["f: VIEW_TEXT_CHANGED seq=3 text not masked: 'qz'"]
    assert rc.field_problems(events, [new, old], "f") == [
        "f: 0 VIEW_TEXT_CHANGED event(s) from (1006, 59), want >= 1"]
    assert rc.field_problems(events, [], "f") == ["f: the field was never found"]


def test_report_carries_the_events_and_the_steps():
    msg = rc.report(["x failed"], [_ev(rc.TEXT_CHANGED, text="abc", seq=9)],
                    ["focus performed=True input_focus=False"])
    assert msg.splitlines()[0] == "x failed"
    assert "9 VIEW_TEXT_CHANGED compose:1006:59 text='abc'" in msg
    assert "input_focus=False" in msg
    assert "(no events)" in rc.report(["y"]) and "(none)" in rc.report(["y"])
    many = rc.summarize([_ev(rc.FOCUSED, seq=i) for i in range(100)], limit=10)
    assert many.splitlines()[0] == "  ... 90 earlier event(s)" and len(many.splitlines()) == 11


# --------------------------------------------------------------------------- #
# Through the fake agent's wire
# --------------------------------------------------------------------------- #
@pytest.fixture
def agent():
    a = fakeagent.FakeAgent()
    socks = []

    def connect():
        s = socket.create_connection(("127.0.0.1", a.port), timeout=5)
        socks.append(s)
        return Client(s)

    a.connect = connect
    yield a
    a.stop()
    for s in socks:
        s.close()


def _record(agent, type_, text, source=FIELD):
    agent.a11y_tap.record(type_, 1001, source[0], source[1], text=text,
                          class_name="android.widget.EditText", host_class=ACV)


def test_masked_event_text_arrives_as_sent_and_passes(agent):
    _record(agent, TYPE_VIEW_FOCUSED, rc.MASK * len(SECRET))
    _record(agent, TYPE_VIEW_TEXT_CHANGED, rc.MASK * (len(SECRET) + 1))
    _record(agent, TYPE_VIEW_TEXT_SELECTION_CHANGED, rc.MASK * (len(SECRET) + 1))
    events = a11y.a11y_focus_to_dict(agent.connect().a11y_focus())["events"]
    assert [e["node_key"] for e in events] == ["compose:1006:59"] * 3
    assert events[1]["text"] == rc.MASK * (len(SECRET) + 1)
    assert rc.event_problems(events, FIELD, "bad_password") == []
    assert rc.leaks([SECRET], events=events) == []


def test_an_unmasked_event_from_the_wire_is_reported(agent):
    _record(agent, TYPE_VIEW_TEXT_CHANGED, SECRET + "q")
    events = a11y.a11y_focus_to_dict(agent.connect().a11y_focus())["events"]
    problems = rc.event_problems(events, FIELD, "bad_password")
    assert len(problems) == 1 and SECRET + "q" in problems[0]
    assert rc.leaks([SECRET], events=events) == [f"events: {SECRET!r}"]


# --------------------------------------------------------------------------- #
# One Compose password predicate for every agent text path
# --------------------------------------------------------------------------- #
def _src(name: str) -> str:
    return (PAYLOAD_DIR / name).read_text()


def test_every_text_path_asks_the_one_compose_password_predicate():
    compose = _src("ComposeInspector.kt")
    # The semantics dump masks by it, and the a11y walk's index is filled by it.
    assert "passwordState(node)" in compose and \
        "Redaction.redactComposeAttrs(attrs, ctx.secrets, password)" in compose, "semantics dump"
    assert "passwords.add(id)" in compose and "unverified.add(id)" in compose, "semantics index"
    a11y_kt = _src("AccessibilityInspector.kt")
    assert "composePassword(view, ident.virtualId, ctx)" in a11y_kt and \
        "index.passwordState(virtualId)" in a11y_kt
    assert "ComposeInspector.passwordState(view, " in a11y_kt, "lite snapshot"
    assert "ComposeInspector.passwordState(host, " in _src("A11yEventTap.kt"), "event tap"
    # The slot table and the modifier scan go by the same values.
    redaction = _src("Redaction.kt")
    for cls in ("androidx.compose.foundation.text.KeyboardOptions",
                "androidx.compose.ui.text.input.ImeOptions",
                "androidx.compose.ui.text.input.PasswordVisualTransformation"):
        assert f'"{cls}"' in redaction, cls


def test_every_text_path_fails_closed():
    """A source whose password status cannot be determined is UNKNOWN, never "not a password",
    and every text path masks an editable UNKNOWN source (Redaction.mustMask)."""
    compose = _src("ComposeInspector.kt")
    a11y_kt = _src("AccessibilityInspector.kt")
    tap = _src("A11yEventTap.kt")
    # UNKNOWN: an unresolved source, an unreadable semantics tree, a text field without
    # keyboard options the check recognises (R8 renamed them).
    assert "val view = ident.view ?: return Redaction.PasswordState.UNKNOWN" in a11y_kt
    assert "if (host == null) return Redaction.PasswordState.UNKNOWN" in tap
    assert "if (node == null) Redaction.PasswordState.UNKNOWN else passwordState(node)" in compose
    assert "return seen ?: Redaction.PasswordState.UNKNOWN" in compose
    # ...masked where the source is editable.
    assert "Redaction.mustMask(state, editable = true)" in compose, "semantics dump"
    assert "Redaction.mustMask(state, isEditable(node))" in a11y_kt, "a11y tree and focus reader"
    assert "state == Redaction.PasswordState.UNKNOWN && isEditableSource(" in tap, "event tap"
    assert "Redaction.mustMaskView(view)" in _src("TreeBuilder.kt"), "View tree"
    assert "Redaction.mustMaskView(view)" in _src("Properties.kt"), "properties"


def test_the_redaction_tokens_the_host_surfaces_are_the_agents():
    """inspect's summary.redaction picks the agent's tokens by prefix (correlate)."""
    from inspector_widget import correlate
    redaction = _src("Redaction.kt")
    for token in correlate._REDACTION_TOKENS:
        assert f'"; {token}: ' in redaction, token


def test_a11yprobe_has_the_visible_password_secret_the_device_checks_grep_for():
    import test_device_redaction as dev
    src = SCENARIOS_KT.read_text()
    m = re.search(r'const val COMPOSE_VISIBLE_PASSWORD_SECRET = "([^"]+)"', src)
    assert m and m.group(1) == dev.COMPOSE_VISIBLE_SECRET
    assert "mutableStateOf(COMPOSE_VISIBLE_PASSWORD_SECRET)" in src
    tag = src.index(f'.testTag("{dev.COMPOSE_VISIBLE_TAG}")')
    field = src[src.rindex("OutlinedTextField(", 0, tag):tag]
    assert "KeyboardType.Password" in field and "VisualTransformation" not in field, field
