# Update Log

## 2026-06-29

### Agent trace privacy redaction coverage

- Expanded `shared.redaction.CLINICAL_TEXT_KEYS` so agent observability logs redact common free-text fields used by tool calls, trace summaries, and nutrition preference flows.
- Newly covered keys include `summary`, `result`, `result_message`, `query`, `object_label`, `input_symptom`, `matched_symptom_term`, `matched_korean_symptom_name`, `description`, `reason`, and `user_input`.
- This reduces the risk that allergy/preference labels, food search queries, symptom terms, or generated answer summaries are written to operational logs as raw text.

### Verification

- `py -m pytest ...` could not run because the Windows `py` launcher is not installed in this environment.
- Ran with the project virtual environment instead:
  - `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests/test_redaction.py tests/test_agent_trace_store.py`
- Result: `6 passed, 15 warnings`.

### Centralized tool argument log sanitization

- Added `shared.redaction.safe_log_arguments()` as the single compact sanitizer for tool-call argument logs.
- Replaced duplicated sanitizer logic in:
  - `agent_app.tool_runtime`
  - `system_app.services.system_request_service`
- The shared sanitizer keeps large payloads compact (`list` count, `dict` keys) while applying the same identifier, secret, and clinical text redaction policy everywhere.
- `patient_id` and `phr_patient_key` now follow the central identifier-hash policy instead of each caller inventing its own presence-only variant.

### Verification

- Ran:
  - `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests/test_redaction.py tests/test_agent_trace_store.py tests/test_agent_app_langgraph_native.py::test_agent_app_multiturn_forces_ae_after_positive_side_effect_lookup tests/test_system_agent_errors.py::test_system_event_worker_submits_chat_to_agent_async_without_write_lock`
- Result: `9 passed, 19 warnings`.
- `ruff` could not run because it is not installed in the current virtual environment.
- Ran syntax/import compilation check instead:
  - `.venv\Scripts\python.exe -m py_compile shared\redaction.py agent_app\tool_runtime.py system_app\services\system_request_service.py tests\test_redaction.py`
- Result: passed.

### Trace timestamp UTC compatibility cleanup

- Added `shared.time_utils.utc_now()` to generate current UTC using `datetime.now(UTC)` while returning naive UTC datetimes for compatibility with the existing SQLAlchemy `DateTime` columns.
- Updated `system_app.models.utcnow()` to use the shared helper so model defaults no longer rely on deprecated `datetime.utcnow()`.
- Updated `system_app.services.agent_trace_store` to use the same helper for trace `created_at`, `completed_at`, and `updated_at` timestamps.
- Added a focused helper test to lock the naive-UTC contract that the current schema expects.

### Verification

- Ran warning-as-error regression test:
  - `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider -W error::DeprecationWarning tests/test_time_utils.py tests/test_agent_trace_store.py`
- Result: `4 passed`.
- Ran syntax/import compilation check:
  - `.venv\Scripts\python.exe -m py_compile shared\time_utils.py system_app\models.py system_app\services\agent_trace_store.py tests\test_time_utils.py`
- Result: passed.

### Agent async ops timestamp cleanup

- Extended the shared UTC timestamp helper usage to the agent async runtime and worker observability path.
- Updated:
  - `agent_app.models`
  - `agent_app.async_tasks`
  - `agent_app.worker_status`
  - `agent_app.ops_readiness`
  - `system_app.services.clock_service`
  - `system_app.services.agent_jobs`
- This aligns async queue timestamps, worker heartbeat timestamps, readiness age calculations, simulation clock pause/resume timestamps, and system-side background job timestamps on the same non-deprecated UTC helper.
- Updated async/ops tests to use `utc_now()` for current-time test setup so warning-as-error regression checks cover the real paths cleanly.

### Verification

- Ran warning-as-error async ops regression test:
  - `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider -W error::DeprecationWarning tests/test_agent_async_callbacks.py tests/test_agent_ops_readiness.py tests/test_time_utils.py`
