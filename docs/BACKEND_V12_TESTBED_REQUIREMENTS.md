# Backend v1.2 테스트베드 구현 및 개발팀 요구사항

## 1. 목적과 현재 상태

이 문서는 AI Server 연동규격 v1.2를 실제 Backend 개발팀에 요구하기 전에 `system_app` 테스트베드에서 먼저 검증한 구현을 기준으로 한다.

테스트베드에 구현된 범위는 다음과 같다.

- Backend가 사용자 메시지를 DB에 먼저 저장하고 불투명 공개 ID를 `message_id`로 AI Server에 전달
- AI Server `/agent/sync/chat` 동기 호출 및 동일 요청 재시도
- AI 응답을 별도 assistant 메시지로 Backend DB에 저장
- `text`, `selection_box`, `input_box`, 표 응답의 화면 표시 및 후속 입력
- AI Server의 Backend DB read-only Query Tools
- AI 답변 공개 ID를 검증하는 비동기 피드백 접수 및 암호화 저장
- AI Server에서 Backend로 요청하는 레코드 변경·알림 정책 변경 API
- Backend 내부 PK와 분리된 채팅·복약·영양·알림 정책 공개 ID
- Backend 쓰기 API의 `request_id` 멱등성
- 변경 가능한 업무 레코드의 `expected_version` 낙관적 잠금
- AI Trace·도구 실행 상태와 Backend 업무 데이터의 저장소 분리

기존 `/agent/multiturn-chat` 및 Backend 소유 `mutation_confirmations` 흐름은 레거시 시나리오 회귀를 위해 남아 있다. 일반 채팅 화면은 `contract_version=v1.2`를 보내 새 경로를 사용한다.

## 2. 서비스 및 데이터 소유권

```mermaid
flowchart LR
    APP[Client / App]
    BE[Backend Server]
    BEDB[(Backend DB)]
    AI[AI Server]
    AIDB[(AI Internal DB)]
    LLM[LLM]

    APP -->|채팅·확인 입력| BE
    BE -->|업무 데이터 RW| BEDB
    BE -->|POST /agent/sync/chat| AI
    AI -->|고정 Query Tools / SELECT only| BEDB
    AI -->|Trace·멱등성·도구 상태| AIDB
    AI -->|허용된 Tool 결과만| LLM
    LLM -->|Tool 선택·인자 생성| AI
    AI -->|쓰기 계약 API| BE

    LLM -. 금지: DB 연결·Raw SQL .-> BEDB
    AI -. 금지: Backend DB 직접 쓰기 .-> BEDB
```

| 데이터 | 원장 소유자 | AI Server 권한 |
|---|---|---|
| 사용자·채팅·복약·영양·알림 정책 | Backend DB | 고정 Query Tool을 통한 SELECT |
| 레코드 생성·변경·삭제 | Backend Server | Backend 쓰기 API 호출만 가능 |
| 요청 멱등성·Trace·도구 실행·대기 작업 | AI Internal DB | 읽기·쓰기 |
| `trace_id` | AI Internal DB | 외부 계약에 노출하지 않음 |

## 3. 채팅 처리 순서

```mermaid
sequenceDiagram
    participant C as Client
    participant B as Backend
    participant DB as Backend DB
    participant A as AI Server
    participant AD as AI Internal DB

    C->>B: 채팅 입력
    B->>DB: user message INSERT
    DB-->>B: message.public_id
    B->>A: POST /agent/sync/chat<br/>request_id + message_id
    A->>AD: request_id 멱등성 / conversation lock
    A->>DB: message_id·patient_id·conversation_id 검증(SELECT)
    A->>DB: 필요한 업무 데이터 조회(SELECT)
    A-->>B: v1.2 ChatSyncResponse
    B->>DB: assistant message INSERT<br/>reply_to_message_id=user.id
    B-->>C: Backend assistant public_id + 응답 payload
```

Backend 필수 규칙:

