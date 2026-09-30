#!/usr/bin/env python3
"""Inspector Widget host CLI.

Subcommands:
  devices                      list attached devices
  packages   --serial          list debuggable (run-as-able) packages
  attach     --serial --package  inject the agent and PING it (Hello)
  dump       --serial --package  inject + dump tree (+ optional props/screenshot/json)

The ``dump`` command does the whole inject + dump + render in one shot.

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

from inspector_widget import adb  # noqa: E402
from inspector_widget import inject as injectmod  # noqa: E402
from inspector_widget import png as pngmod  # noqa: E402
from inspector_widget import strings as stringsmod  # noqa: E402
from inspector_widget.client import Client  # noqa: E402

DEFAULT_SERIAL = adb.DEFAULT_SERIAL
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


def cmd_attach(args) -> int:
    inj = injectmod.inject_and_connect(
        serial=args.serial,
        package=args.package,
        build_out=args.build_out,
        force_reinject=args.force,
    )
    try:
        client = Client(inj.sock, owns_socket=False)
        hello = client.hello()
        warm = " (warm/reused)" if inj.warm else ""
        print(
            f"attached to {args.package} pid={inj.pid}{warm}: "
            f"agent {hello.agent_version}, API {hello.api_level}, abi {hello.abi}"
        )
        print(f"socket=@{inj.socket_name} forwarded tcp:{inj.local_port}")
    finally:
        inj.close()
    return 0


def cmd_dump(args) -> int:
    inj = injectmod.inject_and_connect(
        serial=args.serial,
        package=args.package,
        build_out=args.build_out,
        force_reinject=args.force,
    )
    try:
        client = Client(inj.sock, owns_socket=False)
        client.hello()

        want_screenshot = bool(args.screenshot)
        resp = client.dump_tree(
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
    finally:
        inj.close()
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
    inj = injectmod.inject_and_connect(
        serial=args.serial, package=args.package, build_out=args.build_out,
        force_reinject=args.force)
    try:
        client = Client(inj.sock, owns_socket=False)
        client.hello()
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
            base = args.overlay + ".base.png"
            pngmod.write_png(shot.screenshot, base)
            summary = ovmod.render_compose_overlay(
                base, sem_roots, args.overlay,
                labeled_only=not args.all_boxes,
                scale=(float(shot.screenshot.scale) or args.scale))
            os.remove(base)
            print(f"wrote compose overlay -> {args.overlay} "
                  f"({summary['boxes']} boxes, {summary['labels']} labels)", file=sys.stderr)
    finally:
        inj.close()
    return 0


# --------------------------------------------------------------------------- #

def cmd_a11y(args) -> int:
    from inspector_widget import a11y as a11ymod
    from inspector_widget import overlay as ovmod
    from inspector_widget import adb, a11y_lint as lintmod
    inj = injectmod.inject_and_connect(
        serial=args.serial, package=args.package, build_out=args.build_out,
        force_reinject=args.force)
    try:
        client = Client(inj.sock, owns_socket=False)
        client.hello()
        data = a11ymod.a11y_to_dict(client.dump_a11y(
            root_id=0, include_extras=args.include_extras or args.lint,
            include_rendering_info=args.include_rendering_info or args.lint))
        if data.get("diagnostics"):
            print(f"a11y: {data['diagnostics']}", file=sys.stderr)
        report = None
        if args.lint:
            report = lintmod.run_lint(
                client, density=adb.display_density(args.serial),
                font_scale=adb.font_scale(args.serial),
                include_contrast=not args.no_contrast, scale=args.scale,
                wcag_mode=args.wcag, a11y_data=data)
            data["lint"] = report.to_dict()
            s = report.summary
            print(f"a11y lint: {s['error']} error, {s['warn']} warn, {s['info']} info",
                  file=sys.stderr)

        if args.json:
            text = json.dumps(data, indent=2)
            if args.json == "-":
                print(text)
            else:
                with open(args.json, "w") as f:
                    f.write(text)
                print(f"wrote a11y JSON to {args.json}", file=sys.stderr)
        else:
            order = data.get("focus_order", [])
            if not order:
                print("(no screen-reader focus stops found)")
            for e in order:
                print(f"{e['order']:>3}. {e.get('speak') or '<no label>'}  [{e.get('key')}]")
            for diag in data.get("reading_order_diagnostics", []):
                print(f"a11y: reading order: {diag.get('message')}", file=sys.stderr)

        if args.overlay:
            findings = data["lint"]["findings"] if report is not None else None
            base = args.overlay + ".base.png"
            base_scale = ovmod.write_screen_png(client, data, base, scale=args.scale)
            summary = ovmod.render_a11y_overlay(
                base, data, args.overlay, findings=findings, scale=base_scale)
            os.remove(base)
            print(f"wrote a11y overlay -> {args.overlay} "
                  f"({summary['boxes']} boxes, {summary['flagged']} flagged, "
                  f"{summary['flagged_by_bounds']} by finding bounds)", file=sys.stderr)
    finally:
        inj.close()
    return 0


def cmd_a11y_lint(args) -> int:
    from inspector_widget import a11y_lint as lintmod
    from inspector_widget import adb
    try:
        rules = lintmod.resolve_rule_ids(args.rules)
    except lintmod.UnknownRuleError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    inj = injectmod.inject_and_connect(
        serial=args.serial, package=args.package, build_out=args.build_out,
        force_reinject=args.force)
    try:
        client = Client(inj.sock, owns_socket=False)
        client.hello()
        report = lintmod.run_lint(
            client, density=adb.display_density(args.serial),
            font_scale=adb.font_scale(args.serial),
            include_contrast=not args.no_contrast, scale=args.scale, wcag_mode=args.wcag,
            rules=rules, include_rendering_info=args.include_rendering_info)
        out = report.to_dict()
        if args.json:
            text = json.dumps(out, indent=2)
            if args.json == "-":
                print(text)
            else:
                with open(args.json, "w") as f:
                    f.write(text)
                print(f"wrote a11y-lint JSON to {args.json}", file=sys.stderr)
        else:
            print(lintmod.format_text(report))
        if args.overlay:
            from inspector_widget import overlay as ovmod
            base = args.overlay + ".base.png"
            base_scale = ovmod.write_screen_png(client, report.a11y_data, base, scale=args.scale)
            ov = ovmod.render_a11y_overlay(base, report.a11y_data, args.overlay,
                                           findings=out["findings"], scale=base_scale)
            os.remove(base)
            s = out["summary"]
            print(f"wrote a11y-lint overlay -> {args.overlay} ({ov['boxes']} boxes, "
                  f"{ov['flagged']} flagged; {s['error']} error, {s['warn']} warn, "
                  f"{s['info']} info)", file=sys.stderr)
    finally:
        inj.close()
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
    import inspector_widget as iw
    session = iw.attach(args.serial, args.package, build_out=args.build_out)
    try:
        merged = correlate.inspect_tree(session, include_properties=args.properties)
        if args.overlay:
            shot = session.screenshot(root_id=0, scale=args.scale)
            base = args.overlay + ".base.png"
            pngmod.write_png(shot.screenshot, base)
            summary = ovmod.render_integrated_overlay(
                base, merged, args.overlay,
                scale=(float(shot.screenshot.scale) or args.scale))
            os.remove(base)
            print(f"wrote integrated overlay -> {args.overlay} "
                  f"({summary.get('boxes')} boxes)", file=sys.stderr)
        if args.json:
            _emit_json({"roots": merged.get("roots", []),
                        "summary": merged.get("summary", {}),
                        "sources": merged.get("sources", {})}, args.json)
        else:
            print(json.dumps(merged.get("summary", {}), indent=2))
    finally:
        session.detach()
    return 0


def cmd_inspect_node(args) -> int:
    from inspector_widget import correlate, a11y_lint as lintmod
    import inspector_widget as iw
    node_key, view_id, semantics_id, bounds = _node_selector(args)
    session = iw.attach(args.serial, args.package, build_out=args.build_out)
    try:
        try:
            dossier = correlate.inspect_node(
                session, node_key=node_key, view_id=view_id,
                semantics_id=semantics_id, bounds=bounds,
                include_image=not args.no_image,
                lint_fn=lintmod.lint_a11y,
                density=adb.display_density(args.serial))
        except correlate.NodeKeyError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if dossier is None:
            print("error: no matching element found for the given selector", file=sys.stderr)
            return 1
        if args.json:
            _emit_json(dossier, args.json)
        else:
            print(json.dumps(dossier, indent=2, default=str))
    finally:
        session.detach()
    return 0


def cmd_component_image(args) -> int:
    from inspector_widget import correlate
    import inspector_widget as iw
    node_key, view_id, semantics_id, bounds = _node_selector(args)
    session = iw.attach(args.serial, args.package, build_out=args.build_out)
    try:
        merged = correlate.inspect_tree(session, include_properties=False)
        try:
            node = correlate.find_node(merged, node_key=node_key, view_id=view_id,
                                       semantics_id=semantics_id, bounds=bounds)
        except correlate.NodeKeyError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
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
    finally:
        session.detach()
    return 0


def cmd_screenshot(args) -> int:
    import inspector_widget as iw
    session = iw.attach(args.serial, args.package, build_out=args.build_out)
    try:
        resp = session.screenshot(root_id=0, scale=args.scale)
        if not resp.HasField("screenshot"):
            print("error: agent returned no screenshot", file=sys.stderr)
            return 1
        w, h = pngmod.write_png(resp.screenshot, args.out)
        print(f"wrote screenshot {w}x{h} to {args.out}", file=sys.stderr)
    finally:
        session.detach()
    return 0


def cmd_get_properties(args) -> int:
    import inspector_widget as iw
    session = iw.attach(args.serial, args.package, build_out=args.build_out)
    try:
        resp = session.get_properties(
            args.view_id, include_resolution_stack=args.resolution_stack)
        data = stringsmod.get_properties_to_dict(resp)
        if args.json:
            _emit_json(data, args.json)
        else:
            print(json.dumps(data, indent=2, default=str))
    finally:
        session.detach()
    return 0


def cmd_detach(args) -> int:
    import inspector_widget as iw
    session = iw.attach(args.serial, args.package, build_out=args.build_out)
    session.detach()
    print(f"detached {args.package} on {args.serial}", file=sys.stderr)
    return 0


def _add_build_out_arg(sp):
    """Add --build-out to a subcommand that may inject the agent."""
    sp.add_argument(
        "--build-out", metavar="DIR", default=None,
        help="directory holding libviewspector.so, bootstrap.dex and payload.jar "
             "(default: $INSPECTOR_WIDGET_ARTIFACTS, else $VIEWSPECTOR_ARTIFACTS, "
             "else the checkout's build-out/)")


def _add_selector_args(sp):
    """Add the shared element-selector group used by inspect-node / component-image."""
    sp.add_argument("--node-key", help="'view:<uniqueDrawingId>', 'compose:<acvId>:<semanticsId>' "
                                       "or 'composeview:<acvId>' (from inspect / a11y)")
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
    sp.add_argument("--serial", default=DEFAULT_SERIAL)
    sp.set_defaults(func=cmd_packages)

    sp = sub.add_parser("attach", help="inject the agent and PING it")
    sp.add_argument("--serial", default=DEFAULT_SERIAL)
    sp.add_argument("--package", default=DEFAULT_PACKAGE)
    sp.add_argument("--force", action="store_true", help="force re-injection")
    _add_build_out_arg(sp)
    sp.set_defaults(func=cmd_attach)

    sp = sub.add_parser("dump", help="inject + dump the view tree (one shot)")
    sp.add_argument("--serial", default=DEFAULT_SERIAL)
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
    sp.add_argument("--serial", default=DEFAULT_SERIAL)
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
    sp.add_argument("--serial", default=DEFAULT_SERIAL)
    sp.add_argument("--package", default=DEFAULT_PACKAGE)
    sp.add_argument("--json", metavar="OUT.json|-", help="emit the resolved a11y tree as JSON")
    sp.add_argument("--overlay", metavar="OUT.png",
                    help="render a11y nodes (box + speakable label + reading-order number) over a screenshot")
    sp.add_argument("--lint", action="store_true",
                    help="also run the a11y lint (adds a 'lint' key to --json, colors --overlay "
                         "by severity); implies --rendering-info")
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

    sp = sub.add_parser("a11y-lint",
                        help="run the accessibility lint (R1..R18) over the unified a11y tree "
                             "(Views + Compose)")
    sp.add_argument("--serial", default=DEFAULT_SERIAL)
    sp.add_argument("--package", default=DEFAULT_PACKAGE)
    sp.add_argument("--json", metavar="OUT.json|-", help="emit findings as JSON")
    sp.add_argument("--no-contrast", action="store_true", help="skip the contrast (image) rule")
    sp.add_argument("--wcag", action="store_true", help="use WCAG target sizes (44dp) instead of Material (48dp)")
    sp.add_argument("--scale", type=float, default=1.0, help="screenshot scale for the contrast sample")
    sp.add_argument("--rule", action="append", dest="rules", metavar="RULE_ID",
                    help="only run this rule (repeatable): an id like a11y.label.missing, an "
                         "alias R1..R18, or an ATF name like TouchTargetSize; omit to run all")
    sp.add_argument("--no-rendering-info", action="store_false", dest="include_rendering_info",
                    help="skip per-node ExtraRenderingInfo (disables the text-size rules R11/R18 "
                         "and text-size-aware contrast)")
    sp.add_argument("--overlay", metavar="OUT.png", help="also render a severity-colored overlay")
    sp.add_argument("--force", action="store_true", help="force re-injection")
    _add_build_out_arg(sp)
    sp.set_defaults(func=cmd_a11y_lint)

    # ----- integrated inspector subcommands (mirror the MCP tools) ----- #
    sp = sub.add_parser("inspect",
                        help="whole-screen integrated tree (View + Compose + a11y, correlated)")
    sp.add_argument("--serial", default=DEFAULT_SERIAL)
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
    sp.add_argument("--serial", default=DEFAULT_SERIAL)
    sp.add_argument("--package", default=DEFAULT_PACKAGE)
    _add_selector_args(sp)
    sp.add_argument("--no-image", action="store_true", help="skip cutting the component image")
    sp.add_argument("--json", metavar="OUT.json|-", help="emit the dossier as JSON")
    sp.add_argument("--force", action="store_true", help="force re-injection")
    _add_build_out_arg(sp)
    sp.set_defaults(func=cmd_inspect_node)

    sp = sub.add_parser("component-image",
                        help="cut a per-component image for one element (SKP layer cut else BITMAP crop)")
    sp.add_argument("--serial", default=DEFAULT_SERIAL)
    sp.add_argument("--package", default=DEFAULT_PACKAGE)
    _add_selector_args(sp)
    sp.add_argument("--out", metavar="OUT.png", help="output PNG path (default: a temp file)")
    sp.add_argument("--scale", type=float, default=1.0, help="component image scale")
    sp.add_argument("--force", action="store_true", help="force re-injection")
    _add_build_out_arg(sp)
    sp.set_defaults(func=cmd_component_image)

    sp = sub.add_parser("screenshot", help="capture a screenshot PNG of the app's current UI")
    sp.add_argument("--serial", default=DEFAULT_SERIAL)
    sp.add_argument("--package", default=DEFAULT_PACKAGE)
    sp.add_argument("--out", metavar="OUT.png", required=True, help="output PNG path")
    sp.add_argument("--scale", type=float, default=1.0, help="screenshot scale (<=1.0)")
    sp.add_argument("--force", action="store_true", help="force re-injection")
    _add_build_out_arg(sp)
    sp.set_defaults(func=cmd_screenshot)

    sp = sub.add_parser("get-properties", help="fetch the full attribute set for a single view")
    sp.add_argument("--serial", default=DEFAULT_SERIAL)
    sp.add_argument("--package", default=DEFAULT_PACKAGE)
    sp.add_argument("--view-id", type=int, required=True,
                    help="the view's uniqueDrawingId (the 'id' field from dump)")
    sp.add_argument("--resolution-stack", action="store_true",
                    help="include per-property source + style/layout resolution chain")
    sp.add_argument("--json", metavar="OUT.json|-", help="emit the properties as JSON")
    sp.add_argument("--force", action="store_true", help="force re-injection")
    _add_build_out_arg(sp)
    sp.set_defaults(func=cmd_get_properties)

    sp = sub.add_parser("detach", help="shut down the agent session for an app (sends shutdown)")
    sp.add_argument("--serial", default=DEFAULT_SERIAL)
    sp.add_argument("--package", default=DEFAULT_PACKAGE)
    _add_build_out_arg(sp)
    sp.set_defaults(func=cmd_detach)

    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as e:  # surface a clean error to the shell
        # Set INSPECTOR_WIDGET_LOG=DEBUG (or =TRACE) to get the full traceback.
        if os.environ.get("INSPECTOR_WIDGET_LOG", "").upper() in ("DEBUG", "TRACE"):
            import traceback
            traceback.print_exc()
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
