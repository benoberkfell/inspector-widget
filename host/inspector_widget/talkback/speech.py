# Portions of this file are derived from google/talkback (https://github.com/google/talkback)
# at commit 229212f (TalkBack 16.2), licensed under the Apache License, Version 2.0.
# Reimplemented in Python and modified for Inspector Widget; see NOTICE.
"""What TalkBack 16.2 says when a node takes accessibility focus, with the source of every word.

A port of the focused-event feedback (``TB`` = ``talkback/src/main/java/com/google/android/
accessibility/talkback/``, @229212f):

* TB/compositor/rule/EventTypeViewAccessibilityFocusedFeedbackRule.java:181: the
  "Unlabelled" description OR the node tree description; then the collection item transition
  ("3 of 20") or "Heading"; then the collection transition ("In list, 20 items" / "Out of list")
  or the container transition ("In <containerTitle>"); then "Window <title>" on a window change.
* TB/compositor/roledescription/TreeNodesDescription.java: "selected", the tree description,
  "disabled" / "read only". The tree description is error, hint, the status (checked / expanded)
  prepended to the node's own STATE NAME ROLE (the default order,
  res/values/donottranslate.xml:1242), then its children, then the tooltip, then "for <labels>".
  Children are appended only when the node has NO contentDescription (or is a List/Grid/Pager), and
  only the visible ones that are not accessibility-focusable, recursively: a contentDescription on
  a View container silences its children. Compose never sets a contentDescription on a merging node
  with children; its fake child carries it, so the children are read as well.
* TB/compositor/roledescription/*Description.java: the per-role name / role / state.
* TB/compositor/AccessibilityNodeFeedbackUtils.java: the pieces. Quirks kept on purpose: an
  unchecked checkable node with no stateDescription says no state at all (TreeNodesDescription
  :265, "checked" is added only when checked or in a selection-mode collection), the
  supplementalDescription is never spoken (:177 hard-codes ""), and a status is dropped when the
  tree description is empty (CompositorUtils.conditionalPrepend).

English strings are TalkBack's (res/values/strings_compositor.xml). Settings are the defaults:
speak roles, speak collection info, no element ids, no custom labels, no image captions. Hints
("Double-tap to activate") are separate utterances and are not modelled.

When the node's own description comes out empty (or the node is "Unlabelled"), TalkBack speaks
the focus event's text instead (EventTypeViewAccessibilityFocusedFeedbackRule :207-225): for a
View, the text its whole subtree populates into the event. That is how a focusable ScrollView that
speaks only through an invisible child reads the entire screen in one utterance.

:data:`VERSIONS` holds what differs between the 16.2 source and TalkBack 17.0 as observed on
emulator-5554 (ttsOutput in the verbose log): 17.0 joins the parts with ". " (the
``usePeriodAsSeparator`` feature flag, a stub returning false in the open 16.2 source,
TB/flags/FeatureFlagReader.java:328), and it names a pager by its orientation going in ("In
horizontal pager", "In vertical pager") and says "Out of grid pager" coming out (Thunderbird's
message pager, AntennaPod's player, A11yProbe V13/C16). Everything else checked so far matches,
on the A11yProbe corpus and on Thunderbird, Now in Android and AntennaPod walks: no "not
checked" for an unchecked Checkbox in either toolkit, "off. Notify. Switch" for a View Switch,
"On. Notify. Switch" for Compose, "Button" for an unlabelled control ("Search. Button" for a
clickable ImageView), "not checked. Light. Radio button. 1 of 3. In list. 3 items" for a
single-choice CheckedTextView, "selected. Inbox. 7. Tab. In list. 9 items".

"Editing" (EditTextDescription.stateDescription: input focus and an active keyboard) depends
on the walk: with a hardware keyboard TalkBack 17 says it for every edit box it reaches
(Thunderbird's read-only dropdown fields: "Editing. Every 15 minutes. Edit box. ... read only"),
so tb-walk's model passes ``keyboard=True``.
"""

from __future__ import annotations

import unicodedata
from typing import Any, Dict, List, Optional, Sequence

from . import rules as R
from .rules import Rules
from .tree import TbNode, populated_text

SEP = ", "

# What TalkBack speaks differently by version. "16.2": the open source @229212f. "17.0": the
# Play build observed on emulator-5554 (TALKBACK_DESIGN.md part 9; the real-app walks).
VERSIONS: Dict[str, Dict[str, Any]] = {
    "16.2": {"separator": ", ", "pager_in": {}, "pager_out": "Out of pager"},
    "17.0": {"separator": ". ",
             "pager_in": {"horizontal": "In horizontal pager", "vertical": "In vertical pager"},
             "pager_out": "Out of grid pager"},
}
DEFAULT_VERSION = "17.0"


