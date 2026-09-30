"""Unified-tree a11y lint: every rule proven with a violating fixture and a passing
counter-example.

Fixtures are shaped like ``a11y.a11y_to_dict`` output and follow the host ID
contract:

* real View:   host_view_id = its uniqueDrawingId, virtual_id = -1   -> view:<id>
* Compose node: host_view_id = the AndroidComposeView id, virtual_id = semantics id
                                                                     -> compose:<acv>:<sem>
* node "id" = (host_view_id << 32) ^ (virtual_id & 0xFFFFFFFF); traversal/label
  linkage uses that id.

Density 160 makes 1px == 1dp, so bounds read directly as dp.
"""

from __future__ import annotations

import struct
import time
import zlib
from typing import Any, Dict, List, Optional, Sequence

import pytest

from inspector_widget import a11y_lint as L
from inspector_widget.proto import view_inspection_pb2 as pb

VIS = ("visible_to_user", "enabled")
ACV = "androidx.compose.ui.platform.AndroidComposeView"


# --------------------------------------------------------------------------- #
# Fixture builders.
# --------------------------------------------------------------------------- #
def _node(hv: int, vid: int, cls: str, *, text=None, cd=None, hint=None, state=None,
          flags: Sequence[str] = (), b=(100, 100, 160, 160), kids: Sequence[dict] = (),
          actions: Optional[Sequence[str]] = None, visible=True, **extra) -> Dict[str, Any]:
    fl = (list(VIS) if visible else ["enabled"]) + list(flags)
    if vid != -1:
        fl.append("is_virtual")
    d: Dict[str, Any] = {
        "host_view_id": hv, "virtual_id": vid, "id": L.a11y_node_id(hv, vid),
        "bounds": {"layout": {"x": b[0], "y": b[1], "w": b[2], "h": b[3]}},
        "class_name": cls, "flags": fl,
    }
    for k, v in (("text", text), ("content_description", cd), ("hint_text", hint),
                 ("state_description", state)):
        if v is not None:
            d[k] = v
    if actions is None:
        actions = []
        if "clickable" in flags:
            actions.append("CLICK")
        if "long_clickable" in flags:
            actions.append("LONG_CLICK")
    if actions:
        d["actions"] = [{"id": 0, "name": a} for a in actions]
    if kids:
        d["children"] = list(kids)
    d.update(extra)
    return d


def view(hv: int, cls: str = "android.view.View", **kw) -> Dict[str, Any]:
    return _node(hv, -1, cls, **kw)


def comp(acv: int, sem: int, cls: str = "android.view.View", **kw) -> Dict[str, Any]:
    return _node(acv, sem, cls, **kw)


def decor(hv: int, *kids, b=(0, 0, 1080, 2400)) -> Dict[str, Any]:
    return view(hv, "android.widget.FrameLayout", b=b, kids=kids)


def screen(*roots) -> Dict[str, Any]:
    return {"windows": [{"root_view_id": r["host_view_id"], "root": r} for r in roots]}


def lint(data, density=160, enabled=None, compose=None, **ctx_kw) -> L.LintReport:
    ctx = L.LintContext(density=density, **ctx_kw)
    return L.lint_unified(data, ctx, compose_data=compose, enabled=enabled)


def of(report, rule) -> List[L.Finding]:
    return [f for f in report.findings if f.rule == rule]


def keys(findings) -> List[str]:
    return [f.node_key for f in findings]


CLICK = ("clickable", "focusable")


# --------------------------------------------------------------------------- #
# Identity / keys / window / collection position.
# --------------------------------------------------------------------------- #
def test_view_and_compose_keys_follow_the_id_contract():
    ib = view(11, "android.widget.ImageButton", flags=CLICK, b=(100, 300, 160, 160))
    cbtn = comp(20, 7, "android.widget.Button", flags=CLICK, b=(100, 600, 160, 160))
    host = view(20, ACV, b=(0, 500, 1080, 800), kids=[cbtn], provider_class=ACV)
    rep = lint(screen(decor(1, ib, host)))
    f = of(rep, "a11y.label.missing")
    assert sorted(keys(f)) == ["compose:20:7", "view:11"]
    by_key = {x.node_key: x for x in f}
    assert by_key["view:11"].node["id"] == L.a11y_node_id(11, -1)
    assert by_key["compose:20:7"].node["id"] == L.a11y_node_id(20, 7)
    assert by_key["compose:20:7"].node["host_view_id"] == 20
    assert by_key["compose:20:7"].node["virtual_id"] == 7
    assert by_key["view:11"].window == {"index": 0, "root_view_id": 1}
    d = by_key["view:11"].to_dict()
    assert d["node_key"] == "view:11" and d["alias"] == "R1"


def test_nested_interop_keys_compose_in_view_in_compose():
    # S5: ComposeView -> AndroidView -> RecyclerView -> ComposeView -> unlabeled button
    inner_btn = comp(40, 3, "android.widget.Button", flags=CLICK, b=(100, 900, 160, 160))
    inner_acv = view(40, ACV, b=(0, 850, 1080, 300), kids=[inner_btn], provider_class=ACV)
    rv = view(31, "androidx.recyclerview.widget.RecyclerView", flags=("scrollable",),
              b=(0, 800, 1080, 1000), kids=[inner_acv],
              collection_info={"row_count": -1, "column_count": 1})
    holder = view(30, "androidx.compose.ui.viewinterop.ViewFactoryHolder",
                  b=(0, 800, 1080, 1000), kids=[rv])
    outer_node = comp(10, 5, b=(0, 800, 1080, 1000), kids=[holder])
    outer = view(10, ACV, b=(0, 0, 1080, 2400), kids=[outer_node], provider_class=ACV)
    rep = lint(screen(decor(1, outer)))
    f = of(rep, "a11y.label.missing")
    assert keys(f) == ["compose:40:3"]
    assert f[0].collection == {"container": "view:31", "row": "view:40", "row_index": 0,
                               "column_index": None}


def test_same_semantics_id_in_two_compose_views_gets_distinct_keys():
    cells = []
    for i, acv in enumerate((101, 102)):
        btn = comp(acv, 3, "android.widget.Button", flags=CLICK, b=(900, 300 + i * 200, 160, 160))
        cells.append(view(acv, ACV, b=(0, 300 + i * 200, 1080, 200), kids=[btn], provider_class=ACV))
    rv = view(9, "androidx.recyclerview.widget.RecyclerView", flags=("scrollable",),
              b=(0, 200, 1080, 1600), kids=cells, collection_info={"row_count": 2})
    rep = lint(screen(decor(1, rv)))
    assert sorted(keys(of(rep, "a11y.label.missing"))) == ["compose:101:3", "compose:102:3"]


def test_webview_virtual_nodes_are_not_keyed_as_compose():
    link = comp(50, 12, "android.view.View", flags=CLICK, b=(100, 300, 160, 160))
    wv = view(50, "android.webkit.WebView", b=(0, 200, 1080, 1000), kids=[link],
              provider_class="android.webkit.WebView")
    rep = lint(screen(decor(1, wv)))
    assert keys(of(rep, "a11y.label.missing")) == ["virtual:50:12"]


def test_degenerate_identity_is_diagnosed():
    kids = [_node(1, 4, "android.widget.TextView", text=f"t{i}", b=(0, i * 100, 500, 90))
            for i in range(8)]
    root = _node(1, -1, "android.widget.FrameLayout", b=(0, 0, 1080, 2400), kids=kids)
    rep = lint(screen(root))
    codes = [d["code"] for d in rep.diagnostics]
    assert "identity.degenerate" in codes


def test_compose_detail_join_adds_role_and_test_tag():
    btn = comp(20, 7, "android.view.View", flags=CLICK, cd="Play", b=(100, 600, 160, 160))
    host = view(20, ACV, b=(0, 500, 1080, 800), kids=[btn], provider_class=ACV)
    compose = {"windows": [{"view_id": 20, "root": {
        "id": 20, "name": "AndroidComposeView", "kind": "COMPOSABLE", "children": [
            {"id": 1, "kind": "SEMANTICS", "name": "", "attrs": {}, "children": [
                {"id": 7, "kind": "SEMANTICS", "name": "Play", "source": "Player.kt:88",
                 "attrs": {"Role": "Button", "TestTag": "play", "ContentDescription": "Play",
                           "OnClick": ""}}]}]}}]}
    without = lint(screen(decor(1, host)))
    assert keys(of(without, "a11y.role.missing_on_clickable")) == ["compose:20:7"]
    rep = lint(screen(decor(1, host)), compose=compose)
    assert of(rep, "a11y.role.missing_on_clickable") == []
    assert rep.stats["compose_joined"] >= 1
    rep2 = lint(screen(decor(1, host)), compose=compose, enabled=["R2"],
                density=420)  # 160px @420dpi = 61dp -> no finding, but check the node ref
    rep3 = lint(screen(decor(1, host)), compose=compose, enabled=["R4"])
    assert rep2.findings == [] and rep3.findings == []
    small = comp(20, 7, "android.view.View", flags=CLICK, cd="Play", b=(100, 600, 30, 30))
    host2 = view(20, ACV, b=(0, 500, 1080, 800), kids=[small], provider_class=ACV)
    f = of(lint(screen(decor(1, host2)), compose=compose), "a11y.touch_target.small")[0]
    assert f.node["test_tag"] == "play" and f.node["role"] == "Button"
    assert f.node["source"] == "Player.kt:88"


