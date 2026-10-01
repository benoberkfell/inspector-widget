"""One registry for the capture-and-walk tools on both surfaces (WP S2).

Each :class:`ToolSpec` names a tool, its parameters and the :mod:`.ops` function
behind it. From the same specs this module generates

* the MCP tool entries (:func:`mcp_entries`: description, JSON schema,
  annotations and a handler, in ``mcp_server.TOOLS``' format), and
* the CLI subcommands (:func:`add_cli`: kebab-case flags with the same names
  and defaults, positionals, and a human renderer; ``--json`` prints exactly
  the MCP text).

Arguments are validated once, here (:func:`validate`), the same way for every
transport: an unknown name, a wrong type, a value outside an enum or a range
is ``bad_args``. Toolsets (``INSPECTOR_WIDGET_TOOLSET``) choose what the MCP
server lists; a tool it does not list is still callable by name.

Spec: docs/design/capture-and-walk.md sections 5, 5.12, 5.13 and 10.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from . import ops
from .capture.model import OpError
from .output import dumps

# --------------------------------------------------------------------------- #
# Toolsets (spec 5.12)
# --------------------------------------------------------------------------- #
ENV_TOOLSET = "INSPECTOR_WIDGET_TOOLSET"

#: The four session tools every toolset shares (spec 5.2).
SESSION_TOOLS = ("list_devices", "list_processes", "attach", "detach")
#: The 15 inspection tools from before capture-and-walk.
LEGACY_TOOLS = SESSION_TOOLS + (
    "dump_tree", "get_properties", "screenshot", "dump_compose", "compose_overlay",
    "dump_accessibility", "a11y_lint", "a11y_overlay", "inspect", "inspect_node",
    "component_image")
#: The TalkBack tools (docs/design/talkback-navigation.md part 4).
TALKBACK_TOOLS = ops.TB_TOOL_NAMES
CAPTURE_TOOLS = ops.TOOL_NAMES

TOOLSETS: dict[str, tuple[str, ...]] = {
    "legacy": LEGACY_TOOLS,
    "capture": SESSION_TOOLS + CAPTURE_TOOLS,
    "talkback": SESSION_TOOLS + TALKBACK_TOOLS,
    "all": LEGACY_TOOLS + CAPTURE_TOOLS + TALKBACK_TOOLS,
}
#: Until the deliberate flip (WP S4) the MCP server lists what it listed before
#: the capture tools existed: the 15 legacy tools and the TalkBack tools.
DEFAULT_TOOLSET = "legacy,talkback"


def legacy_talkback(listed: Iterable[str]) -> bool:
    """Whether a listing shows the TalkBack tools in their pre-capture shape: the
    legacy inspection tools are listed and the capture tools are not (the default
    toolset, byte for byte as before). The calls run the same implementation
    (ops.talkback / tb_walk / tb_scenario) either way."""
    names = set(listed)
    return "dump_accessibility" in names and "capture" not in names


def toolset_names(value: str | None = None, env: Mapping[str, str] | None = None
                  ) -> tuple[str, ...]:
    """The tool names a toolset spec lists, in order. ``value`` (else
    ``$INSPECTOR_WIDGET_TOOLSET``, else the default) is a name or a comma list
    of names (``capture,talkback``). An unknown name raises ValueError."""
    env = os.environ if env is None else env
    spec = value if value is not None else (env.get(ENV_TOOLSET) or DEFAULT_TOOLSET)
    parts = [p.strip().lower() for p in str(spec).split(",") if p.strip()]
    if not parts:
        parts = DEFAULT_TOOLSET.split(",")
    names: list[str] = []
    for part in parts:
        if part not in TOOLSETS:
            raise ValueError(f"unknown toolset {part!r} in {ENV_TOOLSET} "
                             f"(one or more of: {', '.join(TOOLSETS)})")
        names.extend(n for n in TOOLSETS[part] if n not in names)
    return tuple(names)


def active_toolset(env: Mapping[str, str] | None = None) -> str:
    """The toolset spec in effect (for --self-check)."""
    env = os.environ if env is None else env
    return (env.get(ENV_TOOLSET) or DEFAULT_TOOLSET).strip()


# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #
@dataclass
class Param:
    """One tool parameter, on both surfaces.

    ``type`` is a JSON-schema type name, ``"string|array"`` for a value that
    is a word or a list (``props``, ``marks``, ``include``), or
    ``"string|integer"`` for a selector that may also be a number (find's
    ``window``: a z index). ``items`` is the
    item type of an array. ``cli`` adds option strings (aliases such as ``-c``)
    beside the canonical ``--kebab-name``; ``positional`` makes the CLI take it
    as a positional (``nargs``). ``surfaces`` says where it exists: a
    transport-only parameter is allow-listed (image's ``inline`` on MCP, its
    ``out`` on the CLI). ``cli_choices`` maps extra CLI words onto enum values
    (``ls`` -> ``list``)."""

    name: str
    type: str
    default: Any = None
    enum: tuple | None = None
    minimum: float | None = None
    maximum: float | None = None
    exclusive_minimum: float | None = None
    help: str = ""
    cli: tuple[str, ...] = ()
    positional: bool = False
    nargs: str | None = None
    items: str | None = None
    surfaces: tuple[str, ...] = ("mcp", "cli")
    cli_choices: Mapping[str, str] | None = None
    keep_words: tuple[str, ...] = ()   # string|array: words that stay a string on the CLI
    check: Callable[[Any], Any] | None = None
    #: CLI flags that set this parameter to a value (``{"--prev": "prev"}``).
    cli_set: Mapping[str, Any] | None = None
    #: An array the CLI flag takes repeatedly as well as comma-separated.
    repeat: bool = False

    @property
    def flag(self) -> str:
        return "--" + self.name.replace("_", "-")


@dataclass
class ToolSpec:
    """A tool: MCP ``name``, CLI ``cli_name``, a one-line ``summary`` (CLI help),
    the MCP ``description``, its params, the ops function and the toolsets it
    belongs to."""

    name: str
    cli_name: str
    summary: str
    params: list[Param]
    fn: Callable[..., dict[str, Any]]
    read_only: bool
    toolsets: set[str]
    description: str = ""
    render: Callable[[dict[str, Any]], list[str]] | None = None

    def param(self, name: str) -> Param | None:
        return next((p for p in self.params if p.name == name), None)

    def params_for(self, surface: str) -> list[Param]:
        return [p for p in self.params if surface in p.surfaces]

    @property
    def device_wide(self) -> bool:
        return self.name in TALKBACK_TOOLS


def _rules_check(v: Any) -> Any:
    from .capture import rules as R
    R.resolve(v)  # raises OpError("bad_args") naming the valid ids
    return v


# ---- shared params ------------------------------------------------------------ #
# Parameter descriptions are spent once (tools/list for the capture toolset stays
# within 12,000 B): serial/package on capture, the capture spec and fields on
# outline; the instructions say the rest.
def _serial(doc: bool = False) -> Param:
    return Param("serial", "string", cli=("-s",),
                 help="Default: last session, $ANDROID_SERIAL, the only device" if doc else "")


def _package(doc: bool = False) -> Param:
    return Param("package", "string", cli=("-p",),
                 help="Default: last session, the only debuggable app running" if doc else "")


def _capture(doc: bool = False) -> Param:
    return Param("capture", "string", "latest", cli=("-c",),
                 help="id, label, latest, prev or latest~N" if doc else "")


def _cursor() -> Param:
    return Param("cursor", "string")


def _max_bytes(default: int | None) -> Param:
    return Param("max_bytes", "integer", default, maximum=32000)  # 0 or less: 32000


def _fields(doc: bool = False) -> Param:
    return Param("fields", "string",
                 help="+src,+dp,+ids,+conf,+sel,+visible,+props:a,b,+params:a,b; -bounds"
                 if doc else "")


def _build_out() -> Param:
    """CLI only: the MCP server takes $INSPECTOR_WIDGET_ARTIFACTS."""
    return Param("build_out", "string", surfaces=("cli",), cli=(),
                 help="directory with the on-device artifacts (default: "
                      "$INSPECTOR_WIDGET_ARTIFACTS, else the checkout's build-out/)")


def _format() -> Param:
    return Param("format", "string", "lines", enum=("lines", "json"))


# ---- descriptions (tools/list for the capture toolset stays <= 12,000 B) ------- #
GRAMMAR = ('Line: ref Type #rid @tag "label" flags [x,y wxh] !issue +N(hidden) tail; '
           'indent 2/level; screen px.')

D_CAPTURE = (
    "Snapshot the app ONCE (views, properties, Compose, accessibility, screenshots, lint) "
    "into the store; returns its id (c7h2kq), lint, issues, an outline preview. Query "
    "it with outline, find, node, image, lint, diff (no device I/O); refs (n23) carry across "
    "captures. Recapture after the UI changes; diff_from=\"prev\" adds what changed.")
D_CAPTURES = ("Manage stored captures (what=\"walks\": TalkBack walks, ids w...); label "
              "takes id+label; export writes files, returns paths; gc all=true wipes all.")
D_OUTLINE = ("Tree of a capture, one line per node. view: ui (Views+Compose+a11y merged), "
             "views, compose, slots (composables, src=File.kt:line), a11y, reading (TalkBack's "
             "stops; explain=true: its words, why=, via=; include_skipped: - lines "
             "merged_into=/hidden_by=/why=). Semantic detail collapses wrappers. " + GRAMMAR)
D_FIND = ("Find nodes in a capture; filters are ANDed. text: substring of label/text/desc/"
          "state/hint; type/rid/tag/src: globs; flags: all of; issue: rule, code or severity; "
          "within: a selector; at: [x,y]; min_dp/max_dp: touch (a11y) size.")
D_NODE = ("Everything about one node (refs: up to 10): ids, bounds, tap_xy, layout/clip, "
          "a11y, compose (slots, file:line), issues, props, parent; facets=\"tb\": "
          "TalkBack (why, speech, prev/next). ref: n23, a key (view:12), a point x,y, or "
          "#rid, @tag, Type\"label\" joined by ' > ' (direct child).")
D_IMAGE = ("PNG: a node's crop of its window's screenshot, or an overlay (marks: boxes by "
           "ref; walk: a tb_walk's steps). Returns the path.")
D_LINT = ("Accessibility lint (R1..R18) of a capture grouped by rule, with fixes; "
          "rules=[\"tb\"]: TalkBack navigation; [\"render.\"]: clipped, hidden, offscreen. "
          "contrast=true samples the screenshot (~4s, cached).")
D_DIFF = ("Compare two captures of one app by ref: changed, moved, added, removed, "
          "rebound; issue deltas; \"new screen\" when little is shared.")

#: The TalkBack debugging loop, in the instructions and tb_walk's description.
TB_LOOP = ('capture -> lint(rules=["tb"]) -> outline(view="reading",explain=true) -> '
           'node(ref,facets="tb") -> tb_walk(start=ref) -> image(overlay="walk")')
_DEVICE_WIDE = "DEVICE-WIDE: "
D_TALKBACK = (_DEVICE_WIDE + "TalkBack status (read-only) | on | off | restore. on snapshots "
              "the accessibility settings first; restore (also at exit) writes them back.")
_TB_WALK_CORE = (_DEVICE_WIDE + "drives the REAL TalkBack (on, then restored) with "
                 "next/prev from start ({start}). Each step is a capture ref + what it says; "
                 "diff: actual vs model (skip, double, out_of_order, loop, trap, escape, stuck, "
                 "left_app) by ref; findings with fixes.")
#: tb_walk with no capture tools listed: only what that listing can follow (refs come from
#: an earlier walk's lines; nothing there reads a stored walk or issues a selector)
D_TB_WALK_ALONE = _TB_WALK_CORE.format(
    start="current, first, a ref from a walk's lines, or a label as spoken")
#: ... with the capture tools listed: the loop that leads to (and from) a walk
D_TB_WALK = (_TB_WALK_CORE.format(start="current, first, a ref or selector")
             + " Stored as a walk (w3f9ak1). Loop: " + TB_LOOP + ".")
D_TB_SCENARIO = (_DEVICE_WIDE + "where real TalkBack focus goes, by ref. focus_after: do "
                 "action (activate|back|tap:<ref>|key:<combo>); restore: activate target, go "
                 "back; survive: focus target, apply mutate (tap:<ref>|activate|key:|broadcast:"
                 "|probe:), watch wait_ms. Verdict, timeline, cause (capture diff).")

INSTRUCTIONS = (
    "Inspector Widget reads the live UI of a debuggable Android app. Workflow: capture() takes "
    "one snapshot (views + properties, Compose, accessibility, screenshots, lint) and returns "
    "an id like c7h2kq with a short outline. Query it with outline, find, "
    "node, image, lint and diff; they never touch the device. Every node has a short ref "
    "(n23) that stays the same in later captures of the app; a ref that is gone "
    "returns an error instead of pointing elsewhere. Outline lines read: ref Type #resourceId "
    "@testTag \"label\" flags [x,y wxh] !issue +N (N hidden descendants); coordinates are "
    "screen pixels. After the UI changes, capture again (capture(diff_from=\"prev\") also "
    "reports what changed). serial and package are optional once a session exists.")
#: Added when the TalkBack tools are listed with the capture tools.
INSTRUCTIONS_TALKBACK = " TalkBack: " + TB_LOOP + "."
#: ... with the legacy tools (no outline): dump_accessibility's focus_order predicts it.
INSTRUCTIONS_TALKBACK_LEGACY = (" TalkBack: dump_accessibility's focus_order predicts its "
                                "order; tb_walk drives the real screen reader and compares.")
#: ... alone: tb_walk is the only TalkBack order there is.
INSTRUCTIONS_TALKBACK_ONLY = (" TalkBack: tb_walk drives the real screen reader through the "
                              "app and reports its order and findings; tb_scenario checks "
                              "focus after an action, back or a list update.")
_LEGACY_HEAD = ("Inspector Widget reads the live UI of a debuggable Android app: Views, Compose "
                "and the accessibility tree TalkBack sees.")
_LEGACY_TOOLS_TEXT = (
    " Each tool reads the device when called: inspect gives the merged tree, "
    "dump_accessibility the reading order, a11y_lint the accessibility findings, inspect_node "
    "one element. Results are compact and brief; detail=\"full\" gives everything and an "
    "oversize result becomes a spill file.")
_LEGACY_TAIL = (
    " serial and package: list_devices and list_processes. The capture-and-walk tools "
    "(capture once, then query the stored snapshot with small calls) are listed with "
    "INSPECTOR_WIDGET_TOOLSET=capture (or all).")
#: The instructions while the capture tools are not listed (the default until S4).
INSTRUCTIONS_LEGACY = _LEGACY_HEAD + _LEGACY_TOOLS_TEXT + _LEGACY_TAIL
INSTRUCTIONS_MAX_BYTES = 900


def instructions(listed: Iterable[str]) -> str:
    """The MCP ``instructions`` for the tools a server lists (at most 900 B).

    The text names only listed tools: the capture workflow when ``capture`` is
    listed, the legacy inspection tools when they are, and the TalkBack line
    that matches what predicts the reading order (``outline``, else
    ``dump_accessibility``, else ``tb_walk`` alone)."""
    names = set(listed)
    if "capture" in names:
        text = INSTRUCTIONS
    else:
        text = _LEGACY_HEAD
        if {"inspect", "dump_accessibility", "a11y_lint", "inspect_node"} <= names:
            text += _LEGACY_TOOLS_TEXT
        text += _LEGACY_TAIL
    if "tb_walk" in names:
        if "outline" in names:
            text += INSTRUCTIONS_TALKBACK
        elif "dump_accessibility" in names:
            text += INSTRUCTIONS_TALKBACK_LEGACY
        else:
            text += INSTRUCTIONS_TALKBACK_ONLY
    return text


_FLAGS_HELP = "click longclick focus scroll checkable checked selected disabled heading " \
              "edit hidden ..."


def _specs() -> list[ToolSpec]:
    return [
        ToolSpec("capture", "capture", "snapshot the app once into the capture store", [
            _serial(doc=True), _package(doc=True),
            Param("label", "string", help="Unique per app"),
            Param("props", "boolean", True),
            Param("resolution_stack", "boolean", False),
            Param("slots", "string", "if_available", enum=("if_available", "enable", "off"),
                  help="enable hot-reloads the app first (DESTRUCTIVE: resets remember{})"),
            Param("screenshot", "boolean", True),
            Param("screenshot_scale", "number", 1.0, exclusive_minimum=0, maximum=1.0,
                  cli=("--scale",)),
            Param("skp", "boolean", False),
            Param("a11y_rendering", "boolean", False),
            Param("lint", "string", "tree", enum=("tree", "full", "none"),
                  help="full adds contrast (~4s)"),
            Param("settle_ms", "integer", 0, minimum=0, maximum=3000),
            Param("diff_from", "string", help="prev or a label"),
            Param("if_changed_since", "string",
                  help="{unchanged:true} if the UI still matches it"),
            Param("outline_lines", "integer", ops.OUTLINE_LINES, minimum=0, maximum=80),
            Param("on_screen", "boolean", True),
            Param("pin", "boolean", False),
            _max_bytes(ops.CAPTURE_MAX_BYTES),
            _build_out(),
        ], ops.capture, False, {"capture"}, D_CAPTURE, _render_capture),
        ToolSpec("captures", "captures", "list and manage stored captures", [
            Param("action", "string", "list", enum=ops.CAPTURE_ACTIONS, positional=True,
                  nargs="?", cli_choices={"ls": "list", "rm": "drop"}),
            Param("id", "string", positional=True, nargs="?"),
            Param("label", "string", positional=True, nargs="?", help="empty removes"),
            Param("what", "string", "nodes", enum=ops.CAPTURES_WHAT),
            Param("format", "string", "jsonl", enum=ops.EXPORT_FORMATS),
            Param("all", "boolean", False, help="list: every app; gc: wipe the store"),
            Param("limit", "integer", ops.CAPTURES_LIMIT, minimum=1, maximum=200),
            _max_bytes(ops.CAPTURES_MAX_BYTES),
            _serial(), _package(),
        ], ops.captures, False, {"capture"}, D_CAPTURES, _render_lines),
        ToolSpec("outline", "outline", "a capture's tree, one line per node", [
            _capture(doc=True), Param("root", "string", help="A node selector"),
            Param("view", "string", "ui",
                  enum=("ui", "views", "compose", "slots", "a11y", "reading")),
            Param("depth", "integer", 3, minimum=0, maximum=999),
            Param("detail", "string", "semantic", enum=("semantic", "all")),
            Param("origin", "string", "app", enum=("app", "all")),
            Param("max_children", "integer", 12, minimum=1, maximum=1000),
            Param("max_lines", "integer", 80, minimum=1, maximum=400),
            _fields(doc=True), _cursor(), _format(), _max_bytes(6000), _serial(), _package(),
            # view="reading" only (TalkBack's walk); unset means the default
            Param("explain", "boolean"),
            Param("granularity", "string", enum=("default", "heading", "control")),
            Param("from", "string"), Param("direction", "string", enum=("next", "prev")),
            Param("include_skipped", "boolean"),
        ], ops.outline, True, {"capture"}, D_OUTLINE, _render_lines),
        ToolSpec("find", "find", "find nodes in a capture (filters are ANDed)", [
            _capture(),
            Param("text", "string"), Param("text_re", "string"),
            Param("type", "string"), Param("rid", "string"), Param("tag", "string"),
            Param("src", "string", help="File.kt or File.kt:line glob"),
            Param("role", "string"),
            Param("flags", "array", items="string", help=_FLAGS_HELP),
            Param("any_flags", "array", items="string"),
            Param("has", "array", items="string",
                  help="label role state stop slots props issues a11y compose view"),
            Param("missing", "array", items="string"),
            Param("issue", "string"), Param("within", "string"),
            Param("at", "array", items="number"), Param("overlaps", "array", items="number"),
            Param("min_dp", "number"), Param("max_dp", "number"),
            Param("kind", "string", enum=("view", "compose", "slot", "a11y")),
            Param("window", "string|integer",
                  help="Window selector or z index (0 = bottom)"),
            Param("in", "string", "ui", enum=("ui", "slots", "all")),
            Param("sort", "string", "tree", enum=("tree", "reading", "top", "area")),
            Param("limit", "integer", 20, minimum=1, maximum=200),
            _fields(), _cursor(),
            Param("count_only", "boolean", False, cli=("--count",)),
            _format(), _max_bytes(3000), _serial(), _package(),
        ], ops.find, True, {"capture"}, D_FIND, _render_lines),
        ToolSpec("node", "node", "everything about one node (or up to 10)", [
            Param("ref", "string", positional=True, nargs="*"),
            Param("refs", "array", items="string"),
            _capture(),
            Param("facets", "string", help="core,issues,a11y,tb,layout,compose,text,props,"
                                           "children,ancestors or all"),
            Param("props", "string|array", "none", items="string",
                  keep_words=("none", "key", "nondefault", "all"),
                  help="none|key|nondefault|all or names"),
            Param("params", "string", "brief", enum=("brief", "raw")),
            Param("ancestors", "boolean", False), Param("children", "boolean", False),
            Param("image", "boolean", False),
            _max_bytes(None), _serial(), _package(),
        ], ops.node, True, {"capture"}, D_NODE, _render_json),
        ToolSpec("image", "image", "a node's crop or an overlay PNG (prints the path)", [
            Param("ref", "string", positional=True, nargs="?"),
            _capture(), Param("window", "string"),
            Param("overlay", "string", "none", enum=("none", "marks", "lint", "reading",
                                                     "bounds", "compose", "walk")),
            Param("marks", "string|array", "auto", items="string", keep_words=("auto", "all")),
            Param("pad", "integer", 16, minimum=0, maximum=2000),
            Param("source", "string", "auto", enum=ops.IMAGE_SOURCES),
            Param("max_side", "integer", 1024, minimum=64, maximum=4096),
            Param("inline", "boolean", False, surfaces=("mcp",),
                  help="Also return the image (~w*h/750 tokens)"),
            Param("out", "string", surfaces=("cli",), help="copy the PNG to this path"),
            _max_bytes(ops.IMAGE_MAX_BYTES), _serial(), _package(),
            Param("walk", "string"),
        ], ops.image, True, {"capture"}, D_IMAGE, _render_image),
        ToolSpec("lint", "lint", "accessibility lint of a capture, grouped", [
            _capture(),
            Param("rules", "array", items="string", cli=("--rule",), check=_rules_check,
                  help="ids, R1..R18, codes, a11y., render. or tb"),
            Param("severity", "string", "info", enum=("error", "warn", "info")),
            Param("within", "string"),
            Param("contrast", "boolean", False), Param("wcag", "boolean", False),
            Param("group", "string", "rule", enum=("rule", "node", "none")),
            Param("per_rule", "integer", 3, minimum=1, maximum=20),
            Param("limit", "integer", 30, minimum=1, maximum=200),
            _cursor(), _max_bytes(4000), _serial(), _package(),
        ], ops.lint, True, {"capture"}, D_LINT, _render_json),
        ToolSpec("diff", "diff", "compare two captures of one app by ref", [
            Param("a", "string", "prev", positional=True, nargs="?"),
            Param("b", "string", "latest", positional=True, nargs="?"),
            Param("within", "string"),
            Param("include", "string|array", items="string",
                  help="text,state,bounds,visibility,a11y,issues,+props,+params,+pixels"),
            Param("min_move_px", "integer", 4, minimum=0, maximum=10000),
            Param("limit", "integer", 40, minimum=1, maximum=200),
            Param("image", "boolean", False), _cursor(),
            _max_bytes(4000), _serial(), _package(),
        ], ops.diff, True, {"capture"}, D_DIFF, _render_lines),
        # TalkBack (docs/design/talkback-navigation.md part 4 B): DEVICE-WIDE
        ToolSpec("talkback", "talkback", "TalkBack status/on/off/restore (DEVICE-WIDE: the "
                 "accessibility settings are snapshotted and restored)", [
            Param("action", "string", "status", enum=ops.TALKBACK_ACTIONS, positional=True,
                  nargs="?"),
            _serial(), _package(),
            Param("verbose_log", "boolean", False,
                  help="on: TalkBack log level VERBOSE (walks read its exact words)"),
        ], ops.talkback, False, {"talkback"}, D_TALKBACK, _render_json),
        ToolSpec("tb_walk", "tb-walk", "drive the real TalkBack (DEVICE-WIDE) through the app "
                 "and diff its order with the model's, by capture ref", [
            _serial(), _package(),
            Param("start", "string", "current", help="current, first, a ref/selector or a label"),
            Param("direction", "string", "next", enum=ops.TB_DIRECTIONS,
                  cli_set={"--prev": "prev"}),
            Param("max_steps", "integer", ops.TB_MAX_STEPS, minimum=1, maximum=300),
            Param("until", "string", "wrap", enum=ops.TB_UNTIL),
            Param("expect", "array", items="string", repeat=True,
                  help="Expected order: refs, selectors or labels"),
            Param("step_timeout_ms", "integer", ops.TB_STEP_TIMEOUT_MS, minimum=100,
                  maximum=10000),
            Param("settle_ms", "integer", ops.TB_SETTLE_MS, minimum=10, maximum=2000),
            Param("recapture", "string", "on_unknown", enum=ops.TB_RECAPTURE),
            Param("utterance", "string", "auto", enum=ops.TB_UTTERANCE),
            Param("injector", "string", "auto", enum=ops.TB_INJECTORS),
            Param("leave_on", "boolean", False),
            Param("max_lines", "integer", 60, minimum=5, maximum=300),
            Param("max_bytes", "integer", ops.TB_WALK_MAX_BYTES, maximum=ops.TB_WALK_HARD_MAX),
            _build_out(),
        ], ops.tb_walk, False, {"talkback"}, D_TB_WALK, _render_tb),
        ToolSpec("tb_scenario", "tb-scenario", "where real TalkBack focus goes after an action, "
                 "after back, or after the list updates (DEVICE-WIDE)", [
            Param("kind", "string", enum=ops.TB_KINDS, positional=True, nargs="?",
                  cli_choices={"focus-after": "focus_after"}),
            _serial(), _package(),
            Param("target", "string", help="A ref, selector or label; default: current focus"),
            Param("action", "string", "activate"),
            Param("mutate", "string"),
            Param("wait_ms", "integer", ops.TB_WAIT_MS, minimum=300, maximum=20000),
            Param("injector", "string", "auto", enum=ops.TB_INJECTORS),
            Param("leave_on", "boolean", False),
            Param("step_timeout_ms", "integer", ops.TB_STEP_TIMEOUT_MS, minimum=100,
                  maximum=10000),
            Param("settle_ms", "integer", ops.TB_SETTLE_MS, minimum=10, maximum=2000),
            _max_bytes(ops.TB_SCENARIO_MAX_BYTES),
            _build_out(),
        ], ops.tb_scenario, False, {"talkback"}, D_TB_SCENARIO, _render_tb),
    ]


# Renderers are defined below; the specs are built lazily so they can refer to them.
SPECS: list[ToolSpec] = []
BY_NAME: dict[str, ToolSpec] = {}


def _init() -> None:
    if not SPECS:
        SPECS.extend(_specs())
        BY_NAME.update({s.name: s for s in SPECS})


def spec(name: str) -> ToolSpec:
    _init()
    return BY_NAME[name]


# --------------------------------------------------------------------------- #
# JSON schema and validation
# --------------------------------------------------------------------------- #
def _type_schema(p: Param) -> dict[str, Any]:
    if p.type == "string|array":
        s: dict[str, Any] = {"type": ["string", "array"], "items": {"type": p.items or "string"}}
    elif p.type == "string|integer":
        s = {"type": ["string", "integer"]}
    elif p.type == "array":
        s = {"type": "array", "items": {"type": p.items or "string"}}
    else:
        s = {"type": p.type}
    return s


def json_schema(ts: ToolSpec) -> dict[str, Any]:
    """The MCP inputSchema of a tool (compact: descriptions only where needed)."""
    props: dict[str, Any] = {}
    for p in ts.params_for("mcp"):
        s = _type_schema(p)
        if p.enum is not None:
            s["enum"] = list(p.enum)
        if p.default is not None:
            s["default"] = p.default
        if p.minimum is not None:
            s["minimum"] = p.minimum
        if p.exclusive_minimum is not None:
            s["exclusiveMinimum"] = p.exclusive_minimum
        if p.maximum is not None:
            s["maximum"] = p.maximum
        if p.help:
            s["description"] = p.help
        props[p.name] = s
    return {"type": "object", "properties": props, "additionalProperties": False}


_PY_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,), "integer": (int,), "number": (int, float), "boolean": (bool,),
}


def describe(p: Param) -> str:
    """``limit: integer 1..200``, ``view: one of ui, views, ...``, ``flags: list of string``."""
    if p.enum is not None:
        return f"{p.name}: one of {', '.join(map(str, p.enum))}"
    typ = {"array": f"list of {p.items or 'string'}",
           "string|array": f"string or list of {p.items or 'string'}",
           "string|integer": "string or integer"}.get(p.type, p.type)
    lo = p.minimum if p.minimum is not None else (
        f">{p.exclusive_minimum}" if p.exclusive_minimum is not None else None)
    if lo is not None or p.maximum is not None:
        typ += f" {'' if lo is None else lo}..{'' if p.maximum is None else p.maximum}"
    return f"{p.name}: {typ}"


def _bad(message: str, ts: ToolSpec, p: Param | None = None) -> OpError:
    if p is not None:
        hint = describe(p)
    else:
        hint = f"{ts.name} takes: {', '.join(x.name for x in ts.params_for('mcp'))}"
    return OpError("bad_args", message, hint=hint)


def _check_scalar(ts: ToolSpec, p: Param, v: Any, typ: str, where: str) -> Any:
    if typ == "integer" and isinstance(v, float) and v.is_integer():
        v = int(v)
    ok = isinstance(v, _PY_TYPES[typ]) and not (typ in ("integer", "number")
                                                and isinstance(v, bool))
    if not ok:
        raise _bad(f"{where} must be {'an' if typ[0] in 'aeiou' else 'a'} {typ}; got {v!r}", ts, p)
    if typ in ("integer", "number"):
        if p.minimum is not None and v < p.minimum:
            raise _bad(f"{where} must be >= {p.minimum}; got {v!r}", ts, p)
        if p.exclusive_minimum is not None and v <= p.exclusive_minimum:
            raise _bad(f"{where} must be > {p.exclusive_minimum}; got {v!r}", ts, p)
        if p.maximum is not None and v > p.maximum:
            raise _bad(f"{where} must be <= {p.maximum}; got {v!r}", ts, p)
    return v


def _split(v: str) -> list[str]:
    return [x.strip() for x in v.split(",") if x.strip()]


def validate(ts: ToolSpec, args: Any, surface: str = "mcp") -> dict[str, Any]:
    """Check ``args`` against the spec (stdlib only); returns them cleaned: an
    explicit null is dropped (the default applies), a whole-number float for an
    integer becomes an int, and a comma string for a list of words becomes the
    list. Anything else that does not fit is ``bad_args``."""
    if args is None:
        args = {}
    if not isinstance(args, Mapping):
        raise OpError("bad_args", f"arguments must be an object; got {type(args).__name__}")
    params = {p.name: p for p in ts.params_for(surface)}
    unknown = sorted(k for k in args if k not in params)
    if unknown:
        raise _bad(f"unknown argument(s) for {ts.name}: {', '.join(map(str, unknown))}", ts)
    out: dict[str, Any] = {}
    for k, v in args.items():
        p = params[k]
        if v is None:
            continue
        if p.type in ("array", "string|array"):
            if isinstance(v, str):
                v = v if p.type == "string|array" and ("," not in v) else _split(v)
            if isinstance(v, (list, tuple)):
                item = p.items or "string"
                v = [_check_scalar(ts, p, x, item, f"{k}[{i}]") for i, x in enumerate(v)]
            elif not isinstance(v, str):
                raise _bad(f"{k} must be a list{' or a string' if '|' in p.type else ''}; "
                           f"got {v!r}", ts, p)
        elif p.type == "string|integer":
            if isinstance(v, float) and v.is_integer():
                v = int(v)
            if isinstance(v, bool) or not isinstance(v, (str, int)):
                raise _bad(f"{k} must be a string or an integer; got {v!r}", ts, p)
        else:
            v = _check_scalar(ts, p, v, p.type, k)
        if p.enum is not None and not isinstance(v, list) and v not in p.enum:
            raise _bad(f"{k} must be one of {', '.join(map(str, p.enum))}; got {v!r}", ts, p)
        if p.check is not None:
            p.check(v)
        out[k] = v
    return out


# --------------------------------------------------------------------------- #
# Running a tool
# --------------------------------------------------------------------------- #
class Result(dict):
    """A tool's response document (a dict), plus the images an MCP reply carries
    beside its text and whether it is an error."""

    images: list[tuple[str, str]]

    def __init__(self, doc: Mapping[str, Any], images: list[tuple[str, str]] | None = None
                 ) -> None:
        super().__init__(doc)
        self.images = list(images or [])

    @property
    def is_error(self) -> bool:
        return ops.is_error(self)

    def text(self, pretty: bool = False) -> str:
        return dumps(dict(self), pretty=pretty)


def execute(name: str, args: Any, ctx: ops.OpContext, *, surface: str = "mcp",
            passthrough: tuple[type[BaseException], ...] = ()) -> Result:
    """Validate and run one tool; every failure becomes the error envelope,
    except the exception types in ``passthrough`` (the MCP server retries a
    session lost mid-call itself)."""
    _init()
    ts = BY_NAME.get(name)
    try:
        if ts is None:
            raise OpError("bad_args", f"unknown tool {name!r}", candidates=[s.name for s in SPECS])
        clean = validate(ts, args, surface)
        inline = bool(clean.pop("inline", False))
        doc = ts.fn(ctx, **clean)
    except passthrough:
        raise
    except Exception as exc:  # noqa: BLE001 - the agent-facing envelope
        # a caller of a tool it does not list (called by name) knows the hidden ones
        listed = ctx.listed if ts is not None and ctx.listed is not None \
            and ts.name in ctx.listed else None
        return Result(listed_hint(ops.error_envelope(exc), listed))
    images: list[tuple[str, str]] = []
    if inline and isinstance(doc.get("path"), str):
        from .capture import images as cimages
        try:
            mime, data, tokens = cimages.inline(doc["path"], int(clean.get("max_side") or 1024))
        except OSError as exc:
            doc = dict(doc, inline_error=str(exc))
        else:
            images.append((mime, data))
            doc = dict(doc, inline_tokens=tokens)
    return Result(doc, images)


_CALL = re.compile(r"\b([a-z_]+)\(")
#: An error's way on for a caller that lists the TalkBack tools but not the capture ones
#: (the default and talkback listings), by error code: a tb_walk captures the screen itself.
_TB_ROUTES = {
    "ref_not_in_capture": "Take refs from a tb_walk's lines (each walk captures the screen), "
                          "or pass the label as spoken.",
    "capture_not_found": "Run tb_walk once (it captures the screen) to get refs, or pass the "
                         "label as spoken.",
    "not_found": "Pass a ref from a tb_walk's lines, or the label as spoken.",
    "ambiguous": "Pass a ref from a tb_walk's lines, or more of the label as spoken.",
    "bad_selector": "Pass a ref from a tb_walk's lines, or the label as spoken.",
    "walk_not_found": "tb_walk() records one.",
}


def listed_hint(env: dict[str, Any], listed: Iterable[str] | None) -> dict[str, Any]:
    """An error envelope whose hint names only tools the caller lists (``listed``; None:
    every tool, unchanged): sentences that call an unlisted tool are dropped, and when none
    is left, a TalkBack-only caller gets the way on it has (:data:`_TB_ROUTES`)."""
    err = env.get("error") if isinstance(env, dict) else None
    hint = err.get("hint") if isinstance(err, dict) else None
    if listed is None or not isinstance(hint, str) or not hint:
        return env
    have = set(listed)
    tools = set(LEGACY_TOOLS) | set(TALKBACK_TOOLS) | set(CAPTURE_TOOLS)

    def unlisted(text: str) -> set[str]:
        return {m for m in _CALL.findall(text) if m in tools} - have

    if not unlisted(hint):
        return env
    keep = [x for x in re.split(r"(?<=[.!?])\s+", hint) if not unlisted(x)]
    new = " ".join(keep).strip() or (_TB_ROUTES.get(str(err.get("code")))
                                     if "tb_walk" in have else None)
    return dict(env, error=dict(err, hint=new or None))


def error_result(exc: BaseException) -> Result:
    return Result(ops.error_envelope(exc))


def run(name: str, args: Any, ctx: ops.OpContext, surface: str = "mcp"
        ) -> tuple[str, list[tuple[str, str]], bool]:
    """``(text, images, is_error)``: the MCP reply of one call (``images`` are
    ``(mime, base64)`` pairs)."""
    res = execute(name, args, ctx, surface=surface)
    return res.text(), res.images, res.is_error


# --------------------------------------------------------------------------- #
# MCP entries
# --------------------------------------------------------------------------- #
#: Tools with a destructive mode: capture(slots="enable") hot-reloads the app
#: (resetting remember{} state) and captures drop/gc(all=true) delete captures
#: of every agent sharing the store. MCP clients ask before running them.
DESTRUCTIVE_TOOLS = frozenset({"capture", "captures", *TALKBACK_TOOLS})


def annotations(ts: ToolSpec) -> dict[str, Any]:
    if ts.read_only:
        return {"readOnlyHint": True}
    out: dict[str, Any] = {"readOnlyHint": False, "destructiveHint": ts.name in DESTRUCTIVE_TOOLS}
    if ts.device_wide:  # TalkBack runs for every app: never run twice by accident
        out["idempotentHint"] = False
    return out


def mcp_entries(toolset: str = "all", *, context: Callable[[], ops.OpContext] | None = None,
                passthrough: tuple[type[BaseException], ...] = ()) -> dict[str, dict[str, Any]]:
    """The capture tools of ``toolset`` in ``mcp_server.TOOLS``' entry format:
    ``{name: {handler, description, schema, annotations, surface}}``. The handler
    takes the argument dict and returns a :class:`Result`; ``context()`` gives
    the server's OpContext."""
    _init()
    names = set(toolset_names(toolset, env={}))
    out: dict[str, dict[str, Any]] = {}
    for ts in SPECS:
        if ts.name not in names:
            continue

        def handler(args: Any, _name: str = ts.name) -> Result:
            if context is None:
                raise RuntimeError("no OpContext for the capture tools")
            return execute(_name, args, context(), surface="mcp", passthrough=passthrough)

        out[ts.name] = {"handler": handler, "description": ts.description,
                        "schema": json_schema(ts), "annotations": annotations(ts),
                        "surface": ts}
    return out


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
#: CLI flags that shape the output, not the call (every generated subcommand).
CLI_OUTPUT_FLAGS = ("json", "pretty", "quiet")