def profile(version: Optional[str]) -> Dict[str, Any]:
    try:
        return VERSIONS[version or DEFAULT_VERSION]
    except KeyError:
        raise ValueError(f"unknown TalkBack version {version!r}; known: {sorted(VERSIONS)}") \
            from None


def separator(version: Optional[str]) -> str:
    return profile(version)["separator"]

# Role.getRole -> the role word (AccessibilityNodeFeedbackUtils.getNodeRoleName :263).
ROLE_WORDS = {
    R.ROLE_BUTTON: "Button", R.ROLE_IMAGE_BUTTON: "Button",
    R.ROLE_FLOATING_ACTION_BUTTON: "Button", R.ROLE_CHECK_BOX: "Check box",
    R.ROLE_DROP_DOWN_LIST: "Drop down list", R.ROLE_EDIT_TEXT: "Edit box", R.ROLE_GRID: "Grid",
    R.ROLE_IMAGE: "Image", R.ROLE_LIST: "List", R.ROLE_PAGER: "Multi-page view",
    R.ROLE_PROGRESS_BAR: "Progress bar", R.ROLE_RADIO_BUTTON: "Radio button",
    R.ROLE_CHECKED_TEXT_VIEW: "Radio button", R.ROLE_SEEK_CONTROL: "Slider",
    R.ROLE_SWITCH: "Switch", R.ROLE_TOGGLE_BUTTON: "Switch", R.ROLE_TAB_BAR: "Tab bar",
    R.ROLE_WEB_VIEW: "Webview",
}

NAVIGATE_NONE, NAVIGATE_ENTER, NAVIGATE_EXIT, NAVIGATE_INTERIOR = 0, 1, 2, 3

# Part kinds that name a node (as opposed to its role or state).
_NAMING_KINDS = {"name", "child", "name(fake)", "label", "hint", "hint(child)", "error", "event"}


class _Seg:
    """One spoken piece: text, the node it came from, what it is, and how it joins the text
    before it (None: the version's separator; " " for the "for <label>" template)."""

    __slots__ = ("text", "node", "kind", "glue")

    def __init__(self, text: str, node: TbNode, kind: str, glue: Optional[str] = None):
        self.text, self.node, self.kind, self.glue = text, node, kind, glue


class Announcement:
    """``text``: the utterance; ``parts``: [{"text", "from", "kind"}]; ``unlabelled``: TalkBack
    found nothing to name the node with (it says "Unlabelled" or only a role word)."""

    def __init__(self, segs: Sequence[_Seg], unlabelled: bool, sep: str = SEP):
        text = ""
        for s in segs:
            text = s.text if not text else text + (sep if s.glue is None else s.glue) + s.text
        # The utterance is spoken trimmed, non-breaking spaces too, but not inside: "Follow:\xa0"
        # says "Follow:" (AntennaPod's show notes) while a row's "[attachment_icon] " child keeps
        # its space before the next part (Thunderbird), on TalkBack 17.0.
        self.text = text.strip()
        self.parts: List[Dict[str, Any]] = [
            {"text": s.text, "from": s.node.key, "kind": s.kind} for s in segs]
        self.unlabelled = unlabelled

    def __repr__(self) -> str:
        return f"Announcement({self.text!r})"


class SpeechState:
    """TalkBack's state between two focus events: the collection it is in
    (UT/monitor/CollectionState.java), the container title and the window."""

    def __init__(self) -> None:
        self.transition = NAVIGATE_NONE
        self.root: Optional[TbNode] = None
        self.item: Optional[tuple] = None  # (row index, column index, heading)
        self.row_col = (False, False)  # whether the row / column changed
        self.last: Optional[TbNode] = None
        self.container_title = ""
        self.window: Optional[int] = None


def _trim(s: Optional[str]) -> str:
    return s if s and s.strip() else ""


