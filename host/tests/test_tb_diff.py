"""talkback/diff.py: classify a walk record (pure; no device, no fakes).

The records mirror what talkback/walk.py saves. Two are shaped on the live
spike (emulator, TalkBack 17.0.0): the Compose main list that wraps onto a
27px "Unlabelled" sliver of a scrolled-off row, and the View screen whose
full-screen ScrollView is a stop that speaks the whole screen while the
"1. ImageButton contentDescription" header under the status bar is never
reached.
"""

from __future__ import annotations

import json

from inspector_widget.talkback import diff
from inspector_widget.talkback import walk as tbwalk


def step(i, key, bounds, label="", speak=None, via="next", utt="model", **extra):
    s = {"i": i, "key": key, "ref": key, "via": via, "moved": True, "label": label,
         "cls": extra.pop("cls", "View"), "bounds": list(bounds), "window": 1,
         "speak": label if speak is None else speak, "utt": utt,
         "window_rect": [0, 0, 1280, 2856]}
    s.update(extra)
    return s


def edge(i, key):
    return {"i": i, "key": key, "ref": key, "via": "edge", "moved": False, "edge": True}


def pstop(key, bounds, label=""):
    return {"key": key, "ref": key, "label": label, "speak": label, "bounds": list(bounds),
            "window": 1, "cls": "View"}


def record(steps, predicted=(), **kw):
    rec = {"steps": steps, "predicted": list(predicted), "ended": "wrap", "direction": "next",
           "cycle": [], "edge": None, "density": 420, "model": "test"}
    rec.update(kw)
    return rec


def codes(res):
    return [f["code"] for f in res["findings"]]


# --------------------------------------------------------------------------- #
# Geometry helpers
# --------------------------------------------------------------------------- #
def test_visual_order_rows_columns_and_a_single_row():
    def v(items):
        return diff.visual_order(items)[0]

    grid = [("a", (0, 0, 100, 50)), ("b", (120, 0, 100, 50)), ("c", (0, 60, 100, 50)),
            ("d", (120, 60, 100, 50))]
    assert v(grid) == ["a", "b", "c", "d"]
    touching = [("a", (0, 0, 100, 50)), ("b", (150, 0, 100, 50)), ("c", (0, 50, 100, 50)),
                ("d", (150, 50, 100, 50))]
    assert v(touching) == ["a", "b", "c", "d"]  # rows that abut (the View layout case)
    # Two columns whose cards have different heights (rows overlap): read per column.
    cols = [("a1", (0, 0, 100, 120)), ("b1", (120, 0, 100, 100)), ("a2", (0, 130, 100, 160)),
            ("b2", (120, 110, 100, 140)), ("a3", (0, 300, 100, 100)), ("b3", (120, 260, 100, 180))]
    assert v(cols) == ["a1", "a2", "a3", "b1", "b2", "b3"]
    row = [("r", (200, 0, 50, 50)), ("l", (0, 5, 50, 40))]
    assert v(row) == ["l", "r"]


def test_lis():
    seq = [0, 2, 1, 3, 5, 4]
    idx = diff._lis(seq)
    assert len(idx) == 4 and all(seq[a] < seq[b] for a, b in zip(idx, idx[1:]))
    assert diff._lis([]) == []


# --------------------------------------------------------------------------- #
# Spike-shaped walks
# --------------------------------------------------------------------------- #
def _main_rows(n, y0=348, h=216):
    return [(f"compose:9:{10 + i}", (0, y0 + i * (h + 3), 1280, h), f"Row {i}. Subtitle {i}")
            for i in range(n)]