# --------------------------------------------------------------------------- #
# R1 -- missing label.
# --------------------------------------------------------------------------- #
def test_r1_unlabeled_view_image_button_is_error():
    rep = lint(screen(decor(1, view(11, "android.widget.ImageButton", flags=CLICK))))
    f = of(rep, "a11y.label.missing")
    assert [x.severity for x in f] == ["error"]
    assert "android:contentDescription" in f[0].message
    # and it is NOT double-reported as R6/R5/R8
    assert of(rep, "a11y.image.no_description") == []
    assert of(rep, "a11y.role.missing_on_clickable") == []
    assert of(rep, "a11y.node.empty_focusable") == []


def test_r1_labeled_image_button_passes():
    rep = lint(screen(decor(1, view(11, "android.widget.ImageButton", flags=CLICK, cd="Like"))))
    assert of(rep, "a11y.label.missing") == []


def test_r1_compose_icon_button_label_from_non_focusable_icon_child_passes():
    icon = comp(20, 9, cd="Delete", b=(129, 129, 59, 59))
    btn = comp(20, 8, flags=CLICK + ("screen_reader_focusable",), kids=[icon])
    rep = lint(screen(decor(1, view(20, ACV, b=(0, 0, 1080, 2400), kids=[btn]))))
    assert of(rep, "a11y.label.missing") == []


def test_r1_card_whose_only_label_is_a_separate_button_fires():
    inner = comp(20, 9, "android.widget.Button", flags=CLICK, cd="Delete", b=(850, 120, 160, 160))
    card = comp(20, 8, flags=CLICK, b=(0, 100, 1000, 400), kids=[inner])
    rep = lint(screen(decor(1, view(20, ACV, b=(0, 0, 1080, 2400), kids=[card]))))
    assert keys(of(rep, "a11y.label.missing")) == ["compose:20:8"]


def test_r1_labeled_by_resolves():
    label = view(12, "android.widget.TextView", text="Volume", b=(100, 100, 300, 60))
    ctl = view(13, flags=CLICK, labeled_by=L.a11y_node_id(12, -1), b=(100, 200, 160, 160))
    assert of(lint(screen(decor(1, label, ctl))), "a11y.label.missing") == []
    ctl2 = view(13, flags=CLICK, b=(100, 200, 160, 160))
    assert keys(of(lint(screen(decor(1, label, ctl2))), "a11y.label.missing")) == ["view:13"]


def test_r1_bare_clickable_box_is_reported_once():
    rep = lint(screen(decor(1, view(40, flags=CLICK))))
    assert [f.rule for f in rep.findings] == ["a11y.label.missing"]


# --------------------------------------------------------------------------- #
# R2 -- touch target on a11y touch bounds.
# --------------------------------------------------------------------------- #
def test_r2_material_checkbox_touch_bounds_48dp_pass():
    cb = comp(20, 5, "android.widget.CheckBox", flags=CLICK + ("checkable",), state=None,
              b=(200, 300, 48, 48), cd="Subscribe")
    assert of(lint(screen(decor(1, view(20, ACV, b=(0, 0, 1080, 2400), kids=[cb])))),
              "a11y.touch_target.small") == []


def test_r2_40dp_warns_and_below_24dp_in_one_dimension_errors():
    ok = view(10, "android.widget.Button", text="A", flags=CLICK, b=(100, 100, 40, 40))
    thin = view(11, "android.widget.Button", text="B", flags=CLICK, b=(300, 100, 10, 200))
    rep = lint(screen(decor(1, ok, thin)))
    sev = {f.node_key: f.severity for f in of(rep, "a11y.touch_target.small")}
    assert sev == {"view:10": "warn", "view:11": "error"}
    f = [x for x in of(rep, "a11y.touch_target.small") if x.node_key == "view:11"][0]
    assert "24dp" in f.message and "Padding alone" not in f.message
    assert f.evidence["bounds_source"].startswith("a11y boundsInScreen")


def test_r2_wcag_mode_uses_44dp():
    b = view(10, "android.widget.Button", text="A", flags=CLICK, b=(100, 100, 45, 45))
    assert of(lint(screen(decor(1, b)), wcag_mode=True), "a11y.touch_target.small") == []
    assert len(of(lint(screen(decor(1, b))), "a11y.touch_target.small")) == 1


def test_r2_row_clipped_at_scroll_edge_is_info_not_error():
    row_clipped = view(21, "android.widget.LinearLayout", flags=CLICK, text="Item 26",
                       b=(0, 2066, 1080, 8))
    row_ok = view(22, "android.widget.LinearLayout", flags=CLICK, text="Item 25",
                  b=(0, 1910, 1080, 156))
    rv = view(20, "androidx.recyclerview.widget.RecyclerView", flags=("scrollable", "focusable"),
              b=(0, 260, 1080, 1814), kids=[row_ok, row_clipped],
              collection_info={"row_count": -1})
    f = of(lint(screen(decor(1, rv))), "a11y.touch_target.small")
    assert [(x.node_key, x.severity) for x in f] == [("view:21", "info")]
    assert "h" in f[0].evidence["clipped_axes"]


def test_r2_small_target_not_at_edge_is_not_excused():
    small = view(21, "android.widget.ImageButton", flags=CLICK, cd="Delete", b=(900, 500, 20, 20))
    rv = view(20, "androidx.recyclerview.widget.RecyclerView", flags=("scrollable",),
              b=(0, 260, 1080, 1814), kids=[small])
    assert [x.severity for x in of(lint(screen(decor(1, rv))), "a11y.touch_target.small")] == ["error"]


def test_r2_inline_link_exception():
    link = comp(20, 9, flags=CLICK, text="terms", b=(300, 300, 60, 20))
    para = comp(20, 8, "android.widget.TextView", text="I accept the terms and conditions",
                b=(100, 300, 800, 40), kids=[link])
    rep = lint(screen(decor(1, view(20, ACV, b=(0, 0, 1080, 2400), kids=[para]))))
    assert of(rep, "a11y.touch_target.small") == []


# --------------------------------------------------------------------------- #
# R3 -- contrast from dominant colours, per window.
# --------------------------------------------------------------------------- #
WHITE = (255, 255, 255)


def _lerp(a, b, t):
    return tuple(int(round(x + (y - x) * t)) for x, y in zip(a, b))


def _text_image(w, h, fg, stroke=2, aa=True, bg=WHITE, period=10):
    """Horizontal 'strokes' every ``period`` rows, anti-aliased rows either side."""
    buf = bytearray(w * h * 4)
    for y in range(h):
        m = y % period
        if 3 <= m < 3 + stroke:
            c = fg
        elif aa and (m == 2 or m == 3 + stroke):
            c = _lerp(fg, bg, 0.5)
        else:
            c = bg
        row = bytes((*c, 255)) * w
        buf[y * w * 4:(y + 1) * w * 4] = row
    return bytes(buf)


def _text_screen(text_px=None, b=(0, 0, 240, 50)):
    extra = {}
    if text_px:
        extra = {"text_size_px": text_px, "text_size_unit": 2}
    tv = view(10, "android.widget.TextView", text="Body", b=b, **extra)
    return screen(decor(1, tv, b=(0, 0, 1080, 2400)))


def _ctx_img(rgba, w, h, rvid=1, ox=0, oy=0, scale=1.0):
    return {rvid: L.WindowImage(w, h, rgba, scale, ox, oy, rvid)}


def test_r3_antialiased_767676_on_white_passes():
    # #767676 on white is 4.54:1 -- the old k-means mean reported ~2.9 and failed it.
    img = _text_image(240, 50, (0x76, 0x76, 0x76), stroke=2, aa=True)
    rep = lint(_text_screen(text_px=14), window_images=_ctx_img(img, 240, 50))
    assert of(rep, "a11y.contrast.low") == []
    assert rep.stats["contrast_checked"] == 1


def test_r3_9a9a9a_fails_for_known_normal_text():
    img = _text_image(240, 50, (0x9A, 0x9A, 0x9A), stroke=2, aa=True)
    f = of(lint(_text_screen(text_px=14), window_images=_ctx_img(img, 240, 50)), "a11y.contrast.low")
    assert len(f) == 1 and f[0].severity == "error"
    assert f[0].evidence["fg_hex"] == "#9A9A9A" and f[0].evidence["bg_hex"] == "#FFFFFF"
    assert f[0].evidence["text_size_class"] == "normal"
    assert f[0].evidence["sample"] == "window:1"


def test_r3_large_text_from_text_size_not_node_height():
    # 3.3:1 grey: fails 4.5 (normal) but passes 3.0 (large). Node is short (40px),
    # text size is 28dp -> large.
    grey = (0x8A, 0x8A, 0x8A)
    img = _text_image(240, 40, grey, stroke=3, aa=False)
    assert of(lint(_text_screen(text_px=28, b=(0, 0, 240, 40)),
                   window_images=_ctx_img(img, 240, 40)), "a11y.contrast.low") == []
    # Tall node (120px) but 14dp text -> normal -> fails.
    img2 = _text_image(240, 120, grey, stroke=3, aa=False)
    f = of(lint(_text_screen(text_px=14, b=(0, 0, 240, 120)),
                window_images=_ctx_img(img2, 240, 120)), "a11y.contrast.low")
    assert [x.severity for x in f] == ["error"]


