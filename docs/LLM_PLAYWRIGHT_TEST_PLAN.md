# Backend(PHR) Snapshot·부작용 흐름 Playwright 검증 계획

- 작성일: 2026-07-26
- 기준 화면: Backend가 루트(`/`)에서 제공하는 React 앱
- 목표 구조: Backend DB가 PHR 원천 데이터를 소유하고 AI Server는 승인 View만
  Read-only로 조회
- 저장 경계: 부작용 평가를 포함한 모든 업무 데이터 쓰기는 Backend 동기 API 사용

별도 `/react` 미리보기와 Jinja/HTMX 화면은 검증 대상이 아니며, 다시 노출되면
회귀로 처리한다. 브라우저 검증은 사용자에게 보이는 결과와 HTTP 경계를 확인하고,
Snapshot 구성·DB 권한·Tool Runtime 주입은 Python 통합 테스트와 DB 사후검증으로
보완한다.

## 1. 검증 대상 계약

### 1.1 채팅 시작 시 항상 생성하는 Snapshot

AI Server는 Backend가 전달한 신뢰된 `patient_id`로 매 동기 채팅 요청마다 다음
현재 정보를 조회한다.

- 최소 환자 프로필
- 현재 활성 질환과 치료
- 오늘 복약 정보
  - 오늘 복약 시간표
  - 오늘 복약 이력
- 오늘 식사 정보
- 현재 알림 정책

### 1.2 필요할 때만 실행하는 Read Tool

- 과거 복약 이력
- 전체 부작용 이력
- 과거 식사 기록

### 1.3 환자정보가 아닌 기준정보

- 의약품별 알려진 부작용
- 음식 영양 기준정보
- 온톨로지와 개념 관계

### 1.4 부작용 Tool Calling 경계

| 구분 | 값 |
|---|---|
| LLM이 생성하는 최소 인자 | `symptom_text`, 증상 발생 시점 등 사용자가 표현한 증상 정보 |
| AI Server가 주입하는 값 | `patient_id`, 환자 Snapshot, 복약 목록, 오늘 복약 상태, `message_id`, 기록 ID, 버전 |
| Tool이 자체 처리하는 값 | 현재 약 후보 매칭, 의약품 부작용 기준정보 조회, 평가 로직 |
| 저장 | 확인이 필요한 경우 사용자 확인 후 Backend 동기 쓰기 API 호출 |

`patient_id`, 복약 목록, Backend 메시지·기록 ID와 버전은 LLM Tool
`inputSchema`에 노출하지 않는다. Frontend는 이 식별자들을 생성하지 않는다.
다만 Backend가 발급한 공개 assistant `message_id`는 구조화 응답의 후속 선택을
연결하기 위해 `source_message_id`로 BFF에 재전송할 수 있다. 내부 record ID와
version은 Frontend가 전송하지 않는다.

## 2. 테스트 환경 원칙

### 2.1 격리된 실행 환경

상태 변경 합격 시나리오는 SQLite 파일을 만들지 않는다. 로컬 수동 검증은
전용 PostgreSQL 테스트베드를 reset API로 초기화해 사용하고, CI 격리 검증은
실행마다 별도 PostgreSQL schema를 만든다.

- Backend Server: Backend 업무 DB에 Read/Write
- AI Server: 같은 Backend DB의 승인 View에 Read-only
- AI 내부 DB: Backend 업무 원장 레코드는 저장하지 않으며, Trace·Tool 실행
  상태·멱등성에 더해 비동기 큐/콜백, worker heartbeat, 대화 lock, pending
  action, Backend write 재시작 상태, 암호화된 feedback 같은 운영·제어 상태 저장
- Agent worker: AI 내부 DB 작업 처리
- 별도 `phr_app`, `phr.db`, `phr_patient_key`: 목표 구조의 테스트 프로세스와
  fixture에서 사용하지 않음

`tools/da_drug_9000_stack.ps1`과
`tools/run_real_llm_playwright.ps1`는 Backend+AI+worker만 기동한다. AI
Server는 Backend 업무 DB의 승인 View를 Read-only URL로 열며, 별도 PHR
프로세스나 PHR DB를 준비하지 않는다.
두 runner는 `.env.9000`을 common 설정으로 사용하고
`.env.agent_app.secret` credential overlay는 Agent API와 worker에만
전달한다. Backend, migration, verifier는 common 설정만 받는다.

