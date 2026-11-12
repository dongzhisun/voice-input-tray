"""Black-box clauses 8: Esc cancel + late-result rejection."""
import pytest

from voicetray.state import Decision, Phase, Token, VoiceState


def test_begin_moves_to_recording_and_issues_unique_task_id():
    st = VoiceState()
    t1 = st.begin("fp-1", now=1.0)
    assert st.phase is Phase.RECORDING
    assert st.generation == 1
    assert st.current_task_id == t1.task_id
    assert len(t1.task_id) == 32

    st.start_transcribing()
    assert st.phase is Phase.TRANSCRIBING
    assert st.accept_result(t1, "你好").action == "insert"
    assert st.phase is Phase.INSERTING
    st.finish_insert()
    assert st.phase is Phase.IDLE

    t2 = st.begin("fp-2", now=2.0)
    assert t2.task_id != t1.task_id
    assert st.generation == 2


def test_result_cannot_be_accepted_twice():
    st = VoiceState()
    t = st.begin("fp")
    st.start_transcribing()
    assert st.accept_result(t, "一次").action == "insert"
    again = st.accept_result(t, "两次")
    assert again.action == "drop"
    assert again.reason == "not-transcribing"


def test_accept_result_before_transcribing_is_dropped():
    st = VoiceState()
    t = st.begin("fp")
    d = st.accept_result(t, "过早")
    assert d == Decision("drop", "not-transcribing")


def test_cancel_bumps_generation_and_drops_late_result():
    st = VoiceState()
    t = st.begin("fp")
    st.start_transcribing()
    late = Token(t.task_id, t.generation)
    st.cancel("user-cancel")
    assert st.generation == 2
    assert st.phase is Phase.IDLE
    d = st.accept_result(late, "迟到文本")
    assert d.action == "drop"
    assert d.reason == "not-transcribing"


def test_stale_generation_is_dropped_even_with_matching_task_id():
    """The native decode cannot be stopped; only the token gate can reject it."""
    st = VoiceState()
    t = st.begin("fp")
    st.start_transcribing()
    wrong_generation = Token(t.task_id, t.generation + 7)
    d = st.accept_result(wrong_generation, "伪造 generation")
    assert d.action == "drop"
    assert d.reason == "stale-generation"


def test_stale_task_id_is_dropped_after_a_new_session_starts():
    st = VoiceState()
    t1 = st.begin("fp-1")
    st.start_transcribing()
    st.cancel("esc")
    t2 = st.begin("fp-2")
    st.start_transcribing()
    d = st.accept_result(t1, "上一个任务的文本")
    assert d.reason == "stale-task-id"
    assert st.accept_result(t2, "当前任务").action == "insert"


def test_target_fingerprint_mismatch_drops_and_cancels():
    st = VoiceState()
    t = st.begin("before-fp")
    st.start_transcribing()
    d = st.accept_result(t, "不该插入", "after-fp")
    assert d.action == "drop"
    assert d.reason == "target-mismatch"
    assert st.phase is Phase.IDLE
    assert st.generation == 2
    assert "target-mismatch" in st.events


@pytest.mark.parametrize("text", ["", "   ", "\n\t"])
def test_empty_result_drops_without_inserting(text):
    st = VoiceState()
    t = st.begin("fp")
    st.start_transcribing()
    d = st.accept_result(t, text)
    assert d.action == "drop"
    assert d.reason == "empty-result"
    assert st.phase is Phase.IDLE


def test_fail_sets_error_state_and_clear_error_recovers():
    st = VoiceState()
    st.fail("模型校验失败")
    assert st.phase is Phase.ERROR
    assert st.last_error == "模型校验失败"
    assert "error" in st.events
    st.clear_error()
    assert st.phase is Phase.IDLE
    assert st.last_error is None


def test_cancel_reason_is_recorded_for_diagnostics():
    st = VoiceState()
    st.begin("fp")
    st.cancel("device-lost")
    assert st.events[-1] == "device-lost"
