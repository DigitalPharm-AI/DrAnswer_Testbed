# AI Server v1.3 채팅 피드백 처리 정책

이 문서는 피드백 endpoint의 명시적 확장 계약이다.
`POST /agent/sync/chat`의 v1.3 요청·응답 계약은 변경하지 않는다.

## 접수 계약

- 경로: `POST /agent/async/chat_feedback`
- 인증: Backend→AI 동기 채팅과 동일한 `AGENT_SYNC_API_TOKEN` Bearer 인증
- 정상 응답: HTTP `202 Accepted`
  - 신규 응답: `status`, 현재 요청 처리 시점의 `reaction`,
    timezone-aware `accepted_at`
  - 기존 opinion-only 행의 저장 응답에는 `reaction`, `accepted_at`이 없을 수
    있으며 replay 시 원본 응답을 그대로 반환한다.
- 요청 필드:
  - `request_id`
  - `message_id`
  - `patient_id`
  - `reaction` (`like` 또는 `dislike`, 선택)
  - `feedback_text` (공백 제거 후 1~4,000자, 선택)
  - `feedback_at` (UTC offset 필수)
- `reaction`, `feedback_text` 중 하나 이상이 필수이며 정의되지 않은 추가
  속성은 거절한다. `reaction=null`을 이용한 선택 해제는 지원하지 않는다.
- 같은 AI 답변의 `like`, `dislike`는 상호배타적인 최신 상태다. 새 반응은
  이전 반응을 대체하고, 의견만 접수할 때는 현재 반응을 변경하지 않는다.
- 멱등성 키: `api_path + request_id`
  - 정규화된 동일 본문은 최초 `202` 응답을 재사용한다.
  - 같은 키에 다른 본문은 `409 IDEMPOTENCY_CONFLICT`로 거절한다.
- AI는 Backend의 `ai_v13_chat_messages` 공개 ID 조회 인터페이스로
  `message_id`, `patient_id`, `role=assistant`를 모두
  검증한 뒤에만 피드백을 접수한다.

브라우저는 이 내부 API를 직접 호출하지 않는다. React는 동일 출처 Backend의
`POST /api/ui/v1/chat/feedback`에
`request_id`, `assistant_message_id`, `feedback_at`과 선택적 `reaction`,
`opinion_text`만 전달한다. 둘 중 하나 이상이 필수다.
Backend는 설정된 테스트 환자와 assistant 메시지의 환자 소유 관계를 DB에서
확인·주입하고
Bearer token도 서버 내부에서만 사용한다.
Agent가 `202`를 반환한 뒤 Backend assistant 메시지 metadata에는
현재 `reaction`과 수락 시각·요청 ID, `opinion_submitted`,
`opinion_submitted_at`, `opinion_request_id`만 기록한다. 반응 메타데이터는
반응 요청의 Agent `accepted_at`이 기존 값보다 최신일 때만 갱신하므로 과거
멱등 replay가 최신 반응을 되돌리지 않는다. 의견만 보내는 요청은 반응
메타데이터를 절대 변경하지 않는다.
의견 평문은 Backend DB에 저장하지 않는다.

## 저장 및 암호화

- 자유문 `feedback_text`는 AES-256-GCM ciphertext로만 AI 내부 DB에 저장한다.
- `feedback` 열은 현재 반응 상태를 `like=true`, `dislike=false`로 저장한다.
  의견 전용 행은 `NULL`이며, 대상별 `feedback IS NOT NULL` 부분 unique
  index로 활성 반응이 하나만 존재하도록 보장한다.
- AAD에는 API 경로, 요청 ID, 공개 메시지 ID, 환자 ID HMAC,
  피드백 시간이 포함되므로 ciphertext를 다른 행으로 이동하면 인증에
  실패한다.
- 요청 본문, 자유문, 환자 ID digest는 키 기반 HMAC-SHA256과 서로 다른
  domain prefix를 사용한다. 평문 SHA digest는 저장하지 않는다.
- 로그에는 자유문과 환자 ID를 전달하지 않는다. 자유문 존재 여부만
  boolean metric으로 기록한다.
- `AGENT_FEEDBACK_ENCRYPTION_KEY`는 URL-safe base64로 인코딩한 정확히
  32-byte 키이며 모든 환경에서 필수다. 키가 없거나 잘못되면 Agent
  시작이 실패한다.
- `AGENT_FEEDBACK_ENCRYPTION_KEY_ID`는 저장 행에 기록한다. 키 교체 시
  보존 기간에 남아 있는 기존 key ID를 복호화할 수 있도록 이전 키를
  별도 키 저장소에 유지해야 한다.

## 저장 완료 상태와 후속 처리 범위

- 현재 v1.3 구현의 완료 범위는 검증된 반응과 의견을 AI 내부 DB에
  내구성 있게 접수하고 `202 Accepted`를 반환하는 지점까지다.
- 접수 행은 `ACCEPTED` 상태로 보존되며, 이번 테스트베드에는 이를 외부
  평가 시스템으로 전송하는 별도 consumer가 연결되어 있지 않다.
- 모델 평가·학습 파이프라인 같은 후속 consumer를 연결할 때 사용할 수
  있도록 `PROCESSING`, `COMPLETED`, `RETRYABLE_FAILED`, `FINAL_FAILED`,
  lease와 backoff 서비스 코드는 준비되어 있지만 현재 런타임이 자동
  실행한다고 간주하지 않는다.
- 후속 consumer를 활성화할 때에는 처리 대상, 복호화 권한, 재시도 가능한
  오류 코드, 최종 실패 운영자를 별도 운영 계약으로 확정해야 한다.
- `error_code`에는 분류 코드만 저장하며 예외 메시지나 자유문을 저장하지
  않는다.
- 기본 보존 기간은 접수 시점부터 604,800초(7일)이며 만료 행은 Agent
  시작 시 실행되는 retention cleanup에서 삭제한다.

환경 변수:

```dotenv
AGENT_FEEDBACK_RETENTION_SECONDS=604800
AGENT_FEEDBACK_MAX_ATTEMPTS=3
AGENT_FEEDBACK_PROCESSING_LEASE_SECONDS=300
AGENT_FEEDBACK_RETRY_BASE_SECONDS=60
AGENT_FEEDBACK_RETRY_MAX_SECONDS=3600
AGENT_FEEDBACK_ENCRYPTION_KEY=
AGENT_FEEDBACK_ENCRYPTION_KEY_ID=feedback-v1
```