두 runner의 상태 격리 방식은 다르다.

- `tools/da_drug_9000_stack.ps1`: 앱별 env 파일이 지정한 로컬 PostgreSQL
  Backend DB와 Agent DB를 사용하는 수동·대화형 확인용이다.
- `tools/run_real_llm_playwright.ps1`: 같은 PostgreSQL-only stack을 기동하고
  브라우저에서 testbed reset API를 먼저 호출하는 실제 LLM 검증 runner다.
  운영·공유 DB에는 실행하지 않는다.
- `tools/run_ci_browser_test.py`: CI용 PostgreSQL schema를 실행마다 격리하고
  종료 시 schema를 제거한다.

### 2.2 Fixture 원칙

- 테스트 환자와 데이터는 모두 합성값을 사용한다.
- 오늘 식사는 현재 UI에 등록 화면이 없으므로 격리된 Backend fixture 또는
  Backend 테스트 API로 준비한다.
- Snapshot 시각은 Backend에 저장된 사용자 메시지 시각을 기준으로 고정한다.
- 각 시나리오는 독립 DB 또는 독립 환자 ID를 사용한다.
- 실제 LLM 시나리오는 문장 전체 일치가 아니라 구조화 응답 타입, Tool 이름,
  서버 주입 경계, DB side effect를 판정한다.

## 3. 기존 CI 필수 검증

```powershell
npm --prefix frontend run build
npm --prefix frontend run test
npm run test:browser
```

### 3.1 React 런타임 시나리오

`frontend/tests/runtime-state.test.cjs`는 격리된 HTTP 서버에서 빌드 산출물을
실행하고 API를 가로채어 다음을 검증한다.

- 사용자 메시지가 AI 응답보다 먼저 한 번만 표시됨
- 실패 시 임의 규칙 기반 답변 없이 명시적 오류와 같은 `request_id` 재시도 제공
- `text`, `selection_box`, `input_box` 계약 렌더링
- assistant hover/focus 피드백·복사 작업과 사용자 메시지 복사
- Backend/AI Server 상태가 실제 `/api/ui/v1/status`를 반영함

### 3.2 배포 루트 읽기 전용 스모크

`tools/playwright_ci_smoke.js`와 `tools/run_ci_browser_test.py`는 다음을
검증한다.

- `/`의 React 앱, HOME/Chat 탭과 초기 BFF 조회 성공
- dashboard, medication scenarios, chat history, status가 모두 `200`
- 읽기 전용 스모크에서 상태 변경 HTTP 요청이 발생하지 않음
- 삭제된 `/react`, 기존 정적 자산, HTMX/Jinja DOM이 존재하지 않음
- 페이지 오류와 브라우저 콘솔 오류가 없음

## 4. P0 시나리오

P0는 Snapshot과 부작용 처리 계약을 배포하기 전에 반드시 통과해야 한다.

