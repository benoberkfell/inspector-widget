# Real-app TalkBack findings

Inspector Widget was run against three open-source Android apps to find real TalkBack
navigation bugs in them and to record every place the tool fell short. This page has both
lists: the app bugs, each reproduced independently, and the tool gaps, ranked as the next
improvement backlog.

## Scope and caveats

- **Found on an emulator with debug builds.** emulator-5558, Android 17 (API 37), 1280x2856 at
  480 dpi, TalkBack 17.0.0.889642762, 30 September to 1 October 2026. Some behaviour may
  differ on a phone, on another TalkBack version, or in a release build. TB-1, for one, only
  exists where a debug-only feature flag is on.
- **Not checked against the apps' issue trackers.** Some of these may already be reported or
  fixed upstream.
- **Builds**

  | App | Package | Build | Version | Source commit |
  |---|---|---|---|---|
  | Thunderbird for Android | `net.thunderbird.android.debug` | fossDebug, demo account | 25.0-SNAPSHOT (code 4) | thunderbird-android `df2d383` (2026-09-30) |
  | Now in Android | `com.google.samples.apps.nowinandroid.demo.debug` | demoDebug | 0.1.2 (code 8) | nowinandroid `a49ed25` (2026-09-22) |
  | AntennaPod | `de.danoeh.antennapod.debug` | freeDebug, two subscriptions | 3.12.1 (code 3120195) | AntennaPod `0ad82a8` (2026-09-29) |

  Source paths below are relative to each app's repository root.
- **Tool:** Inspector Widget at `fcff905`, driven through its CLI. The CLI and the MCP server
  are at parity.
