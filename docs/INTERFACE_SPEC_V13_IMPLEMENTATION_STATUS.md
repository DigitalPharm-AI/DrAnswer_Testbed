# 연동규격 v1.3 구현 상태

- 기준 계약: 닥터앤서 AI 모듈 API 연동규격 v1.3
- 상태: 공개 API와 로컬 통합 흐름 구현 완료, 운영 환경 검증 항목 일부 남음
- 활성 OpenAPI:
  - `AI_V13_CHAT_OPENAPI.json`
  - `AI_V13_ASYNC_MEDICATION_OPENAPI.json`
  - `BACKEND_V13_WRITE_OPENAPI.json`
  - `BACKEND_V13_ASYNC_CALLBACK_OPENAPI.json`
  - `UI_V1_OPENAPI.json`

## 구현 완료

- Backend→AI 동기 채팅은 `POST /agent/sync/chat` 단일 NDJSON 경로를
  사용한다.
- 미복용 접수, 미복용 결과 Callback, 일일 패턴 분석 접수와 알림 정책
  변경 제안 Callback은 v1.3 최소 계약을 사용한다.
- 환자당 채팅 스레드는 하나이며 외부·내부 `conversation_id`를 사용하지
  않는다.
- 기록·정책 변경은 AI 내부 승인 후 Backend 동기 쓰기 API에서 처리한다.
- 외부 ID는 접두사와 소문자 16진수 16자리 형식을 사용한다.
- 공개 성공 응답과 공통 오류 응답을 분리하고, 내부 Trace·Job·승인 정보를
  외부 계약에 노출하지 않는다.
- Backend가 매일 02:00에 일일 패턴 분석을 접수하고 AI Server가 08:30
  정책 제안 전달 작업을 예약한다. AI Server 자체 scheduler는 사용하지
  않는다.
- 현행 OpenAPI와 runtime schema의 일치 여부를 자동 검사한다.

## 남은 운영 항목

### 1. 복수 정책 제안 우선순위

일일 패턴 분석이 서로 다른 정책 변경 후보를 두 개 이상 생성하면 현재
구현은 임의로 선택하지 않고 실패 처리한다. 다음 우선순위를 업무 정책으로
확정하고 계약 테스트를 추가해야 한다.

1. 안전 위험도
2. 연속 미복용 횟수
3. 최근 7일 미복용률
4. 가장 최근 사건

완료 조건은 환자당 하루 최우선 제안 한 건만 전달되고 동일 분석의 재시도가
중복 확인 알림을 만들지 않는 것이다.

### 2. 운영 Reverse proxy의 NDJSON 설정

운영 proxy에서 다음을 확인해야 한다.

- `/agent/sync/chat` 응답 buffering 비활성화
- SSE heartbeat를 NDJSON에 삽입하지 않음
- application과 proxy의 stream idle timeout 정합화
- 중간 chunk와 최종 `completed` 또는 `error` 이벤트가 지연 없이 전달됨

### 3. 운영 다중 replica·재시작 복구 검증

Backend의 02:00 scheduler, AI worker와 Callback 재시도를 다중 replica 및
프로세스 재시작 조건에서 검증해야 한다. 날짜별 `request_id`, 본문 hash,
durable task와 Callback 멱등성으로 실제 발송과 사용자 확인 알림이 각각
한 번만 생성되어야 한다.

### 4. 내부 API 개발환경 인증 정책

운영 환경에서는 내부 토큰이 필수지만 개발 환경은 토큰이 비어 있으면 일부
내부 경로를 허용한다. 모든 환경에서 토큰을 필수화할지 확정한 뒤, 필수화할
경우 로컬 실행 스크립트가 개발용 토큰을 명시적으로 주입하도록 변경한다.

### 5. 운영 전 최종 증적

실제 배포 구성에서 다음 증적을 남긴다.

- Backend↔AI HTTP 요청·응답 캡처가 v1.3 예제와 동일함
- 미복용 접수→AI 조회→Callback→후속 대화 E2E
- 야간 분석→08:30 제안→사용자 승인→Backend 정책 적용 E2E
- PostgreSQL 데이터 이관 전후 건수·참조 무결성·재실행 결과
- 전체 계약, migration, Playwright 회귀 결과

## Backend Read-only View 명칭

Backend가 AI Server에 제공하는 Read-only View는 v1.3 계약과 일치하는
`ai_v13_*` 명칭을 사용한다. AI Server의 전용 DB 계정에는 해당 View의
`SELECT` 권한만 부여한다.