| ID | 준비 및 UI 단계 | 네트워크 검증 | DB 사후검증 | 실패 판정 |
|---|---|---|---|---|
| P0-01 실제 상태 표시 | Backend+AI+worker를 기동하고 `/` 접속 → HOME과 Chat 전환 | 초기 BFF 4종이 `200`; `/api/ui/v1/status`의 실제 readiness가 두 상태 배지와 일치 | 변경 없음 | 연결되지 않은 서비스를 `정상`으로 표시하거나 콘솔 오류 발생 |
| P0-02 기본 Snapshot 성공 | 최소 프로필, 활성 질환·치료, 오늘 복약 시간표·이력, 오늘 식사, 현재 정책을 seed → “오늘 복약과 식사 상태를 알려줘” 전송 | `/api/ui/v1/chat/sync` 1회; 별도 PHR HTTP 호출 0회; 브라우저 요청에 `patient_id`나 Snapshot 없음 | Backend의 사용자·assistant 메시지 각 1건; 업무 데이터 변경 없음 | 현재값 누락, 다른 날짜 정보 혼입, “PHR 미등록” 응답, PHR HTTP 호출 |
| P0-03 단일 약 부작용 | 활성 약과 복약 이력이 하나로 특정되는 상태 → “암로디핀을 먹고 메스꺼웠어” 전송 | 최초 chat 1회; 동기 응답으로 증상 `selection_box`; Tool 모델 인자에는 증상·시점만 존재 | 승인 전 부작용 기록 0건; AI 내부 DB에는 Tool 상태만 존재 | 약 이름을 다시 묻거나 `phr_patient_key` 요구, 임시 text 뒤 비동기 응답, 승인 전 저장 |
| P0-04 복수 약 후보 | 같은 시점의 후보 약을 2개 이상 seed → “이 약을 먹고 메스꺼웠어” 전송 → 약 후보 선택 → 증상 문항 선택 | 후보 선택마다 동기 chat 1회; `source_message_id` 연결; 중복 클릭 차단 | 선택 전·평가 전 부작용 기록 0건 | LLM이 약을 임의 선택, 후보와 다른 약 평가, 동일 선택 중복 요청 |
| P0-05 부작용 저장 | 평가 완료 → 기록 확인 카드 확인 → 승인 | 승인 시 AI→Backend 동기 쓰기 API 정확히 1회; 동일 `request_id` replay는 같은 결과 | Backend 부작용 기록 1건, 공개 ID·version 생성; AI DB 업무 기록 0건 | 별도 PHR DB 쓰기, 중복 기록, 승인 전 기록, version 불일치 |
| P0-06 Snapshot 최신성 | 복용 기록 또는 알림 정책을 변경 → 즉시 해당 상태 질문 | 변경 API 성공 후 새 chat 요청; 이전 Snapshot 재사용 금지 | Backend 변경값과 assistant가 참조한 현재값 일치 | 이전 복약 상태·정책을 답하거나 사용자 간 Snapshot 캐시 공유 |
| P0-07 부분정보 | 필수 Snapshot 일부를 `not_found`, `not_supported`, 단일 도메인 `unavailable`로 각각 fixture | 안전하게 답할 수 있는 경우 chat은 `200`; 필요한 Tool은 실행 차단; 전체 PHR 부재로 뭉뚱그리지 않음 | 업무 데이터 변경 없음; availability 원상태 유지 | `not_found`와 장애 혼동, 미지원 정보를 생성, 필요한 정보 없이 평가·쓰기 실행 |
| P0-08 Backend Read 장애 | Backend DB Read 연결 전체 차단 → 메시지 전송 → 연결 복구 → 같은 버블에서 재시도 | 장애 시 LLM·Tool·쓰기 호출 0회와 명시적 오류; 복구 후 같은 `request_id`; 규칙 기반 fallback 0회 | 장애 중 assistant·업무 기록 0건; 복구 후 user/assistant 각 1건 | 정상처럼 답변, 건강정보를 추정, retry 중복 메시지·중복 기록 |
| P0-09 부작용 신뢰 경계 | MCP Tool catalog와 실제 부작용 요청 검사 | 모든 입력 `additionalProperties: false`; 부작용 LLM schema에 `patient_id`, 복약 목록, 메시지·기록 ID, version 없음 | AI의 Backend 직접 write probe 거절; Backend API 경유 write만 성공 | 부작용 모델이 식별자·업무 레코드·version 생성, AI DB에 업무 데이터 저장 |

### P0 UI 공통 판정

1. 전송 직후 사용자 버블이 `sending` 상태로 한 번만 나타난다.
2. assistant 대기 버블에는 `처리중입니다`만 표시한다.
3. 응답 완료 시 대기 버블이 계약 타입의 assistant 메시지로 교체된다.
4. 증상 문항은 동일 동기 요청의 `selection_box` 또는 `input_box`로 표시한다.
5. 실패한 사용자 버블은 원문과 `다시 시도`를 유지한다.
6. 응답 본문에 `PHR 환자 정보가 등록되지 않음`, `phr_patient_key`,
   “약 이름을 다시 알려 달라”가 근거 없이 등장하면 실패한다.

## 5. P1 시나리오

