# AI Agent Production Readiness

이 문서는 영양+복약 통합 채팅을 controlled pilot으로 올리기 전에 닫아야 하는 P0/P1 운영 기준입니다. 실제 환자 데이터는 eval과 로그에 넣지 않고, synthetic case와 redacted trace만 사용합니다.

## Launch Gates

| Gate | Required Check | Stop Condition | Owner |
| --- | --- | --- | --- |
| Evaluation | `data/evals/agent_production_readiness_cases.json` 30개 이상, critical safety case 15개 이상 | critical safety failure 1건 이상 | QA/Clinical |
| Async Ops | `/agent/ops/readiness` status가 `ok` 또는 승인된 `degraded` | `critical` alert 존재 | Backend/Ops |
| Privacy | `tests/test_redaction.py` 통과, trace 원문 PHI 미노출 | patient_id, MRN/RRN, 증상/식사 자유문장 원문 로그 | Security/Ops |
| Patient Snapshot | `tests/test_patient_snapshot_context.py` 통과 | 별도 PHR 등록을 요구하거나 환자 범위 밖 데이터를 Snapshot에 포함 | Agent/Backend/Data |
| Data Boundary | `python tools/verify_testbed_contract.py` 및 runtime write-denial probe 통과 | Agent가 Backend mount를 RW로 받거나 Backend 업무 DB와 AI Internal DB가 같은 RW 저장소를 공유 | Agent/Backend/Ops |
| Architecture | 사용자 채팅과 필수 Tool continuation은 동기 완료, 채팅 외 background work만 queue/callback 사용 | 임시 채팅 답변을 성공으로 반환하거나 Backend 업무 데이터를 AI DB에 저장 | Agent/Backend |
| P1 Load Budget | `tools/p1_load_probe.py`에서 20-50 concurrency probe 실행 | error rate > 1% 또는 P95 budget 초과 | Ops/Backend |
| P1 Cost Budget | current provider 단가 env 설정 후 일일 비용 추정 | daily budget 초과 또는 단가 미설정 상태로 launch gate 사용 | Ops/Product |
| P1 Failure UX | `system_app.services.failure_copy` 기준 문구 사용 | 실패 알림이 기록 보존/재시도/안전 안내 없이 노출 | Product/UX |
| P1 Trace Store | Agent DB `agent_run_traces`, `agent_run_steps`, `agent_tool_executions`에 sync/async/failed Trace와 token/cost 저장 | Backend 중복 Trace 또는 raw patient_id, MRN/RRN, 식사/증상 자유문장 원문 저장 | Agent/Ops |
| Retention | Trace/Step/Tool/token/cost와 Backend 최소 API 감사에 생성 시점 + 1095일 `expires_at` 적용 | 3년 경과 데이터나 개인정보 파기 대상이 계속 조회됨 | Security/Ops |

권장 P0 CI subset:

```powershell
python tools/verify_testbed_contract.py
py -m pytest -q -p no:cacheprovider tests/test_redaction.py tests/test_agent_ops_readiness.py tests/test_patient_snapshot_context.py tests/test_v13_patient_scope_tools.py tests/test_agent_async_callbacks.py tests/test_migrations_health.py
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
- coverage에는 최소한 `nutrition`, `medication`, `patient_snapshot`, `side_effect`, `async`, `mcp`, `privacy`, `observability`, `governance`, `incident`, `safety`가 포함되어야 합니다.
- 실제 장애가 발생하면 incident trace를 직접 복사하지 말고 redacted summary로 새 eval case를 추가합니다.

Local eval runner:

```powershell
python tools/run_agent_eval_suite.py --deterministic-only
python tools/run_agent_eval_suite.py --output outputs/evals/full-review.json
```

Local 9000 configured-provider stack:

```powershell
powershell -ExecutionPolicy Bypass -File tools/da_drug_9000_stack.ps1 `
  -Action start -EnvFile .env.9000 -AgentEnvFile .env.agent_app.secret
powershell -ExecutionPolicy Bypass -File tools/da_drug_9000_stack.ps1 `
  -Action verify -EnvFile .env.9000 -AgentEnvFile .env.agent_app.secret
powershell -ExecutionPolicy Bypass -File tools/da_drug_9000_stack.ps1 `
  -Action stop -EnvFile .env.9000 -AgentEnvFile .env.agent_app.secret
```

`.env.9000`에는 Bedrock bearer를 두지 않는다. Bearer는
`.env.agent_app.secret`에만 두며 Agent API와 worker만 해당 overlay를
받는다. common 파일에 bearer 키가 있으면 launcher와 verifier가 값 출력 없이
실패한다.
Compose의 `up`·`dev`·`restart`와 직접 실행 runner의
`start`·`restart`도 authenticated `/health/generation/ready`를 release gate로
사용한다. `AGENT_SYNC_API_TOKEN`은 child 메모리에서만 읽고 argv와 상태 출력에는
포함하지 않는다.