def test_spike_main_list_wraps_onto_an_unlabelled_sliver():
    rows = _main_rows(4)
    steps = [step(0, "compose:9:1", (48, 210, 322, 84), "A11yProbe", via="start")]
    steps += [step(i + 1, k, b, lab) for i, (k, b, lab) in enumerate(rows)]
    steps.append(edge(5, rows[-1][0]))
    steps.append(step(6, "compose:9:1", (48, 210, 322, 84), "A11yProbe", via="wrap"))
    steps.append(step(7, "compose:9:30", (0, 348, 1280, 27), "", speak="Unlabelled. In list",
                      utt="logcat"))
    predicted = [pstop("compose:9:1", (48, 210, 322, 84), "A11yProbe")] + \
        [pstop(k, b, lab) for k, b, lab in rows]
    res = diff.analyze(record(steps, predicted, ended="loop"))
    ghosts = [f for f in res["findings"] if f["code"] == "tb.ghost_stop"]
    assert len(ghosts) == 1 and ghosts[0]["refs"] == ["compose:9:30"]
    assert "unlabelled + sliver" in ghosts[0]["msg"] and "1280x27px" in ghosts[0]["msg"]
    assert res["vs_model"]["agree"] == 4 and res["vs_model"]["differ"] == 0
    assert "tb.skipped" not in codes(res) and "tb.out_of_order" not in codes(res)


def test_spike_view_screen_scrollview_stop_and_unreached_header():
    whole = ("1. ImageButton contentDescription, Like this photo, Like this photo, "
             "2. Touch target size, Play, Play")
    steps = [
        step(0, "view:12", (0, 0, 1280, 2856), "", speak=whole, via="start", utt="logcat",
             cls="ScrollView"),
        step(1, "view:16", (48, 149, 144, 144), "Like this photo", speak="Like this photo. Button",
             utt="logcat", cls="ImageButton"),
        step(2, "view:17", (240, 149, 144, 144), "Like this photo", speak="Like this photo. Button",
             utt="logcat", cls="ImageButton"),
        step(3, "view:18", (48, 293, 427, 125), "2. Touch target size", utt="logcat"),
        step(4, "view:20", (48, 418, 227, 144), "Play", speak="Play. Button", utt="logcat"),
        edge(5, "view:20"),
        step(6, "view:12", (0, 0, 1280, 2856), "", speak=whole, via="wrap", utt="logcat"),
    ]
    predicted = [pstop("view:14", (48, 48, 700, 101), "1. ImageButton contentDescription"),
                 pstop("view:16", (48, 149, 144, 144), "Like this photo"),
                 pstop("view:17", (240, 149, 144, 144), "Like this photo"),
                 pstop("view:18", (48, 293, 427, 125), "2. Touch target size"),
                 pstop("view:20", (48, 418, 227, 144), "Play")]
    res = diff.analyze(record(steps, predicted))
    c = codes(res)
    assert "tb.skipped" in c and "tb.double_stop" in c and "model.mismatch" in c
    skipped = next(f for f in res["findings"] if f["code"] == "tb.skipped")
    assert skipped["refs"] == ["view:14"] and "in a full lap" in skipped["msg"]
    dbl = next(f for f in res["findings"] if f["code"] == "tb.double_stop")
    assert dbl["refs"] == ["view:12", "view:16"]
    assert res["vs_model"]["unpredicted"] == ["view:12"]
    assert res["vs_model"]["unvisited"] == ["view:14"]
    assert "tb.ghost_stop" not in c  # the ScrollView speaks (logcat), so it is no ghost
    assert "tb.out_of_order" not in c  # the container is left out of the visual order


# --------------------------------------------------------------------------- #
# Each check
# --------------------------------------------------------------------------- #
def _column(n, x=0):
    return [(f"view:{100 + x + i}", (x, 100 + i * 100, 300, 80), f"Item {i}") for i in range(n)]


