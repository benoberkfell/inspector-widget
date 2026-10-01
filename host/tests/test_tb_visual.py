"""The heuristic reading-intent order (inspector_widget.talkback.visual): an XY-cut per window,
containers kept together, overridable by an explicit expectation."""

from __future__ import annotations

from inspector_widget import talkback as tb

from test_tb_rules import FOCUS, SRF, c, compose_host, n, root


def card(host, sid, text, x, y, h):
    return c(host, sid, cls="android.widget.TextView", text=text, flags=SRF, b=(x, y, 500, h))


def two_columns(zigzag_links: bool = True):
    """C2: two Columns whose cards have different heights, so their rows overlap."""
    a = [card(7, 1, "A1", 0, 0, 120), card(7, 2, "A2", 0, 130, 160), card(7, 3, "A3", 0, 300, 100)]
    b = [card(7, 4, "B1", 540, 0, 100), card(7, 5, "B2", 540, 110, 140),
         card(7, 6, "B3", 540, 260, 180)]
    if zigzag_links:  # Compose's geometric sort without a traversal group per column
        zig = [a[0], b[0], a[1], b[1], a[2], b[2]]
        for prev, nxt in zip(zig, zig[1:]):
            prev["traversal_before"] = nxt["id"]
    col_a = c(7, 10, b=(0, 0, 500, 440), children=a)
    col_b = c(7, 11, b=(540, 0, 500, 440), children=b)
    return tb.build([root(compose_host(7, col_a, col_b, b=(0, 0, 1080, 440)))])


def labels(tree, keys):
    return [tree.node(k).text for k in keys]


def test_two_columns_read_as_columns_while_talkback_zigzags():
    tree = two_columns()
    visual = tb.visual_order(tree)
    assert visual["conf"] == "heuristic"
    assert labels(tree, visual["order"]) == ["A1", "A2", "A3", "B1", "B2", "B3"]
    walk = tb.simulate(tree)
    assert labels(tree, walk.keys()[:6]) == ["A1", "B1", "A2", "B2", "A3", "B3"]


def test_a_row_reads_left_to_right_and_bands_top_to_bottom():
    header = n(2, cls="android.widget.TextView", text="Header", b=(0, 0, 1080, 100))
    row = [n(3 + i, cls="android.widget.Button", text=t, flags=FOCUS, b=(x, 200, 300, 100))
           for i, (t, x) in enumerate([("Right", 700), ("Left", 0), ("Middle", 350)])]
    footer = n(9, cls="android.widget.TextView", text="Footer", b=(0, 900, 1080, 100))
    tree = tb.build([root(footer, *row, header)])
    assert labels(tree, tb.visual_order(tree)["order"]) == [
        "Header", "Left", "Middle", "Right", "Footer"]


def test_one_stop_beside_a_column_is_a_row_not_two_columns():
    # A column split needs at least two stops on each side.
    tree = tb.build([root(
        n(2, cls="android.widget.TextView", text="Avatar", b=(0, 0, 100, 300)),
        n(3, cls="android.widget.TextView", text="Name", b=(200, 0, 500, 100)),
        n(4, cls="android.widget.TextView", text="Status", b=(200, 150, 500, 100)))])
    assert labels(tree, tb.visual_order(tree)["order"]) == ["Avatar", "Name", "Status"]


def test_containers_stay_contiguous():
    # Two cards side by side, each a traversal group with a title and a button: the reader
    # finishes one card before the next, rather than reading both titles first.
    def group(sid, x, name):
        return c(7, sid, b=(x, 0, 500, 300), is_traversal_group=True, children=[
            card(7, sid + 1, f"{name} title", x, 0, 80),
            c(7, sid + 2, cls="android.widget.Button", text=f"{name} action", flags=FOCUS,
              b=(x, 200, 200, 80))])
    grouped = tb.build([root(compose_host(7, group(10, 0, "A"), group(20, 540, "B")))])
    assert labels(grouped, tb.visual_order(grouped)["order"]) == [
        "A title", "A action", "B title", "B action"]
    loose = tb.build([root(compose_host(7, *group(10, 0, "A")["children"],
                                        *group(20, 540, "B")["children"]))])
    assert labels(loose, tb.visual_order(loose)["order"]) == [
        "A title", "B title", "A action", "B action"]


