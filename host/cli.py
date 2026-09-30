#!/usr/bin/env python3
"""Inspector Widget host CLI.

Subcommands (``inspector-widget --help`` lists all of them):
  devices                      list attached devices
  packages   --serial          list debuggable (run-as-able) packages
  attach     --serial --package  inject the agent and PING it (Hello)
  dump       --serial --package  inject + dump tree (+ optional props/screenshot/json)
  ...
  detach     --serial --package  stop a running agent (never injects one)

``--serial`` defaults to ``$ANDROID_SERIAL``, else the only attached device.
Every subcommand except ``detach`` leaves the agent running when it exits, so
the next run reconnects warm; ``--force`` stops it and injects a fresh one.

Run with: ``python3 host/cli.py <subcommand> ...`` (the script adds its own
directory to sys.path so ``inspector_widget`` resolves without installation).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Make `inspector_widget` importable when run as a loose script.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import inspector_widget as iw  # noqa: E402
from inspector_widget import adb  # noqa: E402
from inspector_widget import png as pngmod  # noqa: E402
from inspector_widget import strings as stringsmod  # noqa: E402

DEFAULT_PACKAGE = "com.oberkfell.a11yprobe"


def _print_node(node, resolver, indent=0):
    prefix = "  " * indent
    class_name = resolver.get(node.class_name)
    b = node.bounds.layout
    res = stringsmod._resource_to_dict(resolver, node.resource)
    res_str = f" @{res.get('type')}/{res.get('name')}" if res else ""
    text = resolver.opt(node.text_value)
    text_str = f' "{text}"' if text else ""
    flags = " [WebView]" if (node.flags & 1) else ""
    print(
        f"{prefix}{class_name}{res_str}{text_str}{flags} "
        f"({b.x},{b.y} {b.w}x{b.h}) id={node.id}"
    )
    for child in node.children:
        _print_node(child, resolver, indent + 1)


# --------------------------------------------------------------------------- #
# Subcommand handlers
# --------------------------------------------------------------------------- #
def cmd_devices(args) -> int:
    for d in adb.devices():
        print(f"{d.serial}\t{d.state}")
    return 0


def cmd_packages(args) -> int:
    pkgs = adb.list_debuggable_packages(args.serial)
    if not pkgs:
        print("(no debuggable packages found)", file=sys.stderr)
    for p in pkgs:
        print(p)
    return 0


def _session(args):
    """Inject or warm-connect per ``args``; use as ``with _session(args) as session``.

    Leaving the block disconnects and keeps the agent running (only ``detach``
    stops it), so a concurrent MCP session on the same app is left alone.
    """
    return iw.attach(args.serial, args.package, build_out=args.build_out,
                     force_reinject=getattr(args, "force", False))


def _remove_quietly(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _write_overlay(shot, out_path, render, fallback_scale):
    """Write ``shot`` as ``OUT.png.base.png``, run ``render(base, scale)``, and
    always remove the base file, even if rendering fails."""
    base = out_path + ".base.png"
    try:
        pngmod.write_png(shot.screenshot, base)
        return render(base, float(shot.screenshot.scale) or fallback_scale)
    finally:
        _remove_quietly(base)


def cmd_attach(args) -> int:
    with _session(args) as session:
        info = session.info()
        warm = " (warm/reused)" if info["warm"] else ""
        build = f" (build {info['build_id'][:12]})" if info.get("build_id") else ""
        print(
            f"attached to {args.package} pid={info['pid']}{warm}: "
            f"agent {info['agent_version']}{build}, API {info['api_level']}, abi {info['abi']}"
        )
        print(f"socket=@{session.injection.socket_name} forwarded tcp:{session.injection.local_port}")
    return 0
def cmd_dump(args) -> int:
    with _session(args) as session:
        want_screenshot = bool(args.screenshot)
        resp = session.client.dump_tree(
            root_id=args.root_id,
            properties=args.properties or args.resolution_stack,
            resolution_stack=args.resolution_stack,
            screenshot=want_screenshot,
            scale=args.scale,
        )

        if args.json:
            data = stringsmod.dump_tree_to_dict(resp)
            text = json.dumps(data, indent=2)
            if args.json == "-":
                print(text)
            else:
                with open(args.json, "w") as f:
                    f.write(text)
                print(f"wrote tree JSON to {args.json}", file=sys.stderr)
        else:
            resolver = stringsmod.StringResolver(resp.strings)
            if not resp.roots:
                print("(no root views found)", file=sys.stderr)
            for root in resp.roots:
                _print_node(root, resolver)

        if want_screenshot and resp.HasField("screenshot"):
            w, h = pngmod.write_png(resp.screenshot, args.screenshot)
            print(f"wrote screenshot {w}x{h} to {args.screenshot}", file=sys.stderr)
    return 0
def _compose_text_summary(node, out, depth=0):
    a = node.get("attrs", {}) or {}
    txt = a.get("Text") or a.get("ContentDescription")
    b = (node.get("bounds") or {}).get("layout")
    if txt and b:
        role = a.get("Role", "")
        out.append(f'{"  "*depth}"{txt}" {("["+role+"] ") if role else ""}({b["x"]},{b["y"]} {b["w"]}x{b["h"]})')
    for ch in node.get("children", []) or []:
        _compose_text_summary(ch, out, depth + 1)


def cmd_compose(args) -> int:
    from inspector_widget import overlay as ovmod
    with _session(args) as session:
        client = session.client
        comp = client.dump_compose(
            include_semantics=True,
            include_slot_table=not args.no_slot_table,
            enable_inspection=args.enable_inspection,
        )
        data = stringsmod.dump_compose_to_dict(comp)
        print(f"compose: {data.get('diagnostics','')}", file=sys.stderr)
        if (not args.no_slot_table and not args.enable_inspection
                and not stringsmod.compose_slot_table_populated(data)):
            print("compose: slot table not populated (semantics only). Re-run with "
                  "--enable-inspection for composable names/params/file:line. WARNING: "
                  + stringsmod.ENABLE_INSPECTION_WARNING % "--enable-inspection", file=sys.stderr)
        roots = [w["root"] for w in data.get("windows", []) if w.get("root")]

        if args.json:
            text = json.dumps(data, indent=2)
            if args.json == "-":
                print(text)
            else:
                with open(args.json, "w") as f:
                    f.write(text)
                print(f"wrote compose JSON to {args.json}", file=sys.stderr)
        else:
            lines = []
            for r in roots:
                _compose_text_summary(r, lines)
            print("\n".join(lines) if lines else "(no on-screen compose text found)")

        if args.overlay:
            # Clean overlay = semantics nodes only (on-screen text labels).
            sem = stringsmod.dump_compose_to_dict(
                client.dump_compose(include_semantics=True, include_slot_table=False))
            sem_roots = [w["root"] for w in sem.get("windows", []) if w.get("root")]
            shot = client.screenshot(root_id=0, scale=args.scale)
            summary = _write_overlay(
                shot, args.overlay,
                lambda base, scale: ovmod.render_compose_overlay(
                    base, sem_roots, args.overlay, labeled_only=not args.all_boxes, scale=scale),
                args.scale)
            print(f"wrote compose overlay -> {args.overlay} "
                  f"({summary['boxes']} boxes, {summary['labels']} labels)", file=sys.stderr)
    return 0

# --------------------------------------------------------------------------- #

def cmd_a11y(args) -> int:
    from inspector_widget import a11y as a11ymod
    from inspector_widget import overlay as ovmod
    from inspector_widget import a11y_lint as lintmod
    with _session(args) as session:
        client = session.client
        data = a11ymod.a11y_to_dict(client.dump_a11y(
            root_id=0, include_extras=args.include_extras,
            include_rendering_info=args.include_rendering_info))
        if data.get("diagnostics"):
            print(f"a11y: {data['diagnostics']}", file=sys.stderr)

        if args.json:
            text = json.dumps(data, indent=2)
            if args.json == "-":
                print(text)
            else:
                with open(args.json, "w") as f:
                    f.write(text)
                print(f"wrote a11y JSON to {args.json}", file=sys.stderr)
        else:
            order = [e for e in data.get("focus_order", []) if e.get("is_focus_stop")]
            if not order:
                print("(no screen-reader focus stops found)")
            for e in order:
                b = e.get("bounds") or {}
                print(f"{e['order']:>3}. {e.get('speakable') or '<no label>'} "
                      f"({b.get('x',0)},{b.get('y',0)} {b.get('w',0)}x{b.get('h',0)})")

        if args.overlay:
            findings = None
            if args.lint:
                comp = stringsmod.dump_compose_to_dict(
                    client.dump_compose(include_semantics=True, include_slot_table=False))
                roots = [w["root"] for w in comp.get("windows", []) if w.get("root")]
                ctx = lintmod.LintContext(
                    density=adb.display_density(args.serial),
                    font_scale=adb.font_scale(args.serial),
                    wcag_mode=args.wcag)
                if not args.no_contrast:
                    shot0 = client.screenshot(root_id=0, scale=args.scale)
                    if shot0.HasField("screenshot"):
                        w, h, rgba = pngmod._decode_to_rgba(shot0.screenshot)
                        ctx.screenshot_rgba = rgba; ctx.screenshot_w = w; ctx.screenshot_h = h
                        ctx.screenshot_scale = float(shot0.screenshot.scale) or args.scale
                findings = [f.to_dict() for f in lintmod.lint_tree(roots, ctx)]
            shot = client.screenshot(root_id=0, scale=args.scale)
            summary = _write_overlay(
                shot, args.overlay,
                lambda base, scale: ovmod.render_a11y_overlay(
                    base, data, args.overlay, findings=findings, scale=scale),
                args.scale)
            print(f"wrote a11y overlay -> {args.overlay} "
                  f"({summary['boxes']} boxes, {summary['flagged']} flagged)", file=sys.stderr)
    return 0
def cmd_a11y_lint(args) -> int:
    from inspector_widget import a11y_lint as lintmod
    with _session(args) as session:
        client = session.client
        comp = stringsmod.dump_compose_to_dict(
            client.dump_compose(include_semantics=True, include_slot_table=False))
        roots = [w["root"] for w in comp.get("windows", []) if w.get("root")]
        ctx = lintmod.LintContext(
            density=adb.display_density(args.serial),
            font_scale=adb.font_scale(args.serial),
            wcag_mode=args.wcag)
        if not args.no_contrast:
            shot = client.screenshot(root_id=0, scale=args.scale)
            if shot.HasField("screenshot"):
                w, h, rgba = pngmod._decode_to_rgba(shot.screenshot)
                ctx.screenshot_rgba = rgba; ctx.screenshot_w = w; ctx.screenshot_h = h
                ctx.screenshot_scale = float(shot.screenshot.scale) or args.scale
        enabled = set(args.rules) if args.rules else None
        findings = lintmod.lint_tree(roots, ctx, enabled=enabled)
        summary = lintmod.summarize(findings)
        out = {"density": ctx.density, "font_scale": ctx.font_scale,
               "summary": summary, "findings": [f.to_dict() for f in findings]}
        if args.json:
            text = json.dumps(out, indent=2)
            if args.json == "-":
                print(text)
            else:
                with open(args.json, "w") as f:
                    f.write(text)
                print(f"wrote a11y-lint JSON to {args.json}", file=sys.stderr)
        else:
            print(f"density={ctx.density}dpi font_scale={ctx.font_scale} "
                  f"-> {summary['error']} error, {summary['warn']} warn, {summary['info']} info")
            for f in findings:
                bdp = f.bounds_dp
                print(f"[{f.severity.upper():5}] {f.rule} "
                      f"({bdp['x']},{bdp['y']} {bdp['w']}x{bdp['h']}dp) "
                      f"id={f.node.get('id')}: {f.message}")
        if args.overlay:
            from inspector_widget import a11y as a11ymod
            from inspector_widget import overlay as ovmod
            data = a11ymod.a11y_to_dict(client.dump_a11y(root_id=0, include_extras=True))
            shot2 = client.screenshot(root_id=0, scale=args.scale)
            _write_overlay(
                shot2, args.overlay,
                lambda base, scale: ovmod.render_a11y_overlay(
                    base, data, args.overlay,
                    findings=[f.to_dict() for f in findings], scale=scale),
                args.scale)
            print(f"wrote a11y-lint overlay -> {args.overlay}", file=sys.stderr)
    return 0

# --------------------------------------------------------------------------- #
# Integrated inspector subcommands (mirror the MCP tools: inspect / inspect_node /
# component_image / screenshot / get_properties / detach). These route through the
# high-level Session facade (inspector_widget.attach) rather than a raw Client.
# --------------------------------------------------------------------------- #
def _parse_bounds(spec):
    """Parse a 'x,y,w,h' selector string into a {x,y,w,h} dict (ints)."""
    if not spec:
        return None
    parts = [p.strip() for p in str(spec).split(",")]
    if len(parts) != 4:
        raise ValueError("--bounds must be 'x,y,w,h'")
    x, y, w, h = (int(p) for p in parts)
    return {"x": x, "y": y, "w": w, "h": h}


def _node_selector(args):
    """Resolve the shared selector group into (node_key, view_id, semantics_id, bounds).

    Mirrors the MCP guard: requires at least one selector. Returns the 4-tuple.
    """
    bounds = _parse_bounds(getattr(args, "bounds", None))
    node_key = getattr(args, "node_key", None)
    view_id = getattr(args, "view_id", None)
    semantics_id = getattr(args, "semantics_id", None)
    if not any(v is not None for v in (node_key, view_id, semantics_id, bounds)):
        raise SystemExit("error: provide one of --node-key, --view-id, --semantics-id, --bounds")
    return node_key, view_id, semantics_id, bounds


def _emit_json(obj, dest):
    """Write ``obj`` as pretty JSON to ``dest`` ('-' => stdout, path => file)."""
    text = json.dumps(obj, indent=2, default=str)
    if dest == "-":
        print(text)
    else:
        with open(dest, "w") as f:
            f.write(text)
        print(f"wrote JSON to {dest}", file=sys.stderr)


def cmd_inspect(args) -> int:
    from inspector_widget import correlate, overlay as ovmod
    with _session(args) as session:
        merged = correlate.inspect_tree(session, include_properties=args.properties)
        if args.overlay:
            shot = session.screenshot(root_id=0, scale=args.scale)
            summary = _write_overlay(
                shot, args.overlay,
                lambda base, scale: ovmod.render_integrated_overlay(
                    base, merged, args.overlay, scale=scale),
                args.scale)
            print(f"wrote integrated overlay -> {args.overlay} "
                  f"({summary.get('boxes')} boxes)", file=sys.stderr)
        if args.json:
            _emit_json({"roots": merged.get("roots", []),
                        "summary": merged.get("summary", {}),
                        "sources": merged.get("sources", {})}, args.json)
        else:
            print(json.dumps(merged.get("summary", {}), indent=2))
    return 0
def cmd_inspect_node(args) -> int:
    from inspector_widget import correlate, a11y_lint as lintmod
    node_key, view_id, semantics_id, bounds = _node_selector(args)
    with _session(args) as session:
        dossier = correlate.inspect_node(
            session, node_key=node_key, view_id=view_id,
            semantics_id=semantics_id, bounds=bounds,
            include_image=not args.no_image,
            lint_fn=lintmod.lint_a11y,
            density=adb.display_density(args.serial))
        if dossier is None:
            print("error: no matching element found for the given selector", file=sys.stderr)
            return 1
        if args.json:
            _emit_json(dossier, args.json)
        else:
            print(json.dumps(dossier, indent=2, default=str))
    return 0
def cmd_component_image(args) -> int:
    from inspector_widget import correlate
    node_key, view_id, semantics_id, bounds = _node_selector(args)
    with _session(args) as session:
        merged = correlate.inspect_tree(session, include_properties=False)
        node = correlate.find_node(merged, node_key=node_key, view_id=view_id,
                                   semantics_id=semantics_id, bounds=bounds)
        if node is None:
            print("error: no matching element found for the given selector", file=sys.stderr)
            return 1
        img = correlate.component_image(session, node, out_path=args.out, scale=args.scale)
        if img.get("path"):
            print(f"wrote component image -> {img['path']} (source={img.get('source')})",
                  file=sys.stderr)
        else:
            print(f"error: {img.get('error', 'component image failed')}", file=sys.stderr)
            return 1
        print(json.dumps(img, indent=2, default=str))
    return 0
def cmd_screenshot(args) -> int:
    with _session(args) as session:
        resp = session.screenshot(root_id=0, scale=args.scale)
        if not resp.HasField("screenshot"):
            print("error: agent returned no screenshot", file=sys.stderr)
            return 1
        w, h = pngmod.write_png(resp.screenshot, args.out)
        print(f"wrote screenshot {w}x{h} to {args.out}", file=sys.stderr)
    return 0
def cmd_get_properties(args) -> int:
    with _session(args) as session:
        resp = session.get_properties(
            args.view_id, include_resolution_stack=args.resolution_stack)
        data = stringsmod.get_properties_to_dict(resp)
        if args.json:
            _emit_json(data, args.json)
        else:
            print(json.dumps(data, indent=2, default=str))
    return 0
def cmd_detach(args) -> int:
    """Stop the agent in ``--package`` for every client. Never injects: with no
    agent running there is nothing to stop."""
    session = iw.connect_existing(args.serial, args.package)
    if session is None:
        print(f"no agent running in {args.package} on {args.serial}; nothing to detach",
              file=sys.stderr)
        return 0
    session.shutdown()
    print(f"detached {args.package} on {args.serial} (agent stopped)", file=sys.stderr)
    return 0
def _add_serial_arg(sp):
    sp.add_argument("--serial", default=None,
                    help="device serial (default: $ANDROID_SERIAL, else the only attached device)")


def _add_build_out_arg(sp):
    """Add --build-out to a subcommand that may inject the agent."""
    sp.add_argument(
        "--build-out", metavar="DIR", default=None,
        help="directory holding libviewspector.so, bootstrap.dex and payload.jar "
             "(default: $INSPECTOR_WIDGET_ARTIFACTS, else $VIEWSPECTOR_ARTIFACTS, "
             "else the checkout's build-out/)")


def _add_selector_args(sp):
    """Add the shared element-selector group used by inspect-node / component-image."""
    sp.add_argument("--node-key", help="'view:<uniqueDrawingId>' or 'compose:<semanticsId>'")
    sp.add_argument("--view-id", type=int, help="a view's uniqueDrawingId (the 'id' from dump)")
    sp.add_argument("--semantics-id", type=int, help="a Compose node's semantics id")
    sp.add_argument("--bounds", metavar="x,y,w,h",
                    help="absolute screen-px box; resolves to the deepest covering element")


# Argument parsing
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="inspector-widget",
        description="Inspector Widget — standalone Android View Layout Inspector host.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("devices", help="list attached devices")
    sp.set_defaults(func=cmd_devices)

    sp = sub.add_parser("packages", help="list debuggable packages")
    _add_serial_arg(sp)
    sp.set_defaults(func=cmd_packages)

    sp = sub.add_parser("attach", help="inject the agent and PING it")
    _add_serial_arg(sp)
    sp.add_argument("--package", default=DEFAULT_PACKAGE)
    sp.add_argument("--force", action="store_true", help="force re-injection")
    _add_build_out_arg(sp)
    sp.set_defaults(func=cmd_attach)

    sp = sub.add_parser("dump", help="inject + dump the view tree (one shot)")
    _add_serial_arg(sp)
    sp.add_argument("--package", default=DEFAULT_PACKAGE)
    sp.add_argument("--root-id", type=int, default=0, help="0 == all roots")
    sp.add_argument("--properties", action="store_true", help="inline view properties")
    sp.add_argument(
        "--resolution-stack",
        action="store_true",
        help="include attribute resolution stacks (implies --properties)",
    )
    sp.add_argument(
        "--screenshot",
        metavar="OUT.png",
        help="capture a BITMAP screenshot and write it to this PNG path",
    )
    sp.add_argument("--scale", type=float, default=1.0, help="screenshot scale (<=1.0)")
    sp.add_argument(
        "--json",
        metavar="OUT.json|-",
        help="emit resolved tree as JSON ('-' for stdout)",
    )
    sp.add_argument("--force", action="store_true", help="force re-injection")
    _add_build_out_arg(sp)
    sp.set_defaults(func=cmd_dump)

    sp = sub.add_parser("compose", help="inject + dump the Compose layer (semantics + slot table)")
    _add_serial_arg(sp)
    sp.add_argument("--package", default=DEFAULT_PACKAGE)
    sp.add_argument("--overlay", metavar="OUT.png",
                    help="render the Compose tree as labeled boxes over a screenshot")
    sp.add_argument("--json", metavar="OUT.json|-", help="emit resolved compose tree as JSON")
    sp.add_argument("--scale", type=float, default=1.0, help="screenshot scale for --overlay")
    sp.add_argument("--all-boxes", action="store_true", help="box every node, not just labeled ones")
    sp.add_argument("--no-slot-table", action="store_true", help="semantics only (skip slot table)")
    sp.add_argument("--enable-inspection", action="store_true",
                    help="hot-reload to populate the slot table (composable names, params, "
                         "file:line). DESTRUCTIVE: resets remember{} state in every composition")
    # Deprecated: inspection is now opt-in, so this is the default. Kept so old scripts still parse.
    sp.add_argument("--no-enable-inspection", action="store_true", help=argparse.SUPPRESS)
    sp.add_argument("--force", action="store_true", help="force re-injection")
    _add_build_out_arg(sp)
    sp.set_defaults(func=cmd_compose)


    sp = sub.add_parser("a11y", help="dump the unified accessibility tree (Views + Compose) + TalkBack reading order")
    _add_serial_arg(sp)
    sp.add_argument("--package", default=DEFAULT_PACKAGE)
    sp.add_argument("--json", metavar="OUT.json|-", help="emit the resolved a11y tree as JSON")
    sp.add_argument("--overlay", metavar="OUT.png",
                    help="render a11y nodes (box + speakable label + reading-order number) over a screenshot")
    sp.add_argument("--lint", action="store_true",
                    help="also run the a11y lint and color the overlay by finding severity")
    sp.add_argument("--scale", type=float, default=1.0, help="screenshot scale for --overlay")
    sp.add_argument("--no-contrast", action="store_true", help="skip the contrast (image) lint rule")
    sp.add_argument("--wcag", action="store_true", help="use WCAG target sizes (44dp) for the lint")
    sp.add_argument("--rendering-info", action="store_true", dest="include_rendering_info",
                    help="per-node refreshWithExtraData for layout/text size (costly)")
    sp.add_argument("--no-extras", action="store_false", dest="include_extras",
                    help="skip iterating each node's extras bundle (roleDescription, compose testTag/id)")
    sp.add_argument("--force", action="store_true", help="force re-injection")
    _add_build_out_arg(sp)
    sp.set_defaults(func=cmd_a11y)

    sp = sub.add_parser("a11y-lint", help="run the accessibility lint (R1..R12) over the Compose semantics tree")
    _add_serial_arg(sp)
    sp.add_argument("--package", default=DEFAULT_PACKAGE)
    sp.add_argument("--json", metavar="OUT.json|-", help="emit findings as JSON")
    sp.add_argument("--no-contrast", action="store_true", help="skip the contrast (image) rule")
    sp.add_argument("--wcag", action="store_true", help="use WCAG target sizes (44dp) instead of Material (48dp)")
    sp.add_argument("--scale", type=float, default=1.0, help="screenshot scale for the contrast sample")
    sp.add_argument("--rule", action="append", dest="rules", metavar="RULE_ID",
                    help="only run this rule id (repeatable); omit to run all")
    sp.add_argument("--overlay", metavar="OUT.png", help="also render a severity-colored overlay")
    sp.add_argument("--force", action="store_true", help="force re-injection")
    _add_build_out_arg(sp)
    sp.set_defaults(func=cmd_a11y_lint)

    # ----- integrated inspector subcommands (mirror the MCP tools) ----- #
    sp = sub.add_parser("inspect",
                        help="whole-screen integrated tree (View + Compose + a11y, correlated)")
    _add_serial_arg(sp)
    sp.add_argument("--package", default=DEFAULT_PACKAGE)
    sp.add_argument("--properties", action="store_true",
                    help="inline full view properties under each node's view.properties")
    sp.add_argument("--overlay", metavar="OUT.png",
                    help="render a labelled, color-coded integrated overlay PNG")
    sp.add_argument("--scale", type=float, default=1.0, help="screenshot scale for --overlay")
    sp.add_argument("--json", metavar="OUT.json|-", help="emit the merged tree as JSON")
    sp.add_argument("--force", action="store_true", help="force re-injection")
    _add_build_out_arg(sp)
    sp.set_defaults(func=cmd_inspect)

    sp = sub.add_parser("inspect-node",
                        help="full dossier for ONE element (facets + component image + focused lint)")
    _add_serial_arg(sp)
    sp.add_argument("--package", default=DEFAULT_PACKAGE)
    _add_selector_args(sp)
    sp.add_argument("--no-image", action="store_true", help="skip cutting the component image")
    sp.add_argument("--json", metavar="OUT.json|-", help="emit the dossier as JSON")
    sp.add_argument("--force", action="store_true", help="force re-injection")
    _add_build_out_arg(sp)
    sp.set_defaults(func=cmd_inspect_node)

    sp = sub.add_parser("component-image",
                        help="cut a per-component image for one element (SKP layer cut else BITMAP crop)")
    _add_serial_arg(sp)
    sp.add_argument("--package", default=DEFAULT_PACKAGE)
    _add_selector_args(sp)
    sp.add_argument("--out", metavar="OUT.png", help="output PNG path (default: a temp file)")
    sp.add_argument("--scale", type=float, default=1.0, help="component image scale")
    sp.add_argument("--force", action="store_true", help="force re-injection")
    _add_build_out_arg(sp)
    sp.set_defaults(func=cmd_component_image)

    sp = sub.add_parser("screenshot", help="capture a screenshot PNG of the app's current UI")
    _add_serial_arg(sp)
    sp.add_argument("--package", default=DEFAULT_PACKAGE)
    sp.add_argument("--out", metavar="OUT.png", required=True, help="output PNG path")
    sp.add_argument("--scale", type=float, default=1.0, help="screenshot scale (<=1.0)")
    sp.add_argument("--force", action="store_true", help="force re-injection")
    _add_build_out_arg(sp)
    sp.set_defaults(func=cmd_screenshot)

    sp = sub.add_parser("get-properties", help="fetch the full attribute set for a single view")
    _add_serial_arg(sp)
    sp.add_argument("--package", default=DEFAULT_PACKAGE)
    sp.add_argument("--view-id", type=int, required=True,
                    help="the view's uniqueDrawingId (the 'id' field from dump)")
    sp.add_argument("--resolution-stack", action="store_true",
                    help="include per-property source + style/layout resolution chain")
    sp.add_argument("--json", metavar="OUT.json|-", help="emit the properties as JSON")
    sp.add_argument("--force", action="store_true", help="force re-injection")
    _add_build_out_arg(sp)
    sp.set_defaults(func=cmd_get_properties)

    sp = sub.add_parser("detach", help="stop a running agent for every client (sends SHUTDOWN; "
                                       "never injects)")
    _add_serial_arg(sp)
    sp.add_argument("--package", default=DEFAULT_PACKAGE)
    sp.set_defaults(func=cmd_detach)

    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if hasattr(args, "serial"):
            args.serial = adb.resolve_serial(args.serial)
        return args.func(args)
    except Exception as e:  # surface a clean error to the shell
        # Set INSPECTOR_WIDGET_LOG=DEBUG (or =TRACE) to get the full traceback.
        level = os.environ.get("INSPECTOR_WIDGET_LOG") or os.environ.get("VIEWSPECTOR_LOG") or ""
        if level.upper() in ("DEBUG", "TRACE"):
            import traceback
            traceback.print_exc()
        print(f"error: {e}", file=sys.stderr)
        hint = getattr(e, "hint", None)
        if hint:
            print(f"hint: {hint}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
