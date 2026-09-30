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

import time

import pytest

from fakeagent import DEFAULT_PACKAGE as PKG
from fakeagent import DEFAULT_PID as PID
from fakeagent import DEFAULT_SERIAL as SERIAL

from inspector_widget import adb, inject


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


# =========================================================================== #
# Fail fast: the agent's start-up error, read from logcat while waiting
# =========================================================================== #
# What NiA's R8-shrunk (not renamed) release build logged 12 ms after attach
# (real-app run, emulator-5554, API 37).
NIA_APK = ("/data/app/~~G9JleUeu_3P0lWHxM1JKRw==/com.google.samples.apps.nowinandroid.demo-"
           "78xCzPsN9ay4UIKlMPHQpQ==/base.apk!2")
R8_SHADOWED_KOTLIN = [("E", "ViewSpector", "\n".join([
    "initialize: error invoking com.oberkfell.viewspector.agent.payload.Payload.start",
    "java.lang.reflect.InvocationTargetException",
    "\tat java.lang.reflect.Method.invoke(Native Method)",
    "\tat com.oberkfell.viewspector.agent.Bootstrap$1.run(Bootstrap.java:137)",
    "\tat java.lang.Thread.run(Thread.java:1571)",
    "Caused by: java.lang.NoSuchMethodError: No static method checkNotNullParameter("
    "Ljava/lang/Object;Ljava/lang/String;)V in class Lkotlin/jvm/internal/Intrinsics; or its "
    f"super classes (declaration of 'kotlin.jvm.internal.Intrinsics' appears in {NIA_APK})",
    "\tat com.oberkfell.viewspector.agent.payload.Payload.start(Payload.kt:2)",
    "\t... 3 more",
]))]


def _inject():
    return inject.inject_and_connect(serial=SERIAL, package=PKG)


def _pushes(dev):
    return sum(1 for argv in dev.adb_log if argv[:1] == ["push"])


def test_shadowed_kotlin_fails_fast_with_the_cause(fake_device):
    fake_device.apps[PKG].startup_error = R8_SHADOWED_KOTLIN
    started = time.monotonic()
    with pytest.raises(inject.AgentStartupError) as err:
        _inject()
    assert time.monotonic() - started < 1.0  # not the 13 s socket timeout
    e = err.value
    assert (e.kind, e.cacheable, e.cached) == ("classpath_shadowing", True, False)
    assert e.cause.startswith("java.lang.NoSuchMethodError: No static method checkNotNullParameter")
    msg = str(e)
    assert msg.startswith(f"the agent payload failed to start in '{PKG}' (pid {PID}): "
                          "java.lang.NoSuchMethodError: No static method checkNotNullParameter")
    assert "kotlin.jvm.internal.Intrinsics resolved to the app's own copy (in its base.apk)" in msg
    assert "R8/ProGuard shrank" in msg and "minified Kotlin classes" in msg
    assert "declaration of" not in msg  # restated, not dumped
    assert "isMinifyEnabled = false" in e.hint and "--force" in e.hint
    assert e.log[0].startswith("initialize: error invoking")
    assert fake_device.agent() is None and fake_device.forward_names() == []
    assert not fake_device.unexpected


def test_a_retry_reports_the_remembered_failure_without_injecting(fake_device, mcp, run_cli):
    fake_device.apps[PKG].startup_error = R8_SHADOWED_KOTLIN
    first = mcp("attach")
    assert "NoSuchMethodError" in first["error"] and "isMinifyEnabled" in first["hint"]
    pushes = _pushes(fake_device)
    started = time.monotonic()
    again = mcp("attach")
    assert time.monotonic() - started < 0.5
    assert again["error"].startswith(first["error"])
    assert f"[not re-injected: this app process (pid {PID}) failed this way" in again["error"]
    assert again["hint"] == first["hint"]
    assert len(fake_device.attach_calls) == 1 and _pushes(fake_device) == pushes
    res = mcp("dump_tree")  # every tool attaches the same way
    assert "not re-injected" in res["error"] and len(fake_device.attach_calls) == 1
    cached = inject.failed_injection(SERIAL, PKG, PID, inject.local_build_id())
    assert cached is not None and cached.kind == "classpath_shadowing"


def test_the_cli_prints_the_cause_and_the_hint(fake_device, run_cli):
    fake_device.apps[PKG].startup_error = R8_SHADOWED_KOTLIN
    res = run_cli("attach", "--serial", SERIAL, "--package", PKG)
    assert res.rc == 1
    assert "java.lang.NoSuchMethodError" in res.err and "timed out" not in res.err
    assert "\nhint: Inspect a build of the app without code shrinking" in res.err


