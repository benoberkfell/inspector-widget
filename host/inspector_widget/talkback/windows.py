"""Windows of other apps over the app being inspected (G9 / L5, in part).

The agent sees only its own app's windows, so a system dialog over the app (the notification
permission request, the "Android App Compatibility" 16 KB dialog, any other app's activity
or overlay) is invisible to a capture: it would list the app's stops as on screen while
TalkBack reads the dialog. The window manager knows (``dumpsys window windows``): this
module reads its window list, top first, and finds the first window of another package,
shown above the app's own topmost window, that is no system chrome (status and navigation
bars, the IME, a splash screen, a screen decoration) and covers a fair part of the screen
or takes input focus.

:func:`parse` and :func:`covering` are pure (tests feed them recorded dumps);
:func:`foreign_cover` runs ``dumpsys`` through adb. :func:`token` / :func:`from_token` carry
the answer in a DumpA11yResponse's diagnostics (``capture/fetch.py`` adds it to the stored
accessibility tree, ``talkback/tree.py`` reads it), and :func:`message` / :func:`hint` word
it for errors and diagnostics.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

#: Window types that are system chrome, not something covering the app.
CHROME = frozenset({
    "NAVIGATION_BAR", "NAVIGATION_BAR_PANEL", "STATUS_BAR", "STATUS_BAR_ADDITIONAL",
    "STATUS_BAR_SUB_PANEL", "STATUS_BAR_PANEL", "NOTIFICATION_SHADE", "INPUT_METHOD",
    "INPUT_METHOD_DIALOG", "WALLPAPER", "SCREENSHOT", "ACCESSIBILITY_OVERLAY",
    "MAGNIFICATION_OVERLAY", "DOCK_DIVIDER", "POINTER", "SECURE_SYSTEM_OVERLAY",
    "APPLICATION_STARTING", "DRAG", "VOLUME_OVERLAY", "BOOT_PROGRESS",
    "ACCESSIBILITY_MAGNIFICATION_OVERLAY", "TRUSTED_APPLICATION_OVERLAY",
})
#: A covering window takes input focus or covers at least this share of the app's window.
COVER_AREA = 0.25
_WINDOW = re.compile(r"^  Window #\d+ Window\{\w+ u\d+ (.*)\}:\s*$")
_PACKAGE = re.compile(r"\bpackage=(\S+)")
_TYPE = re.compile(r"\bty=([A-Z_0-9]+)")
_FRAME = re.compile(r"\bframe=\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
_FOCUS = re.compile(r"mCurrentFocus=Window\{\w+ u\d+ (.*)\}")
_TOKEN = re.compile(r"foreign-window pkg=(\S+) type=(\S+) title=([^;]*)")


@dataclass
class Win:
    """One window of the window manager's list."""

    title: str
    package: str = ""
    type: str = ""
    visible: bool = False
    on_screen: bool = False
    frame: Tuple[int, int, int, int] = (0, 0, 0, 0)  # left, top, right, bottom

    @property
    def area(self) -> int:
        left, top, right, bottom = self.frame
        return max(0, right - left) * max(0, bottom - top)


def parse(text: str) -> Tuple[List[Win], Optional[str]]:
    """``dumpsys window windows`` (or ``dumpsys window``) as ``([Win] top first, the title of
    the window with input focus or None)``."""
    wins: List[Win] = []
    cur: Optional[Win] = None
    focus = None
    for line in (text or "").splitlines():
        m = _WINDOW.match(line)
        if m:
            cur = Win(m.group(1).strip())
            wins.append(cur)
            continue
        f = _FOCUS.search(line)
        if f and focus is None:
            focus = f.group(1).strip()
        if cur is None:
            continue
        s = line.strip()
        if s.startswith("mOwnerUid=") or s.startswith("mSession="):
            p = _PACKAGE.search(s)
            if p and not cur.package:
                cur.package = p.group(1)
        elif s.startswith("mAttrs=") and not cur.type:
            t = _TYPE.search(s)
            if t:
                cur.type = t.group(1)
        elif s.startswith("Frames:"):
            fr = _FRAME.search(s)
            if fr:
                cur.frame = tuple(int(x) for x in fr.groups())  # type: ignore[assignment]
        elif s.startswith("isOnScreen="):
            cur.on_screen = s.split("=", 1)[1].strip() == "true"
        elif s.startswith("isVisible="):
            cur.visible = s.split("=", 1)[1].strip() == "true"
    return wins, focus


def covering(wins: List[Win], package: str, focus: Optional[str] = None) -> Optional[Win]:
    """The first window of another package above ``package``'s topmost shown window that is
    no system chrome and takes input focus or covers at least :data:`COVER_AREA` of the
    app's window; None when the app is not shown or nothing covers it."""
    app = next((i for i, w in enumerate(wins) if w.package == package and w.visible
                and w.on_screen and w.type not in CHROME), None)
    if app is None:
        return None
    base = max(1, wins[app].area)
    for w in wins[:app]:
        if w.package == package or not (w.visible and w.on_screen) or w.type in CHROME:
            continue
        if w.title == focus or w.area >= COVER_AREA * base:
            return w
    return None


def foreign_cover(serial: str, package: str) -> Optional[Dict[str, Any]]:
    """What covers ``package`` on ``serial`` now (:func:`covering`), as ``{"package",
    "window", "type", "frame": [x, y, w, h], "focused"}``; None when nothing does or the
    window list cannot be read."""
    from .. import adb

    try:
        out = adb.shell(serial, "dumpsys window windows; dumpsys window | grep mCurrentFocus",
                        check=False)
    except Exception:  # noqa: BLE001 - no adb, no device: nothing known
        return None
    wins, focus = parse(out or "")
    w = covering(wins, package, focus)
    if w is None:
        return None
    left, top, right, bottom = w.frame
    return {"package": w.package, "window": w.title, "type": w.type,
            "frame": [left, top, right - left, bottom - top], "focused": w.title == focus}


def name(cover: Dict[str, Any]) -> str:
    """``com.google.android.permissioncontroller/…GrantPermissionsActivity``, or the package
    and the window type when the window has no activity name (a system dialog)."""
    win = str(cover.get("window") or "")
    if "/" in win:
        pkg, cls = win.split("/", 1)
        short = cls.rsplit(".", 1)[-1]
        return f"{pkg}/…{short}" if "." in cls else win
    return f"{cover.get('package')} ({cover.get('type') or 'window'})"


def message(cover: Dict[str, Any], package: str) -> str:
    return (f"covered by another app's window: {name(cover)}; TalkBack reads it, "
            f"not {package}")


def hint(cover: Dict[str, Any]) -> str:
    return "Dismiss it (BACK, or answer it on the device), then retry."


def token(cover: Dict[str, Any]) -> str:
    """The diagnostics token a stored accessibility tree carries for ``cover``."""
    title = str(cover.get("window") or "").replace(";", ",")
    return f"foreign-window pkg={cover.get('package')} type={cover.get('type')} title={title}"


def from_token(diagnostics: str) -> Optional[Dict[str, Any]]:
    """The cover a DumpA11yResponse's diagnostics carry (:func:`token`), or None."""
    m = _TOKEN.search(diagnostics or "")
    if m is None:
        return None
    return {"package": m.group(1), "type": m.group(2), "window": m.group(3).strip()}


__all__ = ["CHROME", "COVER_AREA", "Win", "covering", "foreign_cover", "from_token", "hint",
           "message", "name", "parse", "token"]