- **How "confirmed" was decided.** One stage hunted for bugs. A second stage independently
  re-ran every claim from a cold start, re-read the cited source, and gave a verdict. Only the
  bugs the second stage confirmed are listed as bugs. Where the second stage corrected part of a
  claim, the correction is folded in and the refuted part is listed under
  [Refuted claims](#refuted-claims-and-sub-claims).
- **Recording ids.** `w…` ids are `tb-walk` recordings and `t…` ids are `tb-scenario` runs.
  Captures have `c…` ids, and `n…` refs are nodes within a capture. They were saved in the run
  machine's cache (`~/Library/Caches/inspector-widget/`) and are not in the repo. A subset is
  checked in as fixtures (see [Evidence in the repo](#evidence-in-the-repo)).
- **Keyboard and gestures.** Walks drove TalkBack by keyboard (Meta+arrows) through a uinput
  virtual keyboard. Keyboard next/previous uses the same traversal as swipe right/left. TB-4,
  NIA-1, AP-1 and AP-2 were also walked with real swipes (`--injector touch`), and the results
  were the same. Explore-by-touch (dragging a finger) is different: it reaches only what is drawn
  on top, so TB-4's covered toolbar and AP-1's 0 px stops are reachable by swiping but not by
  touch.
- **When TalkBack started matters.** By default the tool turns TalkBack on for each walk, after
  the app is already running. A TalkBack user has TalkBack running before the app starts. These
  two cases expose different trees:
  - RecyclerView adds its per-item accessibility delegate (CollectionItemInfo, and
    importantForAccessibility=YES on the item root) only to rows bound while accessibility is
    on.
  - WebViews also behave differently.

  So "N of M" positions (TB-1) and AntennaPod's WebView walls and traps (AP-1, AP-3, AP-4)
  depend on the start order. Each finding below says which case it was confirmed in. To
  reproduce the realistic case, run TalkBack first:

  ```bash
  iw talkback on --verbose-log        # TalkBack first, with verbose speech logging
  adb -s emulator-5558 shell am force-stop <pkg>; <launch the app>
  iw tb-walk --package <pkg> ... --utterance logcat --leave-on
  iw talkback restore                 # always, at the end
  ```

### Conventions for the repro commands

Run these from the repo root:

```bash
iw() { PYTHONPATH=host host/.venv/bin/python host/cli.py "$1" --serial emulator-5558 "${@:2}"; }
TB=net.thunderbird.android.debug
NIA=com.google.samples.apps.nowinandroid.demo.debug
AP=de.danoeh.antennapod.debug

# Thunderbird: the launcher entry is an alias, so "am start -n .../MainActivity" fails
tb_start()  { adb -s emulator-5558 shell am force-stop $TB
              adb -s emulator-5558 shell am start -W -a android.intent.action.MAIN \
                  -c android.intent.category.LAUNCHER -p $TB; }
# Now in Android, cold: with TalkBack off, then tap OK on the system "Android App
# Compatibility" (16 KB) dialog and "Don't allow" on the notification dialog
nia_cold()  { adb -s emulator-5558 shell pm clear $NIA
              adb -s emulator-5558 shell am start -S -W \
                  -n $NIA/com.google.samples.apps.nowinandroid.MainActivity; }
ap_start()  { adb -s emulator-5558 shell am force-stop $AP
              adb -s emulator-5558 shell am start -W -n $AP/de.danoeh.antennapod.activity.MainActivity; }
```

Use `adb shell input` only for BACK/HOME, or for taps while TalkBack is off, because it never
reaches TalkBack. Read full utterances from the saved walk JSON (`"saved"` in the output,
`steps[].speak` where `utt` is `logcat`). The printed walk lines cut speech at about 24
characters, and most of these defects are in the tail.

## Summary

| ID | App | Screen | Kind | Severity | In one line |
|---|---|---|---|---|---|
| [TB-1](#tb-1) | Thunderbird | Message list | wrong_announcement | medium | First message is "2 of 6", list says "6 items" for 5: an empty banner item counts |
| [TB-2](#tb-2) | Thunderbird | List > message > back | restore_failed | high | Back from a message lands on "Navigate up", not the row |
| [TB-3](#tb-3) | Thunderbird | Select via avatar (Compose rows) | focus_lost_on_update | high | Focused avatar is replaced by an unlabelled check button; focus jumps to a hidden toolbar |
| [TB-4](#tb-4) | Thunderbird | Selection (action) mode | ghost_stop | high | Five covered toolbar buttons are read first; the visible action bar is read last |
| [TB-5](#tb-5) | Thunderbird | Selection mode > Delete | focus_lost_on_update | medium | After Delete, focus goes to the top; the result is never announced |
| [TB-6](#tb-6) | Thunderbird | Compose rows: star | unlabelled | medium | Star says only "Button", with no state, and toggling is silent |
| [TB-7](#tb-7) | Thunderbird | Compose rows | wrong_announcement | medium | Reads "[attachment_icon]" and "[conversation_counter]" aloud |
| [TB-8](#tb-8) | Thunderbird | Compose rows | double_stop, missing state | medium | Three stops per message; the avatar says just its initials; "unread" is never spoken |
| [TB-9](#tb-9) | Thunderbird | Message rows | gesture-only action | medium | Swipe actions have no accessibility action |
| [TB-10](#tb-10) | Thunderbird | Folder drawer | unlabelled | medium | Expand chevron is "Unlabelled"; every action is announced as "Tab" |
| [TB-11](#tb-11) | Thunderbird | Settings | wrong_announcement | medium | Every row starts "Account settings"; section titles are list items, not headings |
| [TB-12](#tb-12) | Thunderbird | View rows | wrong_announcement | low | Every row ends "Star", starred or not |
| [TB-13](#tb-13) | Thunderbird | Message header | unlabelled | medium | Star says "Button" and never says whether it is on |
| [NIA-1](#nia-1) | Now in Android | Onboarding topic grid | skip | high | 6 of 19 topics can never be reached by swiping |
| [NIA-2](#nia-2) | Now in Android | Onboarding, Interests, Search | double_stop | medium | Each topic takes two stops |
| [NIA-3](#nia-3) | Now in Android | Feed, two columns | out_of_order | medium | Bookmark buttons of side-by-side cards are interleaved and read after the other card |
| [NIA-4](#nia-4) | Now in Android | Interests > topic > back | restore_failed | medium | Back lands on Search, not the topic row (C14) |
| [NIA-5](#nia-5) | Now in Android | Saved: remove bookmark | focus_lost_on_update | medium | Focus jumps to the nav bar; Undo is 6+ swipes away in a short snackbar |
| [NIA-6](#nia-6) | Now in Android | Onboarding Done | focus_lost_on_update | low | Done resets focus to Search at the top |
| [NIA-7](#nia-7) | Now in Android | Topic chips on cards | wrong_announcement | medium | "Wear OS is not followed" but activating it opens the topic |
| [NIA-8](#nia-8) | Now in Android | Topic screen follow chip | wrong_announcement | low | "Not selected. NOT FOLLOWING. Check box" |
| [NIA-9](#nia-9) | Now in Android | Interests list | wrong_announcement | low | Every row says "Not selected" |
| [NIA-10](#nia-10) | Now in Android | Bookmark/follow toggles | wrong_announcement | low | "checked. Unbookmark"; no toggle names its item |
| [NIA-11](#nia-11) | Now in Android | Feed, partly scrolled cards | skip | low | Bookmark of a half-scrolled card is skipped; a card can be read twice |
| [NIA-13](#nia-13) | Now in Android | Interests list | wrong_announcement | low | "In list. 20 items" for 19 topics (a bottom Spacer item) |
| [AP-1](#ap-1) | AntennaPod | Main screens with the mini player | ghost_stop | high | 66 invisible show-notes stops, 0 px tall below the screen, before the mini player |
| [AP-2](#ap-2) | AntennaPod | Expanded player | escape | high | Swiping past the last control reads the Home screen hidden behind the player |
| [AP-3](#ap-3) | AntennaPod | Expanded player, cover page | ghost_stop | high | 66 off-screen show-notes stops before the seek bar and play controls |
| [AP-4](#ap-4) | AntennaPod | Episode details pager | ghost_stop / trap | high | Reads the previous episode's off-screen notes; stuck after Download if TalkBack started later |
| [AP-5](#ap-5) | AntennaPod | Play/pause button | wrong_announcement | medium | Says "Pause" while paused |
| [AP-6](#ap-6) | AntennaPod | Feed > episode > back | restore_failed | medium | Back lands on the toolbar "Back", not the row |
| [AP-7](#ap-7) | AntennaPod | Feed header | out_of_order | low | Buttons are read before the podcast title above them |
| [AP-8](#ap-8) | AntennaPod | Download/Stream buttons | wrong_announcement | low | No role: "Download", "Stream" |
| [AP-9](#ap-9) | AntennaPod | Episode filter sheet | other | low | Sheet opens on "Clear" with no title |
| [AP-11](#ap-11) | AntennaPod | Expanded player seek bar | unlabelled | low | "0%. Slider", plus bare numbers as separate stops |

NIA-13 was found while reproducing NIA-9. NIA-12 and AP-10 were
[inconclusive](#inconclusive). Severity is the impact on a TalkBack user. "high" means content
or controls become unreachable, or focus is thrown somewhere misleading on a core path.

---

## Thunderbird

<a id="tb-1"></a>
### TB-1: Message list positions and counts are off by one

- **Screen / kind / severity:** Message list (Inbox), Compose rows and View rows /
  wrong_announcement / medium.
- **Confirmed in:** both start orders. The per-row "N of M" is heard only when TalkBack was
  running before the list was bound, which is the realistic case.
- **Repro:** run `iw talkback on --verbose-log`, then `tb_start`, then
  `iw tb-walk --package $TB --start first --utterance logcat --leave-on --json walk.json`, then
  `iw talkback restore`. Read `steps[].speak`. As a causal check, turn off
  `display_in_app_notifications` (overflow > DEBUG: Feature Flags > Apply), relaunch, walk
  again, and set the flag back afterwards.
- **What TalkBack says:** with 5 Compose rows and TalkBack first (wcw61ax), the first message is
  "... You should still be able to read this message.. 2 of 6. In list. 6 items", then
  "3 of 6" through "6 of 6". No message is ever "1 of 6". View rows say
  "unread, Localpart ... Star. 2 of 5. In list. 5 items" through "5 of 5" (ww7ezvy). When
  TalkBack started after the app (wygfouz), the first row says "In list. 6 items" for 5 rows
  and no row has a position. With the flag off (wr43dka), the 4 rows read "1 of 4. In list. 4
  items" through "4 of 4".
- **Why:** `legacy/ui/legacy/src/main/java/com/fsck/k9/ui/messagelist/LegacyMessageListFragment.kt:2045-2047`
  always adds `MessageListViewItem.InAppNotificationBannerList` at index 0 when
  `DISPLAY_IN_APP_NOTIFICATIONS` is on. The flag is on for debug, daily and beta builds
  (`config/featureflag/thunderbird_mobile_featureflag.catalog.json:71` and the other build
  overrides). It defaults to false, and the release override is empty, so current release
  builds are not affected. `MessageListAdapter.kt:225-230` binds the banner to a ComposeView
  that renders nothing (0x0) when there are no notifications. `getItemCount()` (`:164`) still
  counts it, so RecyclerView's CollectionInfo and CollectionItemInfo do too.
- **Fix:** add the banner item only when there is a notification to show, inserting or
  removing it as the notification state changes, or move the banner out of the adapter to a
  view above the RecyclerView. If it must stay an adapter item, override the LayoutManager's
  `onInitializeAccessibilityNodeInfo` / `onInitializeAccessibilityNodeInfoForItem` so that
  `rowCount` and `rowIndex` count only message items.
- **How Inspector Widget surfaces it:**
  - With TalkBack first, `tb-walk` raises tb.wrong_announcement: "the first row of view:39 is
    announced "2 of 6": 1 item(s) before it count but TalkBack never stops on them".
  - It is silent on "In list. 6 items", the only symptom in the tool's default start order.
  - The static data has the cause, but no rule reads it. `outline --view views` shows the 0x0
    ComposeView at adapter position 0 (n1745 in cda8jc, n86 in c2c7dx). The RecyclerView
    reports `rows=6`, and the first View row has `item.row=1` (n2096 in ctda8w).
  - When the dump carries CollectionItemInfo, the talkback.order model speaks the shifted
    positions correctly (21/21 presses, words included, on the fixture
    `thunderbird_list_compose_tb_first`).

<a id="tb-2"></a>
### TB-2: Back from a message does not return focus to its row

- **Screen / kind / severity:** Message list > message view > back / restore_failed / high.
- **Confirmed in:** both start orders.
- **Repro:** run `tb_start`, then
  `iw tb-scenario restore --package $TB --target "Message details demo" --json -`. Repeat with
  View rows (flag `use_compose_for_message_list_items` off). For comparison, run it on a thread
  (`--target "Re: Thread"`), which does restore.
- **What TalkBack does:** opening the message puts focus on "Webview. In horizontal pager". After
  BACK, focus goes null at 56 ms and then lands on "Navigate up. Button. Out of grid pager" at
  531 ms. TalkBack's own log marks that focus `isInitialFocus=true`: it fell back to the first
  node and restored nothing (tgh2po4; hunt runs tswxnsg, tzk3grg, tx706o8). With View rows,
  focus is already on the toolbar's "Navigate up" (view:32) while the message is shown, because
  that View persists across the ViewSwitcher, and it stays there after BACK. A thread restores
  to its row (compose:525:390).
- **Why:** `legacy/ui/legacy/src/main/java/com/fsck/k9/activity/MessageHomeActivity.kt:848-852`
  handles BACK in MESSAGE_VIEW by calling `showMessageList()` (`:1374-1390`). That flips the
  ViewSwitcher (`showFirstView()`) and calls `setActiveMessage(null)`, which rebinds the row.
  Nothing puts accessibility focus back, and because the window is the same, TalkBack has no
  per-window record to restore. Threads go through a fragment back-stack pop
  (`showThread`/`addToBackStack`, `:1208-1210`), which TalkBack does restore.
- **Fix:** remember the opened MessageReference. In `onSwitchComplete(displayedChild == 0)`
  (`:1431-1435`) or in `messageListFragment.onFullyActive()`, scroll that row into view and,
  once it is laid out, send it ACTION_ACCESSIBILITY_FOCUS: for View rows
  `holder.itemView.performAccessibilityAction(ACTION_ACCESSIBILITY_FOCUS, null)`, for Compose
  rows the host ComposeView's
  `accessibilityNodeProvider.performAction(rowSemanticsId, ACTION_ACCESSIBILITY_FOCUS, null)`.
  Neither `requestFocus` nor a `FocusRequester` moves TalkBack focus.
- **How Inspector Widget surfaces it:** `tb-scenario restore` gives verdict `top` and
  tb.restore_failed, and diagnoses single-activity navigation correctly. The static model makes
  no prediction for back.

<a id="tb-3"></a>
### TB-3: Selecting a message by its avatar drops focus onto a hidden toolbar

- **Screen / kind / severity:** Message list (Compose rows), select via the avatar /
  focus_lost_on_update / high.
- **Confirmed in:** both start orders.
- **Repro:**
  1. `tb_start` (Compose rows; `use_compose_for_message_list_items=true`).
  2. `iw capture --package $TB --label l`, then `iw find -c l --text TE --fields +ids` to get the avatar key.
  3. `iw tb-scenario focus-after --package $TB --target compose:<acv>:<id> --action activate --json -`.
- **What TalkBack does:**
  - Focus goes compose:117:80, then null at about 300 ms, then view:32 at about 600 ms
    (tiq3ssh, t8zdyld). TalkBack says "1 selected" once, from the action mode's
    window-state change. It then places initial focus (`isInitialFocus=true`) on "Navigate up.
    Button. Out of list", which the action-mode bar now covers.
  - The selected avatar is a new node that TalkBack reads as just "Button" (wdvjrk4 step 9).
    The row reads "Inline image (data: URI). 2/14/2023. data@example.com. 3 of 6" and never
    says "selected".
  - With View rows, the same action keeps focus on the Select area, which now says "Deselect",
    and TalkBack says "2 selected" (a 2-message thread) and then "selected" (tkl3fqg).
- **Why:** `feature/mail/message/list/api/src/main/kotlin/net/thunderbird/feature/mail/message/list/ui/component/organism/MessageItem.kt:216-240`.
  `AnimatedContent(targetState = selected)` swaps `MessageItemAvatarCircle`
  (`molecule/MessageItemAvatarCircle.kt:48-56`, a combinedClickable with only a monogram) for a
  separate `ButtonIcon(Icons.Outlined.Check)` that has no contentDescription. The focused
  semantics node is removed, so TalkBack loses focus.
- **Fix:** give the leading element one stable semantics node, for example
  `Modifier.toggleable(value = selected, role = Role.Checkbox, onValueChange = { onAvatarClick() })`
  with `contentDescription = "Select message from <sender>"`. Animate only the visual content
  inside it, so selection becomes a "checked" state change on the same node. Expose the state
  on the row too (`Modifier.semantics { selected = isSelected }`).
- **How Inspector Widget surfaces it:** the `focus-after` timeline shows the drop. The model
  predicted the landing node view:32 (first_content). The verdict, `on_close_or_unlabeled` /
  tb.initial_focus, comes with dialog advice that does not fit (gap G12), and nothing notices
  that view:32 is covered (G5). After selection, static lint flags the check button
  `a11y.label.missing` (n239 in c7wz69).

<a id="tb-4"></a>
### TB-4: In selection mode TalkBack reads the covered toolbar first and the action bar last

- **Screen / kind / severity:** Message list in selection (action) mode, both row types /
  ghost_stop (covered stops) / high.
- **Confirmed in:** walks where the tool turned TalkBack on (wdvjrk4) and where TalkBack was
  already running (wjx0ny4, with swipes: the same order and speech).
- **Repro:** run `tb_start` and select one message (activate its avatar, or long-press with
  TalkBack off). Then run
  `iw tb-walk --package $TB --start first --utterance logcat --json -`. For swipes, use
  `--injector touch --start current`, because the touch injector cannot start at `first`.
- **What TalkBack says** (wdvjrk4):
  - Steps 0-4 read the normal toolbar: "Navigate up. Button", "Inbox", "Search. Button",
    "Sort by…. Button", "More options. Button". The action-mode bar (ActionBarContextView n1855,
    [0,156 1280x192]) covers that toolbar exactly, and the screenshot shows the toolbar hidden.
  - The visible action bar comes after the whole list and the Compose FAB (steps 21-26): "Done.
    Button", "1 selected", "Delete. Button", "Mark as read. Button", "Archive. Button", "More
    options. Button".
  - Explore-by-touch cannot reach the covered toolbar. Only linear navigation (swipe or
    keyboard) does.
- **Why:** `core/ui/legacy/theme2/common/src/main/res/values/themes.xml:12` sets
  `windowActionModeOverlay=true`, so AppCompat draws the ActionBarContextView over the
  MaterialToolbar. The toolbar stays important for accessibility and comes first in traversal.
  `LegacyMessageListFragment.kt:2091-2094` (`startAndPrepareActionMode`) and
  `ActionModeCallback.onCreateActionMode` (`:2548`) neither hide the toolbar from accessibility
  nor move focus to the action mode.
- **Fix:** in `onCreateActionMode`, set
  `toolbar.importantForAccessibility = IMPORTANT_FOR_ACCESSIBILITY_NO_HIDE_DESCENDANTS` (and the
  FAB's, if it stays), and restore it in `onDestroyActionMode` (`:2537`). After the action mode
  shows, focus its title or Done button (ACTION_ACCESSIBILITY_FOCUS). Or drop the overlay so the
  action mode replaces the toolbar in the hierarchy.
- **How Inspector Widget surfaces it:**
  - The model agreed with the order (29/29; 27/27 presses on the fixture
    `thunderbird_selection_mode`).
  - The walk flagged only tb.out_of_order on Done / 1 selected / Delete, and nothing on the
    five covered stops.
  - Static lint gave only `a11y.clickable.duplicate_bounds` (warn) on the action items, and the
    outline marked "More options" `!duplicate`. Nothing models a same-window overlay as covering
    (G5).

<a id="tb-5"></a>
### TB-5: Deleting from selection mode throws focus to the top and announces nothing about it

- **Screen / kind / severity:** Message list selection mode > Delete / focus_lost_on_update /
  medium.
- **Confirmed in:** both start orders.
- **Repro:** run `tb_start` and select one message (TB-3). Then run
  `iw tb-scenario focus-after --package $TB --target Delete --action activate --json -`. This
  moves the message to Trash, so move it back afterwards.
- **What TalkBack does:** focus goes view:205, then null at 449 ms, then view:32 at 988 ms.
  TalkBack speaks the window title "Thunderbird Debug" and then places initial focus on
  "Navigate up. Button". It says nothing about the deletion, and there is no Snackbar or Undo.
  Hunt run: tm79xfy.
- **Why:** `R.id.delete` (`LegacyMessageListFragment.kt:2628`) calls `onDelete` (`:1013`). The
  action mode finishes (`onDestroyActionMode`, `:2537-2546`) and the focused Delete button is
  removed. The app places no accessibility focus and announces nothing.
- **Fix:** once the DiffUtil update is laid out, focus the row that took the deleted row's
  place, else the previous row, else the empty list. Announce the result with an accessible
  Snackbar with Undo, or `announceForAccessibility("1 message deleted")`.
- **How Inspector Widget surfaces it:** the `focus-after` timeline shows the drop, and the model
  predicted view:32. The verdict (`on_close_or_unlabeled`) and its dialog advice are misleading
  for a list mutation (G12). The scenario does not record what TalkBack said, so the missing
  announcement needed a logcat tail (G11).

<a id="tb-6"></a>
### TB-6: Compose message rows: the star is an unlabelled, stateless "Button"

- **Screen / kind / severity:** Message list, Compose rows, favourite star / unlabelled /
  medium.
- **Confirmed in:** both start orders.
- **Repro:**
  1. `tb_start`, then `iw tb-walk --package $TB --start first --utterance logcat`.
  2. Get the key with `iw find -c <capture> --tag MessageItem_FavouriteButtonIcon --fields +ids`.
  3. `iw tb-scenario survive --package $TB --target compose:<acv>:<starId> --mutate activate`,
     then toggle the star back.
- **What TalkBack says:** every star says "Button", and its usage hint is just "Press select to
  activate" (wygfouz steps 7, 10, 13, 16, 19). Toggling keeps focus (verdict `kept`), but
  TalkBack's TYPE_VIEW_CLICKED feedback is empty, so nothing is spoken. A walk afterwards still
  says "Button", although the screenshot shows the star filled. The View rows say "Add star" /
  "Remove star".
- **Why:** `feature/mail/message/list/api/src/main/kotlin/net/thunderbird/feature/mail/message/list/ui/component/atom/FavouriteButtonIcon.kt:27-36`
  calls `ButtonIcon` without a contentDescription.
  `components/ui/bolt/src/commonMain/kotlin/net/thunderbird/components/ui/bolt/atom/button/ButtonIcon.kt:25`
  defaults it to null and passes it to the Icon at `:37`, and nothing exposes the favourite
  boolean. `MessageItem.kt:170-175` makes the star visually half height, but the M3 IconButton's
  `minimumInteractiveComponentSize` keeps the touch area at 48 dp.
- **Fix:** use a toggle with state, such as `IconToggleButton(checked = favourite, ...)` with
  `Icon(contentDescription = stringResource(R.string.star_action))`, or
  `Modifier.semantics { contentDescription = "Star"; stateDescription = if (favourite) "Starred" else "Not starred" }`
  with `toggleable` / `Role.Switch`. Alternatively, reuse the View strings "Add star" / "Remove
  star".
- **How Inspector Widget surfaces it:** the walk raises tb.ghost_stop ("is a stop but is
  unlabelled (144x144px, said 'Button')"), and lint has `a11y.label.missing` right. Lint also
  raises `a11y.touch_target.small` "w_dp=48 h_dp=24 ... touch_w_dp=48 touch_h_dp=48", which is
  a false positive: the node TalkBack focuses is 144x144 px = 48x48 dp (G18).

<a id="tb-7"></a>
### TB-7: Compose message rows read placeholder ids aloud

- **Screen / kind / severity:** Message list, Compose rows / wrong_announcement / medium.
- **Confirmed in:** both start orders.
- **Repro:** run `tb_start`, then
  `iw tb-walk --package $TB --start first --utterance logcat --json walk.json`, and read the
  full `speak` of each row.
- **What TalkBack says:** "Re: Thread. 2/10/2023. bob@example.com. [conversation_counter] This
  is the second message in this thread.. 2" (wygfouz step 11; the thread count is a bare "2"
  at the end). A walk of the Trash folder (wyb8ou1 step 5) gave "Inline image attachment.
  2/14/2023. data@example.com. [attachment_icon] . 2 of 4. In list. 4 items".
- **Why:** `feature/mail/message/list/api/src/main/kotlin/net/thunderbird/feature/mail/message/list/ui/component/molecule/MessageBodyContent.kt:94-104`
  calls `appendInlineContent` with `alternateText` set to the constants in
  `organism/MessageItemDefaults.kt:20,22`, which are `"[attachment_icon]"` and
  `"[conversation_counter]"`. Compose exposes alternateText as the text's accessibility
  content. The counter badge's own text is merged in separately.
- **Fix:** pass localized alternateText (`stringResource(R.string.has_attachment)`,
  `pluralStringResource(R.plurals.messages_in_thread, count, count)`) and clear the badge's own
  semantics. Or give the row an explicit contentDescription built from sender, subject, date,
  attachment, thread size and unread state.
- **How Inspector Widget surfaces it:** the model predicted this exact text, but nothing flags
  it. `outline --view reading` cuts labels at about 48 characters and the walk lines at about
  24, so the token is visible only in the saved walk JSON (G2, G17).

<a id="tb-8"></a>
### TB-8: Compose message rows: three stops per message, a bare monogram, no "unread"

- **Screen / kind / severity:** Message list, Compose rows / double_stop and missing state /
  medium.
- **Confirmed in:** both start orders.
- **Repro:** run `tb_start` and `iw tb-walk --package $TB --start first --utterance logcat`.
  Then turn the Compose-rows flag off and walk again to compare.
- **What TalkBack says:** each Compose row takes three stops: the row, the avatar saying only
  its monogram ("SE", "TE", "BO", "AL", "TH", with only the generic activation hint), and the
  star "Button" (wygfouz, wcw61ax). Unread rows (which show the unread dot) never say "unread"
  in Compose. The View rows for the same messages say "unread, ..." (ww7ezvy steps 5, 8, 14).
- **Why:** `molecule/MessageItemAvatarCircle.kt:48-56` adds `combinedClickable` to a Box whose
  only content is the monogram text (`:64`). `organism/MessageItem.kt:113-116` makes the row a
  combinedClickable Surface without merging the avatar or the star. The badges in
  `atom/MessageBadge.kt:26,46` are purely visual.
- **Fix:** see TB-3 for the avatar (a named checkbox with state). Better still, keep the row as
  the only stop and expose Select and Star as `CustomAccessibilityAction`s, with the children
  hidden from accessibility. Add `stateDescription = "Unread"` on the row, matching the View
  rows.
- **How Inspector Widget surfaces it:** tb.double_stop on every row; lint gives
  `a11y.role.missing_on_clickable` (info) on the avatars. Nothing compares the two row
  implementations.

<a id="tb-9"></a>
### TB-9: Swipe actions on message rows have no accessibility equivalent

- **Screen / kind / severity:** Message list rows, View and Compose / gesture-only action /
  medium.
- **Confirmed at:** node level and from TalkBack's own usage hint. TalkBack's actions menu was
  not opened, because the tool cannot open it (G14).
- **Repro:** run `tb_start`, `iw capture --package $TB --label l`, then
  `iw node -c l <row ref> --facets a11y --json`. The swipe preferences on emulator-5558 were
  left = ToggleRead and right = ToggleSelection.
- **What TalkBack offers:** Compose rows expose only CLICK and LONG_CLICK (n1757 in cda8jc). View
  rows expose CLICK, LONG_CLICK and SHOW_ON_SCREEN (n2096 in ctda8w). Neither has custom
  actions. A focused row's hint is "Press select to activate. Press select and hold to long
  press", with no "actions available". Toggling read state (or archive/delete, when
  configured) is gesture-only. A TalkBack user can reach it only through long-press selection
  and the action bar.
- **Why:** `LegacyMessageListFragment.kt:576-588` attaches
  `ItemTouchHelper(MessageListSwipeCallback)`, and nothing in the legacy message list adds
  accessibility actions. The new Compose screen has them
  (`feature/mail/message/list/internal/.../MessageListScreenAccessibilityState.kt:133-155`), but
  the legacy list in use does not.
- **Fix:** in `MessageListAdapter.onBindViewHolder`, call `ViewCompat.addAccessibilityAction`
  for the configured left and right swipe actions, invoking the same handler as `onSwipeAction`
  (`LegacyMessageListFragment.kt:2231-2275`). For Compose rows, add `customActions` in
  `MessageItem`, reusing `SwipeDirectionAccessibilityAction`.
- **How Inspector Widget surfaces it:** only the `node` a11y facet shows it, as an action list
  with no custom actions. No rule notices an ItemTouchHelper without matching actions (G17).

<a id="tb-10"></a>
### TB-10: Folder drawer: unlabelled expand chevron, and every entry is a "Tab"

- **Screen / kind / severity:** Navigation drawer (folders) / unlabelled / medium.
- **Confirmed in:** the tool's default start order.
- **Repro:** run `tb_start`. With TalkBack off, open the drawer with
  `adb -s emulator-5558 shell input tap 84 252`. Then run
  `iw tb-walk --package $TB --start first --utterance logcat --json -`.
- **What TalkBack says** (w7gq3kk):
  - The account header takes two stops: "demo@example.com", then "DE".
  - Then "selected. Inbox. 4. Tab. In list. 9 items", "Outbox. Tab", ..., "Nested. Tab".
  - Step 10 says "Unlabelled". It is the expand chevron of folder "Nested" (compose:55:239,
    144x144 px), with no expanded/collapsed state.
  - Then "Turing. Tab", "Sync account. Tab. In list", "Manage folders. Tab", "Settings. Tab".
    Actions are announced as tabs, and the unread count is a bare number.
- **Why:** `feature/navigation/drawer/dropdown/src/main/kotlin/net/thunderbird/feature/navigation/drawer/dropdown/ui/folder/FolderListItem.kt:133-147`
  is a clickable Box holding only `AnimatedExpandIcon`, and `ui/common/AnimatedExpandIcon.kt:27-33`
  sets `contentDescription = null`. The Tab role comes from the M3 NavigationDrawerItem wrapped
  by `components/ui/bolt/.../organism/drawer/NavigationDrawerItem.kt:44`, which
  `ui/setting/SettingListItem.kt:80` also uses for actions.
- **Fix:** on the chevron, add
  `Modifier.semantics { contentDescription = "Subfolders of $name"; stateDescription = if (expanded) "Expanded" else "Collapsed"; role = Role.Button }`,
  or on Compose 1.8+ put `expand {}` / `collapse {}` actions on the folder row and drop the
  separate stop. Describe the badge ("5 unread"). Render Sync, Manage folders and Settings as
  plain buttons.
- **How Inspector Widget surfaces it:**
  - Model and walk agreed (14/14), and the walk raised tb.double_stop and tb.ghost_stop on the
    chevron.
  - Lint flags the chevron `label.missing` (the existing fixture `thunderbird_drawer` pins
    compose:285:836 "Unlabelled").
  - The walk also raised a false tb.skipped for the texts behind the modal drawer (G5).

<a id="tb-11"></a>
### TB-11: Settings: every row starts "Account settings"

- **Screen / kind / severity:** Settings (top level) / wrong_announcement / medium.
- **Confirmed in:** the tool's default start order.
- **Repro:** run `tb_start`, open the drawer, then Settings. Then run
  `iw tb-walk --package $TB --start first --utterance logcat --json -`.
- **What TalkBack says** (wxc6f3c): "Account settings. General settings. 1 of 11. In list. 11
  items", "Accounts. 2 of 11", "Account settings. demo@example.com. demo@example.com. 3 of 11",
  "Account settings. Add account. 4 of 11", "Backup. 5 of 11", "Account settings. Export
  settings. 6 of 11", ..., "Account settings. Support Thunderbird. 11 of 11". The section
  titles ("Accounts", "Backup", "Miscellaneous") are counted as list items and are not headings.
- **Why:** `legacy/ui/legacy/src/main/res/layout/text_icon_list_item.xml:28`: the decorative
  `@id/icon` has `android:contentDescription="@string/account_settings_action"`, and every
  settings row uses that layout. `account_list_item.xml:25` repeats it for the account row.
- **Fix:** set `android:contentDescription="@null"` (or `importantForAccessibility="no"`) on both
  icons. Mark the section headers as headings (`ViewCompat.setAccessibilityHeading(view, true)`)
  and make them non-clickable.
- **How Inspector Widget surfaces it:** the model and the walk agreed (13/13 presses, words
  included, on the fixture `thunderbird_settings`). The walk raised no finding at all. Lint
  gave only 3 touch_target warnings, on the section titles, which are click+longclick (capture
  cv83cg) (G16, G17).

<a id="tb-12"></a>
### TB-12: View message rows always end with "Star"

- **Screen / kind / severity:** Message list, View rows (`use_compose_for_message_list_items`
  off) / wrong_announcement / low.
- **Confirmed in:** TalkBack first (ww7ezvy).
- **Repro:** turn the flag off (overflow > DEBUG: Feature Flags > Apply), run `tb_start`, then
  `iw tb-walk --package $TB --start first --utterance logcat`.
- **What TalkBack says:** "unread, Localpart ... Star. 2 of 5. In list. 5 items". Every row ends
  "Star", and none was starred. The separate star stop then says "Add star". Each row takes
  three stops: row, "Select", star.
- **Why:** `legacy/ui/legacy/src/main/res/layout/message_list_item.xml:152-159`: the decorative
  `@id/star` ImageView has `android:contentDescription="@string/star_button_description"` and
  stays important for accessibility, so the focusable row collects it into its own label.
- **Fix:** set `android:importantForAccessibility="no"` on `@id/star` (the state is already on
  `star_click_area`). Better, fold Select and Star into row custom actions and put the starred
  state in the row's description.
- **How Inspector Widget surfaces it:** the model and the walk agreed. Lint gave only role/state
  infos on the click areas (G17).

<a id="tb-13"></a>
### TB-13: Message header star says "Button", and its state is silent

- **Screen / kind / severity:** Message view header / unlabelled / medium.
- **Confirmed in:** the tool's default start order, starred and unstarred.
- **Repro:** run `tb_start` and open "Message details demo" (tap with TalkBack off). Then run
  `iw tb-walk --package $TB --start first --utterance logcat --json -`. Star the message with
  `iw tb-scenario survive ... --mutate activate` on the same key, walk again, then unstar it.
- **What TalkBack says:** step 5 (wzu9m96) is view:165, an ImageView of 132x144 px, read as
  "Button". Once starred (the screenshot shows it filled), it still says just "Button": TalkBack
  17 does not speak `isSelected=true` for this ImageView.
- **Why:** `legacy/ui/legacy/src/main/res/layout/message_view_header.xml:57-67`: `@id/flagged`
  has no contentDescription, is 44 dp wide, and shows its state only through `setSelected`
  (`legacy/ui/legacy/src/main/java/com/fsck/k9/view/MessageHeader.java:245`).
- **Fix:** set the contentDescription from code ("Add star" / "Remove star", or "Star" with
  stateDescription "Starred" / "Not starred"). Expose it as checkable through an
  AccessibilityDelegateCompat (className CompoundButton, checkable/checked), and widen it to
  48 dp.
- **How Inspector Widget surfaces it:** the model and the walk agreed. Lint gives
  `a11y.label.missing` and `touch_target` (44 dp wide) on `#flagged` (n606 in cpn9rz).

---

## Now in Android

<a id="nia-1"></a>
### NIA-1: Onboarding topic grid: six topics can never be reached by swiping

- **Screen / kind / severity:** For you > onboarding picker ("What are you interested in?",
  3-row horizontal grid) / skip / high.
- **Confirmed in:** the tool's default start order, keyboard and swipes alike (wtmkpal matches
  wvq4h1u step for step), in 5 forward walks.
- **Repro:** run `nia_cold`, then
  `iw tb-walk --package $NIA --start Compose --max-steps 30 --until edge --utterance logcat`.
  Repeat with `--injector touch`, and backward with `--start Done --prev --until edge`.
- **What TalkBack says** (wvq4h1u):
  1. "Not selected. Compose", "Compose. Check box", "Not selected. Architecture", "Not
     selected. Android Studio & Tools".
  2. [auto-scroll] "Android Studio & Tools. Check box", then "Not selected. Compose" again,
     then "Not selected. Testing".
  3. [auto-scroll] "Testing. Check box", "Not selected. Data Storage", ... "Publishing &
     Distribution", "Android Auto", "Camera & Media", then "Done. Button. disabled. Out of list".

  Each auto-scroll lands on the next item of the **bottom row**. Six topics are reached in no
  walk, in either direction: Performance, New APIs & Libraries, Kotlin, Privacy & Security,
  Platform & Releases, Accessibility. In 4 of 5 walks, Android TV, Wear OS and Games are also
  skipped on the first lap and reached only after the wrap. Architecture's checkbox, cut off at
  the right edge, is never reached. Backward from Done (wox59ex), TalkBack reads Testing,
  Android Studio & Tools, Architecture, Compose ... Headlines, then the header, and does not
  scroll.
- **Why:** `feature/foryou/impl/src/main/kotlin/com/google/samples/apps/nowinandroid/feature/foryou/impl/ForYouScreen.kt:333-363`
  uses `LazyHorizontalGrid(rows = GridCells.Fixed(3))`, which fills items column by column.
  TalkBack follows Compose's semantics traversal order. Before the first scroll that order is
  column by column; after a scroll it runs row by row across the viewport. TalkBack
  auto-scrolls forward from the current item, which is in the bottom row, so the next stop is
  the next bottom-row item, and rows 0-1 of every column that scrolls in are passed over.
- **Fix:** lay the topics out so the linear order matches the data and scrolling is vertical:
  a FlowRow, or rows inside the feed's vertical grid. If the horizontal grid stays, make it a
  traversal group (`Modifier.semantics { isTraversalGroup = true }` on the grid,
  `traversalIndex = index.toFloat()` per item) so it reads column by column. Re-walk forward and
  with `--prev`, with both injectors, until every topic is reached.
- **How Inspector Widget surfaces it:**
  - The walk raised tb.skipped (and tb.edge_stuck with swipes), but the text output left them
    out ("findings_omitted") in favour of five tb.double_stop lines. They were found only by
    parsing the 36 KB saved walk (G3).
  - Two walks ended early on a false tb.loop error (wc8drkq, wtsk72v) (G10).
  - The tb.skipped fix text ("make the content visible") is wrong for this cause (G16).
  - Statically, the capture lists the clipped second-column rows as stops labelled only "Not
    selected" (G6), and lint gives 6 info R12. `outline --view reading` is row-major, which
    disagrees with TalkBack and with the talkback.order model (G6).
  - The model does not auto-scroll, so it diverges at the first auto-scroll (fixture
    `nia_onboarding_grid`: press 4). It does predict the backward walk 14/14.

<a id="nia-2"></a>
### NIA-2: Each topic takes two stops (row plus its own toggle)

- **Screen / kind / severity:** Onboarding picker (medium), Interests list and Search results
  (low) / double_stop.
- **Confirmed in:** the tool's default start order.
- **Repro:** run `nia_cold`, then
  `iw tb-walk --package $NIA --start first --max-steps 22 --utterance logcat`. For Interests,
  tap the tab with TalkBack off and walk `--start first --max-steps 9 --until steps`. For
  Search, search "compose" and walk `--start first --max-steps 16`.
- **What TalkBack says:**
  - Onboarding: "Not selected. Compose", then "Compose. Check box". Both toggle the follow.
  - Interests (wytgfpg): "Not selected. Accessibility. In list. 20 items", "Follow interest.
    Check box", "Not selected. Android Auto", "Follow interest. Check box", and so on.
  - Search (wahyghw): "Not selected. UI. not including Compose", "Follow interest. Check box".

  On Interests and Search the two stops do different things: the row opens the topic and the
  toggle follows it. The defect there is two stops per item, plus a toggle label that never
  names the topic (NIA-10).
- **Why:** on onboarding, `ForYouScreen.kt:385-427` uses `Surface(selected =, onClick =)`, which
  makes the row a selectable stop. It contains `NiaIconToggleButton` (`:411`), a second
  toggleable stop whose contentDescription is the topic name. On Interests and Search,
  `core/ui/src/main/kotlin/com/google/samples/apps/nowinandroid/core/ui/InterestsItem.kt:55-98`
  is a `ListItem` with `.semantics(mergeDescendants = true).clickable(open topic)` and a
  trailing `NiaIconToggleButton` (`:66-85`).
- **Fix:** on onboarding, make the row
  `Modifier.toggleable(value = isSelected, role = Role.Checkbox, onValueChange = ...)` and clear
  the inner toggle's semantics. On Interests, keep the row click for "open topic" and expose
  follow/unfollow as a custom action ("Follow $name"), clearing the trailing button's
  semantics.
- **How Inspector Widget surfaces it:**
  - tb.double_stop fires on every pair. On Interests and Search it is the "may not do what the
    inner control does" variant.
  - The model predicted both stops (9/9 presses, words included, on the fixture
    `nia_interests`).
  - Lint gives 6 info R12 on onboarding, citing the wrong label ("Not selected"), and nothing
    on Interests or Search (G6, G17).

<a id="nia-3"></a>
### NIA-3: Two-column feed: card controls are interleaved, and a Bookmark is read after the other card

- **Screen / kind / severity:** For you feed in the two-column staggered layout (width 600 dp
  and over: tablets, foldables, large display size) / out_of_order / medium.
- **Confirmed in:** the tool's default start order.
- **Repro:**
  1. `adb -s emulator-5558 shell wm density 280`, which gives two feed columns and a navigation
     rail.
  2. Relaunch, then follow Headlines and tap Done (TalkBack off).
  3. `iw tb-walk --package $NIA --start Search --max-steps 40 --until edge --step-timeout-ms 4000 --utterance logcat`.
  4. Afterwards: `adb -s emulator-5558 shell wm density reset`.
- **What TalkBack says** (wg0mhts):
  - Steps 19-22: "MAD Skills Compose: Powerful Toolkit..." (right card, compose:8:206), "Deep
    Links Crash Course: Part 3..." (left card, 8:234), "Bookmark. Check box" (8:247, the left
    card's), "Bookmark. Check box" (8:219, the right card's).
  - Steps 26-29: "Jetpack Compose Composition Tracing" (right), "State holders and UI state
    page" (left), the Bookmark of Composition Tracing, then the Bookmark of State holders.
    Right after hearing "State holders", the next "Bookmark" toggles a different card.
  - Step 18 is a real "Unlabelled": a 500x48 px sliver of a card after an auto-scroll.
- **Why:** `core/ui/src/main/kotlin/com/google/samples/apps/nowinandroid/core/ui/NewsResourceCard.kt:113-123`.
  `Card(onClick)` merges the card text into one stop, but `BookmarkButton` (`:152`,
  `:234-256`) and the topic chips (`NewsResourceTopics`, `:307-343`) stay separate stops.
  `ForYouScreen.kt:166-167` lays cards of different heights side by side in
  `LazyVerticalStaggeredGrid(StaggeredGridCells.Adaptive(300.dp))`, and TalkBack orders those
  stops by position across both columns, not card by card.
- **Fix:** make each card one stop. Expose "Bookmark <title>" and "Open topic <name>" as custom
  actions and clear the inner button's and chips' semantics (the A11yProbe C15 GOOD pattern). Or
  wrap each card's content in `Modifier.semantics { isTraversalGroup = true }`.
- **How Inspector Widget surfaces it:** the model agreed (35/40, and 17/17 up to the first
  auto-scroll on the fixture `nia_feed_two_column`), so it predicted the interleaving, but no
  finding names it. The walk gave only tb.out_of_order "2 of 13 stops" on a Bookmark, plus
  tb.ghost_stop on the sliver. Static lint found nothing (G16). Lint the fixture at 280 dpi:
  at 480 it reports 15 spurious touch-target warnings.

<a id="nia-4"></a>
### NIA-4: Interests > topic > back lands on Search (C14)

- **Screen / kind / severity:** Interests > topic > back / restore_failed / medium.
- **Confirmed in:** the tool's default start order, 2 of 2 runs.
- **Repro:** run `nia_cold`, tap the Interests tab (TalkBack off), then
  `iw tb-scenario restore --package $NIA --target "Android TV" --wait-ms 2500`.
- **What TalkBack does:** opening the topic goes compose:8:222, then null (+476 ms), then the
  topic screen's "Back" (+1429 ms). After back, focus goes null (+862 ms) and then lands on
  "Search" (compose:8:455, +989 ms). The "Android TV" row is still on screen at the same place.
- **Why:** `feature/interests/impl/src/main/kotlin/com/google/samples/apps/nowinandroid/feature/interests/impl/TabContent.kt:58-80`.
  The LazyColumn state survives the Navigation3 list-detail round trip
  (`navigation/InterestsEntryProvider.kt:33-34`, `ListDetailSceneStrategy`). Nothing puts
  accessibility focus back on the row, and because the window is the same, TalkBack starts over
  at the top.
- **Fix:** remember the opened topic id (`rememberSaveable`). When the list pane returns and the
  row is laid out, send ACTION_ACCESSIBILITY_FOCUS to its semantics node through the host view's
  `accessibilityNodeProvider`, as A11yProbe C14 GOOD does. TalkBack 17 ignores FocusRequester
  and paneTitle for this.
- **How Inspector Widget surfaces it:** `tb-scenario restore` gives verdict `top` and
  tb.restore_failed, with the correct single-activity diagnosis. With the default `--wait-ms`,
  the hunt's run pressed back during the transition (G13).

<a id="nia-5"></a>
### NIA-5: Removing a bookmark throws focus to the navigation bar, far from Undo

- **Screen / kind / severity:** Saved, remove a bookmark (list shrinks, Undo snackbar) /
  focus_lost_on_update / medium.
- **Confirmed in:** the tool's default start order.
- **Repro:** run `nia_cold`, finish onboarding, bookmark one or two cards, and open Saved (all
  with TalkBack off). Then run
  `iw tb-scenario focus-after --package $NIA --target Unbookmark --action activate --wait-ms 3000`.
  To hear the snackbar, add `--leave-on`, then run
  `iw tb-walk --package $NIA --start current --max-steps 12 --until steps --leave-on`, then
  `iw talkback restore`.
- **What TalkBack does:** focus goes compose:8:1706, then null (+82 ms), then the "Saved" tab in
  the bottom bar (+164 ms), and an "Alert" pane (the snackbar) appears (tenf30l). On the last
  card it goes to "Search" (+637 ms) instead. From Search, UNDO is 6 swipes away: Search,
  Saved, Settings, "No saved updates", "Updates you save…", "Bookmark removed", "UNDO. Button"
  (wu79mcx). The snackbar is `SnackbarDuration.Short`. Its duration on screen was not
  measured, and `accessibility_interactive_ui_timeout_ms` was unset.
- **Why:** `feature/bookmarks/impl/src/main/kotlin/com/google/samples/apps/nowinandroid/feature/bookmarks/impl/BookmarksScreen.kt:119-131`
  and `:177-187` remove the focused card with no focus handoff.
  `navigation/BookmarksEntryProvider.kt:35-39` shows Undo with `duration = Short`.
- **Fix:** after the removal, focus the next card, else the empty-state text, else the
  snackbar's Undo. Use `SnackbarDuration.Long` or `Indefinite` (with `withDismissAction`) when
  accessibility is on.
- **How Inspector Widget surfaces it:**
  - The focus timeline is only in the saved scenario file, not on stdout (G11).
  - The verdict says tb.initial_focus "focus is on close or unlabeled (compose:8:19)", but that
    node is the labelled Saved tab (G12).
  - The chained walk fell back to model speech (G2).

<a id="nia-6"></a>
### NIA-6: Onboarding Done resets focus to the top

- **Screen / kind / severity:** For you onboarding, Done / focus_lost_on_update / low. When the
  focused node disappears, TalkBack falling back to the first stop is ordinary behaviour, so
  whether this is a defect is a judgement call.
- **Confirmed in:** the tool's default start order.
- **Repro:** run `nia_cold`. Follow Headlines, UI and Compose by tapping (TalkBack off). Then
  run `iw tb-scenario focus-after --package $NIA --target Done --action activate`.
- **What TalkBack does:** focus goes from Done to null (+271 ms), then to "Search" at the top
  (+1000 ms) (ta6abda, t1eiloy). The feed is left scrolled, with the first card's title and
  Bookmark above the viewport (see NIA-11).
- **Why:** `ForYouScreen.kt:296-310`: `NiaButton(onClick = saveFollowedTopics)` removes the
  whole onboarding item, focused button included, and nothing receives focus.
- **Fix:** after saving, focus the first feed card (scrolled into view), or announce "Your feed
  is ready" and then focus it.
- **How Inspector Widget surfaces it:** verdict `initial_ok`. The tool counts a reset to the
  first stop as success (G12).

<a id="nia-7"></a>
### NIA-7: Topic chips announce follow state, but open the topic

- **Screen / kind / severity:** News card topic chips (For you, Saved, Search, topic screens) /
  wrong_announcement / medium.
- **Confirmed in:** the tool's default start order.
- **Repro:** run `nia_cold` and finish onboarding. Run
  `iw tb-walk --package $NIA --start first --max-steps 8 --utterance logcat`, then
  `iw tb-scenario focus-after --package $NIA --target <the chip's key from the walk> --action activate`.
  Use the key: a label target fails here (G4).
- **What TalkBack says:** "Wear OS is not followed. Button" (wrxqt1m step 5, wahyghw step 11),
  which suggests that activating it follows the topic. Activating it opens the Wear OS topic
  screen instead, with "Back" focused, and the topic is still not followed (tf055yi).
- **Why:** `NewsResourceCard.kt:318-339`: `NiaTopicTag(onClick = onTopicClick)` navigates to the
  topic, but its contentDescription is "%1$s is followed" / "%1$s is not followed"
  (`core/ui/src/main/res/values/strings.xml:27-28`), which describes state, not the action.
- **Fix:** label the chip with the topic ("Wear OS"), move followed/not followed to
  stateDescription if it is needed, and name the action
  (`Modifier.semantics { onClick(label = "open topic", action = null) }`).
- **How Inspector Widget surfaces it:** statically it is a Button "Wear OS is not followed", and
  no lint rule questions it (G17).

<a id="nia-8"></a>
### NIA-8: Topic screen follow chip says the state twice and never the action

- **Screen / kind / severity:** Topic screen follow chip / wrong_announcement / low.
- **Confirmed in:** the tool's default start order.
- **Repro:** on the Interests tab, run
  `iw tb-scenario focus-after --package $NIA --target "Android TV" --action activate --wait-ms 2500`,
  then `iw tb-walk --package $NIA --start first --max-steps 5 --until steps --utterance logcat`.
- **What TalkBack says:** "Not selected. NOT FOLLOWING. Check box" (wmpmduh step 1).
- **Why:** `feature/topic/impl/.../TopicScreen.kt:305-315` puts the state itself
  ("FOLLOWING" / "NOT FOLLOWING") as the text of `NiaFilterChip(selected)` (an M3 FilterChip,
  `core/designsystem/.../component/Chip.kt:45-75`). "Not selected ... Check box" is stock
  FilterChip semantics. The app's defect is the state-as-label text.
- **Fix:** use a fixed action label ("Follow" or "Follow Android TV") and let the chip's
  selected state carry followed versus not followed.
- **How Inspector Widget surfaces it:** the capture outline and `node` (n2593) show Checkbox "Not
  selected", with the text dropped (G6). No lint finding.

<a id="nia-9"></a>
### NIA-9: Every Interests row says "Not selected"

- **Screen / kind / severity:** Interests list / wrong_announcement / low.
- **Confirmed in:** the tool's default start order.
- **Repro:** run `nia_cold`, open Interests, then
  `iw tb-walk --package $NIA --start first --max-steps 9 --until steps --utterance logcat`.
- **What TalkBack says:** "Not selected. Accessibility. In list. 20 items", "Not selected.
  Android Auto", "Not selected. Android Studio & Tools", "Not selected. Android TV".
- **Why:** `InterestsItem.kt:95-97` always sets `selected = isSelected`, and
  `navigation/InterestsEntryProvider.kt:47` hard-codes `shouldHighlightSelectedTopic = false` (a
  TODO), so the state is false everywhere, even in two-pane layouts.
- **Fix:** set `selected` only where the highlight is shown (pass a flag and do
  `if (highlight) selected = isSelected`).
- **How Inspector Widget surfaces it:** the capture labels the rows "Not selected" only (G6).
  Lint finds nothing.

<a id="nia-10"></a>
### NIA-10: Bookmark and follow toggles flip their label and never name their item

- **Screen / kind / severity:** Bookmark and follow-interest toggles (feed, Saved, Search,
  Interests) / wrong_announcement / low.
- **Confirmed in:** the tool's default start order.
- **Repro:** bookmark a card, then walk over it with `--utterance logcat`. Or search "compose"
  and walk.
- **What TalkBack says:** "checked. Unbookmark. Check box", "Bookmark. Check box", "checked.
  Unfollow interest. Check box", "Follow interest. Check box". The label flips with the state
  and contradicts it. It never names the article or topic, so every bookmark sounds the same.
- **Why:** `NewsResourceCard.kt:234-256` (strings.xml:18-19) and `InterestsItem.kt:66-85`
  (strings.xml:30-31) put Bookmark/Unbookmark and Follow/Unfollow on a toggleable with
  `Role.Checkbox`.
- **Fix:** use one stable label that names the item ("Bookmark <title>", "Follow <topic>") and
  let the checked state speak, or fold the toggle into the card or row as a custom action
  (NIA-2, NIA-3).
- **How Inspector Widget surfaces it:** statically it is a Checkbox "Unbookmark", checked. Lint's
  R12 is silent by design, and no rule compares a label with its toggle state (G17).

<a id="nia-11"></a>
### NIA-11: Partly scrolled cards lose their Bookmark, or are read twice

- **Screen / kind / severity:** For you feed, single column, with cards partly scrolled off /
  skip / low.
- **Confirmed in:** the tool's default start order. The skip needs a walk that starts with a
  card partly scrolled off, which is the state onboarding's Done leaves.
- **Repro:** run `nia_cold` and finish onboarding. With TalkBack off, fling the feed to the
  bottom (`adb -s emulator-5558 shell input swipe 640 2400 640 300 40`, repeated). Then run
  `iw tb-walk --package $NIA --start first --until edge --utterance logcat`. Also walk 60 steps
  from the top after a fresh launch.
- **What TalkBack says:** w4jv0zv step 3 reads the top card as "May 24, 2021 • Article 📚. This
  year's Google I/…", without its title, followed by "Headlines is followed. Button". Its
  Bookmark is never reached. In wtt0adx, steps 58-60 read compose:8:516 "Build Tiles fast…" in
  full, then its Bookmark (via auto-scroll), then the same card again as "Tiles are one of the
  most used surfaces…".
- **Why:** `NewsResourceCard.kt:113-170`: tall cards (up to about 2000 px) merge their text into
  one stop but keep the BookmarkButton at the top as a separate stop (`:152`). When the top of a
  card is scrolled off, its bookmark is off screen and TalkBack moves past it.
- **Fix:** the same as NIA-3. Make the bookmark a custom action of the card.
- **How Inspector Widget surfaces it:** the model predicted the second reading
  (`compose:8:159#1`). tb.revisit flagged other cards' same-label "Bookmark" and "HEADLINES"
  stops instead, and missed this same-key re-read (G22).

<a id="nia-13"></a>
### NIA-13: The Interests list counts a spacer as an item

- **Screen / kind / severity:** Interests list / wrong_announcement / low. Found while
  reproducing NIA-9. It is the same class as TB-1.
- **Confirmed in:** the tool's default start order.
- **Repro:** as NIA-9, then read the first row's full speech.
- **What TalkBack says:** "Not selected. Accessibility. In list. 20 items" for 19 topics
  (wytgfpg step 3).
- **Why:** `TabContent.kt:50,83`: `withBottomSpacer = true` adds a Spacer item to the LazyColumn,
  which is counted in its CollectionInfo.
- **Fix:** use `contentPadding` (or a spacer outside the lazy list) instead of a spacer item.
- **How Inspector Widget surfaces it:** it doesn't. tb.wrong_announcement does not parse "In
  list. N items", and no static rule compares item counts (G8). The fixture `nia_interests`
  holds it.

---

## AntennaPod

<a id="ap-1"></a>
### AP-1: The collapsed player's show notes are 66 invisible stops before the mini player

- **Screen / kind / severity:** Home, Subscriptions, Queue and feed lists (any main screen with
  the collapsed mini player) / ghost_stop / high.
- **Confirmed in:** both start orders, keyboard and swipes. At least one played episode is
  needed so that the mini player shows.
- **Repro:**
  1. `iw talkback on --verbose-log`, then `ap_start`.
  2. `iw tb-walk --package $AP --start first --until edge --max-steps 45 --utterance logcat --leave-on`
  3. `iw tb-walk --package $AP --start first --prev --max-steps 9 --utterance logcat --leave-on`
  4. `iw talkback restore`
- **What TalkBack says:**
  - wr91td6: step 32 is the last Home item, "You can download any episode to listen to it
    offline.". Step 33 is "Webview" (virtual:805:140), followed by "If your fridge can be
    bricked…", "Bullet. 1 of 19. In list. 19 items", and so on. This is the collapsed player's
    show notes. Every node is 0 px tall at y=4564, below the 2856 px screen.
  - On Subscriptions (w740pd2) there are exactly 66 such stops between "Add podcast" and the
    mini player.
  - Backward (wbunyhi), previous from the mini player lands on "coveron.com/thisweekintech…
    link".
  - TalkBack's log shows ACTION_ACCESSIBILITY_FOCUS returning true and
    TYPE_VIEW_ACCESSIBILITY_FOCUSED following, but no focus box appears on screen.
- **Why:** `app/src/main/java/de/danoeh/antennapod/ui/screen/playback/audio/AudioPlayerFragment.java:152`
  calls `pager.setOffscreenPageLimit(NUM_CONTENT_FRAGMENTS)` (2, `:86`), which keeps
  `ItemDescriptionFragment`'s `ShownotesWebView` alive inside `#playerContent`. The bottom-sheet
  callback (`app/src/main/java/de/danoeh/antennapod/activity/MainActivity.java:299-332`) calls
  `fadePlayerToToolbar` (`AudioPlayerFragment.java:533-541`), which only fades `#playerFragment`
  and the toolbar. Nothing hides `#playerContent` from accessibility while the sheet is
  collapsed, and the app sets importantForAccessibility nowhere. Only the WebView leaks:
  Chromium's virtual nodes report `visible_to_user` while off screen, and off-screen native
  Views do not.
- **Fix:** in `AntennaPodBottomSheetCallback.onStateChanged` / `onSlide`, set
  `playerContent.setImportantForAccessibility(slideOffset == 0f ? IMPORTANT_FOR_ACCESSIBILITY_NO_HIDE_DESCENDANTS : IMPORTANT_FOR_ACCESSIBILITY_AUTO)`,
  and do the opposite for `#playerFragment` (the mini player) when expanded.
- **How Inspector Widget surfaces it:**
  - The walk raises tb.ghost_stop per stop, but the per-step markers vanish once the finding
    cap is hit (G3).
  - The talkback.order model predicts all 66 stops, and the existing fixture
    `antennapod_home_player_collapsed` pins this.
  - Statically, a cold-start capture with TalkBack off has 0 WebView nodes (ckk688). After one
    expand and collapse (cxb73v), or with TalkBack on (cqhjb8), it lists the 66 stops with 0
    issues, and lint is silent (G7).

<a id="ap-2"></a>
### AP-2: Expanded player: swiping past the last control reads the Home screen behind it

- **Screen / kind / severity:** Expanded player (bottom sheet expanded over Home) / escape /
  high.
- **Confirmed in:** both start orders, keyboard and swipes (wmp8nha).
- **Repro:**
  1. Tap the mini player with TalkBack off.
  2. `iw capture --package $AP --label p`, then `iw find -c p --rid butSkip --fields +ids`.
  3. `iw tb-walk --package $AP --start view:<butSkip key> --max-steps 6 --until steps --utterance logcat`.
- **What TalkBack says:** "Skip episode. Button", then "Home", "Search. Button", "More options.
  Button", and the Home cards ("Continue listening. Heading" in wwghs3b). Focus leaves the
  full-screen player and reads the Home screen hidden behind it (wcegl24).
- **Why:** `MainActivity.java:299-317` (`AntennaPodBottomSheetCallback.onStateChanged`): on
  `STATE_EXPANDED`, it only enables the back callback. `#main_content_view`, the sheet's sibling
  in `main.xml`, stays important for accessibility.
- **Fix:** on STATE_EXPANDED, set `main_content_view` to
  `IMPORTANT_FOR_ACCESSIBILITY_NO_HIDE_DESCENDANTS`. Restore AUTO on COLLAPSED and HIDDEN. Give
  the expanded sheet an `accessibilityPaneTitle` ("Player").
- **How Inspector Widget surfaces it:** the walk raises tb.escape: "overlay view:12
  (#audioplayerFragment, 91%)". The capture's reading order lists the covered Home content as
  stops after Skip, which is right, but neither the capture nor lint marks those nodes as
  covered. Lint even reports touch-target errors on them (n4499, 0.7x50.7 dp, in ckv9fz) (G5).

<a id="ap-3"></a>
### AP-3: Expanded player: 66 off-screen show-notes stops come before the seek bar and controls

- **Screen / kind / severity:** Expanded player, cover page (a vertical ViewPager2 whose
  show-notes page sits below) / ghost_stop / high.
- **Confirmed in:** both start orders.
- **Repro:**
  1. `iw talkback on --verbose-log`, then `ap_start`.
  2. `iw tb-scenario focus-after --package $AP --target <mini player key> --action activate --leave-on`
  3. `iw tb-walk --package $AP --start current --max-steps 75 --utterance logcat --leave-on`
  4. `iw talkback restore`
- **What TalkBack says** (w21bfni, wuy5z8f): "swipe up to read shownotes. Shownotes", then
  "Webview", "If your fridge…", the bullets, and so on: 66 WebView stops. Only after them come
  "0%. Slider. Out of list", "Position: 0 minutes", ... "Skip episode". With TalkBack enabled
  after the app started (ws8ot3m), the walk reads the same stops (steps 3-10 are slivers or 0 px
  at y=2821-2856).
- **Why:** `AudioPlayerFragment.java:149-163`: the vertical ViewPager2 keeps the show-notes page
  laid out off screen (`setOffscreenPageLimit(2)`), and `onPageSelected` only calls
  `updateScrollingChild`. ViewPager2 does not hide pages that are not current from
  accessibility.
- **Fix:** in `onPageSelected`, set NO_HIDE_DESCENDANTS on every page but the current one (AUTO
  on the current one), or keep the description WebView GONE until its page is selected. The
  "Shownotes" button already gives a way in.
- **How Inspector Widget surfaces it:**
  - The model puts the 66 WebView stops between Shownotes and the seek bar, which matches.
  - Its `web_hidden_page` hint calls this spot a trap (`Navigator.traps`), but none of these
    walks were trapped (G7).
  - Ghost flagging starts only at step 3. The first two stops overlap the controls but lie
    below the pager's clip, and nothing flags them (G7).

<a id="ap-4"></a>
### AP-4: Episode details: neighbouring episodes' notes are read off screen, and focus sticks when TalkBack started later

- **Screen / kind / severity:** Episode details (ItemPagerFragment, a horizontal pager of
  episodes with a show-notes WebView) / ghost_stop for a TalkBack user, trap when TalkBack
  started after the app / high.
- **Repro:**
  1. `iw talkback on --verbose-log`, then `ap_start`.
  2. From Home > Continue listening, open the second episode (TWiT 1102) with
     `iw tb-scenario focus-after --package $AP --target <row key> --action activate --leave-on`.
  3. `iw tb-walk --package $AP --start current --max-steps 12 --utterance logcat --leave-on`
  4. `iw talkback restore`

  For the other start order, open the episode with TalkBack off and run
  `iw tb-walk --package $AP --start view:<Back key> --max-steps 12 --until steps --utterance logcat`.
- **What TalkBack says:**
  - TalkBack first (w3cswcd): "Back", "Visit website", "More options", then
    virtual:1034:826 "Webview" at [-1280,788 1280x1613], "If your fridge can be bricked…" and
    bullets. These are the previous episode's (TWiT 1103) show notes, all at x<0, while TWiT
    1102 is on screen.
  - The ViewPager2 page root is also a stop of its own: "Page. 3 hours 5 minutes. September 27,
    2026. Webview" (wzi9apx step 3, first episode). On the first episode, TalkBack enters the
    current page's notes after "Download" (wzi9apx, woozepy).
  - TalkBack enabled after the app started: stuck after "Download", 3 of 3 runs (w9wtb7e,
    wccs0h4, wobdltt). Two presses move nothing and say nothing. TalkBack's log shows
    ACTION_ACCESSIBILITY_FOCUS on the **current** page's WebView root returning true, with no
    TYPE_VIEW_ACCESSIBILITY_FOCUSED after it.
- **Why:** `app/src/main/java/de/danoeh/antennapod/ui/screen/episode/ItemPagerFragment.java:108`
  calls `pager.setOffscreenPageLimit(1)`, which keeps the neighbouring episodes' pages, each with
  its own `ShownotesWebView` (`feeditem_fragment.xml:214`), in the accessibility tree at
  x=±1280. The "Page" stop exists because ViewPager2's internal RecyclerView marks each page
  root importantForAccessibility=YES with CollectionItemInfo when accessibility is on at bind
  time: view:872 differs exactly this way between the fixtures
  `antennapod_episode_details_tb_first` and `_tb_later`. The mechanism of the later-start trap
  is unknown. It is on the current page, so hiding the off-screen pages will not address it.
- **Fix:** in `onPageSelected`, hide the pages that are not current
  (NO_HIDE_DESCENDANTS; AUTO on the current one), and make the page root non-focusable so it
  stops being a "Page…" stop.
- **How Inspector Widget surfaces it:**
  - The model predicted entering the WebView after Download, and on the second episode the
    left page's WebView right after the toolbar. It lands on the same node as the TalkBack-first
    walk at all 14 presses (fixture `antennapod_episode_details_tb_first`). The words differ
    only where TalkBack says "Bullet" and the model "•".
  - It diverges from the later-start walk at press 3: TalkBack skipped the "Page" stop and read
    "3 hours 5 minutes" and the date as separate stops (G1).
  - Stuck, the walk gave a generic tb.edge_stuck with "Expose scrolling" advice. tb.trap did not
    fire (G7).
  - Every walk here also raised false tb.escape / tb.ghost_stop findings ("occluded by
    view:898"). view:898 is the empty loading FrameLayout (G5).

<a id="ap-5"></a>
### AP-5: The play/pause button says "Pause" while paused

- **Screen / kind / severity:** Mini player and expanded player play/pause / wrong_announcement
  / medium.
- **Confirmed in:** both start orders. That activating the button starts playback was not
  re-checked.
- **Repro:** play an episode, pause it, then `ap_start`. Then run
  `iw tb-walk --package $AP --start first --prev --max-steps 9 --utterance logcat`.
- **What TalkBack says:** "Pause. Button. Out of list" on the mini player while the icon is ▶
  and the media session is PAUSED (wbunyhi step 7, w740pd2 step 74). The expanded player also
  says "Pause. Button" (wuy5z8f step 69).
- **Why:** `app/src/main/res/layout/external_player_fragment.xml:62` and
  `audioplayer_fragment.xml:152` set `android:contentDescription="@string/pause_label"` while the
  src is `ic_play_48dp`. In
  `app/src/main/java/de/danoeh/antennapod/ui/screen/playback/PlayButton.java:12`, `isShowPlay`
  starts true, so `setIsShowPlay(true)` returns at the guard (`:32`) and never sets the
  description.
- **Fix:** set `@string/play_label` in both layouts, and move `setContentDescription` outside
  the `isShowPlay != showPlay` guard so the first call always applies it.
- **How Inspector Widget surfaces it:** the capture shows the label "Pause" (n3600 in ckv9fz).
  No rule compares a toggle's label with its icon or the playback state. It was found by
  looking at a screenshot (G17).

<a id="ap-6"></a>
### AP-6: Back from an episode does not return focus to its row

- **Screen / kind / severity:** Feed episode list > episode details > back / restore_failed /
  medium.
- **Confirmed in:** TalkBack first.
- **Repro:** go to Subscriptions > Planet Money. Get the row key with
  `iw find -c <capture> --text Middlegarchs --fields +ids`, taking the clickable row rather than
  an inner layout. Then run
  `iw tb-scenario restore --package $AP --target view:<row key> --wait-ms 2500`.
- **What TalkBack does:** activating the row opens the details (focus view:1511). After back,
  focus lands on "Back, Button" (view:1201), not the "Middlegarchs are the new Oligarchs" row
  (txmhpy6). The same happens on Home, where back from details lands on the "Home" title.
- **Why:** `MainActivity.java:505-516` (`loadChildFragment`) hides the list fragment, adds the
  details and calls `addToBackStack(null)`. The details are opened from
  `app/src/main/java/de/danoeh/antennapod/ui/episodeslist/EpisodeItemListAdapter.java:88-92`.
  Nothing restores accessibility focus when the back stack pops, although the row View still
  exists.
- **Fix:** remember the clicked item id. In `onHiddenChanged(false)`, post
  `findViewHolderForItemId(id).itemView.performAccessibilityAction(ACTION_ACCESSIBILITY_FOCUS, null)`.
- **How Inspector Widget surfaces it:** `tb-scenario restore` gives verdict `top` and
  tb.restore_failed. A label target ("Middlegarchs") resolved to an inner LinearLayout, so the
  activation did nothing (G4).

<a id="ap-7"></a>
### AP-7: Feed header buttons are read before the title above them

- **Screen / kind / severity:** Feed (podcast) header / out_of_order / low.
- **Confirmed in:** TalkBack first.
- **Repro:** in the Planet Money feed, run
  `iw tb-walk --package $AP --start view:<More options key> --max-steps 8 --until steps --utterance logcat`.
- **What TalkBack says** (w9fp7sl): "Show information. Button", "Filter. Button", "Show podcast
  settings. Button" (y=672) come before "Planet Money" (y=479) and "NPR" (y=567).
- **Why:** `app/src/main/res/layout/feeditemlist_header.xml:19-26` declares `#buttonContainer`
  (`alignParentBottom`) before the title/author LinearLayout (`:126-130`, `layout_above`), and
  accessibility order follows declaration order in the RelativeLayout.
- **Fix (untested):** declare the title container first, or set
  `android:accessibilityTraversalBefore="@id/buttonContainer"` on it.
- **How Inspector Widget surfaces it:** the walk raises tb.out_of_order "2 of 6 stops", and the
  capture's reading order predicted the inversion. The static side does not flag it.

<a id="ap-8"></a>
### AP-8: Download and Stream have no role

- **Screen / kind / severity:** Episode rows and episode details / wrong_announcement / low.
- **Confirmed in:** TalkBack first.
- **What TalkBack says:** "Download" on feed rows (w9fp7sl steps 9, 11), and "Stream",
  "Download" on details (wzi9apx steps 6-7), all with no role.
- **Why:** `app/src/main/res/layout/secondary_action.xml`'s root is a clickable FrameLayout
  (included at `feeditemlist_item.xml:219-221`). `feeditem_fragment.xml:103-104` (`#butAction1`)
  and `:132-133` (`#butAction2`) are plain LinearLayouts.
- **Fix:** give them a Button class through an AccessibilityDelegate
  (`info.setClassName("android.widget.Button")`), or use MaterialButton / ImageButton.
- **How Inspector Widget surfaces it:** lint `a11y.role.missing_on_clickable` (info) on all
  three, which agrees with the live speech.

<a id="ap-9"></a>
### AP-9: The episode filter sheet opens on "Clear" with no title

- **Screen / kind / severity:** Episode filter bottom sheet / other (missing title) / low.
- **Confirmed in:** TalkBack first.
- **Repro:** run
  `iw tb-scenario focus-after --package $AP --target view:<Filter button key> --action activate`.
- **What TalkBack says:** a second window opens with no panes. TalkBack's ROLE_DIALOG
  window-state feedback is empty, and focus lands on "Clear. Button" (`isInitialFocus=true`).
  Nothing names the sheet. Back returns focus to Filter, which is correct.
- **Why:** `app/src/main/res/layout/filter_dialog.xml:18-19` makes `#clearFilterButton` the first
  child. `ItemFilterDialog` (a BottomSheetDialogFragment) sets no title or pane title.
- **Fix:** add a visible "Filter episodes" heading above Clear, or set the dialog's pane title
  or window title.
- **How Inspector Widget surfaces it:** verdict `initial_ok`, with no finding about the missing
  title. The empty announcement needed a logcat tail (G11).

<a id="ap-11"></a>
### AP-11: Expanded player seek bar is unlabelled, and its numbers are separate stops

- **Screen / kind / severity:** Expanded player seek bar / unlabelled / low.
- **Confirmed in:** TalkBack first, from logcat speech.
- **Repro:** as AP-3. Reach the slider by navigation, not as the walk's start, because the start
  step's speech is always the model's.
- **What TalkBack says** (wuy5z8f steps 62-71): "0%. Slider. Out of list", "Position: 0
  minutes", "Duration: 3 hours 5 minutes", "Playback speed. Button", "1.00", "Rewind. Button",
  "10", "Fast-forward. Button", "30". Each is a separate stop.
- **Why:** `app/src/main/res/layout/audioplayer_fragment.xml:88-96`: `#sbPosition` has no
  contentDescription, and `ChapterSeekBar` has no accessibility code.
- **Fix:** label it "Playback position", with a stateDescription giving elapsed and total
  time. Hide the numeric captions and fold them into the button labels ("Rewind 10 seconds").
- **How Inspector Widget surfaces it:** lint `a11y.label.missing` on `#sbPosition` (n3599),
  which agrees.

---

## Inconclusive

- **NIA-12, a bookmark toggle on the For you feed once threw focus to the "For you" tab**
  (thzyteh). This happened in 1 of 8 runs. Three repeats in the hunt and four in the
  reproduction kept focus on the toggle. Treat it as suspected until it repeats.
- **AP-10, queue reordering may be impossible with TalkBack.** The static facts hold:
  - Queue rows expose only CLICK, LONG_CLICK and SHOW_ON_SCREEN (n5499 in ce29nq).
  - `#drag_handle` is `importantForAccessibility="no"` (`feeditemlist_item.xml:37`).
  - `QueueRecyclerAdapter.java:47-60` starts a drag only from a raw touch on the handle or
    the cover.
  - The long-press menu offers only "Move to top/bottom".

  Nobody tested TalkBack's double-tap-and-hold pass-through drag, so the user impact is
  unproven. The fix, if it is confirmed, is "Move up" / "Move down" accessibility actions on the
  row.

## Refuted claims and sub-claims

Each line is a claim, or part of one, that the reproduction did not uphold, and why.

- The Thunderbird Compose star is a 48x24 dp touch target: its touch and accessibility bounds
  are 48x48 dp. This is a lint false positive (G18).
- TB-8: lint missed the avatar's 40x40 dp size. The accessibility node TalkBack focuses is
  48x48 dp, so not flagging it is correct.
- TB-1: "N of M" is only spoken in selection mode. Real TalkBack users hear it on every row.
  The claim came from the tool turning TalkBack on after the list was bound (G1).
- TB-3: nothing is announced on selection. TalkBack says "1 selected" once. What is missing is
  the state on the row and a label on the check button.
- TB-4: Delete also lands on a hidden "Navigate up". After Delete the action mode closes, so
  "Navigate up" is visible by then.
- TB-5: no announcement at all. TalkBack speaks the window title. Nothing announces the
  deletion.
- TB-2 with View rows: focus is lost and reassigned. Focus was on view:32 the whole time.
- NIA-1: swipes miss more topics than keys (Android TV, Games, Wear OS). Both injectors miss
  these in 4 of 5 runs, so this is run-to-run variation at the end of the grid.
- NIA-1: the order is row-major. It is column-major until the grid first scrolls.
- NIA-1: Android Studio & Tools' checkbox is skipped. It is reached after the first
  auto-scroll. Only Architecture's checkbox is never reached.
- NIA-2: on Interests and Search, both stops do the same thing. They do not: the row opens the
  topic and the toggle follows it.
- NIA-4: with the default wait, focus ends on the "Interests" tab. Both reproduction runs ended
  on Search.
- NIA-5: UNDO is at least 9 swipes away. It is 6 when focus lands on Search, and the distance
  varies. The 4 s snackbar time was assumed, not measured.
- NIA-11: the Pixel Watch card's Bookmark is skipped from a fresh launch. That skip happens
  only when the feed starts partly scrolled.
- C15, the NiA For-you grid loops at the bottom: no loop, in one or two columns. The only "loop"
  was the tool's false tb.loop (G10).
- AP-1: a hard wall at the last item when TalkBack is enabled after app start. Eight walks from
  a fresh state went into the ghost stops instead. The hunt's walls came from a long-lived
  process in an unknown state, and one (wils3xg) had TalkBack already on.
- AP-3: a trap after "Shownotes" when TalkBack is enabled after app start. ws8ot3m moved
  through the show notes. The hunt's trap walk (wvlytwx) fell back to the classic keymap after
  its first press did not move (G15).
- AP-4: a trap on the first episode for real users. With TalkBack first, TalkBack enters the
  notes (wzi9apx, woozepy). The trap appears only when TalkBack started later, and it is on the
  current page.
- AP-11: the slider speech came from the model. It was confirmed from logcat when the slider
  was reached by navigation.

## Earlier observations re-checked

| Claim | Verdict | Evidence |
|---|---|---|
| NiA Interests > topic > back lands on Search (C14) | reproduced | NIA-4. Same class in Thunderbird (TB-2) and AntennaPod (AP-6) |
| NiA For-you grid pages and loops at the bottom (C15) | not reproduced | No loop in one or two columns (winw8nb, wlmxrsu, wems6uy). The real feed problems are NIA-3 and NIA-11 |
| Thunderbird list "N of M" off by one, first message "2 of 21" (V12) | reproduced | TB-1, with 6, 5 and 4 rows. The cause is the empty in-app notification banner item. Debug, daily and beta builds only |
| AntennaPod offscreen pager WebView: ghost stops on Home, trap on the expanded player (C16/V13) | ghost stops reproduced; trap not reproduced on emulator-5558 | AP-1, AP-3. The trap was seen on emulator-5554 (fixture `antennapod_player_expanded`) and in one confounded hunt walk; three walks here moved through |
| Thunderbird Compose star unlabelled per lint (never checked against TalkBack) | reproduced | TB-6: TalkBack says "Button", before and after toggling |
| Thunderbird Compose star 48x24 dp per lint | not reproduced | Lint false positive (G18) |
| A clickable row with its own checkbox (C1) | reproduced | Thunderbird rows (TB-8, TB-12) and NiA topics (NIA-2) |

---

## Tool gaps from the real-app hunt

This is the next improvement backlog, deduplicated across the three apps and both stages, and
ranked within each tier. Function names are in `host/inspector_widget/` unless a path says
otherwise.

### High

**G1. Walks record a different app than a TalkBack user gets, because TalkBack is turned on
after the app has started, and the output never says so.**
- *Evidence:*
  - RecyclerView attaches its item delegate (CollectionItemInfo, and importantForAccessibility
    YES on the item root) only to rows bound while accessibility is on
    (`attachAccessibilityDelegateOnBind`).
  - Thunderbird: with TalkBack later (wygfouz), "In list. 6 items" and no row positions. With
    TalkBack first (wcw61ax), "2 of 6" through "6 of 6". After a selection rebinds one row, only
    that row is numbered (wdvjrk4). tb.wrong_announcement can fire only when positions are
    spoken, so it is silent in the default flow.
  - AntennaPod episode details: with TalkBack first, the page root is a stop and TalkBack
    enters the WebView (wzi9apx, woozepy). With TalkBack later, there is no page stop and it is
    stuck after Download, 3 of 3 (w9wtb7e, wccs0h4, wobdltt). The two dumps differ in exactly
    those item delegates.
  - The model matches the TalkBack-first walk 14/14 and diverges from the later one at press 3,
    so the model cannot tell the two apart either.
  - The hunt's AntennaPod walls and traps came from the later start.
- *Improvement:*
  - Add `--relaunch` to `tb-walk` / `tb-scenario`, and `relaunch=true` to MCP `tb_walk` /
    `tb_scenario`. It turns TalkBack on, force-stops the package, starts its launcher activity
    (resolve it with `cmd package resolve-activity`, since Thunderbird's is an alias), waits for
    the foreground, then runs. Put it in `talkback/walk.py run_walk` and
    `talkback/scenarios.py run_scenario`, sharing the snapshot and restore in
    `talkback/device.py`.
  - Record `talkback_started: before_app | after_app` in every result, by comparing the app
    process start time (`/proc/<pid>/stat`) with the time TalkBack was enabled, and print it.
  - Warn on `after_app` when a RecyclerView has CollectionInfo but its bound item roots lack
    CollectionItemInfo: "rows were bound before TalkBack started: positions and page stops will
    differ for a real user".
  - Mark tb.trap / tb.edge_stuck seen only `after_app` as "unverified for real users".
  - Fixtures: `thunderbird_list_compose_tb_first` / `_tb_later` and
    `antennapod_episode_details_tb_first` / `_tb_later`.

**G2. What TalkBack really said is hidden by default, and cut off where the defects are.**
- *Evidence:*
  - With the default `--utterance auto`, a walk fell back to model speech with no note
    (w5l1y76). The logcat walk (wxvgdx1) showed "In list. 7 items" and "Out of list", at 3x the
    time (29.9 s against 10.6 s for 25 steps).
  - Walk lines cut speech at about 24 characters and `outline --view reading` cuts labels at
    about 48. Positions, counts and "[attachment_icon]" sat in the cut-off tail, visible only in
    the saved walk JSON (40-120 KB).
  - Chaining after `--leave-on` loses logcat ("log level unchanged: TalkBack was already on";
    wu79mcx), because the verbose level can only be set while TalkBack is off.
- *Improvement:*
  - In `talkback/walk.py compact()` / `_line()`, keep the head and the tail of each utterance
    ("Localpart…| … 2 of 6. In list. 6 items") and add a `fields=speak` / `--full-speech`
    option.
  - When `auto` falls back to the model, say so in `notes` with a rerun hint.
  - Have `talkback on` set the verbose log level by default and keep it across `--leave-on`,
    so chained calls still read logcat.

**G3. The summaries drop the decisive findings and keep the noisy ones.**
- *Evidence:*
  - Walk wyymb0o printed 5 tb.double_stop lines and "findings_omitted: 2". The omitted two were
    tb.skipped (the real high-severity NIA-1) and model.mismatch. webs8j6 omitted tb.edge_stuck
    and tb.skipped the same way.
  - Per-step `!ghost_stop` markers vanish once the cap is hit (wr91td6 steps 36-45, w3cswcd
    steps 8-10), which reads as "these stops are fine".
  - The first steps are printed twice.
  - There is no way to page a saved walk; wrxqt1m.json is 120,940 bytes.
- *Improvement:*
  - In `compact()`, rank by severity, then by distinct code. Collapse repeats
    ("tb.double_stop x5: steps 0-1, 2-3, …") and always show each distinct code once. Keep the
    per-step markers, and print the step list once.
  - Add `tb-walk show <walk-id> --steps 17-42 --findings all --fields key,label,speak,via` with
    a byte cap, and its MCP equivalent.

**G4. `--start` / `--target` selectors are fragile, slow to fail, and can act on the wrong
element.**
- *Evidence:* `talkback/walk.py _match` compares only `label` / `cd`, and accepts substrings.
  - "Bookmark" matched "Unbookmark".
  - "Wear OS" activated a news card whose merged text contains it, and left the app for Chrome
    (t5c05ht).
  - The exact spoken label "Wear OS is not followed" failed, because the contentDescription is
    on a child Text. Once matched, it focused that inner Text, which TalkBack never stops on, so
    activate did nothing (t62wh1t).
  - "Middlegarchs" resolved to an inner LinearLayout. "You can download" picked an off-screen
    ancestor with an aggregated label and seeded focus there (woyj5hh), and the findings at step
    0 were caused by the tool's own start.
  - Thunderbird preference rows have label "" (their text is in children), so "Theme, Use
    system default", "Theme. Use" and "Theme" all failed.
  - Each miss costs 60 presses (about a minute) and scrolls the list, which stales the keys
    taken earlier (view:978).
  - Stale Compose keys after a list update are not re-resolved (compose:67:68 after a delete),
    and identical labels ("Button") cannot be told apart.
  - The seek shares the `--max-steps` budget ("within 6 presses").
  - A label missing from the screen gets the hint "Check TalkBack is running".
  - Targets behind an open drawer are accepted (tn66vvs, tasp4if).
- *Improvement:*
  - Resolve selectors among the predicted stops (merge roots) against the model's composed
    speech, preferring exact over whole-word over substring matches, and say which field
    matched.
  - For `activate`, refuse ambiguous matches and list the candidates. Climb to the nearest
    clickable stop, and warn before an activation that leaves the app.
  - Give the seek its own budget (`--seek-max`), stop after one lap, and scroll back before
    giving up.
  - Re-resolve stale keys through the key registry (`correlate`) or a fresh dump.
  - Accept test tag / resource id / "nth stop within <label>" selectors.
  - Refuse covered targets (see G5), and say "label not found among N stops on <activity>"
    instead of the TalkBack hint.

**G5. Occlusion is modelled wrong in both directions.**
- *Evidence, missed overlays:*
  - Thunderbird's action-mode bar exactly covers the toolbar, and the walk read the five
    covered stops first with no finding (wdvjrk4, wjx0ny4).
  - AntennaPod's expanded sheet covers Home, and lint reports touch-target errors on the hidden
    nodes with no covered marker (ckv9fz). Only a live walk's tb.escape catches it.
  - Thunderbird's modal drawer gives a false tb.skipped for 16-19 texts behind the scrim
    (wmuvqax, w7gq3kk), and find/node show no covered marker.
  - `covered_by` applies only to modal dialog windows.
- *Evidence, false overlays:* AntennaPod's empty loading FrameLayout (view:898,
  `feeditem_fragment.xml:221`, no background, its only child GONE) caused tb.ghost_stop
  "occluded by view:898", 4-5 tb.escape per walk (wzi9apx, w9wtb7e, woozepy), and the scenario
  verdict `behind_overlay`.
- *Improvement:*
  - Use one occlusion model for `a11y.py` (`covered_by`), `talkback/diff.py` (`_ghost_reasons`,
    `_check_escape`) and the capture's covered marker.
  - A view occludes only if it draws: a background or foreground drawable, visible children
    over the point, or clickable/focusable.
  - Extend `covered_by` to an open DrawerLayout drawer, an expanded BottomSheetBehavior sheet,
    and an ActionBarContextView in overlay mode.
  - Add tb.covered_stop, with the NO_HIDE_DESCENDANTS fix. Report lint findings on covered nodes
    apart, as is already done for dialogs. Collapse per-step escapes into one finding per
    overlay.

**G6. The capture names stateful nodes by their state, and its reading order disagrees with
the walk model.**
- *Evidence:*
  - `capture/index.py _Index._own()` and the `speakable` label in `a11y.py` take
    contentDescription, then text, then stateDescription, so a merged Compose node is labelled
    by its state. NiA onboarding rows, Interests rows and the topic chip (n2593, whose
    semantics Text is "NOT FOLLOWING") are labelled "Not selected", and settings radio buttons
    "Selected" / "Not selected".
  - Lint's R12 then reports "×3 ("Not selected")". An agent reading the capture concludes these
    controls are unlabelled, while tb-walk's model says "Not selected. Headlines".
  - Label formats differ between the halves: capture joins with ", " and uses capture roles,
    the walk uses ". " and TalkBack roles. A preference heading has no label in the capture
    (n1036).
  - `outline --view reading` is row-major on the NiA grid, while TalkBack and talkback.order
    are column-major before scrolling (c19s28 against w7y77g9 and wox59ex).
  - On AntennaPod Home, the capture listed no WebView stops while the walk model from the same
    state predicted 66.
- *Improvement:*
  - Use one label composer for capture, walk, scenario and findings: TalkBack 17 order, as
    `talkback/speech.py` already does, with state as a separate field. Base R12 on the name.
  - Derive `outline --view reading` from talkback.order, and add a regression test on
    `nia_onboarding_grid`.

**G7. WebViews: the capture misses them, being stuck in front of one is misdiagnosed, and the
trap model rests on a state that did not reproduce.**
- *Evidence:*
  - An AntennaPod cold start with TalkBack off has 0 WebView nodes (ckk688). After one
    expand/collapse or with TalkBack on, there are 66 (cxb73v, cqhjb8), 0 issues, and lint is
    silent about 66 stops at y=4564 with height 0.
  - Stuck walks report tb.edge_stuck "Expose scrolling…" when the model's next stop is a
    WebView root (wdxfwnu, wzjwrh8, w5egssf, wvthzum, w9wtb7e).
  - The `web_traps` hint says "a trap" in walks that moved through the WebView (ws8ot3m,
    w21bfni, wskn09m).
  - `talkback/order.py Navigator.traps()` and `test_the_antennapod_player_trap_is_named_and_explained`
    pin a trap seen on emulator-5554 that three walks on emulator-5558 did not show. The hunt's
    one trap walk is confounded (G15).
  - Ghost checks do not clip virtual bounds to the ancestors' viewports (ws8ot3m steps 1-2).
  - The hint text names AntennaPod (`order.py:920`).
- *Improvement:*
  - The capture flags a WebView whose virtual tree is empty ("enable accessibility or show it
    once"), or asks Chromium to build the tree.
  - Add a lint rule for reading stops whose bounds are empty or outside every ancestor
    viewport.
  - In `diff._check_end`, emit tb.webview_block when stuck and the next predicted stop is a
    WebView root, naming the WebView and its pager or sheet ancestor, regardless of geometry.
  - Reconcile the `web_traps` hints with what the walk did.
  - Re-measure `Navigator.traps` with `--relaunch` in both start orders (G1) before keeping it.
  - Intersect virtual bounds with the ancestors' clip rects, and make the hint text generic.

**G8. List counts are checked only when TalkBack speaks "N of M".**
- *Evidence:*
  - `diff._check_n_of_m` parses only "N of M". It was silent on "In list. 6 items" for 5 rows
    (wygfouz, wxvgdx1, wa5ns8b) and on "In list. 20 items" for 19 topics (wytgfpg).
  - The capture already held the cause: a 0x0 ComposeView at adapter position 0 (n1745 in cda8jc),
    `item.row=1` on the first View row (n1400 in c6c1dm), `rows=6`. There was also a spurious
    SCROLL_BACKWARD on a list already at its top.
- *Improvement:*
  - Parse "In list. N items" / "In grid" and compare N with the stops reached in that container
    over a lap.
  - Add an `a11y_lint` rule: a collection whose rowCount differs from its non-empty items, or
    with a zero-size or never-focusable item first or last (header, footer, spacer), reporting
    the shift TalkBack will speak.
  - Fixtures: `thunderbird_list_compose_tb_first`, `_no_banner`, `nia_interests`.

**G9. System dialogs over the app can be neither walked nor seen.**
- *Evidence:* with NiA's notification permission dialog up, tb-walk failed with "not in the
  foreground (top: com.google.android.permissioncontroller/…GrantPermissionsActivity)", and
  `--package` of the permission controller failed with "not debuggable". Meanwhile the capture
  (cefr89) showed no sign of the dialog and listed Search as on screen. The same happened with
  the system "Android App Compatibility" dialog (c4df23).
- *Improvement:* allow tb-walk / tb-scenario under a foreign top window using only TalkBack
  focus events and logcat, with no injection. Report covering windows from other processes in
  the capture (`dumpsys window` / AccessibilityWindowInfo) as `covered_by`.

**G10. A false tb.loop error ends walks early.**
- *Evidence:* wc8drkq and wtsk72v ended `loop` after a node was re-read following an
  auto-scroll. Continuing from there (wmfue60) reached Done, the tabs and the edge.
- *Improvement:* in the loop check in `diff._check_end`, call it a cycle only when the scroll
  state (container offset or node bounds) repeats too, or after N more presses confirm it.

### Medium

**G11. `tb-scenario` records no speech and no focus reason.**
- *Evidence:* "1 selected" (TB-3), "Thunderbird Debug" (TB-5), the empty click feedback after
  the star toggle (TB-6), the empty dialog feedback (AP-9), and `isInitialFocus` versus
  `isRestoreFocusOrEnsureOnScreen` (TB-2) all needed a separate logcat tail. The `focus-after`
  stdout also omits the timeline, which is only in the saved file, while `restore` prints it.
- *Improvement:* in `scenarios.timeline()`, read the verbose ttsOutput lines and TalkBack's
  focus reason per timeline entry, and add `speak_before` / `speak_after` and the announcements
  (TYPE_ANNOUNCEMENT, live regions, pane titles). Flag "activated, nothing spoken" and "changed
  visually, speech did not". Print the compact timeline for `focus-after`.

**G12. `tb-scenario` verdicts and advice mislabel common outcomes.**
- *Evidence:*
  - `on_close_or_unlabeled`, with dialog and paneTitle advice, was given for a labelled Back
    (tngabmn, tf055yi), for bottom tabs (thzyteh, tdghihv, tenf30l), and for a toolbar hidden
    under the action-mode bar (t8zdyld, tm79xfy).
  - `initial_ok` was given when Done reset focus to the top (ta6abda).
  - `elsewhere` was given when back returned focus to the opener (AntennaPod filter).
  - `stayed_on_opener` was given when a label target hit a node that cannot act (t62wh1t).
  - The landing node is never checked against `covered_by`.
- *Improvement:* in `scenarios._focus_after` / `_restore`, add the verdicts `moved_to_nav`,
  `reset_to_top` (target removed, same screen), `returned_to_opener` and `nothing_happened`.
  Keep `on_close_or_unlabeled` for unlabelled nodes and close/dismiss roles, and give
  `initial_ok` only when `new_screen` is true. Give list-mutation advice (focus the neighbour,
  announce). Check `covered_by` on the landing node, and quote its spoken label.

**G13. `restore` presses back before the opened screen has settled.**
- *Evidence:* with the default wait (t1yhp9e), back was pressed during the transition and the
  verdict was `elsewhere`. With `--wait-ms 2500` (tagjuzc), it was `top`.
- *Improvement:* wait for a window or pane change and for focus to stay `settle-ms` on the new
  screen (bounded by `wait-ms`), or report "opened screen not settled".

**G14. Multi-step flows, the actions menu, custom actions and long-press cannot be expressed.**
- *Evidence:* "Mark all as read", long-press > action mode > Mark read, swipe-to-toggle-read,
  queue reordering and Undo after unbookmarking all needed adb taps with TalkBack off, or
  `--leave-on` chains that lose speech (G2) and leave TalkBack on. App state drifted silently,
  for example with the drawer left open.
- *Improvement:* add scenario sequences (`--steps 'activate:Unbookmark; walk:12'`), a
  `custom:<label>` action (through TalkBack's actions menu, or by action id through the agent),
  long-press (`ACTION_LONG_CLICK` or TalkBack's long-press key), and precondition checks on the
  window, pane and activity.

**G15. An unproven keyboard switches keymaps on the first press that does not move, which can
fabricate a trap.**
- *Evidence:* wvlytwx, the hunt's only expanded-player trap, started through `a11y_act` on
  "Shownotes", so the keyboard had never moved focus. `try_other_keymap` switched to the classic
  Alt keymap (`uinput/classic`), and every later press used a keymap TalkBack 17's default
  enhanced mode may not bind. All its utterances came from the model.
- *Improvement:* in `talkback/walk.py try_other_keymap`, prove the keyboard with presses that
  must move (previous, then next back) before switching. Record "injector unproven" and do not
  raise tb.trap / tb.edge_stuck from an unproven injector.

**G16. Walk detections that are missing.**
- Interleaved card children (NIA-3): the model agreed with TalkBack and no finding fired. Add
  tb.interleaved for a stop separated from its own card by a sibling card's stops, or read
  before its card.
- Auto-scroll that runs along the bottom row of a multi-row horizontal grid (NIA-1) gets the
  visibility and "Expose scrolling" advice. Add tb.autoscroll_row_skip, naming the rows passed
  over, with the traversalIndex or vertical-layout fix.
- A label prefix shared by every item of a list (TB-11, "Account settings") gave a walk with no
  findings at all.

**G17. Lint misses real defects that TalkBack exposed.**
- Bracketed or identifier-like tokens in a label: "[attachment_icon]" (TB-7).
- A leading token shared by most items of a collection: "Account settings" (TB-11).
- A decorative child's contentDescription merged into the row: "Star" (TB-12).
- A label contradicting its state: "Unbookmark" while checked (NIA-10), "Pause" with
  `ic_play_48dp` while the media session is paused (AP-5).
- The C1 pattern statically: an actionable node containing an actionable descendant (NIA-2,
  TB-8).
- `selected=false` on every item of a list (NIA-9).
- Gesture-only actions: an ItemTouchHelper with no custom actions on the items (TB-9), and a
  drag handle hidden from accessibility (AP-10).
- On NiA, lint reported 0/0/0 on Interests, Search, Feed and Settings.

Each is an `a11y_lint` rule. The ItemTouchHelper rule needs the agent to report RecyclerView
item touch listeners.

**G18. Lint false positives.**
- `touch_target.small` on Compose IconButtons whose touch area is 48 dp: "w_dp=48 h_dp=24 …
  touch_w_dp=48 touch_h_dp=48", 6 times per Thunderbird screen. In `rule_touch_target`, judge
  the touch or accessibility bounds when they are present.
- R2 on nodes clipped by a bar (NiA Unbookmark n2645). Skip R2 for nodes flagged
  `render.clipped`.
- Findings on nodes covered by a same-window sheet (G5).
- The outline's `scroll` flag on cards whose node offers only CLICK (n2839). Derive it from the
  accessibility actions.

**G19. JSON output is not clean JSON.**
- *Evidence:*
  - `tb-scenario` / `tb-walk --json -` print a plain "error: …" on failure.
  - `--leave-on` prints its note on stdout before the JSON (wvthzum).
  - `--json FILE` prints "wrote JSON to" on stdout.
  - `find --format json` adds a "capture=… total=…" header and a "next:" footer, and `lint`
    adds "next:" lines.
  - Each of these breaks `json.load`.
- *Improvement:* under `--json`, emit errors as `{"error": {code, message, hint}}` and move
  notes into a field or onto stderr.

**G20. The walk model ignores how RecyclerView's item delegate changes TalkBack's stops.**
- *Evidence:* on `antennapod_episode_details_tb_later`, the model still stops on the ViewPager2
  page root (view:872, "Page…"). TalkBack skipped it and read the duration and date as separate
  stops. The dump marks view:872 neither important nor ignored.
- *Improvement:* check how the agent's ignored/important marking (CONTRACT.md §9) and
  `talkback/rules.py` handle a page root with importantForAccessibility AUTO and no
  CollectionItemInfo. Pin both fixtures press for press.

**G21. Focus moving into the soft keyboard is reported as stuck and lost.**
- *Evidence:* on a Compose screen with the keyboard up, whf4las (keyboard) and wiuvqit (swipes)
  ended `stuck`, with tb.edge_stuck and tb.focus_lost, after "Editing. Message text. Edit box".
  Focus had moved into the input-method window.
- *Improvement:* read the focused node across all windows, including TYPE_INPUT_METHOD. Report
  "focus left the app into <window>", and offer to hide the IME first.

**G22. Other walk false positives and wrong attributions.**
- tb.revisit matches on class and label. It flags different "Bookmark" nodes (wrxqt1m,
  wems6uy) and misses a real same-key re-read (wtt0adx, compose:8:516 at steps 58 and 60). In
  `diff._check_revisit`, use the key plus bounds or ancestry, and fall back to the label only
  after a generation change.
- tb.skipped says "in a full lap" on partial walks (wmfue60, w2z0qwg, ws888m4 with "94
  predicted stops", wbunyhi with "91"), and listed a stop the walk did reach (wtt0adx,
  compose:8:178). In `diff._check_skipped` / `_unvisited`, count only the walked span, claim a
  full lap only after a real wrap, and check against every visited key.
- tb.edge_stuck:
  - names the step after the wrap (wzu9m96: edge at step 11, finding at step 14);
  - names an edge stop outside the scrollable container (wtvdjj3, against a horizontal
    ViewPager2);
  - fires on a short step timeout (wr65oos; wems6uy with `--step-timeout-ms 4000` went
    through);
  - reports a flaky end-of-grid auto-scroll as definitive. The same start went to Done in 4
    runs and to Android TV in 1.

  Repeat the press from the same state and report "n of m". Treat horizontal pagers as paging.
- tb.double_stop on a text field and its clear button (wahyghw), and on rows with a distinct
  secondary action (ws888m4, wmaadkk). Exempt editable fields, and downgrade to info when the
  inner action differs.
- Text output prints stale model speech for auto-scrolled steps (wyymb0o step 17).

### Low

- **G23.** The touch injector cannot `--start first` ("the touch injector has no 'first'
  gesture"). Seek with previous-swipes to the edge, or place focus through the agent first.
- **G24.** The line-based logcat reader (`walk.py TalkBackLog`, `readline` plus `_RE_TTS`) drops
  the continuation lines of multi-line utterances (wtvdjj3 step 10). Join lines up to the next
  logcat header.
- **G25.** The initial-focus model does not know that TalkBack focuses the first list item after
  a fragment replace (Thunderbird thread: the model said view:2022, TalkBack went to the first
  row, ttjpx50).
- **G26.** The model speaks "•" where TalkBack 17 says "Bullet" (3 of 14 presses on
  `antennapod_episode_details_tb_first`).
- **G27.** `capture --label afterCompose` is rejected (labels must be lowercase). Lowercase it
  automatically.
- **G28.** Docs: show `am start -a android.intent.action.MAIN -c android.intent.category.LAUNCHER
  -p <pkg>` for apps whose launcher entry is an alias, and add "TalkBack first" as the
  realistic recipe (until G1 lands).

## What a diagnosis cost

Sizes are as an LLM agent reads them, from the CLI's default output.

| Step | Bytes | Time |
|---|---|---|
| `capture --json` | 1.0-1.4 KB | |
| `outline --view reading` | 0.9-1.9 KB; 3.5 KB on AntennaPod Home; 5.6 KB with a WebView (capped) | |
| `find` / `lint` | 0.3-1.0 KB / 0.7-1.5 KB | |
| `node` (one to three nodes) | 1.0-3.5 KB | |
| `tb-walk` text | 1.2-4.7 KB | 10-55 s (model speech about 3x faster than logcat) |
| saved walk JSON, needed for full speech and omitted findings | 20-120 KB per walk | |
| `tb-scenario --json` | 1.0-3.4 KB | about 8 s |
| a failed `--target` seek | | 60+ s (60 presses) |

Typical diagnoses ran from about 12 KB (Thunderbird star, list count and restore) to about 28 KB
of CLI output (NIA-1, five walks). With the saved walk JSON opened to find what the summaries cut
or left out, they ran to 40-53 KB. The AntennaPod run read about 120 KB of tool output in all.
G2 and G3 account for most of the gap between the two.

## Evidence in the repo

These are under `host/tests/data/realapps/`, in the same format as the existing ones (see its
README). The walks are in `talkback17_hunt_walks.json.gz`, each with its walk id, TalkBack start
order and screen density. No test uses them yet. They are inputs for the gaps above.

| Fixture | Walk | Pins |
|---|---|---|
| `thunderbird_list_compose_tb_first` | wcw61ax | TB-1 "2 of 6" … "6 of 6"; TB-7 tokens; TB-8 triple stops. Model 21/21 |
| `thunderbird_list_compose_tb_later` | wygfouz | G1: no positions, "In list. 6 items" for 5 rows. Model 21/21 |
| `thunderbird_list_compose_no_banner` | wr43dka | TB-1 control: "1 of 4" … "4 of 4" with the flag off. Model 18/18 |
| `thunderbird_selection_mode` | wdvjrk4 | TB-4 covered toolbar read first; TB-3 unlabelled check button. Model 27/27 |
| `thunderbird_settings` | wxc6f3c | TB-11 "Account settings" prefix, no finding. Model 13/13 |
| `nia_onboarding_grid` (+ `_backward`) | wvq4h1u, wox59ex | NIA-1 bottom-row auto-scroll. Model diverges at the first auto-scroll; backward 14/14 |
| `nia_feed_two_column` (280 dpi) | wg0mhts | NIA-3 interleaving. Model 17/17 up to the first auto-scroll |
| `nia_interests` | wytgfpg | NIA-2 double stops, NIA-9 "Not selected", NIA-13 "20 items" for 19. Model 9/9 |
| `antennapod_episode_details_tb_first` | wzi9apx | AP-4 page stop and WebView entry; G5 false overlay (view:898). Model 14/14 keys |
| `antennapod_episode_details_tb_later` | w9wtb7e | G1/G20: stuck after Download; model diverges at press 3 |

"Model n/n" counts the presses where `talkback.simulate` on the fixture dump (keyboard, from the
walk's start) lands on the same node as TalkBack, with the same words wherever TalkBack's speech
was logged.