def _cli_value(p: Param) -> Callable[[str], Any]:
    if p.type == "integer":
        return int
    if p.type == "number":
        return float
    return str


def add_cli(subparsers: Any, *, context: Callable[[argparse.Namespace], ops.OpContext]
            | None = None) -> None:
    """Add one subcommand per spec: the canonical ``--kebab-name`` flag (and
    aliases) for every parameter, with the MCP default; ``--json`` prints the
    MCP text, ``--pretty`` indents it, and without them a human rendering."""
    _init()
    for ts in SPECS:
        sp = subparsers.add_parser(ts.cli_name, help=ts.summary, description=ts.description)
        for p in ts.params_for("cli"):
            if p.positional:
                kw: dict[str, Any] = {"nargs": p.nargs or "?", "help": p.help or None}
                if p.nargs != "*":
                    kw["default"] = p.default
                else:
                    kw["default"] = []
                if p.enum is not None:
                    kw["choices"] = list(p.enum) + list(p.cli_choices or {})
                sp.add_argument(p.name, metavar=p.name.upper(), **kw)
                continue
            names = [p.flag, *p.cli]
            if p.type == "boolean":
                if p.default is True:
                    sp.add_argument(*names, dest=p.name, default=True,
                                    action=argparse.BooleanOptionalAction, help=p.help or None)
                else:
                    sp.add_argument(*names, dest=p.name, default=bool(p.default),
                                    action="store_true", help=p.help or None)
                continue
            kw = {"dest": p.name, "default": p.default, "help": p.help or None}
            if p.enum is not None:
                kw["choices"] = list(p.enum)
            if p.type in ("array", "string|array"):
                kw["metavar"] = "A,B"
                if p.cli and p.cli[0] == "--rule":  # --rule R (repeatable) beside --rules A,B
                    sp.add_argument(*p.cli, dest=p.name + "_one", action="append",
                                    default=None, metavar="RULE", help=argparse.SUPPRESS)
                    names = [p.flag]
                elif p.repeat:  # --expect a --expect b, or --expect a,b
                    kw["action"] = "append"
            else:
                kw["type"] = _cli_value(p)
            sp.add_argument(*names, **kw)
            for flag, value in (p.cli_set or {}).items():
                sp.add_argument(flag, dest=p.name, action="store_const", const=value,
                                default=argparse.SUPPRESS, help=f"same as {p.flag} {value}")
        sp.add_argument("--json", action="store_true",
                        help="print the JSON the MCP tool returns")
        sp.add_argument("--pretty", action="store_true", help="indent the JSON")
        if ts.name == "capture":
            sp.add_argument("-q", "--quiet", action="store_true",
                            help="print only the capture id")
        sp.set_defaults(func=lambda ns, _ts=ts: cli_main(_ts, ns, context),
                        surface_tool=ts.name)


