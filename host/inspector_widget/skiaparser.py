"""Provision & launch Google's skiaparser (``SkiaParserServer``) — the native gRPC server that
turns a device's serialized Skia picture (SKP) into per-view images for the SKP screenshot path.

Distributable & self-contained (stdlib only): detects the host OS/arch, resolves the right package
from Google's PUBLIC SDK manifest (``dl.google.com/android/repository``), downloads it, verifies the
sha1 checksum, unzips into a managed cache, picks the server *version* that covers the device's SKP
version (via the bundled ``version-map.xml``), and launches it on a free port. **No Android Studio
and no Android SDK required** — though an existing SDK copy is reused if present.

Coverage (from version-map.xml): server 1 → SKP 56–73, server 2 → SKP 73–86, server 3 → SKP 82–109.
On Apple Silicon only server 3 ships a native aarch64 binary; older servers are x64 (run via Rosetta).
"""
from __future__ import annotations

import hashlib
import os
import platform
import socket
import stat
import subprocess
import time
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_BASE = "https://dl.google.com/android/repository/"
# Newest manifest first; all currently list the same skiaparser packages.
MANIFESTS = ["repository2-3.xml", "repository2-2.xml", "repository2-1.xml"]
SERVER_BIN = "skia-grpc-server"  # + ".exe" on Windows


class SkiaParserError(RuntimeError):
    pass


# --------------------------------------------------------------------------- host
def host_os_arch() -> Tuple[str, str]:
    """(os, arch) in the manifest's vocabulary: os ∈ {macosx,linux,windows}, arch ∈ {aarch64,x64}."""
    system = platform.system()
    osname = {"Darwin": "macosx", "Linux": "linux", "Windows": "windows"}.get(system)
    if osname is None:
        raise SkiaParserError(f"unsupported host OS: {system}")
    m = platform.machine().lower()
    arch = "aarch64" if m in ("arm64", "aarch64") else ("x64" if m in ("x86_64", "amd64") else m)
    return osname, arch


def default_cache_dir() -> Path:
    env = os.environ.get("INSPECTOR_WIDGET_SKIAPARSER_DIR") or os.environ.get("VIEWSPECTOR_SKIAPARSER_DIR")
    if env:
        return Path(env)
    return Path.home() / ".viewspector" / "skiaparser"


def _sdk_skiaparser_dir() -> Optional[Path]:
    """An existing Android SDK skiaparser dir (reuse a copy AS already downloaded), if any."""
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        v = os.environ.get(var)
        if v and (Path(v) / "skiaparser").is_dir():
            return Path(v) / "skiaparser"
    guess = Path.home() / "Library" / "Android" / "sdk" / "skiaparser"  # macOS default
    return guess if guess.is_dir() else None


