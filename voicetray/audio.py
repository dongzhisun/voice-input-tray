"""In-memory microphone capture.

Black-box clause 9: audio never touches disk. Chunks live in an AudioBuffer
and are concatenated only when the recording is handed to the ASR engine.

Host findings that shaped this module (_spike smoke tests):
  * sounddevice.default.device == [-1, 1]  -> no usable default input.
  * WDM-KS rejects blocking InputStream with "Blocking API not supported yet".
  * Callback mode on device 16 ("麦克风阵列 2 (英特尔(R) 智音技术麦克风)")
    works with a native 16 kHz rate.
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Optional

import numpy as np

DEFAULT_RATE = 16000
DEFAULT_BLOCK = 1600  # 100 ms at 16 kHz


class AudioBuffer:
    """Thread-safe bounded float32 mono buffer."""

    def __init__(self, sample_rate: int = DEFAULT_RATE,
                 max_seconds: float = 60.0) -> None:
        self.sample_rate = int(sample_rate)
        self.max_seconds = float(max_seconds)
        self.overflow = False
        self._chunks: list[np.ndarray] = []
        self._frames = 0
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        return int(self.sample_rate * self.max_seconds)

    @property
    def duration(self) -> float:
        with self._lock:
            return self._frames / float(self.sample_rate)

    @property
    def frames(self) -> int:
        with self._lock:
            return self._frames

    def append(self, chunk: Any) -> int:
        """Append one callback block. Returns frames actually kept."""
        a = np.asarray(chunk, dtype=np.float32)
        if a.ndim == 2:
            a = a[:, 0]
        a = np.ascontiguousarray(a.reshape(-1))
        with self._lock:
            room = max(0, self.capacity - self._frames)
            if a.size > room:
                self.overflow = True
                a = a[:room]
            if a.size:
                self._chunks.append(a.copy())
                self._frames += int(a.size)
            return int(a.size)

    def samples(self) -> np.ndarray:
        with self._lock:
            if not self._chunks:
                return np.zeros(0, dtype=np.float32)
            return np.concatenate(self._chunks).astype(np.float32, copy=False)

    def clear(self) -> None:
        with self._lock:
            self._chunks.clear()
            self._frames = 0
            self.overflow = False


def list_input_devices() -> list[dict]:
    """Enumerate usable input devices for the settings window."""
    import sounddevice as sd
    out = []
    for i, d in enumerate(sd.query_devices()):
        if int(d.get("max_input_channels", 0)) > 0:
            out.append({
                "index": i,
                "name": d.get("name", ""),
                "rate": int(d.get("default_samplerate", 0) or 0),
                "channels": int(d.get("max_input_channels", 0)),
            })
    return out


def choose_device(preferred: Optional[int] = None,
                  sample_rate: int = DEFAULT_RATE) -> Optional[int]:
    """Prefer an explicitly configured device, then a native-16k input."""
    if preferred is not None:
        return int(preferred)
    devices = list_input_devices()
    native = [d for d in devices if d["rate"] == sample_rate]
    if native:
        return native[0]["index"]
    return devices[0]["index"] if devices else None


def _default_stream(**kwargs):
    import sounddevice as sd
    return sd.InputStream(**kwargs)


class Recorder:
    """Callback-driven capture. stream_factory is injectable for tests."""

    def __init__(self, device: Optional[int] = None,
                 sample_rate: int = DEFAULT_RATE,
                 blocksize: int = DEFAULT_BLOCK,
                 stream_factory: Optional[Callable] = None) -> None:
        self.device = device
        self.sample_rate = int(sample_rate)
        self.blocksize = int(blocksize)
        self._stream_factory = stream_factory or _default_stream
        self._stream = None
        self._buffer: Optional[AudioBuffer] = None
        self.statuses: list[str] = []

    @property
    def active(self) -> bool:
        return self._stream is not None

    def start(self, buffer: AudioBuffer) -> None:
        if self._stream is not None:
            raise RuntimeError("recorder already active")
        self._buffer = buffer
        self.statuses = []

        def callback(indata, frames, time_info, status):
            if status:
                self.statuses.append(str(status))
            if self._buffer is not None:
                self._buffer.append(indata)

        kwargs = dict(samplerate=self.sample_rate, channels=1,
                      dtype="float32", blocksize=self.blocksize,
                      callback=callback)
        if self.device is not None:
            kwargs["device"] = self.device
        self._stream = self._stream_factory(**kwargs)
        try:
            self._stream.start()
        except Exception:
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None
            raise

    def stop(self) -> np.ndarray:
        """Stop capture and return the in-memory float32 samples."""
        if self._stream is None:
            return np.zeros(0, dtype=np.float32)
        try:
            try:
                self._stream.stop()
            finally:
                self._stream.close()
        finally:
            self._stream = None
        return self._buffer.samples() if self._buffer is not None else \
            np.zeros(0, dtype=np.float32)

    def abort(self) -> None:
        """Discard the current recording without returning audio."""
        if self._stream is None:
            return
        try:
            try:
                self._stream.stop()
            finally:
                self._stream.close()
        finally:
            self._stream = None
            if self._buffer is not None:
                self._buffer.clear()
