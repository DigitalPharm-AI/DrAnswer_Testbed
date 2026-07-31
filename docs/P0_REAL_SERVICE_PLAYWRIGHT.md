# 실제 서비스 P0 Playwright 검증

이 검증기는 PostgreSQL 기반 Backend·AI Server·worker를 기동한 뒤 테스트베드
reset API로 합성 환자 상태를 초기화하고 실행한다. SQLite 파일을 만들거나
`SYSTEM_DB_PATH`로 로컬 DB 파일을 직접 조회하지 않는다. 따라서 운영 또는 여러
사용자가 공유하는 환경에는 실행하지 않고, `TESTBED_RESET_ENABLED=true`인 전용
로컬 테스트베드에서만 사용한다.

runner는 전용 migration 단계로 Backend와 Agent PostgreSQL schema를 확인한 뒤
서비스를 시작한다. 브라우저 시나리오는 reset 직후 시뮬레이션 시계를 정지하고,
성공·실패와 무관하게 runner가 시작한 서비스와 worker 프로세스 트리를 종료한다.
Backend 식사 상태 판정도 SQLite SELECT가 아니라 동일 origin의
`GET /api/ui/v1/nutrition`을 사용한다.

```powershell
.\tools\run_real_llm_playwright.ps1 `
  -Script tools\playwright_p0_real_service_validation.js `
  -OutputPrefix p0-real-service `
  -EnvFile .env.9000 `
  -AgentEnvFile .env.agent_app.secret
```

실행 전에 React production build와 실제 LLM provider 설정이 준비되어 있어야
한다. `.env.9000`은 common 설정만 담고 Bedrock bearer는
`.env.agent_app.secret`에 둔다. Runner는 credential overlay를 Agent API와
worker에만 전달한다. AI generation readiness가 실제로 `ready`가 아니면
검증기는 건강한 것처럼 우회하지 않고 즉시 실패한다.

검증 범위는 다음과 같다.

- `/` React 루트와 Backend·AI Server 실제 readiness/UI 표시 일치
- 복합 복약 시나리오 4종과 `missed` 상태의 HOME 표시
- 구조화 응답이 정확한 assistant `source_message_id`를 보내는지 확인
- 첫 응답이 음식 후보 또는 입력 카드이면 실제 버튼/입력으로 해당 단계를
  순서대로 완료하고, 최대 8단계 안에 `기록`/`취소` 승인 카드에 도달하는지 확인
- 식사 기록은 `기록` 선택 전 Backend nutrition API의 식사 수가 불변이고,
  선택 후 정확히 1 증가하는지 확인
- 같은 구조화 요청 replay 후에도 Backend nutrition API의 식사 수가 추가
  증가하지 않는지 확인
- 브라우저가 동일 origin의 `/api/ui/v1/**` BFF만 호출하는지 확인
- 브라우저 요청·응답에 `patient_id`, 내부 숫자 ID, `version`이 노출되지 않는지 확인
- `PHR 미등록`, `phr_patient_key`, 임시/fallback 답변 문구가 표시되지 않는지 확인

알림 ID와 cursor도 Backend가 발급한 불투명 문자열만 허용한다. 숫자
`id`/`*_id`가 어느 UI API 요청·응답에서든 발견되면 내부 PK 노출로 실패한다.

실제 LLM이 음식 후보나 입력 카드를 먼저 반환하는 것은 정상 분기로 취급한다.
검증기는 각 카드의 실제 `source_message_id`를 사용해 응답하며, 중간 단계에서
식사가 저장되면 즉시 실패한다. 일반 `text`로 멈추거나 8단계 안에
`기록`/`취소` 승인 카드에 도달하지 못하면 실제 응답 유형, 선택지/입력,
Backend nutrition API count, 네트워크와 실패 화면을 남긴다. 테스트 데이터나
합성 선택지는 사용하지 않는다.

모든 결과는 실행기가 지정한 `REAL_LLM_SERVICE_OUTPUT_DIR` 아래 `p0-real-service-validation/`에 저장된다.

- `summary.json`: 단계별 판정, Backend API 전후 수, 실패 진단
- `network.json`: 브라우저 요청·응답과 구조화 요청 증거
- `01-*.png` ~ `05-*.png`: 주요 단계 화면
- `failure.png`, `browser-errors.json`: 실패 시 추가 증거

