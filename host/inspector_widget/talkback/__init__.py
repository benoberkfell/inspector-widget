"""A static model of TalkBack 16.2's navigation over an Inspector Widget accessibility dump.

A port of google/talkback @229212f (Apache-2.0) run over the unified a11y tree:

* :mod:`.tree`: the tree TalkBack sees (not-important Views hoisted away, service-on
  corrections, window list with modality).
* :mod:`.rules`: shouldFocusNode and the predicates behind it; Role.getRole; the filters.
* :mod:`.order`: OrderedTraversalController / WorkingTree / ReorderedChildrenIterator, window
  traversal, edges and wrap: :func:`simulate` and :func:`reading_order`.
* :mod:`.speech`: the focus announcement with per-part provenance: :func:`announce`.
* :mod:`.explain`: reason codes for stops, non-stops and edges: :func:`explain`.
* :mod:`.visual`: a heuristic reading-intent order: :func:`visual_order`.

Every result carries or can report :data:`TB_RULES_REV`.
"""

from __future__ import annotations

from .explain import explain, why_not, why_stop
from .order import Navigator, Order, reading_order, simulate
from .rules import TB_RULES_REV, Rules
from .speech import Announcement, SpeechState, announce
from .tree import TbNode, TbTree, TbWindow, build
from .visual import visual_order

build_tree = build

__all__ = [
    "TB_RULES_REV",
    "Announcement",
    "Navigator",
    "Order",
    "Rules",
    "SpeechState",
    "TbNode",
    "TbTree",
    "TbWindow",
    "announce",
    "build",
    "build_tree",
    "explain",
    "reading_order",
    "simulate",
    "visual_order",
    "why_not",
    "why_stop",
]
