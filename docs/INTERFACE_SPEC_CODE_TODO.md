# 연동규격 v1.2 코드 정합성 TODO

- 작성일: 2026-07-25
- 상태: 구현 및 검증 완료
- 비교 기준: `닥터앤서_AI모듈_API_연동규격서_v1.2_Tool최소인자_공개PolicyID_추가내용_빨간색.docx`
- 범위: 연동규격 v1.2 코드 정합성 구현 및 검증 이력
- 완료일: 2026-07-26

## 진행 현황

- [x] TODO-1. 외부 ID 형식을 문자열 ID로 통일
- [x] TODO-2. 선택 입력 응답의 타입·페이로드 처리 보완
- [x] TODO-3. 챗봇 피드백 API 구현

## TODO-1. 외부 ID 형식을 문자열 ID로 통일

### 구현 전 차이

규격서는 메시지·복약·식사·음식 ID를 문자열로 정의하고 다음과 같은 예시를 사용한다.

- `user_msg_100`
- `assistant_msg_200`
- `dose_120`
- `meal_42`
- `food_51`

현재 코드는 일부 요청 ID를 `int()` 또는 `_positive_int()`로 변환하며, 데이터베이스의 내부 숫자 PK를 문자열로 바꿔 외부 ID로 사용한다. 따라서 규격서 예시와 같은 불투명 문자열 ID를 처리할 수 없다.

### 구현 항목

- [x] 외부 API에서 사용하는 ID와 데이터베이스 내부 PK를 분리한다.
- [x] 메시지, 복약 이벤트, 식사, 음식 엔터티에 외부 ID 컬럼과 안전한 ID 매핑 계층을 추가한다.
- [x] v1.2 API 경계의 숫자 변환을 공개 ID 우선 조회 방식으로 교체한다.
- [x] 응답에는 저장된 외부 문자열 ID가 반환되게 한다.
- [x] 기존 데이터의 외부 ID를 생성하는 마이그레이션과 백필 방식을 마련한다.
- [x] 기존 양의 숫자형 문자열 ID는 전환 기간 동안 조회만 호환하고 응답은 공개 ID로 통일한다.

### 영향 예상 파일

- `agent_app/tools/backend_query.py`
- `system_app/services/backend_v12_service.py`
- `system_app/models.py`
- 관련 데이터베이스 마이그레이션
- 관련 요청·응답 모델 및 테스트

### 완료 조건

- [x] 규격서 예시 ID가 조회, 저장, 확인 흐름 전체에서 정상 동작한다.
- [x] 존재하지 않거나 잘못된 ID의 처리 결과가 일관된다.
- [x] 외부 응답이 내부 데이터베이스 PK에 의존하지 않는다.
- [x] 기존 데이터와 신규 데이터 모두 회귀 테스트를 통과한다.

## TODO-2. 선택 입력 응답의 타입·페이로드 처리 보완

### 구현 전 차이

- 선택형 응답을 처리한 뒤 `requested_return_type`이 `selection_box`가 아니라 `None`으로 저장되는 경로가 있다.
- 계약 모델에는 `input_box`, `tables`, `inputs`가 있지만 외부 메시지 변환 과정에서는 텍스트와 선택 항목 위주로만 출력되어 구조화된 입력 데이터가 유실될 수 있다.
- 대기 중인 요청 타입과 사용자의 실제 응답 타입을 대조하는 처리가 충분하지 않다.

### 구현 항목

- [x] 선택형 응답의 `requested_return_type`을 `selection_box`로 유지한다.
- [x] 구조화된 결과를 `input_box`, `tables`, `inputs` 응답 필드로 변환한다.
- [x] `message_type`별 필수·금지 필드 조건을 검증한다.
- [x] 대화별로 대기 중인 응답 타입과 상태를 저장하고 다음 사용자 응답과 대조한다.
- [x] 입력 폼의 JSON 문자열 응답을 안전하게 파싱하고 원래 요청과 연결한다.
- [x] 시스템 화면에서 선택형·입력형·테이블형 응답을 모두 표시하고 왕복 처리한다.
- [x] 현행 오류 계약으로 타입 불일치·중복·오래된 응답을 거절한다. 규격서 오류 계약 개정 시 세부 코드만 재정합화한다.

### 영향 예상 파일

- `system_app/routes/chat.py`
- `system_app/templates/partials/chat_log.html`
- `system_app/services/backend_chat_service.py`
- `agent_app/integration/chat_contracts.py`
- 구조화된 응답 생성부 및 관련 테스트

### 완료 조건