# --------------------------------------------------------------------------- manifest
def _fetch(url: str, timeout: float = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "viewspector-skiaparser/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_manifest() -> Dict[int, Dict]:
    """Return {server_version:int -> {"revision":str, "archives":{(os,arch):{"url","size","sha1"}}}}."""
    last_err: Optional[Exception] = None
    for name in MANIFESTS:
        try:
            xml = _fetch(REPO_BASE + name)
            return _parse_manifest(xml)
        except Exception as e:  # try the next manifest
            last_err = e
    raise SkiaParserError(f"could not fetch any SDK manifest: {last_err}")


def _parse_manifest(xml: bytes) -> Dict[int, Dict]:
    root = ET.fromstring(xml)
    out: Dict[int, Dict] = {}
    for rp in root.iter("remotePackage"):
        path = rp.get("path", "")
        if not path.startswith("skiaparser;"):
            continue
        ver = int(path.split(";", 1)[1])
        rev = rp.find("revision")
        revision = (
            ".".join((rev.findtext(x) or "0") for x in ("major", "minor", "micro"))
            if rev is not None else "?"
        )
        archives: Dict[Tuple[str, str], Dict] = {}
        for ar in rp.iter("archive"):
            ho = ar.findtext("host-os") or ""
            ha = ar.findtext("host-arch") or ""  # empty on v1/v2 (x64 implied)
            comp = ar.find("complete")
            if comp is None:
                continue
            url = comp.findtext("url") or ""
            size = int(comp.findtext("size") or "0")
            sha1 = (comp.findtext("checksum") or "").strip().lower()
            archives[(ho, ha)] = {"url": url, "size": size, "sha1": sha1}
        out[ver] = {"revision": revision, "archives": archives}
    if not out:
        raise SkiaParserError("manifest contained no skiaparser packages")
    return out


def _pick_archive(pkg: Dict, osname: str, arch: str) -> Dict:
    archs = pkg["archives"]
    # exact host match
    if (osname, arch) in archs:
        return archs[(osname, arch)]
    # v1/v2 list arch as "" (x64). On aarch64, fall back to the x64 build (Rosetta on macOS).
    if (osname, "") in archs:
        return archs[(osname, "")]
    if arch == "aarch64" and (osname, "x64") in archs:
        return archs[(osname, "x64")]
    raise SkiaParserError(f"no skiaparser archive for {osname}/{arch}; have {list(archs)}")


# --------------------------------------------------------------------------- install
class SkiaParser:
    def __init__(self, cache_dir: Optional[Path] = None):
        self.cache_dir = Path(cache_dir) if cache_dir else default_cache_dir()
        self._manifest: Optional[Dict[int, Dict]] = None

    @property
    def manifest(self) -> Dict[int, Dict]:
        if self._manifest is None:
            self._manifest = fetch_manifest()
        return self._manifest

    def _install_root(self, server_version: int) -> Path:
        return self.cache_dir / str(server_version)

    def _binary(self, root: Path) -> Path:
        name = SERVER_BIN + (".exe" if platform.system() == "Windows" else "")
        # zip lays out files under a top-level "skiaparser/" dir.
        for cand in (root / name, root / "skiaparser" / name):
            if cand.exists():
                return cand
        return root / name

    def installed_binary(self, server_version: int) -> Optional[Path]:
        # 1) our managed cache
        b = self._binary(self._install_root(server_version))
        if b.exists():
            return b
        # 2) an existing Android SDK copy
        sdk = _sdk_skiaparser_dir()
        if sdk is not None:
            b2 = self._binary(sdk / str(server_version))
            if b2.exists():
                return b2
        return None

    def ensure(self, server_version: int, progress=None) -> Path:
        """Ensure server ``server_version`` is installed; download+verify if needed. Returns binary."""
        existing = self.installed_binary(server_version)
        if existing is not None:
            return self._prepare(existing)

        if server_version not in self.manifest:
            raise SkiaParserError(f"skiaparser;{server_version} not in manifest "
                                  f"(have {sorted(self.manifest)})")
        osname, arch = host_os_arch()
        ar = _pick_archive(self.manifest[server_version], osname, arch)
        root = self._install_root(server_version)
        root.mkdir(parents=True, exist_ok=True)
        zip_path = root / Path(ar["url"]).name

        if progress:
            progress(f"downloading {ar['url']} ({ar['size']/1e6:.1f} MB)…")
        data = _fetch(REPO_BASE + ar["url"], timeout=180)
        got = hashlib.sha1(data).hexdigest()
        if ar["sha1"] and got != ar["sha1"]:
            raise SkiaParserError(f"sha1 mismatch for {ar['url']}: expected {ar['sha1']}, got {got}")
        zip_path.write_bytes(data)
        if progress:
            progress(f"verified sha1, unzipping…")
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(root)
        zip_path.unlink(missing_ok=True)

        b = self._binary(root)
        if not b.exists():
            raise SkiaParserError(f"binary {SERVER_BIN} not found after unzip under {root}")
        return self._prepare(b)

    def _prepare(self, binary: Path) -> Path:
        # +x and clear macOS Gatekeeper quarantine so it launches unattended.
        try:
            binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        except OSError:
            pass
        if platform.system() == "Darwin":
            subprocess.run(["xattr", "-d", "com.apple.quarantine", str(binary)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return binary

    # ----------------------------------------------------------------- version map
    def version_map(self) -> List[Tuple[int, int, int]]:
        """[(server_version, skpStart, skpEnd)] from the newest installed/available version-map.xml."""
        # Ensure the newest server is present (its version-map lists all servers' ranges).
        newest = max(self.manifest)
        binary = self.ensure(newest)
        vm = binary.parent / "version-map.xml"
        if not vm.exists():
            raise SkiaParserError(f"version-map.xml missing next to {binary}")
        root = ET.parse(vm).getroot()
        rows = []
        for s in root.iter("server"):
            rows.append((int(s.get("version")), int(s.get("skpStart")), int(s.get("skpEnd"))))
        return sorted(rows)

    def server_for_skp(self, skp_version: int) -> int:
        """The newest server version whose [skpStart, skpEnd] covers ``skp_version`` (inclusive).

        Ranges overlap at the boundaries (e.g. server 3 = 82..109, server 2 = 73..86); both bounds
        are inclusive — verified empirically: skiaparser;3 decodes SKP 109 (its skpEnd). When more
        than one server matches we prefer the newest (highest-versioned) one.
        """
        rows = self.version_map()
        matches = [sv for sv, lo, hi in rows if lo <= skp_version <= hi]
        if matches:
            return max(matches)
        max_hi = max(hi for _, _, hi in rows)
        if skp_version > max_hi:
            raise SkiaParserError(
                f"SKP version {skp_version} is newer than any available skiaparser "
                f"(max supported {max_hi}). The device is too new for the published parser.")
        raise SkiaParserError(f"no skiaparser server covers SKP version {skp_version}")

    def ensure_for_skp(self, skp_version: int, progress=None) -> Path:
        """Provision (download if needed) the server that decodes ``skp_version``; return its binary."""
        sv = self.server_for_skp(skp_version)
        return self.ensure(sv, progress=progress)

    # ----------------------------------------------------------------- launch
    def launch(self, binary: Path, port: Optional[int] = None, startup_timeout: float = 10.0
               ) -> Tuple[subprocess.Popen, int]:
        """Start ``SkiaParserServer <port>`` and wait until it accepts connections. Returns (proc, port)."""
        if port is None:
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
        proc = subprocess.Popen([str(binary), str(port)],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        deadline = time.time() + startup_timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                out = proc.stdout.read().decode(errors="replace") if proc.stdout else ""
                raise SkiaParserError(f"skia-grpc-server exited early (code {proc.returncode}): {out}")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                    return proc, port
            except OSError:
                time.sleep(0.15)
        proc.terminate()
        raise SkiaParserError(f"skia-grpc-server did not start listening on :{port} within "
                              f"{startup_timeout}s")
