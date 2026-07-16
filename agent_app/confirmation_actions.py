from __future__ import annotations

from dataclasses import dataclass

from agent_app.tool_names import (
    UPDATE_MEDICATION_DOSE_EVENT_STATUS,
    UPSERT_NUTRITION_PREFERENCE_FACT,
)

CONFIRMATION_NONE = "none"
CONFIRMATION_USER_REQUIRED = "user_required"
CONFIRMATION_APP_SERVER = "app_server_confirmation"


@dataclass(frozen=True)
class ConfirmationAction:
    action_name: str
    action_type: str
    confirmation_policy: str


class ConfirmationActionRegistry:
    _ACTIONS = {
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
    }

    @classmethod
    def get(cls, action_name: str) -> ConfirmationAction | None:
        return cls._ACTIONS.get(str(action_name or ""))

    @classmethod
    def requires_confirmation(cls, action_name: str) -> bool:
        action = cls.get(action_name)
        return action is not None and action.confirmation_policy == CONFIRMATION_USER_REQUIRED

    @classmethod
    def enabled_action_names(cls) -> frozenset[str]:
        return frozenset(cls._ACTIONS)