- Result: `20 passed`.
- Ran syntax/import compilation check:
  - `.venv\Scripts\python.exe -m py_compile agent_app\models.py agent_app\async_tasks.py agent_app\worker_status.py agent_app\ops_readiness.py system_app\services\clock_service.py system_app\services\agent_jobs.py tests\test_agent_async_callbacks.py tests\test_agent_ops_readiness.py`
- Result: passed.
- Confirmed targeted async/ops paths no longer contain `datetime.utcnow()`.

### AI Agent scenario template simplification

- Rebuilt the AI Agent test scenario workbook as a direct-entry template instead of a broad evaluation workbook.
- Reduced the main workflow to three sheets:
  - `작성`: one scenario per row with 10 editable columns.
  - `턴상세`: optional rows for difficult multi-turn flows only.
  - `선택값`: dropdown values and short writing rules.
- Replaced the previous mojibake labels with normal Korean labels and kept only practical dropdowns for status, priority, domain, actor, and result.
- Updated the generated workbook:
  - `outputs/ai_agent_scenario_template/AI_Agent_Test_Scenario_Template_Simple_dr.xlsx`

### Verification

- Regenerated the workbook with the bundled Node.js runtime and `@oai/artifact-tool`.
- Rendered and checked preview images for `작성`, `턴상세`, and `선택값`.
- Ran workbook inspection and formula error scan:
  - `formula_error_scan.ndjson`
- Result: no formula error matches found.

### Nutrition integration timestamp warning cleanup

- Extended the shared `utc_now()` helper into system-side flows reached by nutrition and medication integration tests.
- Updated:
  - `system_app.services.medication_plan_service`
  - `system_app.services.dashboard_view`
- Simulation reset, reminder-policy deactivation, and pending agent-chat stale checks now use the same non-deprecated UTC timestamp helper as the agent async and trace paths.
- Confirmed `datetime.utcnow()` is no longer present in:
  - `system_app/services/nutrition_preference_service.py`
  - `system_app/services/medication_plan_service.py`
  - `system_app/services/dashboard_view.py`

### Verification

- Ran warning-as-error nutrition integration regression test:
  - `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider -W error::DeprecationWarning tests\test_nutrition_integration.py tests\test_time_utils.py`
- Result: `18 passed, 1 warning`.
- The remaining warning is from the installed FastAPI/Starlette test client dependency, not from project timestamp code.
- Ran syntax/import compilation check:
  - `.venv\Scripts\python.exe -m py_compile shared\time_utils.py system_app\services\nutrition_preference_service.py system_app\services\medication_plan_service.py system_app\services\dashboard_view.py tests\test_nutrition_integration.py tests\test_time_utils.py`
- Result: passed.

### Project-wide UTC timestamp cleanup

- Removed the remaining direct `datetime.utcnow()` calls from Python project code and local test/fixture code.
- Routed current UTC generation through `shared.time_utils.utc_now()` for:
  - PHR model defaults.
  - agent error and policy-confirmation trace IDs.
  - conversation reply completion metadata.
  - dose-event clock ticks and missed-dose flag lifecycle timestamps.
  - PHR profile sync timestamps.
  - policy override deactivation timestamps.
  - side-effect reminder safety completion metadata.
  - LOGS data/tool freshness checks and trace-retention cutoff calculation.
  - system background clock worker ticks.
  - UI trace-retention tests and Playwright adherence fixture timestamps.
- Removed unused `datetime` imports that became obsolete after the helper migration.
- Confirmed no direct `datetime.utcnow()` calls remain in Python files. The remaining `utcnow()` function names are SQLAlchemy default wrappers and now delegate to `utc_now()`.

### Verification

- Ran project-wide search:
  - `rg -n "datetime\.utcnow" . -g "*.py"`
