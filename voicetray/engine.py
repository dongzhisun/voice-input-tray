"""Offline SenseVoice transcription via sherpa-onnx.

Measured (_spike/asr_smoke.py, sherpa-onnx 1.12.40, Python 3.14.3):
    zh.wav 5.59s -> "开放时间早上9点至下午5点。"          decode 610ms
    en.wav 7.15s -> "The tribal chieftain called for ..."  decode 849ms

OfflineStream exposes only accept_waveform / result / (get|set|has)_option:
there is NO cancel or reset. A running native decode cannot be interrupted,
which is why cancellation must be logical (state.py generation token).
"""
from __future__ import annotations

import wave
from pathlib import Path
from typing import Optional

import numpy as np

from . import manifest

class EngineError(RuntimeError):
    pass


def read_wav(path) -> tuple[int, np.ndarray]:
    """Read a PCM16 wav into mono float32 [-1, 1]. Used by tests and smoke."""
    with wave.open(str(path), "rb") as w:
        rate = int(w.getframerate())
        channels = int(w.getnchannels())
        width = int(w.getsampwidth())
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise EngineError(f"unsupported sample width {width}; need PCM16")
    a = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        a = a.reshape(-1, channels)[:, 0].copy()
    return rate, a


class SenseVoiceEngine:
    """Thin, injectable wrapper. `recognizer` is a test seam: any object with
    create_stream()/decode_stream() can be supplied, so unit tests need no
    239 MB model load.
    """

    def __init__(self, model_dir, *, recognizer=None, verify: bool = True,
                 num_threads: int = 2, language: str = "auto",
                 use_itn: bool = True) -> None:
        self.model_dir = Path(model_dir)
        self.num_threads = int(num_threads)
        self.language = language
        self.use_itn = bool(use_itn)
        self._verified = False
        if recognizer is None:
            if verify:
                if not manifest.is_ready(self.model_dir):
                    bad = [name for name, st
                           in manifest.verify_dir(self.model_dir).items()
                           if not st.valid]
                    raise manifest.ModelError(
                        "model not ready, missing/corrupt: " + ", ".join(bad))
                self._verified = True
            model, tokens = manifest.required_paths(self.model_dir)
            import sherpa_onnx
            recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=model, tokens=tokens, use_itn=self.use_itn,
                language=self.language, num_threads=self.num_threads,
                debug=False)
        self._rec = recognizer

    @property
    def verified(self) -> bool:
        return self._verified

    def transcribe(self, samples, sample_rate: int = 16000) -> str:
        a = np.ascontiguousarray(
            np.asarray(samples, dtype=np.float32).reshape(-1))
        if a.size == 0:
            return ""
        stream = self._rec.create_stream()
        stream.accept_waveform(int(sample_rate), a)
        self._rec.decode_stream(stream)
        return (stream.result.text or "").strip()

    def describe(self) -> dict:
        return {
            "model_dir": str(self.model_dir),
            "model_id": manifest.MODEL_ID,
            "verified": self._verified,
            "language": self.language,
            "use_itn": self.use_itn,
            "num_threads": self.num_threads,
            "streaming": False,
            "cancel_api": False,
        }
