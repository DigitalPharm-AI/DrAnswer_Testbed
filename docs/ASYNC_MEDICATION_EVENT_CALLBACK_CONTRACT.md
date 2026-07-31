# 미복용·일일 패턴 비동기 연동 계약

- 기준: 닥터앤서 AI모듈 API 연동규격서 v1.3
- 적용 범위: Backend Server↔AI Server 외부 비동기 API
- 원칙: Backend는 미복용 이벤트와 매일 02:00 일일 패턴 분석을 접수
  요청한다. AI Server는 정책 변경 제안이 있을 때만 Backend Callback을
  호출한다.

## 1. 공통 원칙

- 외부 요청과 Callback은 `Authorization: Bearer {token}`을 사용한다.
- 공개 식별자는 소문자 16진수 16자리 형식을 사용한다.
  - `request_id`: `req_[0-9a-f]{16}`
  - `patient_id`: `patient_[0-9a-f]{16}`
  - `dose_event_id`: `dose_[0-9a-f]{16}`
- 최초 요청자가 `request_id`를 발급하며, 네트워크 재시도에는 같은
  `request_id`와 같은 본문을 사용한다.
- 같은 `request_id`와 같은 본문은 기존 처리 결과를 반환한다.
- 같은 `request_id`가 다른 본문으로 재사용되면 HTTP 409
  `IDEMPOTENCY_CONFLICT`를 반환한다.
- 계약에 없는 필드는 허용하지 않는다.
- 외부 Error Response는 항상 `{request_id, error}` 형식이다.

```json
{
  "request_id": "req_1111111111111111",
  "error": {
    "code": "INVALID_REQUEST",
    "message": "Request schema or required field is invalid.",
    "retryable": false,
    "details": null
  }
}
```

AI 내부 `trace_id`, Agent 이름, 판단 유형, queue task 종류·상태는 외부
오류 응답에 포함하지 않는다. 실제 실패 원인과 Trace는 AI Internal DB에
보존한다.

## 2. 미복용 이벤트 접수

### 2.1 API

| 구분 | 내용 |
| --- | --- |
| Method | `POST` |
| 방향 | Backend Server → AI Server |
| Path | `/agent/async/missed-dose-events` |
| Request Content-Type | `application/json` |
| 성공 상태 | 신규 `202 Accepted`, 동일 재요청 `200 OK` |

### 2.2 Request

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | ---: | --- |
| `request_id` | string | O | Backend가 발급한 비동기 요청 ID |
| `patient_id` | string | O | 대상 환자 공개 ID |
| `dose_event_id` | string | O | 미복용 대상 복약 이벤트 공개 ID |

```json
{
  "request_id": "req_1111111111111111",
  "patient_id": "patient_aaaaaaaaaaaaaaaa",
  "dose_event_id": "dose_bbbbbbbbbbbbbbbb"
}
```

Backend는 약명, 시각, 정책, 대화 이력 같은 Snapshot을 요청 본문에 복제하지
않는다. AI worker가 `patient_id`와 `dose_event_id`의 소유 관계를 검증하고,
승인된 Backend Read-only View에서 현재 문맥과 해당 `request_id`에 저장된
A~E 복약 패턴·톤·사전 정의 메시지 정책을 조회한다. 대상이 없거나 환자
범위가 다르거나 정책 문맥이 없으면 fail-closed로 처리한다. 이 내부 조회
확장은 접수 본문의 세 필드 규격을 변경하지 않는다.

### 2.3 Response

| 필드 | 타입 | 필수 | 값 |
| --- | --- | ---: | --- |
| `request_id` | string | O | Request와 동일 |
| `status` | string | O | `accepted` 또는 `duplicate` |

```json
{
  "request_id": "req_1111111111111111",
  "status": "accepted"
}
```

접수 응답은 작업 완료를 의미하지 않는다. 최종 성공·실패는 다음 전용
Callback으로 전달한다.

## 3. 미복용 처리 결과 Callback

### 3.1 API

| 구분 | 내용 |
| --- | --- |
| Method | `POST` |
| 방향 | AI Server → Backend Server |
| Path | `/api/agent/async/missed-dose-results` |
| Request Content-Type | `application/json` |
| 성공 상태 | `200 OK` |

### 3.2 Request

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | ---: | --- |
| `request_id` | string | O | 접수 요청의 `request_id` |
| `status` | string | O | `completed` 또는 `failed` |
| `result` | object \| null | O | 성공 시 결과, 실패 시 null |
| `error` | object \| null | O | 실패 시 Error Object, 성공 시 null |

`completed`일 때 `result`는 다음 두 필드만 가진다.

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | ---: | --- |
| `message` | string | O | 사용자에게 제시할 미복용 후속 메시지 |
| `requires_reply` | boolean | O | 사용자 응답이 필요한지 여부 |

AI Server의 미복용 이벤트 처리는 LLM·Tool을 호출하지 않고 Read-only
정책 문맥의 사전 정의 메시지를 검증해 반환한다. Backend는 Callback 처리
시 현재 A~E 패턴과 톤을 다시 계산하고, 최종 알림·채팅 본문에는 Backend
카탈로그 메시지를 적용한다. 환자가 이후 동기 채팅에서 증상을 직접 말한
경우에만 별도의 복약 Agent가 부작용·PRO-CTCAE Tool 흐름을 시작한다.

