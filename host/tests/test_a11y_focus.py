"""A11yFocus / A11yAct / WindowInfo over the wire, against the offline fake agent.

The fake mirrors A11yFocus.kt / A11yEventTap.kt (see fakeagent's docstring): the
long-poll waits without the device lock, the event tap keeps 512 records, and
accessibility focus actions are refused while touch exploration is off. These
tests pin the host side (Client, Session, request deadlines, dict shaping) and
the wire contract the walk driver builds on.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

import fakeagent
import inspector_widget
from inspector_widget import a11y
from inspector_widget import client as clientmod
from inspector_widget.client import Client
from inspector_widget.proto import view_inspection_pb2 as pb

SUBMIT = ("compose:1006:2", 1006, 2)   # the Compose "Submit" button in the default scene
OK_BUTTON = ("view:1004", 1004, -1)


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


def _focus(client, **kw):
    return a11y.a11y_focus_to_dict(client.a11y_focus(**kw))


# --------------------------------------------------------------------------- #
# Reading focus
# --------------------------------------------------------------------------- #
def test_no_focus_is_a_valid_answer(agent):
    out = _focus(agent.connect())
    assert out["a11y"] is None
    assert out["seq"] == 0 and out["events"] == [] and not out["focus_event"]
    assert out["touch_exploration"] is False
    assert "no accessibility focus" in out["diagnostics"]


def test_focus_names_the_node_the_dump_marks_focused(agent):
    agent.set_a11y_focus(1006, 2)
    c = agent.connect()
    out = _focus(c, subtree_depth=1)
    f = out["a11y"]
    assert (f["node_key"], f["host_view_id"], f["virtual_id"]) == SUBMIT
    assert f["id"] == a11y.a11y_key(1006, 2)
    assert f["root_view_id"] == 1001 and f["stale"] is False and f["source"] == "view-root"
    assert f["bounds"]["layout"] == {"x": 16, "y": 176, "w": 200, "h": 56}
    assert f["node"]["text"] == "Submit" and "accessibility_focused" in f["node"]["flags"]
    assert f["node"]["node_key"] == "compose:1006:2"
    assert f["window"]["title"] == "A11yProbe" and f["window"]["z"] == 0

    dump = a11y.a11y_to_dict(c.dump_a11y())
    focused = [n for n in a11y._iter_nodes([w["root"] for w in dump["windows"]])
               if "accessibility_focused" in (n.get("flags") or [])]
    assert [(n["node_key"], n["id"]) for n in focused] == [(f["node_key"], f["id"])]


def test_events_after_seq_and_the_next_seq(agent):
    c = agent.connect()
    seq0 = _focus(c)["seq"]
    agent.set_a11y_focus(1004)
    agent.set_a11y_focus(1006, 2)
    out = _focus(c, after_seq=seq0)
    assert [(e["type"], e["node_key"]) for e in out["events"]] == [
        ("VIEW_ACCESSIBILITY_FOCUSED", "view:1004"),
        ("VIEW_ACCESSIBILITY_FOCUS_CLEARED", "view:1004"),
        ("VIEW_ACCESSIBILITY_FOCUSED", "compose:1006:2"),
    ]
    assert out["focus_event"] and out["seq"] == 3
    assert _focus(c, after_seq=out["seq"])["events"] == []
    assert len(_focus(c, after_seq=seq0, max_events=1)["events"]) == 1


def test_events_that_fell_out_of_the_ring_are_counted(agent):
    for _ in range(600):
        agent.a11y_tap.record(fakeagent.TYPE_WINDOW_CONTENT_CHANGED, 1001, 1002, -1,
                              content_change_types=1)
    out = _focus(agent.connect(), after_seq=0)
    assert len(out["events"]) == 512 and out["dropped"] == 88
    assert out["events"][0]["seq"] == 89 and out["events"][0]["content_changes"] == ["SUBTREE"]


def test_input_focus_is_optional(agent):
    assert "input" not in _focus(agent.connect())
    assert "no input focus" in _focus(agent.connect(), include_input_focus=True)["diagnostics"]


# --------------------------------------------------------------------------- #
# Long-poll
# --------------------------------------------------------------------------- #
def test_long_poll_returns_on_the_focus_event(agent):
    c = agent.connect()
    seq0 = _focus(c)["seq"]
    agent.set_a11y_focus(1004, delay=0.15)
    t0 = time.monotonic()
    out = _focus(c, after_seq=seq0, wait_ms=3000, quiet_ms=50)
    took = time.monotonic() - t0
    assert out["focus_event"] and not out["timed_out"]
    assert out["a11y"]["node_key"] == "view:1004"
    assert 0.15 <= took < 1.5, took
    assert 150 <= out["waited_ms"] < 1500


def test_long_poll_times_out_without_a_focus_move(agent):
    c = agent.connect()
    agent.set_a11y_focus(1004)
    seq = _focus(c)["seq"]
    out = _focus(c, after_seq=seq, wait_ms=200)
    assert out["timed_out"] and not out["focus_event"] and out["waited_ms"] >= 200
    assert out["a11y"]["node_key"] == "view:1004"


def test_long_poll_with_a_seq_from_an_earlier_agent_waits_for_new_events(agent):
    c = agent.connect()
    agent.set_a11y_focus(1004, delay=0.1)
    out = _focus(c, after_seq=10_000, wait_ms=2000)
    assert out["focus_event"] and "ahead of the event tap" in out["diagnostics"]


def test_long_poll_does_not_hold_up_other_clients(agent):
    waiter, other = agent.connect(), agent.connect()
    result = {}
    t = threading.Thread(target=lambda: result.setdefault(
        "out", _focus(waiter, after_seq=0, wait_ms=1500)))
    t.start()
    time.sleep(0.1)
    t0 = time.monotonic()
    other.dump_a11y()
    assert time.monotonic() - t0 < 0.5  # not queued behind the 1.5s wait
    t.join(5)
    assert result["out"]["timed_out"]


def test_long_poll_deadline_is_the_base_plus_the_wait(monkeypatch):
    monkeypatch.delenv(clientmod.TIMEOUT_ENV, raising=False)
    monkeypatch.delenv(clientmod.LEGACY_TIMEOUT_ENV, raising=False)
    req = pb.Request()
    req.a11y_focus.wait_ms = 1500
    req.a11y_focus.quiet_ms = 150
    assert clientmod.request_timeout(req) == pytest.approx(31.65)
    req.a11y_focus.wait_ms = 0
    req.a11y_focus.quiet_ms = 0
    assert clientmod.request_timeout(req) == 30.0
    monkeypatch.setenv(clientmod.TIMEOUT_ENV, "0")
    assert clientmod.request_timeout(req) is None


def test_shutdown_wakes_a_long_poll(agent):
    c = agent.connect()
    errors = []

    def wait():
        try:
            c.a11y_focus(after_seq=0, wait_ms=5000)
        except Exception as exc:  # the agent stopped: the reply never comes
            errors.append(exc)

    t = threading.Thread(target=wait)
    t.start()
    time.sleep(0.1)
    t0 = time.monotonic()
    agent.stop()
    t.join(5)
    assert not t.is_alive() and time.monotonic() - t0 < 2
    assert errors and isinstance(errors[0], clientmod.SessionLostError)


# --------------------------------------------------------------------------- #
# Acting
# --------------------------------------------------------------------------- #
def test_accessibility_focus_is_refused_without_touch_exploration(agent):
    out = a11y.a11y_act_to_dict(agent.connect().a11y_act(1006, 2, "accessibility_focus"))
    assert out["performed"] is False and "touch exploration is off" in out["error"]
    assert out["action"] == "ACCESSIBILITY_FOCUS" and out["after"] is None
    assert agent.a11y_focus is None


def test_accessibility_focus_moves_focus_with_touch_exploration(agent):
    agent.touch_exploration = True
    c = agent.connect()
    out = a11y.a11y_act_to_dict(c.a11y_act(1006, 2, "ACCESSIBILITY_FOCUS"))
    assert out["performed"] and "error" not in out
    assert out["after"]["node_key"] == "compose:1006:2"
    follow = _focus(c, after_seq=out["seq_before"], wait_ms=500)
    assert follow["focus_event"] and follow["events"][-1]["type"] == "VIEW_ACCESSIBILITY_FOCUSED"


def test_other_actions_work_without_touch_exploration(agent):
    c = agent.connect()
    out = a11y.a11y_act_to_dict(c.a11y_act(1004, -1, "click"))
    assert out["performed"] and out["action"] == "CLICK"
    assert _focus(c, after_seq=out["seq_before"])["events"][0]["type"] == "VIEW_CLICKED"
    raw = c.a11y_act(1004, -1, "raw", raw_action_id=0x10)
    assert raw.performed and raw.action_id == 0x10


@pytest.mark.parametrize("host,vid,needle", [
    (999, -1, "no View with host_view_id 999"),
    (1004, 7, "serves no virtual nodes"),
    (1006, 77, "no longer resolves"),
])
def test_act_on_a_missing_node_explains(agent, host, vid, needle):
    out = agent.connect().a11y_act(host, vid, "click")
    assert not out.performed and needle in out.error


def test_node_action_names():
    assert clientmod.node_action("click") == pb.NODE_ACTION_CLICK
    assert clientmod.node_action("NODE_ACTION_SHOW_ON_SCREEN") == pb.NODE_ACTION_SHOW_ON_SCREEN
    assert clientmod.node_action("clear-accessibility-focus") == pb.NODE_ACTION_CLEAR_ACCESSIBILITY_FOCUS
    assert clientmod.node_action(pb.NODE_ACTION_RAW) == pb.NODE_ACTION_RAW
    with pytest.raises(ValueError, match="one of: accessibility_focus"):
        clientmod.node_action("tap")


def test_act_args_become_typed_bundle_entries(agent):
    agent.connect().a11y_act(1004, -1, "set_text", args={
        "ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE": "hi", "n": 3, "b": True, "f": 0.5})
    [cmd] = [r.a11y_act for r in agent.requests if r.WhichOneof("command") == "a11y_act"]
    args = {a.key: getattr(a, a.WhichOneof("value")) for a in cmd.args}
    assert args == {"ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE": "hi", "n": 3, "b": True, "f": 0.5}
    assert [a.WhichOneof("value") for a in cmd.args] == [
        "string_value", "int_value", "bool_value", "float_value"]


# --------------------------------------------------------------------------- #
# Windows
# --------------------------------------------------------------------------- #
def test_dump_windows_carry_window_info(agent):
    data = a11y.a11y_to_dict(agent.connect().dump_a11y())
    main, popup = data["windows"]
    assert main["title"] == "A11yProbe" and main["layout_title"].endswith(".MainActivity")
    assert (main["window_type"], main["window_flags"], main["z"]) == (1, "0x81810100", 0)
    assert main["frame"] == {"x": 0, "y": 0, "w": 360, "h": 640}
    assert main["insets"]["status_bars"] == {"left": 0, "top": 24, "right": 0, "bottom": 0,
                                             "visible": True}
    assert main["modal"] is True and popup["modal"] is False and "title" not in popup
    assert "covered_by" not in main  # the popup is not modal


def test_window_info_wins_over_the_diagnostics_token():
    resp = pb.DumpA11yResponse(diagnostics="root#2 window type=1 flags=0x0")
    w = resp.windows.add(root_view_id=2)
    w.root.host_view_id = 2
    w.root.virtual_id = -1
    w.info.window_type = 2
    w.info.wm_flags = 0x28
    [win] = a11y.a11y_to_dict(resp)["windows"]
    assert (win["window_type"], win["window_flags"], win["modal"]) == (2, "0x28", False)


def test_windows_without_info_keep_the_old_shape():
    resp = pb.DumpA11yResponse(diagnostics="root#2 window type=1 flags=0x81810100")
    w = resp.windows.add(root_view_id=2)
    w.root.host_view_id = 2
    w.root.virtual_id = -1
    [win] = a11y.a11y_to_dict(resp)["windows"]
    assert set(win) == {"root_view_id", "root", "window_type", "window_flags", "modal"}


# --------------------------------------------------------------------------- #
# Session (the full attach path over the fake adb)
# --------------------------------------------------------------------------- #
def test_session_focus_and_act(fake_device):
    with inspector_widget.attach(serial=fake_device.serial, package="com.oberkfell.a11yprobe") as s:
        agent = fake_device.agent()
        agent.touch_exploration = True
        seq = s.a11y_focus().seq
        act = s.a11y_act(node_key="compose:1006:5")
        assert act.performed and act.after.virtual_id == 5
        out = a11y.a11y_focus_to_dict(s.a11y_focus(after_seq=seq, wait_ms=500, quiet_ms=20))
        assert out["a11y"]["node_key"] == "compose:1006:5" and out["focus_event"]
        assert s.a11y_act(host_view_id=1004, action="clear_accessibility_focus").performed
        with pytest.raises(ValueError, match="names no ComposeView"):
            s.a11y_act(node_key="compose:5")
        with pytest.raises(ValueError, match="needs node_key or host_view_id"):
            s.a11y_act()
