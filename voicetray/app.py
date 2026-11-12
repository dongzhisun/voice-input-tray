"""Single-process VoiceTray application.

Threading layout (no engine worker subprocess, no IPC):

    main / Tk thread : state machine, target snapshot/confirm, clipboard
                       insertion, status overlay, settings, tray commands
    hook thread      : WH_KEYBOARD_LL detect-only, pushes raw events
    ASR thread       : one OfflineRecognizer.decode_stream per task
    tray thread      : pystray icon + menu
    model thread     : SHA-256 verify / optional download / engine load

IMPORTANT: Tkinter's `after()` is NOT thread-safe. Calling it from an ASR,
tray, or model thread raises "RuntimeError: main thread is not in main loop"
(verified during integration tests). Worker threads therefore push callables
onto `self._calls`, and the Tk `after` pump drains that queue on the main
thread.
"""
from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import tkinter as tk

from . import audio, clipboard, config, manifest, target
from .engine import SenseVoiceEngine
from .flow import VoiceFlow
from .hotkey import HotkeyDecoder, KeyAction, KeyboardHook
from .state import Phase, Token, VoiceState
from .ui import SettingsWindow, StatusWindow, make_tray_image


class VoiceTrayApp:
    def __init__(self, cfg: config.Config, warnings: Optional[list[str]] = None,
                 *, clipboard_backend=None, paste=None,
                 restore_delay_ms: int = 350) -> None:
        self.cfg = cfg
        self.warnings = warnings or []
        self.model_dir = Path(cfg.model_dir)
        self._stopping = False
        self._calls: queue.Queue = queue.Queue()
        # Ctrl+V is asynchronous: keep our text on the clipboard long enough
        # for the target app to read it before restoring the user's content.
        self.restore_delay_ms = int(restore_delay_ms)
        self._pending_restore = None

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("VoiceTray")
        self.root.protocol("WM_DELETE_WINDOW", self.hide_root)

        self.status = StatusWindow(self.root, cfg.show_status_window)
        self.state = VoiceState()
        self.engine: Optional[SenseVoiceEngine] = None
        self.engine_error = ""
        self.flow: Optional[VoiceFlow] = None
        self.decoder = HotkeyDecoder(cfg.allow_injected)
        self.hook = KeyboardHook()
        self.recorder: Optional[audio.Recorder] = None
        self.buffer: Optional[audio.AudioBuffer] = None
        self.token: Optional[Token] = None
        self.icon = None

        self._clipboard_backend = clipboard_backend
        self._paste = paste or clipboard.send_ctrl_v

        self.settings = SettingsWindow(
            self.root, lambda: self.cfg, self._on_settings_saved,
            self._load_engine_async, self._run_selftest)

    # -- thread-safe posting ---------------------------------------------
    def post(self, fn: Callable, *args, **kwargs) -> None:
        """Schedule `fn` on the Tk thread. Safe from any worker thread."""
        self._calls.put((fn, args, kwargs))

    def _process_posted(self) -> None:
        while True:
            try:
                fn, args, kwargs = self._calls.get_nowait()
            except queue.Empty:
                break
            try:
                fn(*args, **kwargs)
            except Exception as e:
                self._recover("后台任务投递异常", e)

    # -- lifecycle -------------------------------------------------------
    def hide_root(self) -> None:
        self.root.withdraw()

    def run(self) -> None:
        for w in self.warnings:
            self.status.show("warning", w)
        self._load_engine_async()
        try:
            self.hook.start()
        except Exception as e:
            self.status.show("error", f"键盘钩子安装失败：{e}")
            self.engine_error = f"hook: {e}"
        threading.Thread(target=self._tray_thread, name="voicetray-tray",
                         daemon=True).start()
        self.root.after(15, self._pump)
        self.root.mainloop()

    def shutdown(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        self._flush_pending_restore()
        try:
            self.hook.stop()
        except Exception:
            pass
        if self.recorder is not None and self.recorder.active:
            self.recorder.abort()
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                pass
        self.root.after(30, self.root.destroy)

    # -- engine ----------------------------------------------------------
    def _load_engine_async(self) -> None:
        if self._stopping:
            return
        self.engine = None
        self.flow = None
        self.engine_error = ""
        self.status.show("info", "正在加载离线模型…")

        def worker():
            try:
                if not manifest.is_ready(self.model_dir):
                    def progress(name, done, total):
                        pct = f"{done * 100 // max(1, total)}%"
                        self.post(self.status.show, "info",
                                  f"下载模型 {name} {pct}")
                    manifest.ensure_model(self.model_dir, progress=progress)
                engine = SenseVoiceEngine(
                    self.model_dir, verify=True,
                    num_threads=self.cfg.num_threads,
                    language=self.cfg.language, use_itn=self.cfg.use_itn)
                self.post(self._engine_ready, engine, "")
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                self.post(self._engine_ready, None, msg)

        threading.Thread(target=worker, name="voicetray-model",
                         daemon=True).start()

    def _engine_ready(self, engine, error: str) -> None:
        if self._stopping:
            return
        if engine is None:
            self.engine_error = error
            self.status.show("error", f"模型加载失败：{error}")
            return
        self.engine = engine
        self.flow = VoiceFlow(
            state=self.state, engine=engine,
            capture=target.capture, insert=self._do_insert,
            notify=self._notify, use_uia=self.cfg.use_uia,
            clock=time.monotonic)
        self.status.show("inserted", "离线模型已就绪 · 按住右 Alt 开始说话")

    # -- event pump ------------------------------------------------------
    def _pump(self) -> None:
        if self._stopping:
            return
        self._process_posted()
        while True:
            raw = self.hook.poll(timeout=0)
            if raw is None:
                break
            message, vk, flags, scan = raw
            ev = self.decoder.feed(message, vk, flags, scan)
            try:
                if ev.action is KeyAction.PRESS:
                    self._on_press()
                elif ev.action is KeyAction.RELEASE:
                    self._on_release()
                elif ev.action is KeyAction.CANCEL:
                    self._on_esc()
            except Exception as e:
                self._recover("快捷键处理异常", e)
        self.root.after(15, self._pump)

    # -- hotkey actions (always on the Tk thread) ------------------------
    def _on_press(self) -> None:
        # A previous task's clipboard restore may still be scheduled; flush
        # it before taking a new snapshot, otherwise we would snapshot our
        # own transient text as if it were the user's clipboard.
        self._flush_pending_restore()
        if self.flow is None or self.engine is None:
            reason = self.engine_error or "模型尚未加载完成"
            self.status.show("warning", f"暂不可用：{reason}")
            return
        if self.state.phase is Phase.ERROR:
            self.state.clear_error()

        self.buffer = audio.AudioBuffer(self.cfg.sample_rate,
                                        self.cfg.max_record_seconds)
        token = self.flow.on_press()
        if token is None:
            return
        try:
            device = audio.choose_device(self.cfg.device,
                                         self.cfg.sample_rate)
            self.recorder = audio.Recorder(
                device=device, sample_rate=self.cfg.sample_rate)
            self.recorder.start(self.buffer)
        except Exception as e:
            self.flow.on_error(token, f"麦克风不可用：{e}")
            self.recorder = None
            return
        self.token = token

    def _on_release(self) -> None:
        token, self.token = self.token, None
        if token is None or self.flow is None:
            return
        try:
            samples = self.recorder.stop() if self.recorder else None
        except Exception as e:
            self.flow.on_error(token, f"停止录音失败：{e}")
            return
        if samples is None or samples.size == 0:
            self.flow.on_error(token, "未采集到音频（请检查麦克风）")
            return
        if self.buffer is not None and self.buffer.overflow:
            self.status.show("warning", "录音已达上限，超出部分被截断")
        self.state.start_transcribing()
        self.status.show("transcribing", "松开按键，正在离线转写…")
        threading.Thread(target=self._decode_worker,
                         args=(token, samples), name="voicetray-asr",
                         daemon=True).start()

    def _decode_worker(self, token: Token, samples) -> None:
        # Never touch VoiceState or Tk here: the native decode cannot be
        # cancelled. A model reload may invalidate `self.flow` mid-decode.
        flow = self.flow
        if flow is None:
            return
        text, error = flow.decode(token, samples, self.cfg.sample_rate)
        if self._stopping:
            return
        self.post(self._on_decoded, token, text, error)

    def _on_decoded(self, token: Token, text: str, error: str) -> None:
        if self.flow is None or self._stopping:
            return
        if error:
            self.flow.on_error(token, f"转写失败：{error}")
        else:
            self.flow.on_result(token, text)

    def _on_esc(self) -> None:
        if self.recorder is not None and self.recorder.active:
            self.recorder.abort()
        if self.flow is not None:
            self.flow.on_cancel()
        self.token = None

    def _recover(self, context: str, exc: Exception) -> None:
        self.state.fail(f"{context}: {exc}")
        self.token = None
        if self.recorder is not None and self.recorder.active:
            try:
                self.recorder.abort()
            except Exception:
                pass
        self.status.show("error", f"{context}：{exc}")

    # -- insertion -------------------------------------------------------
    def _do_insert(self, text: str) -> bool:
        # Hide our overlay before SendInput so it can never be the paste
        # target, even if WS_EX_NOACTIVATE is unsupported on a given host.
        self.status.hide()
        result, pending = clipboard.paste_text(
            text, backend=self._clipboard_backend, paste=self._paste)
        if not result.ok:
            self.status.show("error", f"插入失败：{result.error}")
            clipboard.finish_insert(result, pending)
            return False
        if self.restore_delay_ms <= 0:
            self._finish_restore(pending)
        else:
            self._schedule_restore(pending)
        return True

    def _schedule_restore(self, pending) -> None:
        self._flush_pending_restore()
        self._pending_restore = pending

        def run():
            if self._pending_restore is pending:
                self._pending_restore = None
            self._finish_restore(pending)

        self.root.after(self.restore_delay_ms, run)

    def _finish_restore(self, pending) -> None:
        try:
            report = pending.restore()
        except Exception as e:
            self.status.show("warning", f"剪贴板恢复失败：{e}")
            return
        if not report.complete:
            self.status.show(
                "warning",
                f"剪贴板有 {len(report.failures)} 个格式未能还原（其余已恢复）")

    def _flush_pending_restore(self) -> None:
        pending, self._pending_restore = self._pending_restore, None
        if pending is not None:
            self._finish_restore(pending)

    def _notify(self, level: str, message: str) -> None:
        self.status.show(level, message)

    # -- settings / selftest --------------------------------------------
    def _on_settings_saved(self, new_cfg: config.Config) -> None:
        try:
            new_cfg.save()
        except Exception as e:
            self.status.show("error", f"配置保存失败：{e}")
            return
        engine_params_changed = any([
            new_cfg.num_threads != self.cfg.num_threads,
            new_cfg.language != self.cfg.language,
            new_cfg.use_itn != self.cfg.use_itn,
            new_cfg.model_dir != self.cfg.model_dir,
        ])
        self.cfg = new_cfg
        self.model_dir = Path(new_cfg.model_dir)
        self.decoder.allow_injected = new_cfg.allow_injected
        if self.flow is not None:
            self.flow.use_uia = new_cfg.use_uia
        self.status.set_visible(new_cfg.show_status_window)
        if engine_params_changed:
            self._load_engine_async()
        self.status.show("inserted", "设置已保存")

    def _run_selftest(self) -> None:
        from .cli import selftest
        try:
            report = selftest(model_dir=self.model_dir)
        except Exception as e:
            self.status.show("error", f"自检失败：{e}")
            return
        ok = report["model_ready"] and report["hook_ok"]
        summary = (f"模型 {'✓' if report['model_ready'] else '✗'} · "
                   f"钩子 {'✓' if report['hook_ok'] else '✗'} · "
                   f"输入设备 {len(report['devices'])} 个 · "
                   f"UIA {'✓' if report['uia_ok'] else '降级'}")
        self.status.show("inserted" if ok else "error", summary)
        if report.get("warnings"):
            self.status.show("warning", "；".join(report["warnings"])[:160])

    # -- tray ------------------------------------------------------------
    def _status_text(self) -> str:
        if self.engine is None:
            return "模型加载中/不可用"
        return {
            Phase.IDLE: "待命（按住右 Alt）",
            Phase.RECORDING: "录音中…",
            Phase.TRANSCRIBING: "转写中…",
            Phase.INSERTING: "插入中…",
            Phase.ERROR: "出错（按设置查看）",
        }.get(self.state.phase, "未知")

    def _tray_thread(self) -> None:
        try:
            import pystray
        except Exception as e:
            self.post(self.status.show, "error", f"托盘不可用：{e}")
            return

        def on_menu(action):
            return lambda icon, item: self.post(action)

        menu = pystray.Menu(
            pystray.MenuItem(lambda item: "VoiceTray · " + self._status_text(),
                             None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("设置…", on_menu(self.settings.open), default=True),
            pystray.MenuItem("运行自检", on_menu(self._run_selftest)),
            pystray.MenuItem("重新加载模型", on_menu(self._load_engine_async)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", on_menu(self.shutdown)),
        )
        self.icon = pystray.Icon("voicetray", make_tray_image(),
                                 "VoiceTray · 离线语音输入", menu)
        try:
            self.icon.run()
        except Exception as e:
            if not self._stopping:
                self.post(self.status.show, "error", f"托盘异常：{e}")