#: What TalkBack says for a text that is one symbol and nothing else (SpeechCleanupUtils
#: .cleanUp: a single character is replaced by its spoken name). Generated from TalkBack's
#: own table, TALKBACK_PUNCTUATION_AND_SYMBOL in utils/output/SpeechCleanupUtils.java with
#: the English names of utils/res/values/strings_symbols.xml: every key it has, nothing it
#: lacks ("+" is read as it is). Measured on TalkBack 17.0: AntennaPod's show notes, whose
#: list bullets are their own web elements ("•" -> "Bullet. 1 of 19. In list. 19 items"),
#: and a paragraph that is only "." ("Period").
SYMBOL_NAMES = {
    "&": "Ampersand", "<": "Less than sign", ">": "Greater than sign", "'": "Apostrophe",
    "*": "Asterisk", "@": "At", "\\": "Backslash", "•": "Bullet", "^": "Caret",
    "¢": "Cent sign", ":": "Colon", ",": "Comma", "©": "Copyright", "{": "Left curly bracket",
    "}": "Right curly bracket", "°": "Degree sign", "÷": "Division sign", "…": "Ellipsis",
    "—": "Em dash", "–": "En dash", "!": "Exclamation mark", "`": "Grave accent", "-": "Dash",
    "„": "Low double quote", "×": "Multiplication sign", "\n": "New line",
    "¶": "Paragraph mark", "(": "Left paren", ")": "Right paren", "%": "Percent",
    ".": "Period", "π": "Pi", "#": "Pound", "$": "Dollar sign", "€": "Euro",
    "£": "Pound currency sign", "¥": "Yen", "₱": "Peso sign", "₹": "Rupee", "₫": "Dong sign",
    "¤": "Currency sign", "?": "Question mark", "\"": "Quote", "®": "Registered trademark",
    ";": "Semicolon", "/": "Slash", " ": "Space", "\u00a0": "Space",
    "[": "Left square bracket", "]": "Right square bracket", "√": "Square root",
    "™": "Trademark", "✓": "Check mark", "_": "Underscore", "|": "Vertical line",
    "¬": "Not sign", "¦": "Broken bar", "µ": "Micro sign", "≈": "Almost equal to",
    "≠": "Not equal to", "§": "Section sign", "↑": "Upwards arrow", "←": "Leftwards arrow",
    "→": "Rightwards Arrow", "↓": "Downwards Arrow", "♥": "Black Heart", "~": "Tilde",
    "=": "Equal sign", "￦": "Won currency sign", "₩": "Won currency sign",
    "※": "Reference Mark", "☆": "White star", "★": "Black star", "♡": "White Heart",
    "○": "White circle", "●": "Black circle", "⊙": "Solar symbol", "◎": "Bullseye",
    "♧": "White club suit", "♤": "White spade suit", "☜": "White left pointing index",
    "☞": "White right pointing index", "◐": "Circle with left half black",
    "◑": "Circle with right half black", "□": "White square", "■": "Black square",
    "△": "White up pointing triangle", "▽": "White down pointing triangle",
    "◁": "White left pointing triangle", "▷": "White right pointing triangle",
    "◇": "White diamond", "♩": "Quarter Note", "♪": "Eighth Note",
    "♬": "Beamed sixteenth notes", "♀": "Female symbol", "♂": "Male symbol",
    "【": "Left Black Lenticular Bracket", "】": "Right Black Lenticular Bracket",
    "「": "Left Corner Bracket", "」": "Right Corner Bracket", "±": "Plus minus sign",
    "ℓ": "Liter", "℃": "Celsius degree", "℉": "Fahrenheit degree", "≒": "Approximately equals",
    "∫": "Integral", "⟨": "Mathematical left angle bracket",
    "⟩": "Mathematical right angle bracket", "〒": "Postal mark",
    "▲": "Black triangle pointing up", "▼": "Black triangle pointing down",
    "♦": "Black suit of diamonds", "◆": "Black suit of diamonds",
    "･": "Halfwidth Katakana middle dot", "▪": "Small black square",
    "《": "Left double angle bracket", "》": "Right double angle bracket",
    "¡": "Inverted exclamation mark", "¿": "Inverted question mark", "，": "Full-width comma",
    "！": "Full-width exclamation mark", "。": "Ideographic full stop",
    "？": "Full-width question mark", "·": "Middle dot", "”": "Right double quotation mark",
    "、": "Ideographic comma", "：": "Full-width colon", "；": "Full-width semicolon",
    "＆": "Full-width ampersand", "＾": "Full-width circumflex", "～": "Full-width tilde",
    "“": "Left double quotation mark", "（": "Full-width left parenthesis",
    "）": "Full-width right parenthesis", "＊": "Full-width asterisk",
    "＿": "Full-width underscore", "’": "Right single quotation mark",
    "｛": "Full-width left curly bracket", "｝": "Full-width right curly bracket",
    "＜": "Full-width less than sign", "＞": "Full-width greater than sign",
    "‘": "Left single quotation mark", "\u064e": "\u0641\u064e\u062a\u0652\u062d\u064e\u0629",
    "\u0650": "\u0643\u064e\u0633\u0652\u0631\u064e\u0629",
    "\u064f": "\u0636\u064e\u0645\u064e\u0651\u0629",
    "\u064b": "\u0641\u062a\u062d\u064e\u0629 \u062a\u0646\u0648\u064a\u0646",
    "\u064d": ("\u0643\u064e\u0633\u0652\u0631\u064e\u0629 "
                "\u062a\u064e\u0646\u0652\u0648\u0650\u064a\u0646"),
    "\u064c": ("\u0636\u064e\u0645\u064e\u0651\u0629 "
                "\u062a\u064e\u0646\u0652\u0648\u0650\u064a\u0646"),
    "\u0651": "\u0634\u064e\u062f\u0651\u0629\u200e",
    "\u0652": "\u0633\u064f\u0643\u064f\u0648\u0646\u0652",
}

