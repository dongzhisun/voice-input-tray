"""Black-box clauses 2 / 5 / 11: engine wrapper, wav input, corrupt refusal."""
import numpy as np
import pytest
from types import SimpleNamespace

from voicetray import manifest
from voicetray.engine import EngineError, SenseVoiceEngine, read_wav
from conftest import MODEL_DIR, WAV_DIR


class FakeStream:
    def __init__(self, text):
        self.waveforms = []
        self.decoded = False
        self.result = SimpleNamespace(text=text)

    def accept_waveform(self, sample_rate, samples):
        self.waveforms.append((sample_rate, samples.copy()))


class FakeRecognizer:
    def __init__(self, text="离线识别结果"):
        self.text = text
        self.streams = []

    def create_stream(self):
        s = FakeStream(self.text)
        self.streams.append(s)
        return s

    def decode_stream(self, stream):
        stream.decoded = True


def test_transcribe_calls_accept_then_decode_and_returns_trimmed_text():
    rec = FakeRecognizer("  你好，世界  ")
    eng = SenseVoiceEngine(MODEL_DIR, recognizer=rec, verify=False)
    out = eng.transcribe(np.array([0.1, 0.2], dtype=np.float32), 16000)
    assert out == "你好，世界"
    s = rec.streams[0]
    assert s.decoded is True
    assert len(s.waveforms) == 1
    assert s.waveforms[0][0] == 16000
    np.testing.assert_allclose(s.waveforms[0][1], [0.1, 0.2], rtol=1e-6)


def test_empty_audio_short_circuits_without_creating_a_stream():
    rec = FakeRecognizer()
    eng = SenseVoiceEngine(MODEL_DIR, recognizer=rec, verify=False)
    assert eng.transcribe(np.zeros(0, dtype=np.float32)) == ""
    assert rec.streams == []


def test_corrupt_model_dir_is_refused_before_any_native_load(tmp_path):
    """Clause 2/11: a broken weights file must fail loudly, not crash later."""
    with pytest.raises(manifest.ModelError, match="model not ready"):
        SenseVoiceEngine(tmp_path, verify=True)


def test_verify_can_be_skipped_only_by_explicit_test_seam():
    eng = SenseVoiceEngine(MODEL_DIR, recognizer=FakeRecognizer(), verify=False)
    assert eng.verified is False


def test_describe_reports_non_streaming_and_no_cancel_api():
    eng = SenseVoiceEngine(MODEL_DIR, recognizer=FakeRecognizer(), verify=False)
    d = eng.describe()
    assert d["model_id"] == manifest.MODEL_ID
    assert d["streaming"] is False
    assert d["cancel_api"] is False


@pytest.mark.parametrize("name,min_seconds", [("zh.wav", 5.0), ("en.wav", 7.0)])
def test_read_wav_returns_16k_mono_float32(name, min_seconds):
    rate, samples = read_wav(WAV_DIR / name)
    assert rate == 16000
    assert samples.dtype == np.float32
    assert samples.ndim == 1
    assert samples.size / rate >= min_seconds
    assert np.max(np.abs(samples)) <= 1.0


def test_read_wav_rejects_non_pcm16(tmp_path):
    import wave
    p = tmp_path / "bad.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(1)
        w.setframerate(16000)
        w.writeframes(b"\x00" * 100)
    with pytest.raises(EngineError, match="PCM16"):
        read_wav(p)