1. AI 호출 전에 사용자 메시지를 반드시 커밋한다.
2. AI 요청의 `message_id`는 방금 저장한 사용자 메시지의 `chat_messages.public_id`이다.
3. `request_id`는 Backend가 생성하며, timeout·503·504 재시도에도 본문과 함께 그대로 재사용한다.
4. AI 응답의 `message_id`는 사용자 메시지 ID의 echo이므로 assistant 메시지 ID로 사용하지 않는다.
5. assistant 메시지는 Backend가 별도로 저장하고 `chat_messages.public_id`를 Client에 반환한다.
6. 같은 `request_id`의 AI 응답을 재수신해도 assistant 메시지는 한 건만 저장한다.

테스트베드 Client 진입 API는 `POST /api/chat/sync`이다. 실제 Backend의 Client API 경로는 개발팀 표준을 따르되 위 처리 순서는 동일해야 한다.

## 4. Backend DB 요구 스키마

### 4.1 chat_messages

기존 필드 외에 다음 필드가 필요하다.

| 필드 | 용도 |
|---|---|
| `public_id` | `user_msg_*`, `assistant_msg_*` 형식의 불투명 공개 ID. NOT NULL, UNIQUE |
| `conversation_id` | 대화 단위 식별자 |
| `ai_request_id` | Backend가 AI 호출에 사용한 `request_id` |
| `message_type` | `text`, `selection_box`, `input_box` |
| `message_payload_json` | 규격의 구조화 `message` 객체 |
| `reply_to_message_id` | assistant가 답하는 user message PK |
| `processing_status` | `pending`, `completed`, `retryable_failed`, `final_failed` |

필수 제약:

- `(ai_request_id, role)`은 `ai_request_id`가 비어 있지 않을 때 유일해야 한다.
- `(conversation_id, id)` 조회 인덱스가 필요하다.
- assistant의 `reply_to_message_id`는 같은 환자·대화의 user message를 가리켜야 한다.

### 4.2 변경 가능한 업무 테이블

다음 레코드에 `version INTEGER NOT NULL DEFAULT 1`이 필요하다.

- `dose_events`
- `nutrition_meals`
- `nutrition_foods`
- `reminder_policies`

성공적인 update마다 `version = version + 1`로 변경한다. AI 요청의 `expected_version`과 현재 버전이 다르면 변경하지 않고 `VERSION_CONFLICT`를 반환한다.

외부 계약에서 식별되는 다음 테이블에는 내부 숫자 PK와 별도의 공개 식별자를 추가한다.

| 테이블 | 공개 ID 예시 |
|---|---|
| `chat_messages` | `user_msg_100`, `assistant_msg_200` |
| `dose_events` | `dose_120` |
| `nutrition_meals` | `meal_42` |
| `nutrition_foods` | `food_51` |
| `reminder_policies` | `npol_0123456789abcdef0123456789abcdef` |

- 모든 `public_id`는 문자열, NOT NULL, UNIQUE이며 외부 API와 AI Tool에서 사용하는 불투명 ID이다.
- 숫자형 PK는 Backend 내부 FK·정렬·처리에만 사용하며 AI Server 응답과 LLM에 노출하지 않는다.
- 기존 row에는 배포 migration에서 충돌하지 않는 공개 ID를 backfill한다.
- 전환 기간에는 기존 숫자 ID의 문자열 입력을 안전하게 조회할 수 있지만 응답은 항상 공개 ID를 반환한다.
- 정책 조회 Query Tool과 정책 변경 API는 모두 동일한 `public_id`를 `policy_id`로 사용한다.

### 4.3 Backend 쓰기 멱등성 테이블

테스트베드는 `backend_api_requests`를 사용한다.

- 유일 범위: `(api_path, request_id)`
- 같은 ID + 같은 body + 완료: 저장된 HTTP status와 body 그대로 replay
- 같은 ID + 다른 body: `409 IDEMPOTENCY_CONFLICT`
- 같은 ID + 처리 중: `409 REQUEST_IN_PROGRESS`
- 업무 변경과 멱등성 완료 기록은 하나의 DB transaction으로 커밋

## 5. AI Server가 호출할 Backend 쓰기 API

AI가 쓰기 필요성을 판단하면 모델이 아래 쓰기 Tool을 호출한다. 모델은 대상과 변경값 같은 업무 인자만 생성한다. 각 Tool 내부의 Backend interaction point가 신뢰된 채팅 컨텍스트와 Read 전용 조회 결과로 기술 인자를 채운 뒤 해당 API를 호출하며, Agent의 같은 처리 turn에서 응답을 기다리는 동기 request-response 방식으로 실행한다. 별도 queue나 callback worker가 Backend 쓰기를 대신 실행하지 않는다.

