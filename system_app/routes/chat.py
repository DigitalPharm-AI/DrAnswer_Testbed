from __future__ import annotations

import math
from collections.abc import Callable

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import and_, desc, or_, select
from sqlalchemy.orm import Session

from shared.chat_contracts import input_box_message
from shared.json_utils import dump_json, parse_json_object
from shared.tool_names import CREATE_NUTRITION_MEAL_RECORD
from shared.schemas import MutationConfirmationPrepareRequest
from shared.settings import get_settings
from system_app.db import get_session
from system_app.models import ChatMessage, MutationConfirmation, Notification
from system_app.runtime import SystemRuntime
from system_app.services.background_threads import start_daemon_thread
from system_app.services.backend_chat_service import (
    PendingChatResponseError,
    mark_pending_response_answered,
    pending_response_for_submission,
)
from system_app.services.clock_service import ensure_clock
from system_app.services.conversation_service import (
    POLICY_CONFIRMATION_ERROR,
    POLICY_CONFIRMATION_INVALID_ACTION,
    POLICY_CONFIRMATION_NOT_FOUND,
    POLICY_CONFIRMATION_STALE,
    POLICY_CONFIRMATION_WRONG_CATEGORY,
    acknowledge_notification as acknowledge_notification_record,
    handle_policy_confirmation_message,
)
from system_app.services.dashboard_view import build_chat_context
from system_app.services.food_search_service import english_to_korean_nutrients, scale_nutrients
from system_app.services.missed_dose_flag_service import is_active_missed_dose_flag_for_event
from system_app.services.missed_dose_reply_understanding import (
    annotate_missed_dose_reply,
    build_rule_based_missed_dose_reply_understanding,
    missed_dose_reply_request_metadata,
)
from system_app.services.mutation_confirmation_service import (
    PENDING,
    attach_confirmation_chat_message,
    begin_mutation_resolution,
    confirmation_card_payload,
    has_executing_confirmation,
    pending_confirmation_for_patient,
    pending_confirmation_reply_context,
    prepare_mutation_confirmation,
    prepare_side_effect_record_confirmation_for_chat,
    recover_expired_confirmations,
)
from system_app.services.nutrition_service import MEAL_TYPE_LABELS
from system_app.services.side_effect_reminder_safety import (
    SIDE_EFFECT_REMINDER_SAFETY_INVALID_ACTION,
    SIDE_EFFECT_REMINDER_SAFETY_NOT_FOUND,
    SIDE_EFFECT_REMINDER_SAFETY_STALE,
    SIDE_EFFECT_REMINDER_SAFETY_WRONG_CATEGORY,
    handle_side_effect_reminder_safety_reply,
)
from system_app.services.system_request_service import create_system_event_request
from system_app.services.timeline_service import add_chat_message, ensure_chat_message_for_conversation_alert

STALE_FOOD_SELECTION_DETAIL = "stale_food_selection"
MAX_CHAT_MESSAGE_LENGTH = 4000
ALLOWED_CHAT_EVENT_TYPES = {"multiturn_chat"}
ALLOWED_CHAT_CONTRACT_VERSIONS = {"legacy", "v1.2"}
CONFIRMATION_REPLY_HTTP_STATUS = {
    POLICY_CONFIRMATION_NOT_FOUND: 404,
    POLICY_CONFIRMATION_WRONG_CATEGORY: 409,
    POLICY_CONFIRMATION_STALE: 409,
    POLICY_CONFIRMATION_INVALID_ACTION: 422,
    POLICY_CONFIRMATION_ERROR: 500,
    SIDE_EFFECT_REMINDER_SAFETY_NOT_FOUND: 404,
    SIDE_EFFECT_REMINDER_SAFETY_WRONG_CATEGORY: 409,
    SIDE_EFFECT_REMINDER_SAFETY_STALE: 409,
    SIDE_EFFECT_REMINDER_SAFETY_INVALID_ACTION: 422,
}


def raise_for_confirmation_reply_error(result_message: str) -> None:
    status_code = CONFIRMATION_REPLY_HTTP_STATUS.get(result_message)
    if status_code is not None:
        raise HTTPException(status_code=status_code, detail=result_message)


