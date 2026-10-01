"""Offline tests for capture/refs.py: ref carry-over across captures (spec 3.9)."""

from __future__ import annotations

import copy
import json
import random
import time

import pytest
from capture_builders import LAUNCHER_ITEMS, big_index, launcher_index, wide_index
from capture_keyscenes import (
    C,
    Chain,
    Counter,
    S,
    V,
    key_space,
    rekey,
    scene,
    shift_udids,
)

from inspector_widget.capture import anchors, refs
from inspector_widget.capture import model as m


def plan(new, prev, *, same_pid=True, same_generation=True, start=1000):
    return refs.plan(new, prev, same_pid=same_pid, same_generation=same_generation,
                     alloc=Counter(start))


def launcher_pair(cid="c8m2pa"):
    """(prev, new): the spec launcher (ref-space, n1..n25 and n301..n305) and an
    identical later capture in key space."""
    return launcher_index(), key_space(launcher_index(cid))


def by_ref(prev, result):
    """ref -> key of the new capture, for the carried refs."""
    return {r: k for k, r in result.refmap.items()}


# --------------------------------------------------------------------------- first capture
def test_first_capture_allocates_in_preorder_with_one_alloc_call():
    ix = scene(
        V("DecorView", 1,
          V("LinearLayout", 2,
            V("TextView", 3, label="Title", b=(0, 0, 100, 40)),
            V("FrameLayout", 4, V("TextView", 5, label="Body"))),
          V("View", 6, rid="navigationBarBackground")),
        slots=(S("Column", "Column@Main.kt:10:0", S("Text", "Text@Main.kt:11:0")),),
        cid="c00001")
    alloc = Counter(1)
    rm, tomb = refs.assign(ix, None, same_pid=False, same_generation=True, alloc=alloc)
    order = refs.preorder(ix)
    assert [rm[k] for k in order] == [f"n{i}" for i in range(1, len(order) + 1)]
    assert order[:6] == ["view:1", "view:2", "view:3", "view:4", "view:5", "view:6"]
    assert all(k.startswith("slot:") for k in order[6:])
    assert alloc.calls == [len(order)]
    assert tomb == {}
    assert {n.match for n in ix.nodes.values()} == {"new"}
    assert {n.since for n in ix.nodes.values()} == {"c00001"}

    # the counter is store-global: a later lineage continues from it
    other = scene(V("DecorView", 1, V("TextView", 2, label="x")), cid="c00002",
                  package="com.other")
    rm2, _ = refs.assign(other, None, same_pid=False, same_generation=True, alloc=alloc)
    assert sorted(rm2.values(), key=m.ref_num) == [f"n{len(order) + 1}", f"n{len(order) + 2}"]


# --------------------------------------------------------------------------- mutation suite
def test_unchanged_scene_keeps_every_ref_with_match_id():
    prev, new = launcher_pair()
    res = plan(new, prev)
    assert res.refmap == {n.key: n.ref for n in prev.nodes.values()}
    assert set(res.match.values()) == {"id"}
    assert res.tomb == {} and res.allocated == 0 and res.rebound == {}
    # the store's counter is never touched when nothing is new
    alloc = Counter(1000)
    refs.assign(key_space(launcher_index("c8m2pb")), prev, same_pid=True, same_generation=True,
                alloc=alloc)
    assert alloc.calls == []


def test_text_change_with_the_same_pid_keeps_the_ref():
    prev, new = launcher_pair()
    new.nodes["sem:82:317"].label = "A11yProbe (renamed)"
    new.nodes["sem:82:317"].text = "A11yProbe (renamed)"
    # a list row changes its own text in place (same cell, same position)
    new.nodes["sem:82:338"].label = "Icon button label, edited"
    res = plan(new, prev)
    assert res.refmap["sem:82:317"] == "n9" and res.match["sem:82:317"] == "id"
    assert res.refmap["sem:82:338"] == "n12" and res.match["sem:82:338"] == "id"
    assert res.rebound == {} and res.tomb == {}


def _list_scene(labels, *, cid, pid=100, udids=None, rids=False):
    udids = udids or {lb: 100 + i for i, lb in enumerate(labels)}
    rows = [V("TextView", udids[lb], label=lb, rid=(f"row_{lb}" if rids else None),
              b=(0, 60 * i, 400, 50)) for i, lb in enumerate(labels)]
    return scene(V("DecorView", 1, V("LinearLayout", 2, *rows, b=(0, 0, 400, 800)),
                   b=(0, 0, 400, 800)), cid=cid, pid=pid)


@pytest.mark.parametrize("same_pid", [True, False])
def test_insertion_above_keeps_sibling_refs(same_pid):
    chain = Chain()
    a = chain.publish(_list_scene(["A", "B", "C"], cid="c00001"))
    before = {a.nodes[r].label: r for r in a.nodes if a.nodes[r].label}
    udids = {"Z": 150, "A": 100, "B": 101, "C": 102}
    if not same_pid:
        udids = {k: v + 1000 for k, v in udids.items()}
    b = chain.publish(_list_scene(["Z", "A", "B", "C"], cid="c00002", udids=udids,
                                  pid=100 if same_pid else 200))
    after = {b.nodes[r].label: r for r in b.nodes if b.nodes[r].label}
    for lb in "ABC":
        assert after[lb] == before[lb]
        assert b.nodes[after[lb]].match == ("id" if same_pid else "structure")
    assert after["Z"] not in a.nodes and b.nodes[after["Z"]].match == "new"
    assert chain.last.tomb == {}


def test_removal_writes_a_tombstone():
    chain = Chain()
    long = "A very long label that is well over forty characters in length"
    a = chain.publish(_list_scene(["A", long, "C"], cid="c00001", rids=True))
    gone = next(r for r in a.nodes if a.nodes[r].label == long)
    b = chain.publish(_list_scene(["A", "C"], cid="c00002",
                                  udids={"A": 100, "C": 102}, rids=True))
    assert gone not in b.nodes
    entry = chain.last.tomb[gone]
    assert entry[0] == "TextView"
    assert len(entry[1]) == 40 and entry[1].endswith("…") and long.startswith(entry[1][:-1])
    assert entry[2] == f"#row_{long}" and entry[3] == "c00001"
    assert list(chain.last.tomb) == [gone]
    err = refs.stale_ref_error(gone, "c00002", chain.tomb)
    assert err.code == "ref_not_in_capture"
    assert "c00001" in err.hint and f"#row_{long}" in err.hint
    assert err.to_dict()["error"]["candidates"] == [f"#row_{long}"]