def test_out_of_order_is_checked_per_screen_state():
    items = _column(5)
    order = [0, 2, 1, 3, 4]
    steps = [step(j, items[i][0], items[i][1], items[i][2], via="start" if j == 0 else "next")
             for j, i in enumerate(order)]
    res = diff.analyze(record(steps, [pstop(*it) for it in items], ended="max_steps"))
    ooo = [f for f in res["findings"] if f["code"] == "tb.out_of_order"]
    assert len(ooo) == 1 and len(ooo[0]["refs"]) == 1
    # After an auto-scroll the coordinates restart: no inversion across the boundary.
    scrolled = [step(0, "view:1", (0, 500, 300, 80), "A", via="start"),
                step(1, "view:2", (0, 600, 300, 80), "B"),
                step(2, "view:3", (0, 700, 300, 80), "C"),
                step(3, "view:4", (0, 100, 300, 80), "D", via="autoscroll"),
                step(4, "view:5", (0, 200, 300, 80), "E"),
                step(5, "view:6", (0, 300, 300, 80), "F")]
    assert "tb.out_of_order" not in codes(diff.analyze(record(scrolled, ended="max_steps")))


def test_prev_walk_agrees_with_the_model_in_reverse():
    items = _column(4)
    steps = [step(j, items[i][0], items[i][1], items[i][2], via="start" if j == 0 else "next")
             for j, i in enumerate([3, 2, 1, 0])]
    res = diff.analyze(record(steps, [pstop(*it) for it in items], direction="prev",
                              ended="max_steps"))
    assert res["vs_model"]["agree"] == 3 and res["vs_model"]["differ"] == 0
    assert res["findings"] == []


def test_skipped_only_between_reached_stops_without_a_full_lap():
    items = _column(6)
    steps = [step(0, items[0][0], items[0][1], items[0][2], via="start"),
             step(1, items[2][0], items[2][1], items[2][2])]
    res = diff.analyze(record(steps, [pstop(*it) for it in items], ended="max_steps"))
    skipped = next(f for f in res["findings"] if f["code"] == "tb.skipped")
    assert skipped["refs"] == [items[1][0]] and "between the stops" in skipped["msg"]


def test_loop_and_stuck_and_edge_that_can_scroll():
    a, b, c = _column(3)
    steps = [step(0, a[0], a[1], a[2], via="start"), step(1, b[0], b[1], b[2]),
             step(2, c[0], c[1], c[2]), step(3, b[0], b[1], b[2]), step(4, c[0], c[1], c[2])]
    res = diff.analyze(record(steps, ended="loop", cycle=[b[0], c[0]]))
    loop = next(f for f in res["findings"] if f["code"] == "tb.loop")
    assert loop["sev"] == "error" and loop["refs"] == [b[0], c[0]]
    res = diff.analyze(record(steps[:3] + [edge(3, c[0]), edge(4, c[0])], ended="stuck"))
    assert next(f for f in res["findings"] if f["code"] == "tb.edge_stuck")["sev"] == "error"
    res = diff.analyze(record(steps[:3] + [edge(3, c[0])], ended="edge",
                              edge={"container": "view:9", "container_ref": "view:9",
                                    "container_cls": "RecyclerView", "can_scroll": ["forward"]}))
    f = next(f for f in res["findings"] if f["code"] == "tb.edge_stuck")
    assert "RecyclerView" in f["msg"] and "forward" in f["msg"]


def test_ghost_reasons():
    ok = step(1, "view:1", (0, 0, 300, 100), "Fine")
    assert diff._ghost_reasons(ok, 420) == []
    unl = step(1, "view:1", (0, 0, 300, 100), "", speak="Button", cls="Button")
    assert diff._ghost_reasons(unl, 420) == ["unlabelled"]
    tiny = step(1, "view:1", (0, 0, 5, 100), "x")
    assert diff._ghost_reasons(tiny, 420) == ["tiny"]
    off = step(1, "view:1", (0, 3000, 300, 100), "x")
    assert diff._ghost_reasons(off, 420) == ["offscreen"]
    said = step(1, "view:1", (0, 0, 300, 100), "x", speak="Unlabeled, Button", utt="logcat")
    assert diff._ghost_reasons(said, 420) == ["unlabelled"]


