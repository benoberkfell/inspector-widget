"""Offline tests for inspector_widget.normalize (spec section 3.7)."""

from __future__ import annotations

import copy
import os
import subprocess
import sys

import live_fixtures as lf
import pytest

from inspector_widget import normalize as nz
from inspector_widget import normalize_defaults as nd

HOST_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEXTSTYLE = (
    "TextStyle(color=Color(0.0, 0.0, 0.0, 0.0, None), brush=null, alpha=NaN, fontSize=16.0.sp, "
    "fontWeight=FontWeight(weight=400), fontStyle=null, fontSynthesis=null, "
    "fontFamily=FontFamily.SansSerif, fontFeatureSettings=null, letterSpacing=0.5.sp, "
    "baselineShift=null, textGeometricTransform=null, localeList=null, "
    "background=Color(0.0, 0.0, 0.0, 0.0, None), textDecoration=null, shadow=null, "
    "drawStyle=null, textAlign=Unspecified, textDirection=Unspecified, lineHeight=24.0.sp, "
    "textIndent=null, platformStyle=PlatformTextStyle(spanStyle=null, "
    "paragraphSyle=PlatformParagraphStyle(includeFontPadding=false, "
    "emojiSupportMatch=EmojiSupportMatch.Default)), lineHeightStyle=LineHeightStyle("
    "alignment=LineHeightStyle.Alignment.Center, trim=LineHeightStyle.Trim.None), "
    "lineBreak=LineBreak(strategy=Strategy.Unspecified, strictness=Strictness.Unspecified, "
    "wordBreak=WordBreak.Unspecified), hyphens=Hyphens.Unspecified, textMotion=null)")

MODIFIERS = (
    "semantics(mergeDescendants=true, properties={}) → clickable(enabled=true, "
    "onClick=com.oberkfell.a11yprobe.MainActivityKt$LauncherScreen$1$1$3$1$1@7057973, "
    "onClickLabel=, role=, interactionSource=, indicationNodeFactory=androidx.compose.material3."
    "RippleNodeFactory@91ef94e1) → testTag(tag=launch_heading) → background(color=Color("
    "0.99607843, 0.96862745, 1.0, 1.0, sRGB IEC61966-2.1), shape=RectangleShape) → "
    "graphicsLayer(scaleX=1.0, scaleY=1.0, alpha=1.0, clip=true) → semantics(mergeDescendants="
    "false, properties={IsTraversalGroup=true}) → pointerInput(key1=kotlin.Unit, key2=, keys=, "
    "pointerInputHandler=Function2<androidx.compose.ui.input.pointer.PointerInputScope, "
    "kotlin.coroutines.Continuation<? super kotlin.Unit>, java.lang.Object>)")


# --------------------------------------------------------------------------- formatting
def test_cap_and_colours():
    assert nz.cap("abc", 5) == "abc"
    assert nz.cap("x" * 130) == "x" * 120 + "…(+10)"
    assert nz.color_hex(-16777216) == "#FF000000"
    assert nz.color_hex(0x80FF0000) == "#80FF0000"
    assert nz.color_hex(-15066598, short=True) == "#1A1A1A"
    assert nz.color_hex(0x80FF0000, short=True) == "#80FF0000"  # short only when opaque
    # Compose packs sRGB ARGB into the high 32 bits of a ULong (printed signed)
    assert nz.packed_color("-63864995460415488") == "#FF1D1B20"
    assert nz.packed_color("-63864995460415488", short=True) == "#1D1B20"
    assert nz.packed_color("16") is None  # Color.Unspecified
    assert nz.packed_color("17") is None and nz.packed_color("abc") is None


