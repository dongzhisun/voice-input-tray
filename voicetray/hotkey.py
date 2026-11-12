"""Low-level keyboard decoding and a detect-only WH_KEYBOARD_LL hook.

Measured on this host (_spike/spike_result.json):

    SendInput(VK_RMENU) down -> WM_SYSKEYDOWN 0x0104, vk=0xA5, extended
    SendInput(VK_RMENU) up   -> WM_KEYUP      0x0101, vk=0xA5, extended

The down/up messages are asymmetric, so direction is derived from the
message family, never from vkCode. The hook is detect-only: it always
returns CallNextHookEx(...) and never swallows an event.

ctypes note (learned the hard way in _spike/input_spike.py): LPARAM must be
declared as c_ssize_t. With the default int conversion the 64-bit pointer
is truncated and the callback raises OverflowError.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import queue
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Optional

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_QUIT = 0x0012
WH_KEYBOARD_LL = 13

VK_MENU = 0x12
VK_RMENU = 0xA5
VK_ESCAPE = 0x1B
VK_CONTROL = 0x11
VK_V = 0x56

LLKHF_EXTENDEDKEY = 0x01
LLKHF_INJECTED = 0x10
LLKHF_UP = 0x80

DOWN_MESSAGES = frozenset((WM_KEYDOWN, WM_SYSKEYDOWN))
UP_MESSAGES = frozenset((WM_KEYUP, WM_SYSKEYUP))


class KeyAction(str, Enum):
    PRESS = "press"
    RELEASE = "release"
    CANCEL = "cancel"
    IGNORE = "ignore"


@dataclass(frozen=True)
class KeyEvent:
    action: KeyAction
    vk: int = 0
    message: int = 0
    flags: int = 0
    scan: int = 0
    injected: bool = False
    reason: str = ""


def is_right_alt(vk: int, flags: int = 0) -> bool:
    """Right Alt identification.

    Injected events (SendInput VK_RMENU) arrive with vk == 0xA5. Physical
    right Alt commonly arrives as vk == VK_MENU (0x12) with the extended-key
    flag set and scan code 0x38; left Alt has neither.
    """
    if vk == VK_RMENU:
        return True
    return vk == VK_MENU and bool(flags & LLKHF_EXTENDEDKEY)


class HotkeyDecoder:
    """Stateful decoder: de-duplicates auto-repeat, pairs press/release,
    and turns Esc into a cancel signal. Pure logic, fully unit-testable."""

    def __init__(self, allow_injected: bool = True) -> None:
        self.allow_injected = allow_injected
        self._held = False
        self._esc_held = False

    @property
    def held(self) -> bool:
        return self._held

    def reset(self) -> None:
        self._held = False
        self._esc_held = False

    def feed(self, message: int, vk: int, flags: int = 0,
             scan: int = 0) -> KeyEvent:
        injected = bool(flags & LLKHF_INJECTED)
        base = dict(vk=vk, message=message, flags=flags, scan=scan,
                    injected=injected)
        if injected and not self.allow_injected:
            return KeyEvent(KeyAction.IGNORE, reason="injected-blocked", **base)

        down = message in DOWN_MESSAGES
        up = (message in UP_MESSAGES or bool(flags & LLKHF_UP)) and not down
        if not (down or up):
            return KeyEvent(KeyAction.IGNORE, reason="not-a-key-message", **base)

        if is_right_alt(vk, flags):
            if down:
                if self._held:
                    return KeyEvent(KeyAction.IGNORE, reason="auto-repeat", **base)
                self._held = True
                return KeyEvent(KeyAction.PRESS, reason="rmenu-down", **base)
            if self._held:
                self._held = False
                return KeyEvent(KeyAction.RELEASE, reason="rmenu-up", **base)
            return KeyEvent(KeyAction.IGNORE, reason="unmatched-release", **base)

        if vk == VK_ESCAPE:
            if down and not self._esc_held:
                self._esc_held = True
                return KeyEvent(KeyAction.CANCEL, reason="escape", **base)
            if up:
                self._esc_held = False
            return KeyEvent(KeyAction.IGNORE,
                            reason="escape-repeat-or-up", **base)

        return KeyEvent(KeyAction.IGNORE, reason="unrelated-key", **base)


# --------------------------------------------------------------------------
# Win32 hook plumbing (only imported/initialised when actually started)
# --------------------------------------------------------------------------
class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wt.DWORD),
        ("scanCode", wt.DWORD),
        ("flags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.c_ulonglong),
    ]


_user32 = None
_kernel32 = None
_HOOKPROC = None


def _win():
    global _user32, _kernel32, _HOOKPROC
    if _user32 is None:
        u = ctypes.WinDLL("user32", use_last_error=True)
        k = ctypes.WinDLL("kernel32", use_last_error=True)

        u.CallNextHookEx.argtypes = [wt.HHOOK, ctypes.c_int,
                                    wt.WPARAM, ctypes.c_ssize_t]
        u.CallNextHookEx.restype = ctypes.c_ssize_t
        u.SetWindowsHookExW.argtypes = [ctypes.c_int, wt.LPVOID,
                                        wt.HINSTANCE, wt.DWORD]
        u.SetWindowsHookExW.restype = wt.HHOOK
        u.UnhookWindowsHookEx.argtypes = [wt.HHOOK]
        u.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND,
                                  wt.UINT, wt.UINT]
        u.GetMessageW.restype = ctypes.c_int
        u.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
        u.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
        u.DispatchMessageW.restype = ctypes.c_ssize_t
        u.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM,
                                         ctypes.c_ssize_t]
        k.GetCurrentThreadId.argtypes = []
        k.GetCurrentThreadId.restype = wt.DWORD

        _HOOKPROC = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, ctypes.c_int, wt.WPARAM, ctypes.c_ssize_t)
        _user32, _kernel32 = u, k
    return _user32, _kernel32, _HOOKPROC


class KeyboardHook:
    """Owns a WH_KEYBOARD_LL hook on a dedicated message-pump thread.

    Events are pushed onto a queue as raw (message, vk, flags, scan) tuples;
    the application decodes them on the GUI thread. The callback object is
    kept alive for the hook's whole lifetime to avoid a GC-induced crash.
    """

    def __init__(self) -> None:
        self._queue: queue.Queue = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._proc = None
        self._hook = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._error: Optional[str] = None

    @property
    def error(self) -> Optional[str]:
        return self._error

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._ready.clear()
        self._error = None
        self._thread = threading.Thread(target=self._run,
                                        name="voicetray-hook", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=3.0)
        if self._error:
            raise RuntimeError(self._error)

    def _run(self) -> None:
        u, k, hookproc = _win()
        self._thread_id = int(k.GetCurrentThreadId())

        def on_key(nCode, wParam, lParam):
            if nCode >= 0:
                try:
                    kb = ctypes.cast(
                        lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                    self._queue.put_nowait((int(wParam), int(kb.vkCode),
                                           int(kb.flags), int(kb.scanCode)))
                except Exception:
                    pass
            return u.CallNextHookEx(self._hook or 0, nCode, wParam, lParam)

        self._proc = hookproc(on_key)
        self._hook = u.SetWindowsHookExW(
            WH_KEYBOARD_LL, ctypes.cast(self._proc, ctypes.c_void_p), None, 0)
        if not self._hook:
            self._error = ("SetWindowsHookExW failed, GetLastError="
                           + str(ctypes.get_last_error()))
            self._ready.set()
            return
        self._ready.set()

        msg = wt.MSG()
        while True:
            r = u.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r <= 0:
                break
            u.TranslateMessage(ctypes.byref(msg))
            u.DispatchMessageW(ctypes.byref(msg))
        u.UnhookWindowsHookEx(self._hook)
        self._hook = None

    def stop(self) -> None:
        if self._thread_id:
            u, _, _ = _win()
            u.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None

    def poll(self, timeout: float = 0.0):
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None