def test_expect_overrides_the_heuristic():
    tree = two_columns()
    out = tb.visual_order(tree, expect=["compose:7:4", "a1", "B2", "nope"])
    assert out["conf"] == "expect"
    assert labels(tree, out["order"]) == ["B1", "A1", "B2"]
    assert out["unmatched"] == ["nope"]


def test_windows_follow_talkback_window_order():
    w1 = root(n(2, cls="android.widget.TextView", text="Main", b=(0, 100, 500, 80)))
    w2 = root(n(11, cls="android.widget.TextView", text="Popup", b=(0, 1600, 500, 80)),
              host=10, b=(0, 1500, 1080, 400))
    tree = tb.build([w2, w1])  # z-order says popup first; geometry says main first
    assert labels(tree, tb.visual_order(tree)["order"]) == ["Main", "Popup"]


def test_order_items_takes_plain_boxes():
    # For the live walk (talkback/diff.py), which has keys and bounds but no TalkBack view.
    items = [
        {"key": "b1", "bounds": (540, 0, 500, 100), "window": 0},
        {"key": "a1", "bounds": (0, 0, 500, 120), "window": 0},
        {"key": "a2", "bounds": (0, 130, 500, 160), "window": 0},
        {"key": "b2", "bounds": (540, 110, 500, 140), "window": 0},
        {"key": "popup", "bounds": (0, 1600, 500, 80), "window": 3},
    ]
    assert tb.visual.order_items(items) == ["a1", "a2", "b1", "b2", "popup"]
    boxed = [dict(it, container=it["key"][1]) for it in items[:4]]  # rows as containers
    assert tb.visual.order_items(boxed) == ["a1", "b1", "a2", "b2"]


from inspector_widget.talkback.visual import order_items  # noqa: E402


def test_abutting_grid_reads_row_major():
    """Rows and columns that touch (no whitespace) still split: a 2x2 grid reads a, b, c, d."""
    items = [{"key": "a", "bounds": (0, 0, 100, 50)}, {"key": "b", "bounds": (150, 0, 100, 50)},
             {"key": "c", "bounds": (0, 50, 100, 50)}, {"key": "d", "bounds": (150, 50, 100, 50)}]
    assert order_items(items) == ["a", "b", "c", "d"]


def test_zero_height_item_on_a_row_boundary_terminates():
    """A zero-size gap that would leave one side of the cut empty is skipped, not recursed on."""
    items = [{"key": "a", "bounds": (0, 0, 100, 50)}, {"key": "z", "bounds": (0, 50, 100, 0)}]
    assert sorted(order_items(items)) == ["a", "z"]


def test_a_full_height_strip_beside_two_lines_is_read_first_then_line_by_line():
    """AntennaPod's episode row: the title is the contentDescription of a 12px strip as tall
    as the row, left of the date and size (one line) and the duration (the next). The strip
    blocks every horizontal cut and is alone left of the column cut; the rest reads line by
    line (TalkBack: title, date, size, duration), not column by column."""
    items = [{"key": "title", "bounds": (36, 354, 12, 279)},
             {"key": "date", "bounds": (264, 387, 111, 44)},
             {"key": "size", "bounds": (408, 387, 102, 44)},
             {"key": "duration", "bounds": (264, 556, 143, 44)}]
    assert order_items(items) == ["title", "date", "size", "duration"]
    # a trailing full-height column (a row's action button) is read last
    trailing = [{"key": "name", "bounds": (0, 0, 300, 40)}, {"key": "sub", "bounds": (0, 60, 300, 40)},
                {"key": "more", "bounds": (400, 0, 80, 100)}]
    assert order_items(trailing) == ["name", "sub", "more"]