def test_escape_from_an_overlay_and_from_a_modal_window():
    overlay = {"overlay": "view:70", "ref": "view:70", "cls": "FrameLayout", "area": 0.6,
               "rect": [0, 80, 360, 400]}
    steps = [step(0, "view:71", (20, 100, 320, 60), "OK", via="start"),
             step(1, "view:72", (20, 180, 320, 60), "Cancel"),
             step(2, "view:61", (20, 250, 320, 60), "Behind", covered_by=overlay)]
    res = diff.analyze(record(steps, ended="max_steps"))
    esc = next(f for f in res["findings"] if f["code"] == "tb.escape")
    assert esc["refs"] == ["view:72", "view:61"]
    assert "tb.ghost_stop" not in codes(res)  # an escape is not also reported as occluded
    # Covered but never inside the overlay: an occluded ghost stop instead.
    res = diff.analyze(record([step(0, "view:61", (20, 250, 320, 60), "Behind", via="start",
                                    covered_by=overlay)], ended="max_steps"))
    assert codes(res) == ["tb.ghost_stop"]
    modal = [step(0, "view:5", (0, 0, 100, 100), "Behind a dialog", via="start",
                  window_covered_by=77)]
    assert codes(diff.analyze(record(modal, ended="max_steps"))) == ["tb.escape"]


def test_expect_missing_and_out_of_order():
    items = _column(3)
    steps = [step(j, k, b, lab, via="start" if j == 0 else "next") for j, (k, b, lab) in enumerate(items)]
    res = diff.analyze(record(steps, ended="max_steps"), expect=["Item 1", "Item 0", "Nope"])
    assert res["expect"]["ok"] is False and res["expect"]["missing"] == ["Nope"]
    basis = {(f["code"], f["basis"]) for f in res["findings"]}
    assert ("tb.skipped", "expect") in basis and ("tb.out_of_order", "expect") in basis
    ok = diff.analyze(record(steps, ended="max_steps"), expect=["Item 0", "view:102"])
    assert ok["expect"] == {"ok": True, "matched": 2, "of": 2}


# --------------------------------------------------------------------------- #
# Compact rendering
# --------------------------------------------------------------------------- #
def test_compact_stays_within_the_byte_budget_with_long_speech():
    long = "A very long announcement " * 20
    steps = [step(0, "view:1", (0, 0, 10, 10), long, via="start")]
    steps += [step(i, f"view:{i}", (0, i * 100, 300, 80), long) for i in range(1, 61)]
    rec = record(steps, ended="max_steps")
    rec.update(diff.analyze(rec))
    rec.update(walk="wtest", serial="s", package="p", talkback="17 uinput/enhanced", start="current",
               until="steps", ms={}, utterance="model", restore="restored", legacy_ids=False)
    out = tbwalk.compact(rec, max_lines=60, max_bytes=5000)
    assert len(json.dumps(out, ensure_ascii=False).encode()) <= 5000
    assert out["steps"] == 60 and len(out["lines"]) <= 60


def test_a_reminted_lazy_id_still_counts_as_the_predicted_stop():
    items = _column(3)
    steps = [step(0, items[0][0], items[0][1], items[0][2], via="start"),
             step(1, "compose:7:759", items[1][1], items[1][2], pkey=items[1][0]),
             step(2, items[2][0], items[2][1], items[2][2]), edge(3, items[2][0])]
    res = diff.analyze(record(steps, [pstop(*it) for it in items], ended="edge"))
    assert res["findings"] == [] and res["vs_model"]["agree"] == 2


def test_same_node_by_signature_where_boxes_overlap():
    from inspector_widget.talkback import walk as w
    assert w.same_node("compose:7:141", "View|Row 3", (0, 100, 300, 80),
                       "compose:7:759", "View|Row 3", (0, 102, 300, 80))
    assert not w.same_node("compose:7:141", "View|Row 3", (0, 100, 300, 80),
                           "compose:7:759", "View|Row 3", (0, 400, 300, 80))
    assert not w.same_node("compose:7:1", "View|", (0, 0, 10, 10), "compose:7:2", "View|", (0, 0, 10, 10))