| ID | 준비 및 UI 단계 | 네트워크 검증 | DB 사후검증 | 실패 판정 |
|---|---|---|---|---|
| P1-01 날짜 경계 | 23시대 fixture에서 무기한 복약 일정을 적용 → 다음날로 진행 → HOME과 Chat 확인 | 새 날짜 dashboard와 chat 정상; 과거 상세가 필요할 때만 Read Tool 호출 | 새 날짜 dose event가 정확히 1세트 생성; 전날 이력 보존 | 일정 소멸, 전날 event 재사용, 오늘 Snapshot에 전날 식사 혼입 |
| P1-02 과거 상세 Tool | “지난주 복약 이력”, “이전 부작용”, “어제 식사”를 각각 질문 | 기본 Snapshot 뒤 해당 상세 Read Tool만 호출; 임의 SQL과 전체 테이블 조회 없음 | Read-only이므로 업무 데이터 변경 0건 | 과거 전체를 기본 Snapshot에 항상 적재, 범위 없는 무제한 조회 |
| P1-03 멱등성 연속 동작 | chat 응답 유실, structured 선택 재클릭, 확인 응답 재전송을 순서대로 발생 | 단계별 동일 `request_id` replay; 후속 단계는 별도 ID와 부모 메시지 연결 | user/assistant/부작용 기록이 논리 요청당 각 1건 | replay가 새 결과·새 record 생성, 부모 메시지 연결 유실 |
| P1-04 환자 격리 | 두 합성 환자를 독립 브라우저 컨텍스트 또는 인증 세션으로 실행 | Browser payload에는 `patient_id` 없음; Backend가 세션별로 주입 | 각 환자 메시지·Snapshot·기록이 자기 `patient_id`에만 연결 | 다른 환자의 약·식사·정책·부작용 노출 |
| P1-05 피드백·복사 회귀 | 실제 assistant 응답 hover/focus → 좋아요·싫어요·의견·복사, 사용자 메시지 복사 | 피드백 BFF만 호출; 반응은 상호 배타적; 의견은 암호화 전달 | 피드백 대상 assistant 공개 ID와 환자·대화 일치 | 다른 메시지 연결, 반응 중복 활성, 의견 평문 업무 DB 저장 |
| P1-06 복약·영양 target resolver | 복약 완료, 기존 식사·음식 수정·삭제를 이름·날짜·슬롯·순번으로 요청 | 모델은 semantic selector만 전송; AI Server가 직전 Read Tool 원본에서 공개 대상 ID를 resolve·고정 | 재시도에도 같은 대상 ID와 version 사용; 다른 환자·동명이행 선택 금지 | 모델 schema가 `dose_event_id`, `meal_id`, `food_id`를 요구하거나 retry 시 다른 대상 선택 |

## 6. availability와 실패 응답 판정

| 상태 | LLM·Tool 정책 | UI 기대값 |
|---|---|---|
| `available` | 해당 Snapshot 값을 개인화 판단에 사용 | 정상 응답 |
| `not_found` | 조회는 성공했으나 오늘/현재 기록이 없음을 사실대로 설명 | 예: “오늘 등록된 식사 기록이 없습니다.” |
| `not_supported` | 해당 도메인을 사용하지 않고 지원 범위만 설명 | 다른 available 정보로 답변 가능 |
| 일부 도메인 `unavailable` | 질문을 안전하게 답할 수 있으면 제한사항과 함께 LLM 응답 허용; 그 도메인이 필요한 평가·쓰기는 차단 | 명시적인 부분 조회 제한 |
| Backend Read 전체 장애 또는 환자 범위 검증 실패 | LLM과 Tool을 호출하지 않고 계약 오류 반환 | 실패 버블·재시도, fallback 답변 없음 |

`not_found`, `not_supported`, `unavailable`을 모두 “PHR이 등록되지 않았다”로
표현하면 실패다. LLM이 응답할 수 있는 부분 장애에서도 조회하지 못한 정보를
추정하거나 개인화된 사실처럼 표현하면 실패다.

## 7. Playwright CLI 실행법

### 7.1 사전 확인

```powershell
Get-Command npx
```

`npx`가 확인되면 저장소 루트에서 수동 확인용 9000 스택을 기동할 수 있다.
이 명령은 Backend+AI+worker를 기동하지만 고정 runtime DB를 사용한다.

```powershell
.\tools\da_drug_9000_stack.ps1 `
  -Action start `
  -EnvFile .env.9000 `
  -AgentEnvFile .env.agent_app.secret