def test_pid_change_carries_refs_via_rid_tag_and_structure():
    prev, new = launcher_pair()
    new = rekey(new, shift_udids(1000))  # every udid is different in the new process
    new.meta.pid = 9999
    same_pid, same_gen = refs.identity_flags(new.meta, prev.meta)
    assert (same_pid, same_gen) == (False, True)
    res = plan(new, prev, same_pid=same_pid, same_generation=same_gen)
    # every node carries its ref, nothing is new or retired
    assert res.stats["new"] == 0 and res.tomb == {}
    got = {res.refmap[shift_udids(1000)(n.key)]: n.ref for n in prev.nodes.values()}
    assert all(k == v for k, v in got.items())
    how = {prev.by_key[shift_udids(-1000)(k)]: h for k, h in res.match.items()}
    assert "id" not in how.values()
    assert how["n4"] == "locator"  # #content
    assert how["n24"] == "locator" and how["n25"] == "locator"  # system bar views
    for ref, *_ in LAUNCHER_ITEMS:
        assert how[ref] == "locator"  # @launch_* test tags
    assert how["n10"] == "locator"  # @launcher_list
    assert how["n1"] == "structure" and how["n2"] == "structure" and how["n6"] == "structure"
    assert how["n7"] == "structure" and how["n9"] == "structure"
    assert {how[r] for r in ("n301", "n302", "n303", "n304", "n305")} == {"structure"}


def test_wide_scene_pid_change_carries_every_view_by_rid():
    prev = wide_index()
    new = rekey(key_space(wide_index(capture_id="cw1de1")), shift_udids(50_000))
    res = plan(new, prev, same_pid=False)
    assert res.stats["locator"] == 259 and res.stats["new"] == 0


# --------------------------------------------------------------------------- collections
def _feed(cells, *, cid, pid=100):
    """A RecyclerView #feed; ``cells`` is [(cell udid, item label)] in on-screen order."""
    rows = []
    for i, (u, label) in enumerate(cells):
        rows.append(V("LinearLayout", u,
                      V("TextView", u + 1, rid="title", label=label, b=(0, 100 * i, 300, 50)),
                      V("ImageButton", u + 2, rid="delete", label="Delete",
                        b=(300, 100 * i, 100, 50)),
                      b=(0, 100 * i, 400, 100)))
    return scene(V("DecorView", 1,
                   V("TextView", 2, rid="header", label="Inbox", b=(0, 0, 400, 50)),
                   V("RecyclerView", 3, *rows, rid="feed", b=(0, 50, 400, 500)),
                   b=(0, 0, 400, 600)), cid=cid, pid=pid)


def test_recycled_recyclerview_cell_gets_new_refs_with_rebound_of():
    chain = Chain()
    a = chain.publish(_feed([(100, "Item 0"), (200, "Item 1"), (300, "Item 2")], cid="c00001"))
    # scroll by one: cells 200 and 300 move up, cell 100 is recycled at the bottom
    b = chain.publish(_feed([(200, "Item 1"), (300, "Item 2"), (100, "Item 3")], cid="c00002"))
    for u in (200, 201, 202, 300, 301, 302):
        key = m.view_key(u)
        assert b.by_key[key] == a.by_key[key]
        assert b.nodes[b.by_key[key]].match == "id"
    for u in (100, 101, 102):
        key = m.view_key(u)
        old, new = a.by_key[key], b.by_key[key]
        assert new != old  # same View instance, new data: new ref
        assert b.nodes[new].rebound_of == old and b.nodes[new].match == "new"
        assert old in chain.last.tomb  # "Item 0" left the screen
    assert chain.last.rebound == {m.view_key(u): a.by_key[m.view_key(u)] for u in (100, 101, 102)}
    # the collection's own nodes are untouched
    assert b.by_key["view:3"] == a.by_key["view:3"]


def test_rebinding_in_place_with_shifted_data_is_a_rebound():
    chain = Chain()
    a = chain.publish(_feed([(100, "Item 0"), (200, "Item 1"), (300, "Item 2")], cid="c00001"))
    # notifyDataSetChanged: every cell stays put but shows the next item
    b = chain.publish(_feed([(100, "Item 1"), (200, "Item 2"), (300, "Item 3")], cid="c00002"))
    for u in (100, 200, 300):
        key = m.view_key(u)
        assert b.nodes[b.by_key[key]].rebound_of == a.by_key[key]


def _mail(cells, *, cid):
    """RecyclerView #inbox: each cell has a subject and a sender (both per item)."""
    rows = [V("LinearLayout", u,
              V("TextView", u + 1, rid="subject", label=subject, b=(0, 100 * i, 400, 50)),
              V("TextView", u + 2, rid="sender", label=sender, b=(0, 100 * i + 50, 400, 50)),
              b=(0, 100 * i, 400, 100))
            for i, (u, subject, sender) in enumerate(cells)]
    return scene(V("DecorView", 1, V("RecyclerView", 3, *rows, rid="inbox", b=(0, 0, 400, 600)),
                   b=(0, 0, 400, 600)), cid=cid)


def test_in_place_cell_update_keeps_refs():
    chain = Chain()
    a = chain.publish(_mail([(100, "Lunch?", "Ana"), (200, "Standup", "Bo")], cid="c00001"))
    # the subject is edited in place; the sender (also per item) is still there
    b = chain.publish(_mail([(100, "Lunch at 1?", "Ana"), (200, "Standup", "Bo")],
                            cid="c00002"))
    assert all(b.by_key[k] == a.by_key[k] for k in a.by_key if k in b.by_key)
    assert chain.last.rebound == {}


