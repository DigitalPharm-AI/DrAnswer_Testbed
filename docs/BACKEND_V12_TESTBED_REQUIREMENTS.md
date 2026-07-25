# Backend v1.2 테스트베드 구현 및 개발팀 요구사항

## 1. 목적과 현재 상태

이 문서는 AI Server 연동규격 v1.2를 실제 Backend 개발팀에 요구하기 전에 `system_app` 테스트베드에서 먼저 검증한 구현을 기준으로 한다.

테스트베드에 구현된 범위는 다음과 같다.

- Backend가 사용자 메시지를 DB에 먼저 저장하고 DB PK를 `message_id`로 AI Server에 전달
- AI Server `/agent/sync/chat` 동기 호출 및 동일 요청 재시도
- AI 응답을 별도 assistant 메시지로 Backend DB에 저장
- `text`, `selection_box`, `input_box`, 표 응답의 화면 표시 및 후속 입력
- AI Server의 Backend DB read-only Query Tools
- AI Server에서 Backend로 요청하는 레코드 변경·알림 정책 변경 API
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
    DB-->>B: message.id
    B->>A: POST /agent/sync/chat<br/>request_id + message_id
    A->>AD: request_id 멱등성 / conversation lock
    A->>DB: message_id·patient_id·conversation_id 검증(SELECT)
    A->>DB: 필요한 업무 데이터 조회(SELECT)
    A-->>B: v1.2 ChatSyncResponse
    B->>DB: assistant message INSERT<br/>reply_to_message_id=user.id
    B-->>C: Backend assistant_message_id + 응답 payload
