"""Target snapshot + confirmation for safe insertion.

Win32 GetGUIThreadInfo cannot see the focused control inside Chromium web
pages (verified: hwndFocus == foreground hwnd, hwndCaret == 0). UIA can:
it returns RuntimeId / ClassName / ProcessId of the focused element.
We therefore build a composite fingerprint; any mismatch rejects insertion.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import hashlib
import json
from dataclasses import dataclass, asdict
from typing import Optional

user32 = ctypes.WinDLL("user32", use_last_error=True)


class RECT(ctypes.Structure):
    _fields_ = [("left", wt.LONG), ("top", wt.LONG),
                ("right", wt.LONG), ("bottom", wt.LONG)]


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD), ("flags", wt.DWORD),
        ("hwndActive", wt.HWND), ("hwndFocus", wt.HWND),
        ("hwndCapture", wt.HWND), ("hwndMenuOwner", wt.HWND),
        ("hwndMoveSize", wt.HWND), ("hwndCaret", wt.HWND),
        ("rcCaret", RECT),
    ]


user32.GetForegroundWindow.restype = wt.HWND
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetGUIThreadInfo.argtypes = [wt.DWORD, ctypes.POINTER(GUITHREADINFO)]
user32.GetClipboardSequenceNumber.restype = wt.DWORD


@dataclass(frozen=True)
class TargetSnapshot:
    hwnd: int = 0
    pid: int = 0
    tid: int = 0
    title: str = ""
    class_name: str = ""
    focus_hwnd: int = 0
    caret_hwnd: int = 0
    uia_runtime_id: str = ""
    uia_name: str = ""
    uia_class: str = ""
    uia_control_type: int = 0
    uia_pid: int = 0
    clipboard_seq: int = 0
    degraded: bool = False  # True when UIA was unavailable

    def _effective_focus_hwnd(self) -> int:
        """Chromium and similar windows report hwndFocus == the top-level
        hwnd (there is no distinct Win32 focus control). That value is not a
        usable identity signal and can briefly flip to 0 while focus settles
        after a window switch -- treating it as hard evidence produced a
        false "target changed" rejection in acceptance testing. Only a
        genuinely distinct, non-zero child HWND counts.
        """
        if not self.focus_hwnd or self.focus_hwnd == self.hwnd:
            return 0
        return self.focus_hwnd

    def fingerprint(self) -> str:
        """Fields that must not change between press and insert.

        clipboard_seq is deliberately excluded: other apps may legitimately
        touch the clipboard during recording. focus_hwnd is normalised so
        unobservable cases do not create spurious mismatches.
        """
        d = asdict(self)
        for k in ("clipboard_seq", "degraded"):
            d.pop(k, None)
        d["focus_hwnd"] = self._effective_focus_hwnd()
        return hashlib.sha256(
            json.dumps(d, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()

    def compare(self, other: "TargetSnapshot") -> list[str]:
        """Return the list of fields that differ (empty means confirmed)."""
        diff = []
        for f in ("hwnd", "pid", "tid", "title", "class_name"):
            if getattr(self, f) != getattr(other, f):
                diff.append(f)
        if self._effective_focus_hwnd() != other._effective_focus_hwnd():
            diff.append("focus_hwnd")
        # UIA is best-effort: only compare when both sides have it.
        if self.uia_runtime_id and other.uia_runtime_id and \
                self.uia_runtime_id != other.uia_runtime_id:
            diff.append("uia_runtime_id")
        return diff


_uia_automation = None
_uia_failed = False


def _get_uia():
    global _uia_automation, _uia_failed
    if _uia_failed:
        return None
    try:
        import comtypes
        import comtypes.client
        comtypes.CoInitialize()
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as U
        _uia_automation = comtypes.CoCreateInstance(
            U.CUIAutomation._reg_clsid_, interface=U.IUIAutomation,
            clsctx=comtypes.CLSCTX_INPROC_SERVER)
        return _uia_automation
    except Exception:
        _uia_failed = True
        return None


def _uia_focus() -> dict:
    a = _get_uia()
    if a is None:
        return {}
    try:
        el = a.GetFocusedElement()
        if el is None:
            return {}
        rid = el.GetRuntimeId()
        return {
            "uia_runtime_id": ",".join(str(x) for x in rid) if rid else "",
            "uia_name": el.CurrentName or "",
            "uia_class": el.CurrentClassName or "",
            "uia_control_type": int(el.CurrentControlType or 0),
            "uia_pid": int(el.CurrentProcessId or 0),
        }
    except Exception:
        return {}


def capture(use_uia: bool = True) -> TargetSnapshot:
    hwnd = user32.GetForegroundWindow() or 0
    pid = wt.DWORD()
    tid = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    title_buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, title_buf, 512)
    cls_buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, cls_buf, 256)

    g = GUITHREADINFO()
    g.cbSize = ctypes.sizeof(GUITHREADINFO)
    user32.GetGUIThreadInfo(0, ctypes.byref(g))

    uia = _uia_focus() if use_uia else {}
    degraded = use_uia and not uia.get("uia_runtime_id")

    return TargetSnapshot(
        hwnd=int(hwnd or 0), pid=pid.value, tid=tid,
        title=title_buf.value, class_name=cls_buf.value,
        focus_hwnd=int(g.hwndFocus or 0), caret_hwnd=int(g.hwndCaret or 0),
        clipboard_seq=int(user32.GetClipboardSequenceNumber()),
        degraded=degraded, **uia,
    )


def confirmed(before: TargetSnapshot, after: Optional["TargetSnapshot"] = None,
              use_uia: bool = True) -> tuple[bool, list[str]]:
    after = after if after is not None else capture(use_uia=use_uia)
    diff = before.compare(after)
    return (not diff), diff
