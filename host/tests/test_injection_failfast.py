"""Injection fails fast and says why (real-app run B4), offline.

When the agent can't start (an R8-shrunk app's Kotlin shadowing the payload,
say) it logs the cause within milliseconds of the attach. The host reads the
app's error log while it waits for the agent's socket, so it reports that
cause at once instead of a generic timeout 13 s later, and remembers it per
(serial, package, pid, build) so a retry doesn't inject and fail again.

Runs the real adb / inject code against ``tests/fakeagent.py``, whose device
keeps a logcat and a clock and can script an agent's start-up failure.
"""

from __future__ import annotations

import pytest

from fakeagent import DEFAULT_PACKAGE as PKG
from fakeagent import DEFAULT_PID as PID
from fakeagent import DEFAULT_SERIAL as SERIAL

from inspector_widget import adb


# =========================================================================== #
# adb helpers: the device clock, logcat, process bitness
# =========================================================================== #
def test_parse_logcat_reads_threadtime_epoch_usec_lines():
    text = (
        "--------- beginning of main\n"
        "  1727706579.744101 10709 10709 I ViewSpector: ViewSpector native agent attaching\n"
        "1727706579.756123 10709 10720 E ViewSpector: initialize: error invoking x.Payload.start\n"
        "1727706579.756123 10709 10720 E ViewSpector: \tat java.lang.Thread.run(Thread.java:1571)\n"
        "1727706579.800000 10709 10709 E ActivityThread : Attaching agent with a.so=b failed: x\n"
        "1727706579.900000 10709 10709 W ViewSpector: \n"
        "not a log line\n"
    )
    entries = adb.parse_logcat(text)
    assert [(e.level, e.tag) for e in entries] == [
        ("I", "ViewSpector"), ("E", "ViewSpector"), ("E", "ViewSpector"),
        ("E", "ActivityThread"), ("W", "ViewSpector")]
    first, _, stack, activity, empty = entries
    assert (first.time, first.pid, first.tid) == (1727706579.744101, 10709, 10709)
    assert first.message == "ViewSpector native agent attaching"
    assert stack.message == "\tat java.lang.Thread.run(Thread.java:1571)"
    assert activity.message == "Attaching agent with a.so=b failed: x"
    assert empty.message == ""


def test_logcat_filters_by_pid_time_and_tag_level(fake_device):
    now = fake_device.clock()
    fake_device.log(PID, "E", "ViewSpector", "too old", at=now - 5)
    fake_device.log(PID, "E", "ViewSpector", "agent error\n\tat frame", at=now)
    fake_device.log(PID, "I", "ViewSpector", "agent info", at=now)
    fake_device.log(PID, "E", "SomeAppTag", "the app's own error", at=now)
    fake_device.log(PID + 1, "E", "ViewSpector", "another process", at=now)
    entries = adb.logcat(SERIAL, pid=PID, since=now - 1, specs=("ViewSpector:E",))
    assert [e.message for e in entries] == ["agent error", "\tat frame"]
    both = adb.logcat(SERIAL, pid=PID, since=now - 1, specs=("ViewSpector:I", "*:W"))
    assert [e.message for e in both] == ["agent error", "\tat frame", "agent info",
                                         "the app's own error"]
    everything = adb.logcat(SERIAL, specs=("ViewSpector:E",))
    assert len(everything) == 4  # no pid or time filter
    [argv] = [a for a in fake_device.shell_log() if a.startswith("logcat")][:1]
    assert "-v threadtime -v epoch -v usec" in argv and "-s ViewSpector:E" in argv


def test_logcat_failure_is_none_not_empty(fake_device, monkeypatch):
    assert adb.logcat(SERIAL, pid=PID, specs=("ViewSpector:E",)) == []
    monkeypatch.setattr(fake_device, "_logcat", lambda args: (1, "", "logcat: bad"))
    assert adb.logcat(SERIAL, pid=PID, specs=("ViewSpector:E",)) is None


def test_device_time_with_and_without_nanoseconds(fake_device):
    fake_device.clock = lambda: 1727706579.123456789
    assert adb.device_time(SERIAL) == pytest.approx(1727706579.123456789)
    fake_device.date_supports_nanos = False  # a date without %N prints it verbatim
    assert adb.device_time(SERIAL) == 1727706579.0


def test_process_bitness_reads_the_exe_link_as_the_app(fake_device):
    assert adb.process_bitness(SERIAL, PKG, PID) == 64
    fake_device.apps[PKG].bitness = 32
    assert adb.process_bitness(SERIAL, PKG, PID) == 32
    fake_device.apps[PKG].bitness = None  # unreadable
    assert adb.process_bitness(SERIAL, PKG, PID) is None
    assert adb.process_bitness(SERIAL, "com.example.release", 5151) is None  # not debuggable
    assert f"run-as {PKG} readlink /proc/{PID}/exe" in fake_device.shell_log()
