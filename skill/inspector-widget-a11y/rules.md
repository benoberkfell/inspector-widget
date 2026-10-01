# Inspector Widget — a11y lint rule reference

The `a11y_lint` MCP tool (and the `a11y-lint` CLI subcommand) run rules R1..R23
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
kept apart: counted in `summary.covered` (not the summary counts), listed in
`covered_findings` and grouped by rule in the brief form's `covered_by_rule` (not
in `findings`; `inspect_node` still shows a node's findings and counts them in
`lint_summary.covered`), a section of their own in the text report, `covered` in
the capture `lint()`, and the overlays do not draw that window over the dialog.

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
  (`minimumInteractiveComponentSize`) and pass. A clickable laid out smaller is
  reported with `bounds_source` "Compose layout size" and `touch_w_dp`/`touch_h_dp`
  in the evidence. It is a `warn` when another clickable (not its own row or
  child) lies inside its widened touch area (`touch_rivals`): the extra area is not
  reserved, so the neighbour can take a touch meant for it. With nothing there
  (`touch_area_clear`) every touch in the area reaches it, so it works as a 48dp
  target and the finding is `info` (Thunderbird's message-row stars, laid out
  48x24dp).
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
- **Capture lint:** `lint()` over a capture does not judge R2 where `render.clipped`
  explains the small size (its visible part is not its size: a 9dp sliver of a row at
  a scroll edge, a toggle half under a bar): R2's own `info` (every small axis
  clipped), or a small axis that the clip's edge cuts; the `render.clipped` issue says
  so. A `warn`/`error` on an axis the clip leaves whole (a 20dp-tall control whose
  width is clipped) stays, as in the live `a11y_lint`.
- **Evidence:** `w_dp`, `h_dp`, `min_dp`, `floor_dp`, `standard`, `clipped_axes`,
  `bounds_source` (and `touch_w_dp`/`touch_h_dp` plus `touch_rivals` or
  `touch_area_clear` for the Compose layout case).

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

