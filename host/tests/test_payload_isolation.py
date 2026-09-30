"""The payload must not be shadowed by the target app's classes (device-free).

Bootstrap loads payload.jar in a DexClassLoader whose parent is the app's
classloader, and the parent is asked first. The Gradle build therefore relocates
the payload's Kotlin stdlib and protobuf-lite under com.oberkfell.viewspector.shaded
(agent/build.gradle.kts), so the payload defines only com.oberkfell.viewspector.*
classes. scripts/shadow_check.py verifies that from the dex; these tests cover the
checker on synthetic dex files, and run it on build-out/payload.jar (or
$INSPECTOR_WIDGET_PAYLOAD_JAR) when one is built, and against real APKs when
$INSPECTOR_WIDGET_SHADOW_APKS names them (APK files or directories, separated
by os.pathsep).
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import struct
import sys
import zipfile
from typing import List, Sequence, Tuple

import pytest

_HOST = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.dirname(_HOST)


def _load_checker():
    path = os.path.join(_REPO, "scripts", "shadow_check.py")
    spec = importlib.util.spec_from_file_location("iw_shadow_check", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(mod)
    return mod


sc = _load_checker()


# --------------------------------------------------------------- dex builder
def _uleb(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _sig_parts(sig: str) -> Tuple[List[str], str]:
    params, ret = sig[1:].split(")")
    out, i = [], 0
    while i < len(params):
        j = i
        while params[j] == "[":
            j += 1
        if params[j] == "L":
            j = params.index(";", j)
        out.append(params[i:j + 1])
        i = j + 1
    return out, ret


# A method body that exercises the scanner: wide literals and switch/array
# payloads whose data bytes look like invoke/sget opcodes must be skipped.
def _code_units(refs: Sequence[Tuple[bool, int]]) -> List[int]:
    # const-wide v0 with a literal made of invoke/sget opcode bytes.
    units = [0x0018, 0x716E, 0x6262, 0x7171, 0x6E6E]
    for is_method, idx in refs:
        # invoke-static {} method@idx, or sget-object v0 field@idx.
        units += [0x0071, idx, 0x0000] if is_method else [0x0062, idx]
    units.append(0x000E)  # return-void
    if len(units) % 2:
        units.append(0x0000)  # nop: payloads are 4-byte aligned
    units += [0x0100, 1, 0x0071, 0x0000, 0x0062, 0x0000]  # packed-switch-payload, 1 target
    units += [0x0200, 1, 0x0071, 0x0000, 0x006E, 0x0000]  # sparse-switch-payload, 1 entry
    units += [0x0300, 1, 3, 0, 0x6E71, 0x0062]  # fill-array-data-payload, 3 one-byte elements
    return units


class DexBuilder:
    """Writes the parts of a dex that scripts/shadow_check.py reads (no checksum, no map)."""

    def __init__(self) -> None:
        self.classes: List[dict] = []
        self.extra_types: List[str] = []

    def add_class(self, name: str, superclass: str = "Ljava/lang/Object;", interfaces=(),
                  fields=(), methods=()) -> "DexBuilder":
        """fields: (name, type); methods: (name, sig, refs).

        Each ref is ("m", class, name, sig) or ("f", class, name, type).
        """
        self.classes.append(dict(name=name, superclass=superclass, interfaces=list(interfaces),
                                 fields=list(fields), methods=list(methods)))
        return self

    def build(self) -> bytes:
        strings, types, protos, field_ids, method_ids = set(), set(), set(), set(), set()

        def typ(t):
            types.add(t)
            strings.add(t)

        def fld(c, n, t):
            typ(c)
            typ(t)
            strings.add(n)
            field_ids.add((c, n, t))

        def meth(c, n, s):
            typ(c)
            strings.add(n)
            params, ret = _sig_parts(s)
            for t in params + [ret]:
                typ(t)
            shorty = "".join("L" if t[0] in "L[" else t for t in [ret] + params)
            strings.add(shorty)
            protos.add((shorty, ret, tuple(params)))
            method_ids.add((c, n, s))

        for t in self.extra_types:
            typ(t)
        for c in self.classes:
            typ(c["name"])
            typ(c["superclass"])
            for i in c["interfaces"]:
                typ(i)
            for n, t in c["fields"]:
                fld(c["name"], n, t)
            for n, s, refs in c["methods"]:
                meth(c["name"], n, s)
                for kind, rc, rn, rt in refs:
                    (meth if kind == "m" else fld)(rc, rn, rt)

        s_list = sorted(strings)
        s_idx = {s: i for i, s in enumerate(s_list)}
        t_list = sorted(types)
        t_idx = {t: i for i, t in enumerate(t_list)}
        p_list = sorted(protos)
        p_idx = {(p[1], p[2]): i for i, p in enumerate(p_list)}
        f_list = sorted(field_ids)
        f_idx = {f: i for i, f in enumerate(f_list)}
        m_list = sorted(method_ids)
        m_idx = {m: i for i, m in enumerate(m_list)}

        off = 0x70
        layout = {}
        for key, size in (("strings", 4 * len(s_list)), ("types", 4 * len(t_list)),
                          ("protos", 12 * len(p_list)), ("fields", 8 * len(f_list)),
                          ("methods", 8 * len(m_list)), ("classes", 32 * len(self.classes))):
            layout[key] = off
            off += size
        data = bytearray(off)

        def append(blob: bytes, align: int = 1) -> int:
            while len(data) % align:
                data.append(0)
            at = len(data)
            data.extend(blob)
            return at

        for i, s in enumerate(s_list):
            at = append(_uleb(len(s)) + s.encode() + b"\0")
            struct.pack_into("<I", data, layout["strings"] + 4 * i, at)
        for i, t in enumerate(t_list):
            struct.pack_into("<I", data, layout["types"] + 4 * i, s_idx[t])

        def type_list(ts) -> int:
            if not ts:
                return 0
            return append(struct.pack(f"<I{len(ts)}H", len(ts), *(t_idx[t] for t in ts)), 4)

        for i, (shorty, ret, params) in enumerate(p_list):
            struct.pack_into("<3I", data, layout["protos"] + 12 * i,
                             s_idx[shorty], t_idx[ret], type_list(params))
        for i, (c, n, t) in enumerate(f_list):
            struct.pack_into("<HHI", data, layout["fields"] + 8 * i, t_idx[c], t_idx[t], s_idx[n])
        for i, (c, n, s) in enumerate(m_list):
            params, ret = _sig_parts(s)
            struct.pack_into("<HHI", data, layout["methods"] + 8 * i,
                             t_idx[c], p_idx[(ret, tuple(params))], s_idx[n])

        for ci, c in enumerate(self.classes):
            ifaces = type_list(c["interfaces"])
            fields = sorted(f_idx[(c["name"], n, t)] for n, t in c["fields"])
            methods = []
            for n, s, refs in c["methods"]:
                ids = [(True, m_idx[(rc, rn, rt)]) if kind == "m" else (False, f_idx[(rc, rn, rt)])
                       for kind, rc, rn, rt in refs]
                units = _code_units(ids)
                code = struct.pack(f"<4HII{len(units)}H", 1, 0, 0, 0, 0, len(units), *units)
                methods.append((m_idx[(c["name"], n, s)], append(code, 4)))
            methods.sort()
            blob = _uleb(len(fields)) + _uleb(0) + _uleb(len(methods)) + _uleb(0)
            prev = 0
            for f in fields:
                blob += _uleb(f - prev) + _uleb(1)
                prev = f
            prev = 0
            for m, code_off in methods:
                blob += _uleb(m - prev) + _uleb(9) + _uleb(code_off)
                prev = m
            class_data = append(blob)
            struct.pack_into("<8I", data, layout["classes"] + 32 * ci,
                             t_idx[c["name"]], 1, t_idx[c["superclass"]], ifaces,
                             0xFFFFFFFF, 0, class_data, 0)

        data[:8] = b"dex\n035\0"
        struct.pack_into("<12I", data, 0x38,
                         len(s_list), layout["strings"], len(t_list), layout["types"],
                         len(p_list), layout["protos"], len(f_list), layout["fields"],
                         len(m_list), layout["methods"], len(self.classes), layout["classes"])
        return bytes(data)


def _dex(builder: DexBuilder):
    return sc.parse_dex(builder.build(), scan_code=True)


PAYLOAD = "Lcom/oberkfell/viewspector/agent/payload/Payload;"
INTRINSICS = "Lkotlin/jvm/internal/Intrinsics;"
CHECK_PARAM = ("m", INTRINSICS, "checkNotNullParameter", "(Ljava/lang/Object;Ljava/lang/String;)V")
ARE_EQUAL = "(Ljava/lang/Object;Ljava/lang/Object;)Z"
UNIT_INSTANCE = ("f", "Lkotlin/Unit;", "INSTANCE", "Lkotlin/Unit;")
SHADED_INTRINSICS = "Lcom/oberkfell/viewspector/shaded/kotlin/jvm/internal/Intrinsics;"


def _app_with_shrunk_kotlin() -> DexBuilder:
    """An R8-shrunk app: Intrinsics keeps only what the app calls."""
    return (DexBuilder()
            .add_class(INTRINSICS, methods=[("areEqual", ARE_EQUAL, [])])
            .add_class("Lkotlin/collections/ArraysKt;")
            .add_class("Lkotlin/Unit;")
            .add_class("Landroid/view/inspector/InspectionCompanion;"))  # a bundled framework stub


# ------------------------------------------------------------ parser tests
def test_parser_reads_classes_members_and_code_references():
    dex = _dex(DexBuilder()
               .add_class("Lcom/example/Base;", fields=[("count", "I")])
               .add_class("Lcom/example/Impl;", superclass="Lcom/example/Base;",
                          interfaces=["Ljava/lang/Runnable;"],
                          fields=[("name", "Ljava/lang/String;")],
                          methods=[("run", "()V", [CHECK_PARAM, UNIT_INSTANCE]),
                                   ("size", "(I[Ljava/lang/String;)J", [])]))
    impl = dex.classes["Lcom/example/Impl;"]
    assert impl.superclass == "Lcom/example/Base;"
    assert impl.interfaces == ["Ljava/lang/Runnable;"]
    assert impl.members == {"name:Ljava/lang/String;", "run:()V", "size:(I[Ljava/lang/String;)J"}
    # Exactly the two references: nothing decoded out of the literal or the payload data.
    assert impl.refs == {
        (INTRINSICS, "checkNotNullParameter:(Ljava/lang/Object;Ljava/lang/String;)V"),
        ("Lkotlin/Unit;", "INSTANCE:Lkotlin/Unit;"),
    }
    assert dex.classes["Lcom/example/Base;"].members == {"count:I"}
    assert {INTRINSICS, "Lkotlin/Unit;", "Ljava/lang/Runnable;"} <= dex.types


def test_parse_rejects_non_dex():
    with pytest.raises(ValueError):
        sc.parse_dex(b"PK\x03\x04" + b"\0" * 0x70)


def test_load_archive_merges_multidex_in_runtime_order(tmp_path):
    first = DexBuilder().add_class("Lcom/example/A;", fields=[("first", "I")]).build()
    second = (DexBuilder().add_class("Lcom/example/A;", fields=[("second", "I")])
              .add_class("Lcom/example/B;").build())
    jar = tmp_path / "multi.apk"
    with zipfile.ZipFile(jar, "w") as z:
        z.writestr("classes10.dex", DexBuilder().add_class("Lcom/example/C;").build())
        z.writestr("classes2.dex", second)
        z.writestr("classes.dex", first)
    merged = sc.load_archive(str(jar))
    assert set(merged.classes) == {"Lcom/example/A;", "Lcom/example/B;", "Lcom/example/C;"}
    assert merged.classes["Lcom/example/A;"].members == {"first:I"}  # classes.dex wins


# ---------------------------------------------------------- analysis tests
def test_relocated_payload_has_no_findings():
    payload = _dex(DexBuilder()
                   .add_class(SHADED_INTRINSICS, methods=[(CHECK_PARAM[2], CHECK_PARAM[3], [])])
                   .add_class(PAYLOAD, methods=[("start", "(Ljava/lang/String;)V", [
                       ("m", SHADED_INTRINSICS, CHECK_PARAM[2], CHECK_PARAM[3]),
                       ("m", "Landroid/util/Log;", "i", "(Ljava/lang/String;Ljava/lang/String;)I"),
                       ("m", "Landroid/view/inspector/InspectionCompanion;", "mapProperties",
                        "(Landroid/view/inspector/PropertyMapper;)V"),
                   ])]))
    app = _dex(_app_with_shrunk_kotlin())
    assert sc.payload_report(payload) == {"foreign_classes": [], "unresolved_refs": []}
    assert sc.app_report(payload, app) == {"shadowed": [], "split_packages": [],
                                           "app_resolved_refs": [], "missing_members": []}


def test_unrelocated_payload_reports_shadowing_split_packages_and_missing_members():
    # The NiA R8 failure: the payload's own Intrinsics loses to the app's shrunk copy.
    payload = _dex(DexBuilder()
                   .add_class(INTRINSICS, methods=[(CHECK_PARAM[2], CHECK_PARAM[3], []),
                                                   ("areEqual", ARE_EQUAL, [])])
                   .add_class("Lkotlin/collections/CollectionsKt;")
                   .add_class(PAYLOAD, methods=[("start", "(Ljava/lang/String;)V", [
                       CHECK_PARAM,
                       ("m", INTRINSICS, "areEqual", ARE_EQUAL),
                   ])]))
    app = _dex(_app_with_shrunk_kotlin())
    assert sc.payload_report(payload)["foreign_classes"] == [
        "Lkotlin/collections/CollectionsKt;", INTRINSICS]
    report = sc.app_report(payload, app)
    assert report["shadowed"] == [INTRINSICS]
    assert report["split_packages"] == ["Lkotlin/collections/CollectionsKt;"]
    assert report["missing_members"] == [
        INTRINSICS + ".checkNotNullParameter:(Ljava/lang/Object;Ljava/lang/String;)V"]


def test_reference_the_payload_does_not_define_resolves_against_the_app():
    payload = _dex(DexBuilder().add_class(PAYLOAD, methods=[("start", "()V", [UNIT_INSTANCE])]))
    assert sc.payload_report(payload)["unresolved_refs"] == ["Lkotlin/Unit;"]
    report = sc.app_report(payload, _dex(_app_with_shrunk_kotlin()))
    assert report["app_resolved_refs"] == ["Lkotlin/Unit;"]
    assert report["missing_members"] == ["Lkotlin/Unit;.INSTANCE:Lkotlin/Unit;"]


def test_members_are_looked_up_through_the_app_hierarchy():
    payload = _dex(DexBuilder().add_class(PAYLOAD, methods=[("start", "()V", [
        ("m", "Lkotlin/collections/AbstractList;", "size", "()I"),
        ("m", "Lkotlin/collections/AbstractList;", "toString", "()Ljava/lang/String;"),
        ("m", "Lkotlin/text/Regex;", "find", "()V"),
    ])]))
    app = _dex(DexBuilder()
               .add_class("Lkotlin/collections/AbstractCollection;", methods=[("size", "()I", [])])
               .add_class("Lkotlin/collections/AbstractList;",
                          superclass="Lkotlin/collections/AbstractCollection;")
               # Its superclass is not in the APK: the platform is assumed to complete it.
               .add_class("Lkotlin/text/Regex;", superclass="Ljava/util/AbstractList;"))
    assert sc.app_report(payload, app)["missing_members"] == []


def test_main_exit_status_and_json(tmp_path, capsys):
    good = tmp_path / "good.jar"
    with zipfile.ZipFile(good, "w") as z:
        z.writestr("classes.dex", DexBuilder().add_class(PAYLOAD).build())
    bad = tmp_path / "bad.jar"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("classes.dex", DexBuilder().add_class(INTRINSICS).add_class(PAYLOAD).build())
    apps = tmp_path / "apps"
    apps.mkdir()
    with zipfile.ZipFile(apps / "app.apk", "w") as z:
        z.writestr("classes.dex", _app_with_shrunk_kotlin().build())

    assert sc.main(["--payload", str(good), str(apps)]) == 0
    assert "OK: the payload is isolated" in capsys.readouterr().out
    assert sc.main(["--payload", str(bad), "--json", str(apps)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["foreign_classes"] == [INTRINSICS]
    assert report["apps"]["app.apk"]["shadowed"] == [INTRINSICS]
    assert sc.main(["--payload", str(tmp_path / "missing.jar")]) == 2


# ------------------------------------------------------------ the real build
def test_gradle_build_relocates_the_bundled_libraries():
    with open(os.path.join(_REPO, "agent", "build.gradle.kts"), encoding="utf-8") as f:
        gradle = f.read()
    m = re.search(r"val payloadRelocatedPackages = listOf\((.*?)\)", gradle, re.S)
    assert m, "agent/build.gradle.kts lost payloadRelocatedPackages"
    packages = set(re.findall(r'"([^"]+)"', m.group(1)))
    assert {"kotlin/", "kotlinx/", "com/google/protobuf/", "org/jetbrains/annotations/",
            "org/intellij/lang/annotations/"} <= packages
    root = re.search(r'val payloadShadedRoot = "([^"]+)"', gradle)
    assert root and root.group(1).startswith("com/oberkfell/viewspector/")
    assert "ScopedArtifact.CLASSES" in gradle and "RelocatePayloadClassesTask" in gradle


def _payload_jar() -> str:
    return (os.environ.get("INSPECTOR_WIDGET_PAYLOAD_JAR")
            or os.path.join(_REPO, "build-out", "payload.jar"))


@pytest.fixture(scope="module")
def built_payload():
    path = _payload_jar()
    if not os.path.isfile(path):
        pytest.skip(f"no payload.jar at {path} (run scripts/build.sh)")
    payload = sc.load_archive(path, scan_code=True)
    if not any(c.startswith("Lcom/oberkfell/viewspector/shaded/") for c in payload.classes):
        pytest.skip(f"{path} predates the library relocation; rebuild with scripts/build.sh")
    return payload


def test_built_payload_defines_and_links_only_its_own_and_platform_classes(built_payload):
    assert sc.payload_report(built_payload) == {"foreign_classes": [], "unresolved_refs": []}
    # The relocated libraries are really there (not merely absent).
    for cls in ("Lcom/oberkfell/viewspector/shaded/kotlin/jvm/internal/Intrinsics;",
                "Lcom/oberkfell/viewspector/shaded/com/google/protobuf/GeneratedMessageLite;",
                "Lcom/oberkfell/viewspector/agent/payload/Payload;"):
        assert cls in built_payload.classes


def _shadow_apks() -> List[str]:
    spec = os.environ.get("INSPECTOR_WIDGET_SHADOW_APKS", "")
    paths = [p for p in spec.split(os.pathsep) if p and os.path.exists(p)]
    return sc.apk_paths(paths)


@pytest.mark.parametrize("apk", _shadow_apks() or [None],
                         ids=lambda p: os.path.basename(p) if p else "none")
def test_built_payload_is_not_shadowed_by_real_apps(built_payload, apk):
    if apk is None:
        pytest.skip("set INSPECTOR_WIDGET_SHADOW_APKS to APKs or directories of APKs")
    report = sc.app_report(built_payload, sc.load_archive(apk))
    assert report == {"shadowed": [], "split_packages": [],
                      "app_resolved_refs": [], "missing_members": []}, report