def active_missed_dose_conversation_alert(session: Session):
    clock = ensure_clock(session)
    rows = session.scalars(
        select(Notification)
        .where(
            Notification.notification_type == "conversation_alert",
            Notification.visible_at <= clock.current_time,
            Notification.acknowledged.is_(False),
        )
        .order_by(desc(Notification.visible_at), desc(Notification.id))
        .limit(20)
    ).all()
    for notification in rows:
        metadata = parse_json_object(notification.metadata_json)
        if (
            metadata.get("category") == "missed_dose"
            and metadata.get("status") == "agent_ready"
            and is_active_missed_dose_flag_for_event(session, notification.related_dose_event_id)
        ):
            return notification
    return None


def has_newer_chat_message(session: Session, message: ChatMessage) -> bool:
    stmt = (
        select(ChatMessage.id)
        .where(
            ChatMessage.patient_id == message.patient_id,
            or_(
                ChatMessage.created_at > message.created_at,
                and_(ChatMessage.created_at == message.created_at, ChatMessage.id > message.id),
            ),
        )
        .limit(1)
    )
    return session.scalar(stmt) is not None


def food_selection_for_stage(session: Session, chat_message_id: int, expected_stage: str) -> tuple[ChatMessage, dict, dict]:
    message = session.get(ChatMessage, chat_message_id)
    if message is None or message.patient_id != get_settings().patient_id:
        raise HTTPException(status_code=404, detail="chat_message_not_found")
    metadata = parse_json_object(message.metadata_json)
    fs = metadata.get("food_selection")
    if not isinstance(fs, dict):
        raise HTTPException(status_code=400, detail="no_food_selection")
    if fs.get("stage") != expected_stage or has_newer_chat_message(session, message):
        raise HTTPException(status_code=409, detail=STALE_FOOD_SELECTION_DETAIL)
    return message, metadata, fs