```json
{
  "request_id": "req_1111111111111111",
  "status": "completed",
  "result": {
    "message": "아직 복약 기록이 확인되지 않았어요. 현재 상태를 알려주세요.",
    "requires_reply": true
  },
  "error": null
}
```

`failed`일 때는 내부 예외 문구나 Trace 문맥 대신 일반화된 Error Object를
보낸다.

```json
{
  "request_id": "req_1111111111111111",
  "status": "failed",
  "result": null,
  "error": {
    "code": "AI_PROCESSING_ERROR",
    "message": "An internal AI Server processing error occurred.",
    "retryable": true,
    "details": null
  }
}
```

### 3.3 Ack

| 필드 | 타입 | 필수 | 값 |
| --- | --- | ---: | --- |
| `request_id` | string | O | Callback과 동일 |
| `status` | string | O | `processed` 또는 `duplicate` |

```json
{
  "request_id": "req_1111111111111111",
  "status": "processed"
}
```

Backend는 원 요청과 Callback을 `request_id`로 상관관계 검증하고, 대상
복약 이벤트는 원 요청에 저장된 연결 정보로 찾는다. 충돌 응답에는 내부
`job_type`이나 `job_status`를 노출하지 않는다.

## 4. Backend 기동 일일 패턴 분석

| 구분 | 내용 |
| --- | --- |
| Method | `POST` |
| 방향 | Backend Server → AI Server |
| Path | `/agent/async/daily-medication-pattern-analysis` |
| 실행 시각 | 매일 02:00, Asia/Seoul |
| 성공 상태 | 신규 `202 Accepted`, 동일 재요청 `200 OK` |

요청 본문은 `request_id`, 분석 대상 `patient_id` 배열, `analysis_date`
세 필드만 포함한다. Backend는 `analysis_date`에 유효한 활성 복약 환자를
조회하고, 날짜별 결정적 `request_id`로 전송 상태를 보존한다. 테스트베드는
시뮬레이션 시각 02:00을 기준으로 직전 날짜를 분석일로 사용한다.

AI Server는 접수된 환자별 작업을 내부 queue에 등록한다. Worker가 승인된
Backend Read-only View에서 최근 문맥과 현재 정책을 조회해 분석하며, 제안이
없으면 Callback을 만들지 않는다. 제안이 있으면 08:30에 정책 변경 제안
Callback을 호출한다. 분석·전달 실패의 세부 Trace는 AI Internal DB에만
보존한다.

```json
{
  "request_id": "req_3333333333333333",
  "patient_id": [
    "patient_aaaaaaaaaaaaaaaa",
    "patient_bbbbbbbbbbbbbbbb"
  ],
  "analysis_date": "2026-07-28"
}
```

## 5. 알림 정책 변경 제안 Callback

### 5.1 API

| 구분 | 내용 |
| --- | --- |
| Method | `POST` |
| 방향 | AI Server → Backend Server |
| Path | `/api/agent/async/notification-policy-change-proposals` |
| 성공 상태 | 신규 `202 Accepted`, 동일 재요청 `200 OK` |

### 5.2 Request

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | ---: | --- |
| `request_id` | string | O | AI Server가 발급한 제안 요청 ID |
| `patient_id` | string | O | 제안 대상 환자 공개 ID |
| `proposed_policy` | object | O | 변경을 제안하는 정책 필드 |
| `reason` | string | O | 사용자에게 제시할 제안 근거 |

`proposed_policy`는 v1.3 동기 알림 정책 변경 계약과 같은 범위·검증 규칙을
재사용한다. 포함 가능한 필드는 다음과 같다.

- `extra_reminders`
- `interval_minutes`
- `missed_dose_after_minutes`
- `primary_reminder_timing`
- `primary_reminder_offset_minutes`
- `effective_start_date`
- `effective_end_date`

```json
{
  "request_id": "req_2222222222222222",
  "patient_id": "patient_aaaaaaaaaaaaaaaa",
  "proposed_policy": {
    "extra_reminders": 2,
    "interval_minutes": 20
  },
  "reason": "최근 7일 동안 아침 복약 누락이 반복되었습니다."
}
```

### 5.3 Ack

| 필드 | 타입 | 필수 | 값 |
| --- | --- | ---: | --- |
| `request_id` | string | O | Callback과 동일 |
| `status` | string | O | `accepted` 또는 `duplicate` |

제안 Callback은 정책을 직접 변경하지 않는다. Backend는 환자에게 확인할
대화/알림을 생성하고, 사용자가 승인한 뒤에만 v1.3 동기 알림 정책 쓰기
계약으로 실제 정책을 변경한다.

## 6. 검증 체크리스트

- [ ] 미복용 접수 본문은 세 필드만 허용한다.
- [ ] 신규/중복 미복용 접수는 각각 202/200을 반환한다.
- [ ] 동일 `request_id`의 다른 본문은 409를 반환한다.
- [ ] 미복용 Callback의 성공·실패 pair 검증을 수행한다.
- [ ] Callback ACK의 `request_id`를 AI Server가 검증한다.
- [x] 일일 패턴 분석은 Backend가 02:00에 접수 API를 호출해 시작한다.
- [ ] 정책 제안이 없으면 Backend Callback을 호출하지 않는다.
- [ ] 정책 제안은 Backend에서 사용자 승인 전 적용되지 않는다.
- [ ] worker의 내부 실패·Trace 문맥은 외부 응답에 노출하지 않는다.