- Result: no matches.
- Ran syntax/import compilation check:
  - `.venv\Scripts\python.exe -m py_compile phr_app\models.py system_app\services\agent_error_service.py system_app\services\conversation_service.py system_app\services\dose_event_service.py system_app\services\missed_dose_flag_service.py system_app\services\observability_view.py system_app\services\patient_profile_service.py system_app\services\policy_confirmation_reply.py system_app\services\policy_service.py system_app\services\readiness_controls.py system_app\services\side_effect_reminder_safety.py system_app\services\trace_retention.py system_app\services\workers.py tests\test_ui_pages.py tools\playwright_adherence_pattern_fixture.py`
- Result: passed.
- Ran warning-as-error regression tests across PHR, chat pending state, trace retention, policy/side-effect safety, and missed-dose pattern paths:
  - `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider -W error::DeprecationWarning tests\test_time_utils.py tests\test_phr_app.py tests\test_ui_pages.py::test_chat_log_partial_shows_pending_agent_response_indicator tests\test_ui_pages.py::test_chat_log_partial_keeps_pending_indicator_during_async_continuation tests\test_ui_pages.py::test_logs_trace_retention_cleanup_records_audit_and_redacts_old_spans tests\test_notification_popup.py::test_phr_register_endpoint_saves_issued_key tests\test_notification_popup.py::test_medication_change_after_phr_sync_marks_profile_needs_sync tests\test_notification_popup.py::test_side_effect_safety_reply_keep_and_suppress_update_override tests\test_notification_popup.py::test_policy_confirmation_reply_keeps_success_result_until_confirmed tests\test_adherence_pattern_service.py::test_pattern_d_long_consecutive_missed_creates_single_escalation_stub`
- Result: `15 passed, 1 warning`.
- The remaining warning is from the installed FastAPI/Starlette test client dependency, not from project timestamp code.

### UTC timestamp regression guard

- Added a project-wide regression test to prevent direct `datetime.utcnow` usage from being reintroduced.
- The guard scans Python files under the project root while excluding generated/cache/runtime directories such as `.venv`, `.git`, `__pycache__`, and `outputs`.
- The existing `utc_now()` contract test still verifies that the helper returns naive UTC datetimes compatible with current SQLAlchemy `DateTime` columns.

### Verification

- Ran:
  - `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider -W error::DeprecationWarning tests\test_time_utils.py`
- Result: `2 passed`.
- Ran direct search:
  - `rg -n "datetime\.utcnow" . -g "*.py"`
- Result: no matches.
- Ran syntax/import compilation check:
  - `.venv\Scripts\python.exe -m py_compile tests\test_time_utils.py`
- Result: passed.

### LOGS audit summary redaction

- Hardened the LOGS tool execution view so `AgentDecisionAudit.human_summary` is not rendered as raw clinical text.
- `system_app.services.observability_view` now converts audit summaries into a compact redacted display label with length and stable hash evidence.
- Error messages still use inline secret/contact redaction for operational diagnosis.
- Added a route-level regression test that creates an audit row containing unique allergy/food preference text, renders `/partials/logs`, and verifies the raw clinical text is absent.

### Verification

- Ran:
  - `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\test_ui_pages.py::test_logs_tool_execution_summary_redacts_audit_human_summary tests\test_ui_pages.py::test_logs_partial_renders_trace_drilldown_and_eval_action tests\test_redaction.py`
- Result: `6 passed, 1 warning`.
- The remaining warning is from the installed FastAPI/Starlette test client dependency.
- Ran syntax/import compilation check:
  - `.venv\Scripts\python.exe -m py_compile system_app\services\observability_view.py tests\test_ui_pages.py tests\test_redaction.py`
- Result: passed.

### AgentDecisionAudit persistence redaction

- Hardened the common `record_agent_audit()` persistence path so audit rows do not store raw clinical free text or patient identifiers.
- `structured_payload` is now passed through the central `redact_for_logging()` policy before being serialized.
- `human_summary` is now stored as a compact redacted evidence label with text length and stable hash, instead of the original answer text.
- `error_message` is now passed through inline secret/contact redaction before storage.
- Added redaction coverage for additional agent response text keys such as `advice` and `observations`.
- Kept operational structure such as tool names, status, and non-clinical fields available for audit/debug workflows.