def test_r3_unknown_text_size_between_3_and_4_5_is_warn():
    img = _text_image(240, 50, (0x8A, 0x8A, 0x8A), stroke=2, aa=False)
    f = of(lint(_text_screen(), window_images=_ctx_img(img, 240, 50)), "a11y.contrast.low")
    assert [x.severity for x in f] == ["warn"]
    assert f[0].evidence["text_size_class"] == "unknown"


def test_r3_samples_the_nodes_own_window_with_its_origin():
    # Main window (rvid 1) is white-on-white noise; the dialog window (rvid 2) sits at
    # (100, 500) on screen and holds light-grey text. The finding must come from the
    # dialog window image, offset by its origin.
    dlg_text = view(30, "android.widget.TextView", text="Dialog body", b=(100, 500, 240, 50),
                    text_size_px=14, text_size_unit=2)
    dialog = view(2, "android.widget.FrameLayout", b=(100, 500, 240, 50), kids=[dlg_text])
    main = decor(1, view(10, "android.widget.TextView", text="Main", b=(100, 500, 240, 50),
                         text_size_px=14, text_size_unit=2))
    main_img = _text_image(1080, 2400, (0, 0, 0), stroke=2) if False else bytes(1080 * 2400 * 4)
    dlg_img = _text_image(240, 50, (0xC8, 0xC8, 0xC8), stroke=2, aa=False)
    imgs = {1: L.WindowImage(1080, 2400, main_img, 1.0, 0, 0, 1),
            2: L.WindowImage(240, 50, dlg_img, 1.0, 100, 500, 2)}
    rep = lint(screen(main, dialog), window_images=imgs)
    f = of(rep, "a11y.contrast.low")
    assert [(x.node_key, x.evidence["sample"]) for x in f] == [("view:30", "window:2")]
    assert f[0].window == {"index": 1, "root_view_id": 2}


def test_r3_scaled_screenshot_maps_bounds():
    img = _text_image(120, 25, (0x9A, 0x9A, 0x9A), stroke=1, aa=False, period=5)
    f = of(lint(_text_screen(text_px=14), window_images=_ctx_img(img, 120, 25, scale=0.5)),
           "a11y.contrast.low")
    assert len(f) == 1 and f[0].evidence["scale"] == 0.5


def test_r3_is_fast_on_a_large_text_node():
    W, H = 1080, 700
    img = _text_image(W, H, (0, 0, 0))
    data = _text_screen(text_px=14, b=(0, 0, W, H))
    t = time.time()
    lint(data, enabled=["R3"], window_images=_ctx_img(img, W, H))
    # The old pure-Python k-means took ~1s+ for this node; dominant-colour sampling
    # is well over 5x faster.
    assert time.time() - t < 0.2


# --------------------------------------------------------------------------- #
# R4 -- redundant contentDescription.
# --------------------------------------------------------------------------- #
def test_r4_role_word_in_cd_with_punctuation_warns():
    b = view(10, "android.widget.Button", flags=CLICK, cd="Submit button.")
    f = of(lint(screen(decor(1, b))), "a11y.label.redundant")
    assert [(x.severity, x.evidence["matched_word"]) for x in f] == [("warn", "button")]


def test_r4_visible_text_and_non_role_words_are_not_flagged():
    b1 = view(10, "android.widget.Button", flags=CLICK, text="Upload image", b=(100, 100, 400, 160))
    b2 = view(11, "android.widget.Button", flags=CLICK, cd="Upload image", b=(100, 300, 400, 160))
    b3 = view(12, "android.widget.Button", flags=CLICK, cd="New tab", b=(100, 500, 400, 160))
    assert of(lint(screen(decor(1, b1, b2, b3))), "a11y.label.redundant") == []


def test_r4_icon_cd_inherits_the_role_of_its_button():
    icon = comp(20, 9, cd="Delete button", b=(129, 129, 59, 59))
    btn = comp(20, 8, "android.widget.Button", flags=CLICK, kids=[icon])
    f = of(lint(screen(decor(1, view(20, ACV, b=(0, 0, 1080, 2400), kids=[btn])))),
           "a11y.label.redundant")
    assert keys(f) == ["compose:20:9"]


def test_r4_state_word_on_checkable():
    cb = view(10, "android.widget.CheckBox", flags=CLICK + ("checkable",), cd="Subscribe, unchecked")
    f = of(lint(screen(decor(1, cb))), "a11y.label.redundant")
    assert [(x.severity, x.evidence["reason"]) for x in f] == [("info", "state_word")]


# --------------------------------------------------------------------------- #
# R5 -- clickable without role.
# --------------------------------------------------------------------------- #
def test_r5_clickable_textview_without_role_is_info():
    tv = view(10, "android.widget.TextView", flags=CLICK, text="Submit")
    f = of(lint(screen(decor(1, tv))), "a11y.role.missing_on_clickable")
    assert [x.severity for x in f] == ["info"]
    assert "cannot tell users it is actionable" not in f[0].message


def test_r5_icon_only_clickable_without_role_warns():
    v = view(10, "android.widget.FrameLayout", flags=CLICK, cd="Settings")
    assert [x.severity for x in of(lint(screen(decor(1, v))), "a11y.role.missing_on_clickable")] == ["warn"]


def test_r5_text_field_and_list_rows_are_exempt():
    field = comp(20, 5, "android.widget.EditText", flags=CLICK + ("editable",), text="Email",
                 b=(40, 300, 1000, 168))
    row = comp(20, 7, flags=CLICK, b=(0, 600, 1080, 160), kids=[
        comp(20, 8, "android.widget.TextView", text="Touch target size", b=(40, 620, 800, 60))])
    lazy = comp(20, 6, flags=("scrollable",), b=(0, 500, 1080, 1500), kids=[row],
                collection_info={"row_count": -1, "column_count": 1})
    host = view(20, ACV, b=(0, 0, 1080, 2400), kids=[field, lazy])
    assert of(lint(screen(decor(1, host))), "a11y.role.missing_on_clickable") == []


def test_r5_inner_role_node_counts():
    inner = comp(20, 9, "android.widget.Button", b=(110, 110, 140, 140))
    icon = comp(20, 10, cd="Delete", b=(130, 130, 60, 60))
    outer = comp(20, 8, flags=CLICK, kids=[inner, icon])
    assert of(lint(screen(decor(1, view(20, ACV, b=(0, 0, 1080, 2400), kids=[outer])))),
              "a11y.role.missing_on_clickable") == []


# --------------------------------------------------------------------------- #
# R6 -- images.
# --------------------------------------------------------------------------- #
def test_r6_meaningful_view_image_without_cd_warns():
    # importantForAccessibility="yes" (or a focusable image): TalkBack stops on it and
    # has nothing to say.
    iv = view(10, "android.widget.ImageView", important_for_accessibility="YES")
    f = of(lint(screen(decor(1, iv))), "a11y.image.no_description")
    assert [(x.node_key, x.severity) for x in f] == [("view:10", "warn")]


def test_r6_skips_an_auto_image_talkback_never_sees():
    # <ImageView android:src=.../> with no contentDescription, left at auto: not important
    # for accessibility, so TalkBack (and ATF) never see it. The agent reports AUTO only for
    # such a View; a leading icon inside a clickable row is the common case.
    icon = view(4, "android.widget.ImageView", b=(48, 340, 63, 63))
    row = view(3, "android.widget.LinearLayout", flags=CLICK, b=(0, 300, 1080, 170),
               kids=[icon, view(5, "android.widget.TextView", text="Wi-Fi",
                                important_for_accessibility="YES", b=(160, 340, 600, 63))])
    banner = view(6, "android.widget.ImageView", b=(0, 600, 1080, 500))
    rep = lint(screen(decor(1, view(2, "android.widget.LinearLayout", b=(0, 0, 1080, 2400),
                                    kids=[row, banner]))), density=420)
    assert of(rep, "a11y.image.no_description") == []


def test_r6_decorative_and_labelled_images_pass():
    deco = view(10, "android.widget.ImageView", important_for_accessibility="NO",
                b=(100, 100, 100, 100))
    labelled = view(11, "android.widget.ImageView", cd="Profile photo", b=(300, 100, 100, 100))
    text_icon = comp(20, 5, "android.widget.TextView", text="Icon", b=(100, 400, 200, 60))
    icon_in_button = comp(20, 7, "android.widget.ImageView", b=(530, 530, 60, 60))
    button = comp(20, 6, "android.widget.Button", flags=CLICK, text="Share",
                  b=(500, 500, 300, 120), kids=[icon_in_button])
    host = view(20, ACV, b=(0, 300, 1080, 2000), kids=[text_icon, button])
    rep = lint(screen(decor(1, deco, labelled, host)))
    assert of(rep, "a11y.image.no_description") == []


def test_r6_view_hidden_subtree_is_skipped():
    iv = view(11, "android.widget.ImageView", b=(100, 100, 100, 100))
    box = view(10, "android.widget.FrameLayout", important_for_accessibility="NO_HIDE_DESCENDANTS",
               b=(0, 0, 500, 500), kids=[iv])
    assert of(lint(screen(decor(1, box))), "a11y.image.no_description") == []


# --------------------------------------------------------------------------- #
# R7 -- state.
# --------------------------------------------------------------------------- #
def test_r7_keyword_false_positives_are_gone():
    a = view(10, "android.widget.Button", flags=CLICK, text="Show disabled accounts", b=(100, 100, 600, 160))
    b = view(11, "android.widget.Button", flags=CLICK, text="Sign off", b=(100, 300, 600, 160))
    assert of(lint(screen(decor(1, a, b))), "a11y.state.not_exposed") == []