현재 v1.2 동기 쓰기 Tool 목록:

| AI Tool | 모델 필수 인자 | Backend API | 외부 API `expected_version` |
|---|---|---|---|
| `update_medication_dose_event_status` | `dose_event_id` | `/agent/sync/record-change` | Tool이 조회·주입 |
| `create_nutrition_meal_record` | `meal_type`, `foods` | `/agent/sync/record-change` | 없음 |
| `update_nutrition_meal_record` | `meal_id` + 변경 필드 | `/agent/sync/record-change` | Tool이 조회·주입 |
| `delete_nutrition_meal_record` | `meal_id` | `/agent/sync/record-change` | Tool이 조회·주입 |
| `update_nutrition_food_record` | `meal_id`, `food_id` + 변경 필드 | `/agent/sync/record-change` | Tool이 조회·주입 |
| `delete_nutrition_food_record` | `meal_id`, `food_id` | `/agent/sync/record-change` | Tool이 조회·주입 |
| `change_notification_policy` | 공개 `policy_id`, `decision` + 선택적 `changes` | `/agent/sync/notification-policy-change` | Tool이 조회·주입 |

`dose_event_id`, `meal_id`, `food_id`, `policy_id`는 모두 공개 opaque 문자열이다.

모델에 노출하지 않고 AI Server Tool이 관리하는 필드:

- `patient_id`: 검증된 현재 채팅 컨텍스트에서 주입
- `expected_version`: 대상 row를 Read 전용 Query Tool로 조회한 직후 주입
- `request_id`: 원본 채팅 요청·Tool 이름·Tool call ID·업무 인자를 정규화하여 생성
- `source_chat_request_id`, `conversation_id`, `confirmation_message_id`: Backend가 전달하고 AI Server가 검증한 채팅 컨텍스트에서 주입
- `requested_at`: 현재 요청 컨텍스트에서 주입
- `reason`: 사용자 메시지에서 명시적으로 확인된 Tool 실행이라는 표준 감사 사유를 주입

모든 v1.2 쓰기 Tool의 입력 JSON Schema는 최상위 및 정의된 중첩 객체에 `additionalProperties: false`를 사용한다. 따라서 모델이 위 기술 필드나 정의되지 않은 임의 필드를 추가하면 Backend 호출 전에 거절한다.

Backend 개발팀 전달용 기계 판독 계약은 `docs/BACKEND_V12_WRITE_OPENAPI.json`이다. `python tools/export_backend_v12_openapi.py`로 현재 FastAPI 모델에서 다시 생성할 수 있으며 자동화 테스트가 저장된 파일과 실제 스키마의 일치를 검증한다.

`request_id`는 `source_chat_request_id`, Tool 이름, Tool call ID 및 정규화된 인자 hash로 AI Server가 결정한다. 동일 Tool call을 재시도할 때 같은 `request_id`와 같은 body를 사용한다. 내부 `trace_id`는 Backend 계약으로 전달하지 않는다.

`confirmation_message_id`는 별도의 AI 내부 action ID가 아니라, 현재 Tool 실행을 발생시킨 Backend DB 사용자 메시지의 `public_id`를 사용한다. Backend는 해당 메시지와 환자·대화·원본 요청의 관계를 검증한다.

`create_medication_side_effect_record`, `upsert_nutrition_preference_fact`, `propose_system_policy`는 현재 v1.2 Backend 쓰기 계약이 지원하지 않으므로 위 목록에 포함하지 않는다. 필요할 경우 Backend 리소스와 payload 계약을 먼저 확장한다.

### 5.1 레코드 변경

- `POST /agent/sync/record-change`
- Bearer 인증 필수
- 지원 리소스:
  - `nutrition_meal`: create, update, delete
  - `nutrition_food`: update, delete
  - `medication_dose_event`: update(`taken`)만 지원

Backend는 다음을 검증해야 한다.

