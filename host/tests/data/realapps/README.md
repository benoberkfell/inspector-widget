# Real-app accessibility evidence

Accessibility dumps (`a11y.a11y_to_dict` output, `focus_order` dropped) and TalkBack 17.0
walks from three open-source debuggable apps on emulator-5554 (API 37, 1280x2856, 480dpi):
Thunderbird for Android (fossDebug, a demo account), Now in Android (demoDebug) and
AntennaPod (freeDebug, two subscriptions). Taken by the real-app validation run with the
host at e992335; the walks pressed Meta+Right through a uinput keyboard and read TalkBack's
verbose ttsOutput.

`talkback17_walks.json.gz` holds, per screen, the focus before the walk (`start`) and each
press as `[moved, focused node key, its label, what TalkBack said]`. A walk and its dump were
taken one after the other on the same screen, except where test_realapp_accuracy.py says
otherwise (the toolbar menu was re-created in between, or the dump came from another visit).

Used by test_realapp_accuracy.py (lint false positives, the TalkBack model's order and speech,
WebView content).