#: Character.isWhitespace: what SpannableUtils.trimText trims (a no-break space is not
#: whitespace to Java, so a lone one is a symbol: "Space").
_JAVA_SPACE = frozenset("\t\n\x0b\x0c\r\x1c\x1d\x1e\x1f")
_NO_BREAK = frozenset("\u00a0\u2007\u202f")


def _java_ws(c: str) -> bool:
    return c in _JAVA_SPACE or (c not in _NO_BREAK and unicodedata.category(c) in
                                ("Zs", "Zl", "Zp"))


def spoken_text(s: str) -> str:
    """A node's text as TalkBack speaks it (SpeechCleanupUtils.cleanUp): trimmed of Java
    whitespace, a lone symbol is read by its name; a text of whitespace only by the name of
    its first character ("Space", "New line"); anything else as it is."""
    if not s:
        return s
    i, j = 0, len(s)
    while i < j and _java_ws(s[i]):
        i += 1
    while j > i and _java_ws(s[j - 1]):
        j -= 1
    if j - i == 1:
        return SYMBOL_NAMES.get(s[i], s[i:j])
    if j == i:
        return SYMBOL_NAMES.get(s[0], s[0])
    return s


def node_text(n: TbNode) -> str:
    """AccessibilityNodeInfoUtils.getNodeText (UT :501): the contentDescription, else the text."""
    return _trim(n.content_description) or _trim(n.text)


