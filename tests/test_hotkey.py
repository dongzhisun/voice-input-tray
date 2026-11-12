"""Black-box clauses 4 / 8: right-Alt press-release and Esc cancel."""
import pytest

from voicetray.hotkey import (
    HotkeyDecoder, KeyAction, VK_ESCAPE, VK_MENU, VK_RMENU,
    WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP,
    LLKHF_EXTENDEDKEY, LLKHF_INJECTED, LLKHF_UP,
    is_right_alt,
)

# The exact two events observed in _spike/spike_result.json on this host.
REAL_DOWN = (WM_SYSKEYDOWN, VK_RMENU, LLKHF_EXTENDEDKEY | LLKHF_INJECTED, 0)
REAL_UP = (WM_KEYUP, VK_RMENU, LLKHF_EXTENDEDKEY | LLKHF_INJECTED, 0)


def test_measured_event_pair_decodes_to_press_then_release():
    d = HotkeyDecoder()
    down = d.feed(*REAL_DOWN)
    assert down.action is KeyAction.PRESS
    assert down.injected is True
    assert d.held
    up = d.feed(*REAL_UP)
    assert up.action is KeyAction.RELEASE
    assert not d.held


def test_asymmetric_messages_are_not_misread():
    """down is WM_SYSKEYDOWN while up is WM_KEYUP; direction must come from
    the message family, not from vkCode."""
    d = HotkeyDecoder()
    assert d.feed(WM_SYSKEYDOWN, VK_RMENU, LLKHF_EXTENDEDKEY).action is KeyAction.PRESS
    assert d.feed(WM_SYSKEYUP, VK_RMENU, LLKHF_EXTENDEDKEY).action is KeyAction.RELEASE
    d2 = HotkeyDecoder()
    assert d2.feed(WM_KEYDOWN, VK_RMENU, LLKHF_EXTENDEDKEY).action is KeyAction.PRESS
    assert d2.feed(WM_KEYUP, VK_RMENU, LLKHF_EXTENDEDKEY).action is KeyAction.RELEASE


def test_auto_repeat_is_deduplicated():
    d = HotkeyDecoder()
    assert d.feed(*REAL_DOWN).action is KeyAction.PRESS
    for _ in range(5):
        assert d.feed(*REAL_DOWN).reason == "auto-repeat"
    assert d.feed(*REAL_UP).action is KeyAction.RELEASE


def test_unmatched_release_is_ignored():
    d = HotkeyDecoder()
    ev = d.feed(*REAL_UP)
    assert ev.action is KeyAction.IGNORE
    assert ev.reason == "unmatched-release"


def test_left_alt_is_never_a_hotkey():
    assert not is_right_alt(VK_MENU, 0)
    assert is_right_alt(VK_MENU, LLKHF_EXTENDEDKEY)
    assert is_right_alt(VK_RMENU, 0)
    d = HotkeyDecoder()
    assert d.feed(WM_SYSKEYDOWN, VK_MENU, 0).action is KeyAction.IGNORE


def test_escape_cancels_once_until_keyup():
    d = HotkeyDecoder()
    assert d.feed(WM_KEYDOWN, VK_ESCAPE, 0).action is KeyAction.CANCEL
    assert d.feed(WM_KEYDOWN, VK_ESCAPE, 0).action is KeyAction.IGNORE
    assert d.feed(WM_KEYUP, VK_ESCAPE, 0).action is KeyAction.IGNORE
    assert d.feed(WM_KEYDOWN, VK_ESCAPE, 0).action is KeyAction.CANCEL


def test_injected_events_can_be_blocked():
    d = HotkeyDecoder(allow_injected=False)
    assert d.feed(*REAL_DOWN).reason == "injected-blocked"
    assert d.feed(WM_SYSKEYDOWN, VK_RMENU, LLKHF_EXTENDEDKEY).action is KeyAction.PRESS


def test_up_flag_alone_is_accepted_and_other_messages_are_ignored():
    d = HotkeyDecoder()
    assert d.feed(WM_SYSKEYDOWN, VK_RMENU, LLKHF_EXTENDEDKEY).action is KeyAction.PRESS
    assert d.feed(0x9999, VK_RMENU, LLKHF_EXTENDEDKEY | LLKHF_UP).action is KeyAction.RELEASE
    d.reset()
    assert d.feed(0x0200, VK_RMENU, 0).reason == "not-a-key-message"
    assert d.feed(WM_KEYDOWN, ord("A"), 0).reason == "unrelated-key"