def test_rect_list_and_resources():
    assert nz.rect_list({"layout": {"x": 1, "y": 2, "w": 3, "h": 4}}) == [1, 2, 3, 4]
    assert nz.rect_list({"x": 1, "y": 2, "w": 3, "h": 4, "render_quad": [[0, 0]]}) == [1, 2, 3, 4]
    assert nz.rect_list([1, 2, 3, 4]) == [1, 2, 3, 4]
    assert nz.rect_list(None) is None and nz.rect_list({"nope": 1}) is None
    assert nz.render_quad({"layout": {}, "render": {"x0": 1}}) == {"x0": 1}
    assert nz.render_quad({"x": 0, "render_quad": [[1, 2]]}) == [[1, 2]]
    assert nz.resource_str({"namespace": "android", "type": "id", "name": "content"}) == \
        "@android:id/content"
    assert nz.resource_str({"type": "id", "name": "x"}) == "@id/x"
    assert nz.resource_str({"namespace": "a", "type": "id", "ref": "@id/q"}) == "@id/q"
    assert nz.resource_str("plain") == "plain"


# --------------------------------------------------------------------------- compose values
@pytest.mark.parametrize("key,raw,expected", [
    ("text", "null", None),
    ("text", "", None),
    ("modifier", "Modifier", None),
    ("alpha", "NaN", None),
    ("fontSize", "2143289344", None),  # TextUnit.Unspecified
    ("letterSpacing", "2143289344", None),
    ("color", "16", None),  # Color.Unspecified on a colour key ...
    ("index", "16", "16"),  # ... but a plain 16 elsewhere
    ("tmp0_rcvr", "androidx.compose.foundation.lazy.LazyListItemProviderImpl@ee4a3eb", None),
    ("itemProvider", "property value (Kotlin reflection is not available)", None),
    ("content", "androidx.compose.runtime.internal.ComposableLambdaImpl@40d9548", "λ"),
    ("headlineContent", "androidx.compose.runtime.internal.ComposableLambdaImpl@59d46d7", "λ"),
    ("onClick", "Function0<kotlin.Unit>", "λ"),
    ("title", "androidx.compose.runtime.internal.ComposableLambdaImpl@546be57", None),
    ("onDraw", "Function1<androidx.compose.ui.graphics.drawscope.DrawScope, kotlin.Unit>", "λ"),
    ("measurePolicy", "Function2<a.B, c.D>", None),
    ("scrolledOffset",
     "androidx.compose.material3.AppBarKt$SingleRowTopAppBar$3$$ExternalSyntheticLambda0@d6e7e2d",
     None),
    ("colors", "androidx.compose.material3.ListItemColors@3a7a2e", "ListItemColors"),
    ("owner", ("androidx.compose.ui.platform.AndroidComposeView{a41977d VFED..... ........ "
               "0,0-1280,2856}"), "AndroidComposeView"),
    ("maxLines", "2147483647", "inf"),
    ("maxLines", "3", "3"),
    ("overflow", "1", "Clip"),
    ("overflow", "2", "Ellipsis"),
    ("textAlign", "3", "Center"),
    ("color", "-63864995460415488", "#FF1D1B20"),
    ("contentColor", "-51433715133317120", nz.packed_color("-51433715133317120")),
    ("style", TEXTSTYLE, "16sp/24sp w400 ls0.5sp"),
    ("typography", "Typography(displayLarge=TextStyle(color=...))", "Typography"),
    ("colorScheme", "ColorScheme(primary=Color(0.4, 0.3, 0.6, 1.0, sRGB IEC61966-2.1))",
     "ColorScheme"),
    ("modifiers", "fillMaxWidth(fraction=1.0) → height → drawBehind(onDraw=Function1<a, b>)",
     "fillMaxWidth,height,drawBehind"),
    ("modifier", ("[androidx.compose.ui.ComposedModifier@f014a9, "
                  "androidx.compose.ui.platform.TestTagElement@97d6e576]"), "composed,testTag"),
    ("contentPadding", "PaddingValues(start=0.0.dp, top=116.0.dp, end=0.0.dp, bottom=24.0.dp)",
     "PaddingValues(start=0dp, top=116dp, end=0dp, bottom=24dp)"),
    ("thickness", "1.0", "1.0"),
    ("shape", "RoundedCornerShape(topStart = CornerSize(size = 4.0.dp))",
     "RoundedCornerShape(topStart = CornerSize(size = 4dp))"),
    ("background", "Color(0.99607843, 0.96862745, 1.0, 1.0, sRGB IEC61966-2.1)", "#FFFEF7FF"),
    ("text", "Section heading", "Section heading"),
])
def test_compose_value_rules(key, raw, expected):
    assert nz.compose_value(key, raw) == expected


