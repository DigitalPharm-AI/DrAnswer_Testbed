from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FailureCopy:
    title: str
    body: str
    action_label: str = "다시 시도"
    retryable: bool = True
    severity: str = "warning"


DEFAULT_AGENT_FAILURE = FailureCopy(
    title="AI가 대화를 처리하지 못했습니다",
    body="AI가 대화를 처리하지 못했습니다. 복약과 식사 기록은 저장되어 있어요. 잠시 후 다시 시도해주세요.",
)

ASYNC_TASK_FAILURES = {
    "missed_dose": FailureCopy(
        title="미복용 AI 안내를 완료하지 못했습니다",
        body="백그라운드 AI 작업을 처리하지 못했습니다. AI가 미복용 상황을 처리하지 못했습니다. 현재 복약 기록은 유지되어 있어요. AI 에이전트 오류 알림에서 다시 시도할 수 있습니다.",
    ),
    "daily_pattern": FailureCopy(
        title="일일 패턴 분석을 완료하지 못했습니다",
        body="AI 일일 패턴 분석 결과를 적용하지 못했습니다. 복약 일정은 변경하지 않았고, 오류 알림에서 다시 시도할 수 있습니다.",
    ),
    "chat_continuation": FailureCopy(
        title="AI 답변을 완료하지 못했습니다",
        body="AI가 대화를 처리하지 못했습니다. 복약과 식사 기록은 저장되어 있어요. 잠시 후 다시 시도해주세요.",
    ),
    "push_message": FailureCopy(
        title="AI 알림 생성을 완료하지 못했습니다",
        body="백그라운드 AI 작업을 처리하지 못했습니다. 알림은 새로 보내지 않았고, 잠시 후 다시 시도할 수 있습니다.",
    ),
    "clinician_alert": FailureCopy(
        title="의료진 확인 알림 기록을 완료하지 못했습니다",
        body="백그라운드 AI 작업을 처리하지 못했습니다. 필요한 경우 증상이 심하거나 갑자기 악화되면 의료진에게 직접 연락해주세요.",
        action_label="확인",
        retryable=False,
        severity="critical",
    ),
}

PHR_FAILURES = {
    "phr_read_only_mode": FailureCopy(
        title="PHR 등록이 잠시 중지되었습니다",
        body="PHR 등록이 잠시 중지되어 있습니다. 복약 정보는 화면에 남아 있어요. 잠시 후 다시 등록해주세요.",
    ),
    "phr_patient_not_found": FailureCopy(
        title="PHR 환자 정보를 찾지 못했습니다",
        body="PHR 환자 정보를 찾지 못했습니다. 복약 정보를 다시 등록한 뒤 시뮬레이션을 진행해주세요.",
    ),
    "phr_network_error": FailureCopy(
        title="PHR 서버와 연결하지 못했습니다",
        body="PHR 서버와 연결하지 못했습니다. 복약 정보는 화면에 남아 있어요. 잠시 후 다시 등록해주세요.",
    ),
}


def copy_for_async_task(task_type: str, *, error_type: str = "") -> FailureCopy:
    if error_type in {"agent_network_error", "provider_request_failed", "agent_response_invalid"} and task_type == "chat_continuation":
        return DEFAULT_AGENT_FAILURE
    return ASYNC_TASK_FAILURES.get(task_type, FailureCopy(title="백그라운드 AI 작업 실패", body="백그라운드 AI 작업을 처리하지 못했습니다. 잠시 후 다시 시도해주세요."))


def copy_for_agent_error(error_type: str, *, source_event_type: str = "") -> FailureCopy:
    if error_type == "agent_network_error":
        return FailureCopy(
            title="AI 서버와 연결하지 못했습니다",
            body="AI가 대화를 처리하지 못했습니다. 복약과 식사 기록은 저장되어 있어요. 잠시 후 다시 시도해주세요.",
        )
    if error_type in {"provider_request_failed", "agent_response_invalid", "payload_validation_failed"}:
        return FailureCopy(
            title="AI 답변 생성에 실패했습니다",
            body="AI가 대화를 처리하지 못했습니다. 안전한 기본 안내만 유지하고, 잠시 후 다시 시도해주세요.",
        )
    if source_event_type in ASYNC_TASK_FAILURES:
        return ASYNC_TASK_FAILURES[source_event_type]
    return DEFAULT_AGENT_FAILURE


def copy_for_phr_error(detail: str = "", *, status_code: int | None = None) -> FailureCopy:
    if detail in PHR_FAILURES:
        return PHR_FAILURES[detail]
    if status_code == 503:
        return PHR_FAILURES["phr_read_only_mode"]
    if status_code == 404:
        return PHR_FAILURES["phr_patient_not_found"]
    return PHR_FAILURES["phr_network_error"]
