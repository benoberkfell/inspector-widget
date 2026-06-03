# Inspector Widget — a11y lint rule reference

The `a11y_lint` MCP tool (and the `a11y-lint` CLI subcommand) run rules R1..R12
over the app's **Compose semantics tree** (the merged tree TalkBack consumes),
plus one pixel rule sampled from a screenshot. Every finding is
`{rule, severity, node, bounds, bounds_dp, message, evidence}`. This is the
catalogue: what each rule flags, how severity is decided, and the canonical fix.

Fix order: **error → warn → info**. Group by `rule` and apply one fix pattern
across all instances. The finding's own `message` is the targeted remediation
for that specific node — quote and adapt it.

Run a single rule (e.g. for the VERIFY step) by passing its id:
`a11y_lint(serial, package, rules=["a11y.label.missing"])`.

---

## Per-node rules

### `a11y.label.missing` — actionable element has no label  (R1)
- **Flags:** a node that is actionable (`OnClick`/`OnLongClick`/`SetText`/
  `RequestFocus`, or Role ∈ {Button, Switch, Checkbox, RadioButton, Tab,
  DropdownList}) with no `Text`/`ContentDescription`/`EditableText`/
  `StateDescription`, and no labeled descendant that merges up.
- **Severity:** `error`. TalkBack announces it as an unlabeled control.
- **Fix:** add a `contentDescription` (icon-only buttons/images) or visible
  `Text`. Compose: `Modifier.semantics { contentDescription = "…" }` or the
  component's `contentDescription` parameter. View: `android:contentDescription`.

### `a11y.touch_target.small` — touch target below the minimum  (R2)
- **Flags:** an actionable, visible node whose box is `< 48dp` (Material) or
  `< 44dp` (`wcag_mode=true`) in width or height. Skips the WCAG inline-text-link
  exception (no Role, parent is a Text node).
- **Severity:** `error` if both dimensions `< 32dp`; otherwise `warn`.
  (24dp is the WCAG hard floor.)
- **Fix:** enlarge the *target* — `Modifier.sizeIn(minWidth = 48.dp,
  minHeight = 48.dp)` or `Modifier.minimumInteractiveComponentSize()`. Padding
  alone does **not** grow the target.
- **Evidence:** `w_dp`, `h_dp`, `min_dp`, `standard` (material|wcag).

### `a11y.contrast.low` — text contrast below WCAG  (R3, the one pixel rule)
- **Flags:** a visible, enabled `Text`/`EditableText` node whose measured
  foreground-vs-background contrast (k-means split of sampled pixels) is below
  `4.5:1` (normal text) / `3.0:1` (large, height ≥ ~24dp / ~18pt).
- **Severity:** `error` if normal-size and `< 3.0:1`; otherwise `warn`.
- **Needs an image:** auto-skips if no screenshot was sampled
  (`include_contrast=false`).
- **Fix:** darken the text or lighten the background to reach the required ratio.
  Sampled from real rendered pixels, so it reflects the actual theme.
- **Evidence:** `ratio`, `required`, `fg_hex`, `bg_hex`, `sample`
  (component|screenshot), `text_size_class`, `low_confidence` (few strokes
  sampled).

### `a11y.label.redundant` — duplicated or type-word label  (R4)
- **Flags:** (a) `contentDescription` equals the visible `Text` (info), or
  (b) a node *with a Role* whose label contains a type noun (button, image,
  icon, link, tab, checkbox …) the Role already announces (warn).
- **Severity:** `info` (equals-text) / `warn` (type-noun).
- **Fix:** remove the duplicate `contentDescription` (let visible Text speak); or
  drop the type word — say "Submit", not "Submit button" (Role adds "Button").
- **Evidence:** `reason` (equals_text|type_noun), `matched_word`, `label`.

### `a11y.role.missing_on_clickable` — clickable without a Role  (R5)
- **Flags:** a node with `OnClick` but no `Role`.
- **Severity:** `warn`. Without a Role, TalkBack cannot tell users it is
  actionable.