### Verification

- Added a DB-level regression test proving `record_agent_audit()` redacts:
  - `patient_id`
  - clinical message text
  - agent advice text
  - human summary text
  - inline secret values in error messages
- Ran:
  - `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\test_redaction.py tests\test_ui_pages.py::test_logs_tool_execution_summary_redacts_audit_human_summary tests\test_simulation.py::test_daily_pattern_policy_response_creates_confirmation_then_applies_reply`
- Result: `7 passed, 1 warning`.
- The remaining warning is from the installed FastAPI/Starlette test client dependency.
- Ran syntax/import compilation check:
  - `.venv\Scripts\python.exe -m py_compile shared\redaction.py system_app\services\audit_service.py system_app\services\observability_view.py tests\test_redaction.py`
- Result: passed.

### AgentDecisionAudit direct writer consolidation

- Added `create_agent_decision_audit()` as the single sanitized writer for `AgentDecisionAudit` rows.
- Converted direct audit writes in:
  - `system_app.services.agent_async_callback_service`
  - `system_app.services.agent_callback_service`
  - `system_app.services.agent_error_service`
- Async idempotency rows, dose-taken tool callback rows, notification callback rows, and agent error rows now use the same redaction policy as `record_agent_audit()`.
- Confirmed service-layer `AgentDecisionAudit(...)` construction now exists only inside `system_app.services.audit_service`.
- Added regression coverage proving direct/callback audit rows redact raw callback body, patient identifiers, clinical result text, and inline bearer tokens while preserving operational identifiers such as `notification_id`.

### Verification

- Ran:
  - `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\test_notification_popup.py::test_agent_notification_callback_creates_notification_once_with_idempotency tests\test_system_agent_errors.py::test_handle_system_event_creates_agent_error_notification_and_chat tests\test_redaction.py`
- Result: `9 passed, 1 warning`.
- Ran policy audit structure regression:
  - `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\test_simulation.py::test_daily_pattern_policy_response_creates_confirmation_then_applies_reply`
- Result: `1 passed`.
- Confirmed direct constructor consolidation:
  - `rg -n "AgentDecisionAudit\(" system_app\services -g "*.py"`
- Result: only `system_app\services\audit_service.py` remains.
- Ran syntax/import compilation check:
  - `.venv\Scripts\python.exe -m py_compile shared\redaction.py system_app\services\audit_service.py system_app\services\agent_async_callback_service.py system_app\services\agent_callback_service.py system_app\services\agent_error_service.py tests\test_redaction.py`
- Result: passed.

### Eval backlog audit promotion redaction

- Hardened LOGS eval backlog promotion for `source_type=audit`.
- Legacy or raw `AgentDecisionAudit.human_summary` values are now converted with `redacted_clinical_text_label()` before being written into eval backlog cases.
- Audit error messages still use inline secret/contact redaction so operational failure details remain useful without exposing secrets.
- Added regression coverage that creates a legacy raw audit row and verifies the generated backlog artifact does not contain the original clinical text.

### Verification

- Ran:
  - `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\test_redaction.py tests\test_ui_pages.py::test_promote_observability_eval_case_writes_backlog`
- Result: `9 passed, 1 warning`.
- The remaining warning is from the installed FastAPI/Starlette test client dependency.
- Ran syntax/import compilation check:
  - `.venv\Scripts\python.exe -m py_compile system_app\services\observability_actions.py tests\test_redaction.py`
- Result: passed.

### Agent exception and tool error redaction hardening

