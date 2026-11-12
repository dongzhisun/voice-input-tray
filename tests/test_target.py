"""Black-box clause 6: original-target confirmation."""
import pytest

from voicetray import target as t


def snap(**kw) -> t.TargetSnapshot:
    base = dict(hwnd=100, pid=200, tid=300, title="编辑器",
                class_name="Chrome_WidgetWin_1", focus_hwnd=100,
                uia_runtime_id="42,100,7", uia_name="备注",
                uia_class="Edit", uia_control_type=50004, uia_pid=200)
    base.update(kw)
    return t.TargetSnapshot(**base)


def test_fingerprint_is_stable_and_hex():
    a, b = snap(), snap()
    assert a.fingerprint() == b.fingerprint()
    assert len(a.fingerprint()) == 64


def test_clipboard_sequence_and_degraded_flag_do_not_change_fingerprint():
    """Other apps may legitimately touch the clipboard while we record."""
    a = snap(clipboard_seq=10, degraded=False)
    b = snap(clipboard_seq=999, degraded=True)
    assert a.fingerprint() == b.fingerprint()


@pytest.mark.parametrize("field,value", [
    ("hwnd", 101), ("pid", 201), ("tid", 301), ("title", "另一个窗口"),
    ("class_name", "Notepad"), ("focus_hwnd", 999),
    ("uia_runtime_id", "42,100,8"), ("uia_name", "另一个输入框"),
    ("caret_hwnd", 12), ("uia_class", "RichEdit"),
])
def test_any_identity_field_change_changes_fingerprint(field, value):
    assert snap().fingerprint() != snap(**{field: value}).fingerprint()


def test_compare_returns_all_changed_fields():
    diff = snap().compare(snap(pid=999, title="别的窗口"))
    assert diff == ["pid", "title"]


def test_compare_ignores_missing_uia_on_either_side():
    """UIA is best-effort; a one-sided dropout must not look like a target
    switch, otherwise UIA flakiness would silently discard valid transcripts."""
    before = snap(uia_runtime_id="42,100,7")
    after = snap(uia_runtime_id="")
    assert before.compare(after) == []
    assert after.compare(before) == []


def test_compare_detects_uia_element_change_inside_same_window():
    """The Chromium case Win32 alone cannot see."""
    before = snap(uia_runtime_id="42,100,7")
    after = snap(uia_runtime_id="42,100,9")
    assert before.compare(after) == ["uia_runtime_id"]


def test_focus_hwnd_equal_to_top_level_is_unobservable():
    """Chromium reports hwndFocus == foreground hwnd and can briefly drop it
    to 0 while focus settles. That must not look like a target switch.
    """
    a = snap(hwnd=100, focus_hwnd=100)
    b = snap(hwnd=100, focus_hwnd=0)
    assert a._effective_focus_hwnd() == 0
    assert b._effective_focus_hwnd() == 0
    assert a.compare(b) == []
    assert a.fingerprint() == b.fingerprint()
    distinct = snap(hwnd=100, focus_hwnd=999)
    assert a.compare(distinct) == ["focus_hwnd"]
    assert a.fingerprint() != distinct.fingerprint()


def test_confirmed_with_injected_after_snapshot():
    ok, diff = t.confirmed(snap(), snap())
    assert ok and diff == []
    ok, diff = t.confirmed(snap(), snap(pid=999))
    assert not ok and diff == ["pid"]
