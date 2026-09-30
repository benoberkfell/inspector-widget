#!/usr/bin/env python3
"""Classloader-isolation check for the agent payload (device-free, stdlib only).

Bootstrap loads payload.jar in a DexClassLoader whose parent is the target app's
classloader, and class loading is parent-first: for any class name the app also
defines, the app's copy wins. A payload that bundles the Kotlin stdlib or
protobuf-lite under their own names therefore runs against the app's versions,
which R8 may have shrunk to nothing the payload needs (NoSuchMethodError on the
first line of Payload.start), and its remaining classes share packages with the
app's copies (IllegalAccessError on package-private access). The build relocates
those libraries under com.oberkfell.viewspector.shaded (agent/build.gradle.kts).

This script reads dex files directly, so it needs neither the SDK nor a device.

Payload checks (always):
  foreign classes    payload classes defined outside the payload namespace; any
                     app defining the same name would replace them.
  unresolved refs    types the payload references but neither defines nor gets
                     from the platform (boot classpath); they resolve against
                     the app, or fail.

Per app (each APK given, or every *.apk in a directory given):
  shadowed           payload classes the app also defines (the app's copy wins).
  split packages     payload classes in a package the app also defines.
  app-resolved refs  payload references that resolve to an app class.
  missing members    members that payload code (in classes the app does not
                     replace) uses on shadowed or app-resolved classes, and
                     that the app's class hierarchy lacks (a hierarchy leaving
                     the APK is assumed to be completed by the platform).

Exit status: 0 when every count is zero, 1 when any is not, 2 on bad input.

Usage:
  scripts/shadow_check.py [--payload build-out/payload.jar] [--json] [APK_OR_DIR ...]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import struct
import sys
import zipfile
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

#: Every class the payload defines must live under this descriptor prefix.
PAYLOAD_NAMESPACE = "Lcom/oberkfell/viewspector/"

#: Descriptor prefixes served by the boot classpath. The parent-first chain asks
#: the boot loader before the app, so the app can never replace these.
PLATFORM_PREFIXES: Tuple[str, ...] = (
    "Landroid/",
    "Ljava/",
    "Ljavax/",
    "Ldalvik/",
    "Llibcore/",
    "Lsun/",
    "Lorg/json/",
    "Lorg/xml/",
    "Lorg/w3c/",
    "Lcom/android/internal/",
)

_DEX_NAME = re.compile(r"classes\d*\.dex")
_NO_INDEX = 0xFFFFFFFF


# --------------------------------------------------------------------------- dex
@dataclass
class DexClass:
    """One class_def: its superclass, interfaces and defined members ("name:type")."""

    name: str
    superclass: Optional[str]
    interfaces: List[str] = field(default_factory=list)
    members: Set[str] = field(default_factory=set)
    #: (class descriptor, "name:type") of every field/method its code uses (with scan_code).
    refs: Set[Tuple[str, str]] = field(default_factory=set)


@dataclass
class DexFile:
    """The parts of a dex file this check needs."""

    classes: Dict[str, DexClass]
    types: Set[str]  # every type descriptor in type_ids (defined or referenced)


def _uleb128(data: bytes, off: int) -> Tuple[int, int]:
    result = 0
    shift = 0
    while True:
        b = data[off]
        off += 1
        result |= (b & 0x7F) << shift
        if b < 0x80:
            return result, off
        shift += 7


# Dalvik instruction widths in 16-bit code units, by opcode (the Dalvik bytecode
# format table). Unused opcodes are one unit.
_WIDTH = [1] * 256
for _ops, _w in (
    ((0x02, 0x05, 0x08, 0x13, 0x15, 0x16, 0x19, 0x1A, 0x1C, 0x1F, 0x20, 0x22, 0x23, 0x29,
      0xFE, 0xFF), 2),
    ((0x03, 0x06, 0x09, 0x14, 0x17, 0x1B, 0x24, 0x25, 0x26, 0x2A, 0x2B, 0x2C, 0xFC, 0xFD), 3),
    ((0xFA, 0xFB), 4),
    ((0x18,), 5),
    (range(0x2D, 0x3E), 2),    # cmp*, if-test, if-testz
    (range(0x44, 0x6E), 2),    # aget/aput, iget/iput, sget/sput
    (range(0x6E, 0x73), 3),    # invoke-kind
    (range(0x74, 0x79), 3),    # invoke-kind/range
    (range(0x90, 0xB0), 2),    # binop
    (range(0xD0, 0xE3), 2),    # binop/lit16, binop/lit8
):
    for _op in _ops:
        _WIDTH[_op] = _w
_FIELD_OPS = frozenset(range(0x52, 0x6E))  # iget*/iput*/sget*/sput*: field index in unit 1
_METHOD_OPS = frozenset(list(range(0x6E, 0x73)) + list(range(0x74, 0x79)) + [0xFA, 0xFB])


def _code_refs(data: bytes, code_off: int) -> Iterable[Tuple[bool, int]]:
    """(is_method, index) for every field and method reference in one code_item."""
    (insns_size,) = struct.unpack_from("<I", data, code_off + 12)
    base = code_off + 16
    units = struct.unpack_from(f"<{insns_size}H", data, base)
    pc = 0
    while pc < insns_size:
        unit = units[pc]
        op = unit & 0xFF
        if op == 0 and unit != 0:  # switch / array-data payload pseudo-instructions
            if unit == 0x0100:
                pc += units[pc + 1] * 2 + 4
            elif unit == 0x0200:
                pc += units[pc + 1] * 4 + 2
            elif unit == 0x0300:
                width = units[pc + 1]
                (count,) = struct.unpack_from("<I", data, base + 2 * (pc + 2))
                pc += (count * width + 1) // 2 + 4
            else:
                pc += 1
            continue
        if op in _METHOD_OPS:
            yield True, units[pc + 1]
        elif op in _FIELD_OPS:
            yield False, units[pc + 1]
        pc += _WIDTH[op]


def parse_dex(data: bytes, scan_code: bool = False) -> DexFile:
    """Parse the ids and class defs of one dex; with scan_code, each class's member references."""
    if data[:4] != b"dex\n":
        raise ValueError("not a dex file")
    (string_ids_size, string_ids_off, type_ids_size, type_ids_off,
     proto_ids_size, proto_ids_off, field_ids_size, field_ids_off,
     method_ids_size, method_ids_off, class_defs_size, class_defs_off) = struct.unpack_from(
        "<12I", data, 0x38)

    strings: Dict[int, str] = {}

    def string(i: int) -> str:
        s = strings.get(i)
        if s is None:
            (off,) = struct.unpack_from("<I", data, string_ids_off + 4 * i)
            _, start = _uleb128(data, off)
            end = data.index(b"\0", start)
            # MUTF-8; class and member names are plain ASCII in practice.
            s = data[start:end].replace(b"\xc0\x80", b"\0").decode("utf-8", "replace")
            strings[i] = s
        return s

    types = [string(struct.unpack_from("<I", data, type_ids_off + 4 * i)[0])
             for i in range(type_ids_size)]

    def type_list(off: int) -> List[str]:
        if off == 0:
            return []
        (size,) = struct.unpack_from("<I", data, off)
        return [types[idx] for idx in struct.unpack_from(f"<{size}H", data, off + 4)]

    protos = []
    for i in range(proto_ids_size):
        _shorty, ret, params_off = struct.unpack_from("<3I", data, proto_ids_off + 12 * i)
        protos.append("(" + "".join(type_list(params_off)) + ")" + types[ret])

    fields = []
    for i in range(field_ids_size):
        cls, typ, name = struct.unpack_from("<HHI", data, field_ids_off + 8 * i)
        fields.append((types[cls], f"{string(name)}:{types[typ]}"))

    methods = []
    for i in range(method_ids_size):
        cls, proto, name = struct.unpack_from("<HHI", data, method_ids_off + 8 * i)
        methods.append((types[cls], f"{string(name)}:{protos[proto]}"))

    classes: Dict[str, DexClass] = {}
    for i in range(class_defs_size):
        (cls, _flags, sup, ifaces_off, _src, _annotations,
         data_off, _static_values) = struct.unpack_from("<8I", data, class_defs_off + 32 * i)
        dc = DexClass(types[cls], None if sup == _NO_INDEX else types[sup], type_list(ifaces_off))
        if data_off:
            off = data_off
            counts = []
            for _ in range(4):
                n, off = _uleb128(data, off)
                counts.append(n)
            for list_no, n in enumerate(counts):
                idx = 0
                for _ in range(n):
                    diff, off = _uleb128(data, off)
                    _access, off = _uleb128(data, off)
                    idx += diff
                    if list_no < 2:
                        dc.members.add(fields[idx][1])
                        continue
                    code_off, off = _uleb128(data, off)
                    dc.members.add(methods[idx][1])
                    if scan_code and code_off:
                        for is_method, ref in _code_refs(data, code_off):
                            dc.refs.add(methods[ref] if is_method else fields[ref])
        classes[dc.name] = dc

    return DexFile(classes, set(types))