def test_r7_label_encoded_state_and_stateless_switch_warn():
    a = view(10, "android.widget.Button", flags=CLICK, text="Wi-Fi off", b=(100, 100, 600, 160))
    sw = comp(20, 5, "android.widget.Switch", flags=CLICK, text="Notifications", b=(100, 300, 600, 160))
    host = view(20, ACV, b=(0, 200, 1080, 800), kids=[sw])
    f = of(lint(screen(decor(1, a, host))), "a11y.state.not_exposed")
    assert sorted((x.node_key, x.severity, x.evidence["reason"]) for x in f) == [
        ("compose:20:5", "warn", "stateful_role"), ("view:10", "warn", "label_encodes_state")]


def test_r7_icon_swap_favorite_toggle_is_info_and_real_switch_passes():
    icon = comp(20, 6, cd="Favorite", b=(130, 130, 60, 60))
    fav = comp(20, 5, "android.widget.Button", flags=CLICK, kids=[icon])
    sw = view(11, "android.widget.Switch", flags=CLICK + ("checkable", "checked"),
              text="Notifications", b=(100, 600, 600, 160))
    host = view(20, ACV, b=(0, 0, 1080, 500), kids=[fav])
    f = of(lint(screen(decor(1, host, sw))), "a11y.state.not_exposed")
    assert [(x.node_key, x.severity, x.evidence["reason"]) for x in f] == [
        ("compose:20:5", "info", "possible_toggle")]


# --------------------------------------------------------------------------- #
# R8 -- empty focusable.
# --------------------------------------------------------------------------- #
def test_r8_focusable_empty_node_warns_but_containers_do_not():
    empty = comp(20, 5, flags=("screen_reader_focusable",), b=(100, 100, 200, 200))
    btn = view(12, "android.widget.Button", flags=CLICK, text="OK", b=(100, 700, 300, 160))
    sv = view(11, "android.widget.ScrollView", flags=("focusable", "scrollable"),
              b=(0, 600, 1080, 1000), kids=[btn])
    host = view(20, ACV, b=(0, 0, 1080, 500), kids=[empty])
    f = of(lint(screen(decor(1, host, sv))), "a11y.node.empty_focusable")
    assert keys(f) == ["compose:20:5"]


# --------------------------------------------------------------------------- #
# R9 -- headings.
# --------------------------------------------------------------------------- #
def _texts(n, start_id=100, heading_at=None):
    out = []
    for i in range(n):
        fl = ("heading",) if heading_at == i else ()
        out.append(view(start_id + i, "android.widget.TextView", text=f"Line {i}",
                        flags=fl, b=(40, 100 + i * 120, 800, 100)))
    return out


def test_r9_long_screen_without_headings_is_info_per_window():
    rep = lint(screen(decor(1, *_texts(14))))
    f = of(rep, "a11y.heading.structure")
    assert [(x.severity, x.evidence["reason"], x.node_key) for x in f] == [
        ("info", "no_headings", "view:1")]


def test_r9_heading_present_and_empty_heading():
    assert of(lint(screen(decor(1, *_texts(14, heading_at=0)))), "a11y.heading.structure") == []
    empty = view(99, flags=("heading",), b=(40, 40, 800, 50))
    f = of(lint(screen(decor(1, empty))), "a11y.heading.structure")
    assert [(x.severity, x.evidence["reason"]) for x in f] == [("warn", "empty_heading")]


# --------------------------------------------------------------------------- #
# R10 -- grouping.
# --------------------------------------------------------------------------- #
def test_r10_list_row_with_separate_title_and_subtitle_is_info():
    title = comp(20, 11, "android.widget.TextView", text="Alice",
                 flags=("screen_reader_focusable",), b=(40, 610, 600, 30))
    sub = comp(20, 12, "android.widget.TextView", text="Online",
               flags=("screen_reader_focusable",), b=(40, 644, 600, 24))
    row = comp(20, 10, b=(0, 600, 1080, 80), kids=[title, sub])
    lazy = comp(20, 9, flags=("scrollable",), b=(0, 500, 1080, 1500), kids=[row],
                collection_info={"row_count": -1})
    f = of(lint(screen(decor(1, view(20, ACV, b=(0, 0, 1080, 2400), kids=[lazy])))),
           "a11y.grouping.missing")
    assert keys(f) == ["compose:20:10"]


def test_r10_paragraphs_and_horizontal_chips_pass():
    paras = view(10, "android.widget.LinearLayout", b=(0, 0, 1000, 600), kids=[
        view(11 + i, "android.widget.TextView", text=f"Paragraph {i} ...",
             b=(0, i * 190, 1000, 180)) for i in range(3)])
    chips = view(20, "android.widget.LinearLayout", b=(0, 700, 1000, 40), kids=[
        view(21 + i, "android.widget.TextView", text=f"Tag {i}",
             b=(i * 320, 700, 300, 40)) for i in range(3)])
    assert of(lint(screen(decor(1, paras, chips))), "a11y.grouping.missing") == []


# --------------------------------------------------------------------------- #
# R11 / R18 -- text size (needs ExtraRenderingInfo).
# --------------------------------------------------------------------------- #
def test_r11_px_and_dp_units_warn_sp_passes():
    px = view(10, "android.widget.TextView", text="px text", b=(40, 100, 500, 60),
              text_size_px=42.0)  # text_size_unit 0 (px) is omitted by a11y_to_dict
    dp = view(11, "android.widget.TextView", text="dp text", b=(40, 200, 500, 60),
              text_size_px=42.0, text_size_unit=1)
    sp = view(12, "android.widget.TextView", text="sp text", b=(40, 300, 500, 60),
              text_size_px=42.0, text_size_unit=2)
    unknown = view(13, "android.widget.TextView", text="compose", b=(40, 400, 500, 60))
    rep = lint(screen(decor(1, px, dp, sp, unknown)), density=420, font_scale=1.3)
    f = of(rep, "a11y.text.fixed_scaling")
    assert sorted((x.node_key, x.evidence["unit"]) for x in f) == [("view:10", "px"), ("view:11", "dp")]
    assert f[0].severity == "warn" and f[0].evidence["font_scale"] == 1.3


def test_r18_tiny_text_uses_font_scale():
    # 10sp at font_scale 1.3 renders at 13dp, but is 10sp nominal -> too small.
    tiny = view(10, "android.widget.TextView", text="fine print", b=(40, 100, 500, 60),
                text_size_px=13.0, text_size_unit=2)
    ok = view(11, "android.widget.TextView", text="body", b=(40, 200, 500, 60),
              text_size_px=18.2, text_size_unit=2)  # 14sp * 1.3
    f = of(lint(screen(decor(1, tiny, ok)), font_scale=1.3), "a11y.text.too_small")
    assert [(x.node_key, x.evidence["nominal"]) for x in f] == [("view:10", 10.0)]


def test_text_size_rules_report_when_no_rendering_info():
    rep = lint(screen(decor(1, view(10, "android.widget.TextView", text="x", b=(40, 100, 500, 60)))))
    assert "text_size.unavailable" in [d["code"] for d in rep.diagnostics]


# --------------------------------------------------------------------------- #
# R12 -- duplicate labels (collection-aware).
# --------------------------------------------------------------------------- #
def _rv_rows(n=3):
    rows = []
    for i in range(n):
        y = 300 + i * 200
        rows.append(view(100 + i * 10, "android.widget.LinearLayout", flags=CLICK,
                         b=(0, y, 1080, 190), kids=[
                             view(101 + i * 10, "android.widget.TextView", text=f"Item {i}",
                                  b=(40, y + 60, 700, 60)),
                             view(102 + i * 10, "android.widget.ImageButton", flags=CLICK,
                                  cd="Delete", b=(900, y + 20, 150, 150))]))
    return view(90, "androidx.recyclerview.widget.RecyclerView", flags=("scrollable",),
                b=(0, 260, 1080, 1800), kids=rows, collection_info={"row_count": -1})


def test_r12_per_row_duplicates_in_recyclerview_are_not_flagged():
    assert of(lint(screen(decor(1, _rv_rows()))), "a11y.duplicate.label") == []


def test_r12_lazycolumn_with_androidview_rows_not_flagged():
    crow = comp(20, 10, flags=CLICK, b=(0, 300, 1080, 190), kids=[
        comp(20, 11, "android.widget.Button", flags=CLICK, cd="Delete", b=(900, 320, 150, 150))])
    vrow_btn = view(62, "android.widget.ImageButton", flags=CLICK, cd="Delete", b=(900, 520, 150, 150))
    holder = view(60, "androidx.compose.ui.viewinterop.ViewFactoryHolder", b=(0, 500, 1080, 190),
                  kids=[view(61, "android.widget.LinearLayout", flags=CLICK, b=(0, 500, 1080, 190),
                             kids=[vrow_btn])])
    lazy = comp(20, 9, flags=("scrollable",), b=(0, 260, 1080, 1800), kids=[crow, holder],
                collection_info={"row_count": -1, "column_count": 1})
    host = view(20, ACV, b=(0, 0, 1080, 2400), kids=[lazy])
    assert of(lint(screen(decor(1, host))), "a11y.duplicate.label") == []