- Added `safe_exception_summary()` to produce class/error-type + redacted length/hash summaries instead of raw exception text.
- Added central redaction coverage for `error`, `error_message`, and `last_error` fields.
- Replaced direct `logger.exception(...)` worker logs with safe error summaries in agent async workers, system workers, and background thread guards.
- Agent app execution failures now return safe error summaries to app_server, preventing raw provider or validation exception text from being propagated into local jobs and audit rows.
- Async task failures, worker heartbeat `last_error`, async failure callback payloads, local `AgentJob.error_message`, and system-event request failure result messages now use safe summaries.
- MCP tool exception results and downstream API tool errors now preserve short operational error codes while redacting sentence-style/private error details.
- Agent `tool_result_summary()` now redacts raw tool error fallback text before it can become a user-facing agent summary.

### Verification

- Added regression coverage for:
  - `error`, `error_message`, and `last_error` redaction.
  - raw exception summary redaction.
  - tool result summary redaction while preserving operational error codes.
- Ran:
  - `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\test_redaction.py tests\test_system_agent_errors.py::test_agent_worker_marks_invalid_payload_failed_without_stopping tests\test_system_agent_errors.py::test_agent_worker_records_failure_without_stopping tests\test_agent_app_langgraph_native.py::test_tool_result_round_trips_through_mcp_shape`
- Result: `13 passed, 1 warning`.
- The remaining warning is from the installed FastAPI/Starlette test client dependency.
- Ran syntax/import compilation check:
  - `.venv\Scripts\python.exe -m py_compile shared\redaction.py agent_app\main.py agent_app\generation.py agent_app\async_worker.py agent_app\tool_mcp_server.py agent_app\tool_results.py system_app\services\workers.py system_app\services\background_threads.py system_app\services\agent_error_service.py system_app\services\system_request_service.py tests\test_redaction.py`
- Result: passed.
- Confirmed no direct `logger.exception` calls remain in `agent_app`, `system_app`, or `phr_app`.

### MCP error response redaction hardening

- Hardened MCP result serialization so `status=error` tool responses redact `structuredContent.response` before being returned over the MCP wire.
- Hardened MCP JSON-RPC error conversion so raw error `message` and `data.detail` values are not copied into `content` or `structuredContent`.
- Hardened `tool_result_from_mcp_result()` so inbound error results from another MCP executor are sanitized before becoming `ToolCallResult`.
- Hardened agent `tool_calls_payload()` so failed tool result responses are redacted before they become final `AgentResponse.structured_payload.tool_results`.
- Preserved success tool responses unchanged so the agent can still use medication/nutrition data for normal recommendations.
- Added idempotent handling for already-redacted payload dictionaries to avoid double-redaction artifacts.
- Added `detail` to central clinical/error text redaction keys for FastAPI-style error responses.

### Verification

- Added regression coverage for:
  - MCP error `content`, `structuredContent.error`, and `structuredContent.response.detail` redaction.
  - MCP JSON-RPC error message/data redaction.
  - Final agent `tool_results` error response redaction while preserving success response payloads.
- Ran:
  - `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\test_redaction.py tests\test_agent_app_langgraph_native.py::test_tool_result_round_trips_through_mcp_shape tests\test_agent_app_langgraph_native.py::test_agent_app_mcp_direct_call_enforces_default_tool_allowlist tests\test_agent_app_langgraph_native.py::test_agent_app_mcp_allows_nutrition_tools`
- Result: `16 passed, 1 warning`.
- The remaining warning is from the installed FastAPI/Starlette test client dependency.
- Ran syntax/import compilation check:
  - `.venv\Scripts\python.exe -m py_compile shared\redaction.py agent_app\tool_protocol.py agent_app\tool_results.py agent_app\tool_mcp_server.py tests\test_redaction.py`
- Result: passed.
- Confirmed the related tool path no longer contains raw `error=str(...)` or `error=f"{type(exc).__name__}: ..."` patterns.

### Public route error detail hardening