class _Composer:
    def __init__(self, rules: Rules, focused: TbNode, selection_mode: int, sep: str = SEP,
                 keyboard: bool = False):
        self.r = rules
        self.focused = focused
        self.selection_mode = selection_mode
        self.sep = sep
        self.keyboard = keyboard

    # -- AccessibilityNodeFeedbackUtils --------------------------------------------------------
    def state_description(self, n: TbNode) -> str:
        """getNodeStateDescription (:205): stateDescription, with "Required" for a required
        field."""
        state = _trim(n.state_description)
        if n.has("field_required"):
            state = f"{state} required" if state else "Required"
        return state

    def role_description(self, n: TbNode) -> str:
        """defaultRoleDescription (:230): the node's roleDescription, else the role word."""
        return n.get("role_description") or ROLE_WORDS.get(self.r.role(n), "")

    def text_or_label(self, n: TbNode) -> str:
        """getNodeTextOrLabelDescription (:109): contentDescription, else text. (Custom labels
        and image captions are off.)"""
        return n.content_description or n.text

    def labels(self, n: TbNode) -> str:
        """getDescriptionFromLabelNode (:417): the labelled-by nodes' text, deduplicated."""
        keys = n.get("labeled_by_list") or ([n.get("labeled_by")] if n.get("labeled_by") else [])
        out: List[str] = []
        for k in keys:
            lab = self.r.tree.resolve_raw(k)
            t = (lab.get("content_description") or lab.get("text") or "") if lab else ""
            if t and t not in out:
                out.append(t)
        return self.sep.join(out)

    def needs_label(self, n: TbNode) -> bool:
        """TalkBackLabelManager.needsLabel (TB/labeling/TalkBackLabelManager.java:56): enabled,
        clickable / long-clickable / (focusable and labelable by resource id), NO children and
        no contentDescription or text. (The hasFocusedEventText exemption for ViewGroup roles
        needs the focus event and is not modelled.)"""
        can_add_label = ":id/" in (n.get("view_id_resource_name") or "")
        return (n.has("enabled")
                and (self.r.is_clickable(n) or self.r.is_long_clickable(n)
                     or (self.r.is_focusable(n) and can_add_label))
                and not n.children and not node_text(n))

    def unlabelled(self, n: TbNode) -> List[_Seg]:
        """getUnlabelledNodeDescription (:300): the role word, or "Unlabelled", for a node that
        needs a label and has no text, state, hint or label at all."""
        if not self.needs_label(n) or n.has("checkable") \
                or self.r.role(n) in (R.ROLE_SEEK_CONTROL, R.ROLE_PROGRESS_BAR):
            return []
        if self.state_description(n) or self.text_or_label(n) or n.hint_text or self.labels(n):
            return []
        role = self.role_description(n)
        return [_Seg(role, n, "role")] if role else [_Seg("Unlabelled", n, "unlabelled")]

    # -- RoleDescriptionExtractor + the RoleDescription classes ------------------------------------
    def role_description_text(self, n: TbNode) -> List[_Seg]:
        """RoleDescriptionExtractor.nodeRoleDescriptionText (:92): "Unlabelled", or the
        de-duplicated STATE, NAME, ROLE."""
        unl = self.unlabelled(n)
        if unl:
            return unl
        focused = n is self.focused
        role = self.r.role(n)
        state = name = role_word = ""
        state_kind = "state"
        if role in (R.ROLE_SWITCH, R.ROLE_TOGGLE_BUTTON):  # SwitchDescription
            sd = self.state_description(n)
            name = n.content_description or (n.text if sd else "")
            role_word = self.role_description(n)
            state = sd or n.text or ("on" if n.has("checked") else "off")
            if not sd and n.text:
                state_kind = "name"  # the text is spoken in the state slot
        elif role == R.ROLE_EDIT_TEXT:  # EditTextDescription
            if n.has("password"):
                pieces = [n.content_description or "password"]
                if n.text:
                    if n.supports(R.ACTION_SET_SELECTION):
                        k = len(n.text)
                        pieces.append(f"{k} character" + ("" if k == 1 else "s"))
                    else:
                        pieces.append(n.text)
                name = self.sep.join(pieces)
            else:
                name = n.text or n.content_description
            role_word = self.role_description(n)
            state = self.state_description(n)
            if self.editing(n):  # "Editing", isCurrentlyEditing (EditTextDescription:126)
                state = f"{state}{self.sep}Editing" if state else "Editing"
        elif role in (R.ROLE_IMAGE, R.ROLE_IMAGE_BUTTON):  # NonTextViewsDescription
            name = self.text_or_label(n)
            if n.get("role_description"):
                role_word = n.get("role_description")
            elif role == R.ROLE_IMAGE and n.supports(R.ACTION_SELECT):
                role_word = "Image" if focused else ""
            else:
                role_word = ROLE_WORDS.get(role, "")
            state = self.state_description(n) or ("" if name else "Unlabelled")
        elif role in (R.ROLE_SEEK_CONTROL, R.ROLE_PROGRESS_BAR):  # SeekBarDescription
            if not focused and n.get("live_region") in (None, "NONE", 0):
                return []  # shouldIgnoreDescription
            name = self.text_or_label(n)
            role_word = self.role_description(n)
            state = self.state_description(n) or _range_text(n.get("range_info"))
        elif role == R.ROLE_PAGER or self._is_pager_page(n):  # PagerPageDescription
            name = self.text_or_label(n)
            role_word = n.get("role_description") or "Page"
            state = self.state_description(n)
        else:  # DefaultDescription
            name = self.text_or_label(n)
            role_word = self.role_description(n)
            state = self.state_description(n)
        name = spoken_text(name)
        segs, seen = [], set()
        for text, kind in ((state, state_kind), (name, "name"), (role_word, "role")):
            if text and text.lower() not in seen:  # CompositorUtils.dedupJoin
                seen.add(text.lower())
                segs.append(_Seg(text, n, kind))
        return segs

    def editing(self, n: TbNode) -> bool:
        """node.isFocused() && isKeyBoardActive(): the edit box has input focus and a keyboard
        is up. The dump cannot tell whether a keyboard is up, so this needs ``keyboard``; with
        a hardware keyboard (a key-driven walk) TalkBack 17 says it for every edit box it
        focuses, input-focused or not."""
        return n is self.focused and self.keyboard and (
            n.has("focused") or n.has("focusable") or n.has("editable"))

    def _is_pager_page(self, n: TbNode) -> bool:
        p = n.parent
        return (p is not None and self.r.role(p) == R.ROLE_PAGER
                and sum(1 for c in p.children if c.visible) == 1)

    # -- TreeNodesDescription -------------------------------------------------------------
    def status(self, n: TbNode) -> List[_Seg]:
        """nodeStatusDescription (:238): collapsed / expanded, then "checked" / "not checked"
        only when there is no stateDescription (or the field is required) AND the node is
        checked or the collection is in a selection mode."""
        out: List[_Seg] = []
        if n.supports(R.ACTION_EXPAND):
            out.append(_Seg("collapsed", n, "state"))
        elif n.supports(R.ACTION_COLLAPSE):
            out.append(_Seg("expanded", n, "state"))
        if (not self.state_description(n) or n.has("field_required")) and n.has("checkable") \
                and (self.selection_mode != 0 or n.has("checked")):
            out.append(_Seg("checked" if n.has("checked") else "not checked", n, "state"))
        return out

    def tree_nodes(self, n: TbNode, depth: int) -> List[_Seg]:
        """treeNodesDescription (:285): the node's own description, then its visible,
        non-accessibility-focusable children (in ReorderedChildrenIterator order) when it has no
        contentDescription or is a List, Grid or Pager."""
        from .order import reordered_children

        segs = self.role_description_text(n)
        role = self.r.role(n)
        if role != R.ROLE_WEB_VIEW and depth < 64 and (
                role in (R.ROLE_GRID, R.ROLE_LIST, R.ROLE_PAGER) or not n.content_description):
            kids, _ = reordered_children(self.r, n)
            for c in kids:
                if self.r.is_visible(c) and not self.r.is_accessibility_focusable(c):
                    segs.extend(self.appended_tree(c, depth + 1))
        return segs

    def appended_tree(self, n: TbNode, depth: int = 0) -> List[_Seg]:
        """getAppendedTreeDescription (:192): error, hint, status + tree, tooltip."""
        segs: List[_Seg] = []
        if n.has("content_invalid") and n.get("error"):
            segs.append(_Seg(f"Error: {n.get('error')}", n, "error"))
        hint = n.hint_text
        if hint and not ((self.r.role(n) == R.ROLE_EDIT_TEXT and n.text)
                         or n.has("showing_hint_text")):
            segs.append(_Seg(hint, n, "hint"))  # getHintDescription (:547)
        tree = self.tree_nodes(n, depth)
        if tree:  # conditionalPrepend: the status needs something to prepend to
            segs.extend(self.status(n))
            segs.extend(tree)
        tip = n.get("tooltip_text")
        if tip and tip != (n.content_description or n.text):
            segs.append(_Seg(tip, n, "tooltip"))  # getUniqueTooltipText (:526)
        return segs

    def aggregate(self, n: TbNode) -> List[_Seg]:
        """aggregateNodeTreeDescription (:100), STATE_NAME_ROLE: selected, tree (+ "for
        <labels>"), then disabled or read only."""
        segs: List[_Seg] = []
        if n.has("selected"):
            segs.append(_Seg("selected", n, "selected"))
        tree = self.appended_tree(n)
        label = self.labels(n)
        if label and tree:  # template_labeled_item "%1$s for %2$s"
            tree.append(_Seg(f"for {label}", n, "label", glue=" "))
        segs.extend(tree)
        if not n.has("enabled") and not n.has("heading"):  # announceDisabled (:468), API 31+
            segs.append(_Seg("disabled", n, "disabled"))
        elif self.r.role(n) == R.ROLE_EDIT_TEXT and n.has("enabled") and not n.has("editable"):
            segs.append(_Seg("read only", n, "read_only"))
        return segs