def test_r12_same_parent_duplicates_warn_other_parents_info():
    row = view(10, "android.widget.LinearLayout", b=(0, 300, 1080, 200), kids=[
        view(11, "android.widget.Button", flags=CLICK, text="Edit", b=(0, 300, 300, 160)),
        view(12, "android.widget.Button", flags=CLICK, text="Edit", b=(400, 300, 300, 160))])
    rep = lint(screen(decor(1, row)))
    assert sorted((x.node_key, x.severity) for x in of(rep, "a11y.duplicate.label")) == [
        ("view:11", "warn"), ("view:12", "warn")]
    card_a = view(20, "android.widget.LinearLayout", b=(0, 300, 1080, 200), kids=[
        view(21, "android.widget.Button", flags=CLICK, text="Open", b=(0, 300, 300, 160))])
    card_b = view(30, "android.widget.LinearLayout", b=(0, 600, 1080, 200), kids=[
        view(31, "android.widget.Button", flags=CLICK, text="Open", b=(0, 600, 300, 160))])
    rep2 = lint(screen(decor(1, card_a, card_b)))
    assert sorted((x.node_key, x.severity) for x in of(rep2, "a11y.duplicate.label")) == [
        ("view:21", "info"), ("view:31", "info")]


# --------------------------------------------------------------------------- #
# R13..R17 -- ATF-style rules.
# --------------------------------------------------------------------------- #
def test_r13_duplicate_clickable_bounds():
    outer = view(10, "android.widget.FrameLayout", flags=CLICK, cd="Card", b=(100, 100, 400, 200))
    inner = view(11, "android.widget.Button", flags=CLICK, text="Open", b=(100, 100, 400, 200))
    wrapper = view(9, "android.widget.FrameLayout", b=(100, 100, 400, 200), kids=[outer])
    rep = lint(screen(decor(1, wrapper, inner)))
    f = of(rep, "a11y.clickable.duplicate_bounds")
    assert [(x.node_key, x.evidence["duplicate_of"]) for x in f] == [("view:11", "view:10")]
    moved = view(11, "android.widget.Button", flags=CLICK, text="Open", b=(100, 400, 400, 200))
    assert of(lint(screen(decor(1, wrapper, moved))), "a11y.clickable.duplicate_bounds") == []


def test_r14_editable_with_content_description_is_error():
    bad = view(10, "android.widget.EditText", flags=CLICK + ("editable",), cd="Email",
               b=(40, 100, 1000, 160))
    good = view(11, "android.widget.EditText", flags=CLICK + ("editable", "showing_hint_text"),
                text="name@example.com", hint="name@example.com", b=(40, 400, 1000, 160))
    f = of(lint(screen(decor(1, bad, good))), "a11y.editable.content_description")
    assert [(x.node_key, x.severity) for x in f] == [("view:10", "error")]


def test_r15_vague_link_span_and_action_text():
    tv = view(10, "android.widget.TextView", text="For details click here", b=(40, 100, 800, 60),
              extras={"androidx.view.accessibility.AccessibilityNodeInfoCompat.SPANS_START_KEY": "[12]",
                      "androidx.view.accessibility.AccessibilityNodeInfoCompat.SPANS_END_KEY": "[22]"})
    more = view(11, "android.widget.Button", flags=CLICK, text="Learn more", b=(40, 300, 400, 160))
    good = view(12, "android.widget.Button", flags=CLICK, text="Read the privacy policy",
                b=(40, 500, 600, 160))
    f = of(lint(screen(decor(1, tv, more, good))), "a11y.link.purpose_unclear")
    assert sorted((x.node_key, x.severity, x.evidence["link_text"]) for x in f) == [
        ("view:10", "warn", "click here"), ("view:11", "info", "Learn more")]


def test_r16_form_field_without_label():
    bare = view(10, "android.widget.EditText", flags=CLICK + ("editable",), b=(40, 100, 1000, 160))
    label = view(11, "android.widget.TextView", text="Email", b=(40, 300, 400, 60),
                 label_for=L.a11y_node_id(12, -1))
    with_label_for = view(12, "android.widget.EditText", flags=CLICK + ("editable",),
                          b=(40, 380, 1000, 160))
    with_hint = view(13, "android.widget.EditText", flags=CLICK + ("editable",), hint="Name",
                     b=(40, 600, 1000, 160))
    compose_label = comp(20, 6, "android.widget.TextView", text="Password", b=(60, 820, 300, 40))
    compose_field = comp(20, 5, "android.widget.EditText", flags=CLICK + ("editable",),
                         b=(40, 800, 1000, 160), kids=[compose_label])
    host = view(20, ACV, b=(0, 750, 1080, 400), kids=[compose_field])
    f = of(lint(screen(decor(1, bare, label, with_label_for, with_hint, host))),
           "a11y.form.label_missing")
    assert [(x.node_key, x.severity) for x in f] == [("view:10", "error")]
    # and it is not ALSO an R1 (editable fields are R16's)
    assert of(lint(screen(decor(1, bare))), "a11y.label.missing") == []


def test_r17_traversal_cycle_and_dangling_target():
    a_id, b_id = L.a11y_node_id(10, -1), L.a11y_node_id(11, -1)
    a = view(10, "android.widget.TextView", text="A", b=(40, 100, 400, 60), traversal_before=b_id)
    b = view(11, "android.widget.TextView", text="B", b=(40, 200, 400, 60), traversal_before=a_id)
    c = view(12, "android.widget.TextView", text="C", b=(40, 300, 400, 60),
             traversal_after=L.a11y_node_id(999, -1))
    f = of(lint(screen(decor(1, a, b, c))), "a11y.traversal.order")
    got = sorted((x.severity, x.evidence["reason"], x.node_key) for x in f)
    assert got == [("error", "cycle", "view:10"), ("info", "dangling", "view:12")]
    cyc = [x for x in f if x.evidence["reason"] == "cycle"][0]
    assert cyc.evidence["cycle"] == ["view:10", "view:11"]


def test_r17_linear_chain_passes():
    b_id = L.a11y_node_id(11, -1)
    a = view(10, "android.widget.TextView", text="A", b=(40, 100, 400, 60), traversal_before=b_id)
    b = view(11, "android.widget.TextView", text="B", b=(40, 200, 400, 60))
    assert of(lint(screen(decor(1, a, b))), "a11y.traversal.order") == []


# --------------------------------------------------------------------------- #
# A View-only screen produces findings (the ViewScenarioActivity BAD variants).
# --------------------------------------------------------------------------- #
def test_view_only_screen_produces_findings():
    ib = view(10, "android.widget.ImageButton", flags=CLICK, b=(48, 149, 144, 144))
    small = view(11, "android.widget.Button", flags=CLICK, text="Play", b=(48, 418, 90, 90))
    iv = view(12, "android.widget.ImageView", important_for_accessibility="YES",
              b=(48, 1451, 192, 192))
    role = view(13, "android.widget.TextView", flags=CLICK, text="Submit", b=(48, 1768, 205, 144))
    field = view(14, "android.widget.EditText", flags=CLICK + ("editable",), b=(48, 2438, 1184, 144))
    fixed = view(15, "android.widget.TextView", text="Fixed size", b=(48, 900, 500, 60),
                 text_size_px=42.0)
    ll = view(5, "android.widget.LinearLayout", b=(0, 0, 1280, 2856),
              kids=[ib, small, iv, role, field, fixed])
    rep = lint(screen(decor(1, ll, b=(0, 0, 1280, 2856))), density=420)
    got = {(f.alias, f.node_key) for f in rep.findings}
    assert ("R1", "view:10") in got
    assert ("R2", "view:11") in got
    assert ("R6", "view:12") in got
    assert ("R5", "view:13") in got
    assert ("R16", "view:14") in got
    assert ("R11", "view:15") in got


# --------------------------------------------------------------------------- #
# Rule id validation + diagnostics.
# --------------------------------------------------------------------------- #
def test_rule_aliases_and_atf_names_resolve():
    assert L.resolve_rule_ids(["R1", "r2", "TouchTargetSize", "a11y.contrast.low"]) == {
        "a11y.label.missing", "a11y.touch_target.small", "a11y.contrast.low"}
    assert L.resolve_rule_ids("R13,R14") == {"a11y.clickable.duplicate_bounds",
                                             "a11y.editable.content_description"}
    assert L.resolve_rule_ids(None) is None and L.resolve_rule_ids([]) is None
    assert len(L.ALL_RULE_IDS) == 18
    assert set(L.RULE_CHOICES) >= set(L.ALL_RULE_IDS) | {f"R{i}" for i in range(1, 19)}


def test_unknown_rule_id_raises_a_clear_error():
    with pytest.raises(L.UnknownRuleError) as ei:
        L.resolve_rule_ids(["R1", "a11y.bogus"])
    msg = str(ei.value)
    assert "'a11y.bogus'" in msg and "R1=a11y.label.missing" in msg
    with pytest.raises(L.UnknownRuleError):
        lint(screen(decor(1)), enabled=["nope"])


def test_enabled_subset_by_alias():
    rep = lint(screen(decor(1, view(11, "android.widget.ImageButton", flags=CLICK, b=(100, 100, 20, 20)))),
               enabled=["R2"])
    assert {f.rule for f in rep.findings} == {"a11y.touch_target.small"}


def test_rule_exceptions_surface_as_diagnostics(monkeypatch):
    def boom(n, run):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(L, "NODE_RULES", [("a11y.label.missing", boom)] + L.NODE_RULES[1:])
    rep = lint(screen(decor(1, view(11, "android.widget.ImageButton", flags=CLICK))))
    errs = [d for d in rep.diagnostics if d["code"] == "rule.error"]
    assert errs and errs[0]["rule"] == "a11y.label.missing" and "kaboom" in errs[0]["message"]
    assert rep.summary["rule_errors"] >= 1


