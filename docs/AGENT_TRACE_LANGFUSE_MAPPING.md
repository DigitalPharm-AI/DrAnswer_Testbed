# Agent Trace와 Langfuse 매핑

이 문서는 Agent DB의 감사 이력과 내부 EC2에 self-hosted한 Langfuse의
필드 및 책임 경계다. Agent DB는 감사 원장이고, Langfuse는
조회·분석용 관측 저장소다. Langfuse UI와 수집 endpoint는 VPC 내부에
두고 관리자만 접근한다.

## 확정 아키텍처

1. Agent API/worker는 실행 결과와 관찰값, Langfuse outbox를 같은 DB
   transaction에 기록한다.
2. 별도 `agent_app.langfuse_exporter_main` process가 outbox를 claim한다.
3. trace는 Langfuse v4 호환 OTLP/HTTP
   `/api/public/otel/v1/traces`로 전송한다.
4. score는 `/api/public/scores`, 보존기간 만료 trace 삭제는
   `/api/public/traces/{traceId}`를 사용한다.
5. HTTP 2xx를 받은 뒤에만 outbox를 `COMPLETED`로 바꾼다. Langfuse
   장애는 Agent 요청·Tool 실행·Backend callback의 성공 여부를
   변경하지 않는다.

## 기본 매핑

| Agent DB | Langfuse | 처리 |
|---|---|---|
| `AgentRunTrace.trace_id` | deterministic trace seed | SHA-256 namespace로 유효한 32 hex W3C trace ID 생성 |
| `workflow_name` | trace name | `multiturn_chat`, `missed_dose` 등 안정된 이름 사용 |
| `patient_id_hash` | user id, session id | 환자별 영구 thread 정책과 동일하며 원본 환자 ID는 전송하지 않음 |
| `environment`, `release_version` | environment, release | 그대로 매핑 |
| `metadata_json.langfuse_tags` | tags | workflow, agent, decision type |
| `AgentRunStep.observation_id` | OTEL span id seed | 원문 ID를 내보내지 않고 deterministic 16 hex span ID로 변환 |
| `parent_observation_id` | OTEL parent span id | exporter가 중첩 tree를 복원 |
| `observation_type=agent` | span | 이름을 `attempt.1`, `attempt.2`로 지정해 재시도 경계를 표현 |
| `observation_type=generation` | generation observation | 실제 LLM 호출 |
| `observation_type=tool` | span | Tool 이름과 side-effect 수준을 observation metadata에 기록 |
| `started_at`, `completion_start_time`, `completed_at` | observation timestamps | latency와 TTFT 계산에 사용 |
| `provider`, `model_id`, `model_parameters_json` | model, model parameters | LLM 호출 기준 |
| `usage_details_json`, `cost_details_json` | usage details, cost details | 토큰·비용 기준 |
| `prompt_version_id` | observation metadata | 현재 prompt 관리 주체는 코드/workbook이며 Langfuse Prompt 객체에는 연결하지 않음 |
| `level`, `status_message`, `error_code` | level, status message | 오류 메시지는 비식별 라벨만 전송 |

한 logical request/task는 Langfuse trace 하나다. Agent task가 재시도되면
`trace_id`는 그대로 유지하고 `trace_attempt_number`별 root agent
observation을 추가한다. model/tool/final observation은 해당 attempt
아래에 위치한다.

## 의도적으로 보내지 않는 값

- 환자 ID, 환자 메시지, 프롬프트 원문, 모델 응답 원문
- Tool arguments와 Tool response 원문
- 인증 정보와 Backend 내부 식별자

대신 `input_hash`, `output_hash`, `argument_hash`, `response_hash`, 키 목록,
개수, 상태, 시간만 보낸다. Langfuse 마스킹 설정과 무관하게 전송 전에
AI Server에서 비식별화한다.

## Score 정의