- Added `system_app.routes.public_errors` to keep user-facing route error details separate from raw exception text.
- Medication form validation now returns only allowlisted public error codes such as `required_choice_missing`.
- Nutrition scenario route now returns `unknown_nutrition_scenario` without echoing arbitrary path input.
- PHR registration/update failures now store the safe `failure_copy` body from `copy_for_phr_error()` instead of `str(PhrServiceError)`.
- This prevents upstream PHR exception messages, tokens, or patient/clinical text from being persisted into `SimulationPatientProfile.error_message` and rendered back in the UI.

### Verification

- Added regression coverage for:
  - medication form validation returning a public error code.
  - PHR register failure storing a safe user-facing failure copy and excluding raw upstream error text.
  - nutrition scenario route returning a public error code without echoing the raw scenario key.
- Updated the PHR client failure-copy test double to accept the real `httpx.AsyncClient(..., trust_env=False)` call shape.
- Ran:
  - `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\test_medication_form.py tests\test_nutrition_integration.py::test_nutrition_panel_and_scenario_route_render tests\test_nutrition_integration.py::test_nutrition_scenario_route_returns_public_error_code tests\test_phr_client_failure_copy.py`
- Result: `7 passed, 1 warning`.
- The remaining warning is from the installed FastAPI/Starlette test client dependency.
- Ran syntax/import compilation check:
  - `.venv\Scripts\python.exe -m py_compile system_app\routes\public_errors.py system_app\routes\medications.py system_app\routes\nutrition.py tests\test_medication_form.py tests\test_nutrition_integration.py tests\test_phr_client_failure_copy.py`
- Result: passed.
- Confirmed route/service paths no longer contain `detail=str(exc)`, `mark_phr_sync_failed(session, str(exc))`, or `result_message=str(error)` patterns.

### Health and worker status error redaction hardening

- Hardened agent worker heartbeat serialization so `worker_status_payload()` redacts raw `last_error` values before they are returned by `/agent/async/tasks/status`.
- Hardened system `/health/details` collection so database and workbook probe failures return `safe_exception_summary()` instead of raw exception strings.
- Hardened system agent-async health aggregation so worker `last_error` values received from agent_server are sanitized before being included in app_server health output.
- Hardened `PhrClient` response detail handling so unknown upstream `detail` values are reduced to the public code `phr_upstream_error`.
- Known PHR public codes such as `phr_read_only_mode` and `phr_patient_not_found` remain available for user-safe copy routing.

### Verification

- Added regression coverage for:
  - agent worker heartbeat `last_error` redaction.
  - `/health/details` agent async worker error redaction.
  - PHR client unknown upstream detail sanitization.
- Ran:
  - `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\test_agent_async_callbacks.py::test_agent_worker_heartbeat_status_tracks_running_stopped_and_stale tests\test_migrations_health.py::test_health_details_returns_operational_shape tests\test_migrations_health.py::test_agent_async_health_summarizes_worker_queue tests\test_phr_client_failure_copy.py`
- Result: `5 passed, 1 warning`.
- The remaining warning is from the installed FastAPI/Starlette test client dependency.
- Ran syntax/import compilation check:
  - `.venv\Scripts\python.exe -m py_compile agent_app\worker_status.py system_app\services\health.py system_app\services\phr_client.py tests\test_agent_async_callbacks.py tests\test_migrations_health.py tests\test_phr_client_failure_copy.py`
- Result: passed.
- Confirmed the hardened paths no longer contain the previous raw health/worker/PHR detail patterns.

### Medication form validation hardening

- Hardened medication form submission so date parsing happens inside the public validation path instead of inside the DB write path.
- Added public error codes for `invalid_date_format`, `invalid_date_range`, and `invalid_schedule_time_format`.
- Hardened custom schedule parsing so invalid or empty custom time CSV values return stable public error codes instead of raw parser messages.
- The route now validates schedule time CSV before creating medication plans, while normal preset schedule behavior remains unchanged.

### Verification

- Added regression coverage for:
  - invalid medication date values returning `invalid_date_format` without echoing raw input.
  - end date before start date returning `invalid_date_range`.
  - invalid custom schedule time returning `invalid_schedule_time_format` without echoing raw input.