def test_invisible_nodes_are_not_linted():
    hidden = view(11, "android.widget.ImageButton", flags=CLICK, visible=False)
    assert lint(screen(decor(1, hidden))).findings == []


# --------------------------------------------------------------------------- #
# Entry points: lint_a11y on unified data, run_lint device flow with a fake conn.
# --------------------------------------------------------------------------- #
def test_lint_a11y_accepts_unified_data_and_root_lists():
    data = screen(decor(1, view(11, "android.widget.ImageButton", flags=CLICK)))
    for arg in (data, [data["windows"][0]["root"]]):
        out = L.lint_a11y(arg, density=160)
        assert [f["node_key"] for f in out if f["rule"] == "a11y.label.missing"] == ["view:11"]
        assert all(isinstance(f, dict) for f in out)


def test_lint_a11y_nonpositive_density_falls_back():
    data = screen(decor(1, view(11, "android.widget.Button", flags=CLICK, text="x",
                                b=(100, 100, 90, 90))))
    # 90px is ~34dp at the 420dpi fallback -> flagged; with density=0 treated as px it
    # would have been 90dp and silently passed.
    out = L.lint_a11y(data, density=0)
    assert [f["rule"] for f in out if f["rule"] == "a11y.touch_target.small"]


def _abgr_screenshot(w, h, rgba, scale=1.0):
    raw = struct.pack("<ii", w, h) + bytes([2]) + rgba
    return pb.Screenshot(format=pb.Screenshot.BITMAP, width=w, height=h,
                         bitmap_type=2, data=zlib.compress(raw), scale=scale)


class _FakeConn:
    def __init__(self, images):
        self.images = images
        self.calls: List[tuple] = []

    def dump_a11y(self, **kw):
        self.calls.append(("dump_a11y", kw))
        return pb.DumpA11yResponse()

    def dump_compose(self, **kw):
        self.calls.append(("dump_compose", kw))
        return pb.DumpComposeResponse()

    def screenshot(self, root_id=0, scale=1.0):
        self.calls.append(("screenshot", {"root_id": root_id, "scale": scale}))
        w, h, rgba = self.images[root_id]
        return pb.ScreenshotResponse(screenshot=_abgr_screenshot(w, h, rgba, scale))


def test_run_lint_requests_rendering_info_and_no_recomposition():
    conn = _FakeConn({})
    rep = L.run_lint(conn, density=420, include_contrast=False)
    kinds = dict(conn.calls)
    assert kinds["dump_a11y"]["include_rendering_info"] is True
    assert kinds["dump_compose"]["enable_inspection"] is False
    assert kinds["dump_compose"]["include_slot_table"] is False
    assert rep.findings == []


def test_run_lint_captures_each_text_window_for_contrast():
    img = _text_image(240, 50, (0xC8, 0xC8, 0xC8), stroke=2, aa=False)
    data = _text_screen(text_px=14, b=(0, 0, 240, 50))
    data["windows"][0]["root"]["bounds"]["layout"].update({"w": 240, "h": 50})
    conn = _FakeConn({1: (240, 50, img)})
    rep = L.run_lint(conn, density=160, a11y_data=data)
    assert ("screenshot", {"root_id": 1, "scale": 1.0}) in conn.calls
    f = of(rep, "a11y.contrast.low")
    assert len(f) == 1 and f[0].evidence["sample"] == "window:1"
    assert rep.stats["contrast_windows"] == [1]
    d = rep.to_dict()
    assert d["summary"]["by_rule"]["a11y.contrast.low"] == 1
    assert "a11y.contrast.low" in L.format_text(rep)


def test_run_lint_rejects_unknown_rules_before_touching_the_device():
    conn = _FakeConn({})
    with pytest.raises(L.UnknownRuleError):
        L.run_lint(conn, density=420, rules=["R99"])
    assert conn.calls == []


# --------------------------------------------------------------------------- #
# Clipping refinements (from the live S2 RecyclerView dump).
# --------------------------------------------------------------------------- #
def test_r1_clipped_row_named_by_offscreen_child_is_not_unlabelled():
    txt = view(51, "android.widget.TextView", text="Item 26", b=(39, 2117, 1742, -43), visible=False)
    row = view(50, "android.widget.LinearLayout", flags=CLICK, b=(0, 2066, 1080, 8), kids=[txt])
    rv = view(20, "androidx.recyclerview.widget.RecyclerView", flags=("scrollable",),
              b=(0, 260, 1080, 1814), kids=[row], collection_info={"row_count": -1})
    rep = lint(screen(decor(1, rv)))
    assert of(rep, "a11y.label.missing") == []
    assert [f.severity for f in of(rep, "a11y.touch_target.small")] == ["info"]


def test_r2_only_unclipped_dimensions_decide_severity():
    # Height is clipped by the list's top edge (and tiny); width is a real 44dp.
    ib = view(21, "android.widget.ImageButton", flags=CLICK, cd="Delete", b=(900, 260, 44, 10))
    rv = view(20, "androidx.recyclerview.widget.RecyclerView", flags=("scrollable",),
              b=(0, 260, 1080, 1814), kids=[ib])
    f = of(lint(screen(decor(1, rv))), "a11y.touch_target.small")
    assert [x.severity for x in f] == ["warn"]
    assert "height is clipped" in f[0].message and "h" in f[0].evidence["clipped_axes"]


def test_r12_flat_lazy_items_in_one_visual_row_are_still_duplicates():
    # A LazyColumn item without its own semantics node: its two buttons are direct
    # children of the list, but they sit in the same visual row.
    e1 = comp(20, 11, "android.widget.Button", flags=CLICK, text="Edit", b=(0, 300, 300, 160))
    e2 = comp(20, 12, "android.widget.Button", flags=CLICK, text="Edit", b=(400, 300, 300, 160))
    d1 = comp(20, 13, "android.widget.Button", flags=CLICK, text="Open", b=(0, 500, 300, 160))
    d2 = comp(20, 14, "android.widget.Button", flags=CLICK, text="Open", b=(0, 700, 300, 160))
    lazy = comp(20, 9, flags=("scrollable",), b=(0, 260, 1080, 1800), kids=[e1, e2, d1, d2],
                collection_info={"row_count": -1, "column_count": 1})
    rep = lint(screen(decor(1, view(20, ACV, b=(0, 0, 1080, 2400), kids=[lazy]))))
    assert sorted(keys(of(rep, "a11y.duplicate.label"))) == ["compose:20:11", "compose:20:12"]


# --------------------------------------------------------------------------- #
# End to end from the wire: DumpA11yResponse -> a11y.a11y_to_dict -> lint.
# Guards the lint's assumptions about the resolved dict shape.
# --------------------------------------------------------------------------- #
def _wire_response():
    strings: Dict[str, int] = {}

    def s(text):
        if text not in strings:
            strings[text] = len(strings) + 1
        return strings[text]

    def node(hv, vid, cls, x, y, w, h, **kw):
        n = pb.A11yNode(host_view_id=hv, virtual_id=vid, is_virtual=(vid != -1),
                        class_name=s(cls), visible_to_user=True, enabled=True, **kw)
        n.bounds.layout.x, n.bounds.layout.y = x, y
        n.bounds.layout.w, n.bounds.layout.h = w, h
        return n

    root = node(1, -1, "android.widget.FrameLayout", 0, 0, 1080, 2400)
    ib = node(11, -1, "android.widget.ImageButton", 100, 100, 160, 160, clickable=True,
              focusable=True)
    ib.actions.add(id=0x10)
    tv = node(12, -1, "android.widget.TextView", 100, 400, 600, 60, text=s("Fixed"),
              text_size_px=42.0, text_size_unit=0)
    acv = node(20, -1, ACV, 0, 600, 1080, 800, provider_class=s(ACV))
    btn = node(20, 7, "android.widget.Button", 100, 700, 160, 160, clickable=True,
               focusable=True)
    btn.actions.add(id=0x10)
    acv.children.append(btn)
    root.children.extend([ib, tv, acv])
    resp = pb.DumpA11yResponse()
    w = resp.windows.add(root_view_id=1)
    w.root.CopyFrom(root)
    for text, sid in strings.items():
        resp.strings.entries.add(id=sid, str=text)
    return resp


def test_end_to_end_from_the_wire():
    from inspector_widget import a11y as a11ymod
    data = a11ymod.a11y_to_dict(_wire_response())
    rep = lint(data, density=160)
    assert sorted(keys(of(rep, "a11y.label.missing"))) == ["compose:20:7", "view:11"]
    f11 = of(rep, "a11y.text.fixed_scaling")
    assert [(x.node_key, x.evidence["unit"]) for x in f11] == [("view:12", "px")]
    btn = [x for x in of(rep, "a11y.label.missing") if x.node_key == "compose:20:7"][0]
    assert btn.node["id"] == L.a11y_node_id(20, 7)


def test_r2_vertical_list_does_not_clip_width():
    # A 40dp button flush with the left edge of a vertical list: its width is real.
    btn = comp(20, 11, "android.widget.Button", flags=CLICK, cd="Back", b=(0, 600, 40, 40))
    lazy = comp(20, 9, flags=("scrollable",), b=(0, 260, 1080, 1800), kids=[btn],
                actions=["SCROLL_FORWARD", "SCROLL_DOWN"],
                collection_info={"row_count": -1, "column_count": 1})
    f = of(lint(screen(decor(1, view(20, ACV, b=(0, 0, 1080, 2400), kids=[lazy])))),
           "a11y.touch_target.small")
    assert [(x.severity, "clipped_axes" in x.evidence) for x in f] == [("warn", False)]