- `confirmation_message_id`가 Backend DB의 실제 user message 공개 ID인지
- 해당 메시지의 `patient_id`, `conversation_id`가 요청과 같은지
- `source_chat_request_id`가 같은 환자·대화의 실제 AI 채팅 요청인지
- update/delete의 `expected_version`이 현재 버전과 같은지
- `record_id`, `parent_record_id`의 소유 관계가 올바른지

### 5.2 알림 정책 변경

- `POST /agent/sync/notification-policy-change`
- Bearer 인증 필수
- `policy_id`는 숫자형 DB PK가 아니라 `reminder_policies.public_id`
- AI Server는 변경 전에 `get_notification_policies` Read Tool로 공개 ID와 현재 정책을 조회
- `decision=apply`: 검증 후 변경하고 version 증가
- `decision=keep`: 변경하지 않고 현재 version 반환
- `expected_version` 불일치 시 `409 VERSION_CONFLICT`

모델이 변경할 수 있는 `changes` 필드는 다음으로 제한한다.

| 필드 | 허용값 |
|---|---|
| `extra_reminders` | 0~5 |
| `interval_minutes` | 5~60 |
| `missed_dose_after_minutes` | 15~240 |
| `primary_reminder_timing` | `before`, `at`, `after` |
| `primary_reminder_offset_minutes` | 0~120, `at`이면 0 |
| `effective_start_date` | `YYYY-MM-DD`, 명시적으로 변경할 때 요청일보다 과거 금지 |
| `effective_end_date` | `YYYY-MM-DD`, 시작일 이상, 전체 기간 최대 365일 |

`policy_key`, `slot_label`, 알림 문구 template, `source`, `active`, `version`은 모델 변경 대상이 아니다. Backend는 부분 변경값을 현재 정책과 합친 뒤 정책별 경계와 “마지막 추가 알림 시각 ≤ 미복용 판단 시각” 교차 규칙을 재검증한다. 위반 시 `422 POLICY_BOUNDARY_VIOLATION` 또는 구체적인 날짜 오류 코드를 반환한다.

### 5.3 오류 응답

모든 오류는 HTTP status와 함께 규격의 다음 구조를 반환한다.

Bearer 인증 실패와 Pydantic Request schema 검증 실패도 FastAPI 기본 `detail` 응답을 사용하지 않고 동일한 `success/result/error/processed_at` 계약을 반환한다. 스키마 검증 실패의 `request_id`는 유효한 본문에 포함된 값을 유지하며 오류 코드는 `INVALID_REQUEST`이다.

```json
{
  "success": false,
  "request_id": "동일 요청 ID",
  "result": null,
  "error": {
    "code": "VERSION_CONFLICT",
    "message": "VERSION_CONFLICT",
    "retryable": false,
    "details": {
      "expected_version": 1,
      "current_version": 2
    }
  },
  "processed_at": "2026-07-25T15:00:00+09:00"
}
```

권장 HTTP status:

| 상황 | status |
|---|---:|
| 성공·완료 replay | 200 |
| 리소스 없음 | 404 |
| 멱등성·version·확인 메시지 충돌 | 409 |
| 계약 또는 업무 검증 실패 | 422 |
| 일시적 DB·Backend 장애 | 503 또는 504 |
| 예기치 않은 서버 오류 | 500 |

## 6. Backend DB read-only 접근 요구

Backend 개발팀은 AI Server 전용 DB 계정을 제공해야 한다.

- 허용: 계약에 선언된 `ai_v12_*` view의 `SELECT`
- 금지: `INSERT`, `UPDATE`, `DELETE`, DDL, procedure 실행
- 금지: Backend base table 직접 `SELECT`
- Backend writer 계정과 자격 증명을 공유하지 않음
- AI용 view로 노출 컬럼과 환자 범위를 제한
- 접속 정보는 AI Server secret으로 전달
- 연결 실패와 slow query를 Backend·AI 양쪽에서 관측 가능하게 구성

AI Server Query Tool은 정해진 함수와 parameterized query만 사용한다. LLM이 SQL 문자열, 테이블명, DB 자격 증명, DB session을 생성하거나 전달할 수 없어야 한다.

`ai_v12_legacy_id_map`은 배포 전 이미 대기 중이던 숫자 문자열 ID를 공개 ID로
변환하는 전환 전용 view이다. 고정 Tool 코드만 사용하며 `legacy_id`는 Tool 응답이나
모델 컨텍스트에 포함하지 않는다.

