# Worktree Review Map

현재 워킹트리는 여러 단계의 작업이 같은 브랜치에 함께 남아 있습니다. 리뷰할 때는 아래 세 묶음으로 나누어 보는 것을 권장합니다.

## Review Set A: Nutrition + Medication Integration

영양 입력, 식사 시나리오, 환자별 조회 스코프, UI 통합을 위한 변경입니다.

Primary files:

- `system_app/routes/nutrition.py`
- `system_app/services/nutrition_service.py`
- `system_app/templates/partials/nutrition.html`
- `system_app/templates/partials/overview_summary.html`
- `system_app/models.py`
- `system_app/migrations.py`
- `system_app/routes/__init__.py`
- `system_app/routes/pages.py`
- `system_app/services/dashboard_view.py`
- `system_app/static/styles.css`
- `system_app/templates/index.html`
- `tests/test_nutrition_integration.py`
- `tests/test_migrations_health.py`
- `tests/test_ui_pages.py`

Review focus:

- 모든 nutrition 조회가 `patient_id`로 스코프되는지
- PHR 등록 전후 UI gate가 유지되는지
- 영양/복약 요약이 서로 다른 환자 데이터를 섞지 않는지
- migration이 기존 DB에 안전하게 적용되는지

## Review Set B: Agent Async + Side Effect Flow

복약/부작용/PRO-CTCAE/정책 confirmation이 async worker와 MCP tool 흐름으로 이어지도록 한 변경입니다.

Primary files:

- `agent_app/ae_pro_ctcae.py`
- `agent_app/jobs/worker.py`
- `agent_app/llm/prompts.py`
- `agent_app/providers/rule_based.py`
- `agent_app/tools/catalog.py`
- `agent_app/tools/mcp_server.py`
- `agent_app/tools/permissions.py`
- `agent_app/tools/protocol.py`
- `agent_app/tools/results.py`
- `phr_app/services.py`
- `shared/schemas.py`
- `system_app/services/workers.py`
- `system_app/services/system_request_service.py`
- `system_app/services/agent_response_service.py`
- `system_app/static/notifications.js`
- `system_app/static/notifications/panels.js`
- `tests/test_agent_async_callbacks.py`
- `tests/test_agent_app_langgraph_native.py`
- `tests/test_ae_pro_ctcae_fallback.py`
- `tests/test_system_agent_errors.py`

Review focus:

- `chat_continuation`이 async-first로 접수되고 callback으로 최종 반영되는지
- side-effect lookup 이후 PRO-CTCAE 문항 생성이 중복 tool call 없이 실행되는지
- policy tool은 confirmation 전 실제 정책을 변경하지 않는지
- worker 실패가 queue/dead/failure callback 경로로 남는지

## Review Set C: P0 Production Readiness

P0/P1 readiness hardening 변경입니다.

Primary files:

- `data/evals/agent_production_readiness_cases.json`
- `docs/PRODUCTION_READINESS.md`
- `docs/ARCHITECTURE.md`
- `docs/AGENT_APP_FEATURES.md`
- `docs/WORKTREE_REVIEW_MAP.md`
- `shared/redaction.py`
- `shared/readiness_budget.py`
- `shared/settings.py`
- `agent_app/jobs/readiness.py`
- `agent_app/main.py`
- `agent_app/jobs/tasks.py`
- `agent_app/trace_logging.py`
- `system_app/services/trace_logging.py`
- `system_app/services/failure_copy.py`
- `system_app/services/phr_client.py`
- `phr_app/main.py`
- `phr_app/trace_logging.py`
- `.env.phr_app.example`
- `.env.agent_app.example`
- `.env.system_app.example`
- `tools/p1_load_probe.py`
- `tests/test_agent_ops_readiness.py`
- `tests/test_production_eval_dataset.py`
- `tests/test_redaction.py`
- `tests/test_readiness_budget.py`
- `tests/test_failure_copy.py`
- `tests/test_phr_client_failure_copy.py`
- `tests/test_phr_app.py`

Review focus:

- `/agent/ops/readiness` alert severity가 운영자가 바로 판단할 수 있는지
- trace log에서 `patient_id`, `phr_patient_key`, 증상/식사/복약 자유문장이 원문으로 남지 않는지
- PHR incident containment에서 `PHR_READ_ONLY=true`가 register/update만 막고 read/assess는 유지하는지
- synthetic eval dataset이 nutrition, medication, PHR, async, MCP, privacy, governance, incident coverage를 유지하는지
- P1 load/cost budget이 환경값과 probe 결과로 판단 가능한지
- 실패 유형별 UX copy가 기록 보존, 재시도, 안전 안내를 포함하는지

## Overlap Files

아래 파일은 여러 review set이 같이 지나갑니다. 리뷰 시 diff를 기능 단위로 나누어 확인해야 합니다.

- `agent_app/main.py`: async endpoint + readiness endpoint
- `agent_app/jobs/tasks.py`: async observability + redacted payload metadata
- `phr_app/main.py`: PHR API + read-only containment
- `shared/settings.py`: service settings + `PHR_READ_ONLY`
- `tests/test_phr_app.py`: 기존 PHR behavior + read-only containment
- `docs/ARCHITECTURE.md`: async-first architecture + ops readiness
- `docs/AGENT_APP_FEATURES.md`: API contract + readiness endpoint
- `tests/test_migrations_health.py`: nutrition migrations + health/readiness expectations

## Suggested Review Order

1. Review Set A first, because it changes the user-visible nutrition/medication domain model.
2. Review Set B second, because it changes async agent behavior and callbacks.
3. Review Set C last, because it hardens and documents the production controls around A and B.

Recommended validation:

```powershell
python -m pytest -q -p no:cacheprovider
```

P0-focused validation:

```powershell
python -m pytest -q -p no:cacheprovider tests/test_production_eval_dataset.py tests/test_redaction.py tests/test_agent_ops_readiness.py tests/test_phr_app.py tests/test_agent_async_callbacks.py tests/test_migrations_health.py
```

## Improved Summary Wording

Use this wording instead of the ambiguous dirty-worktree note:

> 현재 워킹트리는 세 리뷰 단위가 함께 있습니다: 영양+복약 UI/데이터 통합, async agent/부작용 흐름, P0/P1 production readiness hardening. 각 묶음은 `docs/WORKTREE_REVIEW_MAP.md` 기준으로 나눠 검토하면 됩니다. 기존 변경은 되돌리지 않았고, readiness 작업은 readiness API, eval dataset, trace redaction, PHR read-only containment, load/cost budget probe, failure UX copy, runbook/architecture 문서에 한정해 보강했습니다.
