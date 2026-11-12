"""Black-box state machine with task id + generation token.

The ASR native decode cannot be interrupted (verified: sherpa_onnx
OfflineStream exposes no cancel/reset). Therefore cancellation is
logical: bump the generation, and reject any result whose token is stale.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Phase(str, Enum):
    IDLE = "idle"
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"
    INSERTING = "inserting"
    ERROR = "error"


@dataclass(frozen=True)
class Token:
    task_id: str
    generation: int


@dataclass
class Session:
    token: Token
    target_fingerprint: str = ""
    started_at: float = 0.0


@dataclass
class Decision:
    action: str  # "insert" | "drop"
    reason: str
    text: str = ""


class VoiceState:
    """Single-process state machine. Not thread-safe by itself; the app
    serialises all transitions through one Tk `after` event pump."""

    def __init__(self) -> None:
        self.phase: Phase = Phase.IDLE
        self._generation = 0
        self._session: Optional[Session] = None
        self.last_error: Optional[str] = None
        self.events: list[str] = []

    # -- transitions -----------------------------------------------------
    def begin(self, fingerprint: str = "", now: float = 0.0) -> Token:
        self._generation += 1
        token = Token(task_id=uuid.uuid4().hex, generation=self._generation)
        self._session = Session(token=token, target_fingerprint=fingerprint,
                                started_at=now)
        self.phase = Phase.RECORDING
        self.last_error = None
        self.events.append("begin")
        return token

    def start_transcribing(self) -> None:
        if self.phase == Phase.RECORDING:
            self.phase = Phase.TRANSCRIBING
            self.events.append("transcribing")

    def cancel(self, reason: str = "cancel") -> None:
        """Esc, window switch, or any failure. Bumps generation so that
        even an already-running native decode is rejected on completion."""
        self._generation += 1
        self._session = None
        self.phase = Phase.IDLE
        self.events.append(reason)

    def fail(self, message: str) -> None:
        self.cancel("error")
        self.phase = Phase.ERROR
        self.last_error = message

    def clear_error(self) -> None:
        if self.phase == Phase.ERROR:
            self.phase = Phase.IDLE
            self.last_error = None

    # -- result gate -----------------------------------------------------
    def accept_result(self, token: Token, text: str,
                      current_fingerprint: str = "") -> Decision:
        """Called when native decode finishes. Rejects stale results even
        though the decode itself could not be stopped."""
        s = self._session
        if self.phase != Phase.TRANSCRIBING or s is None:
            return Decision("drop", "not-transcribing")
        if token.task_id != s.token.task_id:
            return Decision("drop", "stale-task-id")
        if token.generation != self._generation:
            return Decision("drop", "stale-generation")
        if not text or not text.strip():
            self.cancel("empty-result")
            return Decision("drop", "empty-result")
        if s.target_fingerprint and current_fingerprint and \
                current_fingerprint != s.target_fingerprint:
            self.cancel("target-mismatch")
            return Decision("drop", "target-mismatch")
        self.phase = Phase.INSERTING
        self.events.append("accept")
        return Decision("insert", "ok", text=text)

    def finish_insert(self) -> None:
        self._session = None
        self.phase = Phase.IDLE
        self.events.append("inserted")

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def current_task_id(self) -> Optional[str]:
        return self._session.token.task_id if self._session else None