- [x] 선택형 응답이 `selection_box`로 왕복 처리된다.
- [x] 입력형 응답이 표시되고 사용자의 JSON 응답이 정상 처리된다.
- [x] 테이블 데이터가 외부 메시지 변환 과정에서 유실되지 않는다.
- [x] 일반 텍스트 대화 동작에는 회귀가 없다.
- [x] 요청한 타입과 다른 응답을 보낸 경우 계약 검증 오류가 발생한다.

## TODO-3. 챗봇 피드백 API 구현

### 구현 전 차이

규격서에는 `POST /agent/async/chat_feedback`과 HTTP `202 Accepted` 응답이 정의되어 있었지만, 기존 코드에는 요청 모델, 라우트, 서비스 처리, 멱등성 검증이 연결되어 있지 않았다.

### 구현 항목

- [x] 피드백 요청·응답 모델을 추가한다.
  - `request_id`
  - `message_id`
  - `conversation_id`
  - `patient_id`
  - `feedback`
  - `feedback_text`
  - `feedback_at`
- [x] `POST /agent/async/chat_feedback` 라우트를 등록한다.
- [x] 기존 Backend→AI 동기 API와 동일한 인증 원칙을 적용한다.
- [x] 피드백 대상이 AI 답변 메시지인지, 대화·환자 정보가 일치하는지 검증한다.
- [x] `api_path + request_id` 기준 멱등성을 적용한다.
  - 같은 본문 재전송: 최초 응답 재사용
  - 다른 본문 재전송: 충돌 처리
- [x] 자유 입력 피드백의 암호화 저장과 로그 비노출을 보장한다.
- [x] 요청은 빠르게 `202 Accepted`로 반환하고 후속 처리 상태를 저장한다.
- [x] 보존 기간, 실패 처리, 재처리 정책을 구현 시 확정한다.

### 영향 예상 파일

- `agent_app/routes/`의 신규 피드백 라우트
- `agent_app/main.py`
- `agent_app/integration/`의 요청·응답 계약
- `agent_app/persistence/models.py`
- 관련 데이터베이스 마이그레이션
- `agent_app/tools/backend_query.py`
- 관련 단위·통합 테스트

### 완료 조건

- [x] 유효한 요청은 `202`와 `{"status": "accepted"}`를 반환한다.
- [x] 같은 요청의 재전송은 중복 저장 없이 같은 결과를 반환한다.
- [x] 같은 `request_id`에 다른 본문을 보내면 충돌로 처리한다.
- [x] 사용자 메시지 또는 문맥이 맞지 않는 메시지에는 피드백을 연결하지 않는다.
- [x] 자유 입력 피드백 원문이 애플리케이션 로그에 남지 않는다.

## 권장 구현 순서

1. 외부 문자열 ID 지원 및 데이터 마이그레이션
2. 선택·입력·테이블 응답 왕복 처리
3. 문자열 메시지 ID를 사용하는 챗봇 피드백 API

챗봇 피드백이 `message_id`를 사용하므로 ID 형식 변경을 먼저 완료하는 것이 안전하다.

## 오류 계약 처리 방침

오류 계약은 코드 변경 TODO에서 제외한다. 현재 코드 동작을 기준으로 오류 코드, HTTP 상태, 응답 본문의 차이를 다시 확인한 뒤 **연동규격서에서 수정·정합화**한다.

## 구현 및 회귀 테스트

구현 전 기준은 `40 passed, 1 warning`이었다. 2026-07-26 구현 완료 시점의 전체
회귀 결과는 `531 passed, 4 skipped, 4 warnings`이며, 실제 PostgreSQL reader
role 경계 테스트도 `1 passed`로 skip 없이 통과했다. 로컬 9000 스택에서는 채팅
공개 ID 왕복, 멱등 replay, read-only DB write 거절과 피드백 202·replay·409
충돌까지 실제 HTTP로 검증했다. 다음 영역과 신규 공개 ID·구조화 UI·피드백
보안 테스트를 CI 필수 게이트로 추가했다.

- `tests/test_agent_sync_chat_v12.py`
- `tests/test_system_backend_v12.py`
- `tests/test_backend_v12_write_tools.py`
- `tests/test_backend_v12_integration.py`
- `tests/test_external_public_ids_v12.py`
- `tests/test_agent_chat_feedback_v12.py`
- `tests/test_ui_pages.py`
- `tests/test_backend_v12_postgres_boundary.py`
- `tests/test_testbed_boundary_config.py`

세부 피드백 암호화·보존·재처리 정책은 `docs/AI_V12_FEEDBACK_POLICY.md`를 따른다.
