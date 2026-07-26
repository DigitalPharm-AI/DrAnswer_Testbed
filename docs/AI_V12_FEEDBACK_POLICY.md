# AI Server v1.2 채팅 피드백 처리 정책

## 접수 계약

- 경로: `POST /agent/async/chat_feedback`
- 인증: Backend→AI 동기 채팅과 동일한 `AGENT_SYNC_API_TOKEN` Bearer 인증
- 정상 응답: HTTP `202 Accepted`, `{"status":"accepted"}`
- 멱등성 키: `api_path + request_id`
  - 정규화된 동일 본문은 최초 `202` 응답을 재사용한다.
  - 같은 키에 다른 본문은 `409 IDEMPOTENCY_CONFLICT`로 거절한다.
- AI는 Backend의 `ai_v12_chat_messages` 공개 ID 조회 인터페이스로
  `message_id`, `conversation_id`, `patient_id`, `role=assistant`를 모두
  검증한 뒤에만 피드백을 접수한다.

## 저장 및 암호화

- 자유문 `feedback_text`는 AES-256-GCM ciphertext로만 AI 내부 DB에 저장한다.
- AAD에는 API 경로, 요청 ID, 공개 메시지 ID, 대화 ID, 환자 ID HMAC,
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

## 상태와 재처리

상태 전이는 다음과 같다.

```text
ACCEPTED -> PROCESSING -> COMPLETED
                       -> RETRYABLE_FAILED -> PROCESSING
                       -> FINAL_FAILED
```

- 기본 최대 처리 횟수: 3회
- 기본 처리 lease: 300초. 처리 프로세스가 종료되면 lease 만료 후 같은 행을
  재점유하며 최대 횟수 소진 시 `FINAL_FAILED`로 종료한다.
- 기본 backoff: 60초에서 시작하는 지수 backoff
- 최대 backoff: 3,600초
- `error_code`에는 분류 코드만 저장하며 예외 메시지나 자유문을 저장하지
  않는다.
- 정상 완료와 최종 실패는 자동 재처리하지 않는다.
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