def test_compose_value_caps_long_values():
    v = nz.compose_value("text", "y" * 300)
    assert v == "y" * 120 + "…(+180)"
    assert nz.compose_value("text", None) is None


def test_textstyle_brief_keeps_only_non_defaults():
    assert nz.textstyle_brief(TEXTSTYLE) == "16sp/24sp w400 ls0.5sp"
    coloured = TEXTSTYLE.replace("color=Color(0.0, 0.0, 0.0, 0.0, None)",
                                 "color=Color(0.11372549, 0.105882354, 0.1254902, 1.0, sRGB)")
    assert nz.textstyle_brief(coloured) == "16sp/24sp w400 ls0.5sp #1D1B20"
    fancy = ("TextStyle(color=Color(1.0, 0.0, 0.0, 0.5, sRGB), fontSize=12.0.sp, "
             "fontWeight=FontWeight(weight=700), fontStyle=FontStyle.Italic, "
             "fontFamily=FontFamily.Monospace, lineHeight=TextUnit.Unspecified, "
             "textAlign=Center, textDecoration=TextDecoration.Underline)")
    assert nz.textstyle_brief(fancy) == \
        "12sp w700 italic Monospace #80FF0000 align=Center TextDecoration.Underline"
    assert nz.textstyle_brief("TextStyle(lineHeight=20.0.sp)") == "lh20sp"
    assert nz.textstyle_brief("TextStyle()") == "TextStyle()"
    assert nz.textstyle_brief("NotAStyle(x=1)") == "NotAStyle(x=1)"


def test_modifier_brief_keeps_key_arguments():
    assert nz.modifier_brief(MODIFIERS) == (
        "semantics(merge),clickable,testTag(launch_heading),background(#FFFEF7FF),"
        "graphicsLayer,semantics,pointerInput")
    assert nz.modifier_brief(
        "padding(paddingValues=PaddingValues(start=0.0.dp, top=116.0.dp, end=0.0.dp, "
        "bottom=24.0.dp))") == "padding(top=116dp,bottom=24dp)"
    assert nz.modifier_brief("layoutId → padding(horizontal=4.0.dp, vertical=0.0.dp)") == \
        "layoutId,padding(horizontal=4dp)"
    assert nz.modifier_brief("fillMaxWidth(fraction=0.5) → size(width=48.0.dp, height=48.0.dp)"
                             ) == "fillMaxWidth(0.5),size(width=48dp,height=48dp)"
    assert nz.modifier_brief("clickable(enabled=false, role=Button)") == \
        "clickable(disabled,Button)"
    assert nz.modifier_brief("[a.b.FillElement@1, a.b.SizeElement@2, a.b.PaddingElement@3]") == \
        "fill,size,padding"


def test_action_attrs_and_compose_attrs_brief():
    assert nz.is_action_attr("AccessibilityAction(label=null, action=Function0<java.lang.Boolean>)")
    assert nz.is_action_attr("Function1<java.lang.Object, java.lang.Integer>")
    assert not nz.is_action_attr("ScrollAxisRange(value=0.0, maxValue=100.0)")
    assert not nz.is_action_attr(None)
    attrs = {
        "TestTag": "launch_heading", "Text": "Section heading, MissingHeading",
        "Focused": "false",
        "OnClick": "AccessibilityAction(label=null, action=Function0<java.lang.Boolean>)",
        "SetTextSubstitution": "AccessibilityAction(label=null, action=Function1<a, b>)",
        "ShowTextSubstitution": "AccessibilityAction(label=null, action=Function1<a, b>)",
        "ClearTextSubstitution": "AccessibilityAction(label=null, action=Function0<b>)",
        "GetTextLayoutResult": "AccessibilityAction(label=null, action=Function1<c, d>)",
        "CollectionInfo": "androidx.compose.ui.semantics.CollectionInfo@9f1617c",
        "Unused": "null",
    }
    counts: dict = {}
    values, actions = nz.compose_attrs_brief(attrs, counts)
    assert values == {"TestTag": "launch_heading", "Text": "Section heading, MissingHeading",
                      "Focused": "false", "CollectionInfo": "CollectionInfo"}
    assert actions == ["OnClick", "GetTextLayoutResult"]
    assert counts == {"actions": 3, "attrs": 1}
    assert nz.compose_attrs_brief(None) == ({}, [])


