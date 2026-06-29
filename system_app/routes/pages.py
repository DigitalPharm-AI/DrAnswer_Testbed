from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from system_app.db import get_session
from system_app.models import Notification
from system_app.runtime import SystemRuntime
from system_app.services.agent_jobs import create_agent_job
from system_app.services.clock_service import ensure_clock
from system_app.services.dashboard_view import build_dashboard_context, resolve_agent_model_config, serialize_notification_feed, serialize_notifications
from system_app.services.dose_event_service import build_daily_pattern
from system_app.services.governance_change_log import append_governance_change
from system_app.services.observability_actions import (
    post_agent_task_action,
    promote_observability_eval_case,
    update_eval_backlog_case_status,
)
from system_app.services.observability_view import build_observability_context
from system_app.services.nutrition_service import run_nutrition_scenario
from system_app.services.readiness_controls import record_model_fallback_drill, record_online_eval_scan
from system_app.services.release_readiness import write_release_readiness_report
from system_app.services.trace_retention import run_trace_retention_cleanup
from system_app.services.trace_replay import build_trace_replay_detail, write_trace_replay_artifact


def create_pages_router(get_runtime: Callable[[], SystemRuntime]) -> APIRouter:
    router = APIRouter()

    async def logs_template_context(
        request: Request,
        session: Session,
        action_result: dict | None = None,
        observability: dict | None = None,
    ) -> dict:
        context = {
            "request": request,
            "observability": observability or await build_observability_context(session),
            "logs_action_result": action_result,
        }
        return context

    @router.get("/", response_class=HTMLResponse)
    async def index(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
        runtime = get_runtime()
        return runtime.templates.TemplateResponse(
            request,
            "index.html",
            build_dashboard_context(request, session, agent_model_config=await resolve_agent_model_config(runtime.agent_client)),
        )

    @router.get("/partials/time-bar", response_class=HTMLResponse)
    async def time_bar_partial(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
        return get_runtime().templates.TemplateResponse(request, "partials/time_bar.html", build_dashboard_context(request, session))

    @router.get("/partials/timeline", response_class=HTMLResponse)
    async def timeline_partial(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
        return get_runtime().templates.TemplateResponse(request, "partials/timeline.html", build_dashboard_context(request, session))

    @router.get("/partials/notifications", response_class=HTMLResponse)
    async def notifications_partial(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
        return get_runtime().templates.TemplateResponse(request, "partials/notifications.html", build_dashboard_context(request, session))

    @router.get("/partials/active-policies", response_class=HTMLResponse)
    async def active_policies_partial(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
        return get_runtime().templates.TemplateResponse(request, "partials/active_policies.html", build_dashboard_context(request, session))

    @router.get("/partials/logs", response_class=HTMLResponse)
    async def logs_partial(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
        return get_runtime().templates.TemplateResponse(request, "partials/logs.html", await logs_template_context(request, session))

    @router.post("/logs/agent-tasks/actions", response_class=HTMLResponse)
    async def logs_agent_task_action(
        request: Request,
        request_id: str = Form(...),
        action: str = Form(...),
        reason: str = Form(""),
        session: Session = Depends(get_session),
    ) -> HTMLResponse:
        action_result = await post_agent_task_action(request_id, action, reason)
        return get_runtime().templates.TemplateResponse(
            request,
            "partials/logs.html",
            await logs_template_context(request, session, action_result),
        )

    @router.post("/logs/containment/release", response_class=HTMLResponse)
    async def logs_containment_release(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
        observability = await build_observability_context(session)
        agent_tasks = observability.get("agent_tasks") if isinstance(observability.get("agent_tasks"), dict) else {}
        dead_items = agent_tasks.get("dead_items") if isinstance(agent_tasks.get("dead_items"), list) else []
        if not dead_items:
            action_result = {
                "success": True,
                "action": "containment_release",
                "message": "containment release: 해제할 dead task가 없습니다.",
            }
        else:
            results = [
                await post_agent_task_action(
                    str(task.get("request_id") or ""),
                    "dismiss",
                    "bulk containment release from LOGS",
                )
                for task in dead_items
                if task.get("request_id")
            ]
            success_count = sum(1 for result in results if result.get("success"))
            action_result = {
                "success": success_count == len(results) and bool(results),
                "action": "containment_release",
                "request_id": f"{success_count}/{len(results)}",
                "message": f"containment release submitted: {success_count}/{len(results)} dead tasks dismissed",
            }
        return get_runtime().templates.TemplateResponse(
            request,
            "partials/logs.html",
            await logs_template_context(request, session, action_result),
        )

    @router.post("/logs/async-submit", response_class=HTMLResponse)
    async def logs_async_submit(
        request: Request,
        kind: str = Form(...),
        session: Session = Depends(get_session),
    ) -> HTMLResponse:
        runtime = get_runtime()
        safe_kind = str(kind or "").strip()
        with runtime.write_lock:
            if safe_kind == "nutrition":
                result = run_nutrition_scenario(session, "high_sodium_lunch")
                session.commit()
                action_result = {
                    "success": True,
                    "action": "nutrition_async_submit",
                    "request_id": result.get("scenario_key"),
                    "message": "영양 async 제출 시나리오를 기록했습니다.",
                }
            elif safe_kind == "medication":
                clock = ensure_clock(session)
                pattern = build_daily_pattern(session, clock.current_time.date())
                if pattern is None:
                    session.commit()
                    action_result = {
                        "success": False,
                        "action": "medication_async_submit",
                        "message": "복약 async 제출 payload를 만들 수 없습니다.",
                    }
                else:
                    job = create_agent_job(session, "daily_pattern", pattern)
                    session.commit()
                    action_result = {
                        "success": True,
                        "action": "medication_async_submit",
                        "request_id": f"job-{job.id}",
                        "message": "복약 async 제출 job을 생성했습니다.",
                    }
            else:
                action_result = {
                    "success": False,
                    "action": "async_submit",
                    "message": f"지원하지 않는 async 제출 유형입니다: {safe_kind or '-'}",
                }
        return runtime.templates.TemplateResponse(
            request,
            "partials/logs.html",
            await logs_template_context(request, session, action_result),
        )

    @router.post("/logs/eval-backlog/promote", response_class=HTMLResponse)
    async def logs_eval_backlog_promote(
        request: Request,
        source_type: str = Form(...),
        source_id: str = Form(""),
        trace_id: str = Form(""),
        reason: str = Form(""),
        session: Session = Depends(get_session),
    ) -> HTMLResponse:
        action_result = promote_observability_eval_case(
            session,
            source_type=source_type,
            source_id=source_id,
            trace_id=trace_id,
            reason=reason,
        )
        return get_runtime().templates.TemplateResponse(
            request,
            "partials/logs.html",
            await logs_template_context(request, session, action_result),
        )

    @router.post("/logs/eval-backlog/status", response_class=HTMLResponse)
    async def logs_eval_backlog_status(
        request: Request,
        case_id: str = Form(...),
        status: str = Form(...),
        reason: str = Form(""),
        session: Session = Depends(get_session),
    ) -> HTMLResponse:
        action_result = update_eval_backlog_case_status(case_id, status, reason)
        return get_runtime().templates.TemplateResponse(
            request,
            "partials/logs.html",
            await logs_template_context(request, session, action_result),
        )

    @router.post("/logs/governance/change-log", response_class=HTMLResponse)
    async def logs_governance_change_log(
        request: Request,
        change_type: str = Form(...),
        target: str = Form(...),
        summary: str = Form(...),
        owner: str = Form(""),
        rollback: str = Form(""),
        evidence: str = Form(""),
        session: Session = Depends(get_session),
    ) -> HTMLResponse:
        action_result = append_governance_change(
            change_type=change_type,
            target=target,
            summary=summary,
            owner=owner,
            rollback=rollback,
            evidence=evidence,
        )
        return get_runtime().templates.TemplateResponse(
            request,
            "partials/logs.html",
            await logs_template_context(request, session, action_result),
        )

    @router.post("/logs/online-eval/scan", response_class=HTMLResponse)
    async def logs_online_eval_scan(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
        action_result = record_online_eval_scan(session)
        return get_runtime().templates.TemplateResponse(
            request,
            "partials/logs.html",
            await logs_template_context(request, session, action_result),
        )

    @router.post("/logs/model-config/rollback", response_class=HTMLResponse)
    async def logs_model_config_rollback(
        request: Request,
        model_tier: str = Form(...),
        reason: str = Form(""),
        session: Session = Depends(get_session),
    ) -> HTMLResponse:
        runtime = get_runtime()
        try:
            config = await runtime.agent_client.set_model_tier(model_tier)
        except Exception as exc:  # pragma: no cover - network dependent
            action_result = {
                "success": False,
                "action": "model_config_rollback",
                "model_tier": model_tier,
                "message": f"model config rollback failed: {type(exc).__name__}",
            }
        else:
            change_result = append_governance_change(
                change_type="model",
                target=f"model_tier:{config.model_tier}",
                summary=reason or f"LOGS rollback selected model tier {config.model_tier}",
                owner="ai-ops-owner",
                rollback="Restore the previous model tier after eval and canary checks pass.",
                evidence=f"agent /agent/model-config updated to {config.model_id}",
            )
            action_result = {
                "success": bool(change_result.get("success")),
                "action": "model_config_rollback",
                "model_tier": config.model_tier,
                "entry_id": change_result.get("entry_id"),
                "message": f"model config rolled back to {config.model_tier}",
            }
        return runtime.templates.TemplateResponse(
            request,
            "partials/logs.html",
            await logs_template_context(request, session, action_result),
        )

    @router.post("/logs/traces/replay-artifact", response_class=HTMLResponse)
    async def logs_trace_replay_artifact(
        request: Request,
        trace_id: str = Form(...),
        session: Session = Depends(get_session),
    ) -> HTMLResponse:
        action_result = write_trace_replay_artifact(session, trace_id)
        return get_runtime().templates.TemplateResponse(
            request,
            "partials/logs.html",
            await logs_template_context(request, session, action_result),
        )

    @router.post("/logs/traces/retention-cleanup", response_class=HTMLResponse)
    async def logs_trace_retention_cleanup(
        request: Request,
        mode: str = Form("dry_run"),
        session: Session = Depends(get_session),
    ) -> HTMLResponse:
        execute = str(mode or "").strip() == "execute"
        runtime = get_runtime()
        if execute:
            with runtime.write_lock:
                action_result = run_trace_retention_cleanup(session, execute=True)
                session.commit()
        else:
            action_result = run_trace_retention_cleanup(session, execute=False)
        return runtime.templates.TemplateResponse(
            request,
            "partials/logs.html",
            await logs_template_context(request, session, action_result),
        )

    @router.post("/logs/traces/replay-to-eval", response_class=HTMLResponse)
    async def logs_trace_replay_to_eval(
        request: Request,
        trace_id: str = Form(...),
        session: Session = Depends(get_session),
    ) -> HTMLResponse:
        artifact_result = write_trace_replay_artifact(session, trace_id)
        if not artifact_result.get("success"):
            action_result = artifact_result
        else:
            promotion_result = promote_observability_eval_case(
                session,
                source_type="trace",
                trace_id=trace_id,
                reason=f"trace replay artifact promoted from LOGS: {artifact_result.get('artifact_path')}",
                artifact_path=str(artifact_result.get("artifact_path") or ""),
            )
            action_result = {
                "success": bool(promotion_result.get("success")),
                "action": "trace_replay_to_eval_backlog",
                "trace_id": trace_id,
                "case_id": promotion_result.get("case_id"),
                "artifact_path": artifact_result.get("artifact_path"),
                "message": (
                    "trace replay artifact promoted to eval backlog"
                    if promotion_result.get("created")
                    else promotion_result.get("message", "trace replay artifact linked")
                ),
            }
        return get_runtime().templates.TemplateResponse(
            request,
            "partials/logs.html",
            await logs_template_context(request, session, action_result),
        )

    @router.post("/logs/model-fallback/drill", response_class=HTMLResponse)
    async def logs_model_fallback_drill(
        request: Request,
        drill_mode: str = Form("rule_based"),
        session: Session = Depends(get_session),
    ) -> HTMLResponse:
        action_result = await record_model_fallback_drill(drill_mode=drill_mode)
        return get_runtime().templates.TemplateResponse(
            request,
            "partials/logs.html",
            await logs_template_context(request, session, action_result),
        )

    @router.post("/logs/release-readiness/report", response_class=HTMLResponse)
    async def logs_release_readiness_report(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
        observability = await build_observability_context(session)
        action_result = write_release_readiness_report(observability)
        return get_runtime().templates.TemplateResponse(
            request,
            "partials/logs.html",
            await logs_template_context(request, session, action_result, observability=observability),
        )

    @router.get("/partials/logs/traces/{trace_id}", response_class=HTMLResponse)
    async def logs_trace_replay_detail(request: Request, trace_id: str, session: Session = Depends(get_session)) -> HTMLResponse:
        detail = build_trace_replay_detail(session, trace_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="agent_trace_not_found")
        return get_runtime().templates.TemplateResponse(
            request,
            "partials/log_trace_detail.html",
            {
                "request": request,
                "trace_detail": detail,
            },
        )

    @router.get("/partials/chat", response_class=HTMLResponse)
    async def chat_partial(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
        return get_runtime().templates.TemplateResponse(request, "partials/chat.html", build_dashboard_context(request, session))

    @router.get("/partials/chat-log", response_class=HTMLResponse)
    async def chat_log_partial(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
        return get_runtime().templates.TemplateResponse(request, "partials/chat_log.html", build_dashboard_context(request, session))

    @router.get("/partials/chat-history", response_class=HTMLResponse)
    async def chat_history_partial(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
        return get_runtime().templates.TemplateResponse(request, "partials/conversation_history.html", build_dashboard_context(request, session))

    @router.get("/partials/system-request-history", response_class=HTMLResponse)
    async def system_request_history_partial(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
        return get_runtime().templates.TemplateResponse(request, "partials/system_request_history.html", build_dashboard_context(request, session))

    @router.get("/api/notifications/feed")
    async def notifications_feed(after_id: int = 0, session: Session = Depends(get_session)) -> dict:
        clock = ensure_clock(session)
        notifications = serialize_notification_feed(session, clock.current_time, after_id=after_id)
        last_seen_id = after_id
        if notifications:
            last_seen_id = max(row["id"] for row in notifications)
        return {"notifications": notifications, "last_seen_id": last_seen_id, "current_time": clock.current_time.isoformat()}

    @router.get("/api/notifications/{notification_id}")
    async def notification_detail(notification_id: int, session: Session = Depends(get_session)) -> dict:
        notification = session.get(Notification, notification_id)
        if notification is None:
            raise HTTPException(status_code=404, detail="notification_not_found")
        return {"notification": serialize_notifications(session, [notification])[0]}

    return router