.\tools\da_drug_9000_stack.ps1 `
  -Action verify `
  -EnvFile .env.9000 `
  -AgentEnvFile .env.agent_app.secret
```

상태 변경을 포함하는 P0 합격 실행은 격리 DB를 만드는 아래 runner를 사용한다.

```powershell
.\tools\run_real_llm_playwright.ps1
```

### 7.2 CLI 기본 루프

Playwright CLI는 반드시 `open → snapshot → ref 기반 조작 → snapshot` 순서로
사용한다. DOM이 바뀌면 이전 ref를 재사용하지 않는다.

```powershell
function Invoke-PlaywrightCli {
    & npx --yes --package @playwright/cli playwright-cli @args
}

Invoke-PlaywrightCli --session phr-snapshot open http://127.0.0.1:9000 --browser chrome --headed
Invoke-PlaywrightCli --session phr-snapshot snapshot
```

스냅샷에서 확인한 실제 ref로 조작한다. 아래 `eX`는 예시이며 고정 selector가
아니다.

```powershell
Invoke-PlaywrightCli --session phr-snapshot tracing-start
Invoke-PlaywrightCli --session phr-snapshot click eX
Invoke-PlaywrightCli --session phr-snapshot snapshot
Invoke-PlaywrightCli --session phr-snapshot fill eX "암로디핀을 먹고 속이 메스꺼웠어"
Invoke-PlaywrightCli --session phr-snapshot click eX
Invoke-PlaywrightCli --session phr-snapshot snapshot
Invoke-PlaywrightCli --session phr-snapshot requests
Invoke-PlaywrightCli --session phr-snapshot console error
Invoke-PlaywrightCli --session phr-snapshot screenshot
Invoke-PlaywrightCli --session phr-snapshot tracing-stop
Invoke-PlaywrightCli --session phr-snapshot close
```

실행 증적은 새 최상위 폴더를 만들지 않고
`outputs/playwright/<scenario>-<run-id>/` 아래에 저장한다.

### 7.3 CLI에서 확인할 접근성 기준

- tab: `HOME`, `Chat`
- heading: `오늘 복약 일정`, `에이전트와의 대화`
- textbox: `AI 에이전트에게 질문하기`
- send button: `메시지 보내기`
- conversation log: `대화 내용`
- status region: `서버 상태`
- structured response: assistant의 `data-message-type`
- user retry: 실패 버블 내부 `다시 시도`

## 8. 네트워크·PostgreSQL 사후검증 방법

Playwright만으로 내부 SELECT 실행 여부를 단정하지 않는다. 한 시나리오의 합격은
다음 세 증거를 함께 만족해야 한다.

1. 브라우저
   - 화면 상태, 구조화 응답, optimistic UI, 실패·재시도
2. 네트워크
   - `playwright-cli requests`와 서비스 테스트 probe
   - PHR HTTP 호출 부재
   - LLM Tool schema의 최소 인자
   - AI→Backend 동기 쓰기 횟수와 `request_id`
3. PostgreSQL 또는 승인된 테스트 API
   - Backend 공개 ID, version, 레코드 수
   - replay 전후 레코드 수 불변
   - AI DB에 환자 업무 레코드 부재
   - AI Backend DB write probe 거절

브라우저 P0 시나리오의 식사 count는
`GET /api/ui/v1/nutrition`으로 판정한다. 더 깊은 저장소 정합성, Agent DB
업무 레코드 부재, reader role write 거절은
`SYSTEM_POSTGRES_TEST_DATABASE_URL`/`AGENT_POSTGRES_TEST_DATABASE_URL` 기반
통합 테스트에서 직접 확인한다. Playwright 도구는 SQLite 파일 경로나
Python `sqlite3` oracle을 사용하지 않는다.

실환자 자유문장, 실환자 원문, 실 Snapshot 원문은 테스트 로그와 trace에 남기지
않는다. 사후검증 출력은 원칙적으로 합성 ID, count, status, hash, Tool 이름만
보존한다. 브라우저 요청·재시도·화면 상태 자체를 검증해야 하는 격리 실행에서는
합성 테스트 payload만 제한적으로 trace에 보존할 수 있으며, 산출물 접근과 보존
기간을 테스트 정책으로 통제한다.

## 9. UI와 독립적인 특수 검증