# --------------------------------------------------------------------------- library classifier
def test_library_classifier():
    assert nz.is_library_source("ListItem.kt:163")
    assert nz.is_library_source("LazyLayoutItemContentFactory.kt:101")
    assert nz.is_library_source("AndroidCompositionLocals.android.kt:111")
    assert nz.is_library_source(None) and nz.is_library_source("")
    assert not nz.is_library_source("MainActivity.kt:150")
    assert not nz.is_library_source("com/example/ui/FeedRow.kt:42")
    assert nz.origin_of("ListItem", "MainActivity.kt:150") == "app"
    assert nz.origin_of("Surface", "ListItem.kt:163") == "library"
    assert nz.origin_of("colors", "MainActivity.kt:151") == "library"  # lower-case name
    assert nz.origin_of("LaunchedEffect", None) == "library"
    assert len(nd.LIBRARY_FILES) > 300 and "MainActivity" not in nd.LIBRARY_FILES


def test_library_classifier_on_the_real_launcher():
    comp = lf.load("launcher", "compose_slots")
    origins = {}

    def walk(n):
        if n["kind"] == "COMPOSABLE":
            origins[(n["name"], n.get("source"))] = nz.origin_of(n["name"], n.get("source"))
        for c in n.get("children") or []:
            walk(c)

    walk(comp["windows"][0]["root"])
    app = {k for k, v in origins.items() if v == "app"}
    assert all(src and src.startswith("MainActivity.kt") for _, src in app)
    assert ("ListItem", "MainActivity.kt:150") in app
    assert ("Surface", "ListItem.kt:163") not in app


# --------------------------------------------------------------------------- accessibility
def test_a11y_node_brief_drop_rules_and_counts():
    node = {
        "host_view_id": 1, "virtual_id": 11, "id": 4294967307,
        "bounds": {"layout": {"x": 48, "y": 828, "w": 391, "h": 72}},
        "text": "Touch target size", "class_name": "android.widget.TextView",
        "package_name": "com.oberkfell.a11yprobe", "speakable": "Touch target size",
        "flags": ["enabled", "visible_to_user", "is_virtual"], "movement_granularities": 31,
        "max_text_length": -1, "text_selection_start": -1, "text_selection_end": -1,
        "actions_bitmask": 131904, "important_for_accessibility": "YES", "important": True,
        "actions": [{"id": 64, "name": "ACCESSIBILITY_FOCUS"}, {"id": 131072, "name": "SET_SELECTION"},
                    {"id": 16, "name": "CLICK"}, {"id": 16908342, "name": "CUSTOM_0x01020036"}],
        "extras": {"androidx.view.accessibility.AccessibilityNodeInfoCompat.SPANS_START_KEY": "[]",
                   "AccessibilityNodeInfo.roleDescription": "Tab"},
        "collection_info": {"row_count": -1, "column_count": 1, "hierarchical": False,
                            "selection_mode": 0},
        "role": None,
        "children": [{"id": 2}],
    }
    before = copy.deepcopy(node)
    counts: dict = {}
    b = nz.a11y_node_brief(node, "com.oberkfell.a11yprobe", counts)
    assert node == before  # not mutated
    assert b == {
        "host_view_id": 1, "virtual_id": 11, "id": 4294967307, "bounds": [48, 828, 391, 72],
        "text": "Touch target size", "class_name": "android.widget.TextView",
        "speakable": "Touch target size", "actions": ["CLICK", "CUSTOM_0x01020036"],
        "extras": {"AccessibilityNodeInfo.roleDescription": "Tab"},
        "collection_info": {"column_count": 1, "selection_mode": 0},
    }
    assert counts == {"actions": 2, "extras": 1, "defaults": 1}
    # default-true flags become disabled/hidden markers when absent
    assert nz.a11y_node_brief({"flags": ["clickable"]}, None)["flags"] == [
        "clickable", "disabled", "hidden"]
    assert nz.a11y_node_brief({}, None)["flags"] == ["disabled", "hidden"]
    assert "flags" not in nz.a11y_node_brief({"flags": ["enabled", "visible_to_user"]}, None)
    # a foreign package and a non-YES importance are kept
    kept = nz.a11y_node_brief({"package_name": "com.other", "important_for_accessibility": "NO",
                               "flags": ["enabled", "visible_to_user"]}, "com.app")
    assert kept == {"package_name": "com.other", "important_for_accessibility": "NO"}


