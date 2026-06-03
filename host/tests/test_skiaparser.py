"""Tests for inspector_widget.skiaparser — pure logic only (NO downloads).

Covers:
  * server_for_skp boundary selection (inclusive skpEnd; newest wins on overlap;
    too-new -> SkiaParserError) by monkeypatching version_map().
  * host_os_arch mapping.
  * _pick_archive exact match, v1/v2 empty-arch ("" == x64), and the
    aarch64 -> x64 Rosetta fallback.
"""

from __future__ import annotations

import pytest

from inspector_widget import skiaparser
from inspector_widget.skiaparser import SkiaParser, SkiaParserError, _pick_archive


# Real coverage ranges from version-map.xml (server -> SKP skpStart..skpEnd):
#   server 1 -> 56..73, server 2 -> 73..86, server 3 -> 82..109.
VERSION_MAP_ROWS = [(1, 56, 73), (2, 73, 86), (3, 82, 109)]


@pytest.fixture
def parser(monkeypatch):
    p = SkiaParser(cache_dir="/tmp/does-not-matter")
    monkeypatch.setattr(p, "version_map", lambda: list(VERSION_MAP_ROWS))
    return p


# --------------------------------------------------------------------------- #
# server_for_skp boundary logic
# --------------------------------------------------------------------------- #
def test_server_for_skp_picks_unique_range(parser):
    assert parser.server_for_skp(60) == 1   # only server 1 covers 56..73
    assert parser.server_for_skp(100) == 3  # only server 3 covers 82..109


def test_server_for_skp_inclusive_skp_end(parser):
    # skpEnd is inclusive: SKP 109 is the last value server 3 covers.
    assert parser.server_for_skp(109) == 3


def test_server_for_skp_overlap_prefers_newest(parser):
    # 73 is covered by both server 1 (..73) and server 2 (73..) -> newest (2).
    assert parser.server_for_skp(73) == 2
    # 82..86 covered by both server 2 and server 3 -> newest (3).
    assert parser.server_for_skp(84) == 3
    assert parser.server_for_skp(86) == 3


def test_server_for_skp_too_new_raises(parser):
    with pytest.raises(SkiaParserError) as ei:
        parser.server_for_skp(200)
    msg = str(ei.value).lower()
    assert "newer" in msg or "too new" in msg


def test_server_for_skp_below_range_raises(parser):
    with pytest.raises(SkiaParserError) as ei:
        parser.server_for_skp(10)  # below every skpStart but not > max_hi
    assert "no skiaparser server covers" in str(ei.value).lower()


# --------------------------------------------------------------------------- #
# host_os_arch
# --------------------------------------------------------------------------- #
def test_host_os_arch_macos_arm(monkeypatch):
    monkeypatch.setattr(skiaparser.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(skiaparser.platform, "machine", lambda: "arm64")
    assert skiaparser.host_os_arch() == ("macosx", "aarch64")


def test_host_os_arch_linux_x64(monkeypatch):
    monkeypatch.setattr(skiaparser.platform, "system", lambda: "Linux")
    monkeypatch.setattr(skiaparser.platform, "machine", lambda: "x86_64")
    assert skiaparser.host_os_arch() == ("linux", "x64")


def test_host_os_arch_unsupported_os(monkeypatch):
    monkeypatch.setattr(skiaparser.platform, "system", lambda: "Plan9")
    monkeypatch.setattr(skiaparser.platform, "machine", lambda: "x86_64")
    with pytest.raises(SkiaParserError):
        skiaparser.host_os_arch()


# --------------------------------------------------------------------------- #
# _pick_archive
# --------------------------------------------------------------------------- #
def _pkg(archives):
    return {"revision": "1", "archives": archives}


def test_pick_archive_exact_match():
    pkg = _pkg({("macosx", "aarch64"): {"url": "a"}, ("macosx", "x64"): {"url": "b"}})
    assert _pick_archive(pkg, "macosx", "aarch64")["url"] == "a"


def test_pick_archive_empty_arch_means_x64():
    # v1/v2 list arch as "" (x64 implied). An x64 host should match it.
    pkg = _pkg({("linux", ""): {"url": "legacy"}})
    assert _pick_archive(pkg, "linux", "x64")["url"] == "legacy"


def test_pick_archive_aarch64_falls_back_to_x64():
    # No native aarch64 build -> fall back to the x64 archive (Rosetta on macOS).
    pkg = _pkg({("macosx", "x64"): {"url": "x64build"}})
    assert _pick_archive(pkg, "macosx", "aarch64")["url"] == "x64build"


def test_pick_archive_no_match_raises():
    pkg = _pkg({("windows", "x64"): {"url": "w"}})
    with pytest.raises(SkiaParserError):
        _pick_archive(pkg, "macosx", "aarch64")