def cli_args(ts: ToolSpec, ns: argparse.Namespace) -> dict[str, Any]:
    """The tool arguments of parsed CLI flags: values that differ from the
    default only, with lists split on commas and CLI words mapped."""
    out: dict[str, Any] = {}
    for p in ts.params_for("cli"):
        if p.surfaces == ("cli",):
            continue
        v = getattr(ns, p.name, None)
        if p.name == "rules":
            extra = getattr(ns, "rules_one", None) or []
            parts = (_split(v) if isinstance(v, str) else list(v or [])) + \
                [x for e in extra for x in _split(e)]
            v = parts or None
        if p.positional and p.nargs == "*":
            v = list(v or [])
            if ts.name == "node" and p.name == "ref":
                if len(v) > 1:
                    out["refs"] = v
                    continue
                v = v[0] if v else None
        if p.cli_choices and v in p.cli_choices:
            v = p.cli_choices[v]
        if p.repeat and isinstance(v, list):
            v = [x for item in v for x in _split(str(item))] or None
        if isinstance(v, str) and p.type == "array":
            v = _split(v)
            if p.items == "number":
                v = [float(x) if "." in x else int(x) for x in v]
        elif isinstance(v, str) and p.type == "string|array" and v not in p.keep_words \
                and "," in v:
            v = _split(v)
        if v is None or v == p.default or (p.type == "boolean" and p.default is None
                                           and v is False):
            continue  # an unset flag (a boolean without a default reads as false)
        out[p.name] = v
    return out