# --------------------------------------------------------------------------- #
# Live-verified Compose shapes (emulator, Compose ui 1.7): synthetic role /
# contentDescription children, state-only toggles, touch bounds vs layout size.
# Density 390 is the emulator's: 48dp = 117px.
# --------------------------------------------------------------------------- #
def test_r4_compose_button_whose_description_repeats_its_text_child():
    # Button(Modifier.semantics { contentDescription = "Next" }) { Text("Next") }: Compose
    # serves the description on a synthetic child (id + 2e9) and the role on another
    # (id + 1e9), so TalkBack reads "Next, Next, button".
    fake_cd = comp(20, 31 + 2_000_000_000, cd="Next", b=(39, 686, 190, 117))
    fake_role = comp(20, 31 + 1_000_000_000, "android.widget.Button", b=(39, 686, 190, 117))
    txt = comp(20, 33, "android.widget.TextView", text="Next", b=(80, 720, 100, 50))
    btn = comp(20, 31, flags=CLICK + ("screen_reader_focusable",), b=(39, 686, 190, 117),
               kids=[fake_cd, fake_role, txt])
    rep = lint(screen(decor(1, view(20, ACV, b=(0, 0, 1080, 2400), kids=[btn]))))
    f = of(rep, "a11y.label.redundant")
    assert [(x.node_key, x.evidence["reason"]) for x in f] == [("compose:20:31", "equals_text")]
    # the synthetic nodes are folded into the button, not reported on their own
    assert all(":2000000031" not in (x.node_key or "") for x in rep.findings)
    assert of(rep, "a11y.role.missing_on_clickable") == []


def test_r1_bare_switch_that_only_speaks_its_state_is_unlabeled():
    sw = comp(20, 30, flags=CLICK + ("checkable",), state="On", b=(39, 686, 127, 117))
    rep = lint(screen(decor(1, view(20, ACV, b=(0, 0, 1080, 2400), kids=[sw]))))
    f = of(rep, "a11y.label.missing")
    assert keys(f) == ["compose:20:30"] and "state, not what it is" in f[0].message
    named = comp(20, 30, flags=CLICK + ("checkable",), state="On", cd="Wi-Fi",
                 b=(39, 686, 127, 117))
    rep2 = lint(screen(decor(1, view(20, ACV, b=(0, 0, 1080, 2400), kids=[named]))))
    assert of(rep2, "a11y.label.missing") == []


def test_r2_48dp_target_rounded_to_116px_passes():
    b = comp(20, 39, flags=CLICK, cd="Delete", b=(1940, 1060, 116, 116))
    rep = lint(screen(decor(1, view(20, ACV, b=(0, 0, 2076, 2152), kids=[b]))), density=390)
    assert of(rep, "a11y.touch_target.small") == []


def test_r2_compose_small_layout_behind_widened_touch_bounds_warns():
    # Modifier.size(32.dp).clickable: touch bounds widened to 48dp, layout 32dp.
    bare = comp(20, 29, flags=CLICK, cd="32dp button", b=(20, 666, 117, 117),
                layout_size={"w": 78, "h": 78})
    # Material IconButton / Checkbox: minimumInteractiveComponentSize reserves 48dp.
    material = comp(20, 25, flags=CLICK, cd="Add", b=(20, 900, 117, 117),
                    layout_size={"w": 117, "h": 117})
    host = view(20, ACV, b=(0, 0, 2076, 2152), kids=[bare, material], provider_class=ACV)
    f = of(lint(screen(decor(1, host)), density=390), "a11y.touch_target.small")
    assert [(x.node_key, x.severity) for x in f] == [("compose:20:29", "warn")]
    assert f[0].evidence["w_dp"] == 32.0 and f[0].evidence["touch_w_dp"] == 48.0
    assert "minimumInteractiveComponentSize" in f[0].message
    # a View's layout_size (LayoutParams) never overrides its real touch bounds
    v = view(30, "android.widget.ImageButton", flags=CLICK, cd="Info", b=(20, 1200, 117, 117),
             layout_size={"w": 59, "h": 59})
    assert of(lint(screen(decor(1, v)), density=390), "a11y.touch_target.small") == []


def test_r15_per_row_action_in_a_list_takes_its_purpose_from_the_row():
    def row(i):
        more = view(100 + i, "android.widget.ImageButton", flags=CLICK, cd="More info",
                    b=(900, 300 + i * 200, 160, 160))
        title = view(200 + i, "android.widget.TextView", text=f"Item {i}", b=(40, 300 + i * 200, 600, 60))
        return view(300 + i, "android.widget.LinearLayout", b=(0, 300 + i * 200, 1080, 180),
                    kids=[title, more], collection_item_info={"row_index": i})
    rv = view(20, "androidx.recyclerview.widget.RecyclerView", flags=("scrollable",),
              b=(0, 260, 1080, 1814), kids=[row(0), row(1)], collection_info={"row_count": 2})
    alone = view(30, "android.widget.Button", flags=CLICK, text="More info", b=(40, 2100, 400, 160))
    f = of(lint(screen(decor(1, rv, alone))), "a11y.link.purpose_unclear")
    assert [(x.node_key, x.severity) for x in f] == [("view:30", "info")]


def test_r3_button_text_is_measured_against_its_fill_not_the_inset_rim():
    # Live D1 "OPEN DIALOG": a MaterialButton's a11y bounds include top/bottom insets where
    # the window (#FEF7FF) shows, so the rim is window colour while the text (#1D1B20)
    # sits on the grey fill (#D6D7D7). Measuring the fill as "text" gave 1.37:1.
    win, fill, ink = (0xFE, 0xF7, 0xFF), (0xD6, 0xD7, 0xD7), (0x1D, 0x1B, 0x20)
    w, h = 240, 60
    body = _text_image(w, 40, ink, stroke=2, aa=True, bg=fill, period=20)
    rim = bytes((*win, 255)) * (w * 10)
    img = rim + body + rim
    tv = view(10, "android.widget.Button", text="OPEN DIALOG", flags=CLICK, b=(0, 0, w, h),
              text_size_px=14, text_size_unit=2)
    rep = lint(screen(decor(1, tv, b=(0, 0, 1080, 2400))), window_images=_ctx_img(img, w, h))
    assert of(rep, "a11y.contrast.low") == []
    # the same text actually drawn in the fill colour's neighbourhood still fails
    faint = _text_image(w, 40, (0xB0, 0xB0, 0xB0), stroke=2, aa=True, bg=fill, period=20)
    rep2 = lint(screen(decor(1, tv, b=(0, 0, 1080, 2400))),
                window_images=_ctx_img(rim + faint + rim, w, h))
    f = of(rep2, "a11y.contrast.low")
    assert len(f) == 1 and f[0].evidence["bg_hex"] == "#D6D7D7" and f[0].evidence["fg_hex"] == "#B0B0B0"


# --------------------------------------------------------------------------- #
# Rules judged as TalkBack reads the screen.
# --------------------------------------------------------------------------- #
FAKE_ROLE = 1_000_000_000


def _view_tab(hv, x, label, selected, with_info=True):
    fl = ("focusable",) + (("selected",) if selected else ("clickable",))
    extra = {"collection_item_info": {"row_index": 0, "column_index": x // 360, "row_span": 1,
                                      "column_span": 1, "heading": False,
                                      "selected": selected}} if with_info else {}
    return view(hv, "android.widget.LinearLayout", role_description="Tab", flags=fl,
                b=(x, 200, 360, 147), **extra,
                kids=[view(hv + 100, "android.widget.TextView", text=label,
                           b=(x + 100, 250, 160, 47))])


def test_r7_unselected_material_tabs_are_not_stateless():
    # TabLayout.TabView / NavigationBarItemView: roleDescription "Tab", CollectionItemInfo
    # (selected), and only the unselected tabs are clickable.
    strip = view(10, "android.widget.HorizontalScrollView", b=(0, 200, 1080, 147),
                 collection_info={"row_count": 1, "column_count": 3},
                 kids=[_view_tab(11, 0, "Photos", True), _view_tab(12, 360, "Albums", False),
                       _view_tab(13, 720, "Shared", False)])
    assert of(lint(screen(decor(1, strip)), density=420), "a11y.state.not_exposed") == []
    # Without CollectionItemInfo, a selected sibling still tells TalkBack users the state.
    bare = view(10, "android.widget.LinearLayout", b=(0, 200, 1080, 147),
                kids=[_view_tab(11, 0, "Photos", True, False), _view_tab(12, 360, "Albums", False, False)])
    assert of(lint(screen(decor(1, bare)), density=420), "a11y.state.not_exposed") == []


def test_r7_a_lone_tab_without_selection_still_warns_with_a_tab_fix():
    tab = _view_tab(12, 0, "Albums", False, with_info=False)
    f = of(lint(screen(decor(1, view(10, "android.widget.LinearLayout", b=(0, 200, 1080, 147),
                                     kids=[tab]))), density=420), "a11y.state.not_exposed")
    assert [x.node_key for x in f] == ["view:12"]
    assert "setSelected" in f[0].message and "CompoundButton" not in f[0].message