# --------------------------------------------------------------------------- properties
def test_prop_value_normalizes_both_shapes():
    assert nz.prop_value({"type": "COLOR", "value": -16777216}) == "#FF000000"
    assert nz.prop_value({"type": "COLOR", "value": "#FF000000"}) == "#FF000000"  # legacy MCP
    assert nz.prop_value({"type": "GRAVITY", "value": 0, "label": "top|start"}) == "top|start"
    assert nz.prop_value({"type": "GRAVITY", "value": 0}) == 0  # legacy E3: label lost
    assert nz.prop_value({"type": "RESOURCE", "value": {"namespace": "android", "type": "id",
                                                       "name": "content", "ref": "@id/content"}}
                         ) == "@android:id/content"
    assert nz.prop_value({"type": "FLOAT", "value": 0.01785713993012905}) == 0.01785714
    assert nz.prop_value({"type": "FLOAT", "value": 42.0}) == 42.0
    assert nz.prop_value({"type": "DRAWABLE",
                          "value": "android.graphics.drawable.RippleDrawable"}) == "RippleDrawable"
    assert nz.prop_value({"type": "STRING", "value": "a.b.NotAClassName x"}) == "a.b.NotAClassName x"
    assert nz.prop_value({"type": "BOOLEAN", "value": True, "source": "@layout/x",
                          "resolution_stack": ["@layout/x"]}) == {
        "value": True, "source": "@layout/x", "stack": ["@layout/x"]}
    assert nz.props_to_map([{"name": "a", "type": "INT32", "value": 1},
                            {"name": "a", "type": "INT32", "value": 2}, {"type": "INT32"}]) == {"a": 1}


def test_class_family_by_props_then_name():
    assert nz.class_family("MaterialTextView", ["textSize"]) == "TextView"
    assert nz.class_family("SwitchMaterial", ["textSize", "checked"]) == "CompoundButton"
    assert nz.class_family("AppCompatEditText", ["textSize", "inputType"]) == "EditText"
    assert nz.class_family("MaterialButton", ["textSize"]) == "Button"
    assert nz.class_family("CustomThing", ["scaleType"]) == "ImageView"
    assert nz.class_family("FitWindowsLinearLayout", ["baselineAligned", "clipChildren"]) == \
        "LinearLayout"
    assert nz.class_family("ScrollView", ["fillViewport", "clipChildren"]) == "ScrollView"
    assert nz.class_family("DecorView", ["measureAllChildren", "clipChildren"]) == "FrameLayout"
    assert nz.class_family("AndroidComposeView", ["clipChildren"]) == "ViewGroup"
    assert nz.class_family("View", ["alpha"]) == "View"
    assert nz.class_family("MaterialTextView") == "TextView"  # name only
    assert nz.class_family(None) == "View"
    assert nz.key_props_for("CompoundButton")[:2] == ("visibility", "alpha")
    assert "checked" in nz.key_props_for("CompoundButton")
    assert "text" in nz.key_props_for("CompoundButton")
    assert set(nz.KEY_PROPS) == set(nd.FAMILY_PARENTS)


