# React–Backend UI API v1

이 문서는 테스트베드 React 화면과 Backend 사이의 동일 출처 BFF 계약을
설명한다. Backend–AI Server v1.3 채팅·쓰기 계약은 변경하지 않는다.

## 공통 원칙

- 기본 경로는 `/api/ui/v1`이다.
- 공개 계약의 기준은 `docs/UI_V1_OPENAPI.json`이며
  `python tools/export_ui_v1_openapi.py --check`로 런타임 라우트와의
  드리프트를 검사한다.
- 브라우저는 `patient_id`, Agent Bearer token을 보내지
  않는다. Backend가 설정과 DB 문맥에서 결정한다.
- Backend는 브라우저 응답에도 `patient_id`, 내부 숫자 PK, 내부 `version`,
  trace 식별자를 내보내지 않는다. 화면 연결에 필요한 식별자는 Backend가
  발급한 불투명 공개 ID만 사용한다.
- 시간 표시는 `Asia/Seoul` offset을 포함한다.
- AI 처리 시각인 `message_at`은 보존하고, 시뮬레이션 대화 표시에는
  `display_message_at`을 별도로 사용한다.
- 요청 모델은 정의되지 않은 추가 속성을 거절한다.
- Backend는 성공 응답도 라우트별 Pydantic 응답 모델로 검증한 뒤
  직렬화한다. 저장된 멱등 replay 응답에도 같은 검증을 적용한다.
- 성공·실패는 다음 envelope를 사용한다.

```json
{
  "success": true,
  "data": {},
  "error": null
}
```

```json
{
  "success": false,
  "data": null,
  "error": {
    "code": "ERROR_CODE",
    "message": "설명",
    "retryable": false,
    "details": null
  }
}
```

`error.retryable`은 필수다. React는 이 값이 `true`인 실패에만 같은
`request_id`를 이용한 재시도를 제공한다. 네트워크 단절과 클라이언트
timeout은 재시도 가능, 계약을 해석할 수 없는 응답은 재시도 불가로
취급한다.

## 요청 시간 예산

- React의 동기 채팅 제한은 120초다.
- Backend의 Agent 동기 채팅 전체 예산은 100초다. 개별 시도, 지수 backoff,
  자동 재시도를 모두 이 예산 안에서만 수행한다.
- React의 일반 요청 제한은 20초이고, Backend의 Agent 피드백 전체 예산은
  15초다.
- 따라서 하위 호출이 브라우저 제한보다 오래 계속되는 것을 허용하지 않는다.
  예산을 초과한 요청은 재시도 가능한 timeout 오류로 반환한다.

## 화면 API

| Method | Path | 용도 |
|---|---|---|
| `GET` | `/dashboard` | 시계, 복약, 영양, 정책, 활성 알림 통합 조회 |
| `GET` | `/status` | Backend와 AI Server 상태를 분리해 조회 |
| `GET` | `/medication-scenarios` | Backend DB의 테스트 복약 예시 조회 |
| `POST` | `/medication-scenarios/apply` | 선택 시나리오를 시뮬레이션 날짜에 적용 |
| `POST` | `/testbed/reset` | 확인된 테스트베드 데이터 초기화 |
| `POST` | `/clock/advance` | 시뮬레이션 시간을 30분 또는 180분 진행 |
| `POST` | `/clock/play` | 시뮬레이션 시작 및 배속 설정 |
| `POST` | `/clock/pause` | 시뮬레이션 정지 |
| `POST` | `/doses/{dose_event_id}/take` | 공개 dose ID의 복용 완료 처리 |
| `GET` | `/nutrition` | 시뮬레이션 날짜의 영양 상태 조회 |
| `GET` | `/policies` | 사용자 화면용 정책 2종 조회 |
| `PUT` | `/policies/{policy_key}` | 사용자 화면에서 정책을 직접 켜거나 끔 |
| `GET` | `/notifications` | 현재 시각까지 활성화된 미확인 알림 조회 |
| `GET` | `/notifications/{notification_id}` | 공개 알림 ID로 상세 조회 |
| `POST` | `/notifications/ack-all` | 현재 보이는 알림 전체 확인 처리 |
| `GET` | `/chat/history` | 한 대화창의 날짜 단위 이전 이력 조회 |
| `POST` | `/chat/sync` | Backend가 신뢰 식별자를 주입해 AI v1.3 채팅 호출 |
| `POST` | `/chat/feedback` | AI 답변에 반응 및/또는 자유 의견 제출 |

## 멱등성

- `medication-scenarios/apply`, `testbed/reset`, `clock/advance`, `chat/sync`,
  `chat/feedback`은 브라우저가 생성한 안정적인 `request_id`를 사용한다.
- 응답을 받지 못한 재시도는 같은 `request_id`와 같은 정규화 본문을
  다시 사용한다.
- 같은 `request_id`를 다른 본문에 사용하면
  `409 IDEMPOTENCY_CONFLICT`다.