def _dex_order(name: str) -> int:
    digits = name[len("classes"):-len(".dex")]
    return int(digits) if digits else 1


def load_archive(path: str, scan_code: bool = False) -> DexFile:
    """Merge every classesN.dex of an APK or dex-in-jar (or read a bare .dex)."""
    if path.endswith(".dex"):
        with open(path, "rb") as f:
            return parse_dex(f.read(), scan_code)
    merged = DexFile({}, set())
    with zipfile.ZipFile(path) as z:
        names = sorted((n for n in z.namelist() if _DEX_NAME.fullmatch(n)), key=_dex_order)
        if not names:
            raise ValueError(f"{path}: no classes*.dex")
        for n in names:
            dex = parse_dex(z.read(n), scan_code)
            for name, dc in dex.classes.items():
                merged.classes.setdefault(name, dc)  # the first dex wins, as at runtime
            merged.types |= dex.types
    return merged


# --------------------------------------------------------------------- analysis
def _element(desc: str) -> Optional[str]:
    """The class descriptor of a (possibly array) type, or None for primitives."""
    desc = desc.lstrip("[")
    return desc if desc.startswith("L") else None


def _package(desc: str) -> str:
    return desc[1:].rsplit("/", 1)[0] if "/" in desc else ""


def is_platform(desc: str, extra: Iterable[str] = ()) -> bool:
    return desc.startswith(PLATFORM_PREFIXES) or any(desc.startswith(p) for p in extra)