def _talkback_note(ts: ToolSpec, args: Mapping[str, Any], res: Result) -> str | None:
    """The CLI's stderr note after a device-wide call: TalkBack left on (talkback on, a
    walk with leave_on), or a pending snapshot that restore will put back while TalkBack
    is off now (after off it may turn TalkBack back on)."""
    stays = f"note: TalkBack stays on (device-wide) until `{CLI_PROG} talkback restore`"
    if ts.name != "talkback":
        return stays if str(res.get("restore") or "").startswith("left on") else None
    act = args.get("action") or "status"
    if act == "on":
        return stays if res.get("changed") else None
    if res.get("restore_pending") is not True:
        return None
    tbs = res.get("talkback")
    if act == "status" and isinstance(tbs, dict) and tbs.get("enabled"):
        return stays
    return (f"note: `{CLI_PROG} talkback restore` puts back the saved settings (TalkBack too, "
            "if it was on before)")


def cli_main(ts: ToolSpec, ns: argparse.Namespace,
             context: Callable[[argparse.Namespace], ops.OpContext] | None) -> int:
    """Run a generated subcommand: print the MCP text (``--json``) or the human
    rendering; errors go to stderr as the same JSON, exit 1."""
    try:
        args = cli_args(ts, ns)
    except ValueError as exc:
        res = Result(OpError("bad_args", str(exc)).to_dict())
        print(res.text(pretty=ns.pretty), file=sys.stderr)
        return 1
    if context is None:
        from .capture.store import CaptureStore
        ctx = ops.OpContext(CaptureStore(),
                            ops.AttachProvider(build_out=getattr(ns, "build_out", None)), "cli")
    else:
        ctx = context(ns)
    try:
        res = execute(ts.name, args, ctx, surface="cli")
    finally:
        closer = getattr(ctx.sessions, "close_all", None)
        if callable(closer):
            closer()
    if res.is_error:
        print(res.text(pretty=ns.pretty), file=sys.stderr)
        return 1
    out_path = getattr(ns, "out", None)
    if ts.name == "image" and out_path and isinstance(res.get("path"), str):
        shutil.copyfile(res["path"], out_path)
    note = _talkback_note(ts, args, res) if ts.device_wide else None
    if note:
        print(note, file=sys.stderr)
    if ns.json or ns.pretty:
        print(res.text(pretty=ns.pretty))
    elif getattr(ns, "quiet", False):
        print(res.get("capture", ""))
    else:
        render = ts.render or _render_json
        for line in render(res if not out_path else dict(res, path=out_path)):
            print(line)
    return 0


