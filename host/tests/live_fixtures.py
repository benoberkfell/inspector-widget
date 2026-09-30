"""Real tool outputs recorded live, checked in as compact gzipped JSON.

Provenance: recorded 2026-09-30 on ``emulator-5554`` (API 37, arm64, 1280x2856,
480 dpi) with the bundled ``com.oberkfell.a11yprobe`` app, from ``main`` at 2b002f8.
These are **pre-ID1** captures (before improve/a11y-agent-identity). Every Compose
a11y node reports ``host_view_id=1, virtual_id=11``, and inspect's a11y joins are
mostly ``overlap``. Treat that as data, not as a bug in the fixture.

``launcher/`` is ``MainActivity``: the Compose launcher list, 9 Views, 17 semantics
nodes, 399 compose nodes with the slot table populated, and 40 a11y nodes. These are
MCP ``_run_tool`` results, so they have ``serial`` and ``package``:

* ``views`` is ``dump_tree`` with default args. It uses the legacy MCP shape: flat
  bounds, ``resource.ref``, and E3 (GRAVITY/INT_FLAG labels lost).
* ``views_props`` is ``dump_tree(include_properties, include_resolution_stack,
  include_screenshot, scale=0.5)``.
* ``views_cli`` is CLI ``dump --json`` (strings.py shape, no properties).
* ``compose_slots`` is ``dump_compose()`` (slot table populated). ``compose_sem``
  is ``dump_compose(include_slot_table=false)``.
* ``a11y`` is ``dump_accessibility()``. ``a11y_lint`` is ``a11y_lint()``, with 14
  findings.
* ``inspect`` is ``inspect()``. ``inspect_props`` is ``inspect(include_properties,
  include_overlay)``.
* ``get_properties`` is ``get_properties(view_id=82, include_resolution_stack)``.
  ``inspect_node`` is ``inspect_node(view_id=82)``.
* ``compose_overlay`` is ``compose_overlay(scale=0.5)``.

``viewscreen/`` is ``ViewScenarioActivity``: a classic View screen with 40 Views.
These are CLI ``--json`` outputs in the strings.py shapes:

* ``views_props`` is ``dump --properties --json``.
* ``a11y`` is ``a11y --json``.
* ``inspect`` is ``inspect --json``.
* ``get_properties`` is ``get-properties --view-id 13 --json``.

Each screen also has ``screen.png``, a full-resolution 1280x2856 RGBA image.
The launcher's is the adb ``screencap`` of the same moment (``live/ref_main.png``).
The View screen's is the agent's own screenshot (``scen_f/shot.png``).
``fakescenes.replay_scene`` serves them as agent Screenshots.

The original files are ``scratchpad/live/{mcp_phase2_deps,scen_f}/*.json`` and
``scratchpad/live/dump0.json``. The only change is compact re-encoding plus gzip.
"""

from __future__ import annotations

import gzip
import json
import os
from typing import Any

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "live")
SCREENS = ("launcher", "viewscreen")


def path(screen: str, name: str) -> str:
    """Path of a fixture file; JSON fixtures are ``<name>.json.gz``."""
    p = os.path.join(FIXTURE_DIR, screen, name)
    if os.path.exists(p):
        return p
    return p + ".json.gz"


def load(screen: str, name: str) -> Any:
    """A JSON fixture, decoded (a fresh object on every call)."""
    with gzip.open(path(screen, name), "rt", encoding="utf-8") as f:
        return json.load(f)


def names(screen: str) -> list:
    return sorted(n[: -len(".json.gz")] for n in os.listdir(os.path.join(FIXTURE_DIR, screen))
                  if n.endswith(".json.gz"))