def test_a_new_pid_a_new_build_or_force_injects_again(fake_device, tmp_path):
    app = fake_device.apps[PKG]
    app.startup_error = R8_SHADOWED_KOTLIN
    with pytest.raises(inject.AgentStartupError):
        _inject()
    with pytest.raises(inject.AgentStartupError) as err:
        _inject()
    assert err.value.cached and len(fake_device.attach_calls) == 1
    # --force (MCP force=true) injects anyway, and fails the same way.
    with pytest.raises(inject.AgentStartupError) as err:
        inject.inject_and_connect(serial=SERIAL, package=PKG, force_reinject=True)
    assert not err.value.cached and len(fake_device.attach_calls) == 2
    # A rebuilt agent is another build: injected.
    (tmp_path / "build-out" / inject.PAYLOAD_JAR_NAME).write_bytes(b"payload, rebuilt")
    with pytest.raises(inject.AgentStartupError) as err:
        _inject()
    assert not err.value.cached and len(fake_device.attach_calls) == 3
    # A restarted app is another process: injected; with a fixed app it works.
    fake_device.restart_app(PKG, new_pid=PID + 1)
    app.startup_error = None
    inj = _inject()
    try:
        assert not inj.warm and inj.pid == PID + 1 and len(fake_device.attach_calls) == 4
    finally:
        inj.close()
    assert inject._FAILED == {}  # the dead pid's failures were dropped, the live one never failed


def test_a_successful_forced_attach_forgets_the_failure(fake_device):
    app = fake_device.apps[PKG]
    app.startup_error = R8_SHADOWED_KOTLIN
    with pytest.raises(inject.AgentStartupError):
        _inject()
    app.startup_error = None
    inj = inject.inject_and_connect(serial=SERIAL, package=PKG, force_reinject=True)
    inj.close()
    assert inject.failed_injection(SERIAL, PKG, PID, inject.local_build_id()) is None
    inj = _inject()  # warm, no cached error in the way
    try:
        assert inj.warm
    finally:
        inj.close()


def test_errors_logged_before_this_attach_are_not_blamed_on_it(fake_device):
    now = fake_device.clock()
    for level, tag, message in R8_SHADOWED_KOTLIN:  # an earlier attempt into this pid
        fake_device.log(PID, level, tag, message, at=now - 30)
    fake_device.log(PID + 7, "E", "ViewSpector", "initialize: error invoking x")  # another app
    inj = _inject()
    try:
        assert not inj.warm and inj.hello is not None
    finally:
        inj.close()


def test_an_app_crash_during_the_start_is_reported_at_once(fake_device):
    fake_device.apps[PKG].startup_error = [("E", "AndroidRuntime", "\n".join([
        "FATAL EXCEPTION: ViewSpectorLaunch",
        f"Process: {PKG}, PID: {PID}",
        "java.lang.IllegalStateException: boom",
        "\tat com.oberkfell.viewspector.agent.payload.Server.run(Server.kt:90)",
    ]))]
    started = time.monotonic()
    with pytest.raises(inject.AgentStartupError) as err:
        _inject()
    assert time.monotonic() - started < 1.0
    e = err.value
    assert e.kind == "crash" and not e.cacheable
    assert str(e) == (f"'{PKG}' (pid {PID}) crashed while the agent was starting: "
                      "java.lang.IllegalStateException: boom. The stack trace runs through the "
                      "agent (com.oberkfell.viewspector).")
    assert "logcat -b crash" in e.hint and "monkey -p" in e.hint
    assert inject.failed_injection(SERIAL, PKG, PID, inject.local_build_id()) is None


def test_an_agent_library_the_app_cannot_load_is_reported(fake_device):
    spec = f"/data/user/0/{PKG}/{inject.NATIVE_SO_NAME}=..."
    reason = ('java.io.IOException: Unable to dlopen libviewspector.so: dlopen failed: '
              '"/data/user/0/p/libviewspector.so" is 64-bit instead of 32-bit')
    fake_device.apps[PKG].startup_error = [
        ("E", "ActivityThread", f"Attaching agent with {spec} failed: {reason}"),
        ("E", "ActivityThread", f"Attaching agent with {spec} failed: {reason}"),
    ]
    with pytest.raises(inject.AgentStartupError) as err:
        _inject()
    e = err.value
    assert e.kind == "library_load" and e.cacheable and e.cause == reason
    assert str(e) == f"'{PKG}' (pid {PID}) could not load the agent library: {reason}"


def test_an_unrecognised_agent_error_fails_after_a_grace(fake_device, monkeypatch):
    monkeypatch.setattr(inject, "UNKNOWN_ERROR_GRACE", 0.2)
    fake_device.apps[PKG].startup_error = [("E", "ViewSpector", "something new went wrong")]
    started = time.monotonic()
    with pytest.raises(inject.AgentStartupError) as err:
        _inject()
    assert time.monotonic() - started < 2.0
    e = err.value
    assert e.kind == "unknown" and not e.cacheable
    assert "something new went wrong" in str(e) and f"@viewspector_{PID}" in str(e)
    assert "adb logcat -s ViewSpector" in e.hint
    with pytest.raises(inject.AgentStartupError):
        _inject()
    assert len(fake_device.attach_calls) == 2  # not remembered: retried


