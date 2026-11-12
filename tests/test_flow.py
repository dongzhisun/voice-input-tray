"""App-flow tests: press -> decode -> target confirm -> insert, plus every
drop path (clauses 6, 8, 11)."""
import numpy as np
import pytest

from voicetray import target as t
from voicetray.flow import VoiceFlow
from voicetray.state import Phase, VoiceState


class FakeEngine:
    def __init__(self, text="你好，世界", error=None):
        self.text = text
        self.error = error
        self.calls = []

    def transcribe(self, samples, sample_rate=16000):
        self.calls.append((len(samples), sample_rate))
        if self.error:
            raise RuntimeError(self.error)
        return self.text


def snap(**kw):
    base = dict(hwnd=100, pid=200, tid=300, title="编辑器",
                class_name="Chrome_WidgetWin_1", focus_hwnd=100,
                uia_runtime_id="42,100,7")
    base.update(kw)
    return t.TargetSnapshot(**base)


def make_flow(engine=None, first=None, second=None, insert=None,
              notices=None, use_uia=True):
    engine = engine or FakeEngine()
    captures = [first or snap(), second or snap()]
    inserted = []
    notices = notices if notices is not None else []

    def capture(use_uia=True):
        assert len(captures) > 0, "unexpected extra capture"
        return captures.pop(0)

    def do_insert(text):
        inserted.append(text)
        return True if insert is None else insert(text)

    flow = VoiceFlow(state=VoiceState(), engine=engine, capture=capture,
                     insert=do_insert,
                     notify=lambda level, msg: notices.append((level, msg)),
                     use_uia=use_uia)
    return flow, inserted, notices


AUDIO = np.zeros(1600, dtype=np.float32)


def test_full_path_inserts_once_and_returns_to_idle():
    flow, inserted, notices = make_flow()
    token = flow.on_press()
    assert token is not None
    assert flow.state.phase is Phase.RECORDING
    d = flow.on_release_sync(token, AUDIO)
    assert d.action == "insert"
    assert inserted == ["你好，世界"]
    assert flow.state.phase is Phase.IDLE
    levels = [n[0] for n in notices]
    assert "recording" in levels and "transcribing" in levels
    assert "inserted" in levels


def test_target_change_blocks_insertion_and_warns():
    flow, inserted, notices = make_flow(second=snap(pid=999))
    token = flow.on_press()
    d = flow.on_release_sync(token, AUDIO)
    assert d.action == "drop"
    assert d.reason == "target-mismatch"
    assert inserted == []
    assert any("目标已变化" in msg for _, msg in notices)


def test_uia_dropout_on_one_sample_does_not_block_insertion():
    """compare() only checks RuntimeId when both samples have one."""
    flow, inserted, _ = make_flow(second=snap(uia_runtime_id=""))
    token = flow.on_press()
    assert flow.on_release_sync(token, AUDIO).action == "insert"
    assert inserted == ["你好，世界"]


def test_uia_element_change_inside_same_window_blocks_insertion():
    flow, inserted, _ = make_flow(second=snap(uia_runtime_id="42,100,9"))
    token = flow.on_press()
    assert flow.on_release_sync(token, AUDIO).reason == "target-mismatch"
    assert inserted == []


def test_escape_cancel_drops_a_late_decode_result():
    flow, inserted, notices = make_flow()
    token = flow.on_press()
    assert flow.on_cancel() is True
    assert flow.state.generation == 2
    d = flow.on_result(token, "迟到的文本")
    assert d.action == "drop"
    assert d.reason == "missing-snapshot"
    assert inserted == []
    assert any("Esc" in msg for _, msg in notices)


def test_cancel_after_a_new_task_also_rejects_the_old_token():
    flow, inserted, _ = make_flow()
    t1 = flow.on_press()
    flow.on_cancel()
    flow.on_press()  # a second recording begins
    d = flow.on_result(t1, "上一个任务")
    assert d.action == "drop"
    assert inserted == []


def test_engine_failure_sets_error_state_and_never_inserts():
    flow, inserted, notices = make_flow(engine=FakeEngine(error="decode crashed"))
    token = flow.on_press()
    d = flow.on_release_sync(token, AUDIO)
    assert d.action == "drop"
    assert flow.state.phase is Phase.ERROR
    assert "decode crashed" in flow.state.last_error
    assert inserted == []
    assert any(level == "error" for level, _ in notices)


def test_insert_rejection_is_reported_and_does_not_leave_inserting_phase():
    flow, inserted, notices = make_flow(insert=lambda text: False)
    token = flow.on_press()
    d = flow.on_release_sync(token, AUDIO)
    assert d.action == "drop"
    assert flow.state.phase is Phase.ERROR
    assert inserted == ["你好，世界"]
    assert any("插入被拒绝" in msg for _, msg in notices)


def test_insert_exception_is_contained():
    def explode(text):
        raise OSError("clipboard locked")

    flow, _, notices = make_flow(insert=explode)
    token = flow.on_press()
    d = flow.on_release_sync(token, AUDIO)
    assert d.reason == "insert-failed"
    assert flow.state.phase is Phase.ERROR
    assert any("clipboard locked" in msg for _, msg in notices)


def test_press_while_busy_is_ignored():
    flow, _, notices = make_flow()
    first = flow.on_press()
    assert flow.on_press() is None
    assert flow.state.current_task_id == first.task_id
    assert any("已有任务" in msg for _, msg in notices)


def test_press_is_ignored_until_error_is_cleared():
    flow, _, _ = make_flow(engine=FakeEngine(error="x"))
    token = flow.on_press()
    flow.on_release_sync(token, AUDIO)
    assert flow.on_press() is None
    flow.state.clear_error()
    assert flow.on_press() is not None


def test_capture_failure_fails_loudly():
    def bad_capture(use_uia=True):
        raise OSError("UIA unavailable")

    flow = VoiceFlow(state=VoiceState(), engine=FakeEngine(),
                     capture=bad_capture)
    assert flow.on_press() is None
    assert flow.state.phase is Phase.ERROR


def test_target_confirm_failure_cancels_insertion():
    calls = {"n": 0}

    def capture(use_uia=True):
        calls["n"] += 1
        if calls["n"] > 1:
            raise OSError("focus lost")
        return snap()

    inserted = []
    flow = VoiceFlow(state=VoiceState(), engine=FakeEngine(),
                     capture=capture, insert=lambda text: inserted.append(text))
    token = flow.on_press()
    d = flow.on_result(token, "不该插入")
    assert d.reason == "target-confirm-failed"
    assert inserted == []


def test_decode_does_not_mutate_state():
    flow, _, _ = make_flow()
    token = flow.on_press()
    before = (flow.state.phase, flow.state.generation)
    text, error = flow.decode(token, AUDIO)
    assert text == "你好，世界" and error == ""
    assert (flow.state.phase, flow.state.generation) == before


def test_empty_transcript_is_dropped():
    flow, inserted, _ = make_flow(engine=FakeEngine(text="   "))
    token = flow.on_press()
    d = flow.on_release_sync(token, AUDIO)
    assert d.action == "drop"
    assert inserted == []
