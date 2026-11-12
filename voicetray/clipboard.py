"""Clipboard protection and paste-based insertion.

Black-box clause 7: the user's clipboard must be identical before and after
insertion. We therefore snapshot *all* readable clipboard formats (not just
text), set CF_UNICODETEXT, send Ctrl+V, then restore every saved format.

Known boundary: GDI handle-based formats (CF_BITMAP, CF_METAFILEPICT,
CF_ENHMETAFILE, CF_PALETTE) cannot be read back as raw bytes via GlobalLock.
They are recorded in `skipped` and reported to the UI instead of silently
pretending a perfect restore.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Callable, Optional

CF_TEXT = 1
CF_BITMAP = 2
CF_METAFILEPICT = 3
CF_OEMTEXT = 7
CF_DIB = 8
CF_PALETTE = 9
CF_UNICODETEXT = 13
CF_ENHMETAFILE = 14
CF_HDROP = 15
CF_LOCALE = 16
CF_DIBV5 = 17

SKIP_FORMATS = frozenset((CF_BITMAP, CF_METAFILEPICT, CF_ENHMETAFILE,
                          CF_PALETTE))
GMEM_MOVEABLE = 0x0002


class ClipboardError(RuntimeError):
    pass


@dataclass
class ClipboardSnapshot:
    formats: dict[int, bytes] = field(default_factory=dict)
    skipped: list[int] = field(default_factory=list)
    seq: int = 0

    @property
    def text(self) -> str:
        raw = self.formats.get(CF_UNICODETEXT, b"")
        try:
            return raw.decode("utf-16-le", errors="replace").rstrip("\x00")
        except Exception:
            return ""


def _encode_unicode(text: str) -> bytes:
    return text.encode("utf-16-le", errors="replace") + b"\x00\x00"


class MemoryClipboard:
    """In-memory backend for tests and for fault injection."""

    def __init__(self, text: str = "") -> None:
        self._formats: dict[int, bytes] = {}
        if text:
            self._formats[CF_UNICODETEXT] = _encode_unicode(text)
        self._seq = 0
        self._open = False
        self.busy = False

    def open(self, retries: int = 1, delay: float = 0.0) -> None:
        if self.busy or self._open:
            raise ClipboardError("clipboard busy")
        self._open = True

    def close(self) -> None:
        self._open = False

    def formats(self) -> list[int]:
        return list(self._formats)

    def get_data(self, fmt: int) -> bytes:
        return self._formats.get(fmt, b"")

    def set_data(self, fmt: int, data: bytes) -> None:
        self._formats[int(fmt)] = bytes(data)
        self._seq += 1

    def clear(self) -> None:
        self._formats.clear()
        self._seq += 1

    def set_text(self, text: str) -> None:
        self.open()
        try:
            self.clear()
            self.set_data(CF_UNICODETEXT, _encode_unicode(text))
        finally:
            self.close()

    def sequence_number(self) -> int:
        return self._seq


class Win32Clipboard:
    """ctypes backend. All pointers are declared explicitly so that 64-bit
    handles are never truncated to int (the same class of bug as the hook)."""

    def __init__(self) -> None:
        self.u = ctypes.WinDLL("user32", use_last_error=True)
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        u, k = self.u, self.k
        u.OpenClipboard.argtypes = [wt.HWND]
        u.OpenClipboard.restype = wt.BOOL
        u.CloseClipboard.argtypes = []
        u.CloseClipboard.restype = wt.BOOL
        u.EmptyClipboard.argtypes = []
        u.EmptyClipboard.restype = wt.BOOL
        u.EnumClipboardFormats.argtypes = [wt.UINT]
        u.EnumClipboardFormats.restype = wt.UINT
        u.GetClipboardData.argtypes = [wt.UINT]
        u.GetClipboardData.restype = wt.HANDLE
        u.SetClipboardData.argtypes = [wt.UINT, wt.HANDLE]
        u.SetClipboardData.restype = wt.HANDLE
        u.GetClipboardSequenceNumber.argtypes = []
        u.GetClipboardSequenceNumber.restype = wt.DWORD
        k.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
        k.GlobalAlloc.restype = wt.HGLOBAL
        k.GlobalFree.argtypes = [wt.HGLOBAL]
        k.GlobalFree.restype = wt.HGLOBAL
        k.GlobalLock.argtypes = [wt.HGLOBAL]
        k.GlobalLock.restype = ctypes.c_void_p
        k.GlobalUnlock.argtypes = [wt.HGLOBAL]
        k.GlobalUnlock.restype = wt.BOOL
        k.GlobalSize.argtypes = [wt.HGLOBAL]
        k.GlobalSize.restype = ctypes.c_size_t

    def open(self, retries: int = 20, delay: float = 0.01) -> None:
        for _ in range(max(1, retries)):
            if self.u.OpenClipboard(None):
                return
            time.sleep(delay)
        raise ClipboardError("OpenClipboard failed (clipboard busy)")

    def close(self) -> None:
        self.u.CloseClipboard()

    def formats(self) -> list[int]:
        out, fmt = [], 0
        while True:
            fmt = int(self.u.EnumClipboardFormats(fmt))
            if not fmt:
                break
            out.append(fmt)
        return out

    def get_data(self, fmt: int) -> bytes:
        h = self.u.GetClipboardData(int(fmt))
        if not h:
            return b""
        p = self.k.GlobalLock(h)
        if not p:
            return b""
        try:
            size = int(self.k.GlobalSize(h))
            return ctypes.string_at(p, size)
        finally:
            self.k.GlobalUnlock(h)

    def set_data(self, fmt: int, data: bytes) -> None:
        data = bytes(data)
        h = self.k.GlobalAlloc(GMEM_MOVEABLE, max(1, len(data)))
        if not h:
            raise ClipboardError("GlobalAlloc failed")
        p = self.k.GlobalLock(h)
        if not p:
            self.k.GlobalFree(h)
            raise ClipboardError("GlobalLock failed")
        ctypes.memmove(p, data, len(data))
        self.k.GlobalUnlock(h)
        if not self.u.SetClipboardData(int(fmt), h):
            err = ctypes.get_last_error()
            self.k.GlobalFree(h)
            raise ClipboardError(
                f"SetClipboardData failed for format {fmt} "
                f"(GetLastError={err})")
        # Ownership transferred to the OS: do not GlobalFree on success.

    def clear(self) -> None:
        self.u.EmptyClipboard()

    def set_text(self, text: str) -> None:
        self.open()
        try:
            self.clear()
            self.set_data(CF_UNICODETEXT, _encode_unicode(text))
        finally:
            self.close()

    def sequence_number(self) -> int:
        return int(self.u.GetClipboardSequenceNumber())


def snapshot(backend) -> ClipboardSnapshot:
    backend.open()
    try:
        saved: dict[int, bytes] = {}
        skipped: list[int] = []
        for fmt in backend.formats():
            if fmt in SKIP_FORMATS:
                skipped.append(fmt)
                continue
            try:
                saved[fmt] = backend.get_data(fmt)
            except Exception:
                skipped.append(fmt)
        return ClipboardSnapshot(formats=saved, skipped=skipped,
                                 seq=backend.sequence_number())
    finally:
        backend.close()


@dataclass
class RestoreReport:
    attempted: int
    restored: int
    failures: list[tuple[int, str]]

    @property
    def complete(self) -> bool:
        return not self.failures


def restore(backend, snap: ClipboardSnapshot) -> RestoreReport:
    """Best-effort restore of every captured format.

    EmptyClipboard() already wiped the clipboard by this point, so a single
    failing format must NOT abort the rest: that would leave the user with an
    empty clipboard. Text formats go first, then everything else, and any
    failure is reported instead of hidden.
    """
    priority = (CF_UNICODETEXT, CF_TEXT, CF_OEMTEXT, CF_LOCALE)
    ordered = sorted(snap.formats.items(),
                     key=lambda kv: (0 if kv[0] in priority else 1, kv[0]))
    failures: list[tuple[int, str]] = []
    backend.open()
    try:
        backend.clear()
        for fmt, data in ordered:
            try:
                backend.set_data(fmt, data)
            except Exception as e:
                failures.append((int(fmt), str(e)))
    finally:
        backend.close()
    return RestoreReport(attempted=len(ordered),
                         restored=len(ordered) - len(failures),
                         failures=failures)


class ClipboardGuard:
    def __init__(self, backend) -> None:
        self.backend = backend

    def snapshot(self) -> ClipboardSnapshot:
        return snapshot(self.backend)

    def restore(self, snap: ClipboardSnapshot) -> RestoreReport:
        return restore(self.backend, snap)

    @contextmanager
    def protect(self):
        """Always restore, including when insertion raises."""
        s = self.snapshot()
        try:
            yield s
        finally:
            self.restore(s)


@dataclass(frozen=True)
class InsertResult:
    ok: bool
    seq_before: int
    seq_after: int
    competed: bool = False
    restored: bool = False
    skipped_formats: int = 0
    error: str = ""
    restore_failures: int = 0
    restore_failure_formats: str = ""


def send_ctrl_v() -> None:
    """Send Ctrl+V with SendInput. Injectable via `paste` in insert_text."""
    import ctypes.wintypes as _wt

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", _wt.WORD), ("wScan", _wt.WORD),
                    ("dwFlags", _wt.DWORD), ("time", _wt.DWORD),
                    ("dwExtraInfo", ctypes.c_ulonglong)]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", _wt.LONG), ("dy", _wt.LONG),
                    ("mouseData", _wt.DWORD), ("dwFlags", _wt.DWORD),
                    ("time", _wt.DWORD), ("dwExtraInfo", ctypes.c_ulonglong)]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", _wt.DWORD), ("wParamL", _wt.WORD),
                    ("wParamH", _wt.WORD)]

    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT),
                    ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", _wt.DWORD), ("u", _U)]

    u = ctypes.WinDLL("user32", use_last_error=True)
    u.SendInput.argtypes = [_wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    u.SendInput.restype = _wt.UINT

    KEYEVENTF_KEYUP = 0x0002
    INPUT_KEYBOARD = 1
    events = [(0x11, 0), (0x56, 0), (0x56, KEYEVENTF_KEYUP),
              (0x11, KEYEVENTF_KEYUP)]
    arr = (INPUT * len(events))()
    for i, (vk, flags) in enumerate(events):
        arr[i].type = INPUT_KEYBOARD
        arr[i].ki.wVk = vk
        arr[i].ki.wScan = 0
        arr[i].ki.dwFlags = flags
        arr[i].ki.time = 0
        arr[i].ki.dwExtraInfo = 0
    sent = u.SendInput(len(arr), arr, ctypes.sizeof(INPUT))
    if sent != len(events):
        raise ClipboardError(f"SendInput sent {sent}/{len(events)} events")


@dataclass
class PendingInsert:
    """Deferred clipboard restore.

    SendInput(Ctrl+V) only *posts* a message to the foreground thread's queue.
    Restoring the clipboard immediately (or after a plain time.sleep on the
    GUI thread, which pumps nothing) lets the target app read the already
    restored clipboard -- verified in _spike/focus_diag.py: the paste never
    landed. Callers must keep the clipboard alive until the target has had
    time to consume it, then call restore().
    """

    snapshot: ClipboardSnapshot
    guard: ClipboardGuard
    backend: object
    seq_after_set: int

    def restore(self) -> RestoreReport:
        return self.guard.restore(self.snapshot)

    @property
    def competed(self) -> bool:
        return self.backend.sequence_number() != self.seq_after_set


def paste_text(text: str, *, backend=None,
               guard: Optional[ClipboardGuard] = None,
               paste: Optional[Callable] = None) -> tuple[InsertResult,
                                                          PendingInsert]:
    """Set the clipboard and send Ctrl+V WITHOUT restoring yet.

    Returns (result, pending). The caller MUST eventually call
    pending.restore(), otherwise the user's clipboard is left overwritten.
    """
    backend = backend or Win32Clipboard()
    guard = guard or ClipboardGuard(backend)
    paste = paste or send_ctrl_v
    seq_before = backend.sequence_number()
    snap = guard.snapshot()
    error = ""
    try:
        backend.set_text(text)
        seq_after_set = backend.sequence_number()
        paste()
        ok = True
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        ok = False
        seq_after_set = backend.sequence_number()
    result = InsertResult(ok, seq_before, seq_before, False, False,
                          len(snap.skipped), error)
    pending = PendingInsert(snapshot=snap, guard=guard, backend=backend,
                            seq_after_set=seq_after_set)
    return result, pending


def finish_insert(result: InsertResult, pending: PendingInsert) -> InsertResult:
    """Restore the clipboard and complete an InsertResult."""
    try:
        report = pending.restore()
        restored = report.complete
        failures = len(report.failures)
        detail = ", ".join(str(f) for f, _ in report.failures)
    except Exception as e:
        restored, failures, detail = False, -1, str(e)
    return replace(
        result,
        competed=pending.competed,
        restored=restored,
        seq_after=pending.backend.sequence_number(),
        restore_failures=failures,
        restore_failure_formats=detail,
    )


def insert_text(text: str, *, backend=None, guard: Optional[ClipboardGuard] = None,
                paste: Optional[Callable] = None, settle: float = 0.06,
                pump: Optional[Callable[[float], None]] = None) -> InsertResult:
    """Synchronous insert for tests / CLI.

    `pump(seconds)` lets a GUI harness drive its own message loop while the
    clipboard still holds `text`; without it the target never sees the paste.
    Real tray usage should use paste_text() + a delayed finish_insert().
    """
    result, pending = paste_text(text, backend=backend, guard=guard,
                                 paste=paste)
    if settle:
        if pump is not None:
            pump(settle)
        else:
            time.sleep(settle)
    return finish_insert(result, pending)