def test_an_attach_that_never_runs_says_so_at_the_timeout(fake_device, fast_sleep):
    fake_device.apps[PKG].attach_stalls = True
    with pytest.raises(inject.InjectionError) as err:
        _inject()
    e = err.value
    assert not isinstance(e, inject.AgentStartupError)
    assert "the agent never started" in str(e) and "main thread" in str(e)
    assert "adb logcat -s ViewSpector" in e.hint and "force-stop" in e.hint


def test_a_timeout_quotes_the_agents_last_log_lines(fake_device, fast_sleep):
    fake_device.apps[PKG].startup_error = [
        ("I", "ViewSpector", "Payload.start: launching ViewSpector server (build abc)"),
        ("E", "SomeAppTag", "the app's own error"),
    ]
    with pytest.raises(inject.InjectionError) as err:
        _inject()
    msg = str(err.value)
    assert msg.startswith(f"timed out waiting for agent socket 'viewspector_{PID}' in '{PKG}'")
    assert "I/ViewSpector: Payload.start: launching ViewSpector server (build abc)" in msg
    assert "E/SomeAppTag: the app's own error" in msg
    assert "adb logcat -s ViewSpector" in err.value.hint


def test_an_app_that_exits_during_the_wait_is_reported(fake_device, fast_sleep, monkeypatch):
    fake_device.apps[PKG].attach_stalls = True
    attach = adb.attach_agent

    def attach_then_die(*args, **kwargs):
        out = attach(*args, **kwargs)
        fake_device.apps[PKG].pid = None
        return out

    monkeypatch.setattr(adb, "attach_agent", attach_then_die)
    with pytest.raises(inject.InjectionError) as err:
        _inject()
    assert f"'{PKG}' (pid {PID}) exited while the agent was starting." in str(err.value)
    assert "logcat -b crash" in err.value.hint


# =========================================================================== #
# The diagnosis itself
# =========================================================================== #
def _entries(*records, pid=PID, t=100.0):
    out = []
    for i, (level, tag, message) in enumerate(records):
        for line in message.split("\n"):
            out.append(adb.LogEntry(t + i, pid, pid, level, tag, line))
    return out


def _diagnose(*records):
    return inject._diagnose(_entries(*records), SERIAL, PKG, PID, f"viewspector_{PID}")


def test_diagnosis_kinds():
    assert _diagnose() is None
    assert _diagnose(("E", "SomeAppTag", "unrelated")) is None
    bind = _diagnose(("E", "ViewSpector", "Cannot bind @viewspector_4242: another ViewSpector "
                                          "server still holds it\njava.io.IOException: x"))
    assert bind.fatal and bind.error.kind == "bind" and not bind.error.cacheable
    assert "force-stop" in bind.error.hint
    native = _diagnose(("E", "ViewSpector", "Pending JNI exception during FindClass(Bootstrap)"),
                       ("E", "ViewSpector", "Could not find class x/Bootstrap"))
    assert native.fatal and native.error.kind == "native"
    assert "Pending JNI exception during FindClass(Bootstrap)" in str(native.error)
    boot = _diagnose(("E", "ViewSpector", "initialize: failed to bootstrap ViewSpector payload\n"
                                          "java.lang.ClassNotFoundException: x.Payload"))
    assert boot.error.kind == "bootstrap" and boot.error.cacheable
    assert "(java.lang.ClassNotFoundException: x.Payload)" in str(boot.error)
    early = _diagnose(("E", "ViewSpector", "initialize: could not locate an application "
                                           "ClassLoader; aborting bootstrap"))
    assert early.error.kind == "bootstrap" and not early.error.cacheable  # may be too early
    plain = _diagnose(("E", "ViewSpector", "initialize: error invoking x.Payload.start\n"
                                           "java.lang.reflect.InvocationTargetException\n"
                                           "Caused by: java.lang.IllegalStateException: boom"))
    assert plain.error.kind == "payload_start" and plain.error.cacheable
    assert str(plain.error).endswith("java.lang.IllegalStateException: boom.")
    one_load = _diagnose(("E", "ActivityThread", "Attaching agent with /x/libviewspector.so=y "
                                                 "failed: java.io.IOException: nope"))
    assert not one_load.fatal  # the second attempt (no class loader) may still work
    others_so = _diagnose(("E", "ActivityThread", "Attaching agent with /x/libother.so failed: z"))
    assert others_so is None


