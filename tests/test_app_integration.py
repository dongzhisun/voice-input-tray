"""Real Tk application object with faked audio/clipboard/paste.

Exercises the actual app wiring (hook -> state -> ASR thread -> Tk after ->
target confirm -> insert), not just the pure flow logic.
"""
import threading

import numpy as np
import pytest

from voicetray import audio, clipboard as cb, config, target as t
from voicetray.app import VoiceTrayApp
from voicetray.config import Config
from voicetray.flow import VoiceFlow
from voicetray.state import Phase, Token
from conftest import MODEL_DIR


class FakeEngine:
    def __init__(self, text="集成测试文本"):
        self.text = text

    def transcribe(self, samples, sample_rate=16000):
        return self.text


class FakeRecorder:
    def __init__(self, device=None, sample_rate=16000):
        self.active = False
        self._buf = None

    def start(self, buffer):
        self.active = True
        self._buf = buffer
        buffer.append(np.full(1600, 0.1, dtype=np.float32))

    def stop(self):
        self.active = False
        return self._buf.samples()

    def abort(self):
        self.active = False
        if self._buf is not None:
            self._buf.clear()


def snap(**kw):
    base = dict(hwnd=1, pid=2, tid=3, title="目标窗口", class_name="Edit",
                focus_hwnd=1, uia_runtime_id="7,1,2")
    base.update(kw)
    return t.TargetSnapshot(**base)


@pytest.fixture(scope="module")
def app():
    """One Tk root for the whole module.

    Creating and destroying tk.Tk() repeatedly in a single process leaves
    Tcl's global state dirty ("invalid command name tcl_findLibrary"), so
    we build the app once and reset its state between tests instead.
    """
    orig_recorder = audio.Recorder
    orig_choose = audio.choose_device
    audio.Recorder = FakeRecorder
    audio.choose_device = lambda preferred=None, sample_rate=16000: 16
    cfg = Config(model_dir=str(MODEL_DIR), show_status_window=False)
    a = VoiceTrayApp(cfg, clipboard_backend=cb.MemoryClipboard("用户原有剪贴板"),
                     restore_delay_ms=0)
    a.pasted = []
    a._paste = lambda: a.pasted.append(cb.snapshot(a.backend).text)
    a.engine = FakeEngine()
    yield a
    a.root.destroy()
    audio.Recorder = orig_recorder
    audio.choose_device = orig_choose


@pytest.fixture(autouse=True)
def _reset(app):
    from pathlib import Path
    from voicetray.state import VoiceState
    app.state = VoiceState()
    app.token = None
    app.recorder = None
    app.buffer = None
    app.flow = None
    app.engine = FakeEngine()
    app.engine_error = ""
    app.cfg = Config(model_dir=str(MODEL_DIR), show_status_window=False)
    app.model_dir = Path(MODEL_DIR)
    app.decoder.allow_injected = True
    app.status.set_visible(False)
    app.backend = cb.MemoryClipboard("用户原有剪贴板")
    app._clipboard_backend = app.backend
    app.pasted.clear()
    while not app._calls.empty():
        app._calls.get_nowait()
    yield


def install_flow(a, snapshots, engine=None):
    a.flow = VoiceFlow(
        state=a.state, engine=engine or a.engine,
        capture=lambda use_uia=True: snapshots.pop(0),
        insert=a._do_insert, notify=a._notify, use_uia=False)


def drain(a):
    for th in list(threading.enumerate()):
        if th.name == "voicetray-asr":
            th.join(timeout=5)
    a._process_posted()   # what the Tk after pump does on the main thread
    a.root.update()


def test_full_app_path_inserts_and_restores_clipboard(app):
    original = cb.snapshot(app.backend).formats
    install_flow(app, [snap(), snap()])
    app._on_press()
    assert app.state.phase is Phase.RECORDING
    assert app.recorder.active is True
    app._on_release()
    drain(app)
    assert app.state.phase is Phase.IDLE
    assert app.pasted == ["集成测试文本"]
    assert cb.snapshot(app.backend).formats == original


def test_status_overlay_is_marked_noactivate(app):
    """Otherwise our own overlay would steal focus and invalidate the target."""
    assert app.status.noactivate_ok is True


def test_escape_discards_recording_and_late_result(app):
    install_flow(app, [snap()])
    app._on_press()
    app._on_esc()
    assert app.state.phase is Phase.IDLE
    assert app.recorder.active is False
    app._on_decoded(Token("late-task", app.state.generation), "迟到文本", "")
    assert app.pasted == []


def test_target_change_blocks_insertion_in_real_app(app):
    install_flow(app, [snap(pid=1), snap(pid=2)])
    app._on_press()
    app._on_release()
    drain(app)
    assert app.state.phase is Phase.IDLE
    assert app.pasted == []


def test_press_without_engine_is_refused_without_state_change(app):
    app.engine = None
    app.flow = None
    app._on_press()
    assert app.state.phase is Phase.IDLE
    assert app.token is None


def test_device_failure_sets_error_state(app, monkeypatch):
    class BadRecorder(FakeRecorder):
        def start(self, buffer):
            raise RuntimeError("device unavailable")

    monkeypatch.setattr(audio, "Recorder", BadRecorder)
    install_flow(app, [snap()])
    app._on_press()
    assert app.state.phase is Phase.ERROR
    assert "麦克风不可用" in app.state.last_error
    assert app.pasted == []


def test_engine_error_is_reported_without_crashing(app):
    class BrokenEngine:
        def transcribe(self, samples, sample_rate=16000):
            raise RuntimeError("decode crashed")

    install_flow(app, [snap(), snap()], engine=BrokenEngine())
    app._on_press()
    app._on_release()
    drain(app)
    assert app.state.phase is Phase.ERROR
    assert "decode crashed" in app.state.last_error
    assert app.pasted == []


def test_settings_save_updates_runtime_flags(app, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "config_path", lambda: tmp_path / "config.json")
    install_flow(app, [snap(), snap()])
    new = Config(allow_injected=False, use_uia=False,
                 show_status_window=False, model_dir=str(MODEL_DIR))
    app._on_settings_saved(new)
    assert app.decoder.allow_injected is False
    assert app.flow.use_uia is False
    assert (tmp_path / "config.json").is_file()
    assert app.cfg.allow_injected is False