def test_a_cell_whose_only_distinguishing_label_changes_in_place_is_a_rebound():
    """A page-sized scroll, a search filter or a refresh (notifyDataSetChanged)
    hands every View back at its own child index, bound to another item. Nothing
    but the one label tells an edit from a rebinding, so the cell is rebound."""
    chain = Chain()
    a = chain.publish(_feed([(100, "Item 0"), (200, "Item 1"), (300, "Item 2")], cid="c00001"))
    b = chain.publish(_feed([(100, "Item 3"), (200, "Item 4"), (300, "Item 5")], cid="c00002"))
    for u in (100, 101, 102, 200, 201, 202, 300, 301, 302):
        key = m.view_key(u)
        new = b.nodes[b.by_key[key]]
        assert new.ref != a.by_key[key] and new.rebound_of == a.by_key[key], key
    assert b.by_key["view:3"] == a.by_key["view:3"]  # the list itself stays


def _photos(cells, *, cid, first_row=None):
    """A vertical RecyclerView #photos of image-only cells (no label anywhere).
    ``first_row`` gives each cell its CollectionItemInfo row (adapter position)."""
    kids = []
    for i, u in enumerate(cells):
        kw = {"a11y": {"item": {"row": first_row + i, "col": 0}}} if first_row is not None \
            else {}
        kids.append(V("FrameLayout", u, V("ImageView", u + 1, rid="photo",
                                          b=(0, 200 * i, 400, 200)),
                      b=(0, 200 * i, 400, 200), flags=["click"], **kw))
    col = {"a11y": {"collection": {"rows": 99, "cols": 1}}} if first_row is not None else {}
    ix = scene(V("DecorView", 1, V("RecyclerView", 3, *kids, rid="photos", b=(0, 0, 400, 600),
                                   **col),
                 b=(0, 0, 400, 600)), cid=cid)
    anchors.assign_ui_anchors(ix)
    return ix


@pytest.mark.parametrize("rows", [True, False], ids=["adapter-rows", "sibling-shift"])
def test_recycled_image_only_cell_gets_a_new_ref(rows):
    chain = Chain()
    a = chain.publish(_photos([100, 200, 300], cid="c00001", first_row=0 if rows else None))
    # scroll by one: 200 and 300 move up; 100 is recycled at the bottom for row 3
    b = chain.publish(_photos([200, 300, 100], cid="c00002", first_row=1 if rows else None))
    for u in (200, 201, 300, 301):
        key = m.view_key(u)
        assert b.by_key[key] == a.by_key[key] and b.nodes[b.by_key[key]].match == "id"
    for u in (100, 101):
        key = m.view_key(u)
        new = b.nodes[b.by_key[key]]
        assert new.ref != a.by_key[key] and new.rebound_of == a.by_key[key]


def test_image_only_cells_without_positions_keep_refs_when_nothing_moved():
    chain = Chain()
    a = chain.publish(_photos([100, 200, 300], cid="c00001"))
    b = chain.publish(_photos([100, 200, 300], cid="c00002"))
    assert all(b.by_key[k] == a.by_key[k] for k in a.by_key)