### R22 `a11y.toggle.label_contradicts` — a toggle's label names the action
- **Flags:** a checkable control (or a Checkbox/Switch) whose whole name is an undo
  action: "Un…" on bookmark, follow, favorite, like, star, pin, mute, subscribe, save
  and the like, or "Remove (from) bookmarks/favorites/star", with at most two words of
  object after it and no preposition ("Unfollow interest" is one; "Unlock with
  fingerprint" and "Unmute on headset connect" name a feature and are not). Checked,
  TalkBack says "checked. Unbookmark. Check box", which reads as the opposite of what is
  on; not checked, it says only "Unbookmark" and a double-tap does the opposite; either
  way the label flips with the state (Now in Android's bookmark and follow toggles,
  NIA-10). The message quotes what TalkBack says, by its model: the stateDescription
  ("On. Unmute. Switch"), else "checked", and no state for an unchecked node. A
  stateDescription that names the state in words ("Bookmarked. Unbookmark") is no
  contradiction and is not flagged.
- **Severity:** `warn`.
- **Fix:** one stable label that names the item ("Bookmark <title>") on the toggleable,
  so the checked state says on or off; or a plain button (not checkable) whose label
  names the action.
- **Evidence:** `said` (the state TalkBack speaks: "checked", "On", or "" when it says
  none), `checked`, `undo` (the action word), `role`.

---

## Cross-node / structural rules

### R9 `a11y.heading.structure` — section titles, headings missing / empty / duplicated
- **Flags (per window):**
  - A section title that is not a heading (`reason: section_title`): a short,
    text-only stop (1-5 words, no sentence ending, no icon) not marked heading,
    followed in the reading order by two or more stacked, wide controls (actionable
    or checkable rows) taller than it, in a window with two or more such titles
    (sections, not one caption over a list). → `warn` when the title is itself a list
    item (it is counted in the positions TalkBack reads: Thunderbird's settings,
    "Accounts. 2 of 11", TB-11), else `info` (Now in Android's Settings dialog:
    "Theme", "Use Dynamic Color", "Dark mode preference"). Evidence: `rows` (below
    it), `list_item`, `position`.
  - A long screen with no heading. → `info`. "Long" means more than 12 text
    stops, or at least 8 with a scrollable container that can scroll. Text stops
    are the reading order's, counted by what they read: a merged Compose row or a
    View row whose text is in its children counts. A list of three or more items
    counts once (a long inbox or feed is a long list, not a long page); a small
    group (a RadioGroup) counts item by item. Not reported when the window has
    section titles: those findings say where the headings go.
  - A heading with no label. → `warn`.
  - Duplicate adjacent headings. → `info`.
- **Fix:** Compose `Modifier.semantics { heading() }`; View
  `android:accessibilityHeading="true"` (`ViewCompat.setAccessibilityHeading`); a
  header item in a RecyclerView should not be clickable.

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

### R19 `a11y.label.placeholder_token` — a placeholder read aloud
- **Flags:** what a stop reads, as TalkBack reads it (its model's announcement: a
  View's contentDescription silences its text and its children, while a Compose
  merging row's description rides on a synthetic child and its children are still
  read), holding a placeholder: a bracketed snake_case identifier
  (`[attachment_icon]`; Compose inline content's `alternateText`, Thunderbird's
  message rows, TB-7), a resource reference (`@string/x`, `R.string.x`), an unfilled
  format argument (`%s`, `%1$d`), a template placeholder (`{{name}}`, `${name}`), or a
  whole contentDescription that is a resource name (`ic_star_border`,
  `send_button`). Brackets in what users write are not one: a word (`[Draft]`), a
  dotted name or version (`[example.com]`, `[v1.2]`, `[README.md]`), a brand
  (`[iPhone]`); nor is an e-mail address or a user name.
- **Severity:** `warn`, one finding per token per window, on the first stop that reads
  it, with `rows` (how many rows or stops read it).
- **Fix:** give inline content a localized `alternateText`
  (`appendInlineContent(id, alternateText = stringResource(...))`), clear decorative
  inline content from semantics, or replace the row's semantics
  (`Modifier.clearAndSetSemantics { contentDescription = "..." }`; a View row:
  `android:contentDescription`); resolve string resources and format arguments before
  they are set.

### R20 `a11y.label.shared_prefix` — the same child description first in most rows
- **Flags:** a list (three or more items) in which 60% or more of the rows start with
  the same contentDescription of a child that shows no text (an icon): TalkBack reads
  it first on every swipe, before what tells the rows apart (Thunderbird's settings:
  every row with the icon starts "Account settings", TB-11). A shared leading *text*
  is visible content and is not flagged, nor is an icon that tells the rows apart:
  when another row leads with a different description from the same kind of child
  (same class and resource id), "Folder" on four rows and "PDF document" on two.
- **Severity:** `warn`, one finding per list, on its first such row.
- **Fix:** mark the icon decorative: View `android:importantForAccessibility="no"` or
  `contentDescription="@null"`; Compose `contentDescription = null`.
- **Evidence:** `prefix`, `child` and `child_class` (the icon), `rows`, `of`.

### R21 `a11y.label.decorative_merged` — a decorative description in every row
- **Flags:** the same contentDescription of a text-less child merged into 60% or more
  of a list's rows (three or more), not first (that is R20), and decorative: another
  control of the row already says it (Thunderbird's View rows end "Star" while each
  row's own star button says "Add star", TB-12), or it only names a picture (it ends
  in icon, image, arrow, chevron, ...). A description that changes from row to row is
  content.
- **Severity:** `info`.
- **Fix:** `importantForAccessibility="no"` (Compose `contentDescription = null`) on the
  decorative child; the state belongs on the control that changes it.
- **Evidence:** `merged`, `child`, `rows`, `of`, `twin_label` (what the row's own
  control says).

### R23 `a11y.selection.uniform_unselected` — every item says "Not selected"
- **Flags:** a list of five or more items in which every item says a selection
  state (a stateDescription "Not selected" / "Selected": Compose `selected` on a
  non-Tab) and none on screen is selected, while each item holds a control of its own
  named apart from the row, so the row's activation is something else (Now in
  Android's Interests: every row says "Not selected", NIA-9). An onboarding grid whose
  rows toggle the same thing as their own checkbox is not flagged, nor is a list of
  roleless checkable rows with no stateDescription (an unchecked to-do list): TalkBack
  reads those "checked" or nothing, never "Not selected".
- **Severity:** `info`, one finding per list.
- **Fix:** set `selected` only where a selection is shown (`if (highlight) selected =
  isSelected`); View: `setSelected(true)` on the current item only.
- **Evidence:** `state`, `rows` (on screen), `row_count`, `inner_label`.

---

## TalkBack navigation rules (`tb.*`)

The capture-and-walk `lint` (toolset `capture`) also runs the **TalkBack model**
over a stored capture: the stops TalkBack makes, in the order a swipe visits
them, and what it says at each (TalkBack 16.2's traversal rules, calibrated on
TalkBack 17). Its findings are `tb.*` issues on refs, with basis `model`, and
collapse like the a11y ones (`×6 in #message_list cells`). `lint()` reports
`tb.escape`, `tb.window_order`, `tb.wrong_announcement`, `tb.edge_stuck` and
`tb.skipped` with the a11y rules (no false positive on the corpus GOOD variants
or the recorded real apps); `lint(rules=["tb"])` lists every `tb.*` rule,
including the heuristic and opt-in ones (`tb.double_stop`, `tb.ghost_stop`,
`tb.out_of_order`, `tb.boundary_jump`, `tb.custom_action_missing`). Tree
outlines and `lint()` carry the default codes only (`!escape`); the reading view
(`outline(view="reading")`, whose `next` then points at `lint(rules=["tb"])`),
`lint(rules=["tb"])` and `find(issue="tb.<rule>")` carry every code
(`!double_stop`); `node(ref, facets="tb,issues")` explains one. When
`lint(rules=["tb"])` finds nothing, its `next` points at a walk: a trap, a loop,
or focus lost after an action or a list update shows only there.

A real walk (`tb_walk`, device-wide) reports the same codes with basis `walk`
(and `expect` when you passed the order you want), plus the ones only a walk
can see. The walk is ground truth; `model.mismatch` (info) only says where the
model and TalkBack disagreed.

### `tb.skipped` — content TalkBack never reaches  (default)
- **Static:** visible text that is neither a stop nor part of a stop's
  announcement; an ancestor hides it (`importantForAccessibility=
  noHideDescendants`, `hideFromAccessibility`, `clearAndSetSemantics`). The
  reading outline with `include_skipped=true` names the hider (`hidden_by=n40`),
  or the stop whose contentDescription silences a child (`silenced_by=n12`; the
  lint: `no stop says "Wi-Fi": n12 says something else`). An abbreviation of what
  a stop says counts as said ("Aug 5" under "August 5, 2026").
  Not reported: what is hidden for a panel open over it (an open
  `DrawerLayout` drawer hides the content, a modal bottom or side sheet its
  siblings: that keeps focus in the panel), or under a scrim.
- **Walk:** predicted stops a full lap never reached (`diff.skip`,
  `diff.unvisited`), or text on screen no stop read.
- **Fix:** drop the hiding flag (a ComposeView cell with `noHideDescendants`
  hides the whole cell), or fold the text into a stop's label.

### `tb.double_stop` — one item takes two swipes  (opt-in)
- **Static:** a stop with another stop inside it that says the same thing, or
  both clickable (a clickable row and its own Switch / IconButton).
- **Walk:** consecutive stops, one inside the other (`diff.double`).
- **Fix:** Compose `Modifier.toggleable(role = Role.Switch)` / `clickable` on the
  row and `onCheckedChange = null` on the child; View: the child
  `clickable` / `focusable = false`; secondary actions as `customActions`.

### `tb.ghost_stop` — a stop with nothing useful to hear or see  (opt-in)
- **Static:** an unlabelled stop (also `a11y.label.missing`), a focusable
  container whose text children are invisible, a stop of no area or wholly off
  its window (`offscreen`: a pager's off-screen page TalkBack reads anyway, such
  as a WebView's show notes), a sliver at a scroll edge TalkBack cannot scroll in.
- **Walk:** TalkBack lands there.
- **Fix:** label it, or hide it (`clearAndSetSemantics {}` /
  `hideFromAccessibility`; View `importantForAccessibility="no"`, `GONE` rather
  than `alpha = 0`).

### `tb.out_of_order` — read against the visual order  (opt-in, heuristic)
- **Static:** the stops against an XY-cut visual order of each container (two
  columns read zig-zag, `traversalIndex` sorted over the whole screen).
- **Walk:** the same against what TalkBack did (`diff.out_of_order`), or against
  `tb_walk(expect=[refs, selectors or labels])`, which replaces the guess.
- **Fix:** `Modifier.semantics { isTraversalGroup = true }` per column or card,
  `traversalIndex` inside the group; View `accessibilityTraversalBefore/After`
  to a unique, important target; or restructure the layout.

### `tb.boundary_jump` — the order jumps across a View/Compose boundary  (opt-in)
- **Static:** a View subtree read far from its visual place among Compose stops
  (an overlay View beside a full-height ComposeView is read after all of it).
- **Fix:** put the overlay in the layout flow or link it with
  `traversalBefore`; `isTraversalGroup` around the `AndroidView`; make the
  interop root important for accessibility.

### `tb.escape` — focus walks out of a dialog or sheet  (default, error)
- **Static:** stops drawn under a same-window overlay (a `Box` + scrim "dialog",
  a `BottomSheetScaffold` sheet, a custom View overlay) stay reachable. What is
  drawn above what follows the View tree's drawing order: elevation first (the
  capture's View properties), then child order; without properties, an elevated
  kind of View (a FAB, an AppBarLayout, a CardView) drawn earlier is not
  counted under a later scrim.
- **Walk:** focus leaves the overlay for nodes behind it, or reaches a window
  under a modal one (`diff.escape`): one finding per run of steps ("steps 3-11:
  ... read 9 stops behind it"). What is behind the overlay comes from the walk's
  capture, so the walk and the lint agree on it.
- **Fix:** a real `Dialog` / `ModalBottomSheet` (its own window), or hide what it
  covers while it is open (Compose `hideFromAccessibility`, View
  `noHideDescendants`), and give the overlay a `paneTitle`.

### `tb.window_order` — a popup is read last  (default)
- **Static / walk:** a non-focusable `PopupWindow` / dropdown is sorted after the
  content of the window under it.
- **Fix:** a focusable or modal popup (`PopupWindow(focusable = true)`,
  `ListPopupWindow.setModal(true)`), or show it in the layout flow.

### `tb.wrong_announcement` — TalkBack says it wrong  (default)
- **Static:** a merged row reads its texts in composition order, not screen
  order ("$5, Socks"); "N of M" counts an empty header item TalkBack never stops
  on, on screen or scrolled off. `node(ref, facets="tb")` shows every part and
  the ref it came from. A capture taken with TalkBack off has no RecyclerView
  item info (RecyclerView adds it only while a service runs): the model adds the
  positions a TalkBack user hears when the list is at its start or holds every
  item (a grid only when it is clearly a vertical one), and says so in the
  capture's diagnostics when it cannot. The lint line quotes the wrong "N of M"
  itself (`says "2 of 21": counts 1 silent item(s), e.g. n12`).
- **Walk:** the same at the stops TalkBack visited (`diff.speech`).
- **Fix:** compose the texts in reading order, or
  `clearAndSetSemantics { contentDescription = "Socks, $5" }`; keep empty
  header / footer items out of the adapter.

### `tb.edge_stuck` — content a swipe cannot reach  (default)
- **Static:** content past a scroll edge with no scroll action in that
  direction, a pager's other pages with nothing that turns them (no tabs or
  selected page indicator, no "Next" / "Previous page" button, no labelled
  custom action on the pager or above it).
- **Walk:** an edge while the container can still scroll, two presses that move
  nothing (`ended: "stuck"`), hidden items after the last stop (`diff.stuck`).
- **Fix:** scroll semantics and actions (`verticalScroll`, `LazyColumn`,
  RecyclerView, NestedScrollView); page buttons or custom actions for a pager.

### `tb.custom_action_missing` — a gesture-only action  (opt-in)
- **Static:** a swipe-to-dismiss / drag composable (from the slot table: needs
  `capture(slots="enable")`, which resets `remember{}` state) with no labelled
  custom action on the stop TalkBack focuses. An action on a container TalkBack
  never focuses (customActions on the `SwipeToDismissBox` around a row whose
  stop is its Text) is just as unreachable: the finding names that container.
- **Fix:** `Modifier.semantics { customActions = listOf(CustomAccessibilityAction(
  "Delete") { ... }) }` on the node TalkBack stops on (merge the row:
  `semantics(mergeDescendants = true)`); View `ViewCompat.addAccessibilityAction`.

### Walk-only codes
- **`tb.loop`** (error) — focus cycles without reaching an edge (`ended: "loop"`,
  the cycle by ref). Fix: break the `traversalBefore/After` cycle; request input
  focus once (`LaunchedEffect(Unit)`); "load more" as a button.
- **`tb.trap`** — the app takes focus back between presses (`via=stolen`). Fix:
  request focus once, not on every recomposition or timer tick.
- **`tb.revisit`** — a stop read twice in one lap (items re-laid out while
  TalkBack scrolls them). Fix: stable lazy keys, one stop per card.
- **`tb.focus_lost`** — no node holds focus after a press (the focused item was
  disposed while scrolling). Fix: stable keys / `LazyListState`.
- **`tb_scenario` verdicts:** `tb.initial_focus` (after an action focus lands on a
  close button, behind an overlay, nowhere, or stays on the opener: put the
  content first, a window / pane title, no stray `requestFocus()`),
  `tb.restore_failed` (after back it does not return to the item: a `paneTitle`
  per destination, saved list state, stable ids, `setUniqueId`),
  `tb.focus_reset` / `tb.focus_lost` / `tb.focus_drift` (a list update moved,
  dropped or rebound the focused item: `items(key = { it.id })`, DiffUtil with
  stable ids, `supportsChangeAnimations = false`; the `cause` names the rebound
  ref from the before / after captures).

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