테스트베드 SQLite에서는 다음 설정으로 쓰기 차단을 검증한다.

```text
PRAGMA query_only = ON
```

SQLite 테스트베드도 프로세스 내부 설정만 믿지 않고 런타임 저장소를 분리한다.

| 실행 주체 | 자체 RW 저장소 | 타 서비스 저장소 접근 |
|---|---|---|
| Backend `system-app` | `system-runtime`의 `system.db` | 없음 |
| AI `agent-app`, `agent-worker` | 공동 소유 `agent-runtime`의 `agent.db` | `system-runtime`을 `/app/backend-read`에 `ro` mount |
| PHR `phr-app` | `phr-runtime`의 `phr.db` | 없음 |

AI의 SQLite 조회 URL은
`sqlite:///file:/app/backend-read/system.db?mode=ro&uri=true`를 사용한다.
따라서 Compose mount의 `read_only`, SQLite URI의 `mode=ro`, 연결 시
`PRAGMA query_only = ON`의 세 겹으로 쓰기를 차단한다. 개발용 Compose도 프로젝트
루트 전체를 `/app`에 mount하지 않고 서비스별 소스 디렉터리만 read-only로 mount한다.
`./data` RW bind는 Backend 테스트베드의 운영·정책 artifact용으로 `system-app`만 가진다.
기동 순서는 `PHR → Backend migration/health → AI API → AI worker`로 고정해, AI의
read-contract view 검증이 Backend migration보다 먼저 실행되는 race와 상호 의존 cycle을 막는다.

운영 PostgreSQL에서는 DB role의 SELECT-only 권한이 최종 통제이며 AI 연결 session도 read-only로 설정한다.

## 7. 인증과 환경 설정

| 방향 | 인증 | 설정 |
|---|---|---|
| Backend → AI `/agent/sync/chat` | Bearer | 양쪽 `AGENT_SYNC_API_TOKEN` 동일 |
| Backend → AI `/agent/async/chat_feedback` | Bearer | 양쪽 `AGENT_SYNC_API_TOKEN` 동일 |
| AI → Backend 쓰기 API | Bearer | 양쪽 `BACKEND_API_TOKEN` 동일 |
| AI → Backend DB 조회 | DB SELECT-only 계정 | AI `BACKEND_READ_DATABASE_URL` |

테스트베드 주요 설정:

```dotenv
APP_ENV=testbed
SYSTEM_DATABASE_URL=sqlite:///./runtime/system/system.db
AGENT_DATABASE_URL=sqlite:///./runtime/agent/agent.db
PHR_DATABASE_URL=sqlite:///./runtime/phr/phr.db
BACKEND_READ_DATABASE_URL=sqlite:///file:./runtime/system/system.db?mode=ro&uri=true
BACKEND_QUERY_MAX_ROWS=100
BACKEND_RECORD_CHANGE_PATH=/agent/sync/record-change
BACKEND_NOTIFICATION_POLICY_CHANGE_PATH=/agent/sync/notification-policy-change
AGENT_SYNC_API_TOKEN=replace-with-shared-secret
AGENT_SYNC_MAX_RETRIES=2
BACKEND_API_TOKEN=replace-with-a-different-shared-secret
AGENT_FEEDBACK_ENCRYPTION_KEY=replace-with-urlsafe-base64-encoded-32-byte-key
AGENT_FEEDBACK_ENCRYPTION_KEY_ID=feedback-v1
LLM_PROVIDER=rule_based
```

`rule_based` provider는 `APP_ENV=test|testing|testbed`에서만 허용된다. 운영 환경에서는
두 토큰을 서로 다른 값으로 사용하고 secret manager를 통해 주입하며 승인된 외부
모델 provider를 사용한다.

## 8. Backend 개발팀 인수 테스트

개발팀 전달 전후에 최소한 다음을 통과해야 한다.