- 복용 완료, 정책 on/off, 알림 전체 확인은 결과 상태 자체가 반복 적용에
  안전한 절대 상태 변경이다.

## 복약 시나리오와 PHR 경계

- 선택 가능한 5개 시나리오와 약품·치료영역·시간은 Backend DB 행이
  런타임 원본이다.
- 시나리오 적용 시 같은 날짜의 `source_type=testbed_scenario` 일정만
  교체한다. 수동 등록 또는 다른 출처의 일정은 유지한다.
- 초기 DB가 비어 있을 때만 복합 복약 예시를 만들고 첫 항목을 복용 완료로
  표시하며 시계를 60분/초로 시작한다.
- 사용자가 다시 시나리오를 적용할 때에는 어떤 약도 자동 복용 처리하지
  않는다.
- 이 테스트 흐름은 PHR 쓰기를 수행하지 않는다.
- 신규 DB, 리셋 및 시나리오 적용 시에는 AI의 항상 읽는 Snapshot이
  `not_found`가 되지 않도록 Backend에 최소 환자 프로필을 영속 유지한다.

## 테스트베드 리셋

- 요청은 `request_id`와 리터럴 `confirm: true`만 허용한다.
- 리셋은 복약 일정·복약 기록·알림·대화·화면 정책·영양 기록 등
  Backend 소유 테스트 데이터를 초기화하고 시계를 초기 시각의 정지
  상태로 되돌린다.
- DB에 저장된 테스트 시나리오 카탈로그와 참조 데이터는 유지한다.
- 진행 중인 동기 채팅, Agent 작업 또는 실행 중인 쓰기 확인이 있으면
  `409 TESTBED_BUSY`로 거절한다.
- 운영 환경 또는 `TESTBED_RESET_ENABLED=false`에서는 실행할 수 없다.
- AI Server 내부 trace와 암호화된 의견 데이터는 삭제하지 않으며 AI
  Server에 쓰기 요청도 보내지 않는다.
- 성공 후 React는 dashboard, 대화 이력, 시나리오를 다시 조회한다.

## 서버 상태

- React는 `/status`를 주기적으로 조회하고 Backend와 AI Server를 각각
  표시한다.
- Backend 상태는 Backend DB의 최소 `SELECT 1` 결과까지 확인한다.
- AI Server 상태는 Backend만 Bearer 인증으로 호출할 수 있는
  `/health/generation/ready`에서 실제 생성형 provider 최소 호출까지 성공했을
  때만 `ready`로 표시한다. 인증 실패, timeout, 호출 실패 또는 계약 불일치는
  정상으로 표시하지 않는다. 운영 사용자 화면에는 테스트 전용 provider를
  구성하지 않는다.
- `/health/ready`는 컨테이너와 내부 DB·연동 설정을 확인하는 무비용
  readiness이다. 실제 모델 호출을 유발하지 않으며 UI의 AI 정상 판정 근거로
  단독 사용하지 않는다.
- 각 상태에는 `checked_at`과 비밀정보가 없는 `evidence` 코드가 포함된다.
- Agent URL, 토큰, DB 경로, 내부 예외 메시지는 브라우저 응답에 포함하지
  않는다.
- Backend 자체에 연결할 수 없으면 React가 Backend를 `연결 안 됨`, AI
  Server를 `확인 불가`로 표시한다.
- 필수 화면 API 또는 채팅 호출이 실패하면 React는 예시 데이터나 규칙 기반
  답변을 만들지 않고 연결 오류와 재시도 동작만 표시한다.

## 정책

- `medication_schedule_alert`: 복약 일정 알림 생성 여부
- `missed_dose_conversation`: 미복용 판정 뒤 AI 확인 대화 생성 여부

React에서 사용자가 직접 변경한 값은 Backend DB에 저장되고 실제 worker
경로에 반영된다. AI가 정책 변경을 제안·실행하는 경로는 기존 v1.3 확인·쓰기
계약을 계속 사용한다.

## 알림 식별자와 cursor

- 알림의 `id`는 DB 숫자 PK가 아니라 `notif_...` 형식의 Backend 발급
  불투명 공개 ID다.
- `/notifications`의 `after_id`와 `last_seen_id`에도 같은 공개 ID를
  사용한다. 기존 숫자 cursor와 존재하지 않는 공개 cursor는 거절한다.
- cursor가 없는 첫 요청은 현재 활성 알림의 최신 snapshot을 반환한다.
  cursor가 있는 증분 요청은 해당 알림의 `(visible_at, 내부 순서)` 경계
  이후를 오래된 항목부터 안전하게 소비하고, 응답 배열만 최신순으로
  정렬한다. 내부 순서는 응답이나 cursor에 노출하지 않는다.
- `last_seen_id`는 실제로 검사한 행까지만 전진한다. 따라서 `limit`,
  내부 전용 알림 필터, 아직 표시 시각이 되지 않은 예약 알림 때문에 공개
  알림을 영구적으로 건너뛰지 않는다.
