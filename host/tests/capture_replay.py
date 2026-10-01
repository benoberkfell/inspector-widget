"""Real captures, recorded live, replayed over the fake wire (WP L1).

A fixture under ``tests/fixtures/captures/<name>/`` is one published capture as the
store keeps it, trimmed to its source of truth: the agent's protobuf replies
(``raw/<facet>.pb.gz``, gzipped), each window's screenshot as the agent sent it
(``shot/w_<root>.pb``, already deflated, so not gzipped again) and ``meta.json``.
Derived files (index, refmap, images, exports) are left out: they are rebuilt from
these. ``source.json`` says where it came from.

:func:`scene` turns a fixture into a :class:`fakescenes.SceneData`, which answers
Hello, GetWindows, DumpTree (root, properties, resolution stack, screenshot),
GetProperties, Screenshot, DumpCompose (semantics, slot table) and DumpA11y with
the recorded replies, so the real attach, framing, client, fetch, index and query
code runs over it (``capture_harness.harness(scene(name), tmp)``).
:func:`from_capture` wraps it in a :class:`fakeagent.FakeAgent`
(``FakeAgent.from_capture(name)``).

Recording (``python tests/capture_replay.py record STORE_DIR CAPTURE NAME``) copies
a capture out of a store. The fixtures were recorded on emulator-5558 (API 37,
arm64, 1280x2856, 480 dpi, font 1.0) with the agent of improve/capture-wiring after
agent-hardening and a11y-agent-identity (post-ID1 ids, SafeString values):

========================  ===================================================
a11yprobe_launcher        A11yProbe MainActivity: the Compose LazyColumn launcher
a11yprobe_launcher_slots  the same after capture(slots="enable") (slot table)
a11yprobe_viewscreen      ViewScenarioActivity: 47 classic Views, no Compose
a11yprobe_all             MainActivity --es scenario all (every Compose scenario)
a11yprobe_d1              InteropActivity D1: a DialogFragment window over a
                          View screen (two windows, per-window screenshots)
a11yprobe_s1_a / _b       InteropActivity S1 (RecyclerView of ComposeView
                          cells) before and after a 1,100 px fling: recycled
                          cells come back rebound
thunderbird_list_views    Thunderbird (net.thunderbird.android.debug, Compose
                          1.12.1) message list, classic View rows, offline demo
                          mailbox (demo@example.com: synthetic messages)
thunderbird_list_compose  the same list with the debug feature flag
                          use_compose_for_message_list_items: ComposeView rows
nia_foryou                Now in Android (demo debug, Compose 1.10) For you:
                          the topic picker (a horizontally scrolling grid)
nia_settings              Now in Android's Settings dialog: a Compose Dialog
                          window over For you (two windows)
========================  ===================================================
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import sys
from collections.abc import Mapping
from typing import Any

import fakescenes

from inspector_widget.proto import view_inspection_pb2 as pb

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "captures")
#: The facet replies a fixture may hold (raw/<name>.pb.gz), as the store names them.
FACETS = ("windows", "views", "compose_sem", "slots", "a11y", "a11y_render")
#: meta.json keys a fixture keeps (the rest is store bookkeeping).
META_KEYS = ("id", "lineage", "pid", "api", "abi", "agent_version", "device", "created_at",
             "took_ms", "options", "facets", "fingerprint", "consistency",
             "compose_generation", "label", "diagnostics", "schema")


def names() -> list[str]:
    """Every recorded fixture, sorted."""
    if not os.path.isdir(FIXTURES):
        return []
    return sorted(d for d in os.listdir(FIXTURES)
                  if os.path.isfile(os.path.join(FIXTURES, d, "meta.json")))


def path_of(name: str) -> str:
    return name if os.path.isdir(name) else os.path.join(FIXTURES, name)


class Recorded:
    """One fixture's files: ``meta`` (dict), ``raw[facet]`` (bytes), ``shots[root]``."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.name = os.path.basename(os.path.normpath(path))
        with open(os.path.join(path, "meta.json"), encoding="utf-8") as f:
            self.meta: dict[str, Any] = json.load(f)
        self.raw: dict[str, bytes] = {}
        for facet in FACETS:
            p = os.path.join(path, "raw", f"{facet}.pb.gz")
            if os.path.isfile(p):
                with gzip.open(p, "rb") as f:
                    self.raw[facet] = f.read()
        self.shots: dict[int, bytes] = {}
        shot_dir = os.path.join(path, "shot")
        for fn in sorted(os.listdir(shot_dir)) if os.path.isdir(shot_dir) else ():
            if fn.startswith("w_") and fn.endswith(".pb"):
                with open(os.path.join(shot_dir, fn), "rb") as f:
                    self.shots[int(fn[2:-3])] = f.read()
        src = os.path.join(path, "source.json")
        self.source: dict[str, Any] = {}
        if os.path.isfile(src):
            with open(src, encoding="utf-8") as f:
                self.source = json.load(f)

    @property
    def serial(self) -> str:
        return str(self.meta["lineage"]["serial"])

    @property
    def package(self) -> str:
        return str(self.meta["lineage"]["package"])

    @property
    def pid(self) -> int:
        return int(self.meta.get("pid") or 4242)

    @property
    def dpi(self) -> int:
        return int((self.meta.get("device") or {}).get("dpi") or 420)

    @property
    def font_scale(self) -> float:
        return float((self.meta.get("device") or {}).get("font_scale") or 1.0)

    def window_ids(self) -> list[int]:
        data = self.raw.get("windows")
        if data:
            return [int(r) for r in pb.GetWindowsResponse.FromString(data).root_ids]
        return [int(r.id) for r in pb.DumpTreeResponse.FromString(self.raw["views"]).roots]


