from __future__ import annotations

from collections.abc import Callable
from datetime import date

from fastapi import APIRouter, Depends, Path, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from shared.public_ids import new_public_id
from shared.settings import get_settings
from system_app.db import get_session
from system_app.routes.ui_chat import stream_ui_chat, sync_ui_chat
from system_app.routes.ui_responses import (
    ui_error_responses,
    ui_http_error,
    ui_success,
)
from system_app.runtime import SystemRuntime
from system_app.services.ui_policy_service import (
    UI_POLICY_KEYS,
    set_ui_policy,
    ui_policy_state,
)
from system_app.services.ui_view_service import (
    NotificationCursorNotFound,
    acknowledge_all_notifications,
    chat_history_page,
    notification_detail,
    notification_list,
    nutrition_dashboard_ui_view,
)
from system_app.ui_contracts import (
    EmptyUiRequest,
    UiChatHistoryResponse,
    UiChatRequest,
    UiChatSyncResponse,
    UiNotificationAcknowledgementResponse,
    UiNotificationDetailResponse,
    UiNotificationListResponse,
    UiNutritionResponse,
    UiPoliciesResponse,
    UpdateUiPolicyRequest,
)


def create_patient_ui_router(
    get_runtime: Callable[[], SystemRuntime],
) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/nutrition",
        response_model=UiNutritionResponse,
        responses=ui_error_responses(422, 500),
    )
    async def get_nutrition(
        date_value: date | None = Query(default=None, alias="date"),
        session: Session = Depends(get_session),
    ):
        return ui_success(
            UiNutritionResponse,
            nutrition_dashboard_ui_view(
                session,
                patient_id=get_settings().patient_id,
                target_date=date_value,
            ),
        )

    @router.get(
        "/policies",
        response_model=UiPoliciesResponse,
        responses=ui_error_responses(422, 500),
    )
    async def get_policies(session: Session = Depends(get_session)):
        return ui_success(
            UiPoliciesResponse,
            {
                "policies": ui_policy_state(
                    session,
                    patient_id=get_settings().patient_id,
                )
            },
        )

    @router.put(
        "/policies/{policy_key}",
        response_model=UiPoliciesResponse,
        responses=ui_error_responses(404, 422, 500),
    )
    async def update_policy(
        policy_key: str,
        payload: UpdateUiPolicyRequest,
        session: Session = Depends(get_session),
    ):
        if policy_key not in UI_POLICY_KEYS:
            raise ui_http_error(
                404,
                "UI_POLICY_NOT_FOUND",
                "The UI policy was not found.",
            )
        with get_runtime().write_lock:
            policies = set_ui_policy(
                session,
                policy_key,
                payload.enabled,
                patient_id=get_settings().patient_id,
            )
            session.commit()
        return ui_success(UiPoliciesResponse, {"policies": policies})

    @router.get(
        "/notifications",
        response_model=UiNotificationListResponse,
        responses=ui_error_responses(422, 500),
    )
    async def get_notifications(
        after_id: str | None = Query(
            default=None,
            pattern=r"^notif_[0-9a-f]{32}$",
        ),
        limit: int = Query(default=50, ge=1, le=100),
        session: Session = Depends(get_session),
    ):
        try:
            return ui_success(
                UiNotificationListResponse,
                notification_list(
                    session,
                    after_id=after_id,
                    limit=limit,
                ),
            )
        except NotificationCursorNotFound as exc:
            raise ui_http_error(
                422,
                "NOTIFICATION_CURSOR_INVALID",
                "The notification cursor is invalid or no longer available.",
            ) from exc

    @router.get(
        "/notifications/{notification_id}",
        response_model=UiNotificationDetailResponse,
        responses=ui_error_responses(404, 422, 500),
    )
    async def get_notification(
        notification_id: str = Path(
            pattern=r"^notif_[0-9a-f]{32}$",
        ),
        session: Session = Depends(get_session),
    ):
        result = notification_detail(session, notification_id)
        if result is None:
            raise ui_http_error(
                404,
                "NOTIFICATION_NOT_FOUND",
                "The notification was not found.",
            )
        return ui_success(
            UiNotificationDetailResponse,
            {"notification": result},
        )

    @router.post(
        "/notifications/ack-all",
        response_model=UiNotificationAcknowledgementResponse,
        responses=ui_error_responses(422, 500),
    )
    async def acknowledge_notifications(
        _payload: EmptyUiRequest | None = None,
        session: Session = Depends(get_session),
    ):
        with get_runtime().write_lock:
            count = acknowledge_all_notifications(session)
            session.commit()
        return ui_success(
            UiNotificationAcknowledgementResponse,
            {"acknowledged_count": count},
        )

    @router.get(
        "/chat/history",
        response_model=UiChatHistoryResponse,
        responses=ui_error_responses(422, 500),
    )
    async def get_chat_history(
        before_date: date | None = None,
        limit_days: int = Query(default=1, ge=1, le=7),
        limit_turns: int = Query(default=50, ge=1, le=200),
        session: Session = Depends(get_session),
    ):
        return ui_success(
            UiChatHistoryResponse,
            chat_history_page(
                session,
                before_date=before_date,
                limit_days=limit_days,
                limit_turns=limit_turns,
            ),
        )

    @router.post(
        "/chat/sync",
        response_model=UiChatSyncResponse,
        responses=ui_error_responses(409, 422, 500, 502),
    )
    async def sync_chat(
        payload: UiChatRequest,
        session: Session = Depends(get_session),
    ):
        return await sync_ui_chat(get_runtime(), session, payload)

    @router.post(
        "/chat/stream",
        response_class=StreamingResponse,
        responses={
            200: {
                "description": "Synchronous final-answer stream.",
                "content": {"text/event-stream": {}},
            }
        },
    )
    async def stream_chat(
        payload: UiChatRequest,
        session: Session = Depends(get_session),
    ):
        if payload.request_id is None:
            payload = payload.model_copy(
                update={"request_id": new_public_id("request")}
            )
        return stream_ui_chat(
            get_runtime(),
            session,
            payload,
            sync_ui_chat=sync_ui_chat,
        )

    return router