def create_chat_router(get_runtime: Callable[[], SystemRuntime]) -> APIRouter:
    router = APIRouter()

    @router.post("/chat/policy-confirmation")
    def policy_confirmation_chat(
        request: Request,
        message: str = Form(...),
        notification_id: int | None = Form(None),
        session: Session = Depends(get_session),
    ):
        runtime = get_runtime()
        with runtime.write_lock:
            _, result_message = handle_policy_confirmation_message(session, message, notification_id)
            raise_for_confirmation_reply_error(result_message)
        return runtime.templates.TemplateResponse(request, "partials/chat.html", build_chat_context(request, session))

    @router.post("/chat/ae-response")
    def ae_response(
        request: Request,
        chat_message_id: int = Form(...),
        response_text: str = Form(...),
        question_index: int | None = Form(None),
        session: Session = Depends(get_session),
    ):
        runtime = get_runtime()
        with runtime.write_lock:
            normalized_response = response_text.strip()
            if not normalized_response:
                raise HTTPException(status_code=422, detail="ae_response_required")
            if len(normalized_response) > MAX_CHAT_MESSAGE_LENGTH:
                raise HTTPException(status_code=422, detail="ae_response_too_long")
            message = session.get(ChatMessage, chat_message_id)
            if message is None or message.patient_id != get_settings().patient_id:
                raise HTTPException(status_code=404, detail="chat_message_not_found")
            metadata = parse_json_object(message.metadata_json)
            ae_payload = metadata.get("ae_pro_ctcae")
            if not isinstance(ae_payload, dict):
                raise HTTPException(status_code=400, detail="chat_message_has_no_ae_prompt")
            questions = ae_payload.get("questions") if isinstance(ae_payload.get("questions"), list) else []
            responses = ae_payload.get("responses") if isinstance(ae_payload.get("responses"), list) else []
            was_complete = bool(questions) and len(responses) >= len(questions)
            if question_index is None:
                answered = {row.get("question_index") for row in responses if isinstance(row, dict)}
                question_index = next((index for index in range(len(questions)) if index not in answered), None)
            if question_index is None or not 0 <= question_index < len(questions):
                raise HTTPException(status_code=422, detail="ae_question_invalid")
            existing_response = next(
                (
                    row
                    for row in responses
                    if isinstance(row, dict) and row.get("question_index") == question_index
                ),
                None,
            )
            if existing_response is not None:
                if str(existing_response.get("response_text") or "").strip() != normalized_response:
                    raise HTTPException(status_code=409, detail="ae_question_already_answered")
                return runtime.templates.TemplateResponse(
                    request,
                    "partials/chat.html",
                    build_chat_context(request, session),
                )
            question_text = ""
            if isinstance(questions[question_index], dict):
                question_text = str(questions[question_index].get("question") or "")
            responses.append(
                {
                    "question_index": question_index,
                    "question": question_text,
                    "response_text": normalized_response,
                }
            )
            ae_payload["responses"] = responses
            metadata["ae_pro_ctcae"] = ae_payload
            message.metadata_json = dump_json(metadata)
            add_chat_message(
                session,
                role="user",
                content=f"{question_index + 1}번 문항: {normalized_response}",
                sender_type="patient",
                category="ae_response",
                metadata={"ae_response_to": chat_message_id, "question_index": question_index},
            )
            if questions and len(responses) >= len(questions) and not was_complete:
                draft = metadata.get("side_effect_record_draft")
                completion_metadata = {"ae_response_to": chat_message_id}
                if isinstance(draft, dict):
                    completion_metadata["side_effect_record_draft"] = draft
                    completion_metadata["side_effect_questionnaire_result"] = {
                        "matched": bool(ae_payload.get("matched")),
                        "match_type": str(ae_payload.get("match_type") or ""),
                        "matched_symptom_term": str(ae_payload.get("matched_symptom_term") or ""),
                        "matched_korean_symptom_name": str(ae_payload.get("matched_korean_symptom_name") or ""),
                        "questions": questions,
                        "responses": responses,
                    }
                completion_message = add_chat_message(
                    session,
                    role="assistant",
                    content=("문항 응답을 확인했습니다. 아래에서 부작용 평가 결과를 기록할지 확인해주세요." if isinstance(draft, dict) else "PRO-CTCAE 문항 응답을 확인했습니다."),
                    sender_type="assistant",
                    category="ae_response",
                    metadata=completion_metadata,
                )
                if isinstance(draft, dict):
                    prepare_side_effect_record_confirmation_for_chat(
                        session,
                        completion_message,
                    )
            session.commit()
        return runtime.templates.TemplateResponse(request, "partials/chat.html", build_chat_context(request, session))

    @router.post("/chat/side-effect-reminder-safety")
    def side_effect_reminder_safety_chat(
        request: Request,
        action: str = Form(...),
        notification_id: int | None = Form(None),
        session: Session = Depends(get_session),
    ):
        runtime = get_runtime()
        with runtime.write_lock:
            _, result_message = handle_side_effect_reminder_safety_reply(session, notification_id, action)
            if result_message in CONFIRMATION_REPLY_HTTP_STATUS:
                session.rollback()
            raise_for_confirmation_reply_error(result_message)
            session.commit()
        return runtime.templates.TemplateResponse(request, "partials/chat.html", build_chat_context(request, session))

    @router.post("/chat/system")
    def system_chat(
        request: Request,
        event_type: str = Form("multiturn_chat"),
        message: str = Form(...),
        contract_version: str = Form("legacy"),
        session: Session = Depends(get_session),
    ):
        runtime = get_runtime()
        normalized_message = message.strip()
        if not normalized_message:
            raise HTTPException(status_code=422, detail="chat_message_required")
        if len(normalized_message) > MAX_CHAT_MESSAGE_LENGTH:
            raise HTTPException(status_code=422, detail="chat_message_too_long")
        if event_type not in ALLOWED_CHAT_EVENT_TYPES:
            raise HTTPException(status_code=422, detail="chat_event_type_invalid")
        if contract_version not in ALLOWED_CHAT_CONTRACT_VERSIONS:
            raise HTTPException(status_code=422, detail="chat_contract_version_invalid")
        with runtime.write_lock:
            patient_id = get_settings().patient_id
            recover_expired_confirmations(session, patient_id)
            if has_executing_confirmation(session, patient_id):
                session.commit()
                raise HTTPException(status_code=409, detail="mutation_confirmation_execution_in_progress")
            clock = ensure_clock(session)
            pending_confirmation = pending_confirmation_for_patient(session, patient_id)
            request_metadata: dict = {}
            if contract_version == "v1.2":
                request_metadata["contract_version"] = "v1.2"
            if pending_confirmation is not None:
                request_metadata["pending_mutation_confirmation_id"] = pending_confirmation.public_id
                request_metadata["pending_mutation_confirmation"] = pending_confirmation_reply_context(
                    session,
                    pending_confirmation,
                )
            missed_dose_prompt = None if pending_confirmation is not None else active_missed_dose_conversation_alert(session)
            if missed_dose_prompt is not None:
                prompt_message = ensure_chat_message_for_conversation_alert(session, missed_dose_prompt)
                understanding = build_rule_based_missed_dose_reply_understanding(normalized_message)
                annotate_missed_dose_reply(
                    session,
                    missed_dose_prompt,
                    patient_reply=normalized_message,
                    understanding=understanding,
                    prompt_message=prompt_message,
                )
                request_metadata.update(missed_dose_reply_request_metadata(missed_dose_prompt, understanding))
                acknowledge_notification_record(session, missed_dose_prompt.id, resume_conversation_clock=False, commit=False)
            request_notification = create_system_event_request(
                session,
                event_type,
                normalized_message,
                clock.current_time,
                metadata=request_metadata or None,
            )
            notification_id = request_notification.id
            session.commit()
        start_daemon_thread(
            name=f"system-event-request-{notification_id}",
            target=runtime.system_event_worker,
            args=(event_type, normalized_message, notification_id),
        )
        return runtime.templates.TemplateResponse(request, "partials/chat.html", build_chat_context(request, session))

    @router.post("/chat/contract-response")
    def contract_response_chat(
        request: Request,
        conversation_id: str = Form(...),
        source_chat_request_id: str = Form(...),
        response_kind: str = Form(...),
        selection_message: str = Form(""),
        input_labels: list[str] = Form(default=[]),
        input_values: list[str] = Form(default=[]),
        session: Session = Depends(get_session),
    ):
        if response_kind == "selection":
            message = selection_message.strip()
            requested_return_type = "selection_box"
            if not message:
                raise HTTPException(status_code=422, detail="selection_message_required")
        elif response_kind == "input":
            try:
                message = input_box_message(input_labels, input_values)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            requested_return_type = "input_box"
        else:
            raise HTTPException(status_code=422, detail="contract_response_kind_invalid")

        runtime = get_runtime()
        with runtime.write_lock:
            patient_id = get_settings().patient_id
            try:
                pending_response = pending_response_for_submission(
                    session,
                    patient_id=patient_id,
                    conversation_id=conversation_id,
                    requested_return_type=requested_return_type,
                    message=message,
                    source_chat_request_id=source_chat_request_id,
                )
            except PendingChatResponseError as exc:
                raise HTTPException(status_code=409, detail=exc.code) from exc
            clock = ensure_clock(session)
            request_notification = create_system_event_request(
                session,
                "multiturn_chat",
                message,
                clock.current_time,
                metadata={
                    "contract_version": "v1.2",
                    "agent_conversation_id": conversation_id,
                    "source_chat_request_id": source_chat_request_id,
                    "requested_return_type": requested_return_type,
                },
            )
            request_metadata = parse_json_object(request_notification.metadata_json)
            response_chat_message = session.get(
                ChatMessage,
                int(request_metadata["chat_message_id"]),
            )
            if response_chat_message is None:
                raise HTTPException(status_code=500, detail="contract_response_message_missing")
            mark_pending_response_answered(
                pending_response,
                response_message_id=response_chat_message.public_id,
                response_request_id=str(request_metadata["ai_request_id"]),
            )
            notification_id = request_notification.id
            session.commit()
        start_daemon_thread(
            name=f"system-event-request-{notification_id}",
            target=runtime.system_event_worker,
            args=("multiturn_chat", message, notification_id),
        )
        return runtime.templates.TemplateResponse(
            request,
            "partials/chat.html",
            build_chat_context(request, session),
        )

    @router.post("/chat/mutation-confirmation")
    def mutation_confirmation_chat(
        request: Request,
        confirmation_id: str = Form(...),
        action: str = Form(...),
        session: Session = Depends(get_session),
    ):
        runtime = get_runtime()
        should_start_worker = False
        with runtime.write_lock:
            patient_id = get_settings().patient_id
            recover_expired_confirmations(session, patient_id)
            row = session.scalar(
                select(MutationConfirmation).where(
                    MutationConfirmation.public_id == confirmation_id,
                    MutationConfirmation.patient_id == patient_id,
                )
            )
            if row is None:
                raise HTTPException(status_code=404, detail="mutation_confirmation_not_found")
            if action not in {"confirm", "cancel"}:
                raise HTTPException(status_code=422, detail="mutation_confirmation_invalid_resolution")
            if row.status == PENDING:
                try:
                    begin_mutation_resolution(
                        session,
                        confirmation_id,
                        action,
                        patient_id=patient_id,
                    )
                except ValueError as exc:
                    session.rollback()
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
                should_start_worker = True
            session.commit()
        if should_start_worker:
            start_daemon_thread(
                name=f"mutation-confirmation-{confirmation_id}",
                target=runtime.mutation_confirmation_worker,
                args=(confirmation_id, action),
            )
        return runtime.templates.TemplateResponse(
            request,
            "partials/chat.html",
            build_chat_context(request, session),
        )

    @router.post("/chat/food-select")
    def food_select(
        request: Request,
        chat_message_id: int = Form(...),
        food_ref_id: str = Form(...),
        session: Session = Depends(get_session),
    ):
        runtime = get_runtime()
        with runtime.write_lock:
            message, metadata, fs = food_selection_for_stage(session, chat_message_id, "awaiting_food_choice")
            candidates = fs.get("candidates") if isinstance(fs.get("candidates"), list) else []
            selected = next((c for c in candidates if isinstance(c, dict) and c.get("food_ref_id") == food_ref_id), None)
            if selected is None:
                raise HTTPException(status_code=400, detail="candidate_not_found")
            fs["selected_food"] = selected
            fs["stage"] = "awaiting_grams"
            metadata["food_selection"] = fs
            message.metadata_json = dump_json(metadata)
            session.commit()
        return runtime.templates.TemplateResponse(request, "partials/chat.html", build_chat_context(request, session))

    @router.post("/chat/food-grams")
    def food_grams(
        request: Request,
        chat_message_id: int = Form(...),
        portion_g: float = Form(...),
        meal_type: str = Form(...),
        session: Session = Depends(get_session),
    ):
        runtime = get_runtime()
        if not math.isfinite(portion_g) or not 1 <= portion_g <= 2000:
            raise HTTPException(status_code=422, detail="invalid_food_portion")
        if meal_type not in MEAL_TYPE_LABELS:
            raise HTTPException(status_code=422, detail="invalid_meal_type")
        with runtime.write_lock:
            message, metadata, fs = food_selection_for_stage(session, chat_message_id, "awaiting_grams")
            if not isinstance(fs.get("selected_food"), dict):
                raise HTTPException(status_code=400, detail="no_food_selection")
            selected = fs["selected_food"]
            serving = float(selected.get("serving_size") or 100)
            ratio = portion_g / serving if serving else 1.0
            nutrients = selected.get("nutrients") if isinstance(selected.get("nutrients"), dict) else {}
            selected["scaled_nutrients"] = scale_nutrients(nutrients, ratio)
            fs["selected_food"] = selected
            fs["portion_g"] = portion_g
            fs["meal_type"] = meal_type
            fs["stage"] = "awaiting_confirm"
            metadata["food_selection"] = fs
            message.metadata_json = dump_json(metadata)
            session.commit()
        return runtime.templates.TemplateResponse(request, "partials/chat.html", build_chat_context(request, session))

    @router.post("/chat/food-confirm")
    def food_confirm(
        request: Request,
        chat_message_id: int = Form(...),
        session: Session = Depends(get_session),
    ):
        runtime = get_runtime()
        with runtime.write_lock:
            message, metadata, fs = food_selection_for_stage(session, chat_message_id, "awaiting_confirm")
            if not isinstance(fs.get("selected_food"), dict):
                raise HTTPException(status_code=400, detail="no_food_selection")
            selected = fs["selected_food"]
            meal_type = str(fs.get("meal_type") or "lunch")
            portion_g = float(fs.get("portion_g") or selected.get("serving_size") or 100)
            scaled = selected.get("scaled_nutrients") if isinstance(selected.get("scaled_nutrients"), dict) else selected.get("nutrients", {})
            korean_nutrients = english_to_korean_nutrients(scaled)

            confirmed_foods = fs.get("confirmed_foods") if isinstance(fs.get("confirmed_foods"), list) else []
            confirmed_foods.append(
                {
                    "food_ref_id": selected.get("food_ref_id", ""),
                    "food_name": selected["food_name"],
                    "portion": f"{int(portion_g)}g",
                    "nutrients": korean_nutrients,
                    "meal_type": meal_type,
                }
            )
            fs["confirmed_foods"] = confirmed_foods

            foods_queue = fs.get("foods_queue") if isinstance(fs.get("foods_queue"), list) else []

            if foods_queue:
                # 큐에 남은 음식이 있으면 다음 음식 카드 선택으로 이동
                next_search = foods_queue.pop(0)
                fs["foods_queue"] = foods_queue
                fs["stage"] = "awaiting_food_choice"
                fs["query"] = next_search.get("query", "")
                fs["candidates"] = next_search.get("candidates", [])
                fs["selected_food"] = None
                fs["portion_g"] = None
                fs["default_meal_type"] = next_search.get("meal_type", "") or fs.get("default_meal_type", "")
                fs["meal_type"] = None
            else:
                # The completed selection becomes a proposal; DB writes wait for common confirmation.
                origin_notification_id = fs.get("origin_request_notification_id")
                try:
                    origin_notification_id = int(origin_notification_id)
                except (TypeError, ValueError) as exc:
                    raise HTTPException(status_code=409, detail="food_selection_origin_missing") from exc
                origin_notification = session.get(Notification, origin_notification_id)
                if origin_notification is None:
                    raise HTTPException(status_code=409, detail="food_selection_origin_missing")
                origin_metadata = parse_json_object(origin_notification.metadata_json)
                clock = ensure_clock(session)
                meal_label = MEAL_TYPE_LABELS.get(meal_type, meal_type)
                names = ", ".join(f["food_name"] for f in confirmed_foods)
                prepared = prepare_mutation_confirmation(
                    session,
                    MutationConfirmationPrepareRequest(
                        patient_id=get_settings().patient_id,
                        action_name=CREATE_NUTRITION_MEAL_RECORD,
                        tool_call_id=f"food-confirm:{message.id}",
                        arguments={
                            "patient_id": get_settings().patient_id,
                            "foods": confirmed_foods,
                            "meal_type": meal_type,
                        },
                        trace_id=str(fs.get("origin_trace_id") or origin_metadata.get("trace_id") or f"food-confirm:{message.id}"),
                        source_event_type="nutrition_management_agent",
                        request_context={
                            "message": origin_metadata.get("request_message") or f"{meal_label} 식사로 {names} 기록",
                            "event_type": origin_metadata.get("event_type") or "multiturn_chat",
                            "current_time": clock.current_time.isoformat(),
                            "callback_context": {
                                "notification_id": origin_notification.id,
                                "conversation_id": origin_metadata.get("agent_conversation_id") or "",
                            },
                        },
                    ),
                )
                if not prepared.confirmation_required or not prepared.confirmation_id:
                    raise HTTPException(
                        status_code=409,
                        detail=f"nutrition_meal_confirmation_{prepared.status}",
                    )
                confirmation = session.scalar(select(MutationConfirmation).where(MutationConfirmation.public_id == prepared.confirmation_id))
                if confirmation is None:
                    raise HTTPException(status_code=409, detail="mutation_confirmation_not_found")

                fs["stage"] = "done"
                confirmation_message = add_chat_message(
                    session,
                    role="assistant",
                    content="식사 기록 전에 아래 내용을 확인해주세요.",
                    sender_type="assistant",
                    category="mutation_confirmation",
                    metadata={"mutation_confirmation": confirmation_card_payload(confirmation)},
                )
                attach_confirmation_chat_message(session, confirmation, confirmation_message)
                origin_metadata["status"] = "needs_confirmation"
                origin_notification.metadata_json = dump_json(origin_metadata)

            metadata["food_selection"] = fs
            message.metadata_json = dump_json(metadata)
            session.commit()
        return runtime.templates.TemplateResponse(request, "partials/chat.html", build_chat_context(request, session))

    return router
