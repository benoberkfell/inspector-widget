"""Windows of other apps over the app being inspected (G9 / L5, in part).

The agent sees only its own app's windows, so a system dialog over the app (the notification
permission request, the "Android App Compatibility" 16 KB dialog, any other app's activity
or overlay) is invisible to a capture: it would list the app's stops as on screen while
TalkBack reads the dialog. The window manager knows (``dumpsys window windows``): this
module reads its window list, top first, and finds the first window of another package,
shown above the app's own topmost window on the same display, that is no system chrome
(status and navigation bars, the IME, a splash screen, a screen decoration) and lies over a
fair part of the app's window, or over some of it with input focus. A window beside the app
(the other half of a split screen) covers none of it, however large or focused.

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
#: A covering window lies over at least this share of the app's window (or over some of it
#: with input focus).
COVER_AREA = 0.25
_WINDOW = re.compile(r"^  Window #\d+ Window\{\w+ u\d+ (.*)\}:\s*$")
_PACKAGE = re.compile(r"\bpackage=(\S+)")
_TYPE = re.compile(r"\bty=([A-Z_0-9]+)")
_FRAME = re.compile(r"\bframe=\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
_FOCUS = re.compile(r"mCurrentFocus=Window\{\w+ u\d+ (.*)\}")
_DISPLAY = re.compile(r"\bmDisplayId=(\d+)")
_TOKEN = re.compile(r"foreign-window pkg=(\S+) type=(\S+)(?: focused=([01]))? title=([^;]*)")


@dataclass
class Win:
    """One window of the window manager's list."""

    title: str
    package: str = ""
    type: str = ""
    visible: bool = False
    on_screen: bool = False
    frame: Tuple[int, int, int, int] = (0, 0, 0, 0)  # left, top, right, bottom
    display: Optional[int] = None

    @property
    def area(self) -> int:
        left, top, right, bottom = self.frame
        return max(0, right - left) * max(0, bottom - top)

    def overlap(self, other: "Win") -> int:
        """The area this window's frame shares with ``other``'s."""
        a, b = self.frame, other.frame
        w = min(a[2], b[2]) - max(a[0], b[0])
        h = min(a[3], b[3]) - max(a[1], b[1])
        return max(0, w) * max(0, h)


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
        if s.startswith("mDisplayId=") and cur.display is None:
            d = _DISPLAY.match(s)
            if d:
                cur.display = int(d.group(1))
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
    """The first window of another package above ``package``'s topmost shown window, on its
    display, that is no system chrome and lies over at least :data:`COVER_AREA` of the
    app's window, or over some of it with input focus; None when the app is not shown or
    nothing covers it. A window beside the app (split screen, freeform) covers nothing of
    it, focused or not. When the app's frame is unknown (0 x 0), a window's own size
    decides, as it is all there is to go by."""
    app = next((i for i, w in enumerate(wins) if w.package == package and w.visible
                and w.on_screen and w.type not in CHROME), None)
    if app is None:
        return None
    a = wins[app]
    known = a.area > 0
    base = max(1, a.area)
    for w in wins[:app]:
        if w.package == package or not (w.visible and w.on_screen) or w.type in CHROME:
            continue
        if a.display is not None and w.display is not None and w.display != a.display:
            continue  # another screen (a second display, a fold's outer screen)
        over = w.overlap(a) if known else w.area
        if over >= COVER_AREA * base or (over > 0 and w.title == focus):
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


def system_dialog(cover: Dict[str, Any]) -> bool:
    """Whether ``cover`` is a window of the system itself that is no activity (package
    ``android``: the "Android App Compatibility" 16 KB dialog), answered with its button."""
    return cover.get("package") == "android" and "/" not in str(cover.get("window") or "")


def dismiss(cover: Dict[str, Any]) -> str:
    """How ``cover`` goes away, in a few words: a system dialog by its button (OK), an
    activity with input focus by BACK (the key goes to the focused window), anything else
    by closing it on the device (BACK would reach another window)."""
    if system_dialog(cover):
        return "its OK button dismisses it"
    if "/" in str(cover.get("window") or "") and cover.get("focused") is not False:
        return "BACK dismisses it"
    return "close it on the device"


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
    if system_dialog(cover):
        return "Answer it on the device (its OK button), then retry."
    if "/" in str(cover.get("window") or "") and cover.get("focused") is not False:
        return "Dismiss it (BACK, or answer it on the device), then retry."
    return "Close it on the device, then retry."


def token(cover: Dict[str, Any]) -> str:
    """The diagnostics token a stored accessibility tree carries for ``cover``."""
    title = str(cover.get("window") or "").replace(";", ",")
    focused = cover.get("focused")
    focus = "" if focused is None else f" focused={int(bool(focused))}"
    return (f"foreign-window pkg={cover.get('package')} type={cover.get('type')}{focus} "
            f"title={title}")


def from_token(diagnostics: str) -> Optional[Dict[str, Any]]:
    """The cover a DumpA11yResponse's diagnostics carry (:func:`token`), or None."""
    m = _TOKEN.search(diagnostics or "")
    if m is None:
        return None
    out: Dict[str, Any] = {"package": m.group(1), "type": m.group(2),
                           "window": m.group(4).strip()}
    if m.group(3) is not None:
        out["focused"] = m.group(3) == "1"
    return out


__all__ = ["CHROME", "COVER_AREA", "Win", "covering", "dismiss", "foreign_cover", "from_token",
           "hint", "message", "name", "parse", "system_dialog", "token"]
