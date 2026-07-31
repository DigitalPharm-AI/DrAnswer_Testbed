# 연동규격 v1.3 단일 환자 스레드 식별자 경계

- 상태: 활성 계약
- 기준: 환자당 채팅 스레드 1개
- 외부 식별자: `patient_id`, `message_id`, `request_id`

## 경계 규칙

`conversation_id`는 다음 어느 경계에도 요청·응답 필드로 존재하지 않는다.

- React ↔ Backend UI API
- Backend → AI 동기 채팅·피드백·비동기 이벤트 API
- AI → Backend 동기 쓰기·비동기 결과 callback API
- AI Tool의 모델 생성 인자와 Backend로 전송되는 Tool 관리 인자
- `ai_v13_*` Backend Read View의 명시적 열

정의되지 않은 최상위 필드는 각 strict DTO가 거절한다. 과거 데이터에서
임의 `metadata`, `context`, `arguments`, `details` 안에 남은 동일 키는 경계
직렬화 전에 재귀 제거한다. `ai_v13_chat_messages` View도 과거
`message_payload_json`과 `metadata_json`의 최상위 잔존 키를 제거한다.

메시지 연결은 다음 값으로 충분하다.

- 환자 범위: `patient_id`
- 사용자·assistant 메시지 관계: `message_id`와 Backend DB의
  `reply_to_message_id`
- 요청 멱등성: `request_id`

## 내부 스키마

중복 스레드 열은 Backend DB와 Agent DB에서 모두 제거했다. 환자별 동기 처리
락은 `agent_patient_locks.patient_id`, Trace·Tool·승인·피드백의 비식별 상관
관계는 `patient_id_hash`만 사용한다. 외부 계약과 내부 저장소 모두 환자당
단일 스레드 원칙을 따른다.

## 회귀 검증

계약 회귀 테스트는 다음을 확인한다.

1. 활성 DTO/OpenAPI schema의 `properties`에 retired 식별자가 없다.
2. 중첩 metadata/context/Tool arguments에 주입된 retired 식별자가 경계를
   통과하지 않는다.
3. Backend Read View가 실제 내부 `conversation_id` 열을 projection하지 않는다.
4. 메시지 조회·피드백·승인은 `patient_id`와 공개 메시지 ID로 범위를 검증한다.
