# 대화 기반 쓰기 승인 계약

> 마지막 갱신: 2026-07-30

## 확정 방향

쓰기 승인의 단일 원본은 AI Server의 Agent DB이다. Backend는 승인 ID나 승인
키를 발급하지 않는다. Backend에는 v1.3 쓰기 API와 사용자가 실제로 승인
버튼을 선택한 채팅 메시지만 남는다.

## 처리 순서

1. LLM은 쓰기가 필요하면 `request_record_approval`을 호출한다.
2. AI Server는 모델 인자를 검증하고, 환자·메시지·현재 Snapshot에서 필요한
   값을 주입해 권위 있는 업무 인자를 만든다.
3. AI Server는 업무 인자와 카드 표시 정보를 Agent DB에 암호화해 저장한다.
   공개 응답에는 카드 표시 정보만 포함하며 키는 포함하지 않는다.
4. Backend는 카드와 이후 사용자 선택 메시지를 일반 채팅 메시지 및
   `reply_to_message_id` 관계로 저장한다.
5. 사용자가 기록·변경·삭제를 선택하면 AI Server가 선택 메시지와 Agent DB
   상태를 대조한다.
6. 검증에 성공한 경우 AI Server가 `apv_...` 형식의 짧은 일회용 키를 만들고
   신뢰된 Tool 호출 경계에만 주입한다.
7. 쓰기 Tool은 키로 Agent DB의 원래 업무 인자를 복원하고, 같은 환자·Tool·
   승인 메시지·원본 요청에 결합됐는지 재검증한다.
8. AI Server는 v1.3 Backend 동기 쓰기 API를 호출한다. Backend 요청에는
   `approval_key`가 포함되지 않는다.
9. Backend는 `confirmation_message_id`가 해당 환자의 실제 선택 메시지이고,
   승인 카드에 대한 reply이며, `source_chat_request_id`와 일치하는지 검증한다.
10. Backend 응답이 확정되면 AI Server는 키를 `consumed` 처리한 뒤 LLM 최종
    응답 노드가 Tool 결과를 바탕으로 사용자 답변을 생성한다.

## 책임 경계

### LLM

- 사용자 표현에 가까운 최소 업무 인자만 생성한다.
- `patient_id`, `expected_version`, `request_id`,
  `source_chat_request_id`, `confirmation_message_id`, `approval_key`,
  `requested_at`을 생성하지 않는다.
- 승인 키를 보거나 최종 답변에 포함하지 않는다.

### AI Server

- 승인 상태와 암호화된 권위 인자를 Agent DB에서 관리한다.
- 사용자 선택을 검증한 뒤에만 짧은 키를 발급한다.
- Tool 실행 시 키만 전달하고, Tool 내부에서 원래 인자를 복원한다.
- 동일 승인 재시도에는 같은 `request_id`와 키를 사용한다.
- 응답 확정 후 키를 소비하며, 다른 환자·메시지·Tool·인자로의 재사용을
  거부한다.

### Backend

- 승인 카드를 포함한 assistant 메시지와 사용자 선택 메시지를 저장한다.
- v1.3의 `confirmation_message_id` reply edge를 검증한다.
- 업무 DB 쓰기와 `(api_path, request_id)` 멱등성 결과를 하나의 transaction으로
  처리한다.
- 승인 키나 내부 승인 상태를 저장하지 않는다.

## Agent DB 상태

`AgentPendingAction`은 다음 상태를 사용한다.

- `PENDING`: 카드 발급 후 사용자 선택 대기
- `APPROVED`: 사용자 승인 검증 완료, 키 사용 가능
- `SENDING`: Backend 동기 쓰기 실행 중
- `CONSUMED`: Backend 결과 확정, 재실행 금지
- `CANCELLED`: 사용자 취소
- `EXPIRED`: 유효시간 만료
- `SUPERSEDED`: 더 최신 제안으로 대체

업무 인자와 카드 상세는 AES-GCM으로 암호화한다. DB에는 평문 키 대신
`approval_key_hash`만 저장한다. Trace에는 원시 인자나 키 대신 hash와 키 존재
여부만 기록한다.

## 제거한 레거시

- `POST /api/agent/mutation-confirmations/prepare`
- Backend `MutationConfirmation` 모델과 서비스
- Backend `mutation_confirmations` 테이블
- 외부 또는 LLM-visible `confirmation_id`
- Backend 메시지 metadata의 `approved_mutation_confirmation`
- 승인 키를 Backend 요청이나 AI 응답에 포함하는 경로

과거 테이블은 `20260730_0007_remove_backend_mutation_confirmations`
마이그레이션에서 삭제한다.

## 검증 기준

- 승인 전 업무 DB 변경이 없다.
- 취소·만료·대체된 승인은 실행되지 않는다.
- 승인 Tool 입력은 정확히 `{"approval_key": "apv_..."}`만 허용한다.
- 키가 환자·Tool·선택 메시지·원본 요청·업무 인자에 결합된다.
- Backend 요청과 AI 응답·로그에 키가 없다.
- Backend는 실제 `confirmation_message_id` reply edge가 없으면 쓰기를 거절한다.
- 전송 실패는 같은 `request_id`로만 재시도한다.
- 성공 또는 terminal 실패 뒤 키는 소비된다.
