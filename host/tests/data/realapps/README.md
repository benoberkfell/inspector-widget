# Real-app accessibility evidence

Accessibility dumps (`a11y.a11y_to_dict` output, `focus_order` dropped) and TalkBack 17.0
walks from three open-source debuggable apps on emulator-5554 (API 37, 1280x2856, 480dpi):
Thunderbird for Android (fossDebug, a demo account), Now in Android (demoDebug) and
AntennaPod (freeDebug, two subscriptions). Taken by the real-app validation run with the
host at e992335; the walks pressed Meta+Right through a uinput keyboard and read TalkBack's
verbose ttsOutput. `antennapod_player_expanded`, `antennapod_home_player_collapsed`,
`thunderbird_message_tb_on` and `antennapod_player_expanded_tb_on` (each of the last two with
a backward walk) came later, from live `tb-walk` runs (the dump each walk started from, and
the walk).

`talkback17_walks.json.gz` holds, per screen, the focus before the walk (`start`) and each
press as `[moved, focused node key, its label, what TalkBack said]`. A walk and its dump were
taken one after the other on the same screen, except where test_realapp_accuracy.py says
otherwise (the toolbar menu was re-created in between, or the dump came from another visit).

Used by test_realapp_accuracy.py (lint false positives, the TalkBack model's order and speech,
WebView content).

## Real-app hunt evidence (emulator-5558)

`talkback17_hunt_walks.json.gz` and the dumps it names come from the real-app findings run
(`docs/realapp-findings.md`, host at fcff905): emulator-5558, API 37, 1280x2856, TalkBack 17.0,
uinput keyboard, verbose ttsOutput. Each dump is the one its walk started from (`a11y_to_dict`,
`windows` and `diagnostics` only), except `nia_onboarding_grid_backward`, whose walk started
from the same dump as `nia_onboarding_grid` (`"dump"`). Each entry has the same `start` and
`steps` as above, plus:

- `walk`: the tb-walk id that `docs/realapp-findings.md` cites.
- `talkback_started`: `before_app` means TalkBack was on before the app process started, which
  is what a TalkBack user gets. `after_app` means the walk turned TalkBack on with the app
  already running. `before_walk` means TalkBack was already on and the order is not recorded.
  RecyclerView rows bound before TalkBack started have no CollectionItemInfo, so the `_tb_first`
  and `_tb_later` pairs show the same screen both ways.
- `density`: lint at this dpi. `nia_feed_two_column` was taken at `wm density 280`.
- `autoscroll`: indices into `steps` where TalkBack auto-scrolled. The model does not
  auto-scroll, so stop a press-for-press comparison there.
- `note`: what the walk shows. Its step numbers are the walk's: step 0 is `start`, so walk step
  n is `steps[n-1]`.
- `said` is null where TalkBack's speech was not logged or focus did not move.

No test uses these yet. They pin the app bugs and the tool gaps in `docs/realapp-findings.md`,
and are inputs for the next round's tests.

## TalkBack-first relaunch evidence (emulator-5558, walk-fidelity)

`talkback17_relaunch_walks.json.gz` holds AntennaPod walks taken in both TalkBack start orders
with `relaunch` (G1), so the trap the model predicts (`talkback/order.py Navigator.traps`,
pinned by `test_the_antennapod_player_trap_is_named_and_explained` from an emulator-5554 walk)
can be re-judged. Same emulator and TalkBack as the hunt; host at the walk-fidelity branch.
Each entry has the hunt entries' fields (`source`, `walk`, `talkback_started`, `density`,
`start`, `steps` as `[moved, key, label, said]`, `note`) plus `ended`, `injector_proven` and the
walk's finding codes (`findings`, with an `unverified` basis when the walk marked one). The
dump the walk started from (`a11y_to_dict`, `windows` and `diagnostics` only, taken with
TalkBack on) is inline as `dump` for the first walk of each group. Every run restarted the
app, so the others have node keys of their own process and no dump: `same_screen_as` names
the walk whose dump shows their screen; compare their labels and speech, not their keys.

- `antennapod_player_expanded_tb_first_1..3`: `tb_scenario(relaunch=true,
  target="#fragmentLayout", action="activate", leave_on=true)` opened the expanded player with
  TalkBack running, then `tb_walk(start="#add_to_favorites_item", max_steps=12, until="steps")`.
- `antennapod_player_expanded_tb_later_1..3`: the app started and the mini player tapped with
  TalkBack off; the walk turned TalkBack on.
- In all 6, after "swipe up to read shownotes. Shownotes" TalkBack goes on into the show notes
  WebView ("Webview", then the notes and "Bullet. 1 of 19. In list. 19 items"). The
  emulator-5554 trap did not happen in either start order.
- `antennapod_episode_details_relaunch_tb_first_1..2`: AP-4 TalkBack first (opened with
  `tb_scenario(relaunch=true, target=CardView"TWiT 1103…")`): the "Page. …" stop, then into the
  show notes after "Download" (wzi9apx's shape).
- `antennapod_episode_details_relaunch_tb_later_1..3`: AP-4 TalkBack after the app: no "Page"
  stop; stuck after "Download" in 1 of 3 (`wznjfok`, its tb.edge_stuck now info with basis
  `unverified: after_app`), into the WebView in the other 2.
