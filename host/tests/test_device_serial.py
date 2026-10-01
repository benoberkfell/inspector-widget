"""Which device the device tests target (offline: the environment is monkeypatched).

``conftest.device_serial()`` is ``$ANDROID_SERIAL`` (what adb itself honours), else
emulator-5554; ``device_present()`` and the smoke test use it, and the TalkBack corpus test
stays opt-in through ``INSPECTOR_WIDGET_TB_DEVICE`` (a serial, or 1 for ``device_serial()``).
"""

from __future__ import annotations

import subprocess

import pytest

import conftest


def test_device_serial_is_android_serial_else_emulator_5554(monkeypatch):
    monkeypatch.delenv("ANDROID_SERIAL", raising=False)
    assert conftest.device_serial() == "emulator-5554"
    monkeypatch.setenv("ANDROID_SERIAL", " emulator-5556 ")
    assert conftest.device_serial() == "emulator-5556"
    monkeypatch.setenv("ANDROID_SERIAL", "")
    assert conftest.device_serial() == "emulator-5554"


def test_device_present_checks_android_serial_by_default(monkeypatch):
    listing = "List of devices attached\nemulator-5554\tdevice\nemulator-5556\tdevice\n"
    monkeypatch.setattr(conftest.shutil, "which", lambda _name: "/usr/bin/adb")
    monkeypatch.setattr(conftest.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, listing, ""))
    monkeypatch.setenv("ANDROID_SERIAL", "emulator-5556")
    assert conftest.device_present()
    monkeypatch.setenv("ANDROID_SERIAL", "emulator-5558")
    assert not conftest.device_present()
    assert conftest.device_present("emulator-5554")


def test_the_smoke_test_targets_android_serial(monkeypatch):
    import test_smoke_emulator as smoke

    seen = []
    monkeypatch.setenv("ANDROID_SERIAL", "emulator-5556")
    monkeypatch.setattr(smoke, "device_present", lambda serial: seen.append(serial) or False)
    fixture = smoke.session.__wrapped__()
    with pytest.raises(pytest.skip.Exception, match="emulator-5556"):
        next(fixture)
    assert seen == ["emulator-5556"]


def test_the_talkback_corpus_test_stays_opt_in(monkeypatch):
    monkeypatch.setenv("ANDROID_SERIAL", "emulator-5556")
    monkeypatch.delenv("INSPECTOR_WIDGET_TB_DEVICE", raising=False)
    assert conftest.tb_device_serial() is None  # TalkBack is device-wide: never by default
    monkeypatch.setenv("INSPECTOR_WIDGET_TB_DEVICE", "1")
    assert conftest.tb_device_serial() == "emulator-5556"
    monkeypatch.setenv("INSPECTOR_WIDGET_TB_DEVICE", "emulator-5558")
    assert conftest.tb_device_serial() == "emulator-5558"
    monkeypatch.setenv("INSPECTOR_WIDGET_TB_DEVICE", "0")
    assert conftest.tb_device_serial() is None