def test_static_defaults_and_pivots():
    assert nz.is_static_default("View", "alpha", 1.0)
    assert nz.is_static_default("View", "alpha", 1)  # int/float equal
    assert not nz.is_static_default("View", "alpha", 0.5)
    assert nz.is_static_default("View", "enabled", True)
    assert not nz.is_static_default("View", "enabled", 1)  # bool vs int never confused
    assert nz.is_static_default("View", "foregroundGravity", "top|start")
    assert nz.is_static_default("View", "foregroundGravity", 0)  # legacy E3 form
    assert nz.is_static_default("TextView", "maxLines", 2147483647)
    assert nz.is_static_default("CompoundButton", "breakStrategy", "simple")  # via Button
    assert not nz.is_static_default("TextView", "breakStrategy", "simple")
    assert nz.is_static_default("TextView", "baseline", 75)  # derived font metric
    assert nz.is_static_default("View", "transformPivotX", 640.0, [0, 0, 1280, 2856])
    assert nz.is_static_default("View", "transformPivotY", 62.5, [48, 48, 757, 125])
    assert not nz.is_static_default("View", "transformPivotX", 0.0, [0, 0, 1280, 2856])
    assert not nz.is_static_default("View", "transformPivotX", 640.0)  # no bounds: keep
    assert nz.static_default("View", "nope") == (False, None)


def test_nondefault_props_static_and_majority():
    props = {
        1: {"alpha": 1.0, "enabled": True, "text": "OK", "textColor": "#FF1A1A1A",
            "importantForAccessibility": "yes"},
        2: {"alpha": 1.0, "enabled": True, "text": "OK", "textColor": "#FF1A1A1A",
            "importantForAccessibility": "yes"},
        3: {"alpha": 0.4, "enabled": False, "text": "OK", "textColor": "#FFEEEEEE",
            "importantForAccessibility": "no"},
        9: {"alpha": 1.0, "textColor": "#FF1A1A1A"},
    }
    classes = {1: "TextView", 2: "TextView", 3: "TextView", 9: "ImageView"}
    before = copy.deepcopy(props)
    values, omitted = nz.nondefault_props(props, classes)
    assert props == before
    # textColor/importance majority (2 of 3 TextViews) hidden, the odd one out stays;
    # text is exempt from the majority rule; alpha/enabled are static defaults.
    assert values[1] == {"text": "OK"}
    assert values[3] == {"alpha": 0.4, "enabled": False, "text": "OK",
                         "textColor": "#FFEEEEEE", "importantForAccessibility": "no"}
    assert values[9] == {"textColor": "#FF1A1A1A"}  # a class with < 3 views: no majority
    assert omitted == {1: 4, 2: 4, 3: 0, 9: 1}
    # no majority below the threshold / without a strict majority
    v2, _ = nz.nondefault_props({1: {"x": 1}, 2: {"x": 2}, 3: {"x": 3}},
                                {1: "C", 2: "C", 3: "C"})
    assert v2 == {1: {"x": 1}, 2: {"x": 2}, 3: {"x": 3}}
    # source/stack-wrapped values compare on their value
    v3, o3 = nz.nondefault_props({5: {"alpha": {"value": 1.0, "source": "@style/x"}}}, {5: "View"})
    assert v3 == {5: {}} and o3 == {5: 1}


def test_nondefault_props_family_group_majority_for_rare_classes():
    tv = {"textSize": 14.0, "textColorHint": "#611D1B20", "importantForAutofill": "yes"}
    props = {1: {**tv, "text": "a"}, 2: {**tv, "text": "b"}, 3: {**tv, "text": "c"},
             7: {**tv, "text": "Notifications", "checked": True, "textSize": 42.0}}
    classes = {1: "MaterialTextView", 2: "MaterialTextView", 3: "MaterialTextView",
               7: "SwitchMaterial"}
    groups = {v: nz.family_group(nz.class_family(classes[v], props[v])) for v in props}
    assert groups == {1: "TextView", 2: "TextView", 3: "TextView", 7: "TextView"}
    assert nz.family_group("ScrollView") == "ViewGroup" and nz.family_group("View") == "View"
    alone, _ = nz.nondefault_props(props, classes)
    assert "textColorHint" in alone[7]  # 1 SwitchMaterial: no class majority
    values, omitted = nz.nondefault_props(props, classes, groups=groups)
    # the group's majority hides the theme colour and autofill flag, not the switch's own
    assert values[7] == {"text": "Notifications", "checked": True, "textSize": 42.0}
    assert omitted[7] == 2
    assert values[1] == alone[1]  # a class with its own majority is unaffected