`verify`는 Backend·AI health와 worker 상태뿐 아니라 다음 release-blocking 경계를
검사한다. 활성 9000 helper와 Compose는 별도 `phr-app`을 기동하지 않는다.

- Backend 업무 DB와 AI Internal PostgreSQL DB의 database·runtime role이 분리되어 있는지
- AI의 Backend DB role이 `ai_v13_*` View만 조회하고 원본 table·쓰기를 거절하는지
- AI `/health/ready`가 DB·read contract·인증 설정을 모두 통과해 `status=ready`인지
- AI 관점에서 Backend DB read가 성공하고 main DB write probe가 거절되는지
- 실제 `React /api/ui/v1/chat/sync → Backend → AI /agent/sync/chat → Backend Read-only Snapshot` 경로와 replay가 정상인지
- 별도 PHR 등록·PHR HTTP 조회 없이 현재 환자정보와 부작용 평가가 동작하는지
- Backend→AI와 AI→Backend Bearer token이 서로 다른지
- 저장된 Backend v1.3 OpenAPI가 현재 코드와 일치하는지

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
- 환자용 React 화면에는 운영용 LOGS 탭이 없다. Eval lifecycle 변경은
  `tools/manage_eval_review_queue.py` 또는 승인된 별도 운영 도구로 수행하고,
  변경 이력은 case의 `lifecycle.history`에 남긴다.

`deterministic-only`는 CI에서 자동 gate로 사용합니다. 전체 suite는 semantic/behavioral case를 `review_required`로 남기며, 모델 채점 또는 QA/Clinical 리뷰가 붙기 전에는 승격 증거로 단독 사용하지 않습니다.

## P1 Governance And Observability

승인된 운영 콘솔 또는 observability sink는 다음 readiness controls를 제공해야
합니다. 환자용 React 화면이나 Backend API에 Trace 조회 기능을 넣지 않습니다.

| Control | Artifact | Required Behavior |
| --- | --- | --- |
| Prompt/model/tool change log | `data/governance/agent_change_log.json` | 변경 target, summary, owner, rollback, evidence를 남깁니다. |
| Trace ownership/detail | Agent DB 단일 원장과 승인된 운영 조회 도구 | Backend Trace API·테이블을 만들지 않고 redacted trace hash, model/prompt, token/cost, tool step metadata만 조회합니다. |
| High-risk human handoff gate | `propose_notification_policy`, `request_record_approval → change_notification_policy`, `propose_system_policy` | 정책 후보는 직접 적용하지 않고, 실제 `change_notification_policy` 쓰기는 사용자 승인 뒤에만 동기 실행합니다. |
| Tool/data freshness probe | 계약 CI와 승인된 운영 probe | tool source 존재, catalog match, eval/change-log JSON 상태를 검증합니다. |
| Online eval loop | `data/evals/agent_online_eval_findings.json` | 최근 trace의 실패, tool error, high-risk handoff 누락, latency/cost watch를 deterministic scan으로 기록합니다. |
| Trace replay artifact | `outputs/replays/{trace_id}.json` | incident trace를 redacted replay artifact로 저장해 eval 승격과 재현 검토에 사용합니다. |
| Cost budget gate | Agent Trace 집계와 승인된 운영 gate | trace token/cost를 daily budget, eval budget, token price 설정과 비교합니다. |
| Trace replay to eval backlog | 승인된 운영 도구 | redacted replay artifact를 만들고 `trace_replay_artifact` 증거가 있는 behavioral eval backlog case를 생성하거나 연결합니다. |
| Release readiness score | 별도 운영 gate | online eval, failed trace, cost, provider incident rollback readiness, eval blocker, alert와 local job을 release decision으로 결합합니다. |
| Release readiness report | 별도 운영 산출물 | 환자용 Backend/React가 아닌 승인된 운영 도구가 eval·오류·비용·trace coverage를 요약합니다. |

## P1 Load And Cost Gates

Load probe:

```powershell
python tools/p1_load_probe.py --concurrency 50 --iterations 200 --internal-api-token $env:INTERNAL_API_TOKEN
```

환자정보 Read 부하는 별도 PHR 등록 API가 아니라 Backend DB Snapshot 조회와
상세 Query Tool을 대상으로 측정합니다. 현재 P1 load probe는 Backend·AI health,
AI readiness, React BFF status를 측정합니다. 별도 PHR 서비스 경로는 없습니다.

Default P1 thresholds:

| Metric | Default |
| --- | --- |
| concurrency target | 50 |
| max error rate | 0.01 |
| health P95 | 1000 ms |
| agent readiness/async accept P95 | 2000 ms |
| React BFF status P95 | 2000 ms |

Cost budget settings:

| Env | Purpose |
| --- | --- |
| `AGENT_DAILY_COST_BUDGET_USD` | pilot daily model spend ceiling |
| `AGENT_EVAL_COST_BUDGET_USD` | daily eval spend ceiling |
| `AGENT_COST_INPUT_USD_PER_1M_TOKENS` | current provider input token price |
| `AGENT_COST_OUTPUT_USD_PER_1M_TOKENS` | current provider output token price |

