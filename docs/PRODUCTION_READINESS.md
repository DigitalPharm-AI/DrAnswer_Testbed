# AI Agent Production Readiness

이 문서는 영양+복약 통합 채팅을 controlled pilot으로 올리기 전에 닫아야 하는 P0/P1 운영 기준입니다. 실제 환자 데이터는 eval과 로그에 넣지 않고, synthetic case와 redacted trace만 사용합니다.

## Launch Gates

| Gate | Required Check | Stop Condition | Owner |
| --- | --- | --- | --- |
| Evaluation | `data/evals/agent_production_readiness_cases.json` 30개 이상, critical safety case 15개 이상 | critical safety failure 1건 이상 | QA/Clinical |
| Async Ops | `/agent/ops/readiness` status가 `ok` 또는 승인된 `degraded` | `critical` alert 존재 | Backend/Ops |
| Privacy | `tests/test_redaction.py` 통과, trace 원문 PHI 미노출 | patient_id, PHR key, 증상/식사 자유문장 원문 로그 | Security/Ops |
| PHR Containment | `PHR_READ_ONLY=true`에서 write 503, read 유지 | read-only 모드에서 register/update 성공 | Data/PHR |
| Data Boundary | `python tools/verify_testbed_contract.py` 및 runtime write-denial probe 통과 | Agent가 Backend mount를 RW로 받거나 세 서비스가 하나의 RW DB 디렉터리를 공유 | Agent/Backend/Ops |
| Architecture | system chat은 async-first, slow work는 callback으로 완료 | sync-only 문서/운영 절차로 배포 | Agent/Backend |
| P1 Load Budget | `tools/p1_load_probe.py`에서 20-50 concurrency probe 실행 | error rate > 1% 또는 P95 budget 초과 | Ops/Backend |
| P1 Cost Budget | current provider 단가 env 설정 후 일일 비용 추정 | daily budget 초과 또는 단가 미설정 상태로 launch gate 사용 | Ops/Product |
| P1 Failure UX | `system_app.services.failure_copy` 기준 문구 사용 | 실패 알림이 기록 보존/재시도/안전 안내 없이 노출 | Product/UX |
| P1 Trace Store | `agent_run_traces`, `agent_run_steps`에 redacted trace/cost/tool step 저장 | raw patient_id, PHR key, 식사/증상 자유문장 원문 저장 | Backend/Ops |

권장 P0 CI subset:

```powershell
python tools/verify_testbed_contract.py
py -m pytest -q -p no:cacheprovider tests/test_production_eval_dataset.py tests/test_redaction.py tests/test_agent_ops_readiness.py tests/test_phr_app.py tests/test_agent_async_callbacks.py tests/test_migrations_health.py
```

권장 P1 CI subset:

```powershell
py -m pytest -q -p no:cacheprovider tests/test_agent_trace_store.py tests/test_agent_eval_runner.py tests/test_nutrition_integration.py tests/test_readiness_budget.py
python tools/run_agent_eval_suite.py --deterministic-only
```

## Eval Dataset

- Canonical file: `data/evals/agent_production_readiness_cases.json`
- 모든 case는 synthetic이어야 합니다.
- 각 case는 `expected_behavior`, `prohibited_behavior`, `severity`, `owner`, `pass_gate`를 가져야 합니다.
- coverage에는 최소한 `nutrition`, `medication`, `phr`, `side_effect`, `async`, `mcp`, `privacy`, `observability`, `governance`, `incident`, `safety`가 포함되어야 합니다.
- 실제 장애가 발생하면 incident trace를 직접 복사하지 말고 redacted summary로 새 eval case를 추가합니다.

Local eval runner:

```powershell
python tools/run_agent_eval_suite.py --deterministic-only
python tools/run_agent_eval_suite.py --output outputs/evals/full-review.json
```

Local 9000 rule_based stack:

```powershell
powershell -ExecutionPolicy Bypass -File tools/da_drug_9000_stack.ps1 start
powershell -ExecutionPolicy Bypass -File tools/da_drug_9000_stack.ps1 verify
powershell -ExecutionPolicy Bypass -File tools/da_drug_9000_stack.ps1 stop
```

`verify`는 세 서비스 health와 worker 상태뿐 아니라 다음 release-blocking 경계를 검사한다.

- Backend, AI, PHR의 자체 SQLite 파일과 RW runtime 경로가 분리되어 있는지
- AI의 Backend DB URL이 `mode=ro&uri=true`인지
- AI `/health/ready`가 DB·read contract·인증 설정을 모두 통과해 `status=ready`인지
- AI 관점에서 Backend DB read가 성공하고 main DB write probe가 거절되는지
- 실제 `Backend /api/chat/sync → AI /agent/sync/chat → Backend read-only DB` 경로와 replay가 정상인지
- Backend→AI와 AI→Backend Bearer token이 서로 다른지
- 저장된 Backend v1.2 OpenAPI가 현재 코드와 일치하는지

동일 검사는 `.github/workflows/testbed-boundary-contract.yml`에서 PR과 push마다 실행한다.

Human review queue for semantic/behavioral evals:

```powershell
python tools/manage_eval_review_queue.py export
python tools/manage_eval_review_queue.py mark --case-id CASE_ID --status reviewed --reviewer qa-owner --evidence "artifact or transcript reference"
python tools/run_agent_eval_suite.py --review-state data/evals/agent_eval_review_queue.json --output outputs/evals/full-review.json
```

Eval backlog integration:

- 기본 실행은 `data/evals/agent_eval_backlog.json`이 있으면 자동으로 함께 읽습니다.
- `--no-backlog`는 local debugging에서만 backlog 편입을 끕니다.
- 중복 backlog ID는 새 ID로 바꾸지 않고 stable ID를 유지한 채 excluded로 report합니다.
- schema가 깨진 backlog case는 suite 실패로 처리합니다.
- backlog lifecycle은 `open → reviewed → cleared`입니다.
- `high`와 `critical` backlog case는 `cleared`가 되기 전까지 CI gate 실패로 처리합니다. `reviewed`는 owner가 봤다는 표시이며 gate 해제가 아닙니다.
- LOGS 탭의 Eval backlog에서 lifecycle 상태를 바꿀 수 있고, 변경 이력은 case의 `lifecycle.history`에 남습니다.

`deterministic-only`는 CI에서 자동 gate로 사용합니다. 전체 suite는 semantic/behavioral case를 `review_required`로 남기며, 모델 채점 또는 QA/Clinical 리뷰가 붙기 전에는 승격 증거로 단독 사용하지 않습니다.

## P1 Governance And Observability

LOGS 탭은 pilot 운영자가 다음 readiness controls를 한 화면에서 확인하도록 확장되어야 합니다.

| Control | Artifact | Required Behavior |
| --- | --- | --- |
| Prompt/model/tool change log | `data/governance/agent_change_log.json` | 변경 target, summary, owner, rollback, evidence를 남깁니다. |
| Trace replay/detail | `GET /partials/logs/traces/{trace_id}` | redacted trace hash, model/prompt, tool step metadata, replay plan을 표시합니다. |
| High-risk human handoff gate | `apply_notification_policy`, `apply_system_policy` | agent가 직접 적용하지 않고 `policy_confirmation_required`와 `human_handoff_required`를 남깁니다. |
| Tool/data freshness probe | LOGS Tool/Data Catalog | tool source 존재, catalog match, eval/change-log JSON 상태를 표시합니다. |
| Online eval loop | `data/evals/agent_online_eval_findings.json` | 최근 trace의 실패, tool error, high-risk handoff 누락, latency/cost watch를 deterministic scan으로 기록합니다. |
| Trace replay artifact | `outputs/replays/{trace_id}.json` | incident trace를 redacted replay artifact로 저장해 eval 승격과 재현 검토에 사용합니다. |
| Cost budget gate | LOGS Cost budget gate | trace token/cost를 daily budget, eval budget, token price 설정과 비교합니다. |
| Model fallback drill | `data/governance/model_fallback_drills.json` | provider 장애 시 `LLM_PROVIDER=rule_based` 전환 절차, owner, rollback 조건을 dry-run으로 기록합니다. |