def test_role_only_logcat_speech_is_unlabelled():
    s = step(1, "view:6", (195, 236, 117, 117), "", speak="Button", utt="logcat")
    assert diff._ghost_reasons(s, 390) == ["unlabelled"]
    s = step(1, "view:26", (39, 1000, 1998, 117), "", speak="Edit box", utt="logcat")
    assert diff._ghost_reasons(s, 390) == ["unlabelled"]
    s = step(1, "view:5", (39, 236, 117, 117), "Like this photo", speak="Like this photo. Button",
             utt="logcat")
    assert diff._ghost_reasons(s, 390) == []


def test_double_stop_from_speech_not_from_the_containers_child_texts():
    row = step(0, "compose:14:3", (0, 300, 1280, 190), "Item 0 (compose) | Done",
               speak="Item 0 (compose)", via="start", flags=["focusable"])
    box = step(1, "compose:14:5", (900, 330, 120, 120), "Done", speak="checked. Done. Check box",
               cls="CheckBox", flags=["clickable", "focusable"])
    assert "tb.double_stop" not in codes(diff.analyze(record([row, box], ended="max_steps")))
    row["flags"] = ["clickable", "focusable"]  # a clickable card around its own Checkbox (C1)
    res = diff.analyze(record([row, box], ended="max_steps"))
    f = next(f for f in res["findings"] if f["code"] == "tb.double_stop")
    assert "both clickable stops" in f["msg"] and f["refs"] == ["compose:14:3", "compose:14:5"]


def test_a_walk_from_the_middle_to_the_edge_covers_only_that_part():
    items = _column(6)
    steps = [step(0, items[3][0], items[3][1], items[3][2], via="start"),
             step(1, items[4][0], items[4][1], items[4][2]),
             step(2, items[5][0], items[5][1], items[5][2]), edge(3, items[5][0])]
    res = diff.analyze(record(steps, [pstop(*it) for it in items], ended="edge"))
    assert "tb.skipped" not in codes(res) and "unvisited" not in res["vs_model"]
    # prev from the middle to the top edge: the stops after the start are not covered
    back = [step(0, items[2][0], items[2][1], items[2][2], via="start"),
            step(1, items[1][0], items[1][1], items[1][2]), edge(2, items[1][0])]
    res = diff.analyze(record(back, [pstop(*it) for it in items], ended="edge", direction="prev"))
    skipped = next(f for f in res["findings"] if f["code"] == "tb.skipped")
    assert skipped["refs"] == [items[0][0]] and "back to the edge" in skipped["msg"]


def test_a_full_screen_scrim_over_a_button_is_not_a_double_stop():
    button = step(0, "compose:13:50", (100, 1500, 400, 120), "Apply", speak="Apply. Button",
                  via="start", flags=["clickable"], ancestors=["compose:13:1"])
    scrim = step(1, "compose:13:32", (0, 0, 1280, 2856), "", speak="Close sheet",
                 flags=["clickable"], ancestors=["compose:13:1"])
    assert "tb.double_stop" not in codes(diff.analyze(record([button, scrim], ended="max_steps")))
    scrim["ancestors"] = []
    button["ancestors"] = ["compose:13:32"]  # the Apply button inside the clickable scrim
    assert "tb.double_stop" in codes(diff.analyze(record([button, scrim], ended="max_steps")))


