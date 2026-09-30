#!/usr/bin/env python3
"""Regenerate ``LIBRARY_FILES`` in ``inspector_widget/normalize_defaults.py``.

The slot table reports composable call sites as ``File.kt:line``. A call whose
file belongs to the Jetpack Compose libraries is library code (hidden by
``dump_compose``'s ``user_code_only``); anything else is app code. This script
collects the Kotlin file basenames of the Compose runtime, ui, foundation,
animation, material and material3 artifacts that define composables:

* every ``.kt`` in a ``*-sources.jar`` that contains ``@Composable``;
* the ``SourceFile`` attribute of every class in an artifact's AAR/JAR whose
  constant pool references ``androidx/compose/runtime/Composer`` (the Compose
  compiler threads a Composer through every composable). This covers versions
  cached without sources jars.

Usage (prints the replacement ``_LIBRARY_FILES`` block)::

    python3 host/tests/gen_library_files.py [~/.gradle/caches/modules-2/files-2.1]

It only reads the local Gradle cache, so build an app that uses the Compose
versions you care about first. Not collected by pytest.
"""

from __future__ import annotations

import glob
import io
import os
import struct
import sys
import textwrap
import zipfile

GROUPS = ("androidx.compose.runtime", "androidx.compose.ui", "androidx.compose.foundation",
          "androidx.compose.animation", "androidx.compose.material",
          "androidx.compose.material3", "androidx.compose.material3.adaptive")
SKIP = ("icons", "test", "lint", "samples", "ios", "desktop", "jvmstubs", "linux", "macos",
        "js", "wasm")
COMPOSER = "Landroidx/compose/runtime/Composer;"


def _composable_source_file(cls: bytes):
    """SourceFile of a class that defines composables, else None."""
    f = io.BytesIO(cls)
    if f.read(4) != b"\xca\xfe\xba\xbe":
        return None
    f.read(4)
    count = struct.unpack(">H", f.read(2))[0]
    pool = [None] * count
    i = 1
    while i < count:
        tag = f.read(1)[0]
        if tag == 1:
            size = struct.unpack(">H", f.read(2))[0]
            pool[i] = f.read(size).decode("utf-8", "replace")
        elif tag in (7, 8, 16, 19, 20):
            f.read(2)
        elif tag in (3, 4, 9, 10, 11, 12, 17, 18):
            f.read(4)
        elif tag in (5, 6):
            f.read(8)
            i += 1
        elif tag == 15:
            f.read(3)
        else:
            return None
        i += 1
    if not any(isinstance(s, str) and COMPOSER in s for s in pool):
        return None
    f.read(6)
    f.read(2 * struct.unpack(">H", f.read(2))[0])
    for _ in range(2):  # fields, then methods
        for _ in range(struct.unpack(">H", f.read(2))[0]):
            f.read(6)
            for _ in range(struct.unpack(">H", f.read(2))[0]):
                f.read(2)
                f.read(struct.unpack(">I", f.read(4))[0])
    for _ in range(struct.unpack(">H", f.read(2))[0]):
        name = struct.unpack(">H", f.read(2))[0]
        data = f.read(struct.unpack(">I", f.read(4))[0])
        if pool[name] == "SourceFile":
            return pool[struct.unpack(">H", data)[0]]
    return None


def _classes(jar: zipfile.ZipFile):
    for n in jar.namelist():
        if n.endswith(".class"):
            try:
                yield _composable_source_file(jar.read(n))
            except (struct.error, IndexError):
                continue


def collect(cache: str) -> set:
    names = set()
    for group in GROUPS:
        for art in sorted(os.listdir(os.path.join(cache, group))) if os.path.isdir(
                os.path.join(cache, group)) else ():
            if any(s in art for s in SKIP):
                continue
            for path in glob.glob(os.path.join(cache, group, art, "*", "*", "*")):
                if path.endswith("-sources.jar") and "samples" not in path:
                    with zipfile.ZipFile(path) as z:
                        for n in z.namelist():
                            if n.endswith(".kt") and b"@Composable" in z.read(n):
                                names.add(os.path.basename(n))
                elif path.endswith(".aar"):
                    with zipfile.ZipFile(path) as z:
                        if "classes.jar" in z.namelist():
                            with zipfile.ZipFile(io.BytesIO(z.read("classes.jar"))) as cj:
                                names.update(n for n in _classes(cj) if n)
                elif path.endswith(".jar"):
                    with zipfile.ZipFile(path) as z:
                        names.update(n for n in _classes(z) if n)
    return {n[:-3] for n in names if n.endswith(".kt")}


def main(argv) -> int:
    cache = argv[1] if len(argv) > 1 else os.path.expanduser(
        "~/.gradle/caches/modules-2/files-2.1")
    names = sorted(collect(cache))
    body = "\n".join(textwrap.wrap(" ".join(names), width=92, break_long_words=False,
                                   break_on_hyphens=False))
    print(f'_LIBRARY_FILES = """\n{body}\n"""')
    print(f"# {len(names)} files", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
