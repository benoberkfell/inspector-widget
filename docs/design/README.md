# Design notes

These notes come out of the September 2026 audit. The code is the source of truth. They record intent and rationale.

| Doc | What it covers |
|---|---|
| [`capture-and-walk.md`](capture-and-walk.md) | The agent-first capture model: capture once, retain it by id, then walk and query it with small budgeted tools (`outline`, `find`, `node`, `image`, `lint`, `diff`). Phase 0 output hygiene first. |
| [`capture-and-walk-work-packages.md`](capture-and-walk-work-packages.md) | The same plan split into file-partitioned work packages, with dependencies and acceptance criteria. |
| [`talkback-navigation.md`](talkback-navigation.md) | Debugging TalkBack navigation bugs: TalkBack 16.2's real focus/order/speech rules (source-cited), an error taxonomy (skips, traps, double stops, focus drift/reset, dialog escape, edge-stuck lists, boundary jumps…), a static TalkBack model, `tb_walk` driving real TalkBack via a uinput keyboard, `tb_scenario`, a bug corpus, and the phased plan. |
| [`render-vs-ui.md`](render-vs-ui.md) | Comparing what Compose declares (layout, semantics, text layout, layers) with what renders (pixels): the render facet, host-side analysis, and `render_check` / `render_node` / `render_diff` / `interact`. |

Paths under `scratchpad/` or shown as `<audit scratch>` refer to throwaway evidence from the audit and are not in the repo.
