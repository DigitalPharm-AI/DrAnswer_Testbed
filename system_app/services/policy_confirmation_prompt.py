from __future__ import annotations

from sqlalchemy.orm import Session

from shared.schemas import MultipleChoiceOption, MultipleChoicePrompt, NotificationPolicyDelta
from system_app.services.policy_confirmation_constants import (
    GENERIC_POLICY_CONFIRMATION_SUMMARIES,
    POLICY_ACTION_DECREASE,
    POLICY_ACTION_INCREASE,
    POLICY_ACTION_KEEP,
    POLICY_ACTION_LABELS,
    UNSUITABLE_POLICY_CONFIRMATION_PHRASES,
)
from system_app.services.policy_confirmation_diff import policy_confirmation_actions, recommended_policy_action


def multiple_choice_option(number: int, value: str, description: str = "") -> dict:
    label = POLICY_ACTION_LABELS.get(value, value)
    return MultipleChoiceOption(
        number=number,
        value=value,
        label=label,
        text=f"{number}. {label}",
        description=description,
    ).model_dump(mode="json")


def build_multiple_choice_prompt(
    *,
    prompt_type: str,
    question: str,
    action_values: list[str],
    recommended_value: str | None = None,
) -> dict:
    option_descriptions = {
        POLICY_ACTION_INCREASE: "제안된 방향대로 알림을 더 촘촘하게 조정합니다.",
        POLICY_ACTION_DECREASE: "제안된 방향대로 알림 부담을 줄입니다.",
        POLICY_ACTION_KEEP: "현재 알림 정책을 유지합니다.",
    }
    return MultipleChoicePrompt(
        prompt_type=prompt_type,
        question=question,
        recommended_value=recommended_value,
        options=[
            MultipleChoiceOption.model_validate(multiple_choice_option(index, value, option_descriptions.get(value, "")))
            for index, value in enumerate(action_values, start=1)
        ],
    ).model_dump(mode="json")


def policy_confirmation_question_from_agent_summary(summary: str | None) -> str | None:
    if summary is None:
        return None
    cleaned = " ".join(summary.strip().split())
    if not cleaned or cleaned in GENERIC_POLICY_CONFIRMATION_SUMMARIES:
        return None
    if any(phrase in cleaned for phrase in UNSUITABLE_POLICY_CONFIRMATION_PHRASES):
        return None
    if cleaned.endswith(("?", "요?", "까요?")) or "어떨까요" in cleaned or "괜찮을까요" in cleaned:
        return cleaned
    return None


def policy_delta_slot_group_label(deltas: list[NotificationPolicyDelta]) -> str:
    slot_labels = [delta.slot_label for delta in deltas]
    slot_names = [label.split()[0] for label in slot_labels]
    if {"아침", "점심", "야간"}.issubset(set(slot_names)):
        return "아침, 점심, 야간 모든 시간대"
    if len(slot_labels) == 1:
        return slot_labels[0]
    return ", ".join(slot_labels)


def policy_confirmation_question_from_deltas(deltas: list[NotificationPolicyDelta]) -> str | None:
    if not deltas:
        return None
    same_extra = {delta.extra_reminders for delta in deltas}
    same_interval = {delta.interval_minutes for delta in deltas}
    target = policy_delta_slot_group_label(deltas)
    if len(same_extra) == 1 and len(same_interval) == 1:
        extra_reminders = next(iter(same_extra))
        interval_minutes = next(iter(same_interval))
        return f"{target}의 복약 알림을 추가 {extra_reminders}회, {interval_minutes}분 간격으로 변경하시는 건 어떨까요?"
    return f"{target}의 복약 알림을 아래 후보처럼 변경하시는 건 어떨까요?"


def policy_confirmation_question(
    recommended_action: str | None,
    agent_summary: str | None = None,
    deltas: list[NotificationPolicyDelta] | None = None,
) -> str:
    agent_question = policy_confirmation_question_from_agent_summary(agent_summary)
    if agent_question:
        return agent_question
    delta_question = policy_confirmation_question_from_deltas(deltas or [])
    if delta_question:
        return delta_question
    if recommended_action == POLICY_ACTION_INCREASE:
        return "최근 기록을 보면 이 시간대에는 알림을 조금 더 늘려보는 게 도움이 될 것 같아요. 늘려볼까요?"
    if recommended_action == POLICY_ACTION_DECREASE:
        return "알림이 조금 부담스러울 수 있어 보여요. 이번에는 알림을 줄여볼까요?"
    if recommended_action == POLICY_ACTION_KEEP:
        return "지금 알림 설정도 크게 나쁘지 않아 보여요. 현행대로 유지해볼까요?"
    return "알림을 늘릴지 줄일지 조금 애매해요. 어떤 방식이 더 편하실까요?"


def policy_confirmation_prompt(session: Session, deltas: list[NotificationPolicyDelta], agent_summary: str | None = None) -> dict:
    recommended_action = recommended_policy_action(session, deltas)
    return build_multiple_choice_prompt(
        prompt_type="policy_confirmation",
        question=policy_confirmation_question(recommended_action, agent_summary, deltas),
        action_values=policy_confirmation_actions(recommended_action),
        recommended_value=recommended_action,
    )
