from __future__ import annotations

from dataclasses import dataclass

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

CONFIRMATION_USER_REQUIRED = "user_required"


@dataclass(frozen=True)
class ConfirmationAction:
    action_name: str
    action_type: str
    confirmation_policy: str


class ConfirmationActionRegistry:
    _ACTIONS = {
        CHANGE_NOTIFICATION_POLICY: ConfirmationAction(
            action_name=CHANGE_NOTIFICATION_POLICY,
            action_type="agent_tool",
            confirmation_policy=CONFIRMATION_USER_REQUIRED,
        ),
        CREATE_MEDICATION_SIDE_EFFECT_RECORD: ConfirmationAction(
            action_name=CREATE_MEDICATION_SIDE_EFFECT_RECORD,
            action_type="agent_tool",
            confirmation_policy=CONFIRMATION_USER_REQUIRED,
        ),
        UPDATE_MEDICATION_DOSE_EVENT_STATUS: ConfirmationAction(
            action_name=UPDATE_MEDICATION_DOSE_EVENT_STATUS,
            action_type="agent_tool",
            confirmation_policy=CONFIRMATION_USER_REQUIRED,
        ),
        UPSERT_NUTRITION_PREFERENCE_FACT: ConfirmationAction(
            action_name=UPSERT_NUTRITION_PREFERENCE_FACT,
            action_type="agent_tool",
            confirmation_policy=CONFIRMATION_USER_REQUIRED,
        ),
        CREATE_NUTRITION_MEAL_RECORD: ConfirmationAction(
            action_name=CREATE_NUTRITION_MEAL_RECORD,
            action_type="agent_tool",
            confirmation_policy=CONFIRMATION_USER_REQUIRED,
        ),
        UPDATE_NUTRITION_MEAL_RECORD: ConfirmationAction(
            action_name=UPDATE_NUTRITION_MEAL_RECORD,
            action_type="agent_tool",
            confirmation_policy=CONFIRMATION_USER_REQUIRED,
        ),
        DELETE_NUTRITION_MEAL_RECORD: ConfirmationAction(
            action_name=DELETE_NUTRITION_MEAL_RECORD,
            action_type="agent_tool",
            confirmation_policy=CONFIRMATION_USER_REQUIRED,
        ),
        UPDATE_NUTRITION_FOOD_RECORD: ConfirmationAction(
            action_name=UPDATE_NUTRITION_FOOD_RECORD,
            action_type="agent_tool",
            confirmation_policy=CONFIRMATION_USER_REQUIRED,
        ),
        DELETE_NUTRITION_FOOD_RECORD: ConfirmationAction(
            action_name=DELETE_NUTRITION_FOOD_RECORD,
            action_type="agent_tool",
            confirmation_policy=CONFIRMATION_USER_REQUIRED,
        ),
    }

    @classmethod
    def get(cls, action_name: str) -> ConfirmationAction | None:
        return cls._ACTIONS.get(str(action_name or ""))

    @classmethod
    def requires_confirmation(cls, action_name: str) -> bool:
        action = cls.get(action_name)
        return action is not None and action.confirmation_policy == CONFIRMATION_USER_REQUIRED
