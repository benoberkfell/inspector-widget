"""Capture and walk: snapshot one moment of an app once, then query it offline.

A capture is an immutable record of one app on one device (views + properties,
Compose semantics and slot table, the unified accessibility tree, per-window
screenshots). It is kept in an on-disk store shared by the CLI and the MCP server
and walked with small, budgeted queries (outline, find, node, image, lint, diff)
that never touch the device.

This package root only re-exports the pure data model. Keep it that way: importing
``inspector_widget.capture`` must not import protobuf or Pillow. The submodules
that need them (fetch, index, images) import them themselves.
"""

from __future__ import annotations

from .model import (
    CONF_VALUES,
    ERROR_CODES,
    FACET_STATUS,
    FLAGS,
    KINDS,
    SCHEMA,
    TREE_NAMES,
    CaptureMeta,
    CaptureOptions,
    Index,
    Issue,
    LineageState,
    OpError,
    RawCapture,
    Tree,
    UNode,
    a11y_key,
    a11y_path_key,
    default_store_root,
    index_from_jsonl,
    index_to_jsonl,
    is_capture_id,
    is_key,
    is_ref,
    is_valid_label,
    parse_key,
    ref_num,
    ref_str,
    remap_ids,
    sem_key,
    slot_key,
    view_key,
    window_key,
)

__all__ = [
    "CONF_VALUES", "ERROR_CODES", "FACET_STATUS", "FLAGS", "KINDS", "SCHEMA", "TREE_NAMES",
    "CaptureMeta", "CaptureOptions", "Index", "Issue", "LineageState", "OpError",
    "RawCapture", "Tree", "UNode",
    "a11y_key", "a11y_path_key", "default_store_root", "index_from_jsonl", "index_to_jsonl",
    "is_capture_id", "is_key", "is_ref", "is_valid_label", "parse_key", "ref_num", "ref_str",
    "remap_ids", "sem_key", "slot_key", "view_key", "window_key",
]
