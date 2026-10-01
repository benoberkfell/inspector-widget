"""What the device redaction checks assert, as pure functions over shaped dicts.

``test_device_redaction.py`` runs these against a live A11yProbe; ``test_redaction_checks.py``
runs them offline (synthetic dicts, and events carried through the fake agent's wire). Every
check returns a list of problems (empty = pass), each naming the condition that failed and
the texts involved, so a failing device run says what went wrong without a re-run.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional, Tuple

MASK = "•"
TEXT_CHANGED = "VIEW_TEXT_CHANGED"
FOCUSED = "VIEW_FOCUSED"

Source = Tuple[int, int]  # (host_view_id, virtual_id); virtual_id -1 = the View itself


def is_masked(text: Optional[str]) -> bool:
    """One or more mask dots and nothing else."""
    return bool(text) and set(text) == {MASK}


def leaks(secrets: Iterable[str], **dumps: Any) -> List[str]:
    """``"<dump>: '<secret>'"`` for every secret that appears anywhere in a dump (as JSON, so
    nested values and dict keys count)."""
    secrets = [s for s in secrets if s]
    out = []
    for name, data in dumps.items():
        blob = data if isinstance(data, str) else json.dumps(data, default=str, ensure_ascii=False)
        out += [f"{name}: {s!r}" for s in secrets if s in blob]
    return out


def from_source(events: Iterable[Dict[str, Any]], source: Source) -> List[Dict[str, Any]]:
    """The events whose source is ``source``."""
    host, vid = source
    return [e for e in events if e.get("host_view_id") == host and e.get("virtual_id", -1) == vid]


def of_type(events: Iterable[Dict[str, Any]], type_: str) -> List[Dict[str, Any]]:
    return [e for e in events if e.get("type") == type_]


def event_problems(events: List[Dict[str, Any]], source: Source, label: str,
                   min_text_changes: int = 1) -> List[str]:
    """Problems with the events of a password field ``source``: fewer than
    ``min_text_changes`` VIEW_TEXT_CHANGED events from it, or any text / pane title of its
    events that is not all mask dots."""
    mine = from_source(events, source)
    out = []
    changed = of_type(mine, TEXT_CHANGED)
    if len(changed) < min_text_changes:
        out.append(f"{label}: {len(changed)} VIEW_TEXT_CHANGED event(s) from "
                   f"{source}, want >= {min_text_changes}")
    for e in mine:
        for key in ("text", "pane_title"):
            v = e.get(key)
            if v and not is_masked(v):
                out.append(f"{label}: {e.get('type')} seq={e.get('seq')} {key} not masked: {v!r}")
    return out


def field_problems(events: List[Dict[str, Any]], sources: List[Source], label: str) -> List[str]:
    """:func:`event_problems` for a field typed into under one or more identities, the last
    current (its activity can be recreated between attempts): text changes from the last,
    every text masked from all of them. No identity at all is a problem too."""
    if not sources:
        return [f"{label}: the field was never found"]
    out = event_problems(events, sources[-1], label)
    for s in dict.fromkeys(sources):
        if s != sources[-1]:
            out += event_problems(events, s, label, min_text_changes=0)
    return out


def summarize(events: Iterable[Dict[str, Any]], limit: int = 60) -> str:
    """One line per event (the newest ``limit``): seq, type, node key and text."""
    lines = [f"  {e.get('seq')} {e.get('type')} {e.get('node_key')} "
             f"text={e.get('text')!r}" + (f" pane={e['pane_title']!r}" if e.get("pane_title") else "")
             for e in events]
    if len(lines) > limit:
        lines = [f"  ... {len(lines) - limit} earlier event(s)"] + lines[-limit:]
    return "\n".join(lines) or "  (no events)"


def report(problems: List[str], events: Iterable[Dict[str, Any]] = (),
           steps: Iterable[str] = ()) -> str:
    """An assertion message: the problems, then the recorded events and the steps taken."""
    return ("\n".join(problems) + "\nevents:\n" + summarize(events)
            + "\nsteps:\n" + ("\n".join(f"  {s}" for s in steps) or "  (none)"))