- Ran:
  - `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\test_medication_form.py tests\test_simulation.py::test_schedule_and_choice_resolution_supports_presets_and_custom_values`
- Result: `7 passed, 1 warning`.
- The remaining warning is from the installed FastAPI/Starlette test client dependency.
- Ran syntax/import compilation check:
  - `.venv\Scripts\python.exe -m py_compile system_app\routes\medications.py system_app\services\simulation_constants.py tests\test_medication_form.py`
- Result: passed.
- Confirmed medication route no longer parses submitted start/end dates directly inside `create_medication_plan(...)` arguments.

### MCP HTTP status error recovery hardening

- Hardened `AgentMcpToolServer.tools_call()` so downstream `httpx.HTTPStatusError` responses are handled before the generic exception path.
- Public FastAPI `detail` string codes such as `invalid_date_format` are now preserved as `ToolCallResult.error`.
- Tool error responses include safe operational fields such as `status_code`, `detail`, and `elapsed_ms` without copying raw response bodies.
- Private or sentence-style upstream detail values are converted to redacted clinical/error labels before being serialized over MCP.
- This lets the agent distinguish recoverable validation failures from provider/runtime failures while keeping MCP error payloads safe.

### Verification

- Added regression coverage for:
  - preserving public HTTP detail codes as tool error codes.
  - redacting private HTTP detail text in MCP error serialization.
  - existing MCP round-trip and nutrition tool allowlist behavior.
- Ran:
  - `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\test_agent_app_langgraph_native.py::test_http_status_tool_error_preserves_public_detail_code_and_redacts_private_detail tests\test_agent_app_langgraph_native.py::test_tool_result_round_trips_through_mcp_shape tests\test_agent_app_langgraph_native.py::test_agent_app_mcp_allows_nutrition_tools tests\test_redaction.py::test_mcp_error_tool_result_sanitizes_response_and_content`
- Result: `4 passed, 1 warning`.
- The remaining warning is from the installed FastAPI/Starlette test client dependency.
- Ran syntax/import compilation check:
  - `.venv\Scripts\python.exe -m py_compile agent_app\tool_mcp_server.py tests\test_agent_app_langgraph_native.py`
- Result: passed.

### Nutrition meal validation hardening

- Hardened nutrition meal recording so invalid nutrient values return public validation codes instead of raw `float(...)` conversion errors.
- Added public validation codes for:
  - `invalid_nutrient_value`
  - `negative_nutrient_value`
  - `invalid_nutrition_date`
  - `unsupported_meal_type`
  - `foods_required`
  - `food_name_required`
- Added finite/non-negative checks for nutrient amounts to block `NaN`, infinity, and negative intake values.
- Hardened `/api/agent/nutrition/meals` so `ValueError` from nutrition service is returned as HTTP 422 with an allowlisted public detail code.
- This lets MCP preserve recoverable nutrition validation codes for the agent without leaking raw food/nutrient input text.

### Verification

- Added regression coverage for:
  - invalid nutrient text returning `invalid_nutrient_value` without echoing raw input.
  - negative nutrient values returning `negative_nutrient_value`.
  - existing patient-scoped meal query behavior.
  - existing MCP HTTP status error code preservation.
- Ran:
  - `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\test_nutrition_integration.py::test_agent_nutrition_record_meal_returns_public_error_for_invalid_nutrients tests\test_nutrition_integration.py::test_agent_nutrition_api_keeps_meal_queries_patient_scoped tests\test_agent_app_langgraph_native.py::test_http_status_tool_error_preserves_public_detail_code_and_redacts_private_detail`
- Result: `3 passed, 1 warning`.
- The remaining warning is from the installed FastAPI/Starlette test client dependency.
- Ran syntax/import compilation check:
  - `.venv\Scripts\python.exe -m py_compile system_app\services\nutrition_service.py system_app\routes\agent_api.py tests\test_nutrition_integration.py`
- Result: passed.