# --------------------------------------------------------------------------- #
# Human renderers (the CLI without --json)
# --------------------------------------------------------------------------- #
CLI_PROG = "inspector-widget"


def _parse_call(text: str) -> tuple[str, list[Any], dict[str, Any]] | None:
    """``(tool, positional, keywords)`` of a ``next`` hint (``query.call``'s
    form, JSON values), or None when it is not one."""
    import ast
    import re

    src = re.sub(r"([(,])(in|from)=", r"\1\2_=", text.strip())
    try:
        tree = ast.parse(src, mode="eval").body
    except SyntaxError:
        return None
    if not isinstance(tree, ast.Call) or not isinstance(tree.func, ast.Name):
        return None
    names = {"true": True, "false": False, "null": None}

    def value(node: ast.AST) -> Any:
        if isinstance(node, ast.Name) and node.id in names:
            return names[node.id]
        if isinstance(node, ast.List):
            return [value(x) for x in node.elts]
        return ast.literal_eval(node)

    try:
        pos = [value(a) for a in tree.args]
        kw = {(k.arg[:-1] if k.arg in ("in_", "from_") else k.arg): value(k.value)
              for k in tree.keywords}
    except (ValueError, KeyError, TypeError):
        return None
    return tree.func.id, pos, kw


def _cli_word(v: Any) -> str:
    import shlex

    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, list):
        return shlex.quote(",".join(str(x) for x in v))
    return shlex.quote(str(v))


