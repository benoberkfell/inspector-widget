"""Packaging guard: every subpackage of ``inspector_widget`` must be listed in
``[tool.setuptools] packages`` in pyproject.toml.

The list is explicit, so a new subpackage that isn't added to it is silently left
out of the wheel. Every test that imports from the checkout still passes, and the
import only fails after ``pip install``. ``inspector_widget.talkback`` nearly
shipped that way.
"""

from __future__ import annotations

import os
import tomllib

_HOST_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _source_packages() -> set:
    found = set()
    root = os.path.join(_HOST_DIR, "inspector_widget")
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        if "__init__.py" in filenames:
            rel = os.path.relpath(dirpath, _HOST_DIR)
            found.add(rel.replace(os.sep, "."))
    return found


def test_every_subpackage_ships_in_the_wheel() -> None:
    with open(os.path.join(_HOST_DIR, "pyproject.toml"), "rb") as f:
        declared = set(tomllib.load(f)["tool"]["setuptools"]["packages"])
    missing = sorted(_source_packages() - declared)
    assert not missing, (
        f"not in [tool.setuptools] packages, so not in the wheel: {missing}")