def load(name: str) -> Recorded:
    return Recorded(path_of(name))


def scene(name: str | Recorded) -> fakescenes.SceneData:
    """The fixture as a SceneData whose replies are the recorded ones."""
    rec = name if isinstance(name, Recorded) else load(name)
    views = pb.DumpTreeResponse.FromString(rec.raw["views"])
    views.ClearField("screenshot")  # the store keeps it in shot/; served per request
    a11y = pb.DumpA11yResponse.FromString(rec.raw["a11y"])
    sem = (pb.DumpComposeResponse.FromString(rec.raw["compose_sem"])
           if rec.raw.get("compose_sem") else None)
    slots = pb.DumpComposeResponse.FromString(rec.raw["slots"]) if rec.raw.get("slots") else None
    screens = {root: _shot_maker(data) for root, data in rec.shots.items()}
    return fakescenes.SceneData(
        rec.name, views=views, a11y=a11y, compose_sem=sem, compose_slots=slots,
        screens=screens, window_ids=rec.window_ids(), api_level=int(rec.meta.get("api") or 37),
        abi=str(rec.meta.get("abi") or "arm64-v8a"), slots_populated=slots is not None)


def _shot_maker(data: bytes):
    def make(_scale: float) -> pb.Screenshot:
        # The recorded pixels, whatever scale is asked for: the message carries the
        # scale it was taken at, which is what the host crops by.
        return pb.Screenshot.FromString(data)
    return make


def from_capture(name: str | Recorded, **kw: Any):
    """A FakeAgent serving the recorded capture (``FakeAgent.from_capture``)."""
    import fakeagent

    sc = scene(name)
    return fakeagent.FakeAgent(behaviour=sc.behaviour, **kw)


def replay_device(dev: Any, rec: Recorded) -> None:
    """Make a harness FakeDevice look like the recording device: the recorded app
    (package and pid) is running, and density and font scale are the recorded
    ones (lint measures touch targets in dp)."""
    if rec.package not in getattr(dev, "apps", {}):
        dev.add_app(rec.package, rec.pid)
    dev.physical_density, dev.override_density = rec.dpi, None
    dev.font_scale = f"{rec.font_scale}"


# --------------------------------------------------------------------------- #
# Recording
# --------------------------------------------------------------------------- #
def record(capture_dir: str, name: str, out_root: str = FIXTURES,
           source: Mapping[str, Any] | None = None) -> str:
    """Copy one published capture (``<store>/captures/<id>``) into a fixture."""
    out = os.path.join(out_root, name)
    if os.path.isdir(out):
        shutil.rmtree(out)
    os.makedirs(os.path.join(out, "raw"))
    for facet in FACETS:
        src = os.path.join(capture_dir, "raw", f"{facet}.pb")
        if os.path.isfile(src):
            with open(src, "rb") as f:
                data = f.read()
            with open(os.path.join(out, "raw", f"{facet}.pb.gz"), "wb") as f:
                # mtime=0: the same capture always gives the same bytes
                f.write(gzip.compress(data, compresslevel=9, mtime=0))
    shot_src = os.path.join(capture_dir, "shot")
    if os.path.isdir(shot_src):
        os.makedirs(os.path.join(out, "shot"))
        for fn in sorted(os.listdir(shot_src)):
            if fn.startswith("w_") and fn.endswith(".pb"):
                shutil.copyfile(os.path.join(shot_src, fn), os.path.join(out, "shot", fn))
    with open(os.path.join(capture_dir, "meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    meta = {k: meta[k] for k in META_KEYS if k in meta}
    with open(os.path.join(out, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=1, sort_keys=True)
        f.write("\n")
    if source:
        with open(os.path.join(out, "source.json"), "w", encoding="utf-8") as f:
            json.dump(dict(source), f, indent=1, sort_keys=True)
            f.write("\n")
    return out


if __name__ == "__main__":  # pragma: no cover - a recording helper
    if len(sys.argv) >= 5 and sys.argv[1] == "record":
        store, cid, name = sys.argv[2:5]
        extra = json.loads(sys.argv[5]) if len(sys.argv) > 5 else {}
        print(record(os.path.join(store, "captures", cid), name, source=extra))
    else:
        print(__doc__)
        print("usage: capture_replay.py record STORE_DIR CAPTURE_ID NAME [SOURCE_JSON]")
        sys.exit(2)