def _range_text(ri: Optional[Dict[str, Any]]) -> str:
    """SeekBarDescription.seekBarPercentText: String.valueOf(float) keeps the ".0"."""
    if not ri:
        return ""
    cur = float(ri.get("current", 0.0))
    kind = ri.get("type")
    if kind in ("PERCENT", 2):
        return f"{_java_float(cur)} percent"
    if kind in ("INT", 0):
        return str(int(cur))
    if kind in ("FLOAT", 1):
        return _java_float(cur)
    return ""


def _java_float(x: float) -> str:
    return f"{x:.1f}" if x == int(x) else repr(x)


def _mark_descendants(segs: List[_Seg], focused: TbNode) -> List[_Seg]:
    """Tag the pieces that came from descendants: ``child`` for their names, ``<kind>(child)``
    otherwise; ``(fake)`` for Compose's synthetic children."""
    for s in segs:
        if s.node is focused:
            continue
        if s.node.is_fake:
            s.kind = f"{s.kind}(fake)"
        elif s.kind == "name":
            s.kind = "child"
        else:
            s.kind = f"{s.kind}(child)"
    return segs


# --------------------------------------------------------------------------------------------
# Collections (UT/monitor/CollectionState.java + TB/compositor/CollectionStateFeedbackUtils.java)
# --------------------------------------------------------------------------------------------


def _is_collection_or_item(r: Rules, n: TbNode) -> bool:
    return r.filter_collection(n) or bool(n.get("collection_item_info"))


def collection_root_exclude_self(r: Rules, n: TbNode) -> Optional[TbNode]:
    """getCollectionRootExcludeSelf: the nearest collection above ``n``, allowing one collection
    item on the way up."""
    found = None
    for a in n.ancestors():
        if _is_collection_or_item(r, a):
            found = a
            break
    if found is None or r.filter_collection(found):
        return found
    for a in found.ancestors():
        if _is_collection_or_item(r, a):
            return a if r.filter_collection(a) else None
    return None


def _should_enter(r: Rules, root: TbNode) -> bool:
    """CollectionState.shouldEnter (:952): more than one item (or unknown counts), and not a
    flat collection that holds another flat one (only the innermost is announced: a feed
    holding a row of chips says "In list" for the chips, not for the feed)."""
    ci = root.get("collection_info")
    if ci:
        rows, cols = int(ci.get("row_count", -1)), int(ci.get("column_count", -1))
        if not (rows == -1 and cols == -1) and rows * cols in (0, 1):
            return False
    elif len(root.children) <= 1:
        return False
    if r.filter_flat_collection(root) and r.holds_flat_collection(root):
        return False
    return True