1. user message가 커밋되기 전에 AI 호출이 발생하지 않는다.
2. AI가 받은 `message_id`로 Backend DB user message를 조회할 수 있다.
3. 완료된 AI 요청을 동일 `request_id`로 재전송하면 Agent가 재실행되지 않고 같은 응답이 반환된다.
4. 같은 `request_id`에 다른 body를 보내면 409가 반환된다.
5. 같은 AI 응답을 Backend가 두 번 받아도 assistant 메시지는 한 건이다.
6. `expected_version`이 오래된 update는 업무 데이터를 바꾸지 않는다.
7. 확인 메시지 공개 ID·환자·대화가 일치하지 않으면 쓰기가 거절된다.
8. AI DB 장애가 Backend 업무 원장을 손상시키지 않는다.
9. AI DB 계정으로 Backend DB 쓰기를 시도하면 DB 수준에서 거절된다.
10. 외부 요청·응답과 Backend 업무 테이블에 AI 내부 `trace_id`가 저장되지 않는다.
11. `selection_box` 버튼 입력이 같은 conversation의 새 user message로 저장된다.
12. `input_box` 입력이 JSON 객체 문자열로 저장·전달된다.
13. 정책 조회 결과와 정책 변경 응답의 `policy_id`가 동일한 공개 ID이며 숫자형 DB PK가 노출되지 않는다.
14. 모델이 `patient_id`, `expected_version`, `reason` 또는 정의되지 않은 추가 속성을 쓰기 Tool 인자로 전달하면 Backend 호출 전에 거절된다.
15. 정책별 경계나 교차 규칙을 위반한 변경은 version과 정책 데이터를 바꾸지 않는다.
16. OpenAPI에 `BackendApiBearer` security scheme과 401·404·409·422·500·503·504 계약 응답이 선언된다.
17. 인증 실패와 Request schema 검증 실패도 공통 오류 응답 구조를 유지한다.
18. AI 컨테이너에서 Backend SQLite mount는 read-only이고 실제 write probe가 거절된다.
19. Backend·AI·PHR의 자체 DB URL과 RW volume이 서비스 소유권별로 분리된다.
20. 저장된 Backend v1.2 OpenAPI와 현재 코드에서 생성한 OpenAPI가 일치한다.
21. AI 채팅 OpenAPI는 실제 400 변환 동작과 일치하며 401·404·409·500·503·504를 선언한다.
22. AI DB 조회 계정은 `ai_v12_*` view만 SELECT할 수 있고 raw Backend table과 모든 쓰기가 거절된다.
23. AI Server 재시작·동시 실행·LLM `tool_call_id` 재생성 후에도 최초 canonical `request_id`와 `expected_version`을 재사용한다.
24. terminal Backend 응답은 늦은 retryable 응답이나 transport 실패로 덮어쓸 수 없다.
25. `system_app`은 `agent_app` 런타임을 import하지 않고 양쪽 공용 계약은 `shared`에서만 소유한다.
26. message·dose event·meal·food 조회 및 쓰기 응답은 공개 ID만 반환하며 숫자 문자열 입력은 전환 호환으로만 처리된다.
27. 대기 중인 구조화 응답과 다른 `requested_return_type`, 잘못된 선택지·입력 필드, 중복·오래된 응답은 거절된다.
28. `tables`, `selection_box`, 숫자·dropdown `input_box`가 Backend 저장과 화면 왕복 과정에서 유실되지 않는다.
29. 유효한 AI 답변 피드백은 `202 {"status":"accepted"}`를 반환하고 동일 본문은 중복 저장 없이 replay된다.
30. 같은 피드백 `request_id`에 다른 본문을 보내면 `409 IDEMPOTENCY_CONFLICT`이며 user 메시지나 다른 환자·대화의 메시지는 피드백 대상으로 인정하지 않는다.
31. `feedback_text`는 AES-256-GCM 암호문으로만 AI DB에 저장되고 키 누락·오류 시 Agent startup과 readiness가 fail-closed 된다.

자동화 명령:

```powershell
# Compose/9000 경계와 저장된 OpenAPI drift를 함께 검사
python tools/verify_testbed_contract.py

# 실행 중인 9000 stack에서 DB 경계와 실제 Backend→AI HTTP 흐름까지 검사
python tools/verify_testbed_contract.py --runtime

# Compose 문법과 merge 결과 검사
docker compose -f docker-compose.yml -f docker-compose.dev.yml config --quiet
```