Token prices default to `0` so stale hard-coded prices do not silently become launch gates. Set them from the current provider price sheet before using cost as a release blocker.

## Self-hosted Langfuse Export

- Langfuse는 내부 EC2/VPC의 private HTTPS endpoint와 관리자 UI로
  운영합니다.
- Agent request/worker path는 Langfuse에 직접 접속하지 않습니다.
  `agent_observability_exports` outbox와
  `python -m agent_app.langfuse_exporter_main`을 사용합니다.
- 초기 성공 trace sampling은 100%입니다. 비율을 낮춘 뒤에도 오류,
  미복용, write Tool, 사용자 feedback trace는 항상 수집합니다.
- 기본 export retry는 최대 12회이고 lease recovery, exponential
  backoff+jitter, circuit breaker, `DEAD` 상태를 사용합니다.
- `DEAD` 건수와 oldest pending age를 운영 경보에 연결해야 합니다.
- Agent trace retention은 1,095일입니다. 만료 시 remote trace deletion
  outbox를 남기며, Langfuse Enterprise retention을 쓰는 경우 project
  retention도 1,095일로 맞춥니다.

Structured application logs collected from stdout are not deleted by the
application DB cleanup. The external log sink must enforce the same 3-year
retention/deletion rule and provide deletion verification. Agent DB Trace and
Backend minimal audit rows use indexed `expires_at`; raw clinical free text is
never an observability retention substitute.

## Failure UX Copy

Reusable copy lives in `system_app/services/failure_copy.py`.

| Failure | User-facing behavior |
| --- | --- |
| agent network/provider failure | tell the user the AI answer failed, records are saved, retry is available |
| async missed dose LLM failure | keep the missed-dose record, expose the stable AI error code for retry/inspection, and never substitute a stock message |
| daily pattern failure | do not change reminder policy, point to retry |
| Backend patient Snapshot partial | 확인 가능한 도메인만 답하고 `not_found`, `not_supported`, `unavailable`을 구분 |
| Backend Read unavailable | LLM·Tool·쓰기 호출 없이 명시적 오류와 재시도 제공 |
| clinician alert failure | do not imply delivery succeeded; advise direct clinical contact for severe/worsening symptoms |

## Async Ops Dashboard

Primary endpoints:

- `GET /agent/ops/readiness`: 운영자가 보는 readiness summary, alert severity, dead task sample, runbook link
- `GET /agent/async/tasks/status`: queue counts와 worker heartbeat
- `GET /agent/async/tasks/dead`: dead task 상세
- `POST /agent/async/tasks/{request_id}/actions`: dead task만 `retry` 또는 `dismiss` 처리
- `GET /health/details`: system_app에서 agent async 상태를 함께 요약
- Agent DB 승인 운영 조회/내보내기: redacted Trace와
  model/tool/final-response Step, token/cost 상세(Backend API로 노출하지 않음)

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
4. 승인된 Agent DB 운영 조회 또는 observability sink에서 `request_id`/내부
   `trace_id`로 model tier, prompt version, token/cost, Tool side-effect Step을
   확인합니다. Backend Trace API를 사용하지 않습니다.
5. privacy/safety issue면 redacted trace와 eval case coverage를 확인합니다. 원문 환자 데이터를 incident 문서에 붙이지 않습니다.

### Contain

| Scenario | Containment |
| --- | --- |
| Provider outage or unsafe LLM output | agent_app/worker 중지 또는 승인된 이전 provider/model tier로 rollback |
| Backend Read scope or Snapshot corruption | AI 채팅 중지, AI DB reader 자격 증명 회수 또는 영향 View 권한 차단, Backend 원장 검증 |
| Backend write API idempotency/version risk | 영향 쓰기 Tool 권한 차단 후 Backend request ledger와 업무 원장 검증 |
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
- `patient_id`, MRN/RRN 계열 식별자는 hash 형태로만 남깁니다.
- 증상, 식사, 복약명, 사용자 메시지, 환자 Snapshot evidence 같은 clinical free
  text는 원문 대신 length/hash/count만 남깁니다.
- secret/token/API key/email/phone은 마스킹합니다.
- raw payload가 필요한 debugging은 운영 로그가 아니라 제한된 incident workspace에서 승인 후 수행합니다.

## Rollback Paths

| Change Type | Rollback |
| --- | --- |
| Model quality issue | 승인된 이전 provider/model tier로 재시작 |
| Patient data/View issue | 영향 AI Read View 권한 차단 또는 AI 채팅 중지, Backend 원장과 View를 검증한 뒤 복구 |
| Async worker issue | worker 중지, running/callback_sent task reset 또는 dead task 재처리 계획 수립 |
| Prompt/policy issue | prompt workbook/policy workbook 이전 버전 복구 |
| Tool permission issue | affected MCP tool permission 차단 후 eval 재실행 |
