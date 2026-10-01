"""Selectors that hit the stop TalkBack reads (gap G4), offline.

talkback/select.py resolves a walk's start or a scenario's target among the model's stops,
over the real-app hunt dumps (tests/data/realapps: Now in Android's feed, Thunderbird's
settings) and small synthetic trees: exact beats whole word beats substring ("Bookmark" never
picks "Unbookmark"), a tie is refused for an activation and listed, keys and Texts inside a
row climb to the row, and a label no stop speaks says so with the screen's stop count.
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
