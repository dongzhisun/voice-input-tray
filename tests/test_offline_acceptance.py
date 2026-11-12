"""Real-model black-box evidence for clauses 1, 3, 5, 6, 7, 8, 9.

These tests load the actual 239 MB int8 weights. They are intentionally in
a separate module so the pure-logic suite stays fast.
"""
import socket
from pathlib import Path

import numpy as np
import pytest

from voicetray import clipboard as cb
from voicetray import target as t
from voicetray.engine import SenseVoiceEngine, read_wav
from voicetray.flow import VoiceFlow
from voicetray.state import VoiceState
from conftest import MODEL_DIR, WAV_DIR, ROOT


@pytest.fixture(scope="module")
def engine():
    return SenseVoiceEngine(MODEL_DIR, verify=True, num_threads=2)


def test_bundled_model_is_verified_before_use(engine):
    assert engine.verified is True
    assert engine.describe()["streaming"] is False
    assert engine.describe()["cancel_api"] is False


@pytest.mark.parametrize("name,expected", [
    ("zh.wav", "开放时间"),
    ("en.wav", "tribal chieftain"),
])
def test_real_offline_recognition_zh_and_en(engine, name, expected):
    rate, samples = read_wav(WAV_DIR / name)
    text = engine.transcribe(samples, rate)
    assert expected in text, f"{name} -> {text!r}"


def test_recognition_still_works_with_network_disabled(engine, monkeypatch):
    """Clause 3: offline. We block socket creation rather than unplugging."""
    def blocked(*args, **kwargs):
        raise OSError("network disabled by test")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    rate, samples = read_wav(WAV_DIR / "zh.wav")
    assert "开放时间" in engine.transcribe(samples, rate)


@pytest.mark.parametrize("name", ["zh.wav", "en.wav"])
def test_full_flow_with_real_asr_and_clipboard_protection(engine, name):
    """Clause 6+7 end to end: verify target, paste, restore clipboard."""
    backend = cb.MemoryClipboard("用户原有剪贴板内容")
    original = cb.snapshot(backend).formats
    inserts = []
    snapshots = [t.TargetSnapshot(hwnd=1, pid=2, tid=3, title="目标窗口",
                                  class_name="Edit", focus_hwnd=1,
                                  uia_runtime_id="7,1,2"),
                 t.TargetSnapshot(hwnd=1, pid=2, tid=3, title="目标窗口",
                                  class_name="Edit", focus_hwnd=1,
                                  uia_runtime_id="7,1,2")]

    def capture(use_uia=True):
        return snapshots.pop(0)

    def insert(text):
        r = cb.insert_text(text, backend=backend, paste=lambda: None, settle=0)
        inserts.append((text, r))
        return r.ok and r.restored

    notices = []
    flow = VoiceFlow(state=VoiceState(), engine=engine, capture=capture,
                     insert=insert,
                     notify=lambda level, msg: notices.append((level, msg)))
    token = flow.on_press()
    rate, samples = read_wav(WAV_DIR / name)
    decision = flow.on_release_sync(token, samples, rate)

    assert decision.action == "insert", (name, decision)
    assert inserts and inserts[-1][0].strip()
    assert inserts[-1][1].restored is True
    assert cb.snapshot(backend).formats == original
    assert flow.state.phase.value == "idle"


def test_target_change_with_real_asr_drops_transcript(engine):
    snapshots = [t.TargetSnapshot(hwnd=1, pid=2, tid=3, title="A"),
                 t.TargetSnapshot(hwnd=9, pid=2, tid=3, title="B")]
    inserts = []
    flow = VoiceFlow(state=VoiceState(), engine=engine,
                     capture=lambda use_uia=True: snapshots.pop(0),
                     insert=lambda text: inserts.append(text) or True)
    token = flow.on_press()
    rate, samples = read_wav(WAV_DIR / "zh.wav")
    d = flow.on_release_sync(token, samples, rate)
    assert d.reason == "target-mismatch"
    assert inserts == []


def test_escape_cancel_drops_real_late_result(engine):
    """Clause 8: native decode runs to completion but the token gate drops it."""
    snapshots = [t.TargetSnapshot(hwnd=1, pid=2, tid=3, title="A")]
    inserts = []
    flow = VoiceFlow(state=VoiceState(), engine=engine,
                     capture=lambda use_uia=True: snapshots.pop(0),
                     insert=lambda text: inserts.append(text) or True)
    token = flow.on_press()
    flow.on_cancel()
    rate, samples = read_wav(WAV_DIR / "zh.wav")
    text, error = flow.decode(token, samples, rate)  # native decode finishes
    assert text and not error
    d = flow.on_result(token, text)                   # gate rejects it
    assert d.action == "drop"
    assert inserts == []


def test_no_recording_files_written_anywhere_in_project():
    """Clause 9: audio lives only in memory."""
    found = []
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(ROOT)
        if rel.parts and rel.parts[0] in (
                ".venv", ".git", "_spike", "dist", "build", "__pycache__"):
            continue
        if p.suffix.lower() in (".wav", ".pcm", ".raw", ".mp3", ".flac"):
            found.append(rel.as_posix())
    assert sorted(found) == ["assets/test_wavs/en.wav",
                             "assets/test_wavs/zh.wav"], found


def test_source_contains_no_disk_recording_writes():
    """Static guard: the app never opens an audio file for writing."""
    readers = []
    for p in (ROOT / "voicetray").glob("*.py"):
        src = p.read_text(encoding="utf-8")
        assert "sf.write(" not in src, p.name
        assert "writeframes(" not in src, p.name
        if "wave.open(" in src:
            assert 'wave.open(str(path), "rb")' in src, p.name
            assert '"wb"' not in src, p.name
            readers.append(p.name)
    assert readers == ["engine.py"], readers
