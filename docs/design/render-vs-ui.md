# Render-vs-UI design (from the ui-state-gaps auditor, parts 1-5; Compose signatures verified with javap on 1.7.0 + 1.12.1)
Jars/protos extracted under scratchpad/ui-state-gaps/. NOTHING was verified on a device.

## Model: the "render facet" of a retained capture
One on-device DumpRender pass (same main-thread hop as the view/semantics/a11y facets) + one PixelCopy PER WINDOW (+ optional SKP). The host stores it under capture_id; every render tool is a HOST-SIDE query. Only a new capture, RenderState (settle probe) and PerformAction (interact) touch the device. Borrow AS's generation / "unchanged" short-circuit: if_changed_since=change_count → unchanged=true.

## Proto sketch (additive)
LayerInfo{layer_id, alpha, scale_x/y, rotation_x/y/z, translation_x/y, clip, shadow_elevation, outline(str), has_render_effect, compositing_strategy, blend_mode}
ModifierBox{desc(str), box Rect}; Constraints{min_w,max_w,min_h,max_h (-1 = inf)}
TextLayoutInfo{full_text, visible_text (text[0:getLineEnd(last, visibleEnd=true)]), line_count, max_lines, overflow(1 Clip 2 Ellipsis 3 Visible 4 StartEllipsis 5 MiddleEllipsis), soft_wrap, did_overflow_width/height, has_visual_overflow, ellipsized, color_argb, font_size_px, font_weight, layout_w/h, max_width_constraint}
LayoutNodeInfo{semantics_id (== ComposeNode SEMANTICS id == A11yNode.virtual_id), parent_semantics_id, draw_index (pre-order over zSortedChildren), composable(str; only if the slot table is already populated), source(str), call_path(str), declared Rect (positionInWindow + size, unclipped), render Quad (localToWindow corners, only when transformed), visible Rect (boundsInWindow; 0 = fully clipped), clip_ancestor, flags{PLACED 1, ATTACHED 2, DEACTIVATED 4, TRANSPARENT 8, HAS_DRAW 16, HAS_LAYER 32, CLIPS_CHILDREN 64, HAS_SEMANTICS 128, INTEROP_VIEW 256}, z_index, layers[], modifiers[], text, interop_view_id, constraints, first_baseline, touch_bounds}
ThemeSnapshot{color_scheme[NamedColor], density, font_scale, layout_direction, ui_mode, density_dpi, screen_w/h_dp, locale, insets[type, l/t/r/b, visible]}
RenderWindow{root_view_id, compose_view_id, frame Rect (window→screen), z_order, window_type, title, nodes[] (flat), screenshot (this root's own PixelCopy)}
DumpRenderCommand{root_view_id (0 = all), include_text/layers/modifiers/theme/screenshot, scale, include_skp, use_slot_table_if_present (NEVER hot-reloads), if_changed_since}
DumpRenderResponse{windows[], theme, strings, change_count, frame_stable, has_pending_work, skp, diagnostics, unchanged}
RenderStateCommand{} → {change_count, has_pending_work, tree_hash}
PerformActionCommand{semantics_id, action (OnClick|OnLongClick|ScrollBy|SetText), arg_x/y, text} → {performed, error}
Also: DumpComposeResponse.Window gains `Rect frame`; slot ComposeNodes get id = semanticsId via NodeGroup.getNode(); zero-size bounds are emitted.

## Extraction (one main-thread hop)
1 windows from RootsDetector (frame, z, WM.LayoutParams type/title). 2 AndroidComposeView.getRoot() → recurse getZSortedChildren() (public in both versions), draw_index pre-order. 3 LayoutInfo: getSemanticsId/isPlaced/isAttached/isDeactivated/getWidth/Height; c = getCoordinates(); declared = positionInWindow(c) + size; visible = boundsInWindow(c); unclipped = findRootCoordinates(c).localBoundingBoxOf(c, false); Quad via localToWindow (prefix-resolved mangled names); TRANSPARENT = NodeCoordinator.isTransparent() (public; alpha 0 anywhere up the chain). 4 layers: ModifierInfo.getExtra() is GraphicLayerInfo → layerId; the private field `graphicsLayer` on GraphicsLayerOwnerLayer (same name in 1.7/1.12) → public GraphicsLayer getters; fallback RenderNodeLayer via DeviceRenderNode getters. clip_ancestor in a second pass. 5 HAS_DRAW = getNodes$*().has$*(Nodes.getDraw()==4). 6 text: unmerged SemanticsOwner walk; the GetTextLayoutResult action ((Function1) action.getAction()).invoke(ArrayList()) → element 0. 7 slot join ONLY if already populated: NodeGroup.getNode() → semanticsId → nearest named CallGroup (name + SourceLocation). 8 theme: root.getCompositionLocalMap().get(ColorSchemeKt.getLocalColorScheme()) → get*-0d7_KjU getters; LocalDensity/LocalConfiguration; getRootWindowInsets. 9 consistency: RecomposerInfo.changeCount sum + tree hash before/after PixelCopy → frame_stable.
Mangling: internal names changed from `$ui_release` (1.7) to `$ui` (1.12), so resolve by prefix. ComposerImpl is split into GapComposer/LinkComposer in 1.12. The CompositionObserver API differs by version.

## Host analysis (inspector_widget/render_check.py)
Normalize to screen px via RenderWindow.frame. Status per node (first match wins; keep all reasons): not_attached, deactivated, not_placed, zero_size, transparent (+ ancestor), offscreen, clipped_full/partial (+ clip ancestor composable/source), occluded_full/partial, ok.
Occlusion: geometric (later draw order / later windows, non-ancestor, not TRANSPARENT, HAS_DRAW, intersecting); visible_fraction; pixel confirmation vs the SKP layer or expected ink.
Ink: border-dominant background → the ink bbox by delta-E; rules ink_touches_clip_edge, draws_nothing, semantics_vs_ink IoU.
Text rules: text.truncated, text.visual_overflow_clipped, text.color_off_theme (nearest ColorScheme role), text.ink_color_mismatch.
Layout rules: overlap.siblings (suppressed for Box/stacking parents), align.baseline, align.insets, bounds.touch_vs_drawn.

## Tools (CLI parity: capture --facets render, render-check, render-node, render-diff, interact)
render_check(capture_id, composable?, source?, text?, node_key?, checks?[text|visibility|occlusion|bounds|overlap|color|alignment], min_severity=warn, max_findings=25, images=findings|none|all, window=all, include_nodes=false) → {capture_id, summary{windows, layout_nodes, composables, slot_table, frame_stable, findings{error,warn,info}}, findings[{id, rule, severity, node_key, composable, source, message, evidence, crop}], screenshot, annotated}
render_node(capture_id, selector) → declared/visible/quad/ink, status + reasons, layers, modifier boxes, text layout, occluders (composable/source), clip chain, theme colors, images (a crop with declared/visible/ink/occluder overlays; the isolated SKP layer).
render_query(capture_id, where{status|rule|composable|source|text|flag}, fields, limit).
render_diff(a, b, composable?, source?, max_changes=30): match by semantics_id → (composable, source, sibling path) → text; added/removed/moved/resized/status_changed/text_changed/layer_changed/pixels_changed_fraction/unexplained_pixel_regions + a heatmap PNG + change_count_delta.
interact(serial, package, action tap|long_press|swipe|type|semantics_action, node_key?|bounds?, text?, capture_after=true): `input tap` at the visible centre, or PerformActionCommand; then settle (RenderState has_pending_work=false and change_count/tree_hash stable over 2 polls, or timeout) → {settled, elapsed_ms, capture_id}.

## Workflows: truncated label (text.truncated at ProfileCard.kt:42, maxLines=1, parent constraint, fontScale 1.3); "button not showing" (occluded by a Surface / an ancestor alpha 0 in AnimatedVisibility / clipped by the LazyColumn); expand-reveal diff (DetailRow added but clipped_partial 0.2 by Card height(120.dp)).

## Phases
P0 correctness: per-window screenshots + frame; screenshot/overlays accept window/root_id; expose get_windows; Compose window→screen px; zero-size bounds; slot-node ids; inspect uses the slot table when already populated (makes the SKP path live).
P1: DumpRender geometry/flags/draw order/layer alpha+clip/TextLayoutResult; rules text.truncated, node.invisible, node.occluded (geometric), bounds.mismatch; render_check + render_node + CLI; View isShown/alpha/getGlobalVisibleRect + TextView ellipsis; deliberate render bugs in a11yprobe (ellipsized label, alpha-0 button, occluded button, clipped image, off-screen item); live-verify.
P2: render_diff, interact, RenderState settle, pixel-diff heatmap, if_changed_since.
P3: full layers + Quad, ModifierBox + baselines, ThemeSnapshot + color rules, config/insets, SKP layer isolation.
P4: lazy list state, Fragment/Nav/Activity stacks, remembered values, recomposition counts (per-version adapters; a time-window session facet).
