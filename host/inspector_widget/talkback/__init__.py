# Portions of this file are derived from google/talkback (https://github.com/google/talkback)
# at commit 229212f (TalkBack 16.2), licensed under the Apache License, Version 2.0.
# Reimplemented in Python and modified for Inspector Widget; see NOTICE.
"""A static model of TalkBack's navigation over an Inspector Widget accessibility dump.

A port of google/talkback @229212f (16.2, Apache-2.0) run over the unified a11y tree, calibrated
against TalkBack 17.0.0 on emulator-5554 (the default wording, :data:`VERSIONS`):

* :mod:`.tree`: the tree TalkBack sees (not-important Views hoisted away, service-on
  corrections, window list with modality).
* :mod:`.rules`: shouldFocusNode and the predicates behind it; Role.getRole; the filters.
* :mod:`.order`: OrderedTraversalController / WorkingTree / ReorderedChildrenIterator, window
  traversal, edges and wrap: :func:`simulate` and :func:`reading_order`.
* :mod:`.speech`: the focus announcement with per-part provenance: :func:`announce`.
* :mod:`.explain`: reason codes for stops, non-stops and edges: :func:`explain`.
* :mod:`.visual`: a heuristic reading-intent order: :func:`visual_order`, and
  :func:`.visual.order_items` for plain boxes.

:meth:`Navigator.initial_focus` gives the focus a window gets when it appears (simulate's
``start="initial"``); :attr:`TbNode.signature` identifies a node across captures without ids
or bounds. Every result carries or can report :data:`TB_RULES_REV`. This package imports no
device code: the live walk (device, inject, walk) must stay out of these imports.
"""

from __future__ import annotations

from .explain import explain, why_not, why_stop
from .order import Navigator, Order, reading_order, simulate
from .rules import TB_RULES_REV, Rules
from .speech import DEFAULT_VERSION, VERSIONS, Announcement, SpeechState, announce
from .tree import TbNode, TbTree, TbWindow, build
from .visual import visual_order

build_tree = build

__all__ = [
    "DEFAULT_VERSION",
    "TB_RULES_REV",
    "VERSIONS",
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