def payload_report(payload: DexFile, allow: Iterable[str] = ()) -> Dict[str, List[str]]:
    """Payload-only invariants: nothing foreign defined, nothing unresolved referenced."""
    allow = tuple(allow)
    foreign = sorted(c for c in payload.classes if not c.startswith(PAYLOAD_NAMESPACE))
    unresolved = sorted({
        e for e in (_element(t) for t in payload.types)
        if e and e not in payload.classes and not is_platform(e, allow)
    })
    return {"foreign_classes": foreign, "unresolved_refs": unresolved}


def _has_member(app: DexFile, cls: str, key: str) -> bool:
    todo, seen = [cls], set()
    while todo:
        c = todo.pop()
        if c in seen:
            continue
        seen.add(c)
        dc = app.classes.get(c)
        if dc is None:
            if c != "Ljava/lang/Object;":
                return True  # the hierarchy leaves the APK: assume the platform has it
            continue
        if key in dc.members:
            return True
        if dc.superclass:
            todo.append(dc.superclass)
        todo.extend(dc.interfaces)
    name = key.split(":", 1)[0]
    return name in {"<init>", "equals", "hashCode", "toString", "getClass", "clone",
                    "finalize", "notify", "notifyAll", "wait"}


def app_report(payload: DexFile, app: DexFile, allow: Iterable[str] = ()) -> Dict[str, List[str]]:
    """How the app's classes would shadow or leak into the payload under parent-first loading."""
    allow = tuple(allow)
    shadowed = sorted(c for c in payload.classes if c in app.classes and not is_platform(c, allow))
    app_packages = {_package(c) for c in app.classes}
    split = sorted(c for c in payload.classes
                   if c not in app.classes and _package(c) in app_packages)
    resolved = sorted({
        e for e in (_element(t) for t in payload.types)
        if e and e not in payload.classes and e in app.classes and not is_platform(e, allow)
    })
    # Code that still loads from the payload (its class is not shadowed), using members of
    # classes that now come from the app.
    from_app = set(shadowed) | set(resolved)
    missing = sorted({f"{c}.{key}"
                      for dc in payload.classes.values() if dc.name not in app.classes
                      for c, key in dc.refs
                      if c in from_app and not _has_member(app, c, key)})
    return {"shadowed": shadowed, "split_packages": split,
            "app_resolved_refs": resolved, "missing_members": missing}


