from shared.tool_argument_validation import tool_argument_validation_error
from shared.tool_names import (
    CREATE_NUTRITION_MEAL_RECORD,
    GET_MEDICATION_DOSE_STATUS,
    GET_NUTRITION_MEAL_RECORD_LIST,
    SEARCH_NUTRITION_FOOD_CANDIDATES,
    SOURCE_MEDICATION_AGENT,
    SOURCE_NUTRITION_MANAGEMENT_AGENT,
)
from shared.tool_permissions import validate_tool_permission


def _denial(
    tool_name: str,
    arguments: object,
    *,
    source_event_type: str,
) -> str | None:
    return validate_tool_permission(
        {"name": tool_name, "arguments": arguments},
        source_event_type=source_event_type,
        payload={},
    )


def test_catalog_schema_is_the_runtime_source_for_structural_validation() -> None:
    valid = {
        "food_queries": ["토스트", "우유"],
        "limit_per_query": 6,
        "meal_type": "breakfast",
    }
    assert tool_argument_validation_error(
        SEARCH_NUTRITION_FOOD_CANDIDATES,
        valid,
    ) is None

    denial = tool_argument_validation_error(
        SEARCH_NUTRITION_FOOD_CANDIDATES,
        valid | {"unknown": True},
    )
    assert denial is not None
    assert "Additional properties" in denial

    denial = tool_argument_validation_error(
        SEARCH_NUTRITION_FOOD_CANDIDATES,
        {"food_queries": ["   "]},
    )
    assert denial is not None
    assert "does not match" in denial


def test_selected_record_action_uses_its_catalog_schema() -> None:
    denial = tool_argument_validation_error(
        CREATE_NUTRITION_MEAL_RECORD,
        {"meal_type": "breakfast"},
    )

    assert denial is not None
    assert "'foods' is a required property" in denial


def test_contextual_date_range_rule_remains_at_permission_boundary() -> None:
    denial = _denial(
        GET_MEDICATION_DOSE_STATUS,
        {
            "start_date": "2026-01-01",
            "end_date": "2026-02-15",
        },
        source_event_type=SOURCE_MEDICATION_AGENT,
    )

    assert denial == f"{GET_MEDICATION_DOSE_STATUS} date range is too large"


def test_nutrition_meal_list_accepts_one_bounded_date_range() -> None:
    arguments = {
        "start_date": "2026-07-19",
        "end_date": "2026-07-25",
    }

    assert tool_argument_validation_error(
        GET_NUTRITION_MEAL_RECORD_LIST,
        arguments,
    ) is None
    assert _denial(
        GET_NUTRITION_MEAL_RECORD_LIST,
        arguments,
        source_event_type=SOURCE_NUTRITION_MANAGEMENT_AGENT,
    ) is None


def test_nutrition_meal_list_rejects_mixed_single_day_and_range() -> None:
    denial = _denial(
        GET_NUTRITION_MEAL_RECORD_LIST,
        {
            "meal_date": "2026-07-25",
            "start_date": "2026-07-19",
            "end_date": "2026-07-25",
        },
        source_event_type=SOURCE_NUTRITION_MANAGEMENT_AGENT,
    )

    assert denial == (
        f"{GET_NUTRITION_MEAL_RECORD_LIST} requires either meal_date or "
        "start_date/end_date, not both"
    )
