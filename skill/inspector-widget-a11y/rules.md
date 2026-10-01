# Inspector Widget — a11y lint rule reference

The `a11y_lint` MCP tool (and the `a11y-lint` CLI subcommand) run rules R1..R18
over the app's **unified accessibility tree**: every node TalkBack sees, classic
Views and Compose alike, in one pass. RecyclerView cells that are ComposeViews,
AndroidViews inside Compose, Fragments, dialogs and popups (each window is
linted) are all covered. The Compose semantics tree only adds detail (exact
`Role`, `testTag`). One rule (contrast) samples a screenshot of each window.

The rules judge what TalkBack reads. A View that is not important for
accessibility (left at `importantForAccessibility="auto"` and not clickable,
focusable, labelled or otherwise important: layout containers, decorative icons)
is never seen by TalkBack, so no rule reports it; its children still count. Focus
stops (R9, R10) are the reading order's stops. A finding on a window under an
open modal dialog carries `window.covered_by` (the dialog's `root_view_id`):
still a defect, but TalkBack cannot reach it until the dialog closes. Those are
kept apart: `covered_findings` (not `findings`), `summary.covered` (not the
summary counts), a section of their own in the text report, and the overlay does
not draw that window over the dialog.

Every finding looks like:

```json
{
  "rule": "a11y.touch_target.small", "alias": "R2", "severity": "warn",
  "node_key": "compose:1234:42",
  "node": {"id": 5299989643306, "key": "compose:1234:42", "name": "IconButton",
           "role": "Button", "label": "Favorite", "class_name": "android.widget.Button",
           "host_view_id": 1234, "virtual_id": 42, "test_tag": "fav", "source": null},
  "bounds": {"x": 24, "y": 880, "w": 96, "h": 96},
  "bounds_dp": {"x": 9.1, "y": 335.2, "w": 36.6, "h": 36.6},
  "window": {"index": 0, "root_view_id": 77},
  "collection": {"container": "view:900", "row": "view:913", "row_index": 3, "column_index": 0},
  "message": "Touch target is 36.6x36.6dp (< 48dp) ...",
  "evidence": {"w_dp": 36.6, "h_dp": 36.6, "min_dp": 48, "standard": "material"}
}
```

- **`node_key`** is the selector for `inspect_node`: `view:<uniqueDrawingId>` for a
  View, `compose:<AndroidComposeView id>:<semantics id>` for a Compose node
  (`virtual:<host>:<id>` for other virtual providers such as WebView). Compose
  keys are valid until the next recomposition re-mints semantics ids (the
  response's `generation` changes then); `inspect_node` re-resolves a key the lint
  handed out when the match is unambiguous, otherwise re-lint. `node.id` is the a11y node id
  (`host_view_id << 32 ^ virtual_id`), the same id the a11y dump and overlay use.
- **`bounds`** are the accessibility (touch) bounds in screen px; `bounds_dp` uses
  the device density.
- **`collection`** is set when the node sits in a RecyclerView / LazyColumn /
  ListView row: which list, which row.
- The response also has a **`summary`** (counts by severity and `by_rule`, plus
  `rule_errors`), **`diagnostics`** (a rule that crashed, a window that could not
  be captured, missing text-size info, a pre-fix agent with non-unique ids) and
  **`stats`** (node, window and joined-Compose counts, windows sampled for
  contrast).

Fix order: **error → warn → info**. Group by `rule` and apply one fix pattern
across all instances. The finding's `message` is the remediation for that node,
worded for a View or for Compose depending on the node. Quote it and adapt it.

Select rules with `rules=[...]` (MCP) or `--rule` (CLI). Each rule can be named
by its id (`a11y.label.missing`), its alias (`R1`) or its ATF check name
(`SpeakableTextPresent`). An unknown name is an error that lists the valid ones.
For example, to re-check one rule after a fix:
`a11y_lint(serial, package, rules=["R2"])`.

How labels are computed: like TalkBack, a node's accessible name is its
`contentDescription`, else its text/stateDescription, else the text of its
**non-focusable** descendants (a Compose `IconButton` is named by its `Icon`'s
description), else its `labeledBy` target. A descendant that takes its own focus
(a separate button) never names its parent. Compose puts a merging node's role on
a synthetic child (class name, or roleDescription for Tab and Switch) and its
description on another; both are folded into the node.

---

## Per-node rules

### R1 `a11y.label.missing` — actionable element has no accessible name  (ATF SpeakableTextPresent)
- **Flags:** a visible clickable / long-clickable node (not a text field, see R16)
  whose computed name is empty. This includes an unlabeled `ImageButton`, an icon-only
  Compose button whose `Icon` has `contentDescription = null`, a Checkbox with no
  label, and a clickable card whose only labelled child is a *separate* button
  (TalkBack never stops on such a card, so its click cannot be reached).
- **Not flagged:** a list, or a long-click-only container, that TalkBack never
  stops on (TalkBack's shouldFocusNode: nothing of its own to say, its children
  are the stops; e.g. a long-clickable RecyclerView), and a WebView (TalkBack
  calls it "Webview"; its page is its content).
- **Severity:** `error`. TalkBack announces only the role ("button"). `info` when
  the node is clipped (see R2): its label may be in the part scrolled out of view
  (Compose drops the children a list scrolled away); scroll it in and re-lint.
- **Fix:** View: `android:contentDescription` (icon-only) or visible `android:text`;
  for a CheckBox/Switch give it text or point its label at it with `android:labelFor`.
  Compose: pass `contentDescription` to the `Icon`/`Image`, or
  `Modifier.semantics { contentDescription = "…" }`; for a toggle, make the Row that
  holds its label `toggleable` and pass `onCheckedChange = null` to the control.

### R2 `a11y.touch_target.small` — touch target below the minimum  (ATF TouchTargetSize)
- **Flags:** a visible, enabled, actionable node whose **accessibility (touch)
  bounds** are `< 48dp` (Material) or `< 44dp` (`wcag_mode`) in width or height,
  with 1px of slack for rounding (a 48dp target measures 116-118px at 2.4375x).
  The WCAG inline-link exception (an unroled link inside a run of text, or a web
  link sharing its line with text) is skipped.
- **Compose:** Compose widens the touch bounds of *every* clickable to 48dp, so they
  alone cannot tell a stock M3 control from a `Modifier.size(24.dp).clickable`. The
  agent also reports each Compose node's layout size (its LayoutNode, `layout_size`
  in the a11y dump). Stock M3 Checkbox/IconButton/Switch reserve 48dp there
  (`minimumInteractiveComponentSize`) and pass; a clickable laid out smaller is a
  `warn` with `bounds_source` "Compose layout size" and `touch_w_dp`/`touch_h_dp`
  in the evidence: the extra touch area is not reserved, so a neighbour or a clip
  can take it and the visible control stays small.
- **Clipping (on evidence only):** a dimension is clipped where the node touches
  an edge of a scroll container that can still scroll past that edge (its scroll
  actions say so; a container with none counts for every edge; a pager's page at
  rest fills it and is whole), where its bounds are reported clipped, or where
  Compose laid it out larger than it shows. The window's edge alone is no
  evidence: a 40dp overflow button flush with the screen edge is a real 40dp
  target. If only clipped dimensions are small the finding is `info` ("scroll it
  into view and re-lint"). Otherwise the clipped dimensions are not used to
  decide severity.
- **Severity:** `error` if an unclipped dimension is `< 24dp` (the WCAG 2.5.8
  floor); otherwise `warn`.
- **Fix:** Compose: `Modifier.minimumInteractiveComponentSize()` or
  `sizeIn(minWidth = 48.dp, minHeight = 48.dp)`. Padding grows the target only when
  it is applied *after* (inside) `clickable`. View: `android:minWidth/minHeight` or
  padding on the clickable view itself. A `TouchDelegate` helps users but is not
  reflected in accessibility bounds, so this rule still reports it. Web content
  (a WebView's page): CSS `min-width`/`min-height` or padding on the link/button.
  Every rule gives web content HTML/CSS advice, never Compose's.
- **Evidence:** `w_dp`, `h_dp`, `min_dp`, `floor_dp`, `standard`, `clipped_axes`,
  `bounds_source` (and `touch_w_dp`/`touch_h_dp` for the Compose layout case).

### R3 `a11y.contrast.low` — text contrast below WCAG 1.4.3  (ATF TextContrast; the one pixel rule)
- **Flags:** a visible, enabled, non-password node with its own text whose
  measured contrast is below `4.5:1` (normal) or `3.0:1` (large text, ≥ 24dp ≈
  18pt).
- **Not flagged:** text of an inactive component (WCAG exempts it): the node is
  disabled, or its nearest actionable / focusable ancestor is. Compose puts a
  disabled Button's or TextField's label on a child Text that reports enabled.
- **Measurement:** the background is the dominant colour (the mode of the node's
  edge pixels, else of the whole crop). The foreground is the most frequent colour
  among the most-contrasting quarter of the remaining "ink" pixels. Anti-aliased
  edge pixels fall between the two and never set either colour, so `#767676` on
  white (4.54:1) passes. Each window (activity, dialog, popup) is sampled from its
  own screenshot. Large sampling areas are downsampled.
- **Text size** comes from the View's ExtraRenderingInfo (`text_size_px`), not the
  node height. Compose text never reports it, so `text_size_class` is `unknown`
  and the 4.5:1 threshold is applied. A ratio between 3.0 and 4.5 with unknown
  size is then a `warn`, not an `error`.
- **Severity:** `error` when the text fails for its (known) size or is below
  `3.0:1`; `warn` for unknown size in 3.0–4.5 or when the sample is low confidence
  (busy background or very few ink pixels).
- **Needs an image:** skipped with `include_contrast=false` / `--no-contrast`.
- **Fix:** darken the text or lighten the background.
- **Evidence:** `ratio`, `required`, `fg_hex`, `bg_hex`, `sample`
  (`window:<root_view_id>`, `screenshot` or `component`), `scale`, `bg_fraction`, `ink_fraction`,
  `text_size_class` (`normal|large|unknown`), `text_size_dp`, `low_confidence`.

### R4 `a11y.label.redundant` — redundant contentDescription  (ATF RedundantDescription)
- **Flags:** only a **contentDescription** (never visible text), with punctuation
  normalised, judged against the role TalkBack announces with it: the node's own
  when it is its own focus stop, else its focus owner's.
  (a) it contains a word naming that role ("Submit button." on a Button, "Delete
  button" on the Icon inside an IconButton). → `warn`.
  (b) it contains a state word ("checked", "selected") on a checkable or
  selectable node. → `info`.
  (c) it equals the node's own visible text. → `info`.
  "Upload image" on a Button is **not** flagged, because "image" is not a
  Button's role word. Nor is it on the `Icon` inside an `IconButton`: Compose
  keeps `Role.Image` in the semantics but does not announce it for an image merged
  into a larger control (TalkBack says "Upload image, button").
- **Fix:** describe the purpose only. Use `stateDescription` for custom state
  text. Drop a contentDescription that repeats the text.

### R5 `a11y.role.missing_on_clickable` — clickable element exposes no role
- **Flags:** a clickable, labelled node with no role (class, Compose `Role`,
  or roleDescription, including the one Compose puts on its synthetic role
  child). Exempt: text fields, stateful nodes, list rows (a clickable
  row of a RecyclerView/LazyColumn, or a node filling one), and nodes whose
  non-focusable child carries the role.
- **Severity:** `info` when the node has visible text; `warn` when it is
  icon-only. TalkBack still says "double-tap to activate", but it cannot say what
  kind of control this is, and the control is missing from control navigation.
- **Fix:** Compose: `Modifier.clickable(role = Role.Button)` or
  `semantics { role = Role.Button }`. View: use a Button, or set the class name or
  roleDescription through an `AccessibilityDelegateCompat`.

### R6 `a11y.image.no_description` — image with no description  (ATF ImageContentDescription)
- **Flags:** a visible, non-actionable image (ImageView class or Compose
  `Role.Image`) with no contentDescription or labeledBy that is not marked
  decorative and is not part of a labelled control. An *actionable* image with no
  name is R1 (`error`).
- **Not flagged:** an image TalkBack never sees: `importantForAccessibility="no"`,
  inside a `noHideDescendants` subtree, or a plain `ImageView` left at `auto` with
  no description that is not clickable or focusable (not important for
  accessibility; ATF skips it too). A Compose `Image(contentDescription = null)`
  emits no semantics, so it never reaches the tree. So R6 fires on an image that
  TalkBack does stop on (`importantForAccessibility="yes"`, focusable, or a
  Compose image with a node of its own) and that has nothing to say.
- **Severity:** `warn`.
- **Fix:** add a meaningful description, or mark it decorative
  (`android:importantForAccessibility="no"` / `contentDescription = null`, or
  `Modifier.semantics { hideFromAccessibility() }`, which replaces the deprecated
  `invisibleToUser()` in Compose 1.8+).

### R7 `a11y.state.not_exposed` — stateful-looking control with no state
- **Flags:** an actionable node with no checkable, checked, selected,
  stateDescription or range info that:
  - has a stateful role (Switch, Checkbox, RadioButton, Tab). → `warn`. A Tab's
    state is its selection: an unselected tab is fine when it carries
    CollectionItemInfo or another tab in its window is selected (Material
    TabLayout, BottomNavigationView, NavigationRailView, Compose `Tab` and
    `NavigationDrawerItem`, whose items each sit in their own lazy-list item).
  - has a label ending in a state word ("Wi-Fi off", "Sync enabled"), excluding
    phrasal verbs such as "Sign off" and "Log on". → `warn`.
  - has a label containing "toggle". → `warn`.
  - is icon-only and named like a toggle ("Favorite", "Like", "Mute",
    "Bookmark"), the usual icon-swap toggle. → `info`, "if this toggles".
- **Fix:** Compose: `Modifier.toggleable(value = …)` / `selectable(…)`, or
  `semantics { stateDescription = … }`. View: a CompoundButton, or
  `ViewCompat.setStateDescription`. For a tab: mark the selected one selected
  (`setSelected(true)`; `Tab(selected = …)` / `selectable(role = Role.Tab)`).

### R8 `a11y.node.empty_focusable` — focusable but announces nothing
- **Flags:** a visible node that is focusable or screen-reader-focusable but not
  actionable or editable (those are R1/R16). It has no name, role, range or hint,
  and no focusable descendants. Scroll containers are exempt.
- **Severity:** `warn`.
- **Fix:** give it content, or remove it from the tree
  (`Modifier.clearAndSetSemantics {}` / `importantForAccessibility="no"`).

### R11 `a11y.text.fixed_scaling` — text size ignores font scale  (ATF TextSize)
- **Flags:** a View text node whose ExtraRenderingInfo text unit is px, dp, pt,
  in or mm (not sp). Requires `include_rendering_info`, which the lint requests
  by default. Compose text does not report a size, so a diagnostic says when
  this rule could not run.
- **Severity:** `warn`.
- **Fix:** size text in `sp`.
- **Evidence:** `unit`, `text_size_px`, `text_size_dp`, `font_scale`.

### R14 `a11y.editable.content_description` — text field has a contentDescription  (ATF EditableContentDesc)
- **Flags:** an editable node with a contentDescription. TalkBack reads it only
  while the field is empty, in place of the hint or label, and drops it once text
  is entered, so the field loses its name when the user reviews what they typed.
- **Severity:** `error`; `info` for a Compose field whose description is
  "Search": Material3's `SearchBarDefaults.InputField` sets it itself, so there is
  nothing to change unless you set it.
- **Fix:** remove it. Label the field with a hint, `labelFor`, TextInputLayout, or
  the Compose TextField `label` / `placeholder`.

### R15 `a11y.link.purpose_unclear` — link/action text does not describe its purpose  (ATF LinkPurposeUnclear)
- **Flags:** link spans (from the compat span extras) or link-role nodes whose
  text is vague ("click here", "here", "more", "read more", "learn more", "link").
  → `warn`. An actionable non-link with such a label. → `info`, except inside a
  list row, where the row gives a per-row action ("More info") its purpose in
  context.
- **Fix:** name the destination ("Read the pricing FAQ"), or give it a
  descriptive contentDescription. Plain `ClickableSpan`s inside a TextView are
  only visible when the agent exports span extras.

### R16 `a11y.form.label_missing` — form field has no label
- **Flags:** an editable node with no text, hint, contentDescription, labeledBy,
  incoming `labelFor`, or label child (a Compose TextField's `label` Text).
- **Severity:** `error`. TalkBack announces only "edit box".
- **Fix:** View: `android:hint`, `android:labelFor` on the visible label, or
  TextInputLayout. Compose: `label = { Text(…) }`.

### R18 `a11y.text.too_small` — tiny text
- **Flags:** a View text node below 12sp at the default font scale. The size is
  computed from the rendered size and `font_scale`: sp text is divided by the font
  scale, dp/px text is taken as rendered.
- **Severity:** `warn`.
- **Fix:** at least 12sp (14–16sp for body text).

---

## Cross-node / structural rules

### R9 `a11y.heading.structure` — headings missing / empty / duplicated
- **Flags (per window):**
  - A long screen with no heading. → `info`. "Long" means more than 12 text
    stops, or at least 8 with a scrollable container that can scroll. Text stops
    are the reading order's, so the texts inside a (focusable) ScrollView or
    RecyclerView count.
  - A heading with no label. → `warn`.
  - Duplicate adjacent headings. → `info`.
- **Fix:** Compose `Modifier.semantics { heading() }`; View
  `android:accessibilityHeading="true"`.

### R10 `a11y.grouping.missing` — related text read as separate stops
- **Flags:** a non-focusable container with short, one- or two-line text leaves
  (≤ 60 chars, ≤ 40dp tall) that are each their own TalkBack stop, stacked
  vertically with ≤ 8dp gaps and overlapping horizontally. It needs at least 2
  leaves in a list row, or at least 3 elsewhere. Paragraphs and horizontal chip
  rows are not flagged. The container may be a View that is not important for
  accessibility (a plain LinearLayout), and it may sit inside a focusable
  ScrollView or RecyclerView.
- **Severity:** `info`.
- **Fix:** Compose `Modifier.semantics(mergeDescendants = true) {}`. View: make the
  container focusable / `screenReaderFocusable`.

### R12 `a11y.duplicate.label` — distinct actionable elements share one label  (ATF DuplicateSpeakableText)
- **Flags:** actionable nodes announced with the same label. Repeats in
  **different rows of the same list** (RecyclerView, LazyColumn, ListView, a
  collection) are fine: "Delete" in every row is expected. Repeats under the
  **same parent** are `warn`. Other repeats (different cards, not a list) are `info`.
- **Fix:** disambiguate ("Delete photo" vs "Delete album"), or give context via
  the row.
- **Evidence:** `label`, `same_parent`, `duplicates` (node keys), `node_ids`.

### R13 `a11y.clickable.duplicate_bounds` — clickable elements with identical bounds  (ATF DuplicateClickableBounds)
- **Flags:** a second clickable node in the same window with exactly the same
  bounds as another, usually a clickable wrapper around a clickable child.
  TalkBack focuses both and a double-tap activates only one.
- **Severity:** `warn`.
- **Fix:** keep one of them clickable.

### R17 `a11y.traversal.order` — traversal constraints  (ATF TraversalOrder)
- **Flags:**
  - `traversalBefore` / `traversalAfter` links that form a cycle. → `error`. The
    reading order becomes undefined.
  - A link to a node that is not in the tree. → `info`.
- **Fix:** remove one constraint in the cycle; point links at nodes that exist.

---

## Severity → action

| Severity | Meaning | Action |
|----------|---------|--------|
| `error` | Real breakage a screen-reader user hits | Fix first; block on it |
| `warn`  | Likely defect / sub-threshold | Fix next |
| `info`  | Structure / quality nudge, or a result to confirm (clipped, "if it toggles") | Fix when polishing |

Triage from the `summary`. Read `diagnostics` before trusting a clean result:
it says when contrast could not sample a window, when text size was
unavailable, or when a rule crashed. After a fix, re-run with just that rule
(`rules=["R1"]`) and confirm the count dropped.