def test_nondefault_on_the_real_view_screen_keeps_what_matters():
    data = lf.load("viewscreen", "views_props")
    classes, bounds = {}, {}

    def walk(n):
        classes[n["id"]] = n["qualified_name"]
        bounds[n["id"]] = nz.rect_list(n["bounds"])
        for c in n.get("children") or []:
            walk(c)

    walk(data["roots"][0])
    props = {int(v): nz.props_to_map(pl) for v, pl in data["properties"].items()}
    values, omitted = nz.nondefault_props(props, classes, bounds=bounds)
    total = sum(len(p) for p in props.values())
    kept = sum(len(v) for v in values.values())
    assert kept + sum(omitted.values()) == total
    assert kept / len(values) < 15  # ~115 properties per view -> a dozen or so
    # the bad-contrast text keeps its colour, the checked switch keeps checked,
    # the decorative image keeps importantForAccessibility=no
    assert values[13]["text"] == "Hard to read text (1.6:1)"
    assert values[16]["checked"] is True and values[16]["text"] == "Notifications"
    assert values[21]["importantForAccessibility"] == "no"
    assert "transformPivotX" not in values[13] and "alpha" not in values[13]


def test_import_is_cheap_and_protobuf_free():
    code = ("import sys, time; t=time.perf_counter(); "
            "import inspector_widget.output, inspector_widget.normalize, "
            "inspector_widget.normalize_defaults; dt=time.perf_counter()-t; "
            "bad=[m for m in sys.modules if m.startswith(('google.protobuf','PIL'))]; "
            "print(dt, bad); sys.exit(1 if bad else 0)")
    r = subprocess.run([sys.executable, "-c", code], cwd=HOST_DIR, capture_output=True,
                       text=True, env=dict(os.environ, PYTHONPATH=HOST_DIR), check=False)
    assert r.returncode == 0, r.stdout + r.stderr
    assert float(r.stdout.split()[0]) < 0.05, r.stdout


def test_action_attrs_from_the_hardened_agent():
    """SafeString (agent-hardening) sends an unlabelled AccessibilityAction as
    "<action>", a labelled one as its label and a lambda as "<lambda>": all three
    are actions or lambdas, never attr values (seen live on Thunderbird)."""
    assert nz.is_action_attr("<action>")
    assert nz.is_action_attr(" <action> ", "Whatever")
    assert nz.is_action_attr("Open message", "OnClick")  # labelled: by key
    assert not nz.is_action_attr("Open message", "ContentDescription")
    assert not nz.is_action_attr("Archive, Delete", "CustomActions")  # labels TalkBack offers
    attrs = {
        "OnClick": "<action>", "OnLongClick": "Select message",
        "SetTextSubstitution": "<action>", "ShowTextSubstitution": "<action>",
        "ClearTextSubstitution": "<action>", "GetTextLayoutResult": "<action>",
        "RequestFocus": "<action>", "TestTag": "onboarding_welcome_start_button",
        "Text": "Get started", "Role": "Button", "CustomActions": "Archive, Delete",
    }
    counts: dict = {}
    values, actions = nz.compose_attrs_brief(attrs, counts)
    assert values == {"TestTag": "onboarding_welcome_start_button", "Text": "Get started",
                      "Role": "Button", "CustomActions": "Archive, Delete"}
    assert actions == ["OnClick", "OnLongClick", "GetTextLayoutResult", "RequestFocus"]
    assert counts == {"actions": 3}
    # a slot parameter holding a lambda: kept as the λ marker for on*/content, else dropped
    assert nz.compose_value("onClick", "<lambda>") == nz.LAMBDA
    assert nz.compose_value("content", "<lambda>") == nz.LAMBDA
    assert nz.compose_value("transform", "<lambda>") is None