- **Fix:** use a `Button`/`IconButton`, or
  `Modifier.semantics { role = Role.Button }`.

### `a11y.image.no_description` — image with no contentDescription  (R6)
- **Flags:** an image (Role=Image, name ∈ {Image, Icon, AsyncImage}, or
  ImageView/ImageButton) with no `contentDescription`, not marked
  `InvisibleToUser`.
- **Severity:** `error` if the image is also clickable (definite bug); otherwise
  `warn` (null-vs-missing is ambiguous post-hoc).
- **Fix:** set a meaningful `contentDescription`, **or** mark it decorative
  (`contentDescription = null` and not focusable, or
  `Modifier.semantics { invisibleToUser() }`).

### `a11y.state.not_exposed` — stateful-looking control with no state  (R7)
- **Flags:** an actionable node that looks like a toggle (Role=Switch, or label
  contains on/off/enabled/disabled/mute/unmute/toggle) but exposes no
  `ToggleableState`, `Selected`, or `StateDescription`.
- **Severity:** `warn`.
- **Fix:** `Modifier.toggleable(value = …)` / `selectable(…)`, or
  `Modifier.semantics { stateDescription = if (on) "On" else "Off" }`.

### `a11y.node.empty_focusable` — focusable but announces nothing  (R8)
- **Flags:** a focusable node (`OnClick`/`RequestFocus`/`Focused`), visible, with
  no label, no Role, no StateDescription, and no labeled descendant.
- **Severity:** `warn`.
- **Fix:** give it content/label, **or** remove it from the a11y tree with
  `Modifier.clearAndSetSemantics {}` (Compose) / `importantForAccessibility=no`
  (View).

---

## Cross-node / structural rules

### `a11y.heading.structure` — headings missing / empty / duplicated  (R9)
- **Flags:** (a) a long content screen (>12 text nodes, content > 1.5× screen
  height) with **no** headings → `info`; (b) a `Heading` node with an empty
  label → `warn`; (c) duplicate adjacent identical headings → `info`.
- **Fix:** mark section titles with `Modifier.semantics { heading() }`; give every
  heading a non-empty label; remove duplicate stops.

### `a11y.grouping.missing` — separable leaves that should merge  (R10)
- **Flags:** a non-clickable parent containing ≥3 separately-labeled,
  tightly-stacked leaf children (small gaps) — they read as separate focus stops
  when they should be one.
- **Severity:** `info`.
- **Fix:** wrap the row in `Modifier.semantics(mergeDescendants = true) {}` (or a
  single clickable parent) so TalkBack announces them together.

### `a11y.text.fixed_scaling` — text sized in dp/px not sp  (R11)
- **Flags:** a `Text` node whose text-size attribute resolves to `dp`/`px`
  instead of `sp` (best-effort, tree-only).
- **Severity:** `info`.
- **Fix:** size text in `sp` (`fontSize = 16.sp`) so it honors the user's
  font-scale setting.

### `a11y.duplicate.label` — same label on distinct actionable nodes  (R12)
- **Flags:** ≥2 actionable nodes (in different parents — uniform list rows are
  excluded) sharing the same normalized label.
- **Severity:** `info`.
- **Fix:** disambiguate ("Open settings" vs "Open profile") or group them so the
  context is clear.
- **Evidence:** `label`, `node_ids`, `sources`.

---

## Severity → action

| Severity | Meaning | Action |
|----------|---------|--------|
| `error` | Real breakage a screen-reader user hits | Fix first; block on it |
| `warn`  | Likely defect / sub-threshold | Fix next |
| `info`  | Structure / quality nudge | Fix when polishing |

The lint `summary` gives `error`/`warn`/`info` totals and `by_rule` counts —
triage from there. After a fix, re-run `a11y_lint(..., rules=[<that rule>])` and
confirm the count dropped.
