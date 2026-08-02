from __future__ import annotations

from datetime import date, datetime
from typing import Any

from shared.nutrition_domain import meal_type_label
from shared.tool_catalog import ToolCatalog
from shared.tool_names import (
    CHANGE_NOTIFICATION_POLICY,
    CREATE_MEDICATION_SIDE_EFFECT_RECORD,
    CREATE_NUTRITION_MEAL_RECORD,
    DELETE_NUTRITION_FOOD_RECORD,
    DELETE_NUTRITION_MEAL_RECORD,
    UPDATE_MEDICATION_DOSE_EVENT_STATUS,
    UPDATE_NUTRITION_FOOD_RECORD,
    UPDATE_NUTRITION_MEAL_RECORD,
    UPSERT_NUTRITION_PREFERENCE_FACT,
)


def project_record_arguments_to_action_schema(
    action_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Keep only fields declared by the selected canonical write action."""

    action_tools = ToolCatalog.tools_for(action_name)
    if len(action_tools) != 1:
        raise ValueError("record_approval_action_schema_missing")
    input_schema = action_tools[0].get("inputSchema")
    if not isinstance(input_schema, dict):
        raise ValueError("record_approval_action_schema_missing")
    projected = _project_value_to_schema(arguments, input_schema)
    if not isinstance(projected, dict):
        raise ValueError("record_approval_arguments_invalid")
    return projected


def _project_value_to_schema(value: Any, schema: dict[str, Any]) -> Any:
    schema_type = schema.get("type")
    if schema_type == "object" and isinstance(value, dict):
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            return {}
        return {key: _project_value_to_schema(child, properties[key]) for key, child in value.items() if key in properties and isinstance(properties[key], dict)}
    if schema_type == "array" and isinstance(value, list):
        item_schema = schema.get("items")
        if not isinstance(item_schema, dict):
            return list(value)
        return [_project_value_to_schema(item, item_schema) for item in value]
    return value


def approval_display(
    action_name: str,
    arguments: dict[str, Any],
    *,
    display_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the non-secret approval card owned by the AI Server."""

    if action_name == CREATE_MEDICATION_SIDE_EFFECT_RECORD:
        display = {
            "title": "부작용 평가 기록",
            "question": "다음 부작용 평가 결과를 기록할까요?",
            "tables": [
                {
                    "table_title": None,
                    "rows": _side_effect_approval_rows(
                        arguments,
                        display_context=display_context,
                    ),
                }
            ],
            "action_label": "기록",
        }
    elif action_name == UPDATE_MEDICATION_DOSE_EVENT_STATUS:
        display = {
            "title": "복약 완료 기록",
            "question": "선택한 복약 일정을 복약 완료로 기록할까요?",
            "action_label": "기록",
        }
    elif action_name == CREATE_NUTRITION_MEAL_RECORD:
        display = {
            "title": "식사 기록",
            "question": "다음 식사 내용을 기록할까요?",
            "tables": [
                {
                    "table_title": None,
                    "rows": _nutrition_meal_approval_rows(arguments),
                }
            ],
            "action_label": "기록",
        }
    elif action_name == UPDATE_NUTRITION_MEAL_RECORD:
        display = {
            "title": "식사 기록 수정",
            "question": "확인한 내용으로 식사 기록을 수정할까요?",
            "action_label": "수정",
        }
    elif action_name == DELETE_NUTRITION_MEAL_RECORD:
        display = {
            "title": "식사 기록 삭제",
            "question": "선택한 식사 기록을 삭제할까요?",
            "action_label": "삭제",
        }
    elif action_name == UPDATE_NUTRITION_FOOD_RECORD:
        display = {
            "title": "음식 기록 수정",
            "question": "확인한 내용으로 음식 기록을 수정할까요?",
            "action_label": "수정",
        }
    elif action_name == DELETE_NUTRITION_FOOD_RECORD:
        display = {
            "title": "음식 기록 삭제",
            "question": "선택한 음식 기록을 삭제할까요?",
            "action_label": "삭제",
        }
    elif action_name == CHANGE_NOTIFICATION_POLICY:
        keep = str(arguments.get("decision") or "") == "keep"
        display = {
            "title": "알림 정책 확인",
            "question": (
                "현재 알림 정책을 유지할까요?"
                if keep
                else _notification_policy_approval_question(
                    arguments,
                    display_context=display_context,
                )
            ),
            "tables": [
                {
                    "table_title": None,
                    "rows": _notification_policy_approval_rows(
                        arguments,
                        display_context=display_context,
                    ),
                }
            ],
            "action_label": "유지" if keep else "변경",
        }
    else:
        display = {
            "title": "기록 확인",
            "question": "확인한 내용을 적용할까요?",
            "action_label": "적용",
        }
    return _with_approval_time_rows(
        display,
        action_name=action_name,
        arguments=arguments,
        display_context=display_context,
    )


def _with_approval_time_rows(
    display: dict[str, Any],
    *,
    action_name: str,
    arguments: dict[str, Any],
    display_context: dict[str, Any] | None,
) -> dict[str, Any]:
    context = display_context or {}
    requested_at = _display_datetime(context.get("requested_at"))
    target_time = _record_target_time(
        action_name,
        arguments,
        display_context=context,
    )
    request_time_label = (
        "변경 요청 시간"
        if action_name == CHANGE_NOTIFICATION_POLICY
        else "기록 요청 시간"
    )
    target_time_label = (
        "적용 시점"
        if action_name == CHANGE_NOTIFICATION_POLICY
        else "기록 대상 시간"
    )
    time_rows = [
        {
            "column": request_time_label,
            "value": requested_at or "확인되지 않음",
        },
        {
            "column": target_time_label,
            "value": target_time or "확인되지 않음",
        },
    ]

    tables = display.get("tables")
    normalized_tables = (
        [
            {
                **table,
                "rows": list(table.get("rows") or []),
            }
            for table in tables
            if isinstance(table, dict)
        ]
        if isinstance(tables, list)
        else []
    )
    if normalized_tables:
        normalized_tables[0]["rows"].extend(time_rows)
    else:
        normalized_tables = [
            {
                "table_title": None,
                "rows": time_rows,
            }
        ]
    return {
        **display,
        "tables": normalized_tables,
    }


def _record_target_time(
    action_name: str,
    arguments: dict[str, Any],
    *,
    display_context: dict[str, Any],
) -> str:
    if action_name == CREATE_MEDICATION_SIDE_EFFECT_RECORD:
        return str(arguments.get("symptom_onset_text") or display_context.get("symptom_onset_text") or "").strip()

    if action_name == CREATE_NUTRITION_MEAL_RECORD:
        return _meal_target_time(
            arguments,
            requested_at=display_context.get("requested_at"),
            default_to_requested_date=True,
        )

    if action_name == UPDATE_NUTRITION_MEAL_RECORD:
        snapshot_target = _meal_target_time_from_snapshot(
            arguments,
            display_context=display_context,
            apply_updates=True,
        )
        if snapshot_target:
            return snapshot_target
        return _meal_target_time(
            arguments,
            requested_at=display_context.get("requested_at"),
            default_to_requested_date=False,
        )

    if action_name in {
        DELETE_NUTRITION_MEAL_RECORD,
        UPDATE_NUTRITION_FOOD_RECORD,
        DELETE_NUTRITION_FOOD_RECORD,
    }:
        return _meal_target_time_from_snapshot(
            arguments,
            display_context=display_context,
            apply_updates=False,
        )

    if action_name == UPDATE_MEDICATION_DOSE_EVENT_STATUS:
        return _dose_target_time_from_snapshot(
            arguments,
            display_context=display_context,
        )

    if action_name == CHANGE_NOTIFICATION_POLICY:
        changes = arguments.get("changes")
        if isinstance(changes, dict):
            start = str(changes.get("effective_start_date") or "").strip()
            end = str(changes.get("effective_end_date") or "").strip()
            if start and end:
                return f"{start} ~ {end}"
            if start:
                return f"{start}부터"
            if end:
                return f"{end}까지"
        return "사용자 승인 시점부터"

    if action_name == UPSERT_NUTRITION_PREFERENCE_FACT:
        return "사용자 승인 시점부터"
    return ""


def _notification_policy_approval_rows(
    arguments: dict[str, Any],
    *,
    display_context: dict[str, Any] | None,
) -> list[dict[str, str]]:
    context = display_context or {}
    snapshot = context.get("notification_policy")
    policy = snapshot if isinstance(snapshot, dict) else {}
    rows: list[dict[str, str]] = []

    slot_label = str(policy.get("slot_label") or "").strip()
    if slot_label:
        rows.append({"column": "시간대", "value": slot_label})

    changes = arguments.get("changes")
    if not isinstance(changes, dict) or not changes:
        return rows

    descriptions: list[str] = []
    current_values: list[str] = []
    changed_values: list[str] = []
    handled: set[str] = set()

    if "missed_dose_after_minutes" in changes:
        descriptions.append("미복용 알림 시점")
        current_values.append(
            _minutes_after_schedule(
                policy.get("missed_dose_after_minutes")
            )
        )
        changed_values.append(
            _minutes_after_schedule(
                changes.get("missed_dose_after_minutes")
            )
        )
        handled.add("missed_dose_after_minutes")

    if "extra_reminders" in changes:
        descriptions.append("추가 알림 횟수")
        current_values.append(
            _count_display(policy.get("extra_reminders"))
        )
        changed_values.append(
            _count_display(changes.get("extra_reminders"))
        )
        handled.add("extra_reminders")

    if "interval_minutes" in changes:
        descriptions.append("추가 알림 간격")
        current_values.append(
            _minute_interval(policy.get("interval_minutes"))
        )
        changed_values.append(
            _minute_interval(changes.get("interval_minutes"))
        )
        handled.add("interval_minutes")

    primary_keys = {
        "primary_reminder_timing",
        "primary_reminder_offset_minutes",
    }
    if primary_keys.intersection(changes):
        descriptions.append("기본 알림 시점")
        current_values.append(_primary_reminder_display(policy))
        changed_values.append(
            _primary_reminder_display({**policy, **changes})
        )
        handled.update(primary_keys)

    for key, label in (
        ("effective_start_date", "정책 시작일"),
        ("effective_end_date", "정책 종료일"),
    ):
        if key not in changes:
            continue
        descriptions.append(label)
        current_values.append(_known_value(policy.get(key)))
        changed_values.append(_known_value(changes.get(key)))
        handled.add(key)

    for key in sorted(set(changes).difference(handled)):
        descriptions.append(key)
        current_values.append(_known_value(policy.get(key)))
        changed_values.append(_known_value(changes.get(key)))

    if descriptions:
        rows.extend(
            [
                {
                    "column": "변경 항목",
                    "value": ", ".join(descriptions),
                },
                {
                    "column": "현재 설정",
                    "value": ", ".join(current_values),
                },
                {
                    "column": "변경 설정",
                    "value": ", ".join(changed_values),
                },
            ]
        )
    return rows


def _notification_policy_approval_question(
    arguments: dict[str, Any],
    *,
    display_context: dict[str, Any] | None,
) -> str:
    context = display_context or {}
    snapshot = context.get("notification_policy")
    policy = snapshot if isinstance(snapshot, dict) else {}
    changes = arguments.get("changes")
    if (
        isinstance(changes, dict)
        and set(changes) == {"missed_dose_after_minutes"}
    ):
        slot_label = str(policy.get("slot_label") or "").strip()
        current_value = _minutes_after_schedule(
            policy.get("missed_dose_after_minutes")
        )
        changed_value = _minutes_after_schedule(
            changes.get("missed_dose_after_minutes")
        )
        prefix = f"{slot_label} " if slot_label else ""
        return (
            f"{prefix}미복용 알림을 {current_value}에서 "
            f"{changed_value}로 변경할까요?"
        )
    return "확인한 내용으로 알림 정책을 변경할까요?"


def _known_value(value: Any) -> str:
    text = str(value if value is not None else "").strip()
    return text or "확인되지 않음"


def _minutes_after_schedule(value: Any) -> str:
    text = _known_value(value)
    if text == "확인되지 않음":
        return text
    return f"복약 예정 {text}분 후"


def _minute_interval(value: Any) -> str:
    text = _known_value(value)
    return f"{text}분" if text != "확인되지 않음" else text


def _count_display(value: Any) -> str:
    text = _known_value(value)
    return f"{text}회" if text != "확인되지 않음" else text


def _primary_reminder_display(policy: dict[str, Any]) -> str:
    timing = str(policy.get("primary_reminder_timing") or "").strip()
    offset = _known_value(policy.get("primary_reminder_offset_minutes"))
    if timing == "at":
        return "복약 예정 시각"
    if timing == "before":
        return f"복약 예정 {offset}분 전"
    if timing == "after":
        return f"복약 예정 {offset}분 후"
    return "확인되지 않음"


def _meal_target_time(
    arguments: dict[str, Any],
    *,
    requested_at: Any,
    default_to_requested_date: bool,
) -> str:
    meal_date = str(arguments.get("meal_date") or "").strip()
    meal_time = str(arguments.get("meal_time") or "").strip()
    meal_type = str(arguments.get("meal_type") or "").strip()
    if not meal_date and default_to_requested_date:
        meal_date = _display_date(requested_at)
    if not meal_date:
        return ""
    if meal_time:
        return f"{meal_date} {meal_time[:5]}"
    display_label = meal_type_label(meal_type)
    return f"{meal_date} {display_label}" if display_label else meal_date


def _meal_target_time_from_snapshot(
    arguments: dict[str, Any],
    *,
    display_context: dict[str, Any],
    apply_updates: bool,
) -> str:
    meal_id = str(arguments.get("meal_id") or "").strip()
    if not meal_id:
        return ""
    snapshot = display_context.get("trusted_patient_context")
    if not isinstance(snapshot, dict):
        return ""
    meals = snapshot.get("today_meals")
    if not isinstance(meals, list):
        return ""
    for meal in meals:
        if not isinstance(meal, dict):
            continue
        candidate_id = str(meal.get("meal_id") or meal.get("id") or "").strip()
        if candidate_id != meal_id:
            continue
        target_values = dict(meal)
        if apply_updates:
            for key in ("meal_date", "meal_time", "meal_type"):
                if arguments.get(key) is not None:
                    target_values[key] = arguments[key]
        return _meal_target_time(
            target_values,
            requested_at=display_context.get("requested_at"),
            default_to_requested_date=False,
        )
    return ""


def _dose_target_time_from_snapshot(
    arguments: dict[str, Any],
    *,
    display_context: dict[str, Any],
) -> str:
    dose_event_id = str(arguments.get("dose_event_id") or "").strip()
    if not dose_event_id:
        return ""
    snapshot = display_context.get("trusted_patient_context")
    if not isinstance(snapshot, dict):
        return ""
    today_medication = snapshot.get("today_medication")
    if not isinstance(today_medication, dict):
        return ""
    events = today_medication.get("dose_events")
    if not isinstance(events, list):
        return ""
    for event in events:
        if not isinstance(event, dict):
            continue
        candidate_id = str(event.get("dose_event_id") or event.get("id") or "").strip()
        if candidate_id == dose_event_id:
            return _display_datetime(event.get("scheduled_for"))
    return ""


def _display_datetime(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            return text
    return parsed.strftime("%Y-%m-%d %H:%M")


def _display_date(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            return ""


def _nutrition_meal_approval_rows(arguments: dict[str, Any]) -> list[dict[str, str]]:
    meal_type = str(arguments.get("meal_type") or "").strip()
    display_label = meal_type_label(meal_type) or "확인 필요"

    food_names: list[str] = []
    foods = arguments.get("foods")
    if isinstance(foods, list):
        for food in foods:
            if not isinstance(food, dict):
                continue
            food_name = str(food.get("food_name") or "").strip()
            if food_name and food_name not in food_names:
                food_names.append(food_name)

    return [
        {"column": "시기", "value": display_label},
        {"column": "음식 종류", "value": ", ".join(food_names) or "확인 필요"},
    ]


def _side_effect_approval_rows(
    arguments: dict[str, Any],
    *,
    display_context: dict[str, Any] | None,
) -> list[dict[str, str]]:
    context = display_context or {}
    symptom_name = str(context.get("symptom_name") or "").strip()
    if not symptom_name:
        symptom_name = _symptom_name_from_matched_effects(arguments)
    if not symptom_name:
        symptom_name = str(arguments.get("symptom_text") or "증상").strip()

    rows = [{"column": "증상", "value": symptom_name}]
    rows.extend(_pro_ctcae_response_rows(arguments.get("severity")))

    matched_items = _unique_text_values(arguments.get("matched_items"))
    if matched_items:
        rows.append({"column": "관련 가능 약물", "value": ", ".join(matched_items)})
    return rows


def _symptom_name_from_matched_effects(arguments: dict[str, Any]) -> str:
    for value in _unique_text_values(arguments.get("matched_effects")):
        _, separator, effect = value.partition(":")
        normalized = effect.strip() if separator else value.strip()
        if normalized:
            return normalized
    return ""


def _pro_ctcae_response_rows(severity: Any) -> list[dict[str, str]]:
    if not isinstance(severity, dict):
        return []
    questions = severity.get("questions")
    responses = severity.get("responses")
    if not isinstance(questions, list) or not isinstance(responses, list):
        return []

    questions_by_code = {str(question.get("item_code") or ""): question for question in questions if isinstance(question, dict)}
    rows: list[dict[str, str]] = []
    for index, response in enumerate(responses):
        if not isinstance(response, dict):
            continue
        response_text = str(response.get("response_text") or "").strip()
        if not response_text:
            continue
        item_code = str(response.get("item_code") or "")
        question = questions_by_code.get(item_code)
        question_text = str(question.get("question") or "").strip() if isinstance(question, dict) else ""
        response_type = str(question.get("response_type") or "").strip() if isinstance(question, dict) else ""
        rows.append(
            {
                "column": response_type or question_text or f"문항 {index + 1}",
                "value": response_text,
            }
        )
    return rows


def _unique_text_values(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        normalized = str(item or "").strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return result