## v1.3 결정적 실서비스 경계 검증

Bedrock 호출 없이도 React → Backend SSE → AI Server NDJSON과 비동기
missed-dose callback 경계를 검증할 수 있다. 아래 runner는 실행마다 Backend와
Agent용 PostgreSQL schema를 새로 만들고, Backend schema의 계약 view만 읽을 수
있는 전용 reader role을 만든다. runner가 시작한 System·Agent·Agent worker
프로세스와 생성한 schema/role은 성공·실패와 관계없이 종료·삭제된다.

```powershell
$env:BROWSER_POSTGRES_TEST_DATABASE_URL = `
  "postgresql+psycopg://postgres@127.0.0.1:55432/dranswer_system"
npm run test:browser:v13
```

이 모드는 testbed에서만 허용되는 `deterministic_test` provider를 사용한다.
브라우저에는 실제 Backend BFF만 노출되며, Backend는 실제 Agent HTTP
`POST /agent/sync/chat` 응답을 NDJSON으로 파싱한다. 검증 범위는 다음과 같다.

- 사용자 말풍선이 terminal 응답 전에 즉시 `sending` 상태로 표시되는지
- SSE delta 순서와 단일 `completed` terminal, 공개 message ID 정합성
- 미복용 감지 → 비동기 Agent 요청 → worker → Backend callback → 알림의
  `대화 확인` → Agent 생성 채팅 메시지
- assistant 좋아요와 의견이 Backend를 거쳐 Agent feedback API에 접수되는지
- runner가 소유한 Agent 프로세스를 중단했을 때 명시적 오류가 표시되고
  Backend/React가 규칙 기반 assistant 대체 답변을 생성하지 않는지
- Agent/PostgreSQL receipt를 이용해 sync chat, feedback, missed-dose callback이
  실제 서비스 경계를 통과했는지

결과는 `output/playwright/v13-real-service-*/`에 저장된다. 그중
`service-boundary-evidence.json`은 브라우저 검증과 별도로 Agent/Backend DB의
격리 schema에서 확인한 실제 HTTP 처리 receipt 수를 담는다.

## PRO-CTCAE 실제 Workbook 성공 경로

`data/pro_ctcae_korean_parsed.xlsx`를 사용하는 실제 Bedrock 시나리오는 다음
순서로 검증한다.

1. 테스트베드를 초기화하고 `복합 복약` 일정을 적용한다.
2. 시뮬레이션 시각을 09:30까지 진행해 메트포르민 미복용 상태를 만든다.
3. Chat에서 `어제 약 먹고나서 메스꺼웠어`를 전송한다.
4. `관련 약 선택` 카드에서 `메트포르민 500mg`을 선택한다.
5. `메스꺼움 관련 자가 보고 설문`과 Workbook의 첫 표준 문항·선택지가
   `selection_box`로 표시되는지 확인한다.
6. Agent DB에서 첫 요청의 부작용 후보 Tool이 1회, 후속 요청의 부작용 Tool과
   PRO-CTCAE Tool이 각각 1회 실행됐는지 확인한다.

동일 Tool 이름과 동일 인자의 중복 호출은 실패다. PRO-CTCAE Tool 실행이
실패한 경우에도 프론트가 기술 오류나 임시 설문을 직접 만들면 실패다. 최종 응답
노드는 성공한 약물 근거와 실패한 Tool 결과를 함께 받아 자연어로 제한사항을
설명해야 하며, 표준 문항을 추정하거나 설문이 준비됐다고 말해서는 안 된다.

2026-07-29 실제 실행은 통과했다.

- 최초 요청: `req_995482f137a87858`
- 후속 약 선택 요청: `req_d6dd830a35b2a16a`
- 최종 trace: `945ab8c9-8ded-4303-9287-957db9e85f8f`
- Tool 결과: `get_medication_side_effect_assessment=SUCCESS` 1회,
  `get_pro_ctcae_questionnaire=SUCCESS` 1회
- 화면 증적:
  `output/playwright/pro-ctcae-real-service/symptom-to-pro-ctcae.png`
- 기계 판정:
  `output/playwright/pro-ctcae-real-service/summary.json`
