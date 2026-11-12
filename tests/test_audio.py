"""Black-box clause 9: in-memory capture, no recording ever hits disk."""
import numpy as np
import pytest

from voicetray import audio


def test_buffer_concatenates_mono_chunks_in_order():
    b = audio.AudioBuffer(sample_rate=16000)
    b.append(np.array([0.1, 0.2], dtype=np.float32))
    b.append(np.array([0.3], dtype=np.float32))
    np.testing.assert_allclose(b.samples(), [0.1, 0.2, 0.3], rtol=1e-6)
    assert b.frames == 3
    assert b.duration == pytest.approx(3 / 16000)


def test_buffer_takes_first_channel_only():
    b = audio.AudioBuffer()
    b.append(np.array([[0.5, 9.0], [0.25, 9.0]], dtype=np.float32))
    np.testing.assert_allclose(b.samples(), [0.5, 0.25], rtol=1e-6)


def test_buffer_is_bounded_and_flags_overflow():
    b = audio.AudioBuffer(sample_rate=10, max_seconds=0.5)  # capacity 5
    kept = b.append(np.ones(8, dtype=np.float32))
    assert kept == 5
    assert b.overflow is True
    assert b.frames == 5
    assert b.append(np.ones(2, dtype=np.float32)) == 0
    assert b.samples().shape == (5,)


def test_buffer_clear_resets_everything():
    b = audio.AudioBuffer(sample_rate=10, max_seconds=0.5)
    b.append(np.ones(8, dtype=np.float32))
    b.clear()
    assert b.frames == 0 and b.overflow is False
    assert b.samples().shape == (0,)
    assert b.samples().dtype == np.float32


def test_buffer_is_thread_safe_under_concurrent_appends():
    import threading
    b = audio.AudioBuffer(sample_rate=16000, max_seconds=10)

    def worker():
        for _ in range(50):
            b.append(np.ones(100, dtype=np.float32))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert b.frames == 4 * 50 * 100
    assert b.samples().shape == (20000,)


class FakeStream:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.started = False
        self.stopped = False
        self.closed = False
        self.callback = kwargs["callback"]

    def start(self):
        self.started = True
        frame = np.full((self.kwargs["blocksize"], 1), 0.5, dtype=np.float32)
        self.callback(frame, self.kwargs["blocksize"], None, None)
        self.callback(frame, self.kwargs["blocksize"], None, None)

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


def test_recorder_uses_callback_mode_and_returns_in_memory_audio():
    made = []

    def factory(**kwargs):
        s = FakeStream(**kwargs)
        made.append(s)
        return s

    rec = audio.Recorder(device=16, sample_rate=16000, blocksize=100,
                         stream_factory=factory)
    buf = audio.AudioBuffer(sample_rate=16000, max_seconds=10)
    rec.start(buf)
    assert rec.active
    assert made[0].kwargs["device"] == 16
    assert made[0].kwargs["channels"] == 1
    assert made[0].kwargs["dtype"] == "float32"
    samples = rec.stop()
    assert not rec.active
    assert made[0].stopped and made[0].closed
    assert samples.shape == (200,)
    assert np.all(samples == 0.5)


def test_recorder_abort_discards_audio():
    rec = audio.Recorder(stream_factory=FakeStream)
    buf = audio.AudioBuffer()
    rec.start(buf)
    rec.abort()
    assert buf.frames == 0
    assert rec.stop().shape == (0,)


def test_recorder_cannot_start_twice():
    rec = audio.Recorder(stream_factory=FakeStream)
    rec.start(audio.AudioBuffer())
    with pytest.raises(RuntimeError, match="already active"):
        rec.start(audio.AudioBuffer())
    rec.stop()


def test_recorder_cleans_up_when_stream_start_fails():
    class BadStream(FakeStream):
        def start(self):
            raise RuntimeError("device unavailable")

    rec = audio.Recorder(stream_factory=BadStream)
    with pytest.raises(RuntimeError, match="device unavailable"):
        rec.start(audio.AudioBuffer())
    assert not rec.active


def test_choose_device_prefers_native_16k(monkeypatch):
    devices = [
        {"index": 3, "name": "48k mic", "rate": 48000, "channels": 2},
        {"index": 16, "name": "16k mic array", "rate": 16000, "channels": 1},
    ]
    monkeypatch.setattr(audio, "list_input_devices", lambda: devices)
    assert audio.choose_device() == 16
    assert audio.choose_device(preferred=3) == 3