def _counts(root: TbNode):
    ci = root.get("collection_info") or {}
    return int(ci.get("row_count", -1)) if ci else -1, int(ci.get("column_count", -1)) if ci else -1


def _item_state(r: Rules, root: TbNode, n: TbNode) -> Optional[tuple]:
    """getListItemState / getTableItemState: the self-or-ancestor (below the root) with
    CollectionItemInfo."""
    if not root.get("collection_info"):
        return None
    item = None
    for a in [n, *n.ancestors()]:
        if a is root:
            break
        if a.get("collection_item_info"):
            item = a
            break
    if item is None:
        return None
    info = item.get("collection_item_info")
    return (int(info.get("row_index", -1)), int(info.get("column_index", -1)),
            bool(info.get("heading")) or r.is_heading(item))


def _plural(k: int, one: str) -> str:
    return f"{k} {one}" if k == 1 else f"{k} {one}s"


def _update_collection(r: Rules, st: SpeechState, n: TbNode) -> None:
    """CollectionState.updateCollectionInformation (:676): the NONE/ENTER/INTERIOR/EXIT
    machine."""
    new_root = st.root if (st.root is not None and n is st.root) \
        else collection_root_exclude_self(r, n)
    if st.transition in (NAVIGATE_ENTER, NAVIGATE_INTERIOR):
        if new_root is not None and new_root is st.root:
            st.transition = NAVIGATE_INTERIOR
        elif new_root is not None and _should_enter(r, new_root):
            st.transition = NAVIGATE_ENTER
        else:
            st.transition = NAVIGATE_EXIT
    else:
        st.transition = NAVIGATE_ENTER if new_root is not None and _should_enter(r, new_root) \
            else NAVIGATE_NONE
    if st.transition == NAVIGATE_ENTER:
        item = _item_state(r, new_root, n)
        st.row_col = (True, True) if item else (False, False)
        st.root, st.last, st.item = new_root, n, item
    elif st.transition == NAVIGATE_INTERIOR:
        item = _item_state(r, new_root, n)
        if item is None:
            st.row_col = (False, False)
        elif st.item is None or st.last is None or st.last is n:
            st.row_col = (True, True)
        else:
            st.row_col = (item[0] != st.item[0], item[1] != st.item[1])
        st.root, st.last, st.item = new_root, n, item
    elif st.transition == NAVIGATE_EXIT:
        st.item, st.row_col = None, (False, False)
    else:
        st.root, st.item, st.row_col = None, None, (False, False)


def _collection_name(root: TbNode) -> str:
    return root.get("container_title") or node_text(root)


def _collection_transition(r: Rules, st: SpeechState, sep: str = SEP,
                           version: Optional[str] = None) -> str:
    """getCollectionTransitionDescription (:54), for lists, grids and pagers without a
    roleDescription. TalkBack 17 words a pager by its orientation (:data:`VERSIONS`)."""
    if st.root is None or st.transition not in (NAVIGATE_ENTER, NAVIGATE_EXIT):
        return ""
    role = r.role(st.root)
    kind = {R.ROLE_LIST: "list", R.ROLE_GRID: "grid", R.ROLE_STAGGERED_GRID: "grid",
            R.ROLE_PAGER: "pager"}.get(role)
    if kind is None:
        return ""
    name = _collection_name(st.root)
    rows, cols = _counts(st.root)
    prof = profile(version)
    if kind == "pager":
        if st.transition == NAVIGATE_EXIT:
            word = prof["pager_out"] if rows > -1 and cols > -1 else "Out of pager"
            return word + (f" {name}" if name else "")
        axis = ("horizontal" if rows == 1 and cols > 1 else
                "vertical" if cols == 1 and rows > 1 else None)
        return prof["pager_in"].get(axis, "In pager") + (f" {name}" if name else "")
    if st.transition == NAVIGATE_EXIT:
        return f"Out of {kind}" + (f" {name}" if name else "")
    head = f"In {kind}" + (f" {name}" if name else "")
    if kind == "list":
        vertical = rows >= cols
        count = rows if vertical else cols
        if rows > -1 and cols > -1 and count >= 0:
            return f"{head}{sep}{_plural(count, 'item')}"
        return head
    if kind == "grid":
        extra = [x for x in ((_plural(rows, "row") if rows > -1 else ""),
                             (_plural(cols, "column") if cols > -1 else "")) if x]
        return sep.join([head, *extra])
    return head