```

Backend 필수 규칙:

1. AI 호출 전에 사용자 메시지를 반드시 커밋한다.
2. AI 요청의 `message_id`는 방금 저장한 사용자 메시지의 Backend DB PK 문자열이다.
3. `request_id`는 Backend가 생성하며, timeout·503·504 재시도에도 본문과 함께 그대로 재사용한다.
4. AI 응답의 `message_id`는 사용자 메시지 ID의 echo이므로 assistant 메시지 ID로 사용하지 않는다.
5. assistant 메시지는 Backend가 별도로 저장하고 자체 DB PK를 Client에 반환한다.
6. 같은 `request_id`의 AI 응답을 재수신해도 assistant 메시지는 한 건만 저장한다.

테스트베드 Client 진입 API는 `POST /api/chat/sync`이다. 실제 Backend의 Client API 경로는 개발팀 표준을 따르되 위 처리 순서는 동일해야 한다.

## 4. Backend DB 요구 스키마

### 4.1 chat_messages

기존 필드 외에 다음 필드가 필요하다.

| 필드 | 용도 |
|---|---|
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

### 4.3 Backend 쓰기 멱등성 테이블

테스트베드는 `backend_api_requests`를 사용한다.

- 유일 범위: `(api_path, request_id)`
- 같은 ID + 같은 body + 완료: 저장된 HTTP status와 body 그대로 replay
- 같은 ID + 다른 body: `409 IDEMPOTENCY_CONFLICT`
- 같은 ID + 처리 중: `409 REQUEST_IN_PROGRESS`
- 업무 변경과 멱등성 완료 기록은 하나의 DB transaction으로 커밋

## 5. AI Server가 호출할 Backend 쓰기 API

### 5.1 레코드 변경

- `POST /agent/sync/record-change`
- Bearer 인증 필수
- 지원 리소스:
  - `nutrition_meal`: create, update, delete
  - `nutrition_food`: update, delete
  - `medication_dose_event`: update(`taken`)만 지원

Backend는 다음을 검증해야 한다.

- `confirmation_message_id`가 Backend DB의 실제 user message PK인지
- 해당 메시지의 `patient_id`, `conversation_id`가 요청과 같은지
- `source_chat_request_id`가 같은 환자·대화의 실제 AI 채팅 요청인지
- update/delete의 `expected_version`이 현재 버전과 같은지
- `record_id`, `parent_record_id`의 소유 관계가 올바른지

### 5.2 알림 정책 변경

- `POST /agent/sync/notification-policy-change`
- Bearer 인증 필수
- `decision=apply`: 검증 후 변경하고 version 증가
- `decision=keep`: 변경하지 않고 현재 version 반환
- `expected_version` 불일치 시 `409 VERSION_CONFLICT`

### 5.3 오류 응답

모든 오류는 HTTP status와 함께 규격의 다음 구조를 반환한다.

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

- 허용: 승인된 테이블·view의 `SELECT`
- 금지: `INSERT`, `UPDATE`, `DELETE`, DDL, procedure 실행
- Backend writer 계정과 자격 증명을 공유하지 않음
- 가능하면 AI용 view로 노출 컬럼과 환자 범위를 제한
- 접속 정보는 AI Server secret으로 전달
- 연결 실패와 slow query를 Backend·AI 양쪽에서 관측 가능하게 구성

AI Server Query Tool은 정해진 함수와 parameterized query만 사용한다. LLM이 SQL 문자열, 테이블명, DB 자격 증명, DB session을 생성하거나 전달할 수 없어야 한다.

테스트베드 SQLite에서는 다음 설정으로 쓰기 차단을 검증한다.

```text
PRAGMA query_only = ON
```

운영 PostgreSQL에서는 DB role의 SELECT-only 권한이 최종 통제이며 AI 연결 session도 read-only로 설정한다.

## 7. 인증과 환경 설정

| 방향 | 인증 | 설정 |
|---|---|---|
| Backend → AI `/agent/sync/chat` | Bearer | 양쪽 `AGENT_SYNC_API_TOKEN` 동일 |
| AI → Backend 쓰기 API | Bearer | 양쪽 `BACKEND_API_TOKEN` 동일 |
| AI → Backend DB 조회 | DB SELECT-only 계정 | AI `BACKEND_READ_DATABASE_URL` |

테스트베드 주요 설정:

```dotenv
BACKEND_READ_DATABASE_URL=sqlite:///./data/system.db
BACKEND_QUERY_MAX_ROWS=100
BACKEND_RECORD_CHANGE_PATH=/agent/sync/record-change
BACKEND_NOTIFICATION_POLICY_CHANGE_PATH=/agent/sync/notification-policy-change
AGENT_SYNC_API_TOKEN=replace-with-shared-secret
AGENT_SYNC_MAX_RETRIES=2
BACKEND_API_TOKEN=replace-with-different-shared-secret
```

운영 환경에서는 두 토큰을 서로 다른 값으로 사용하고 secret manager를 통해 주입한다.

## 8. Backend 개발팀 인수 테스트

개발팀 전달 전후에 최소한 다음을 통과해야 한다.

1. user message가 커밋되기 전에 AI 호출이 발생하지 않는다.
2. AI가 받은 `message_id`로 Backend DB user message를 조회할 수 있다.
3. 완료된 AI 요청을 동일 `request_id`로 재전송하면 Agent가 재실행되지 않고 같은 응답이 반환된다.
4. 같은 `request_id`에 다른 body를 보내면 409가 반환된다.
5. 같은 AI 응답을 Backend가 두 번 받아도 assistant 메시지는 한 건이다.
6. `expected_version`이 오래된 update는 업무 데이터를 바꾸지 않는다.
7. 확인 메시지 PK·환자·대화가 일치하지 않으면 쓰기가 거절된다.
8. AI DB 장애가 Backend 업무 원장을 손상시키지 않는다.
9. AI DB 계정으로 Backend DB 쓰기를 시도하면 DB 수준에서 거절된다.
10. 외부 요청·응답과 Backend 업무 테이블에 AI 내부 `trace_id`가 저장되지 않는다.
11. `selection_box` 버튼 입력이 같은 conversation의 새 user message로 저장된다.
12. `input_box` 입력이 JSON 객체 문자열로 저장·전달된다.

## 9. 테스트베드 검증 결과

2026-07-25 기준:

- 전체 자동화 테스트: `431 passed, 3 skipped`
- 신규 v1.2 테스트:
  - 메시지 선저장과 별도 assistant ID
  - 동일 채팅 요청 replay
  - 확인 메시지 검증
  - version 증가와 충돌
  - Backend 쓰기 멱등성
  - SQLite Query Tool 쓰기 차단
  - 화면 input box JSON 변환과 동일 conversation 유지
- 실제 `data/system.db` migration:
  - schema migration 총 32개
  - `PRAGMA integrity_check = ok`
  - journal mode `wal`

일관된 migration 후 스냅샷:

`data/backups/system-backend-v12-consistent-20260725-155948.db`
