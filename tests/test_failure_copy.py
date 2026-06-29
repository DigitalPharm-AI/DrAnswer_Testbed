from __future__ import annotations

from system_app.services.failure_copy import copy_for_agent_error, copy_for_async_task, copy_for_phr_error


def test_async_failure_copy_preserves_user_safe_retry_context():
    copy = copy_for_async_task("missed_dose")

    assert copy.retryable is True
    assert "AI가 미복용 상황을 처리하지 못했습니다" in copy.body
    assert "다시 시도" in copy.body
    assert "처방" not in copy.body


def test_agent_network_failure_copy_keeps_saved_records_context():
    copy = copy_for_agent_error("agent_network_error", source_event_type="multiturn_chat")

    assert copy.title == "AI 서버와 연결하지 못했습니다"
    assert "복약과 식사 기록은 저장" in copy.body
    assert copy.action_label == "다시 시도"


def test_phr_read_only_copy_explains_containment_without_losing_inputs():
    copy = copy_for_phr_error("phr_read_only_mode", status_code=503)

    assert copy.title == "PHR 등록이 잠시 중지되었습니다"
    assert "복약 정보는 화면에 남아" in copy.body
    assert copy.retryable is True