def cli_hint(text: str) -> str:
    """A ``next`` hint (``outline(root="n10",fields="-bounds")``) as the CLI
    command that runs it (``inspector-widget outline --root n10
    --fields=-bounds``). A hint for a tool without a CLI subcommand here, or
    one that does not parse, is returned unchanged."""
    _init()
    parsed = _parse_call(text)
    if parsed is None or parsed[0] not in BY_NAME:
        return text
    tool, pos, kw = parsed
    ts = BY_NAME[tool]
    words = [CLI_PROG, ts.cli_name]
    positionals = [p for p in ts.params_for("cli") if p.positional]
    given = dict(zip((p.name for p in positionals), pos))
    for k in list(kw):
        if any(p.name == k for p in positionals):
            given[k] = kw.pop(k)
    if given:
        last = max(i for i, p in enumerate(positionals) if p.name in given)
        for p in positionals[:last + 1]:
            v = given.get(p.name, p.default)
            if v is None:
                return text
            words.append(_cli_word(v))
    for k, v in kw.items():
        p = ts.param(k)
        if p is None or "cli" not in p.surfaces:
            return text
        if p.type == "boolean":
            if bool(v) != bool(p.default):
                words.append(p.flag if v else "--no-" + p.flag[2:])
            continue
        word = _cli_word(v)
        words.append(f"{p.flag}={word}" if word.startswith("-") else f"{p.flag} {word}")
    return " ".join(words)