def _group(descs: List[str], depth: int = 3) -> List[Tuple[str, int]]:
    """Counts by leading package segments (member strings group by their class)."""
    counts: Dict[str, int] = {}
    for d in descs:
        key = "/".join(d.split(";", 1)[0].lstrip("L").split("/")[:depth])
        counts[key] = counts.get(key, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def _print_section(label: str, items: List[str]) -> None:
    print(f"  {label}: {len(items)}")
    for pkg, n in _group(items)[:6]:
        print(f"    {pkg}: {n}")
    if items:
        print(f"    e.g. {items[:3]}")


def apk_paths(targets: List[str]) -> List[str]:
    out = []
    for t in targets:
        if os.path.isdir(t):
            out.extend(sorted(os.path.join(t, n) for n in os.listdir(t) if n.endswith(".apk")))
        else:
            out.append(t)
    return out


def main(argv: Optional[List[str]] = None) -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--payload", default=os.path.join(here, "..", "build-out", "payload.jar"),
                    help="payload.jar to check (default: build-out/payload.jar)")
    ap.add_argument("--allow-prefix", action="append", default=[], metavar="DESC",
                    help="extra descriptor prefix to treat as platform, e.g. Lorg/apache/http/")
    ap.add_argument("--json", action="store_true", help="print a JSON report")
    ap.add_argument("apks", nargs="*", help="APKs, or directories of APKs, to check against")
    args = ap.parse_args(argv)

    try:
        payload = load_archive(args.payload, scan_code=True)
        apps = [(p, load_archive(p)) for p in apk_paths(args.apks)]
    except (OSError, ValueError, zipfile.BadZipFile) as e:
        print(f"shadow_check: {e}", file=sys.stderr)
        return 2

    report = {"payload": os.path.normpath(args.payload),
              "payload_classes": len(payload.classes),
              **payload_report(payload, args.allow_prefix),
              "apps": {os.path.basename(p): app_report(payload, app, args.allow_prefix)
                       for p, app in apps}}
    problems = (len(report["foreign_classes"]) + len(report["unresolved_refs"])
                + sum(len(v) for a in report["apps"].values() for v in a.values()))

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"== payload {report['payload']} ({report['payload_classes']} classes)")
        _print_section("classes outside com/oberkfell/viewspector", report["foreign_classes"])
        _print_section("referenced types neither in the payload nor the platform",
                       report["unresolved_refs"])
        for name, a in report["apps"].items():
            print(f"== vs {name}")
            _print_section("payload classes shadowed by the app", a["shadowed"])
            _print_section("payload classes in split packages", a["split_packages"])
            _print_section("payload references resolved by the app", a["app_resolved_refs"])
            _print_section("members missing on the app's classes", a["missing_members"])
        print("OK: the payload is isolated" if problems == 0
              else f"FAIL: {problems} isolation problem(s)")
    return 0 if problems == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
