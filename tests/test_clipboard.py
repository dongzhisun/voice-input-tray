"""Black-box clause 7: clipboard protection under normal and hostile cases."""
import pytest

from voicetray import clipboard as cb


def paste_noop():
    return None


def test_insert_sets_text_for_paste_then_restores_user_clipboard():
    backend = cb.MemoryClipboard("用户复制的内容")
    seen = {}

    def paste():
        seen["clipboard_during_paste"] = backend.get_data(cb.CF_UNICODETEXT)

    r = cb.insert_text("识别出来的文本", backend=backend,
                       paste=paste, settle=0)
    assert r.ok is True
    assert r.restored is True
    assert r.error == ""
    assert seen["clipboard_during_paste"] == cb._encode_unicode("识别出来的文本")
    assert cb.snapshot(backend).text == "用户复制的内容"


def test_all_readable_formats_are_restored_not_just_text():
    backend = cb.MemoryClipboard("原文")
    backend.set_data(cb.CF_OEMTEXT, b"oem-bytes")
    backend.set_data(0xC001, b"custom-format")
    before = cb.snapshot(backend)

    r = cb.insert_text("新文本", backend=backend, paste=paste_noop, settle=0)

    assert r.ok
    after = cb.snapshot(backend)
    assert after.formats == before.formats
    assert after.formats[0xC001] == b"custom-format"


def test_paste_failure_still_restores_clipboard():
    backend = cb.MemoryClipboard("用户原文")

    def broken_paste():
        raise RuntimeError("SendInput failed")

    r = cb.insert_text("新文本", backend=backend,
                       paste=broken_paste, settle=0)
    assert r.ok is False
    assert "SendInput failed" in r.error
    assert r.restored is True
    assert cb.snapshot(backend).text == "用户原文"


def test_clipboard_competition_is_detected_and_original_is_restored():
    backend = cb.MemoryClipboard("用户原文")

    def competing_app():
        # Another process copies something while our paste window is open.
        backend.set_data(cb.CF_UNICODETEXT, cb._encode_unicode("别人复制的"))

    r = cb.insert_text("新文本", backend=backend, paste=competing_app, settle=0)
    assert r.ok is True
    assert r.competed is True
    assert cb.snapshot(backend).text == "用户原文"


def test_gdi_handle_formats_are_reported_as_skipped():
    class GdiBackend(cb.MemoryClipboard):
        def formats(self):
            return [cb.CF_BITMAP, cb.CF_UNICODETEXT]

        def get_data(self, fmt):
            if fmt == cb.CF_BITMAP:
                return b""
            return super().get_data(fmt)

    backend = GdiBackend("原文")
    r = cb.insert_text("新文本", backend=backend, paste=paste_noop, settle=0)
    assert r.ok
    assert r.skipped_formats == 1
    assert cb.snapshot(backend).text == "原文"


def test_guard_protect_restores_even_when_body_raises():
    backend = cb.MemoryClipboard("用户原文")
    guard = cb.ClipboardGuard(backend)
    with pytest.raises(RuntimeError, match="boom"):
        with guard.protect():
            backend.set_text("临时内容")
            raise RuntimeError("boom")
    assert cb.snapshot(backend).text == "用户原文"


def test_clipboard_busy_raises_clear_error():
    backend = cb.MemoryClipboard("x")
    backend.open()
    with pytest.raises(cb.ClipboardError, match="busy"):
        backend.open()
    backend.close()


def test_empty_original_clipboard_stays_empty():
    backend = cb.MemoryClipboard("")
    r = cb.insert_text("新文本", backend=backend, paste=paste_noop, settle=0)
    assert r.ok
    assert cb.snapshot(backend).formats.get(cb.CF_UNICODETEXT, b"") == b""


def test_snapshot_text_decodes_utf16le():
    s = cb.ClipboardSnapshot(
        formats={cb.CF_UNICODETEXT: "中文".encode("utf-16-le") + b"\x00\x00"})
    assert s.text == "中文"


class FlakyBackend(cb.MemoryClipboard):
    """One custom format refuses to be written back."""

    def __init__(self, text, fail_fmt):
        super().__init__(text)
        self.fail_fmt = fail_fmt
        self.armed = False

    def set_data(self, fmt, data):
        if self.armed and int(fmt) == self.fail_fmt:
            raise cb.ClipboardError(f"format {fmt} rejected")
        return super().set_data(fmt, data)


def test_restore_is_best_effort_and_reports_failures():
    backend = FlakyBackend("用户原文", fail_fmt=0xC001)
    backend.set_data(0xC001, b"custom-format")
    snap = cb.snapshot(backend)
    backend.armed = True
    report = cb.restore(backend, snap)
    assert report.attempted == 2
    assert report.restored == 1
    assert report.complete is False
    assert report.failures[0][0] == 0xC001
    # Text formats are restored first, so a failing exotic format must never
    # leave the user with an empty clipboard.
    assert cb.snapshot(backend).text == "用户原文"


def test_paste_text_defers_restore_until_called():
    backend = cb.MemoryClipboard("用户原文")
    seen = []

    def paste():
        seen.append(cb.snapshot(backend).text)

    result, pending = cb.paste_text("新文本", backend=backend, paste=paste)
    assert result.ok is True
    assert seen == ["新文本"]
    # Clipboard must still hold our text while the target consumes Ctrl+V.
    assert cb.snapshot(backend).text == "新文本"
    report = pending.restore()
    assert report.complete is True
    assert cb.snapshot(backend).text == "用户原文"


def test_finish_insert_completes_result_from_pending():
    backend = cb.MemoryClipboard("用户原文")
    result, pending = cb.paste_text("新文本", backend=backend,
                                    paste=paste_noop)
    final = cb.finish_insert(result, pending)
    assert final.ok and final.restored and final.restore_failures == 0
    assert cb.snapshot(backend).text == "用户原文"


def test_insert_text_pumps_messages_before_restoring():
    """The regression behind _spike/focus_diag.py: a plain sleep restores
    the clipboard before the target app ever reads it, so nothing is pasted.
    """
    backend = cb.MemoryClipboard("用户原文")
    observed = []

    def pump(seconds):
        observed.append(cb.snapshot(backend).text)

    r = cb.insert_text("新文本", backend=backend, paste=paste_noop,
                       settle=0.01, pump=pump)
    assert observed == ["新文本"]
    assert r.ok is True and r.restored is True
    assert cb.snapshot(backend).text == "用户原文"


def test_insert_reports_partial_restore_instead_of_losing_user_text():
    backend = FlakyBackend("用户原文", fail_fmt=0xC001)
    backend.set_data(0xC001, b"custom-format")
    r = cb.insert_text(
        "新文本", backend=backend,
        paste=lambda: setattr(backend, "armed", True), settle=0)
    assert r.ok is True
    assert r.restored is False
    assert r.restore_failures == 1
    assert "49153" in r.restore_failure_formats
    after = cb.snapshot(backend)
    assert after.text == "用户原文"
    assert 0xC001 not in after.formats
