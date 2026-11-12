"""Application flow as injectable pure logic.

No Tk and no Win32 calls live here: capture / engine / insert / notify are
all injectable, so the full press -> record -> decode -> confirm -> insert
path is unit-testable without a real desktop.

Threading contract (single process, no IPC):
  GUI thread : on_press / on_result / on_error / on_cancel
  ASR thread : decode (never mutates shared state)
The GUI thread calls on_result only after the worker has posted its result
back through the Tk `after` pump.
"""
from __future__ import annotations

from typing import Callable, Optional

import numpy as np

from . import target as target_mod
from .state import Decision, Phase, Token, VoiceState


class VoiceFlow:
    def __init__(self, *, state: VoiceState, engine, capture=None,
                 insert: Optional[Callable[[str], bool]] = None,
                 notify: Optional[Callable[[str, str], None]] = None,
                 use_uia: bool = True, clock=None) -> None:
        self.state = state
        self.engine = engine
        self._capture = capture or target_mod.capture
        self._insert = insert or (lambda text: True)
        self._notify = notify or (lambda level, message: None)
        self.use_uia = use_uia
        self._clock = clock or (lambda: 0.0)
        self._snapshots: dict[str, target_mod.TargetSnapshot] = {}

    def _emit(self, level: str, message: str) -> None:
        self._notify(level, message)

    # -- GUI thread ------------------------------------------------------
    def on_press(self) -> Optional[Token]:
        if self.state.phase != Phase.IDLE:
            self._emit("warning", "已有任务在进行，忽略本次按下")
            return None
        try:
            snap = self._capture(use_uia=self.use_uia)
            fingerprint = snap.fingerprint()
        except Exception as e:
            self.state.fail(f"target-capture-failed: {e}")
            self._emit("error", f"无法记录输入目标：{e}")
            return None
        token = self.state.begin(fingerprint, now=self._clock())
        self._snapshots[token.task_id] = snap
        self._emit("recording", "录音中…（松开右 Alt 结束，Esc 取消）")
        return token

    def on_result(self, token: Token, text: str) -> Decision:
        before = self._snapshots.pop(token.task_id, None)
        if before is None:
            # Cancelled or already delivered: never insert.
            self._emit("info", "结果已过期，已丢弃")
            return Decision("drop", "missing-snapshot")

        try:
            after = self._capture(use_uia=self.use_uia)
            diff = before.compare(after)
        except Exception as e:
            self.state.fail(f"target-confirm-failed: {e}")
            self._emit("error", f"无法确认输入目标，已取消插入：{e}")
            return Decision("drop", "target-confirm-failed")

        # Use the tolerant field-by-field comparison, not a whole-hash
        # equality: UIA may be unavailable on one of the two samples.
        current_fp = before.fingerprint() if not diff else "__TARGET_CHANGED__"
        decision = self.state.accept_result(token, text, current_fp)

        if decision.action == "insert":
            try:
                ok = bool(self._insert(text))
            except Exception as e:
                self.state.fail(f"insert-failed: {e}")
                self._emit("error", f"插入失败：{e}")
                return Decision("drop", "insert-failed")
            if not ok:
                self.state.fail("insert-rejected")
                self._emit("error", "插入被拒绝，未修改目标内容")
                return Decision("drop", "insert-rejected")
            self.state.finish_insert()
            self._emit("inserted", f"已插入 {len(text)} 字")
        elif decision.reason == "target-mismatch":
            self._emit("warning",
                       "目标已变化（" + ", ".join(diff) + "），已取消插入")
        elif decision.reason.startswith("stale-"):
            self._emit("info", "任务已取消，迟到结果已丢弃")
        return decision

    def on_error(self, token: Token, message: str) -> None:
        self._snapshots.pop(token.task_id, None)
        self.state.fail(message)
        self._emit("error", message)

    def on_cancel(self, reason: str = "Esc 已取消，未插入任何内容") -> bool:
        if self.state.phase not in (Phase.RECORDING, Phase.TRANSCRIBING):
            return False
        self._snapshots.clear()
        self.state.cancel("user-cancel")
        self._emit("warning", reason)
        return True

    # -- ASR worker thread ----------------------------------------------
    def decode(self, token: Token, samples: np.ndarray,
               sample_rate: int = 16000) -> tuple[str, str]:
        """Never mutates shared state; safe to call off the GUI thread."""
        try:
            return self.engine.transcribe(samples, sample_rate), ""
        except Exception as e:
            return "", f"{type(e).__name__}: {e}"

    # -- convenience for tests / offline batch runs ----------------------
    def on_release_sync(self, token: Token, samples: np.ndarray,
                        sample_rate: int = 16000) -> Decision:
        """Synchronous full path, used by tests and the CLI self-check."""
        if self.state.phase != Phase.RECORDING:
            return Decision("drop", "not-recording")
        self.state.start_transcribing()
        self._emit("transcribing", "转写中…")
        text, error = self.decode(token, samples, sample_rate)
        if error:
            self.on_error(token, error)
            return Decision("drop", "engine-error")
        return self.on_result(token, text)
