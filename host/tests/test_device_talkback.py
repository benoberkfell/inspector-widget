"""The TalkBack corpus on a live device (opt-in: it turns TalkBack on device-wide).

    INSPECTOR_WIDGET_TB_DEVICE=emulator-5556 \\
        pytest host/tests/test_device_talkback.py -m "device and talkback" -v

For every scenario/variant in tests/data/tb_corpus_expected.json (the
A11yProbe TalkBack corpus, docs/design/talkback-navigation.md part 5) it
launches the screen, drives the real TalkBack and checks:

* BAD variants raise their expected tb.* findings from the walk AND from the
  model alone (walk.static_walk over the dump the walk started from);
* GOOD variants end the way the entry says (a full lap: wrap) with no findings,
  from the walk or the model;
* the utterances hold the entry's expected speech (TalkBack's own, from its log);
* model.mismatch is empty, or exactly the pinned calibration delta;
* scenario entries (focus_after / restore / survive) reach their verdict.

TalkBack is turned on once for the module, with its verbose log so walks read
the real announcements, and restored (settings and log level) at the end.
INSPECTOR_WIDGET_TB_RECORD=<dir> also writes every walk and the dump it
started from there, to refresh the offline fixtures in tests/data/tb_walks/.
The corpus APK must be installed (scripts/install-a11yprobe.sh SERIAL --no-launch) and the
agent built (./scripts/build.sh, or INSPECTOR_WIDGET_ARTIFACTS). About 13 minutes.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = [pytest.mark.device, pytest.mark.talkback]

DATA = Path(__file__).parent / "data"
EXPECTED = json.loads((DATA / "tb_corpus_expected.json").read_text())
SERIAL = os.environ.get("INSPECTOR_WIDGET_TB_DEVICE")
RECORD = os.environ.get("INSPECTOR_WIDGET_TB_RECORD")
PKG = "com.oberkfell.a11yprobe"


def _entry_id(e):
    return f"{e['scenario']}-{e['variant']}-{e.get('kind', 'walk')}"


@pytest.fixture(scope="module")
def talkback():
    if not SERIAL:
        pytest.skip("opt-in: set INSPECTOR_WIDGET_TB_DEVICE=<serial> (TalkBack is device-wide)")
    if shutil.which("adb") is None:
        pytest.skip("adb not on PATH")
    from inspector_widget.talkback import device
    device.action(SERIAL, "on", package=None, verbose_log=True)
    try:
        yield SERIAL
    finally:
        device.action(SERIAL, "restore")


def launch(scenario: str, variant: str) -> None:
    comp = (".TbViewActivity" if scenario.startswith("tb_v")
            else ".InteropActivity" if scenario.startswith("tb_h") else ".MainActivity")
    subprocess.run(["adb", "-s", SERIAL, "shell", "am", "start", "-S", "-W", "-n", f"{PKG}/{comp}",
                    "--es", "scenario", scenario, "--es", "variant", variant],
                   capture_output=True, check=True)
    time.sleep(2.5)


def _codes(findings):
    return sorted({f["code"] for f in findings or []})


def _record(entry, result):
    """Copy the full walk record (and its start dump) for the offline fixtures; the
    record names the dump beside it, not the cache path it was saved under."""
    if not RECORD or not result.get("saved"):
        return
    base = Path(RECORD) / _entry_id(entry)
    base.parent.mkdir(parents=True, exist_ok=True)
    rec = json.loads(Path(result["saved"]).read_text())
    if rec.get("dump"):
        with open(rec["dump"], "rb") as src, gzip.open(f"{base}.a11y.pb.gz", "wb") as dst:
            dst.write(src.read())
        rec["dump"] = f"{base.name}.a11y.pb.gz"
    with gzip.open(f"{base}.json.gz", "wt") as dst:
        json.dump(rec, dst)


@pytest.mark.parametrize("entry", [e for e in EXPECTED["entries"] if e.get("kind", "walk") == "walk"],
                         ids=_entry_id)
def test_corpus_walk(talkback, entry):
    import inspector_widget as iw
    from inspector_widget.proto import view_inspection_pb2 as pb
    from inspector_widget.talkback import walk

    launch(entry["scenario"], entry["variant"])
    with iw.attach(SERIAL, PKG) as session:
        res = walk.run_walk(session, **entry.get("walk", {}))
    _record(entry, res)
    rec = json.loads(Path(res["saved"]).read_text())
    got = _codes(rec["findings"])
    want = entry.get("expected_findings") or []
    exp = entry.get("walk_expect") or {}
    if exp.get("ended"):
        assert rec["ended"] == exp["ended"], res
    if entry["variant"] == "good" and not want:
        assert [c for c in got if c != "model.mismatch"] == [], res
    else:
        assert set(want) <= set(got), res
    pinned = entry.get("model_mismatch")
    mism = [f for f in rec["findings"] if f["code"] == "model.mismatch"]
    if pinned is None:
        assert mism == [], (entry["scenario"], entry["variant"], mism)
    else:
        assert rec["vs_model"].get("differ", 0) <= pinned.get("differ", 0), rec["vs_model"]
    if entry.get("expected_stops"):
        labels = [s.get("label") or s.get("speak") for s in rec["steps"] if s.get("moved") and s.get("key")]
        for want_label in entry["expected_stops"]:
            assert any(want_label.lower() in (lab or "").lower() for lab in labels), (want_label, labels)
    said = " | ".join(s.get("speak") or "" for s in rec["steps"] if s.get("moved"))
    for words in entry.get("expected_speech") or []:
        assert words in said, (words, said)
    static = entry.get("static_findings")
    if static is not None and rec.get("dump"):
        resp = pb.DumpA11yResponse()
        resp.ParseFromString(Path(rec["dump"]).read_bytes())
        model_codes = _codes(walk.static_walk(resp, expect=entry.get("walk", {}).get("expect"))["findings"])
        assert set(static) <= set(model_codes), (static, model_codes)
        if entry["variant"] == "good" and not static:
            assert model_codes == [], model_codes


@pytest.mark.parametrize("entry", [e for e in EXPECTED["entries"] if e.get("kind", "walk") != "walk"],
                         ids=_entry_id)
def test_corpus_scenario(talkback, entry):
    import inspector_widget as iw
    from inspector_widget.talkback import scenarios

    launch(entry["scenario"], entry["variant"])
    with iw.attach(SERIAL, PKG) as session:
        res = scenarios.run_scenario(session, entry["kind"], **entry.get("args", {}))
    assert res.get("verdict") in entry["expect_verdict"], res
    want = entry.get("expected_findings") or []
    got = [res["finding"]["code"]] if res.get("finding") else []
    assert got == want, res