def test_a_cut_off_last_row_with_nothing_to_scroll_is_edge_stuck():
    parent = [0, 200, 1280, 1000]
    rows = [step(i, f"view:{10 + i}", (0, 200 + i * 180, 1280, 180), f"Log entry {i + 1}",
                 via="start" if i == 0 else "next", parent_rect=parent) for i in range(5)]
    rows.append(step(5, "view:15", (0, 1100, 1280, 100), "Log entry 6", parent_rect=parent))
    res = diff.analyze(record(rows + [edge(6, "view:15")], ended="edge"))
    f = next(f for f in res["findings"] if f["code"] == "tb.edge_stuck")
    assert f["refs"] == ["view:15"] and "cut off" in f["msg"]
    rows[-1]["container"] = "view:2"  # inside a scroller: TalkBack scrolls it, not this check's case
    assert "tb.edge_stuck" not in codes(diff.analyze(record(rows + [edge(6, "view:15")], ended="edge")))


def test_a_merged_row_read_out_of_screen_order_is_a_wrong_announcement():
    parts = [{"text": "$5", "rect": [1951, 303, 47, 59]}, {"text": "Socks", "rect": [78, 303, 111, 59]}]
    row = step(1, "compose:7:5", (39, 264, 1998, 137), "$5 | Socks", speak="$5. Socks", utt="logcat",
               parts=parts)
    head = step(0, "compose:7:4", (39, 175, 301, 69), "Cart", via="start")
    f = next(f for f in diff.analyze(record([head, row]))["findings"] if f["code"] == "tb.wrong_announcement")
    assert f["refs"] == ["compose:7:5"] and "'$5' before 'Socks'" in f["msg"]
    assert "clearAndSetSemantics" in f["fix"]
    row["speak"] = "Socks. $5"  # the same row read in screen order
    assert "tb.wrong_announcement" not in codes(diff.analyze(record([head, row])))


def test_a_model_stop_talkback_scrolls_into_view_first_is_not_judged_a_ghost():
    head = step(0, "compose:11:4", (39, 175, 900, 69), "Products", via="start")
    clipped = step(1, "compose:11:43", (0, 2066, 2076, 86), "", speak="Unlabelled", flags=["clickable"])
    assert "tb.ghost_stop" in codes(diff.analyze(record([head, clipped], ended="autoscroll")))
    clipped["show_on_screen"] = True  # static_walk: TalkBack shows it (and its clipped text) first
    assert "tb.ghost_stop" not in codes(diff.analyze(record([head, clipped], ended="autoscroll")))


def test_alike_nodes_of_different_list_items_are_not_the_same_node():
    from inspector_widget.talkback import walk as w
    # NiA: the HEADLINES chips of two news cards, the second scrolled into the first's slot
    a = ("compose:8:552", "View|HEADLINES", (120, 2352, 231, 144))
    b = ("compose:8:646", "View|HEADLINES", (120, 2352, 231, 144))
    assert w.same_node(*a, *b)  # no item context known: alike is the same (a re-minted id)
    assert not w.same_node(*a, *b, ctx_a="Introducing Compose Camp", ctx_b="Android 16 beta")
    assert w.same_node(*a, *b, ctx_a="Introducing Compose Camp", ctx_b="Introducing Compose Camp")


def test_a_recycled_row_view_in_its_old_slot_is_another_stop_in_the_model():
    # V6 BAD_B: RecyclerView rebinds the View that showed "Mail 3" to "Mail 31" and lays it
    # out in the very slot it had; it is another stop, not a label changed in place
    m = tbwalk.Model()
    m.stops = [tbwalk.PStop("view:14", "Mail 3", "Mail 3", (0, 530, 2076, 137), 1, "TextView",
                            "", True)]
    assert m.match("view:14", "TextView|Mail 31", (0, 530, 2076, 137), ctx="",
                   item_root=True) is None
    assert m.match("view:14", "TextView|Mail 3", (0, 530, 2076, 137), ctx="",
                   item_root=True) is m.stops[0]
    # outside a list a label that changes in place is the same node ("Play" -> "Pause")
    m.stops = [tbwalk.PStop("view:5", "Play", "Play", (0, 0, 100, 100), 1, "Button")]
    assert m.match("view:5", "Button|Pause", (0, 0, 100, 100)) is m.stops[0]