`tools/da_drug_9000_stack.ps1 verify`는 health/worker 점검 후 위 runtime 검사를
실행하고 `outputs/runtime/9000/verify.json`의 `boundary_contract`에 증거를 남긴다.
AI readiness는 단순 liveness `/health`가 아니라 DB·read contract·양방향 인증 설정을
검사하는 `/health/ready`의 `status=ready`를 배포 및 9000 기동 gate로 사용한다.
HTTP probe는 `Backend /api/chat/sync → AI /agent/sync/chat → Backend read-only DB`
경로를 실제 호출하고, 동일 `request_id` replay, user/assistant ID 분리, `trace_id`
미노출, AI endpoint 직접 무인증 호출의 401을 확인한다. 이어서 저장된 assistant
공개 ID로 `POST /agent/async/chat_feedback`을 호출해 최초 202, 동일 본문 replay
202, 동일 `request_id`의 다른 본문 409까지 실제 HTTP 경계에서 확인한다.
CI에서는 `.github/workflows/testbed-boundary-contract.yml`이 Compose 확장, 경계 단위
테스트, OpenAPI drift, Backend v1.2 계약 회귀 테스트를 PR마다 실행한다. CI의
PostgreSQL 16 service에서는 별도 AI reader role을 생성해 view SELECT만 허용되고
raw table 조회·DML·DDL이 모두 거절되는지도 실제 권한으로 검증한다.

## 9. 테스트베드 검증 결과

2026-07-26 기준:

- 전체 회귀 테스트: `531 passed, 4 skipped, 4 warnings`
- 공개 ID·구조화 응답·피드백·OpenAPI·migration 통합 묶음:
  `113 passed, 1 warning`
- 피드백 runtime probe 추가 후 경계·OpenAPI·피드백 회귀:
  `27 passed, 1 warning`
- 실제 PostgreSQL 18 임시 클러스터의 AI reader role 경계 테스트:
  `1 passed`(skip 없음)
  - `ai_v12_*` view SELECT 허용
  - raw Backend table 조회, DML, DDL 거절
  - 종료 후 PostgreSQL 프로세스와 listener가 남지 않음을 확인
- 실제 로컬 9000 스택 검증: `ok=true`
  - Backend, AI, PHR health 정상
  - AI readiness의 Agent DB, Backend read DB, Backend write API, 동기 인증,
    피드백 암호화 구성 모두 `OK`
  - Backend 채팅 200 및 동일 요청 replay
  - `user_msg_*`, `assistant_msg_*` 공개 ID 왕복과 `trace_id` 비노출
  - AI 채팅 endpoint 무인증 직접 호출 401
  - 피드백 최초 202, 동일 본문 replay 202, 다른 본문 충돌 409
  - SQLite Backend read 연결의 실제 write probe 거절
- v1.2 구현 회귀 범위:
  - 메시지·복약·식사·음식 내부 PK와 외부 공개 ID 분리
  - 기존 데이터 공개 ID backfill과 전환 기간 숫자 ID 조회 호환
  - 선택형·입력형·테이블형 응답의 저장, 검증, 화면 왕복
  - 대기 요청 타입, 중복·오래된 응답 및 허용되지 않은 입력 거절
  - Backend 쓰기 멱등성, version 충돌, 확인 메시지 및 정책 경계 검증
  - Tool 기술 인자와 정의되지 않은 추가 속성 차단
  - 피드백 대상 assistant 메시지·환자·대화 검증
  - 피드백 자유문 AES-256-GCM 암호화, 로그 비노출, 재처리·보존 상태
  - 1초 polling time-bar의 전체 대시보드 조회를 최소 전용 조회로 분리하고,
    pool size 2에서 동시 50요청 및 연결 전량 반환 검증
  - Backend/AI OpenAPI artifact drift 검사
- 실제 System DB migration:
  - schema migration 총 33개
  - `20260725_0005_external_public_ids` 적용
  - 메시지·복약·식사·음식 공개 ID 컬럼, backfill 및 UNIQUE index 적용
  - 이전 `reminder_policies.public_id` 공개 ID migration 유지

공개 정책 ID migration 전 일관된 스냅샷:

`data/backups/system-before-policy-public-id-20260725.db`