- 알림과 복약 이벤트의 내부 FK는 Backend 내부에서만 유지한다.

## 채팅 출력

AI 응답 `message_type`은 다음 세 종류다.

- `text`: 일반 텍스트·표 결과
- `selection_box`: 정해진 선택지 중 하나를 제출
- `input_box`: 숫자 또는 dropdown 필드를 제출

`selection_box` 또는 `input_box`가 `pending`인 동안 일반 composer와 빠른
질문은 비활성화한다. 구조화 응답의 값과 유형은 Backend에서 다시 검증한다.
구조화 응답은 대상 assistant 메시지의 공개 ID를 `source_message_id`로
반드시 전송한다. 생략, 다른 대화의 ID, 이미 답한 카드 또는 최신 카드가 아닌
ID는 거절한다.

이 `source_message_id`는 React와 Backend 사이의 UI 결속 필드다. Backend는
검증이 끝난 응답 관계를 `chat_messages.reply_to_message_id`로 저장하며,
Backend→AI Server 동기 채팅 계약에는 `source_message_id`를 추가하지 않는다.
AI Server는 현재 `message_id`로 Backend read view를 조회해 원문 대화,
구조화 `message_payload_json`, `reply_to_message_id`를 함께 복원한다. 따라서
음식 후보, 부작용 설문, 일반 선택 질문을 직전 카드의 유형과 payload로
구분하며 LLM의 추측이나 “최신 pending” fallback에 의존하지 않는다.

AI Server의 `recent_chat`은 고정 20개 메시지로 자르지 않는다. 설정된 Backend
read 안전 상한 안에서 현재 사용자 메시지까지의 환자 대화 전체를 시간순으로
전달하고, 각 항목에는 user-visible 구조화 payload를 포함한다. 안전 상한으로
더 오래된 이력이 잘린 경우 `recent_chat_complete=false`로 명시한다.

채팅 이력은 구조화 assistant 메시지에 `response_message_id`, 답한 user
메시지에 `source_message_id`를 제공한다. React는 시간순으로 뒤따르는 메시지를
추측하지 않고 이 두 공개 ID가 서로 일치할 때만 제출 결과를 카드에 복원한다.
모든 확정 메시지는 Backend가 DB 저장 순서에서 발급한 양의 정수
`sort_sequence`를 제공한다. React는 시뮬레이션 시각이나 공개 메시지 ID가
아니라 이 값을 기준으로 메시지를 정렬한다. `/chat/sync`와 완료 SSE event는
각각 `user_sort_sequence`, `assistant_sort_sequence`를 반환하며, 이 순서값은
Backend→AI Server 계약에는 포함하지 않는다. 전송 중인 React 임시 메시지는
확정 순서값이 없으므로 현재 확정 이력 뒤에 놓고, 완료 응답을 받으면 Backend
순서값으로 교체한다.
과거 대화는 오늘 날짜 구간부터 시작하며, 대화창 맨 위에서 위로 스크롤할
때 바로 전 날짜를 한 구간씩 불러온다.

## 피드백 제출

브라우저는 아래 필드만 사용한다. `reaction`, `opinion_text` 중 하나 이상을
보내야 한다.

```json
{
  "request_id": "uuid",
  "assistant_message_id": "assistant_msg_...",
  "reaction": "like",
  "opinion_text": "자유 의견",
  "feedback_at": "2026-07-26T09:30:00+09:00"
}
```

`reaction`은 `like` 또는 `dislike`이며 같은 답변에서는 하나만 활성화된다.
새 반응은 기존 반응을 대체하고 의견만 보내면 반응은 유지된다. Backend는
해당 메시지가 설정된 환자와
연속 대화의 AI 답변인지 확인한 뒤 내부 식별자와 Bearer token을 주입한다.
의견 평문은 Backend DB에 저장하지 않고, AI Server 내부 DB에 암호문으로만
접수한다. 성공 응답과 채팅 이력은 현재 `reaction`,
`opinion_submitted`, `opinion_submitted_at`을 제공한다.

## 잔여 강화 과제

- React는 공통 envelope 구조를 런타임에 검증하지만 endpoint별 `data`
  전체 구조는 아직 TypeScript 정적 타입에 의존한다.
  `docs/UI_V1_OPENAPI.json`에서 TypeScript 타입과 런타임 validator를 함께
  생성하는 단계가 남아 있다.
- `/dashboard`, `/status`의 브라우저 제한은 12초지만 System DB의 전역
  pool/statement 제한은 최대 30초다. 두 요청은 read-only이며 React에서
  single-flight로 호출하지만, 운영 SLO 확정 시 서버 read 예산도 별도로
  12초 미만으로 제한할지 결정해야 한다.
- 이 문서는 단일 환자 테스트베드 계약이다. 멀티 사용자 운영 전환 시에는
  인증 세션에서 환자 범위를 파생하고 리소스별 권한 및 CSRF 정책을 별도
  공개 계약으로 추가해야 한다.