| Trace replay to eval backlog | LOGS `Replay to eval backlog` | Writes a replay artifact and creates or links a behavioral eval backlog case with `trace_replay_artifact` evidence. |
| Release readiness score | LOGS Release readiness score | Combines online eval, failed traces, cost, fallback drill, eval blockers, alerts, and local jobs into a release decision. |
| Release readiness report | `outputs/readiness/release-readiness-*.json` and `.md` | Captures eval result, alerts, failed jobs, cost, fallback status, trace coverage, and scorecard evidence for release review. |

## P1 Load And Cost Gates

Load probe:

```powershell
python tools/p1_load_probe.py --concurrency 50 --iterations 200 --internal-api-token $env:INTERNAL_API_TOKEN
```

PHR registration write path까지 검증할 때만 synthetic write probe를 켭니다.

```powershell
python tools/p1_load_probe.py --concurrency 20 --iterations 50 --include-phr-register
```

Default P1 thresholds:

| Metric | Default |
| --- | --- |
| concurrency target | 50 |
| max error rate | 0.01 |
| health P95 | 1000 ms |
| agent readiness/async accept P95 | 2000 ms |
| PHR registration P95 | 3000 ms |

Cost budget settings:

| Env | Purpose |
| --- | --- |
| `AGENT_DAILY_COST_BUDGET_USD` | pilot daily model spend ceiling |
| `AGENT_EVAL_COST_BUDGET_USD` | daily eval spend ceiling |
| `AGENT_COST_INPUT_USD_PER_1M_TOKENS` | current provider input token price |
| `AGENT_COST_OUTPUT_USD_PER_1M_TOKENS` | current provider output token price |

Token prices default to `0` so stale hard-coded prices do not silently become launch gates. Set them from the current provider price sheet before using cost as a release blocker.

## Failure UX Copy

Reusable copy lives in `system_app/services/failure_copy.py`.

| Failure | User-facing behavior |
| --- | --- |
| agent network/provider failure | tell the user the AI answer failed, records are saved, retry is available |
| async missed dose failure | keep missed-dose record, point to AI error retry |
| daily pattern failure | do not change reminder policy, point to retry |
| PHR read-only | explain PHR registration is temporarily paused and local medication input remains |
| clinician alert failure | do not imply delivery succeeded; advise direct clinical contact for severe/worsening symptoms |

## Async Ops Dashboard

Primary endpoints:

- `GET /agent/ops/readiness`: 운영자가 보는 readiness summary, alert severity, dead task sample, runbook link
- `GET /agent/async/tasks/status`: queue counts와 worker heartbeat
- `GET /agent/async/tasks/dead`: dead task 상세
- `POST /agent/async/tasks/{request_id}/actions`: dead task만 `retry` 또는 `dismiss` 처리
- `GET /health/details`: system_app에서 agent async 상태를 함께 요약
- `GET /api/agent/async/traces`: redacted agent run trace 목록
- `GET /api/agent/async/traces/{trace_id}`: redacted trace와 model/tool/final response step 상세

Dead task action payload:

```json
{"action": "retry", "reason": "callback endpoint recovered"}
```

```json
{"action": "dismiss", "reason": "duplicate incident handled manually"}
```

`retry`는 task를 `pending`으로 되돌리고 attempts/lock을 초기화합니다. `dismiss`는 운영자가 별도 처리 완료했다고 보고 `failed`로 닫습니다. 두 액션 모두 `dead` 상태가 아닌 task에는 409를 반환해야 합니다.

Critical alert codes:

| Code | Meaning | First Action |
| --- | --- | --- |
| `agent_async_worker_unavailable` | active task가 있는데 running worker heartbeat 없음 | worker 프로세스 상태 확인 후 재시작 |
| `agent_async_dead_tasks_present` | dead task 존재 | dead sample과 callback/system 로그 확인 |
| `agent_async_callback_failures_detected` | callback delivery failure | system_app callback endpoint와 internal token 확인 |
| `agent_provider_failures_detected` | model/provider failure | provider credential, model tier, fallback 전환 검토 |

Warning alert codes:

- `agent_async_worker_not_reporting`
- `agent_async_worker_stale`
- `agent_async_pending_age_exceeded`

## Incident Runbook

### Detect

1. `/agent/ops/readiness`가 `critical`이면 incident로 취급합니다.
2. `/health/details`에서 `agent_async_dead_tasks_present`, `agent_async_worker_unavailable`, `agent_server_unreachable` warning을 확인합니다.
3. 관련 시간대의 `trace_id`, `request_id`, `task_type`, `prompt_version_id`, `model_tier`를 기록합니다.

### Diagnose

1. `/agent/async/tasks/dead`에서 dead sample을 확인합니다.
2. callback failure면 system_app callback endpoint, `INTERNAL_API_TOKEN`, network reachability를 확인합니다.
3. provider failure면 Bedrock credential, region, model id, timeout을 확인합니다.
4. `/api/agent/async/traces?request_id=...` 또는 `/api/agent/async/traces/{trace_id}`로 model tier, prompt version, token/cost, tool side effect step을 확인합니다.
5. privacy/safety issue면 redacted trace와 eval case coverage를 확인합니다. 원문 환자 데이터를 incident 문서에 붙이지 않습니다.

### Contain

| Scenario | Containment |
| --- | --- |
| Provider outage or unsafe LLM output | agent_app/worker 중지 또는 승인된 이전 provider/model tier로 rollback (`rule_based`는 test/testbed 전용) |
| PHR write risk or sync corruption | `PHR_READ_ONLY=true`로 phr_app 재시작 |
| callback storm or duplicate side effects | agent worker 중지 후 queue/dead task 점검 |
| policy confirmation bypass risk | worker 중지, policy confirmation route 회귀 테스트 후 재개 |
| PHI/log exposure suspicion | trace logging off 또는 log sink 접근 제한, redaction test 실행 |

Containment 기록에는 owner, approver, start time, affected workflow, rollback condition이 포함되어야 합니다.

### Fix

1. 원인에 맞는 deterministic/semantic/behavioral eval case를 추가합니다.
2. redaction, permission, callback, retry, prompt/model config 중 어느 control이 실패했는지 명시합니다.
3. P0 CI subset과 관련 Playwright scenario를 실행합니다.
4. readiness endpoint가 `ok` 또는 승인된 `degraded`로 돌아온 뒤 worker를 재개합니다.

## Redaction Policy

- trace log는 `shared.redaction.redact_for_logging`을 통과해야 합니다.
- `patient_id`, `phr_patient_key`, MRN/RRN 계열 식별자는 hash 형태로만 남깁니다.
- 증상, 식사, 복약명, 사용자 메시지, PHR evidence 같은 clinical free text는 원문 대신 length/hash/count만 남깁니다.
- secret/token/API key/email/phone은 마스킹합니다.
- raw payload가 필요한 debugging은 운영 로그가 아니라 제한된 incident workspace에서 승인 후 수행합니다.

## Rollback Paths

| Change Type | Rollback |
| --- | --- |
| Model quality issue | 승인된 이전 provider/model tier로 재시작; test/testbed에서만 `LLM_PROVIDER=rule_based` 사용 |
| PHR data issue | `PHR_READ_ONLY=true`, PHR sync 중지, last known good DB snapshot 복구 |
| Async worker issue | worker 중지, running/callback_sent task reset 또는 dead task 재처리 계획 수립 |
| Prompt/policy issue | prompt workbook/policy workbook 이전 버전 복구 |
| Tool permission issue | affected MCP tool permission 차단 후 eval 재실행 |