def _item_transition(r: Rules, st: SpeechState, n: TbNode) -> List[str]:
    """getCollectionItemTransitionDescription (:205): "3 of 20" in a list, "Row 2, Column 3" in
    a grid, with "Heading" when the item (or the focused node) is one."""
    if st.root is None or st.transition not in (NAVIGATE_ENTER, NAVIGATE_INTERIOR) \
            or not any(st.row_col) or st.item is None:
        return []
    role = r.role(st.root)
    rows, cols = _counts(st.root)
    row, col, heading = st.item
    out: List[str] = []
    if role == R.ROLE_GRID:
        if heading or r.is_heading(n):
            out.append("Heading")
        if st.row_col[0] and row != -1:
            out.append(f"Row {row + 1}")
        if st.row_col[1] and col != -1:
            out.append(f"Column {col + 1}")
        return out
    if role == R.ROLE_LIST:
        if heading or r.is_heading(n):
            out.append("Heading")
        index = row if rows >= cols else col
        if index >= 0 and cols > 1 and rows != -1:
            out.append(f"{index + 1} of {cols}")
        elif index >= 0 and rows > 1 and cols != -1:
            out.append(f"{index + 1} of {rows}")
    return out


# --------------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------------


def event_text(node: TbNode) -> str:
    """getEventContentDescriptionOrEventAggregateText for the focus event ``node`` sends: its
    contentDescription, else the populated text of its View subtree joined the way
    AccessibilityEventUtils.getEventAggregateText does (", " after a letter or digit, else " ").
    Only Views: a Compose node's event carries its own text, already in its description."""
    if node.facet not in ("view", "interop"):
        return ""
    if node.content_description:
        return node.content_description
    out = ""
    for t in populated_text(node.raw):
        out = t if not out else out + (", " if out[-1].isalnum() else " ") + t
    return out


def announce(nav_or_rules: Any, node: TbNode, state: Optional[SpeechState] = None, *,
             transitions: bool = True, version: Optional[str] = None,
             keyboard: bool = False) -> Announcement:
    """What TalkBack says when ``node`` takes accessibility focus.

    ``nav_or_rules``: a :class:`~.order.Navigator` or :class:`~.rules.Rules`. ``state`` carries
    the collection / container / window TalkBack was in (updated in place); without it the
    announcement is the one for a first focus. ``transitions=False`` leaves out the collection,
    container and window transitions (the node's own description only). ``version``: a key of
    :data:`VERSIONS` (default :data:`DEFAULT_VERSION`). ``keyboard``: TalkBack is driven by a
    hardware keyboard (an edit box it reaches says "Editing").
    """
    rules: Rules = getattr(nav_or_rules, "rules", nav_or_rules)
    st = state if state is not None else SpeechState()
    if transitions:
        _update_collection(rules, st, node)
    sel = 0
    if st.root is not None and st.item is not None:
        sel = int((st.root.get("collection_info") or {}).get("selection_mode", 0) or 0)
    sep = separator(version)
    comp = _Composer(rules, node, sel, sep, keyboard)

    unl = comp.unlabelled(node)
    segs: List[_Seg] = list(unl) if unl else _mark_descendants(comp.aggregate(node), node)
    if unl or not segs:  # viewAccessibilityFocusedDescription (:207-225): the event's text
        ev = event_text(node)
        if ev:
            segs = [_Seg(ev, node, "event")]
    # Nothing names the node: "Unlabelled", or only role / state words were found. A WebView
    # names itself by its role ("Webview"); its page is not a label it lacks.
    unlabelled = not any(s.kind in _NAMING_KINDS for s in segs) \
        and rules.role(node) != R.ROLE_WEB_VIEW

    item = _item_transition(rules, st, node) if transitions else []
    if item:
        segs.extend(_Seg(t, node, "collection") for t in item)
    elif rules.is_heading(node):
        word = node.get("role_description") or "Heading"
        if not any(sg.text.lower() == word.lower() for sg in segs):  # "Show notes. heading 2"
            segs.append(_Seg(word, node, "heading"))

    if transitions:
        coll = _collection_transition(rules, st, sep, version)
        if coll:
            segs.append(_Seg(coll, st.root or node, "collection"))
        else:
            container = next((a for a in [node, *node.ancestors()] if a.get("container_title")),
                             None)
            title = container.get("container_title") if container is not None else ""
            if title != st.container_title:
                if title:
                    segs.append(_Seg(f"In {title}", container, "container"))
                elif st.container_title:
                    segs.append(_Seg(f"Out of {st.container_title}", node, "container"))
            st.container_title = title
        win = node.window
        if st.window is not None and st.window != win.index and win.title:
            segs.append(_Seg(f"Window {win.title}", node, "window"))
        st.window = win.index
    return Announcement([s for s in segs if s.text], unlabelled, sep)