- MCP Tool 계약 검증은 모든 입력이 폐쇄되어 있는지 확인한다. 부작용 Tool은
  `patient_id`, 복약 목록, 메시지·기록 ID, version이 모델 입력 스키마에 없어야
  한다. 복약·영양 변경 Tool의 공개 target ID 제거는 P1-06 resolver 완료 조건으로
  별도 추적한다. 공개 `policy_id`는 확정 예외다.
- Snapshot Loader 통합 테스트는 날짜 경계, availability, 환자 격리, Read-only
  권한을 직접 검증한다.
- 실제 Bedrock 검증은 비용과 응답 변동성 때문에 별도 실행하되, 문구 전체
  일치보다 구조화 응답, Tool 선택, 서버 주입, DB side effect를 판정한다.
- persona challenge lab은 탐색용이며 제품 UI 합격 게이트를 대신하지 않는다.

## 10. 완료 조건

- 기존 React 회귀와 읽기 전용 스모크가 모두 통과한다.
- P0 전 항목이 격리된 목표 구조 스택에서 통과한다.
- P1은 날짜 경계와 다중 환자 배포 전에 통과한다.
- 실패 시 screenshot, trace, 네트워크 요약, redacted DB 사후검증 결과를
  `outputs/playwright/`에 남긴다.
- 별도 PHR 서비스나 `phr_patient_key`가 테스트 성공의 전제가 되면 목표 구조
  검증으로 인정하지 않는다.

## 11. 2026-07-30 실행 결과

### 자동 회귀

- Python 전체: `727 passed, 2 skipped`
- React 런타임: `18 passed`
- React production build: 성공
- 브라우저 CI smoke: 성공

### 격리된 v1.3 실제 서비스 경계 Playwright

이 실행은 명시적인 `deterministic_test` provider를 사용하는 합격 게이트다.
규칙 기반 운영 fallback이 아니라, 별도 PostgreSQL 스키마의
Backend·Agent·worker·React 실제 프로세스 경계와 계약을 결정적으로 검증한다.

증적:

- v1.3 실제 서비스 구조·상태·채팅·피드백 검증:
  `output/playwright/v13-real-service-1785341645145-1755bbfe/`
- 결과 요약:
  `output/playwright/v13-real-service-1785341645145-1755bbfe/p0-real-service-validation/summary.json`

| 항목 | 결과 |
|---|---|
| React 루트와 HOME/Chat | 통과 |
| Backend 상태 | 실제 probe 근거와 함께 `정상` 표시 |
| AI Server 상태 | 격리 provider generation probe 근거와 함께 `정상` 표시 |
| 오늘 Snapshot의 화면 입력 | 4종 복약 일정 확인 |
| 미복용 비동기 경로 | 알림·Agent worker·Callback·후속 대화 생성 통과 |
| 동기 채팅 | NDJSON `start → text_delta → completed` 통과 |
| optimistic user bubble | 전송 직후 `전송 중` 상태로 즉시 한 번 표시 |
| 피드백 경계 | assistant 반응·의견 요청이 Agent까지 `202`로 접수됨 |
| fallback 금지 | Agent 연결 실패를 강제로 발생시켜 대체 답변이 없음을 확인 |
| Frontend 신뢰 경계 | POST 본문에 `patient_id`, Snapshot, 기록 ID, version 없음 |
| 별도 PHR 호출 | 브라우저 네트워크에서 0건 |

현재 수동 9000 스택의 실제 Bedrock 최소 generation probe는
`GENERATION_PROVIDER_INVOCATION_FAILED`를 반환했다. 따라서 P0-03~P0-05의
실제 LLM 문장 생성과 UI 승인 완료 시나리오는 이번 실행에서 성공 판정하지
않았다. AWS가 반환한 직접 원인은 만료되었거나 유효하지 않은 security
token이며, UI와 readiness는 이를 정상으로 위장하지 않고 `not_ready`로
표시한다.

부작용 Snapshot 평가·약 후보 선택·확인 카드·AI Server 소유의 일회용 승인
키·Backend v1.3 동기 저장·동일 `request_id` replay는 Python 계약/통합
테스트로 검증했다.
Bedrock 접근이 복구되면 동일 Playwright 계획의 P0-03~P0-05를 다시 실행해
브라우저 성공 경로까지 최종 승인한다.
