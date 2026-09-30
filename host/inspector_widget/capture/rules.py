"""The issue rule catalog (spec "Capture and Walk", section 3.8).

Every issue a capture carries (``UNode.issues``) names a rule here. The node
stores only ``{id, sev, evidence, conf}``; the message and the fix live once in
this catalog, so a lint response prints them once per rule instead of once per
node.

A rule has:

* ``id``: ``a11y.<group>.<x>`` (accessibility lint) or ``render.<x>`` (render
  signals).
* ``short``: the code used after ``!`` in outline lines: the group for
  ``a11y.<group>.<x>``, and ``x`` for ``render.<x>``. Short codes are not unique
  (``a11y.label.missing`` and ``a11y.label.redundant`` are both ``label``).
* ``sev``: the default (worst) severity. A single issue may be milder; the issue's
  own ``sev`` wins.
* ``msg`` and ``fix``: one line each.
* ``alias``: ``R1``..``R18`` for the accessibility rules (the numbering of
  ``skill/inspector-widget-a11y/rules.md``), and ``atf``: the Accessibility Test
  Framework check name where one exists.
* ``planned``: the rule id is reserved but nothing produces it yet.

``resolve()`` turns user input (ids, aliases, ATF names, short codes, family
prefixes such as ``a11y.``) into rule ids and rejects anything else with
``OpError("bad_args")`` (E10). Pure Python; importing it loads nothing heavy.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .model import OpError

SEV_RANK = {"error": 3, "warn": 2, "info": 1}
SEVERITIES = ("error", "warn", "info")


@dataclass(frozen=True)
class Rule:
    id: str
    short: str
    sev: str
    msg: str
    fix: str | None = None
    alias: str | None = None
    atf: str | None = None
    planned: bool = False

    @property
    def family(self) -> str:
        return self.id.split(".", 1)[0]

    @property
    def label(self) -> str:
        """How the rule is named in follow-up calls: its alias, else its id."""
        return self.alias or self.id

    def to_dict(self) -> dict:
        out = {"rule": self.id, "short": self.short, "sev": self.sev, "msg": self.msg}
        for k in ("fix", "alias", "atf"):
            v = getattr(self, k)
            if v:
                out[k] = v
        if self.planned:
            out["planned"] = True
        return out


def short_code(rule_id: str) -> str:
    """``a11y.<group>.<x>`` -> group; ``render.<x>`` -> x; anything else -> its last part."""
    parts = rule_id.split(".")
    if parts[0] == "a11y" and len(parts) >= 3:
        return parts[1]
    if parts[0] == "render" and len(parts) >= 2:
        return parts[1]
    return parts[-1]


def _r(rule_id: str, sev: str, msg: str, fix: str | None = None, alias: str | None = None,
       atf: str | None = None, planned: bool = False) -> Rule:
    return Rule(rule_id, short_code(rule_id), sev, msg, fix, alias, atf, planned)


_CATALOG: tuple[Rule, ...] = (
    # Accessibility lint, R1..R12 (inspector_widget.a11y_lint), R13..R18 (the unified lint).
    _r("a11y.label.missing", "error",
       "Actionable node has no accessible name; TalkBack reads only its role.",
       "Add visible text or Modifier.semantics { contentDescription = \"…\" }",
       "R1", "SpeakableTextPresent"),
    _r("a11y.touch_target.small", "warn", "Touch target under 48dp.",
       "Modifier.minimumInteractiveComponentSize() or sizeIn(48.dp, 48.dp)",
       "R2", "TouchTargetSize"),
    _r("a11y.contrast.low", "error", "Text contrast below WCAG 1.4.3 (4.5:1; 3:1 large text).",
       "Darken the text or lighten the background", "R3", "TextContrast"),
    _r("a11y.label.redundant", "warn", "Label repeats the visible text or the announced role.",
       "Drop the duplicate contentDescription or the type word", "R4", "RedundantDescription"),
    _r("a11y.role.missing_on_clickable", "warn",
       "Clickable node has no Role, so TalkBack cannot announce what it does.",
       "Modifier.semantics { role = Role.Button }, or Button/ListItem(onClick)", "R5"),
    _r("a11y.image.no_description", "warn", "Image has no contentDescription.",
       "Set contentDescription, or contentDescription = null if decorative",
       "R6", "ImageContentDescription"),
    _r("a11y.state.not_exposed", "warn", "Looks stateful but exposes no state.",
       "Modifier.toggleable(…) or stateDescription", "R7"),
    _r("a11y.node.empty_focusable", "warn", "Takes focus but announces nothing.",
       "Give it a label, or clearAndSetSemantics {} to remove it", "R8"),
    _r("a11y.heading.structure", "warn", "Headings missing, empty or duplicated.",
       "Modifier.semantics { heading() } on section titles", "R9"),
    _r("a11y.grouping.missing", "info", "Related text reads as separate focus stops.",
       "Modifier.semantics(mergeDescendants = true) {} on the row", "R10"),
    _r("a11y.text.fixed_scaling", "warn", "Text size ignores the user's font scale.",
       "Use sp for text sizes", "R11", "TextSize"),
    _r("a11y.duplicate.label", "warn", "Distinct actionable nodes share one label.",
       "Make each label say what the action does", "R12", "DuplicateSpeakableText"),
    _r("a11y.clickable.duplicate_bounds", "warn", "Clickable nodes share identical bounds.",
       "Merge them or remove the inner click handler", "R13", "DuplicateClickableBounds"),
    _r("a11y.editable.content_description", "error", "Editable field has a contentDescription.",
       "Use a label or hint instead", "R14", "EditableContentDesc"),
    _r("a11y.link.purpose_unclear", "warn", "Link or action text does not say where it goes.",
       "Describe the destination (\"Read the privacy policy\")", "R15", "LinkPurposeUnclear"),
    _r("a11y.form.label_missing", "error", "Form field has no label.",
       "Add a visible label linked by labelFor or semantics", "R16"),
    _r("a11y.traversal.order", "error", "Traversal order constraints form a cycle or dangle.",
       "Fix traversalBefore/After or isTraversalGroup", "R17", "TraversalOrder"),
    _r("a11y.text.too_small", "warn", "Text renders below 12sp.",
       "Use at least 12sp", "R18"),
    # Render signals (capture/analyzers.py).
    _r("render.clipped", "warn", "Only part of the node is visible.",
       "Scroll it into view, or give the parent room (height, maxLines)"),
    _r("render.hidden", "info", "Present but not drawn (visibility, alpha 0, or not visible to "
       "accessibility).", "Check visibility, alpha and the enclosing window"),
    _r("render.offscreen", "info", "Laid out outside its window or the screen.",
       "Check offsets, translation and window placement"),
    _r("render.zero_size", "info", "Has content or actions but zero width or height.",
       "Check its size modifiers or layout params"),
    _r("render.text_overflow", "warn", "Text is cut off or ellipsized.",
       "Allow more lines or room", planned=True),
    _r("render.covered", "warn", "Drawn under another node.", planned=True),
    _r("render.drawn_mismatch", "info", "Drawn pixels differ from the declared bounds.",
       planned=True),
)

#: rule id -> Rule
RULES: dict[str, Rule] = {r.id: r for r in _CATALOG}
#: "R5" -> rule id (keys upper case; lookups are case-insensitive)
ALIASES: dict[str, str] = {r.alias: r.id for r in _CATALOG if r.alias}
#: ATF check name -> rule id
ATF_NAMES: dict[str, str] = {r.atf: r.id for r in _CATALOG if r.atf}
FAMILIES = ("a11y", "render")

_LOOKUP: dict[str, str] = {}
for _rule in _CATALOG:
    _LOOKUP[_rule.id.lower()] = _rule.id
    if _rule.alias:
        _LOOKUP[_rule.alias.lower()] = _rule.id
    if _rule.atf:
        _LOOKUP[_rule.atf.lower()] = _rule.id
        _LOOKUP[(_rule.atf + "check").lower()] = _rule.id
_BY_SHORT: dict[str, list[str]] = {}
for _rule in _CATALOG:
    _BY_SHORT.setdefault(_rule.short, []).append(_rule.id)


def get(rule_id: str, sev: str | None = None, msg: str | None = None) -> Rule:
    """The catalog entry for ``rule_id``; an id the catalog does not know (a newer
    lint's rule) gets a generic entry built from the finding, so no finding is lost."""
    rule = RULES.get(rule_id)
    if rule is not None:
        return rule
    text = (msg or rule_id).split(". ")[0][:100]
    return Rule(rule_id, short_code(rule_id), sev if sev in SEV_RANK else "warn", text)


def short(rule_id: str) -> str:
    rule = RULES.get(rule_id)
    return rule.short if rule else short_code(rule_id)


def is_known(rule_id: str) -> bool:
    return rule_id in RULES


def resolve(specs: Iterable[str] | str | None) -> list[str] | None:
    """Rule ids selected by user input; None (or empty) means every rule.

    Accepted spellings, case-insensitive: the id (``a11y.role.missing_on_clickable``),
    the alias (``R5``), the ATF check name (``TouchTargetSize``), a short code
    (``clipped``, ``role``; every rule with that code) and a family prefix
    (``a11y.``, ``render.``, ``a11y.label.``). Comma-separated strings are split.
    Anything else raises ``OpError("bad_args")`` naming the valid rules.
    """
    if specs is None:
        return None
    if isinstance(specs, str):
        specs = [specs]
    out: list[str] = []
    unknown: list[str] = []
    for spec in specs:
        for part in str(spec).split(","):
            p = part.strip()
            if not p:
                continue
            key = p.lower()
            ids: list[str] = []
            if key in _LOOKUP:
                ids = [_LOOKUP[key]]
            elif key in _BY_SHORT:
                ids = list(_BY_SHORT[key])
            elif key.endswith(".") and any(r.id.startswith(key) for r in _CATALOG):
                ids = [r.id for r in _CATALOG if r.id.startswith(key)]
            if ids:
                out.extend(i for i in ids if i not in out)
            else:
                unknown.append(p)
    if unknown:
        valid = " ".join(f"{r.alias}={r.id}" for r in _CATALOG if r.alias)
        raise OpError(
            "bad_args", f"unknown rule {', '.join(repr(u) for u in unknown)}",
            hint=f"Use a rule id, alias, short code or family (a11y., render.). {valid} "
                 "render.clipped render.hidden render.offscreen render.zero_size",
        )
    return out or None


def worst(sevs: Iterable[str]) -> str | None:
    best = None
    for s in sevs:
        if best is None or SEV_RANK.get(s, 0) > SEV_RANK.get(best, 0):
            best = s
    return best


def at_least(sev: str, minimum: str) -> bool:
    """``sev`` is ``minimum`` or more severe (error > warn > info)."""
    return SEV_RANK.get(sev, 0) >= SEV_RANK.get(minimum, 0)


__all__ = [
    "ALIASES", "ATF_NAMES", "FAMILIES", "RULES", "SEVERITIES", "SEV_RANK", "Rule",
    "at_least", "get", "is_known", "resolve", "short", "short_code", "worst",
]