def test_diagnosis_prefers_a_fatal_record_over_an_unknown_one():
    found = _diagnose(("E", "ViewSpector", "something odd"), *R8_SHADOWED_KOTLIN)
    assert found.fatal and found.error.kind == "classpath_shadowing"


def test_shadowing_without_the_declaring_apk_is_only_probable():
    shadow = _diagnose(("E", "ViewSpector", "initialize: error invoking x.Payload.start\n"
                                            "java.lang.reflect.InvocationTargetException\n"
                                            "Caused by: java.lang.NoClassDefFoundError: Failed "
                                            "resolution of: Lcom/google/protobuf/"
                                            "GeneratedMessageLite;"))
    e = shadow.error
    assert e.kind == "classpath_shadowing"
    assert ("com.google.protobuf.GeneratedMessageLite probably resolved to the app's own copy, "
            "which R8/ProGuard shrank: the app's minified protobuf classes") in str(e)


def test_a_linkage_error_in_an_unrelated_class_is_not_called_shadowing():
    found = _diagnose(("E", "ViewSpector", "initialize: error invoking x.Payload.start\n"
                                           "Caused by: java.lang.NoSuchMethodError: No virtual "
                                           "method foo()V in class Lcom/example/Bar; or its super "
                                           "classes (declaration of 'com.example.Bar' appears in "
                                           "/system/framework/framework.jar)"))
    assert found.error.kind == "payload_start"


# =========================================================================== #
# ABI preflight: libviewspector.so must match the app process's native ABI
# =========================================================================== #
def _elf(elf_class, machine):
    """The first bytes of an ELF shared object: class, little-endian, e_machine."""
    return (b"\x7fELF" + bytes([elf_class, 1, 1, 0]) + b"\x00" * 8
            + (3).to_bytes(2, "little") + machine.to_bytes(2, "little") + b"\x00" * 44)


ARM64_SO = _elf(2, 183)


@pytest.mark.parametrize("header, abi", [
    (_elf(2, 183), "arm64-v8a"), (_elf(1, 40), "armeabi-v7a"), (_elf(2, 62), "x86_64"),
    (_elf(1, 3), "x86"), (_elf(2, 243), "riscv64"), (_elf(2, 999), None),
    (b"fake libviewspector.so", None), (b"", None),
])
def test_elf_abi_reads_the_header(tmp_path, header, abi):
    path = tmp_path / "lib.so"
    path.write_bytes(header)
    assert inject.elf_abi(str(path)) == abi
    assert inject.elf_abi(str(tmp_path / "missing.so")) is None


@pytest.fixture
def arm64_agent(fake_device, tmp_path):
    (tmp_path / "build-out" / inject.NATIVE_SO_NAME).write_bytes(ARM64_SO)
    return fake_device


def test_a_32_bit_app_is_refused_before_pushing(arm64_agent):
    arm64_agent.apps[PKG].bitness = 32
    with pytest.raises(inject.InjectionError) as err:
        _inject()
    msg = str(err.value)
    assert msg.startswith(f"{inject.NATIVE_SO_NAME} in build-out is built for arm64-v8a, but "
                          f"'{PKG}' (pid {PID}) runs as a 32-bit armeabi-v7a process "
                          "(app_process32)")
    assert "ships only 32-bit native libraries" in err.value.hint
    assert not arm64_agent.pushed and not arm64_agent.attach_calls


def test_an_x86_64_device_is_refused_before_pushing(arm64_agent):
    arm64_agent.abi = "x86_64"
    with pytest.raises(inject.InjectionError, match="runs as a 64-bit x86_64 process"):
        _inject()
    arm64_agent.apps[PKG].bitness = None  # the exe link can't be read: every process is x86
    with pytest.raises(inject.InjectionError) as err:
        _inject()
    assert ("is built for arm64-v8a, but this x86_64 device runs x86_64 / x86 processes"
            in str(err.value))
    assert "native-bridge" in str(err.value)
    assert not arm64_agent.pushed and not arm64_agent.attach_calls


def test_a_matching_or_undecidable_abi_injects(arm64_agent):
    inj = _inject()  # 64-bit app on an arm64 device
    inj.close()
    arm64_agent.restart_app(PKG, new_pid=PID + 1)
    arm64_agent.apps[PKG].bitness = None  # unknown on arm64: could be either, the attach decides
    inj = _inject()
    try:
        assert not inj.warm and len(arm64_agent.attach_calls) == 2
    finally:
        inj.close()


def test_a_library_that_is_not_an_elf_skips_the_check(fake_device):
    inj = _inject()  # the placeholder build-out .so
    inj.close()
    assert not any("readlink" in cmd or "ro.product.cpu.abi" in cmd
                   for cmd in fake_device.shell_log())