| 이름 | 형식 | 의미 |
|---|---|---|
| `agent_success` | BOOLEAN | attempt가 완료되고 응답 검증도 통과함 |
| `response_validation` | BOOLEAN | 완료 응답이 Agent response contract 검증을 통과함 |
| `tool_success_rate` | NUMERIC 0~1 | 해당 attempt의 성공 Tool 수 / 전체 Tool 수 |
| `retry_count` | NUMERIC | 현재까지 logical trace 재시도 횟수. 동일 score id를 update |
| `user_feedback` | BOOLEAN | like=true, dislike=false. 동일 assistant message의 score id를 update |

자유문 feedback은 AI DB에 암호문으로만 7일 보관한다. Langfuse에는
본문·본문 hash·message ID를 보내지 않으며, 자유문 존재 여부만 score
metadata로 보낸다.

## 비용과 sampling

- 비용은 `AGENT_COST_INPUT_USD_PER_1M_TOKENS`,
  `AGENT_COST_OUTPUT_USD_PER_1M_TOKENS`에 설정한 승인 단가로 계산한다.
  EC2 환경 검증은 Langfuse가 활성화됐을 때 두 단가가 0보다 큰지
  확인한다.
- 초기 `LANGFUSE_SUCCESS_SAMPLE_RATE=1.0`으로 전량 수집한다.
- 나중에 성공 trace 비율을 낮춰도 오류 trace, `missed_dose`, write
  Tool 실행은 항상 수집한다. 사용자 feedback이 도착한 sampled-out
  trace도 다시 `PENDING`으로 승격해 수집한다.

## 장애 처리

- outbox 상태: `PENDING → PROCESSING → COMPLETED`
- 일시 장애: `PROCESSING → RETRYABLE_FAILED → PROCESSING`
- 재시도 소진 또는 4xx 설정 오류: `DEAD`
- lease가 만료된 `PROCESSING`은 다른 exporter가 재claim한다.
- 기본 12회 exponential backoff+jitter, 5회 연속 일시 장애 시 60초
  circuit open을 적용한다.
- 외부 응답 본문과 secret은 오류 이력에 저장하지 않는다.
- deterministic trace/span/score ID로 재처리 상관관계를 유지한다.
- 전달 의미는 at-least-once다. Langfuse가 HTTP 요청을 수락한 직후
  exporter가 DB 완료 표시 전에 종료되면 같은 deterministic ID로
  재전송될 수 있으므로 중복 가능성을 운영 지표에서 확인한다.

## 보존과 삭제

- Agent trace/step/tool 원장은 1,095일 보관한다.
- 만료 시 같은 DB transaction에서 `TRACE_DELETE` outbox를 만들고
  Agent 원장을 삭제한다. exporter는 Langfuse trace를 삭제하며
  Langfuse의 연관 observation/score 삭제 cascade를 사용한다.
- 완료·skip·dead outbox는 30일 뒤 정리한다. 미전송
  `PENDING/PROCESSING/RETRYABLE_FAILED`는 retention cleanup이 임의로
  삭제하지 않는다.
- Self-hosted Enterprise를 사용한다면 Langfuse project retention도
  1,095일로 설정해 이중 안전장치로 둔다. OSS에서는 위 명시적
  `TRACE_DELETE`가 기본 삭제 경로다.

## 운영 원칙

1. Agent DB에 먼저 append-only로 기록하고 Langfuse 전송 실패가 Agent
   업무 성공 여부를 바꾸지 않게 한다.
2. Langfuse 전송은 durable outbox와 별도 비동기 exporter로만 수행한다.
3. 같은 `trace_id` 아래 `trace_attempt_number`별 실행을 유지한다.
4. 원문 마스킹은 Langfuse가 아니라 AI Server 전송 직전에 완료한다.
5. `DEAD` 수, 가장 오래된 pending 시간, 재시도 수와 삭제 event 완료를
   운영 지표로 감시한다.