def _next_lines(doc: Mapping[str, Any]) -> list[str]:
    return [f"next: {cli_hint(h)}" for h in doc.get("next") or []]


def _footer(doc: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    for key in ("hidden", "notes", "truncated", "omitted"):
        if doc.get(key):
            out.append(f"{key}: {dumps(doc[key])}")
    return out + _next_lines(doc)


_HEADER_SKIP = frozenset({"lines", "rows", "next", "hidden", "notes", "truncated", "omitted",
                          "outline", "on_screen", "diff", "windows", "facets"})


def _header(doc: Mapping[str, Any]) -> str:
    parts = []
    for k, v in doc.items():
        if k in _HEADER_SKIP or isinstance(v, (dict, list)):
            continue
        parts.append(f"{k}={v}")
    return " ".join(parts)


def _render_lines(doc: Mapping[str, Any]) -> list[str]:
    if not any(k in doc for k in ("lines", "rows", "outline")):
        return _render_json(doc)  # captures show / export / gc: a document, not lines
    out = [_header(doc)]
    for k in ("summary", "issues", "counts"):
        if isinstance(doc.get(k), dict):
            out.append(f"{k}: {dumps(doc[k])}")
    rows = doc.get("lines")
    if rows is None and doc.get("rows") is not None:
        rows = [dumps(r) for r in doc["rows"]]
    out.extend(rows or [])
    if doc.get("outline"):
        out.append("outline:")
        out.extend(doc["outline"])
    return out + _footer(doc)


def _render_json(doc: Mapping[str, Any]) -> list[str]:
    body = {k: v for k, v in doc.items() if k != "next"}
    return [dumps(body, pretty=True), *_next_lines(doc)]


def _render_image(doc: Mapping[str, Any]) -> list[str]:
    out = [str(doc.get("path"))]
    extra = {k: v for k, v in doc.items() if k not in ("path",)}
    out.append(dumps(extra))
    return out


def _render_tb(doc: Mapping[str, Any]) -> list[str]:
    """tb_walk / tb_scenario for humans: a header, the steps (or the timeline), the
    classified diff, each finding with its fix, then the next commands."""
    out = [_header(doc)]
    for line in doc.get("lines") or []:
        out.append(f"  {line}")
    for ev in doc.get("timeline") or []:
        out.append(f"  +{ev}")
    for key in ("diff", "expect"):
        if isinstance(doc.get(key), dict):
            out.append(f"{key}: {dumps(doc[key])}")
    findings = list(doc.get("findings") or [])
    if isinstance(doc.get("finding"), dict):
        findings.append(doc["finding"])
    for f in findings:
        refs = f" ({' '.join(f['refs'])})" if f.get("refs") else ""
        out.append(f"{f.get('code')} [{f.get('sev')}{'/' + f['basis'] if f.get('basis') else ''}]"
                   f"{refs} {f.get('msg')}")
        if f.get("fix"):
            out.append(f"  fix: {f['fix']}")
    for key in ("notes", "recaptured", "panes"):
        if doc.get(key):
            out.append(f"{key}: {dumps(doc[key])}")
    return out + _next_lines(doc)


def _render_capture(doc: Mapping[str, Any]) -> list[str]:
    if doc.get("unchanged"):
        return [str(doc.get("capture")), f"unchanged (age {doc.get('age_s')}s)"]
    head = str(doc.get("capture"))
    if doc.get("label"):
        head += f" @{doc['label']}"
    out = [head]
    out.append(" ".join(f"{k}={doc[k]}" for k in ("session", "pid", "device", "took_ms",
                                                  "consistency") if doc.get(k) is not None))
    out.append("facets: " + " ".join(f"{k}={v}" for k, v in (doc.get("facets") or {}).items()))
    for w in doc.get("windows") or []:
        out.append(f"window: {w}")
    for key in ("lint", "issues", "warning", "store", "note", "moved_from"):
        if doc.get(key):
            out.append(f"{key}: {doc[key]}")
    for d in doc.get("diagnostics") or []:
        out.append(f"diagnostic: {d}")
    d = doc.get("diff")
    if isinstance(d, dict):
        out.append(f"diff vs {d.get('a')}: {dumps(d.get('summary') or {})}"
                   + (f" verdict={d['verdict']}" if d.get("verdict") else ""))
        out.extend(d.get("lines") or [])
    out.extend(doc.get("outline") or [])
    if doc.get("on_screen"):
        out.append("on screen: " + " | ".join(doc["on_screen"]))
    return out + _next_lines(doc)


# Build the registry now that the renderers exist.
_init()


def cli_names() -> list[str]:
    _init()
    return [s.cli_name for s in SPECS]


def _cli_option_strings(ts: ToolSpec) -> tuple[set[str], set[str]]:
    """(the flags of ``ts``' subcommand that take a value, every flag it has)."""
    takes: set[str] = set()
    every = {"--json", "--pretty", "-h", "--help"}
    if ts.name == "capture":
        every |= {"-q", "--quiet"}
    for p in ts.params_for("cli"):
        if p.positional:
            continue
        opts = {p.flag, *p.cli}
        every |= set(p.cli_set or ())
        if p.type == "boolean":
            if p.default is True:
                opts.add("--no-" + p.flag[2:])
        else:
            takes |= opts
        every |= opts
    return takes, every


def cli_argv(argv: Iterable[str]) -> list[str]:
    """``argv`` as the capture subcommands' parser should read it:

    * ``--json -`` is ``--json``: the legacy subcommands take ``--json
      OUT.json|-``, these always print to stdout, and the habit should not be
      an argparse error;
    * a flag's value that starts with ``-`` but is no flag of the subcommand
      is joined to it (``--fields -bounds`` -> ``--fields=-bounds``, ``--at
      -5,10``): spec 6.4's field removal must work as written, and argparse
      would read ``-bounds`` as an unknown option."""
    argv = list(argv)
    if not argv or argv[0] not in cli_names():
        return argv
    _init()
    ts = next(t for t in SPECS if t.cli_name == argv[0])
    takes, every = _cli_option_strings(ts)
    out: list[str] = []
    skip = False
    for i, a in enumerate(argv):
        if skip:
            skip = False
            continue
        nxt = argv[i + 1] if i + 1 < len(argv) else None
        if a == "--json" and nxt == "-":
            out.append(a)
            skip = True
        elif (a in takes and nxt is not None and nxt.startswith("-") and nxt != "-"
              and nxt.split("=", 1)[0] not in every):
            out.append(f"{a}={nxt}")
            skip = True
        else:
            out.append(a)
    return out


def names(specs: Iterable[ToolSpec] | None = None) -> list[str]:
    return [s.name for s in (specs if specs is not None else SPECS)]


__all__ = [
    "BY_NAME",
    "CLI_OUTPUT_FLAGS",
    "DEFAULT_TOOLSET",
    "ENV_TOOLSET",
    "INSTRUCTIONS",
    "INSTRUCTIONS_LEGACY",
    "INSTRUCTIONS_TALKBACK",
    "LEGACY_TOOLS",
    "TB_LOOP",
    "Param",
    "Result",
    "SESSION_TOOLS",
    "SPECS",
    "TALKBACK_TOOLS",
    "TOOLSETS",
    "ToolSpec",
    "active_toolset",
    "add_cli",
    "cli_args",
    "cli_hint",
    "cli_argv",
    "describe",
    "error_result",
    "execute",
    "instructions",
    "json_schema",
    "legacy_talkback",
    "mcp_entries",
    "run",
    "spec",
    "toolset_names",
    "validate",
]
