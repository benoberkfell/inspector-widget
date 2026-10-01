"""Selectors that hit the stop TalkBack reads (gap G4), offline.

talkback/select.py resolves a walk's start or a scenario's target among the model's stops,
over the real-app hunt dumps (tests/data/realapps: Now in Android's feed, Thunderbird's
settings) and small synthetic trees: exact beats whole word beats substring ("Bookmark" picks
"Bookmark" over "Unbookmark", and an activation refuses a match inside a word), a tie is
refused for an activation and listed, keys and Texts inside a row climb to the row, a label
no stop speaks says so with the screen's stop count, and a list under an open dialog is
never scrolled to find one.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from inspector_widget.output import dumps, utf8_len
from inspector_widget.talkback import select as S

DATA = Path(__file__).parent / "data" / "realapps"


def dump(name):
    return json.loads(gzip.decompress((DATA / f"{name}.a11y.json.gz").read_bytes()))


def stops(name):
    return S.stops_from_dump(dump(name))


# --------------------------------------------------------------------------- synthetic trees
def _node(key, cls="android.view.View", text=None, cd=None, flags=(), kids=(), bounds=(0, 0, 100, 50),
          **extra):
    host, _, virt = key.partition(":")[2].partition(":")
    n = {"node_key": key, "class_name": cls, "host_view_id": int(host),
         "virtual_id": int(virt) if virt else -1,
         "flags": ["enabled", "visible_to_user", *flags],
         "bounds": {"layout": dict(zip("xywh", bounds, strict=True))},
         "children": list(kids), **extra}
    if text:
        n["text"] = text
    if cd:
        n["content_description"] = cd
    return n


def _button(key, label, y, **kw):
    return _node(key, "android.widget.Button", text=label, flags=("clickable", "focusable"),
                 bounds=(0, y, 200, 48), actions=[{"id": 16, "name": "CLICK"}], **kw)


def _screen(*kids):
    root = _node("view:1", "android.widget.FrameLayout", kids=kids, bounds=(0, 0, 400, 800))
    return {"windows": [{"root_view_id": 1, "window_type": 1, "root": root}]}


# --------------------------------------------------------------------------- ranking
def test_exact_beats_a_word_inside_another_word():
    st = S.stops_from_dump(_screen(_button("view:10", "Unbookmark", 0),
                                   _button("view:11", "Bookmark", 60)))
    m = S.resolve(st, "Bookmark")
    assert m.key == "view:11" and m.how == "exact" and m.field == "label" and not m.ambiguous
    # only the longer word on screen: a substring match, the lowest rank
    m = S.resolve(S.stops_from_dump(_screen(_button("view:10", "Unbookmark", 0))), "Bookmark")
    assert m.how == "substring"


def test_whole_words_beat_substrings_and_punctuation_does_not_matter():
    st = S.stops_from_dump(_screen(_button("view:10", "Themes gallery", 0),
                                   _node("view:20", "android.widget.LinearLayout",
                                         flags=("clickable", "focusable"),
                                         kids=[_node("view:21", "android.widget.TextView", text="Theme"),
                                               _node("view:22", "android.widget.TextView",
                                                     text="Use system default")],
                                         bounds=(0, 100, 400, 100))))
    for sel in ("Theme", "Theme. Use", "Theme, Use system default", "theme use system default"):
        m = S.resolve(st, sel)
        assert m.key == "view:20", sel  # the row, by its children's texts
    assert S.resolve(st, "Theme").how == "exact"


def test_nia_feed_bookmark_and_the_topic_chip_by_its_spoken_label():
    st = stops("nia_feed_two_column")
    m = S.resolve(st, "Bookmark")
    assert m.how == "exact" and m.field == "label" and m.key == "compose:8:95"
    assert [c.key for c in m.candidates] == ["compose:8:95", "compose:8:131", "compose:8:159",
                                             "compose:8:191"]
    # NIA-7: the description is on a child Text; the stop is the chip
    m = S.resolve(st, "Wear OS is not followed")
    assert (m.key, m.field, m.how) == ("compose:8:110", "label", "exact")
    assert m.node.speech == "Wear OS is not followed. Button"
    # scoped: the card's own Bookmark, and its n-th stop
    m = S.resolve(st, "Bookmark within Deep Links")
    assert m.key == "compose:8:159" and not m.ambiguous and m.via == "within compose:8:146"
    assert S.resolve(st, "2nd stop within Deep Links").key == "compose:8:159"
    assert S.resolve(st, "first stop within Deep Links").key == "compose:8:146"


def test_a_tie_is_refused_for_an_activation_and_listed_in_under_a_kilobyte():
    """t5c05ht: "Wear OS" activated a news card whose text holds the words, and left the
    app for Chrome. The chips show "WEAR OS" but TalkBack speaks their description ("Wear
    OS is followed"), so the card and every chip match alike: an activation refuses."""
    card = _node("compose:8:100", flags=("clickable", "focusable"), bounds=(0, 0, 400, 300),
                 kids=[_node("compose:8:101", "android.widget.TextView",
                             text="The new Pixel Watch: start building for Wear OS!")])
    chips = [_node(f"compose:8:{200 + i}", flags=("clickable", "focusable"), bounds=(0, 320 + 60 * i, 200, 48),
                   kids=[_node(f"compose:8:{300 + i}", "android.widget.TextView", text="WEAR OS",
                               cd=f"Wear OS is {'not ' if i else ''}followed")])
             for i in range(7)]
    st = S.stops_from_dump(_screen(card, *chips))
    with pytest.raises(S.SelectError) as err:
        S.require(st, "Wear OS", activate=True, where="MainActivity")
    e = err.value
    assert e.code == "ambiguous" and len(e.tried) == 5
    assert str(e) == "'Wear OS' matches 8 stops (word label) on MainActivity; 3 more not listed"
    assert e.tried[0].startswith('compose:8:100 "The new Pixel Watch')
    assert all(t.startswith("compose:8:2") for t in e.tried[1:])
    doc = {"error": {"code": e.code, "message": str(e), "hint": e.hint, "candidates": e.tried}}
    assert utf8_len(dumps(doc)) <= 1000
    # a walk start takes the first, and says so
    m = S.require(st, "Wear OS", activate=False)
    assert m.key == "compose:8:100" and "8 stops match" in m.notes[0]
    # the full description names one
    assert S.require(st, "Wear OS is followed", activate=True).key == "compose:8:200"


def test_a_few_words_inside_a_long_text_are_too_loose_to_activate():
    """t5c05ht, again with only the card on screen: "Wear OS" is 2 of the 12 words of the
    card's title. A walk may start there; an activation is refused, naming the card."""
    card = _node("compose:8:100", flags=("clickable", "focusable"), bounds=(0, 0, 400, 300),
                 kids=[_node("compose:8:101", "android.widget.TextView",
                             text="The new Google Pixel Watch is here: start building for Wear OS!")])
    st = S.stops_from_dump(_screen(card, _button("view:10", "Settings", 400)))
    m = S.resolve(st, "Wear OS")
    assert (m.key, m.how) == ("compose:8:100", "word") and m.cover < S.WEAK
    assert S.require(st, "Wear OS", activate=False).key == "compose:8:100"
    with pytest.raises(S.SelectError) as err:
        S.require(st, "Wear OS", activate=True)
    assert err.value.code == "ambiguous" and "too loose to activate" in str(err.value)
    assert err.value.tried == ['compose:8:100 "The new Google Pixel Watch is here: sta…"']
    m = S.require(S.stops_from_dump(_screen(_button("view:11", "Theme settings", 0))), "Theme",
                  activate=True)
    assert m.key == "view:11" and m.cover == 0.5


def test_keys_climb_to_the_stop_talkback_focuses():
    st = stops("nia_feed")
    m = S.resolve(st, "compose:8:526")  # the chip's Text
    assert (m.key, m.how, m.via) == ("compose:8:524", "inner", "compose:8:526")
    m = S.resolve(st, "compose:8:511")  # the Bookmark icon
    assert m.key == "compose:8:509"
    m = S.resolve(st, "compose:8:69")  # the feed list: its first stop
    assert (m.key, m.how) == ("compose:8:497", "within")
    assert S.resolve(st, "compose:8:524").how == "key"
    assert S.resolve(st, "compose:8:99999") is None


def test_test_tags_and_resource_ids_resolve_to_the_owning_stop():
    st = stops("nia_feed")
    m = S.resolve(st, "@topicTag:19")  # testTagsAsResourceId: on the chip's Text
    assert (m.key, m.how, m.via) == ("compose:8:524", "tag", "compose:8:526")
    assert S.resolve(st, "#newsResourceCard:85").key == "compose:8:497"
    assert S.resolve(st, "@nobody") is None


def test_thunderbird_settings_rows_by_their_child_texts():
    """TB-11 shape: preference rows have no label of their own (label "": their texts are
    children); "General settings" names the row, and a label absent from the screen fails
    with the stop count and nothing that could scroll it in."""
    st = stops("thunderbird_settings")
    m = S.resolve(st, "General settings")
    assert m.key == "view:398" and m.how == "exact" and m.node.label == ""
    assert S.resolve(st, "Theme") is None
    assert S.can_bring_more(dump("thunderbird_settings")) is None
    with pytest.raises(S.SelectError) as err:
        S.require(st, "Theme", activate=True, where="SettingsActivity")
    assert err.value.code == "start_not_found"
    assert str(err.value) == "label 'Theme' not found among 13 stops on SettingsActivity"


def test_nia_onboarding_double_stops_are_a_tie_but_done_is_one():
    st = stops("nia_for_you")
    assert S.resolve(st, "Done").key == "compose:8:90"
    m = S.resolve(st, "Headlines")  # NIA-2: the row and its own toggle
    assert m.ambiguous and [c.key for c in m.candidates] == ["compose:8:94", "compose:8:100"]
    assert S.can_bring_more(dump("nia_for_you"))  # the feed scrolls


def test_a_blank_window_left_on_top_does_not_hide_the_stops_under_it():
    """Now in Android after the 16 KB dialog: a dialog window of the app with nothing
    visible sits on top, and the model marked the activity covered by it (no stops at all,
    so every label failed). TalkBack reads the activity: so do the selectors."""
    d = _screen(_button("view:10", "Bookmark", 0))
    d["windows"][0]["covered_by"] = 251
    blank = _node("view:251", "android.widget.FrameLayout", bounds=(0, 234, 400, 500))
    blank["flags"] = ["enabled"]
    d["windows"].append({"root_view_id": 251, "window_type": 2, "modal": True, "root": blank})
    assert S.resolve(S.stops_from_dump(d), "Bookmark").key == "view:10"
    # a covering window that shows something still covers
    shown = _node("view:300", "android.widget.FrameLayout", bounds=(0, 234, 400, 500),
                  kids=[_button("view:301", "OK", 300)])
    d["windows"][0]["covered_by"] = 300
    d["windows"][1] = {"root_view_id": 300, "window_type": 2, "modal": True, "root": shown}
    assert [s.key for s in S.stops_from_dump(d)] == ["view:301"]


def test_covered_targets_and_links():
    link = _button("view:30", "Read more", 200, role_description="link")
    st = S.stops_from_dump(_screen(_button("view:10", "Save", 0), link))
    with pytest.raises(S.SelectError) as err:
        S.require(st, "Save", activate=True, covered=lambda s: "DrawerLayout view:5")
    assert "lies under DrawerLayout view:5" in str(err.value)
    m = S.require(st, "Save", activate=False, covered=lambda s: "DrawerLayout view:5")
    assert m.notes == ["view:10 lies under DrawerLayout view:5"]
    m = S.require(st, "Read more", activate=True)
    assert m.notes == ["activating view:30 may leave the app: it is a web link"]
    url = S.stops_from_dump(_screen(_button("view:40", "https://example.com/x", 0)))
    assert url[0].link == "a URL"


def test_custom_actions_are_the_labelled_app_actions():
    n = {"actions": [{"id": 16, "name": "CLICK", "label": "open"},
                     {"id": 0x0102003D, "name": "SHOW_ON_SCREEN", "label": "show"},
                     {"id": 0x7F0A0012, "name": "CUSTOM", "label": "Delete"},
                     {"id": 0x7F0A0013, "name": "CUSTOM_0x7f0a0013"}]}
    assert S.custom_actions(n) == {"Delete": 0x7F0A0012}


def test_norm_and_keys():
    assert S.norm("  Theme.  Use, system—default! ") == "theme use system default"
    assert S.is_key("view:12") and S.is_key("compose:8:-3") and S.is_key("virtual:4:5")
    assert not S.is_key("n12") and not S.is_key("Bookmark")


# --------------------------------------------------------------------------- review fixes
def test_an_activation_refuses_a_match_inside_a_word():
    """With no "Bookmark" on screen, "Bookmark" only matches inside "Unbookmark": the
    opposite control (NiA's Saved screen, where every card says Unbookmark). An activation
    refuses it, listing it; a walk may start there, with a note that says so."""
    for label, sel in (("Unbookmark", "Bookmark"), ("Unfollow", "follow"),
                       ("Disable notifications", "able")):
        st = S.stops_from_dump(_screen(_button("view:10", label, 0), _button("view:11", "Settings", 60)))
        with pytest.raises(S.SelectError) as err:
            S.require(st, sel, activate=True, where="MainActivity")
        assert err.value.code == "ambiguous", sel
        assert "only part of a word" in str(err.value) and "on MainActivity" in str(err.value)
        assert err.value.tried == [f'view:10 "{label}. Button"']
        m = S.require(st, sel, activate=False)
        assert m.key == "view:10" and m.how == "substring"
        assert m.notes == [f"{sel!r} is only part of a word of view:10 \"{label}. Button\": no "
                           f"stop says it as a word"]
    # the whole word still activates, and an exact match still wins over the longer word
    st = S.stops_from_dump(_screen(_button("view:10", "Unbookmark", 0), _button("view:11", "Bookmark", 60)))
    assert S.require(st, "Bookmark", activate=True).key == "view:11"
    st = S.stops_from_dump(_screen(_button("view:10", "Unbookmark", 0)))
    assert S.require(st, "Unbookmark", activate=True).key == "view:10"


def _dialog_over_a_list():
    """An activity whose RecyclerView scrolls, under a modal dialog whose own ListView
    scrolls too (the dialog covers the activity: a11y.apply_window_meta's covered_by)."""
    rows = [_button(f"view:{21 + i}", f"Episode {i}", 100 * i) for i in range(3)]
    lst = _node("view:20", "androidx.recyclerview.widget.RecyclerView", kids=rows,
                bounds=(0, 0, 1080, 2000), actions=[{"id": 4096, "name": "SCROLL_FORWARD"}])
    act = _node("view:1", "android.widget.FrameLayout", kids=[lst], bounds=(0, 0, 1080, 2000))
    opts = [_button(f"view:{61 + i}", f"Option {i}", 400 + 60 * i) for i in range(2)]
    dlist = _node("view:60", "android.widget.ListView", kids=opts, bounds=(100, 400, 880, 600),
                  actions=[{"id": 4096, "name": "SCROLL_FORWARD"}])
    dlg = _node("view:50", "android.widget.FrameLayout", kids=[dlist], bounds=(100, 400, 880, 600))
    return {"windows": [{"root_view_id": 1, "window_type": 1, "root": act, "covered_by": 50},
                        {"root_view_id": 50, "window_type": 2, "modal": True, "root": dlg}]}


def test_a_list_under_an_open_dialog_is_never_scrolled_for_a_label():
    """The seek scrolls to bring a label in: never the list behind a modal dialog (that
    changes the app's state behind it for nothing), the dialog's own list instead."""
    d = _dialog_over_a_list()
    assert [s.key for s in S.stops_from_dump(d)] == ["view:61", "view:62"]
    assert S.can_bring_more(d) == "view:60 [880x600] scrolls"
    assert [k for k, _ids in S.scrollables(d)] == ["view:60"]
    # with only the covered list scrolling, nothing can bring a label in
    d["windows"][1]["root"]["children"][0]["actions"] = []
    assert S.can_bring_more(d) is None and S.scrollables(d) == []
    # a blank window left on top covers nothing: its list is scrolled again
    d = _dialog_over_a_list()
    d["windows"][1]["root"] = _node("view:50", "android.widget.FrameLayout", bounds=(0, 0, 1, 1))
    d["windows"][1]["root"]["flags"] = ["enabled"]
    assert [k for k, _ids in S.scrollables(d)] == ["view:20"]
    assert S.covering_window(_dialog_over_a_list(), "view:22") == 50
    assert S.covering_window(_dialog_over_a_list(), "view:61") is None


def test_the_focused_window_s_list_is_scrolled_first():
    d = _dialog_over_a_list()
    d["windows"][0].pop("covered_by")  # a non-modal popup over the list
    d["windows"][1]["modal"] = False
    assert [k for k, _ids in S.scrollables(d)] == ["view:20", "view:60"]
    d["windows"][1]["root"]["children"][0]["children"][0]["flags"].append("accessibility_focused")
    assert [k for k, _ids in S.scrollables(d)] == ["view:60", "view:20"]


def test_pre_pane_titles_are_titles_never_a_navigation_bar_s_labels():
    """pre:pane=Saved on Now in Android's For you screen: the nav bar's "Saved" text is on
    every destination, so it is no title; the top bar's "Now in Android" is."""
    assert S.titles(dump("nia_for_you")) == ["Now in Android"]
    assert "Saved" not in S.titles(dump("nia_feed_two_column"))  # the nav rail's label
    assert "Interests" in S.titles(dump("nia_interests"))
    assert S.titles(dump("thunderbird_selection_mode"))[-1] == "1 selected"  # the action mode
    assert S.titles(dump("thunderbird_settings")) == ["Settings"]  # not the rows' texts
    assert S.titles(dump("antennapod_episodes")) == ["Episodes"]  # in a long-clickable toolbar
    assert S.titles(dump("thunderbird_theme_dialog")) == ["Theme"]  # the dialog, not under it


def test_the_selectors_number_stops_with_the_talkback_model():
    """Every hunt dump resolves through talkback.order (the model's stops); a broken call
    into it (a signature change: TypeError) propagates instead of quietly switching every
    selector to the a11y model's numbering. Only a dump the model cannot read falls back."""
    from inspector_widget.talkback import order
    for p in sorted(DATA.glob("*.a11y.json.gz")):
        d = json.loads(gzip.decompress(p.read_bytes()))
        assert len(S.stops_from_dump(d)) == len(order.reading_order(d, keyboard=True)["focus_order"]), p
    d = _screen(_button("view:10", "Save", 0))
    S.stops_from_dump(d)
    real = order.reading_order
    try:
        order.reading_order = lambda *a, **k: (_ for _ in ()).throw(TypeError("keyboard"))
        with pytest.raises(TypeError):
            S.stops_from_dump(d)
        order.reading_order = lambda *a, **k: (_ for _ in ()).throw(KeyError("windows"))
        d["windows"][0]["root"]["children"][0]["order"] = 1
        assert [s.key for s in S.stops_from_dump(d)] == ["view:10"]
    finally:
        order.reading_order = real
