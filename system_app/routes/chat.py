from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import and_, desc, or_, select
from sqlalchemy.orm import Session

from shared.json_utils import dump_json, parse_json_object
from system_app.db import get_session
from system_app.models import ChatMessage, Notification
from system_app.runtime import SystemRuntime
from system_app.services.background_threads import start_daemon_thread
from system_app.services.clock_service import ensure_clock
from system_app.services.conversation_service import acknowledge_notification as acknowledge_notification_record
from system_app.services.conversation_service import handle_policy_confirmation_message
from system_app.services.dashboard_view import build_dashboard_context
from system_app.services.food_search_service import english_to_korean_nutrients, scale_nutrients
from system_app.services.missed_dose_flag_service import is_active_missed_dose_flag_for_event
from system_app.services.missed_dose_reply_understanding import (
    annotate_missed_dose_reply,
    build_rule_based_missed_dose_reply_understanding,
    missed_dose_reply_request_metadata,
)
from system_app.services.nutrition_service import MEAL_TYPE_LABELS, record_meal
from system_app.services.side_effect_reminder_safety import create_side_effect_reminder_safety_prompt, handle_side_effect_reminder_safety_reply
from system_app.services.system_request_service import create_system_event_request
from system_app.services.timeline_service import add_chat_message, ensure_chat_message_for_conversation_alert

STALE_FOOD_SELECTION_DETAIL = "stale_food_selection"


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
    if message is None:
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
            handle_policy_confirmation_message(session, message, notification_id)
        return runtime.templates.TemplateResponse(request, "partials/chat.html", build_dashboard_context(request, session))

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
            message = session.get(ChatMessage, chat_message_id)
            if message is None:
                raise HTTPException(status_code=404, detail="chat_message_not_found")
            metadata = parse_json_object(message.metadata_json)
            ae_payload = metadata.get("ae_pro_ctcae")
            if not isinstance(ae_payload, dict):
                raise HTTPException(status_code=400, detail="chat_message_has_no_ae_prompt")
            questions = ae_payload.get("questions") if isinstance(ae_payload.get("questions"), list) else []
            responses = ae_payload.get("responses") if isinstance(ae_payload.get("responses"), list) else []
            if question_index is None:
                answered = {row.get("question_index") for row in responses if isinstance(row, dict)}
                question_index = next((index for index in range(len(questions)) if index not in answered), 0)
            question_text = ""
            if 0 <= question_index < len(questions) and isinstance(questions[question_index], dict):
                question_text = str(questions[question_index].get("question") or "")
            responses = [row for row in responses if not (isinstance(row, dict) and row.get("question_index") == question_index)]
            responses.append(
                {
                    "question_index": question_index,
                    "question": question_text,
                    "response_text": response_text.strip(),
                }
            )
            ae_payload["responses"] = responses
            metadata["ae_pro_ctcae"] = ae_payload
            message.metadata_json = dump_json(metadata)
            add_chat_message(
                session,
                role="user",
                content=f"{question_index + 1}번 문항: {response_text.strip()}",
                sender_type="patient",
                category="ae_response",
                metadata={"ae_response_to": chat_message_id, "question_index": question_index},
            )
            if questions and len(responses) >= len(questions):
                add_chat_message(
                    session,
                    role="assistant",
                    content="PRO-CTCAE 문항 응답을 기록했습니다.",
                    sender_type="assistant",
                    category="ae_response",
                    metadata={"ae_response_to": chat_message_id},
                )
                create_side_effect_reminder_safety_prompt(session, chat_message_id)
            session.commit()
        return runtime.templates.TemplateResponse(request, "partials/chat.html", build_dashboard_context(request, session))

    @router.post("/chat/side-effect-reminder-safety")
    def side_effect_reminder_safety_chat(
        request: Request,
        action: str = Form(...),
        notification_id: int | None = Form(None),
        session: Session = Depends(get_session),
    ):
        runtime = get_runtime()
        with runtime.write_lock:
            handle_side_effect_reminder_safety_reply(session, notification_id, action)
            session.commit()
        return runtime.templates.TemplateResponse(request, "partials/chat.html", build_dashboard_context(request, session))

    @router.post("/chat/system")
    def system_chat(
        request: Request,
        event_type: str = Form("multiturn_chat"),
        message: str = Form(...),
        session: Session = Depends(get_session),
    ):
        runtime = get_runtime()
        with runtime.write_lock:
            clock = ensure_clock(session)
            missed_dose_prompt = active_missed_dose_conversation_alert(session)
            request_metadata = None
            if missed_dose_prompt is not None:
                prompt_message = ensure_chat_message_for_conversation_alert(session, missed_dose_prompt)
                understanding = build_rule_based_missed_dose_reply_understanding(message)
                annotate_missed_dose_reply(
                    session,
                    missed_dose_prompt,
                    patient_reply=message,
                    understanding=understanding,
                    prompt_message=prompt_message,
                )
                request_metadata = missed_dose_reply_request_metadata(missed_dose_prompt, understanding)
                acknowledge_notification_record(session, missed_dose_prompt.id, resume_conversation_clock=False, commit=False)
            request_notification = create_system_event_request(session, event_type, message, clock.current_time, metadata=request_metadata)
            notification_id = request_notification.id
            session.commit()
        start_daemon_thread(
            name=f"system-event-request-{notification_id}",
            target=runtime.system_event_worker,
            args=(event_type, message, notification_id),
        )
        return runtime.templates.TemplateResponse(request, "partials/chat.html", build_dashboard_context(request, session))

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
        return runtime.templates.TemplateResponse(request, "partials/chat.html", build_dashboard_context(request, session))

    @router.post("/chat/food-grams")
    def food_grams(
        request: Request,
        chat_message_id: int = Form(...),
        portion_g: float = Form(...),
        meal_type: str = Form(...),
        session: Session = Depends(get_session),
    ):
        runtime = get_runtime()
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
        return runtime.templates.TemplateResponse(request, "partials/chat.html", build_dashboard_context(request, session))

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
            confirmed_foods.append({
                "food_ref_id": selected.get("food_ref_id", ""),
                "food_name": selected["food_name"],
                "portion": f"{int(portion_g)}g",
                "nutrients": korean_nutrients,
                "meal_type": meal_type,
            })
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
                # 모든 음식 처리 완료 → 한꺼번에 기록
                record_meal(
                    session,
                    foods=confirmed_foods,
                    meal_type=meal_type,
                )
                fs["stage"] = "done"
                meal_label = MEAL_TYPE_LABELS.get(meal_type, meal_type)
                names = ", ".join(f["food_name"] for f in confirmed_foods)
                add_chat_message(
                    session,
                    role="assistant",
                    content=f"{meal_label} 식사를 기록했습니다: {names}",
                    sender_type="assistant",
                    category="nutrition",
                )

            metadata["food_selection"] = fs
            message.metadata_json = dump_json(metadata)
            session.commit()
        return runtime.templates.TemplateResponse(request, "partials/chat.html", build_dashboard_context(request, session))

    return router
