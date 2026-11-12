"""Tkinter status overlay and settings window.

The status overlay MUST NOT take foreground focus, otherwise the target
snapshot taken at press time would be invalidated by our own window and
every insertion would be rejected as "target changed". We therefore set
WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW on its native HWND.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional

from . import audio, manifest
from .config import Config

_BG = "#20242e"
_COLORS = {
    "recording": "#ff6b6b",
    "transcribing": "#ffd166",
    "inserted": "#4ade80",
    "info": "#93c5fd",
    "warning": "#fbbf24",
    "error": "#f87171",
}
_PREFIX = {
    "recording": "● 录音中",
    "transcribing": "◌ 转写中",
    "inserted": "✓ 已插入",
    "info": "ℹ",
    "warning": "⚠",
    "error": "✕ 错误",
}


def make_tray_image(color: str = "#5b8cff"):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((23, 8, 41, 38), radius=9, fill=color)
    d.arc((12, 22, 52, 56), start=0, end=180, fill=color, width=5)
    d.line((32, 50, 32, 59), fill=color, width=5)
    d.line((23, 59, 41, 59), fill=color, width=5)
    return img


def _apply_noactivate(win: tk.Misc) -> bool:
    try:
        u = ctypes.windll.user32
        u.GetParent.argtypes = [wt.HWND]
        u.GetParent.restype = wt.HWND
        u.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
        u.GetWindowLongW.restype = ctypes.c_long
        u.SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_long]
        u.SetWindowLongW.restype = ctypes.c_long
        hwnd = u.GetParent(win.winfo_id()) or win.winfo_id()
        GWL_EXSTYLE = -20
        WS_EX_NOACTIVATE = 0x08000000
        WS_EX_TOOLWINDOW = 0x00000080
        style = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
        u.SetWindowLongW(hwnd, GWL_EXSTYLE,
                         style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
        return True
    except Exception:
        return False


class StatusWindow:
    """Frameless, always-on-top, focus-stealing-free status pill."""

    def __init__(self, root: tk.Tk, visible: bool = True) -> None:
        self.root = root
        self.visible = visible
        self.noactivate_ok = False
        self._hide_job: Optional[str] = None

        self.win = tk.Toplevel(root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", 0.93)
        self.win.configure(bg=_BG)
        self.label = tk.Label(
            self.win, text="", fg=_COLORS["info"], bg=_BG,
            font=("Microsoft YaHei UI", 10), padx=16, pady=9,
            justify="left", wraplength=340, anchor="w")
        self.label.pack(fill="both")
        self.win.update_idletasks()
        self.noactivate_ok = _apply_noactivate(self.win)
        self._place()

    def _place(self) -> None:
        self.win.update_idletasks()
        w = max(self.win.winfo_width(), 200)
        h = max(self.win.winfo_height(), 36)
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        self.win.geometry(f"+{max(0, sw - w - 28)}+{max(0, sh - h - 80)}")

    def set_visible(self, value: bool) -> None:
        self.visible = value
        if not value:
            self.hide()

    def hide(self) -> None:
        if self._hide_job is not None:
            try:
                self.root.after_cancel(self._hide_job)
            except Exception:
                pass
            self._hide_job = None
        self.win.withdraw()

    def show(self, level: str, message: str) -> None:
        # Errors are always visible; other levels follow the user's toggle.
        if not self.visible and level != "error":
            return
        color = _COLORS.get(level, _COLORS["info"])
        prefix = _PREFIX.get(level, "")
        text = f"{prefix}  {message}" if prefix else message
        self.label.configure(text=text, fg=color)
        self.win.deiconify()
        self.win.lift()
        self._place()
        if self._hide_job is not None:
            try:
                self.root.after_cancel(self._hide_job)
            except Exception:
                pass
            self._hide_job = None
        if level in ("recording", "transcribing"):
            return  # stay until the phase ends
        ttl = 8000 if level == "error" else (4000 if level in
                                             ("warning", "inserted") else 2500)
        self._hide_job = self.root.after(ttl, self.hide)


class SettingsWindow:
    def __init__(self, root: tk.Tk, get_config: Callable[[], Config],
                 on_save: Callable[[Config], None],
                 on_reload_model: Callable[[], None],
                 on_selftest: Callable[[], None]) -> None:
        self.root = root
        self.get_config = get_config
        self.on_save = on_save
        self.on_reload_model = on_reload_model
        self.on_selftest = on_selftest
        self.win: Optional[tk.Toplevel] = None

    def open(self) -> None:
        if self.win is not None and self.win.winfo_exists():
            self.win.deiconify()
            self.win.lift()
            self._refresh_model_status()
            return
        self._build()

    def _devices(self) -> list[str]:
        try:
            devs = audio.list_input_devices()
            return ["自动（优先原生 16kHz）"] + [
                f"[{d['index']}] {d['name']} ({d['rate']} Hz)"
                for d in devs]
        except Exception as e:
            return ["自动（优先原生 16kHz）", f"（设备枚举失败：{e}）"]

    def _build(self) -> None:
        cfg = self.get_config()
        self.win = win = tk.Toplevel(self.root)
        win.title("VoiceTray 设置")
        win.resizable(False, False)
        win.geometry("480x470")
        win.protocol("WM_DELETE_WINDOW", win.withdraw)

        frm = ttk.Frame(win, padding=16)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        row = 0

        def label(text):
            nonlocal row
            ttk.Label(frm, text=text).grid(column=0, row=row, sticky="w",
                                          pady=5)
            return row

        r = label("麦克风设备")
        self.var_device = tk.StringVar()
        devices = self._devices()
        chosen = devices[0]
        if cfg.device is not None:
            for d in devices:
                if d.startswith(f"[{cfg.device}]"):
                    chosen = d
                    break
        self.cmb_device = ttk.Combobox(frm, textvariable=self.var_device,
                                       values=devices, state="readonly")
        self.cmb_device.set(chosen)
        self.cmb_device.grid(column=1, row=r, sticky="ew", pady=5)

        row += 1
        r = label("识别语言")
        self.var_lang = tk.StringVar(value=cfg.language)
        ttk.Combobox(frm, textvariable=self.var_lang, state="readonly",
                     values=["auto", "zh", "en", "ja", "ko", "yue"]
                     ).grid(column=1, row=r, sticky="ew", pady=5)

        row += 1
        r = label("推理线程数")
        self.var_threads = tk.IntVar(value=cfg.num_threads)
        ttk.Spinbox(frm, from_=1, to=16, textvariable=self.var_threads
                    ).grid(column=1, row=r, sticky="ew", pady=5)

        row += 1
        r = label("最长录音（秒）")
        self.var_maxsec = tk.DoubleVar(value=cfg.max_record_seconds)
        ttk.Spinbox(frm, from_=1, to=300, increment=5,
                    textvariable=self.var_maxsec
                    ).grid(column=1, row=r, sticky="ew", pady=5)

        row += 1
        self.var_uia = tk.BooleanVar(value=cfg.use_uia)
        ttk.Checkbutton(frm, text="使用 UIA 输入框指纹（网页输入位置更可靠）",
                        variable=self.var_uia).grid(column=0, row=row,
                                                    columnspan=2, sticky="w",
                                                    pady=3)
        row += 1
        self.var_injected = tk.BooleanVar(value=cfg.allow_injected)
        ttk.Checkbutton(frm, text="允许模拟按键（自动化验收用）",
                        variable=self.var_injected).grid(column=0, row=row,
                                                         columnspan=2,
                                                         sticky="w", pady=3)
        row += 1
        self.var_overlay = tk.BooleanVar(value=cfg.show_status_window)
        ttk.Checkbutton(frm, text="显示状态浮窗",
                        variable=self.var_overlay).grid(column=0, row=row,
                                                        columnspan=2,
                                                        sticky="w", pady=3)

        row += 1
        ttk.Separator(frm).grid(column=0, row=row, columnspan=2,
                                sticky="ew", pady=10)
        row += 1
        self.lbl_model = ttk.Label(frm, text="", wraplength=440,
                                   foreground="#444")
        self.lbl_model.grid(column=0, row=row, columnspan=2, sticky="w")
        self._refresh_model_status()

        row += 1
        btns = ttk.Frame(frm)
        btns.grid(column=0, row=row, columnspan=2, sticky="ew", pady=(14, 0))
        ttk.Button(btns, text="保存", command=self._save).pack(side="left")
        ttk.Button(btns, text="重新加载模型",
                   command=self.on_reload_model).pack(side="left", padx=6)
        ttk.Button(btns, text="运行自检",
                   command=self.on_selftest).pack(side="left")
        ttk.Button(btns, text="关闭",
                   command=win.withdraw).pack(side="right")

        row += 1
        ttk.Label(frm, text=(
            "快捷键固定为「按住右 Alt」，Esc 取消。\n"
            "插入始终先保存并在完成后恢复剪贴板。\n"
            "权重许可证见随包 LICENSE（FunASR），非 Apache-2.0。"),
            foreground="#666", justify="left"
        ).grid(column=0, row=row, columnspan=2, sticky="w", pady=(14, 0))

    def _refresh_model_status(self) -> None:
        if self.win is None or not self.win.winfo_exists():
            return
        from pathlib import Path
        model_dir = Path(self.get_config().model_dir)
        try:
            status = manifest.verify_dir(model_dir)
            bad = [k for k, v in status.items() if not v.valid]
            text = ("模型：已校验 ✓（离线可用）" if not bad else
                    "模型：缺失或损坏 → " + ", ".join(bad))
        except Exception as e:
            text = f"模型：检查失败 {e}"
        self.lbl_model.configure(text=text + f"\n目录：{model_dir}")

    def _save(self) -> None:
        cfg = self.get_config()
        chosen = self.var_device.get()
        device = None
        if chosen.startswith("["):
            try:
                device = int(chosen[1:chosen.index("]")])
            except ValueError:
                device = None
        new = Config(
            hotkey="rmenu", device=device, sample_rate=16000,
            language=self.var_lang.get(), use_itn=cfg.use_itn,
            num_threads=int(self.var_threads.get()),
            max_record_seconds=float(self.var_maxsec.get()),
            allow_injected=bool(self.var_injected.get()),
            use_uia=bool(self.var_uia.get()),
            model_dir=cfg.model_dir,
            show_status_window=bool(self.var_overlay.get()),
        )
        problems = new.validate()
        if problems:
            from tkinter import messagebox
            messagebox.showerror("设置无效", "\n".join(problems),
                                 parent=self.win)
            return
        self.on_save(new)