def _grid(cells, *, cid, first_row, cols=2):
    """A GridLayoutManager RecyclerView #grid; a11y (row, col) per cell."""
    kids = []
    for i, (u, label) in enumerate(cells):
        r, c = first_row + i // cols, i % cols
        kids.append(V("FrameLayout", u,
                      V("TextView", u + 1, rid="name", label=label,
                        b=(200 * c, 300 * (i // cols) + 200, 200, 50)),
                      b=(200 * c, 300 * (i // cols), 200, 300),
                      a11y={"item": {"row": r, "col": c}}))
    ix = scene(V("DecorView", 1, V("RecyclerView", 3, *kids, rid="grid", b=(0, 0, 400, 600),
                                   a11y={"collection": {"rows": 50, "cols": cols}}),
                 b=(0, 0, 400, 600)), cid=cid)
    anchors.assign_ui_anchors(ix)
    return ix


def test_grid_cells_use_row_and_column_as_their_position():
    ix = _grid([(100, "Photo 0"), (200, "Photo 1"), (300, "Photo 2"), (400, "Photo 3")],
               cid="c00001", first_row=2)
    got = [ix.nodes[ix.by_key[m.view_key(u)]].anchor.rsplit("/", 1)[-1]
           for u in (100, 200, 300, 400)]
    assert got == ["FrameLayout[4]", "FrameLayout[5]", "FrameLayout[6]", "FrameLayout[7]"]


def test_grid_page_jump_rebinds_every_cell():
    chain = Chain()
    a = chain.publish(_grid([(100, "Photo 0"), (200, "Photo 1"), (300, "Photo 2"),
                             (400, "Photo 3")], cid="c00001", first_row=0))
    # the same Views at the same child indexes now show photos 4-7
    b = chain.publish(_grid([(100, "Photo 4"), (200, "Photo 5"), (300, "Photo 6"),
                             (400, "Photo 7")], cid="c00002", first_row=2))
    for u in (100, 101, 200, 201, 300, 301, 400, 401):
        key = m.view_key(u)
        new = b.nodes[b.by_key[key]]
        assert new.ref != a.by_key[key] and new.rebound_of == a.by_key[key], key


def test_grid_scroll_by_one_row_keeps_the_cells_that_stayed():
    chain = Chain()
    a = chain.publish(_grid([(100, "Photo 0"), (200, "Photo 1"), (300, "Photo 2"),
                             (400, "Photo 3")], cid="c00001", first_row=0))
    # row 1 moved up; the row-0 Views were recycled for row 2 (photos 4 and 5)
    b = chain.publish(_grid([(300, "Photo 2"), (400, "Photo 3"), (100, "Photo 4"),
                             (200, "Photo 5")], cid="c00002", first_row=1))
    for u in (300, 301, 400, 401):
        assert b.by_key[m.view_key(u)] == a.by_key[m.view_key(u)]
    for u in (100, 200):
        assert b.nodes[b.by_key[m.view_key(u)]].rebound_of == a.by_key[m.view_key(u)]


def test_an_insertion_above_keeps_labelled_cells_whose_positions_shifted():
    def rows(cells, *, cid):
        kids = [V("FrameLayout", u, V("TextView", u + 1, rid="t", label=lab,
                                      b=(0, 100 * i, 400, 100)),
                  b=(0, 100 * i, 400, 100), a11y={"item": {"row": r, "col": 0}})
                for i, (u, lab, r) in enumerate(cells)]
        return scene(V("DecorView", 1, V("RecyclerView", 3, *kids, rid="l", b=(0, 0, 400, 600),
                                         a11y={"collection": {"rows": 9, "cols": 1}}),
                       b=(0, 0, 400, 600)), cid=cid)

    chain = Chain()
    a = chain.publish(rows([(100, "Ana", 0), (200, "Bo", 1)], cid="c00001"))
    # a new message was inserted at the top: every item moved one position down
    b = chain.publish(rows([(300, "Cy", 0), (100, "Ana", 1), (200, "Bo", 2)], cid="c00002"))
    for u in (100, 101, 200, 201):
        assert b.by_key[m.view_key(u)] == a.by_key[m.view_key(u)]


def _inbox(cells, *, cid, first_row):
    """A RecyclerView with one section header cell (#section_title) plus rows."""
    kids = []
    for i, (u, kind, label) in enumerate(cells):
        inner = V("TextView", u + 1, rid="section_title" if kind == "h" else "msg", label=label,
                  b=(0, 100 * i, 400, 50))
        kids.append(V("FrameLayout", u, inner, b=(0, 100 * i, 400, 100),
                      a11y={"item": {"row": first_row + i, "col": 0}}))
    return scene(V("DecorView", 1, V("RecyclerView", 3, *kids, rid="inbox", b=(0, 0, 400, 600),
                                     a11y={"collection": {"rows": 99, "cols": 1}}),
                   b=(0, 0, 400, 600)), cid=cid)


def test_a_freshly_inflated_header_never_inherits_the_old_headers_ref_by_locator():
    chain = Chain()
    a = chain.publish(_inbox([(100, "h", "Today"), (200, "m", "Lunch?"), (300, "m", "Standup")],
                             cid="c00001", first_row=0))
    # scrolled: "Today" left; the "Yesterday" header is a new ViewHolder
    b = chain.publish(_inbox([(400, "m", "Invoice"), (500, "h", "Yesterday"), (600, "m", "Hi")],
                             cid="c00002", first_row=7))
    title = b.nodes[b.by_key["view:501"]]
    assert title.ref != a.by_key["view:101"] and title.match == "new"
    assert a.by_key["view:101"] in chain.last.tomb


def test_a_single_page_pager_never_carries_a_page_title_to_another_page():
    def pager(u, title, *, cid):
        return scene(V("DecorView", 1,
                       V("RecyclerView", 3,
                         V("FrameLayout", u,
                           V("TextView", u + 1, rid="page_title", label=title,
                             b=(0, 100, 400, 60)),
                           V("Button", u + 2, rid="next", label="Next", b=(0, 700, 400, 80)),
                           b=(0, 0, 400, 800)),
                         rid="pager", b=(0, 0, 400, 800)),
                       b=(0, 0, 400, 800)), cid=cid)

    chain = Chain()
    a = chain.publish(pager(100, "Welcome", cid="c00001"))
    b = chain.publish(pager(200, "Pick a plan", cid="c00002"))
    assert b.by_key["view:200"] != a.by_key["view:100"]
    assert b.by_key["view:201"] != a.by_key["view:101"]  # #page_title
    assert b.by_key["view:202"] != a.by_key["view:102"]  # #next
    assert b.by_key["view:3"] == a.by_key["view:3"]


def test_a_compose_pager_page_tag_does_not_carry_across_pages():
    def page(sid, title, *, cid):
        return scene(V("DecorView", 1,
                       V("AndroidComposeView", 82,
                         C(1, C(40, C(sid, C(sid + 1, tag="page_title", label=title,
                                             b=(0, 100, 400, 60)),
                                        b=(0, 0, 400, 800)),
                                tag="pager", attrs={"CollectionInfo": "CollectionInfo"},
                                b=(0, 0, 400, 800)),
                           b=(0, 0, 400, 800)),
                         b=(0, 0, 400, 800)),
                       b=(0, 0, 400, 800)), cid=cid)

    chain = Chain()
    a = chain.publish(page(10, "Welcome", cid="c00001"))
    b = chain.publish(page(20, "Pick a plan", cid="c00002"))
    assert b.by_key["sem:82:21"] != a.by_key["sem:82:11"]
    assert b.by_key["sem:82:40"] == a.by_key["sem:82:40"]  # @pager itself carries


def test_a_fresh_cell_never_inherits_a_scrolled_off_cells_ref():
    def grid(cells, *, cid):
        rows = [V("FrameLayout", u, V("ImageView", u + 1, b=(0, 100 * i, 400, 100)),
                  b=(0, 100 * i, 400, 100)) for i, u in enumerate(cells)]
        return scene(V("DecorView", 1, V("RecyclerView", 3, *rows, rid="grid",
                                         b=(0, 0, 400, 300)), b=(0, 0, 400, 300)), cid=cid)

    chain = Chain()
    a = chain.publish(grid([100, 200, 300], cid="c00001"))
    # scrolled by two: 100 and 200 went to the pool, 400 and 500 were inflated
    # where they used to be (same size, same place, no labels)
    b = chain.publish(grid([300, 400, 500], cid="c00002"))
    assert b.by_key["view:300"] == a.by_key["view:300"]
    fresh = {b.by_key[k] for k in ("view:400", "view:401", "view:500", "view:501")}
    assert not fresh & set(a.nodes)
    assert {a.by_key[k] for k in ("view:100", "view:200")} <= set(chain.last.tomb)


def test_cells_carry_by_content_across_a_restart():
    chain = Chain()
    a = chain.publish(_feed([(100, "Item 0"), (200, "Item 1"), (300, "Item 2")], cid="c00001"))
    new = _feed([(1200, "Item 1"), (1100, "Item 0"), (1400, "Item 9")], cid="c00002", pid=200)
    b = chain.publish(new)
    assert b.by_key["view:1100"] == a.by_key["view:100"]  # "Item 0" moved down, same ref
    assert b.by_key["view:1200"] == a.by_key["view:200"]
    assert b.by_key["view:1101"] == a.by_key["view:101"]  # its title too
    assert b.nodes[b.by_key["view:1400"]].match == "new"  # never the ref of "Item 2"
    assert a.by_key["view:300"] in chain.last.tomb


def _lazy(items, *, cid, gen=0, pid=100, feed_id=40):
    """A LazyColumn (CollectionInfo) of rows [(semantics id, label)], plus a tagged button."""
    rows = [C(sid, label=label, b=(0, 100 * i, 400, 90), flags=["click"])
            for i, (sid, label) in enumerate(items)]
    return scene(V("DecorView", 1,
                   V("AndroidComposeView", 82,
                     C(1, C(feed_id, *rows, tag="feed", attrs={"CollectionInfo": "CollectionInfo"},
                            b=(0, 0, 400, 800)),
                       C(90, tag="fab", label="Add", b=(300, 700, 90, 90)),
                       b=(0, 0, 400, 800)),
                     b=(0, 0, 400, 800)),
                   b=(0, 0, 400, 800)), cid=cid, gen=gen, pid=pid)


def test_lazycolumn_id_reminting_carries_by_structure():
    chain = Chain()
    a = chain.publish(_lazy([(10, "Row 0"), (11, "Row 1"), (12, "Row 2")], cid="c00001"))
    # scrolled out and back: Compose minted new semantics ids for every row
    b = chain.publish(_lazy([(20, "Row 0"), (21, "Row 1"), (22, "Row 2")], cid="c00002"))
    for old_sid, new_sid in ((10, 20), (11, 21), (12, 22)):
        ref = b.by_key[m.sem_key(82, new_sid)]
        assert ref == a.by_key[m.sem_key(82, old_sid)]
        assert b.nodes[ref].match == "structure"
    assert b.nodes[b.by_key["sem:82:40"]].match == "id"
    assert chain.last.tomb == {}


def test_lazycolumn_reused_semantics_id_with_new_content_is_a_rebound():
    chain = Chain()
    a = chain.publish(_lazy([(10, "Row 0"), (11, "Row 1"), (12, "Row 2")], cid="c00001"))
    # node 10 was reused for "Row 3" at the bottom after a scroll
    b = chain.publish(_lazy([(11, "Row 1"), (12, "Row 2"), (10, "Row 3")], cid="c00002"))
    assert b.by_key["sem:82:11"] == a.by_key["sem:82:11"]
    new = b.nodes[b.by_key["sem:82:10"]]
    assert new.ref != a.by_key["sem:82:10"] and new.rebound_of == a.by_key["sem:82:10"]


def test_generation_bump_disables_the_id_pass_but_locators_still_carry():
    chain = Chain()
    a = chain.publish(_lazy([(10, "Row 0"), (11, "Row 1")], cid="c00001"))
    # slots="enable" recomposed everything: new generation, every semantics id re-minted
    new = _lazy([(10, "Row 0"), (11, "Row 1")], cid="c00002", gen=1)
    new = rekey(new, lambda k: (m.sem_key(82, int(k.split(":")[2]) + 100)
                                if k.startswith("sem:") else k))
    b = chain.publish(new)
    assert refs.identity_flags(new.meta, a.meta) == (True, False)
    how = {k: n.match for k, n in ((n.key, n) for n in b.nodes.values())}
    assert "id" not in how.values()
    assert how["sem:82:140"] == "locator" and how["sem:82:190"] == "locator"  # @feed, @fab
    assert b.by_key["sem:82:140"] == a.by_key["sem:82:40"]
    assert b.by_key["sem:82:110"] == a.by_key["sem:82:10"]  # rows by structure
    assert how["sem:82:110"] == "structure"
    assert how["view:1"] == "structure"  # views too: the whole id pass is off
    assert chain.last.stats["new"] == 0


def test_twin_idless_siblings_reordered_get_new_refs_and_are_never_swapped():
    def twins(widths, *, cid, pid, base):
        kids = [V("LinearLayout", base + 100 + 10 * i,
                  V("TextView", base + 101 + 10 * i, label="Delete", b=(0, 60 * i, w, 50)),
                  b=(0, 60 * i, w, 50))
                for i, w in enumerate(widths)]
        return scene(V("DecorView", 1 + base, V("LinearLayout", 2 + base, *kids, rid="list",
                                                b=(0, 0, 400, 400)), b=(0, 0, 400, 400)),
                     cid=cid, pid=pid)

    chain = Chain()
    a = chain.publish(twins([100, 200], cid="c00001", pid=100, base=0))
    # a restart with the two identical-looking rows swapped
    b = chain.publish(twins([200, 100], cid="c00002", pid=200, base=1000))
    old = {r for r in a.nodes if a.nodes[r].depth >= 2}
    new = {r for r in b.nodes if b.nodes[r].depth >= 2}
    assert len(old) == len(new) == 4
    assert not old & new  # new refs, never swapped
    assert chain.last.ambiguous >= 2
    assert b.by_key["view:1002"] == a.by_key["view:2"]  # #list still carries

    # the same twins, unchanged across a restart, keep their refs by ordinal
    c = chain.publish(twins([200, 100], cid="c00003", pid=300, base=2000))
    assert {r for r in c.nodes if c.nodes[r].depth >= 2} == new
    assert all(c.nodes[r].match == "structure" for r in new)


def test_look_alike_rows_with_different_content_follow_their_content():
    def rows(order, *, cid, pid, base):
        kids = [V("LinearLayout", base + 100 + 10 * i, V("TextView", base + 101 + 10 * i, label=lb,
                                                   b=(0, 60 * i, 300, 50)),
                  b=(0, 60 * i, 300, 50))
                for i, lb in enumerate(order)]
        return scene(V("DecorView", 1 + base, V("LinearLayout", 2 + base, *kids,
                                                b=(0, 0, 400, 400)), b=(0, 0, 400, 400)),
                     cid=cid, pid=pid)

    chain = Chain()
    a = chain.publish(rows(["Alpha", "Beta"], cid="c00001", pid=1, base=0))
    b = chain.publish(rows(["Beta", "Alpha"], cid="c00002", pid=2, base=1000))

    def ref_of(ix, label):
        text = next(r for r in ix.nodes if ix.nodes[r].label == label)
        return ix.nodes[text].parent, text

    for lb in ("Alpha", "Beta"):
        assert ref_of(b, lb) == ref_of(a, lb)  # the row follows its content, not its slot


def test_geometry_carries_a_reparented_node_across_a_restart():
    prev = scene(V("DecorView", 1, V("LinearLayout", 2,
                                     V("ImageView", 3, b=(10, 10, 100, 100)),
                                     b=(0, 0, 400, 400)), b=(0, 0, 400, 400)),
                 cid="c00001", pid=1)
    chain = Chain()
    a = chain.publish(prev)
    # a new wrapper appeared around the image
    b = chain.publish(scene(V("DecorView", 11, V("LinearLayout", 12,
                                                 V("FrameLayout", 14,
                                                   V("ImageView", 13, b=(12, 10, 100, 100)),
                                                   b=(0, 0, 200, 200)),
                                                 b=(0, 0, 400, 400)), b=(0, 0, 400, 400)),
                            cid="c00002", pid=2))
    assert b.by_key["view:13"] == a.by_key["view:3"]
    assert b.nodes[b.by_key["view:13"]].match == "geometry"
    assert b.nodes[b.by_key["view:14"]].match == "new"


def test_geometry_never_breaks_a_tie():
    chain = Chain()
    a = chain.publish(scene(V("DecorView", 1,
                              V("FrameLayout", 2, V("ImageView", 3, b=(0, 0, 100, 100)),
                                rid="a", b=(0, 0, 400, 400)),
                              V("FrameLayout", 4, V("ImageView", 5, b=(0, 0, 100, 100)),
                                rid="b", b=(0, 0, 400, 400)),
                              b=(0, 0, 400, 800)), cid="c00001", pid=1))
    # after a restart one image sits in a new container: #a's or #b's? Equal IoU, no guess.
    b = chain.publish(scene(V("DecorView", 11,
                              V("CardView", 12, V("ImageView", 13, b=(0, 0, 100, 100)),
                                b=(0, 0, 400, 400)),
                              b=(0, 0, 400, 800)), cid="c00002", pid=2))
    img = b.nodes[b.by_key["view:13"]]
    assert img.match == "new" and img.ref not in a.nodes
    assert {a.by_key["view:3"], a.by_key["view:5"]} <= set(chain.last.tomb)

    # with only one candidate the same image is carried by geometry
    chain2 = Chain()
    a2 = chain2.publish(scene(V("DecorView", 1,
                                V("FrameLayout", 2, V("ImageView", 3, b=(0, 0, 100, 100)),
                                  rid="a", b=(0, 0, 400, 400)),
                                b=(0, 0, 400, 800)), cid="c00001", pid=1))
    b2 = chain2.publish(scene(V("DecorView", 11,
                                V("CardView", 12, V("ImageView", 13, b=(0, 0, 100, 100)),
                                  b=(0, 0, 400, 400)),
                                b=(0, 0, 400, 800)), cid="c00002", pid=2))
    assert b2.by_key["view:13"] == a2.by_key["view:3"]
    assert b2.nodes[b2.by_key["view:13"]].match == "geometry"


# --------------------------------------------------------------------------- contract details
def test_cross_lineage_and_key_space_prev_are_rejected():
    prev, new = launcher_pair()
    other = key_space(launcher_index("c8m2pa"))
    other.meta.lineage = ("emulator-5556", other.meta.package)
    with pytest.raises(m.OpError) as e:
        plan(other, prev)
    assert e.value.code == "bad_args"
    with pytest.raises(ValueError):
        plan(new, key_space(launcher_index("c1")))


def test_allocation_is_deterministic_and_checked():
    prev, _ = launcher_pair()

    new1 = rekey(key_space(launcher_index("c8m2pa")), shift_udids(7))
    new2 = copy.deepcopy(new1)
    for ix in (new1, new2):
        ix.nodes[f"view:{77 + 7}"].label = "status"  # change something so structure must work
    r1 = plan(new1, prev, same_pid=False, start=500)
    r2 = plan(new2, prev, same_pid=False, start=500)
    assert r1.refmap == r2.refmap and r1.match == r2.match and r1.tomb == r2.tomb

    def bad_alloc(n):
        return 0

    with pytest.raises(ValueError):
        refs.plan(key_space(launcher_index("c9")), None, same_pid=False, same_generation=True,
                  alloc=bad_alloc)
    chain = Chain()
    chain.publish(_list_scene(["A", "B"], cid="c00001"))
    with pytest.raises(RuntimeError):  # a counter that hands out a ref still in use
        refs.plan(_list_scene(["A", "B", "Q"], cid="c00002",
                              udids={"A": 100, "B": 101, "Q": 900}),
                  chain.prev, same_pid=True, same_generation=True, alloc=lambda n: 1)


def test_provenance_survives_apply_and_the_jsonl_round_trip():
    chain = Chain()
    chain.publish(_feed([(100, "Item 0"), (200, "Item 1")], cid="c00001"))
    chain.publish(_feed([(100, "Item 0"), (200, "Item 1")], cid="c00002"))
    c = chain.publish(_feed([(200, "Item 1"), (100, "Item 2")], cid="c00003"))
    title1 = c.nodes[c.by_key["view:201"]]
    assert title1.match == "id" and title1.since == "c00001"
    recycled = c.nodes[c.by_key["view:101"]]
    assert recycled.match == "new" and recycled.since == "c00003"
    assert recycled.rebound_of == chain.history[1].by_key["view:101"]
    back = m.index_from_jsonl(m.index_to_jsonl(c, compress=True), c.meta)
    got = back.nodes[recycled.ref]
    assert (got.match, got.since, got.rebound_of) == ("new", "c00003", recycled.rebound_of)
    assert back.nodes[title1.ref].since == "c00001"


def test_tombstones_are_capped_lru():
    tomb = {f"n{i}": ["View", None, f"n{i}", "c00001"] for i in range(1, 6)}
    refs.touch_tomb(tomb, "n1")  # n1 was just looked up
    merged = refs.merge_tomb(tomb, {"n6": ["TextView", "x", "#x", "c00002"]}, cap=4)
    assert list(merged) == ["n4", "n5", "n1", "n6"]
    assert refs.touch_tomb(merged, "n99") is None
    st = m.LineageState(tomb=dict(merged))
    refs.apply_to_lineage(st, {"n7": ["View", None, "n7", "c00003"]}, cap=4)
    assert list(st.tomb) == ["n5", "n1", "n6", "n7"]
    assert refs.merge_tomb({}, {f"n{i}": [] for i in range(6000)}) .__len__() == refs.TOMB_CAP
    err = refs.stale_ref_error("n42", "c00009", {})
    assert err.code == "ref_not_in_capture" and "c00009" in err.message


def test_identity_flags():
    a = m.CaptureMeta(id="c00001", lineage=("s", "p"), pid=5, compose_generation=0)
    assert refs.identity_flags(a, m.CaptureMeta(id="c00002", lineage=("s", "p"), pid=5)) == (
        True, True)
    assert refs.identity_flags(a, m.CaptureMeta(id="c00002", lineage=("s", "p"), pid=6,
                                                compose_generation=2)) == (False, False)
    assert refs.identity_flags(a, None) == (False, False)
    assert refs.identity_flags(m.CaptureMeta(id="c00003", lineage=("s", "p")),
                               m.CaptureMeta(id="c00004", lineage=("s", "p"))) == (False, True)


def test_iou():
    assert refs.iou([0, 0, 10, 10], [0, 0, 10, 10]) == 1.0
    assert refs.iou([0, 0, 10, 10], [5, 0, 10, 10]) == pytest.approx(50 / 150)
    assert refs.iou([0, 0, 10, 10], [20, 20, 5, 5]) == 0.0
    assert refs.iou([0, 0, 0, 10], [0, 0, 0, 10]) == 0.0
    assert refs.iou(None, [0, 0, 1, 1]) == 0.0


def test_matching_5000_nodes_is_fast():
    prev = big_index(5000)
    new = key_space(big_index(5000, capture_id="cb1g01"))
    for same_pid in (True, False):
        t0 = time.perf_counter()
        res = plan(new, prev, same_pid=same_pid)
        dt = time.perf_counter() - t0
        assert res.stats["new"] == 0
        assert dt < 1.0, dt
    # a completely new screen of the same size (geometry and structure do the work)
    other = rekey(new, shift_udids(100_000))
    for n in other.nodes.values():
        n.rid = None
    t0 = time.perf_counter()
    plan(other, prev, same_pid=False)
    assert time.perf_counter() - t0 < 3.0


# --------------------------------------------------------------------------- property test
class _World:
    """A mutable app screen with logical identities, for the random carry-over test."""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.pid = 1000
        self.gen = 0
        self.next_lid = 0
        self.next_udid = 1
        self.nodes: dict[int, dict] = {}
        self.udid: dict[int, int] = {}
        self.root = self.make("DecorView")
        self.content = self.make("LinearLayout", rid="content")
        self.nodes[self.root]["kids"] = [self.content]
        self.feed = self.make("RecyclerView", rid="feed")
        self.nodes[self.content]["kids"] = [self.feed]
        for _ in range(6):
            self.insert_leaf(self.content)
        for _ in range(4):
            self.insert_cell()
        for _ in range(3):
            box_ = self.make("LinearLayout")
            self.nodes[self.content]["kids"].append(box_)
            for _ in range(rng.randint(1, 3)):
                self.insert_leaf(box_)

    def make(self, type_: str, **kw) -> int:
        lid = self.next_lid
        self.next_lid += 1
        self.nodes[lid] = {"type": type_, "kids": [], **kw}
        self.udid[lid] = self.next_udid
        self.next_udid += 1
        return lid

    def label(self) -> str:
        return self.rng.choice(["Delete", "OK", f"L{self.rng.randint(0, 40)}",
                                f"Unique {self.next_lid}"])

    def insert_leaf(self, parent: int) -> None:
        kw = {"label": self.label()}
        if self.rng.random() < 0.2:
            kw["rid"] = f"r{self.next_lid}"
        if self.rng.random() < 0.1:
            kw["tag"] = f"t{self.next_lid}"
        leaf = self.make(self.rng.choice(["TextView", "Button", "ImageView"]), **kw)
        kids = self.nodes[parent]["kids"]
        kids.insert(self.rng.randint(0, len(kids)), leaf)

    def insert_cell(self, udids: list[int] | None = None) -> None:
        cell = self.make("LinearLayout")
        title = self.make("TextView", rid="title", label=f"Item {self.next_lid}")
        btn = self.make("ImageButton", rid="delete", label="Delete")
        self.nodes[cell]["kids"] = [title, btn]
        if udids:
            for lid, u in zip((cell, title, btn), udids):
                self.udid[lid] = u
        kids = self.nodes[self.feed]["kids"]
        kids.insert(self.rng.randint(0, len(kids)), cell)

    def all_below(self, lid: int) -> list[int]:
        out = [lid]
        for k in self.nodes[lid]["kids"]:
            out.extend(self.all_below(k))
        return out

    def parent_of(self) -> dict[int, int]:
        return {k: p for p, n in self.nodes.items() for k in n["kids"]}

    def mutate(self) -> str:
        rng = self.rng
        op = rng.choice(["relabel", "insert", "delete", "reorder", "restart", "genbump",
                         "recycle", "cell", "wrap", "none"])
        live = self.all_below(self.root)
        if op == "relabel":
            cands = [lid for lid in live if "label" in self.nodes[lid]]
            if cands:
                self.nodes[rng.choice(cands)]["label"] = self.label()
        elif op == "insert":
            parents = [lid for lid in live if self.nodes[lid]["type"] == "LinearLayout"]
            self.insert_leaf(rng.choice(parents))
        elif op == "delete":
            parents = self.parent_of()
            cands = [lid for lid in live if lid not in (self.root, self.content, self.feed)]
            if cands:
                victim = rng.choice(cands)
                self.nodes[parents[victim]]["kids"].remove(victim)
        elif op == "reorder":
            cands = [lid for lid in live if len(self.nodes[lid]["kids"]) >= 2]
            if cands:
                rng.shuffle(self.nodes[rng.choice(cands)]["kids"])
        elif op == "restart":
            self.pid += 1
            for lid in self.nodes:
                self.udid[lid] = self.next_udid
                self.next_udid += 1
        elif op == "genbump":
            self.gen += 1
        elif op == "recycle":
            cells = self.nodes[self.feed]["kids"]
            if cells:
                victim = rng.choice(cells)
                cells.remove(victim)
                udids = [self.udid[x] for x in self.all_below(victim)]
                self.insert_cell(udids)
        elif op == "cell":
            self.insert_cell()
        elif op == "wrap":
            # a new wrapper around a leaf, in a new process: geometry has to carry it
            parents = self.parent_of()
            leaves = [lid for lid in live if not self.nodes[lid]["kids"]
                      and parents.get(lid) not in (None, self.feed)]
            if leaves:
                leaf = rng.choice(leaves)
                kids = self.nodes[parents[leaf]]["kids"]
                wrapper = self.make("FrameLayout")
                kids[kids.index(leaf)] = wrapper
                self.nodes[wrapper]["kids"] = [leaf]
                self.nodes[leaf].pop("rid", None)
                self.nodes[leaf].pop("tag", None)
            self.pid += 1
            for lid in self.nodes:
                self.udid[lid] = self.next_udid
                self.next_udid += 1
        return op

    def spec(self, lid: int, depth: int, y: list[int]) -> dict:
        n = self.nodes[lid]
        if n["type"] == "FrameLayout" and len(n["kids"]) == 1:  # a wrapper takes its child's box
            kid = self.spec(n["kids"][0], depth, y)
            return V("FrameLayout", self.udid[lid], kid, b=kid["b"])
        top = y[0]
        y[0] += 40
        kids = [self.spec(k, depth + 1, y) for k in n["kids"]]
        kw = {k: n[k] for k in ("rid", "tag", "label") if k in n}
        return V(n["type"], self.udid[lid], *kids,
                 b=(8 * depth, top, 1000 - 16 * depth, max(40, y[0] - top)), **kw)

    def scene(self, cid: str) -> tuple[m.Index, dict[str, int]]:
        ix = scene(self.spec(self.root, 0, [0]), cid=cid, pid=self.pid, gen=self.gen)
        lids = {m.view_key(self.udid[lid]): lid for lid in self.all_below(self.root)}
        return ix, lids


def _cap_id(i: int) -> str:
    alphabet = m.CROCKFORD
    s = ""
    for _ in range(5):
        s = alphabet[i % 32] + s
        i //= 32
    return "c" + s


@pytest.mark.parametrize("seed", [1, 2])
def test_random_mutations_never_reuse_or_duplicate_a_ref(seed):
    rng = random.Random(seed)
    world = _World(rng)
    chain = Chain()
    retired: set[str] = set()
    ever: set[str] = set()
    prev_lids: dict[str, int] = {}
    rounds = 500  # x2 seeds = 1,000 rounds
    ops_seen: set[str] = set()
    stats: dict[str, int] = {}
    for i in range(rounds):
        op = world.mutate() if i else "none"
        ops_seen.add(op)
        new, lids = world.scene(_cap_id(i + 1))
        before_next = chain.alloc.next
        prev = chain.prev
        prev_refs = set(prev.nodes) if prev is not None else set()
        out = chain.publish(new)
        res = chain.last
        for k, v in res.stats.items():
            stats[k] = stats.get(k, 0) + v
        values = list(res.refmap.values())
        # one ref per node, one node per ref
        assert len(values) == len(set(values)) == len(new.nodes)
        for ref in values:
            if ref in prev_refs:
                continue
            assert m.ref_num(ref) >= before_next, (i, op, ref)  # fresh: never reused
        assert not (set(values) & retired), (i, op)
        assert set(res.tomb) == prev_refs - set(values)
        retired |= set(res.tomb)
        ever |= set(values)
        # device identity is sound outside collections
        if prev is not None:
            for key, how in res.match.items():
                if how != "id" or key not in lids:
                    continue
                ref = res.refmap[key]
                old_key = prev.nodes[ref].key
                inside_feed = lids[key] in world.all_below(world.feed)
                if not inside_feed and old_key in prev_lids:
                    assert prev_lids[old_key] == lids[key], (i, op, key)
        if op == "none" and prev is not None:
            assert res.stats["new"] == 0 and not res.tomb, (i, res.stats)
        prev_lids = lids
        assert len(out.nodes) == len(new.nodes)
    assert ops_seen >= {"relabel", "insert", "delete", "reorder", "restart", "genbump",
                        "recycle", "cell", "wrap", "none"}
    assert sum(stats.values()) and all(stats[k] for k in
                                       ("id", "locator", "structure", "geometry", "new",
                                        "rebound"))
    assert json.dumps(sorted(ever, key=m.ref_num)[:3]) == '["n1", "n2", "n3"]'


def test_a_rid_reused_by_another_screen_never_carries_to_another_view_class():
    """Live on Thunderbird: MessageHome's #coordinator_layout (a RelativeLayout from
    layout/message_list) and MessageCompose's (a CoordinatorLayout from
    layout/message_compose) are unique on each screen, and are different Views."""
    def screen(cls, layout, udid, *, cid):
        return scene(V("DecorView", 1,
                       V(cls, udid, V("AppBarLayout", udid + 1, rid="app_bar_layout",
                                      b=(0, 0, 400, 100),
                                      facets={"view": {"class": "AppBarLayout",
                                                       "layout_res": layout}}),
                         rid="coordinator_layout", b=(0, 0, 400, 800),
                         facets={"view": {"class": cls, "layout_res": layout}}),
                       b=(0, 0, 400, 800)), cid=cid)

    chain = Chain()
    a = chain.publish(screen("RelativeLayout", "@app:layout/message_list", 200, cid="c00001"))
    b = chain.publish(screen("CoordinatorLayout", "@app:layout/message_compose", 450,
                             cid="c00002"))
    assert b.by_key["view:450"] != a.by_key["view:200"]
    assert b.by_key["view:451"] != a.by_key["view:201"]  # another layout's app bar
    # the same View class re-inflated from the same layout (a recreated fragment) carries
    c = chain.publish(screen("CoordinatorLayout", "@app:layout/message_compose", 700,
                             cid="c00003"))
    assert c.by_key["view:700"] == b.by_key["view:450"]
    assert c.by_key["view:701"] == b.by_key["view:451"]
