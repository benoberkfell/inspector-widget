"""ACTION_NAMES must decode the framework's R.id-backed accessibility actions.

The values are android.R.id.accessibilityAction* constants, read from the
SDK's android.jar. A previous table was shifted, so every View's
SHOW_ON_SCREEN decoded as CUSTOM_0x01020036 and Compose's SCROLL_DOWN
(0x0102003A) was unrecognised, which broke the scroll-dependent lint rules.
"""

from inspector_widget.a11y import ACTION_NAMES

# android.R.id values from platforms/android-37.0/android.jar.
FRAMEWORK_IDS = {
    "SHOW_ON_SCREEN": 0x01020036,
    "SCROLL_TO_POSITION": 0x01020037,
    "SCROLL_UP": 0x01020038,
    "SCROLL_LEFT": 0x01020039,
    "SCROLL_DOWN": 0x0102003A,
    "SCROLL_RIGHT": 0x0102003B,
    "CONTEXT_CLICK": 0x0102003C,
    "SET_PROGRESS": 0x0102003D,
    "MOVE_WINDOW": 0x01020042,
    "SHOW_TOOLTIP": 0x01020044,
    "HIDE_TOOLTIP": 0x01020045,
    "PAGE_UP": 0x01020046,
    "PAGE_DOWN": 0x01020047,
    "PAGE_LEFT": 0x01020048,
    "PAGE_RIGHT": 0x01020049,
    "PRESS_AND_HOLD": 0x0102004A,
    "IME_ENTER": 0x01020054,
    "DRAG_START": 0x01020055,
    "DRAG_DROP": 0x01020056,
    "DRAG_CANCEL": 0x01020057,
    "SHOW_TEXT_SUGGESTIONS": 0x01020058,
    "SCROLL_IN_DIRECTION": 0x0102005E,
    "SET_EXTENDED_SELECTION": 0x0102005F,
}


def test_framework_action_ids_decode_to_their_names():
    for name, value in FRAMEWORK_IDS.items():
        assert ACTION_NAMES.get(value) == name, (name, hex(value))


def test_no_name_maps_to_two_ids():
    names = list(ACTION_NAMES.values())
    assert len(names) == len(set(names))