def _m3_tabs():
    def ctab(sem, x, label, selected):
        fl = CLICK + ("screen_reader_focusable",) + (("selected",) if selected else ())
        return comp(50, sem, flags=fl, b=(x, 200, 360, 147), layout_size={"w": 360, "h": 147},
                    kids=[comp(50, sem + 1, "android.widget.TextView", text=label,
                               b=(x + 100, 250, 160, 47)),
                          comp(50, sem + FAKE_ROLE, role_description="Tab", b=(x, 200, 360, 147))])
    return screen(view(50, ACV, provider_class=ACV, b=(0, 0, 1080, 2400), kids=[
        comp(50, 1, b=(0, 200, 1080, 147), kids=[
            ctab(2, 0, "Photos", True), ctab(4, 360, "Albums", False), ctab(6, 720, "Shared", False)])]))


def test_compose_tab_role_rides_on_the_fake_childs_role_description():
    # Compose serves Role.Tab / Role.Switch of a merging node on its synthetic role child as
    # roleDescription (class android.view.View); the fold must pick it up, with or without
    # the Compose join, so no "clickable without a role" (R5) and no R7 on unselected tabs.
    rep = lint(_m3_tabs(), density=420)
    assert of(rep, "a11y.role.missing_on_clickable") == []
    assert of(rep, "a11y.state.not_exposed") == []
    sw = comp(60, 3, flags=CLICK + ("screen_reader_focusable",), b=(0, 400, 1080, 147),
              kids=[comp(60, 4, "android.widget.TextView", text="Wi-Fi", b=(40, 440, 400, 60)),
                    comp(60, 3 + FAKE_ROLE, role_description="Switch", b=(0, 400, 1080, 147))])
    rep = lint(screen(view(60, ACV, provider_class=ACV, b=(0, 0, 1080, 2400), kids=[sw])), density=420)
    assert [(f.alias, f.node_key) for f in rep.findings
            if f.alias in ("R5", "R7")] == [("R7", "compose:60:3")]


def _blend(fg, bg, a):
    return tuple(round(f * a + b * (1 - a)) for f, b in zip(fg, bg))


def test_r3_skips_the_label_of_a_disabled_compose_button():
    # M3 disabled Button: container onSurface@12%, content onSurface@38% (~2.3:1). Compose
    # puts the label on a child Text whose own node reports enabled; WCAG 1.4.3 exempts
    # inactive components.
    surface, on = (0xFE, 0xF7, 0xFF), (0x1D, 0x1B, 0x20)
    cont = _blend(on, surface, 0.12)
    txt = _blend(on, cont, 0.38)
    W, H = 1080, 600
    px = bytearray()
    for y in range(H):
        for x in range(W):
            c = surface
            if 100 <= x < 500 and 100 <= y < 205:
                c = cont
            if 180 <= x < 420 and 135 <= y < 170 and ((x // 3 + y // 3) % 2 == 0):
                c = txt
            px += bytes((c[0], c[1], c[2], 255))
    label = comp(20, 9, "android.widget.TextView", text="Continue", b=(180, 130, 240, 45))
    btn = comp(20, 7, "android.widget.Button", flags=CLICK + ("screen_reader_focusable",),
               b=(100, 90, 400, 126), layout_size={"w": 400, "h": 126}, kids=[label])
    btn["flags"].remove("enabled")
    data = screen(view(20, ACV, provider_class=ACV, b=(0, 0, W, H), kids=[btn]))

    def contrast_findings():
        ctx = L.LintContext(density=420)
        ctx.window_images[20] = L.WindowImage(W, H, bytes(px), 1.0, 0, 0, 20)
        return of(L.lint_unified(data, ctx), "a11y.contrast.low")

    assert contrast_findings() == []
    btn["flags"].append("enabled")  # the same pixels on an enabled button are a real failure
    assert [f.node_key for f in contrast_findings()] == ["compose:20:9"]


def test_r4_judges_a_merged_icon_by_the_role_talkback_announces():
    # IconButton { Icon(Icons.Default.Upload, contentDescription = "Upload image") }:
    # TalkBack focuses the IconButton and says "Upload image, button".
    ib = comp(30, 5, flags=CLICK + ("screen_reader_focusable",), b=(100, 100, 126, 126),
              layout_size={"w": 126, "h": 126},
              kids=[comp(30, 6, cd="Upload image", b=(134, 134, 58, 58)),
                    comp(30, 5 + FAKE_ROLE, "android.widget.Button", b=(100, 100, 126, 126))])
    data = screen(view(30, ACV, provider_class=ACV, b=(0, 0, 1080, 2400), kids=[ib]))
    cd = {"windows": [{"view_id": 30, "root": {"kind": "SEMANTICS", "id": 5,
                                               "attrs": {"Role": "Button", "OnClick": "x"},
                                               "children": [{"kind": "SEMANTICS", "id": 6, "attrs": {
                                                   "ContentDescription": "[Upload image]",
                                                   "Role": "Image"}}]}}]}
    assert of(lint(data, density=420, compose=cd), "a11y.label.redundant") == []
    assert of(lint(data, density=420), "a11y.label.redundant") == []
    # A standalone image (its own stop) that says "image" is still redundant.
    logo = comp(30, 8, "android.widget.ImageView", cd="Company logo image", b=(100, 400, 200, 200))
    data = screen(view(30, ACV, provider_class=ACV, b=(0, 0, 1080, 2400), kids=[logo]))
    assert [f.node_key for f in of(lint(data, density=420), "a11y.label.redundant")] == ["compose:30:8"]


def _article(scroll_focusable: bool):
    texts = [view(10 + i, "android.widget.TextView", text=f"Paragraph {i} of the article body.",
                  important_for_accessibility="YES", b=(0, 100 + i * 110, 1080, 100))
             for i in range(20)]
    fl = ("focusable", "scrollable") if scroll_focusable else ("scrollable",)
    return screen(decor(1, view(2, "android.widget.ScrollView", flags=fl, actions=["SCROLL_FORWARD"],
                                b=(0, 0, 1080, 2400),
                                kids=[view(3, "android.widget.LinearLayout", b=(0, 0, 1080, 2400),
                                           kids=texts)])))


def test_r9_fires_inside_a_focusable_scroll_view():
    # ScrollView is focusable by default; its content is still read item by item.
    for focusable in (True, False):
        f = of(lint(_article(focusable), density=420, enabled=["R9"]), "a11y.heading.structure")
        assert [x.evidence["reason"] for x in f] == ["no_headings"], focusable


def test_r10_fires_for_rows_under_a_focusable_recycler_view():
    def row(hv, y, a, b):
        return view(hv, "android.widget.LinearLayout", b=(0, y, 1080, 150),
                    kids=[view(hv + 1, "android.widget.TextView", text=a, b=(48, y + 20, 600, 50)),
                          view(hv + 2, "android.widget.TextView", text=b, b=(48, y + 75, 600, 50))])
    grid = view(20, "android.widget.LinearLayout", b=(0, 200, 1080, 400), kids=[
        view(21, "android.widget.TextView", text="Name", b=(48, 220, 600, 50)),
        view(22, "android.widget.TextView", text="Ada Lovelace", b=(48, 275, 600, 50)),
        view(23, "android.widget.TextView", text="Analyst", b=(48, 330, 600, 50))])
    scroll = view(2, "androidx.core.widget.NestedScrollView", flags=("focusable", "scrollable"),
                  b=(0, 0, 1080, 2400), kids=[grid])
    f = of(lint(screen(decor(1, scroll)), density=420, enabled=["R10"]), "a11y.grouping.missing")
    assert [x.node_key for x in f] == ["view:20"]


def test_r14_stock_m3_search_field_is_info_and_the_message_is_accurate():
    sb = comp(40, 3, "android.widget.EditText", cd="Search",
              flags=CLICK + ("editable", "screen_reader_focusable"), b=(40, 150, 1000, 147),
              kids=[comp(40, 4, "android.widget.TextView", text="Search messages", b=(150, 195, 500, 55))])
    f = of(lint(screen(view(40, ACV, provider_class=ACV, b=(0, 0, 1080, 2400), kids=[sb])),
                density=420), "a11y.editable.content_description")
    assert [(x.node_key, x.severity) for x in f] == [("compose:40:3", "info")]
    assert "SearchBar" in f[0].message
    own = view(7, "android.widget.EditText", cd="Email", flags=CLICK + ("editable",), b=(40, 400, 1000, 147))
    f = of(lint(screen(decor(1, own)), density=420), "a11y.editable.content_description")
    assert [(x.node_key, x.severity) for x in f] == [("view:7", "error")]
    assert "instead of the text the user typed" not in f[0].message
    assert "only while the field is empty" in f[0].message


def test_findings_on_a_window_under_a_modal_dialog_say_so():
    activity = decor(2, view(3, "android.widget.ImageButton", flags=CLICK, b=(100, 300, 160, 160)))
    dialog = view(9, "android.widget.FrameLayout", b=(100, 800, 880, 600),
                  kids=[view(10, "android.widget.ImageButton", flags=CLICK, b=(140, 840, 160, 160))])
    data = screen(activity, dialog)
    data["windows"][0]["covered_by"] = 9
    f = of(lint(data), "a11y.label.missing")
    by_key = {x.node_key: x for x in f}
    assert by_key["view:3"].window == {"index": 0, "root_view_id": 2, "covered_by": 9}
    assert "covered_by" not in by_key["view:10"].window
