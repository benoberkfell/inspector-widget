"""The result documents of the legacy tools, built once for both surfaces.

``mcp_server`` returns these dicts from its tools, and the CLI builds the same
ones for its ``--json`` output, so that after ``output.slim`` / ``output.finalize``
the two surfaces print the same bytes for the same arguments (Phase 0, spec
section 2.3). Each function takes data already shaped by ``strings``, ``a11y``,
``a11y_lint`` or ``correlate`` and only adds the target (``serial``, ``package``)
and the fields a tool always reports. Key order matters: it is the output order.

No protobuf and no device I/O here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def dump_tree(data: Mapping[str, Any], serial: str, package: str,
              include_properties: bool = False) -> dict[str, Any]:
    """``dump_tree``: ``strings.dump_tree_to_dict`` roots (and properties keyed by
    view id when asked for) under the target. The screenshot, when asked for, is
    added by the caller (the MCP saves a PNG; the CLI writes ``--screenshot``)."""
    roots = list(data.get("roots") or [])
    out: dict[str, Any] = {"serial": serial, "package": package, "roots": roots,
                           "root_count": len(roots)}
    if include_properties:
        out["properties"] = dict(data.get("properties") or {})
    return out


def get_properties(group: Mapping[str, Any], serial: str, package: str) -> dict[str, Any]:
    """``get_properties``: one ``strings.property_group_to_dict`` group."""
    return {"serial": serial, "package": package, "view_id": group.get("view_id"),
            "group": dict(group)}


def with_target(data: Mapping[str, Any], serial: str, package: str,
                **extra: Any) -> dict[str, Any]:
    """``data`` followed by ``serial``, ``package`` and ``extra`` (dump_compose,
    dump_accessibility, inspect_node and component_image)."""
    out = dict(data)
    out.update(serial=serial, package=package)
    out.update(extra)
    return out


def slot_table_note(flag: str, warning: str) -> str:
    """dump_compose's note when the slot table is empty: how to populate it,
    naming the surface's own ``flag``, and ``warning`` (strings.
    ENABLE_INSPECTION_WARNING, a ``%s`` template for the flag)."""
    return (f"slot table not populated (semantics only). Pass {flag} for composable "
            f"names/params/file:line. WARNING: " + warning % flag)


def a11y_lint(report: Mapping[str, Any], serial: str, package: str) -> dict[str, Any]:
    """``a11y_lint``: ``LintReport.to_dict()`` plus whether contrast was sampled."""
    stats = report.get("stats") or {}
    return with_target(report, serial, package,
                       contrast_sampled=bool(stats.get("contrast_windows")))


def inspect(merged: Mapping[str, Any], serial: str, package: str) -> dict[str, Any]:
    """``inspect``: the merged tree of ``correlate.inspect_tree``."""
    return {"serial": serial, "package": package, "roots": merged.get("roots", []),
            "summary": merged.get("summary", {}), "sources": merged.get("sources", {})}


__all__ = ["a11y_lint", "dump_tree", "get_properties", "inspect", "slot_table_note",
           "with_target"]
