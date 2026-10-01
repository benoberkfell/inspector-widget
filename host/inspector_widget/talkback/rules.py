# Portions of this file are derived from google/talkback (https://github.com/google/talkback)
# at commit 229212f (TalkBack 16.2), licensed under the Apache License, Version 2.0.
# Reimplemented in Python and modified for Inspector Widget; see NOTICE.
"""TalkBack 16.2's focus predicates, ported from google/talkback @229212f.

Citations are to the TalkBack tree at that commit; ``UT`` =
``utils/src/main/java/com/google/android/accessibility/utils/``. Every predicate runs over the
TalkBack view (:mod:`.tree`): ``parent``/``children`` are the nodes TalkBack gets, with
not-important Views hoisted through.

Where the port deliberately differs from the Java it says so:

* Web content (``WebInterfaceUtils``): TalkBack hands navigation inside a WebView to the
  WebView (ACTION_NEXT/PREVIOUS_HTML_ELEMENT), and Chromium picks the next element. The
  elements it moves through are approximated by :meth:`Rules.web_elements`, calibrated on
  TalkBack 17.0 walks (Thunderbird's message body, A11yProbe V13, AntennaPod): document order,
  elements with words or an action, whether on screen or not.
* ``ClassLoadingCache.checkInstanceOf`` loads classes in TalkBack's own process: framework
  classes and the androidx/material classes TalkBack bundles. :data:`_SUPERCLASS` stands in for
  that class loader; any other class name matches only itself.
* The picture-in-picture branch needs window data the dump does not carry, so it never fires.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Set, Tuple

from .tree import TbNode, TbTree

TB_RULES_REV = "talkback@229212f (16.2)"

# AccessibilityNodeInfo action ids (legacy bitmask actions, then android.R.id-backed ones).
ACTION_FOCUS = 0x00000001
ACTION_SELECT = 0x00000004
ACTION_CLICK = 0x00000010
ACTION_LONG_CLICK = 0x00000020
ACTION_ACCESSIBILITY_FOCUS = 0x00000040
ACTION_NEXT_HTML_ELEMENT = 0x00000400
ACTION_PREVIOUS_HTML_ELEMENT = 0x00000800
ACTION_SCROLL_FORWARD = 0x00001000
ACTION_SCROLL_BACKWARD = 0x00002000
ACTION_SET_SELECTION = 0x00020000
ACTION_EXPAND = 0x00040000
ACTION_COLLAPSE = 0x00080000
ACTION_SHOW_ON_SCREEN = 0x01020036
ACTION_SCROLL_TO_POSITION = 0x01020037
ACTION_SCROLL_UP = 0x01020038
ACTION_SCROLL_LEFT = 0x01020039
ACTION_SCROLL_DOWN = 0x0102003A
ACTION_SCROLL_RIGHT = 0x0102003B
ACTION_SET_PROGRESS = 0x0102003D
ACTION_PAGE_UP = 0x01020046
ACTION_PAGE_DOWN = 0x01020047
ACTION_PAGE_LEFT = 0x01020048
ACTION_PAGE_RIGHT = 0x01020049
SCROLL_ACTIONS = (ACTION_SCROLL_FORWARD, ACTION_SCROLL_BACKWARD, ACTION_SCROLL_DOWN,
                  ACTION_SCROLL_UP, ACTION_SCROLL_RIGHT, ACTION_SCROLL_LEFT)

# Role.java:195-587.
ROLE_NONE = 0
ROLE_BUTTON = 1
ROLE_CHECK_BOX = 2
ROLE_DROP_DOWN_LIST = 3
ROLE_EDIT_TEXT = 4
ROLE_GRID = 5
ROLE_IMAGE = 6
ROLE_IMAGE_BUTTON = 7
ROLE_LIST = 8
ROLE_RADIO_BUTTON = 9
ROLE_SEEK_CONTROL = 10
ROLE_SWITCH = 11
ROLE_TAB_BAR = 12
ROLE_TOGGLE_BUTTON = 13
ROLE_VIEW_GROUP = 14
ROLE_WEB_VIEW = 15
ROLE_PAGER = 16
ROLE_CHECKED_TEXT_VIEW = 17
ROLE_PROGRESS_BAR = 18
ROLE_NUMBER_PICKER = 29
ROLE_SCROLL_VIEW = 30
ROLE_HORIZONTAL_SCROLL_VIEW = 31
ROLE_KEYBOARD_KEY = 32
ROLE_TEXT_ENTRY_KEY = 34
ROLE_STAGGERED_GRID = 35
ROLE_FLOATING_ACTION_BUTTON = 36
ROLE_NON_MODAL_ALERT = 37
ROLE_SNACKBAR = 38
ROLE_AUDIO_CAPTION = 39
ROLE_NAVIGATION = 41
ROLE_SEARCH = 42

ROLE_NAMES = {
    ROLE_NONE: "none", ROLE_BUTTON: "button", ROLE_CHECK_BOX: "check_box",
    ROLE_DROP_DOWN_LIST: "drop_down_list", ROLE_EDIT_TEXT: "edit_text", ROLE_GRID: "grid",
    ROLE_IMAGE: "image", ROLE_IMAGE_BUTTON: "image_button", ROLE_LIST: "list",
    ROLE_RADIO_BUTTON: "radio_button", ROLE_SEEK_CONTROL: "seek_control", ROLE_SWITCH: "switch",
    ROLE_TAB_BAR: "tab_bar", ROLE_TOGGLE_BUTTON: "toggle_button", ROLE_VIEW_GROUP: "view_group",
    ROLE_WEB_VIEW: "web_view", ROLE_PAGER: "pager", ROLE_CHECKED_TEXT_VIEW: "checked_text_view",
    ROLE_PROGRESS_BAR: "progress_bar", ROLE_NUMBER_PICKER: "number_picker",
    ROLE_SCROLL_VIEW: "scroll_view", ROLE_HORIZONTAL_SCROLL_VIEW: "horizontal_scroll_view",
    ROLE_KEYBOARD_KEY: "keyboard_key", ROLE_TEXT_ENTRY_KEY: "text_entry_key",
    ROLE_STAGGERED_GRID: "staggered_grid", ROLE_FLOATING_ACTION_BUTTON: "floating_action_button",
    ROLE_NON_MODAL_ALERT: "non_modal_alert", ROLE_SNACKBAR: "snackbar",
    ROLE_AUDIO_CAPTION: "audio_caption", ROLE_NAVIGATION: "navigation", ROLE_SEARCH: "search",
}

# What TalkBack's class loader can resolve, child -> superclass (the stand-in for
# ClassLoadingCache.checkInstanceOf, UT/ClassLoadingCache.java:67-86).
_V, _VG, _TV = "android.view.View", "android.view.ViewGroup", "android.widget.TextView"
_FL, _LL = "android.widget.FrameLayout", "android.widget.LinearLayout"
_SUPERCLASS: Dict[str, Optional[str]] = {
    _V: None, _VG: _V, _TV: _V,
    "android.widget.Button": _TV,
    "android.widget.CompoundButton": "android.widget.Button",
    "android.widget.CheckBox": "android.widget.CompoundButton",
    "android.widget.RadioButton": "android.widget.CompoundButton",
    "android.widget.Switch": "android.widget.CompoundButton",
    "android.widget.ToggleButton": "android.widget.CompoundButton",
    "android.widget.CheckedTextView": _TV,
    "android.widget.EditText": _TV,
    "android.widget.AutoCompleteTextView": "android.widget.EditText",
    "android.widget.MultiAutoCompleteTextView": "android.widget.AutoCompleteTextView",
    "android.inputmethodservice.ExtractEditText": "android.widget.EditText",
    "android.widget.Chronometer": _TV, "android.widget.TextClock": _TV,
    "android.widget.ImageView": _V,
    "android.widget.ImageButton": "android.widget.ImageView",
    "android.widget.ZoomButton": "android.widget.ImageButton",
    "android.widget.QuickContactBadge": "android.widget.ImageView",
    "android.widget.ProgressBar": _V,
    "android.widget.AbsSeekBar": "android.widget.ProgressBar",
    "android.widget.SeekBar": "android.widget.AbsSeekBar",
    "android.widget.RatingBar": "android.widget.AbsSeekBar",
    "android.inputmethodservice.Keyboard$Key": None,
    _FL: _VG, _LL: _VG,
    "android.widget.RelativeLayout": _VG, "android.widget.GridLayout": _VG,
    "android.widget.AbsoluteLayout": _VG, "android.widget.Toolbar": _VG,
    "android.widget.TableLayout": _LL, "android.widget.TableRow": _LL,
    "android.widget.RadioGroup": _LL,
    "android.webkit.WebView": "android.widget.AbsoluteLayout",
    "android.widget.TabWidget": _LL,
    "android.widget.HorizontalScrollView": _FL,
    "android.widget.ScrollView": _FL,
    "android.widget.NumberPicker": _LL,
    "android.widget.SearchView": _LL,
    "android.widget.AdapterView": _VG,
    "android.widget.AbsSpinner": "android.widget.AdapterView",
    "android.widget.Spinner": "android.widget.AbsSpinner",
    "android.widget.AbsListView": "android.widget.AdapterView",
    "android.widget.GridView": "android.widget.AbsListView",
    "android.widget.ListView": "android.widget.AbsListView",
    "android.widget.ExpandableListView": "android.widget.ListView",
    "android.widget.ViewAnimator": _FL,
    "android.widget.DatePicker": _FL, "android.widget.TimePicker": _FL,
    "android.widget.CalendarView": _FL,
    "androidx.recyclerview.widget.RecyclerView": _VG,
    "androidx.viewpager.widget.ViewPager": _VG,
    "androidx.core.widget.NestedScrollView": _FL,
    "com.google.android.material.floatingactionbutton.FloatingActionButton":
        "android.widget.ImageButton",
}

_FAB_CLASS = "com.google.android.material.floatingactionbutton.FloatingActionButton"
_VIEW_PAGER_CLASSES = ("androidx.viewpager.widget.ViewPager", "android.support.v4.view.ViewPager",
                       "androidx.core.view.ViewPager", "com.android.internal.widget.ViewPager")


def is_instance(class_name: str, reference: str) -> bool:
    """ClassLoadingCache.checkInstanceOf (UT/ClassLoadingCache.java:80): equal names match;
    otherwise both must be classes TalkBack can load and ``reference`` a superclass."""
    if not class_name:
        return False
    if class_name == reference:
        return True
    c = _SUPERCLASS.get(class_name, "")
    if c == "" and class_name not in _SUPERCLASS:
        return False
    while c:
        if c == reference:
            return True
        c = _SUPERCLASS.get(c)
    return False


def _empty(s: Optional[str]) -> bool:
    """TextUtils.isEmpty."""
    return not s


class Rules:
    """TalkBack's predicates, bound to one TalkBack view.

    ``cache`` plays the role of an OrderedTraversalStrategy's speakingNodesCache: shared by the
    predicates that pass one in the Java. Calls that pass ``null`` there pass ``None`` here.
    """

    def __init__(self, tree: TbTree):
        self.tree = tree
        self.cache: Dict[int, bool] = {}
        self._role: Dict[int, int] = {}
        self._focus: Dict[int, Tuple[bool, str]] = {}
        self._flat_inside: Dict[int, bool] = {}

    # ---- primitives (UT/AccessibilityNodeInfoUtils.java) -----------------------------------
    def is_visible(self, n: Optional[TbNode]) -> bool:
        """isVisible (UT/AccessibilityNodeInfoUtils.java:2364): isVisibleToUser (after the
        service-on corrections of the TalkBack view)."""
        return n is not None and n.visible

    @staticmethod
    def is_clickable(n: Optional[TbNode]) -> bool:
        """isClickable (:1264): the flag or ACTION_CLICK."""
        return n is not None and (n.has("clickable") or n.supports(ACTION_CLICK))

    @staticmethod
    def is_long_clickable(n: Optional[TbNode]) -> bool:
        """isLongClickable (:1282)."""
        return n is not None and (n.has("long_clickable") or n.supports(ACTION_LONG_CLICK))

    @staticmethod
    def is_focusable(n: Optional[TbNode]) -> bool:
        """isFocusable (:1297): the input-focus flag or ACTION_FOCUS."""
        return n is not None and (n.has("focusable") or n.supports(ACTION_FOCUS))

    @staticmethod
    def supports_web_actions(n: Optional[TbNode]) -> bool:
        """WebInterfaceUtils.supportsWebActions: NEXT/PREVIOUS_HTML_ELEMENT."""
        return n is not None and n.supports(ACTION_NEXT_HTML_ELEMENT, ACTION_PREVIOUS_HTML_ELEMENT)

    def is_actionable_for_accessibility(self, n: Optional[TbNode]) -> bool:
        """isActionableForAccessibility (:1159): clickable, long-clickable, focusable, or
        ACTION_FOCUS / NEXT_HTML_ELEMENT / PREVIOUS_HTML_ELEMENT. ACTION_ACCESSIBILITY_FOCUS does
        not count (Compose adds it to every node)."""
        if n is None:
            return False
        if self.is_clickable(n) or self.is_long_clickable(n):
            return True
        if n.has("focusable"):
            return True
        return n.supports(ACTION_FOCUS, ACTION_NEXT_HTML_ELEMENT, ACTION_PREVIOUS_HTML_ELEMENT)

    @staticmethod
    def is_scrollable(n: Optional[TbNode]) -> bool:
        """isScrollable (:1855): any scroll action, not the scrollable flag."""
        return n is not None and n.supports(*SCROLL_ACTIONS)

    @staticmethod
    def has_text(n: Optional[TbNode]) -> bool:
        """hasText (:1879): text, contentDescription or hint; always False for a node with
        CollectionInfo (its text is only used for collection transitions)."""
        return (n is not None and not n.get("collection_info")
                and bool(n.text or n.content_description or n.hint_text))

    @staticmethod
    def has_valid_range_info(n: Optional[TbNode]) -> bool:
        """hasValidRangeInfo (:2847)."""
        ri = n.get("range_info") if n is not None else None
        if not ri:
            return False
        lo, hi, cur = ri.get("min", 0.0), ri.get("max", 0.0), ri.get("current", 0.0)
        return hi - lo > 0.0 and lo <= cur <= hi

    def has_state_description(self, n: Optional[TbNode]) -> bool:
        """hasStateDescription (:1893): stateDescription, checkable, or a valid RangeInfo."""
        return n is not None and (bool(n.state_description) or n.has("checkable")
                                  or self.has_valid_range_info(n))

    def is_focusable_or_clickable(self, n: Optional[TbNode]) -> bool:
        """isFocusableOrClickable (:1908): visible and (screenReaderFocusable or actionable)."""
        return (n is not None and self.is_visible(n)
                and (n.has("screen_reader_focusable") or self.is_actionable_for_accessibility(n)))

    # ---- Role.getRole (UT/Role.java:772) ------------------------------------------------------
    def role(self, n: Optional[TbNode]) -> int:
        if n is None:
            return ROLE_NONE
        r = self._role.get(id(n))
        if r is None:
            r = self._role[id(n)] = self._compute_role(n)
        return r

    def _compute_role(self, n: TbNode) -> int:
        if n.has("text_entry_key"):
            return ROLE_TEXT_ENTRY_KEY
        cls = n.class_name
        if is_instance(cls, _FAB_CLASS):
            return ROLE_FLOATING_ACTION_BUTTON
        if is_instance(cls, "android.widget.ImageView") or cls == "android.widget.Image":
            # node.isClickable(): the flag only.
            return ROLE_IMAGE_BUTTON if n.has("clickable") else ROLE_IMAGE
        for ref, role in (("android.widget.Switch", ROLE_SWITCH),
                          ("android.widget.ToggleButton", ROLE_TOGGLE_BUTTON),
                          ("android.widget.RadioButton", ROLE_RADIO_BUTTON),
                          ("android.widget.CompoundButton", ROLE_CHECK_BOX),
                          ("android.widget.Button", ROLE_BUTTON),
                          ("android.widget.CheckedTextView", ROLE_CHECKED_TEXT_VIEW),
                          ("android.widget.EditText", ROLE_EDIT_TEXT)):
            if is_instance(cls, ref):
                return role
        valid_range = self.has_valid_range_info(n)
        set_progress = n.supports(ACTION_SET_PROGRESS)
        if is_instance(cls, "android.widget.SeekBar") or (valid_range and set_progress):
            return ROLE_SEEK_CONTROL
        if is_instance(cls, "android.widget.ProgressBar") or (valid_range and not set_progress):
            return ROLE_PROGRESS_BAR
        if is_instance(cls, "android.inputmethodservice.Keyboard$Key"):
            return ROLE_KEYBOARD_KEY
        if is_instance(cls, "android.webkit.WebView"):
            return ROLE_WEB_VIEW
        if is_instance(cls, "android.widget.TabWidget"):
            return ROLE_TAB_BAR
        ci = n.get("collection_info")
        if is_instance(cls, "android.widget.HorizontalScrollView") and not ci:
            return ROLE_HORIZONTAL_SCROLL_VIEW
        if is_instance(cls, "android.widget.ScrollView"):
            return ROLE_SCROLL_VIEW
        if any(is_instance(cls, c) for c in _VIEW_PAGER_CLASSES):
            return ROLE_PAGER
        if is_instance(cls, "android.widget.NumberPicker"):
            return ROLE_NUMBER_PICKER
        if is_instance(cls, "android.widget.Spinner"):
            return ROLE_DROP_DOWN_LIST
        if is_instance(cls, "android.widget.GridView"):
            return ROLE_GRID
        if is_instance(cls, "android.widget.AbsListView"):
            return ROLE_LIST
        if n.supports(ACTION_PAGE_UP, ACTION_PAGE_DOWN, ACTION_PAGE_LEFT, ACTION_PAGE_RIGHT):
            return ROLE_PAGER  # ViewPager2
        if ci:
            if cls == "android.widget.listview":
                return ROLE_LIST
            if cls == "android.widget.gridview":
                return ROLE_GRID
            if cls == "androidx.recyclerview.widget.StaggeredGridLayoutManager":
                return ROLE_STAGGERED_GRID
            if (ci.get("row_count") or 0) > 1 and (ci.get("column_count") or 0) > 1:
                return ROLE_GRID
            return ROLE_LIST
        if cls in ("com.google.android.material.snackbar.BaseTransientBottomBar",):
            return ROLE_NON_MODAL_ALERT
        if cls == "com.google.android.material.snackbar.SnackBar":
            return ROLE_SNACKBAR
        if cls == "androidx.media3.ui.SubtitleView":
            return ROLE_AUDIO_CAPTION
        if cls in ("com.google.android.material.navigation.NavigationView",
                   "com.google.android.material.navigation.NavigationBarView",
                   "com.google.android.material.navigationrail.NavigationRailView"):
            return ROLE_NAVIGATION
        if is_instance(cls, "android.widget.SearchView"):
            return ROLE_SEARCH
        if is_instance(cls, "android.view.ViewGroup"):
            return ROLE_VIEW_GROUP
        return ROLE_NONE

    # ---- scroll items -------------------------------------------------------------------------
    def is_scroll_item(self, parent: Optional[TbNode]) -> bool:
        """isScrollItem (:1933): the parent scrolls (any scroll action), or is a List, Grid,
        ScrollView or HorizontalScrollView by role. A Spinner never counts."""
        if parent is None:
            return False
        role = self.role(parent)
        if role == ROLE_DROP_DOWN_LIST:
            return False
        if self.is_scrollable(parent):
            return True
        return role in (ROLE_LIST, ROLE_GRID, ROLE_SCROLL_VIEW, ROLE_HORIZONTAL_SCROLL_VIEW)

    def is_top_level_scroll_item(self, n: Optional[TbNode]) -> bool:
        """isTopLevelScrollItem (:1920): visible, and a direct child of a scroll item parent."""
        return n is not None and self.is_visible(n) and self.is_scroll_item(n.parent)

    # ---- speaking ---------------------------------------------------------------------------
    def is_speaking_node(self, n: TbNode, cache: Optional[Dict[int, bool]],
                         visited: Set[int]) -> bool:
        """isSpeakingNode (:1001): text, stateDescription, checkable, or non-actionable
        speaking children."""
        if cache is not None and id(n) in cache:
            return cache[id(n)]
        result = (self.has_text(n) or self.has_state_description(n) or n.has("checkable")
                  or self.has_non_actionable_speaking_children(n, cache, visited))
        if cache is not None:
            cache[id(n)] = result
        return result

    def has_non_actionable_speaking_children(self, n: TbNode, cache: Optional[Dict[int, bool]],
                                             visited: Set[int]) -> bool:
        """hasNonActionableSpeakingChildren (:1045): a visible child that is not focusable or
        clickable and speaks, recursively. A speaking top-level scroll item under a parent that
        is not clickable is skipped (the list scrolls, its items are stops). A child already
        visited in this search ends it (the Java's loop guard, :1061). Otherwise, invisible
        children with text count too (:1097)."""
        found = self._visible_speaking_child(n, cache, visited)
        if found is not None:
            return found
        return self.has_invisible_non_actionable_speaking_children(n)

    def _visible_speaking_child(self, n: TbNode, cache: Optional[Dict[int, bool]],
                                visited: Set[int]) -> Optional[bool]:
        """The visible-children loop of hasNonActionableSpeakingChildren (:1053-1091): True
        when a child speaks for ``n``, False when the loop guard ends the search, None when no
        visible child speaks."""
        for child in n.children:
            if id(child) in visited:
                return False
            visited.add(id(child))
            if not self.is_visible(child):
                continue
            if self.is_focusable_or_clickable(child):
                continue
            if (self.is_top_level_scroll_item(child)
                    and self.is_speaking_node(child, cache, visited)
                    and not (self.is_clickable(n) or self.is_long_clickable(n))):
                continue
            if self.is_speaking_node(child, cache, visited):
                return True
        return None

    def speech_source(self, n: TbNode) -> Optional[str]:
        """Which isSpeakingNode branch makes ``n`` speak: ``text``, ``state``, ``checkable``,
        ``children`` (visible non-actionable children), ``invisible_children`` (only children
        that are not visible, :1109) or None."""
        if self.has_text(n):
            return "text"
        if self.has_state_description(n):
            return "state"
        if n.has("checkable"):
            return "checkable"
        if self._visible_speaking_child(n, self.cache, set()):
            return "children"
        if self.has_invisible_non_actionable_speaking_children(n):
            return "invisible_children"
        return None

    def has_invisible_non_actionable_speaking_children(self, n: TbNode) -> bool:
        """hasInvisibleNonActionableSpeakingChildren (:1097-1127): an INVISIBLE child that is
        not screen-reader-focusable or actionable and has text makes the node speak, unless the
        node itself passes FILTER_AUTO_SCROLL. This is how a card whose text children are all
        off screen still gets focus (and says nothing useful: a ghost stop)."""
        if self.filter_auto_scroll(n):
            return False
        for child in n.children:
            if not child.visible and self.has_text(child) \
                    and not (child.has("screen_reader_focusable")
                             or self.is_actionable_for_accessibility(child)):
                return True
        return False

    def invisible_speaking_children(self, n: TbNode) -> list:
        """The children that make :meth:`has_invisible_non_actionable_speaking_children` true."""
        if self.filter_auto_scroll(n):
            return []
        return [c for c in n.children
                if not c.visible and self.has_text(c)
                and not (c.has("screen_reader_focusable")
                         or self.is_actionable_for_accessibility(c))]

    def is_accessibility_focusable(self, n: TbNode) -> bool:
        """isAccessibilityFocusable (:814): focusable-or-clickable, or a speaking top-level
        scroll item (no speaking cache)."""
        return self.is_focusable_or_clickable(n) or (
            self.is_top_level_scroll_item(n) and self.is_speaking_node(n, None, set()))

    # ---- shouldFocusNode ----------------------------------------------------------------------
    def are_bounds_identical_to_window(self, n: TbNode) -> bool:
        """areBoundsIdenticalToWindow (:2428). The window's bounds are the frame when the dump
        has it, else the window root's bounds."""
        return n.rect == n.window.bounds

    def should_focus_node(self, n: Optional[TbNode], check_children: bool = True) -> bool:
        """shouldFocusNode (UT/AccessibilityNodeInfoUtils.java:838), with the traversal's
        speaking cache."""
        if n is None:
            return False
        return self.focus_decision(n, check_children)[0]

    def focus_decision(self, n: TbNode, check_children: bool = True) -> Tuple[bool, str]:
        """shouldFocusNode with the branch that decided it:

        ``web`` (a WebView's root or an element its WebView moves focus to), ``web_hidden``
        (web content whose WebView is not visible), ``web_part`` (web content read as part of
        an element, or a container with nothing to say),
        ``not_visible``, ``window_wrapper`` (bounds equal to the window's, has children, neither
        focusable nor clickable), ``leaf`` (accessibility-focusable with no
        children: always focused, the unlabeled-button path), ``speaking`` (focusable with
        something to speak), ``silent_container`` (focusable, has children, nothing to speak),
        ``text_orphan`` (not focusable, has text or a state, no focusable ancestor),
        ``focusable_ancestor`` (not focusable; an ancestor takes the focus), ``no_speech``.
        With ``check_children=False`` only accessibility-focusability is decided
        (``focusable`` / ``not_focusable``), as for the ancestor check.
        """
        k = (id(n), check_children)
        hit = self._focus.get(k)  # type: ignore[arg-type]
        if hit is not None:
            return hit
        res = self._focus_decision(n, check_children)
        self._focus[k] = res  # type: ignore[index]
        return res

    def _focus_decision(self, n: TbNode, check_children: bool) -> Tuple[bool, str]:
        if self.supports_web_actions(n):
            # shouldFocusNode (:848): web content is focused if its WebView container is
            # visible. (Linear navigation reaches a WebView's root through nodeFilterOrWebView,
            # with no visibility check, and the WebView then moves through its own elements:
            # see order.Navigator and web_elements.)
            root = self.web_root_of(n)
            container = self.web_container_of(n)
            visible = container.visible if container is not None else n.visible
            if root is not None and n is not root and (not self.is_web_element(n) or any(
                    self.is_web_element(a) for a in n.ancestors() if a is not root
                    and root in a.ancestors())):
                return False, "web_part"
            return visible, ("web" if visible else "web_hidden")
        if not self.is_visible(n):
            return False, "not_visible"
        if self.are_bounds_identical_to_window(n) and n.children \
                and not self.is_focusable_or_clickable(n):
            return False, "window_wrapper"
        a11y_focusable = self.is_focusable_or_clickable(n) or (
            self.is_top_level_scroll_item(n) and self.is_speaking_node(n, None, set()))
        if not check_children:
            return a11y_focusable, "focusable" if a11y_focusable else "not_focusable"
        if a11y_focusable:
            if not n.children:
                return True, "leaf"
            if self.is_speaking_node(n, self.cache, set()):
                return True, "speaking"
            return False, "silent_container"
        focused_ancestor = self.focusable_ancestor(n)
        if focused_ancestor is None and (self.has_text(n) or self.has_state_description(n)):
            return True, "text_orphan"
        return False, "focusable_ancestor" if focused_ancestor is not None else "no_speech"

    def focusable_ancestor(self, n: TbNode) -> Optional[TbNode]:
        """The nearest ancestor accepted by ``shouldFocusNode(ancestor, cache, false)``."""
        for a in n.ancestors():
            if self.focus_decision(a, check_children=False)[0]:
                return a
        return None

    # ---- web content ----------------------------------------------------------------------------
    def is_web_root(self, n: Optional[TbNode]) -> bool:
        """The node TalkBack stops on before it hands navigation to a WebView: Role WEB_VIEW with
        the HTML navigation actions. findTargetFromNativeElement searches with
        ``nodeFilterOrWebView`` (TB/focusmanagement/FocusProcessorForLogicalNavigation.java:1357),
        which accepts it without shouldFocusNode, so no visibility check: TalkBack 17 walked into
        a WebView on an offscreen pager page (A11yProbe V13)."""
        return n is not None and self.role(n) == ROLE_WEB_VIEW and self.supports_web_actions(n)

    def web_root_of(self, n: Optional[TbNode]) -> Optional[TbNode]:
        """WebInterfaceUtils.ascendToWebView (UT/WebInterfaceUtils.java:344): the nearest
        self-or-ancestor with Role WEB_VIEW, for a node with the HTML actions."""
        if not self.supports_web_actions(n):
            return None
        return next((a for a in [n, *n.ancestors()] if self.role(a) == ROLE_WEB_VIEW), None)

    def web_container_of(self, n: Optional[TbNode]) -> Optional[TbNode]:
        """WebInterfaceUtils.ascendToWebViewContainer (UT/WebInterfaceUtils.java:335): the
        self-or-ancestor with Role WEB_VIEW whose parent's is not. In the agent's dump that is
        the WebView View, which holds Chromium's root (also Role WEB_VIEW)."""
        if not self.supports_web_actions(n):
            return None
        for a in [n, *n.ancestors()]:
            if self.role(a) == ROLE_WEB_VIEW and self.role(a.parent) != ROLE_WEB_VIEW:
                return a
        return None

    def outer_web_root(self, n: Optional[TbNode]) -> Optional[TbNode]:
        """The outermost web root around ``n``: the page an inner WebView-role node (a frame)
        belongs to, whose elements the WebView walks through in one document order."""
        root = self.web_root_of(n)
        if root is None:
            return None
        for a in root.ancestors():
            if self.is_web_root(a):
                root = a
        return root

    def is_web_element(self, n: TbNode) -> bool:
        """An element ACTION_NEXT_HTML_ELEMENT moves to (Chromium decides; this is the
        approximation measured on TalkBack 17.0): it has words of its own, an action, or is a
        heading. The texts inside a link or button are part of it, see :meth:`web_elements`."""
        if not self.supports_web_actions(n) or self.role(n) == ROLE_WEB_VIEW:
            return False
        words = (n.content_description or n.text or "").strip()
        return bool(words) or self.is_heading(n) or n.has("checkable") or n.has("focusable") \
            or self.is_clickable(n) or self.is_long_clickable(n)

    def web_elements(self, root: TbNode) -> List[TbNode]:
        """The elements TalkBack reaches inside the web root ``root``, in document (pre-)order.

        Measured on TalkBack 17.0: Thunderbird's message body (the root "Webview", a paragraph,
        an image) and A11yProbe V13 (a heading, a paragraph, two links: not the containers
        around the links, nor the texts inside them). On screen or not, and of zero size or
        not: on AntennaPod's home TalkBack read the collapsed player's show notes, every one
        reported 0px tall below the screen. An element's descendants are read as part of it."""
        out: List[TbNode] = []
        stack = list(reversed(root.children))
        while stack:
            n = stack.pop()
            if self.is_web_element(n):
                out.append(n)
                continue
            stack.extend(reversed(n.children))
        return out

    # ---- filters ------------------------------------------------------------------------------
    def filter_auto_scroll(self, n: Optional[TbNode]) -> bool:
        """FILTER_AUTO_SCROLL (:458): scrollable, visible, and a Spinner, List, Grid, ScrollView
        or HorizontalScrollView by role. A pager is NOT auto-scrolled."""
        if n is None or not self.is_scrollable(n) or not self.is_visible(n):
            return False
        return self.role(n) in (ROLE_DROP_DOWN_LIST, ROLE_LIST, ROLE_GRID, ROLE_SCROLL_VIEW,
                                ROLE_HORIZONTAL_SCROLL_VIEW)

    @staticmethod
    def is_heading(n: Optional[TbNode]) -> bool:
        """isHeading (:2901); the pre-N ListView workaround does not apply on API 24+."""
        return n is not None and n.has("heading")

    def filter_container_with_unfocusable_heading(self, n: TbNode) -> bool:
        """FILTER_CONTAINER_WITH_UNFOCUSABLE_HEADING (:273): a descendant (BFS, self included)
        that is a heading leaf TalkBack would not focus on its own."""
        queue = [n]
        while queue:
            c = queue.pop(0)
            if self.is_heading(c) and not c.children and not self.should_focus_node(c):
                return True
            queue.extend(c.children)
        return False

    def filter_control(self, n: TbNode) -> bool:
        """FILTER_CONTROL (:374): a control role, or clickable/long-clickable unless the parent
        is a List or Grid (a clickable list row is not a control)."""
        if self.role(n) in (ROLE_BUTTON, ROLE_IMAGE_BUTTON, ROLE_EDIT_TEXT, ROLE_CHECK_BOX,
                            ROLE_RADIO_BUTTON, ROLE_TOGGLE_BUTTON, ROLE_SWITCH,
                            ROLE_DROP_DOWN_LIST, ROLE_SEEK_CONTROL, ROLE_FLOATING_ACTION_BUTTON):
            return True
        return not self.node_is_list_or_grid_item(n) and (
            self.is_clickable(n) or self.is_long_clickable(n))

    def node_is_list_or_grid_item(self, n: TbNode) -> bool:
        """nodeIsListOrGridItem (:3019)."""
        return n.parent is not None and self.role(n.parent) in (ROLE_LIST, ROLE_GRID)

    def including_children(self, pred: Callable[[TbNode], bool]) -> Callable[[TbNode], bool]:
        """getFilterIncludingChildren (:317): ``pred``, or a visible non-accessibility-focusable
        descendant matching it (a Switch embedded in a focusable row)."""
        def accept(n: TbNode) -> bool:
            if pred(n):
                return True
            stack = list(n.children)
            while stack:
                c = stack.pop()
                if pred(c) and self.is_visible(c) and not self.is_accessibility_focusable(c):
                    return True
                stack.extend(c.children)
            return False
        return accept

    def filter_container(self, n: TbNode) -> bool:
        """FILTER_CONTAINER (:256): List, Grid, Pager, ScrollView, HorizontalScrollView or
        WebView by role, or a containerTitle."""
        return (self.role(n) in (ROLE_LIST, ROLE_GRID, ROLE_PAGER, ROLE_SCROLL_VIEW,
                                 ROLE_HORIZONTAL_SCROLL_VIEW, ROLE_WEB_VIEW)
                or bool(n.get("container_title")))

    def filter_collection(self, n: Optional[TbNode]) -> bool:
        """FILTER_COLLECTION (:477)."""
        return n is not None and (self.role(n) in (ROLE_LIST, ROLE_GRID, ROLE_PAGER)
                                  or bool(n.get("collection_info")))

    def filter_flat_collection(self, n: TbNode) -> bool:
        """FILTER_FLAT_COLLECTION (UT/monitor/CollectionState.java:129): a collection that is
        not marked hierarchical."""
        ci = n.get("collection_info")
        return self.filter_collection(n) and not (ci and ci.get("hierarchical"))

    def holds_flat_collection(self, n: TbNode) -> bool:
        """hasMatchingDescendant(n, FILTER_FLAT_COLLECTION), memoised."""
        hit = self._flat_inside.get(id(n))
        if hit is None:
            hit = self._flat_inside[id(n)] = any(
                self.filter_flat_collection(d) for d in list(n.iter())[1:])
        return hit

    def node_filter(self, granularity: str = "default",
                    pivot: Optional[TbNode] = None) -> Callable[[TbNode], bool]:
        """NavigationTarget.createNodeFilter (TB/focusmanagement/NavigationTarget.java:300):
        shouldFocusNode, plus the granularity's check. ``container`` navigation really runs
        findContainerTarget; here it keeps the stops whose nearest container differs from the
        pivot's."""
        base = self.should_focus_node
        if granularity in ("default", None):
            return base
        if granularity == "heading":
            return lambda n: base(n) and (self.is_heading(n)
                                          or self.filter_container_with_unfocusable_heading(n))
        if granularity == "control":
            ctrl = self.including_children(self.filter_control)
            return lambda n: base(n) and ctrl(n)
        if granularity == "container":
            here = self.container_of(pivot) if pivot is not None else None
            return lambda n: (base(n) and self.container_of(n) is not None
                              and self.container_of(n) is not here)
        raise ValueError(f"unknown granularity {granularity!r}")

    def container_of(self, n: TbNode) -> Optional[TbNode]:
        """The nearest self-or-ancestor accepted by FILTER_CONTAINER."""
        for a in [n, *n.ancestors()]:
            if self.filter_container(a):
                return a
        return None
