# 복약 알림 POC

FastAPI Backend인 `system_app`, LangGraph AI Server인 `agent_app`, React
Frontend로 구성된 복약 알림 POC입니다. 환자·복약·식사·정책 데이터의 원장은
Backend DB이며, AI Server는 승인된 `ai_v13_*` View를 Read-only로 조회합니다.

Backend↔AI Server 외부 API는 연동규격 v1.3을 기준으로 구현합니다. 코드 반영
범위와 검증 항목은
[`docs/INTERFACE_SPEC_V13_IMPLEMENTATION_STATUS.md`](docs/INTERFACE_SPEC_V13_IMPLEMENTATION_STATUS.md)를 참고하세요.
한 명의 환자를 대상으로 복약 등록, 시간 시뮬레이션, 복약 알림/대화 알림, 미복용 후속 질문, AI 기반 알림 정책 최적화를 한 화면에서 검증할 수 있습니다.

## 구성

- `system_app`
  - Backend DB는 운영·로컬 테스트베드·자동 테스트 모두 PostgreSQL 사용
  - 복약 이벤트 생성, 시뮬레이션 시계, 알림 센터, React용 BFF API
  - 빌드된 React 앱을 루트(`/`)에서 제공
  - AI 응답 검증 후 정책 적용
- `frontend`
  - React 기반 테스트베드 화면
  - `npm run build` 시 `system_app/static/react`에 배포 자산 생성
- `agent_app`
  - 독립 LangGraph 중심 에이전트 서버
  - 채팅 시작 시 Backend DB에서 현재 환자 Snapshot을 구성
  - Snapshot을 재사용하는 복약·부작용·영양 Tool 실행
  - 업무 쓰기는 Backend v1.3 동기 API로만 요청
  - 비동기 작업은 API 서버와 별도 worker(`python -m agent_app.worker_main`)가 DB queue에서 처리
  - Langfuse 전송은 요청 경로와 분리된 durable outbox exporter
    (`python -m agent_app.langfuse_exporter_main`)가 처리
- AI Internal DB
  - 동기·비동기·실패 Trace/Step, token/cost, 요청 멱등성, 환자별 동시성
    lock, Tool 실행 상태의 단일 원장
  - Trace 계열 데이터는 임상 원문 대신 hash/redaction을 저장하고 3년 뒤 삭제
  - 비동기 queue/worker heartbeat, Backend write retry, 사용자 확인 대기,
    feedback 처리 상태 저장
  - 환자 업무 데이터의 원장으로 사용하지 않음
- `shared`
  - 공용 설정과 API 스키마

## 빠른 실행

1. Python 3.11+ 설치
2. 가상환경 생성 후 의존성 설치

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

3. 환경변수 설정

```powershell
Copy-Item .env.agent_app.example .env.agent_app
Copy-Item .env.agent_app.secret.example .env.agent_app.secret
Copy-Item .env.system_app.example .env.system_app
```

활성 경로의 공용 설정은 `.env.agent_app`, `.env.system_app`에 각각 둡니다.
Bedrock credential은 `.env.agent_app.secret`에만 두며 Agent API와 worker만
이 overlay를 읽습니다. Backend, migration, verifier, Langfuse exporter에는
전달하지 않습니다.
공용 `.env`는 자동으로 읽지 않습니다. 테스트처럼 하나의 명시적 환경 파일을
사용할 때만 `DA_DRUG_ENV_FILE`에 해당 경로를 지정합니다.
`system_app`의 내부 Agent API를 보호하려면 `.env.agent_app`와
`.env.system_app`에 같은 `INTERNAL_API_TOKEN` 값을 설정합니다. 이 값은 개발,
테스트베드, 운영 환경 모두에서 필수이며 누락되면 서비스가 시작되지 않습니다.

`agent_app` 필수 값:

- `LLM_PROVIDER=bedrock_anthropic`
- `LLM_MODEL_TIER=sonnet`
- `LLM_FAST_MODEL=global.anthropic.claude-haiku-4-5-20251001-v1:0`
- `LLM_SONNET_MODEL=global.anthropic.claude-sonnet-4-6`
- `LLM_REASONING_ENABLED=true`
- `LLM_REASONING_EFFORT=medium`
- `LLM_EXTENDED_THINKING_BUDGET_TOKENS=1024`
- `AWS_REGION=ap-northeast-2`
- `.env.agent_app.secret`의 `AWS_BEARER_TOKEN_BEDROCK`

선택 값:

- `.env.agent_app.secret`의 `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`: Bearer token 대신 IAM access key를 쓰는 경우
- `.env.agent_app.secret`의 `AWS_SESSION_TOKEN`: 임시 자격 증명을 쓰는 경우
- `.env.agent_app.secret`의 `AWS_PROFILE`: 로컬 AWS profile을 쓰는 경우
- `LLM_MAX_TOKENS=4096`
- `LLM_TEMPERATURE=0.2`
- `AGENT_DATABASE_URL=postgresql+psycopg://...`: AI Internal PostgreSQL runtime role
- `AGENT_MIGRATION_DATABASE_URL=postgresql+psycopg://...`: 필수 전용 DDL role
- `AGENT_STARTUP_MIGRATIONS_ENABLED=false`: 모든 환경의 필수값. API/worker는 schema를 검증만 하며 DDL을 실행하지 않습니다.
- `AGENT_DB_POOL_*`, `BACKEND_READ_DB_POOL_*`: Agent RW와 Backend Read-only connection pool/timeout을 독립 조정
- `AGENT_TASK_MAX_ATTEMPTS`, `AGENT_TASK_VISIBILITY_TIMEOUT_SECONDS`, `AGENT_TASK_RETRY_BASE_SECONDS`, `AGENT_TASK_RETRY_MAX_SECONDS`: agent 비동기 작업 재시도/lock timeout 조정값
- `AGENT_EMBEDDED_WORKER_ENABLED=false`: 운영 권장값. agent API 서버 안에서 worker thread를 띄우지 않고 별도 worker 프로세스를 사용합니다.

사용할 모델 등급과 모델 ID는 `agent_app` 환경변수로 관리하며 환자용 React 화면에서는 변경하지 않습니다.
`AWS_BEARER_TOKEN_BEDROCK`를 쓸 때는 Bedrock Converse API를 호출합니다. 서울 리전(`ap-northeast-2`)에서는 Claude Haiku 4.5/Sonnet 4.6을 Global Cross-Region inference로 호출하므로 `global.`로 시작하는 model ID를 기본값으로 둡니다.
reasoning을 켜면 Haiku 4.5는 `LLM_EXTENDED_THINKING_BUDGET_TOKENS` 기반 Extended Thinking을, Sonnet 4.6은 `LLM_REASONING_EFFORT` 기반 Adaptive Thinking을 사용합니다. Haiku budget은 1,024 이상이면서 `LLM_MAX_TOKENS`보다 작아야 합니다.
AWS 콘솔의 Bedrock Model access에서 Anthropic 모델 접근 권한을 먼저 활성화해야 합니다.

4. 에이전트 서버 실행

LangGraph 에이전트 API 서버:

```powershell
$env:DA_DRUG_SERVICE = "agent_app"
$env:DA_DRUG_ENV_FILE = ".env.agent_app,.env.agent_app.secret"
uvicorn agent_app.main:app --host 127.0.0.1 --port 8001 --reload
```

Docker Compose에서는 `agent_app.main:app`가 `agent-app` 서비스의 8001 포트에서 실행되고, 비동기 작업 처리는 `agent-worker` 서비스가 담당합니다.
`agent-migrate`가 먼저 완료된 뒤 두 runtime 서비스가 시작됩니다.

5. 시스템 서버 실행

Backend 전용 migration을 먼저 실행합니다.

```powershell
python -m system_app.migrate
```

```powershell
uvicorn system_app.main:app --host 127.0.0.1 --port 8000 --reload
```

6. 에이전트 worker 실행

비동기 미복용 처리와 일일 패턴 분석·정책 제안 전달을 처리합니다.
미복용은 Backend가 최소 식별자만 전달하고 결과를 전용 Callback으로 받습니다.
일일 패턴 분석은 Backend가 매일 02:00에
`/agent/async/daily-medication-pattern-analysis`를 호출해 시작하며, 정책
변경안이 생성된 경우에만 Backend의 제안 Callback으로 전달합니다.
사용자 채팅과 필요한 Tool continuation은 `/agent/sync/chat`의 동일 요청 안에서
최종 응답까지 처리합니다.

```powershell
python -m agent_app.worker_main
```

7. 브라우저에서 `http://127.0.0.1:8000` 접속

Langfuse를 활성화한 환경에서는 별도 exporter도 실행합니다.

```powershell
python -m agent_app.langfuse_exporter_main
```

Self-hosted Langfuse endpoint와 UI는 내부 관리자 망에 두고,
`.env.agent_app`의 `LANGFUSE_*` project key/URL을 설정합니다. trace,
score, sampling, 재시도, 보존·삭제 정책은
[`docs/AGENT_TRACE_LANGFUSE_MAPPING.md`](docs/AGENT_TRACE_LANGFUSE_MAPPING.md)를
따릅니다.

## 주요 시나리오

1. Backend에 준비된 테스트 복약 시나리오를 선택해 오늘 복약 일정을 등록한다.
2. `30분 진행`, `3시간 진행`, `60분/초`, `30분/초` 등의 배속 재생으로 시뮬레이션 시간을 움직인다.
3. 정해진 시각에 `medication_alert`가 발생한다.
4. 알림 횟수, 간격, 미복용 판단 시간, 알림 문구는 `data/default_notification_policies.xlsx`의 기본 정책과 환자별 DB override를 해석해 결정된다.
5. 미복용 AI 알림은 `AI가 대화를 요청합니다.` 형식으로 뜨고, 환자는 `채팅 열기`로 이동해 일반 멀티턴 채팅에서 답변한다.
6. 채팅 요청마다 AI Server가 Backend DB에서 최소 프로필, 활성 질환·치료, 오늘 복약·식사, 현재 알림 정책 Snapshot을 Read-only로 조회한다.
7. 환자 응답에 부작용 의심 표현이 있으면 Agent가 같은 Snapshot의 복약정보와 AI Server의 의약품 기준정보를 사용해 평가하고, 필요하면 PRO-CTCAE 자기보고식 문항을 표시한다.
8. 확인된 부작용 기록, 복약 상태, 식사 및 정책 변경은 AI DB에 직접 쓰지 않고 Backend 동기 쓰기 API를 통해 반영한다.

### 채팅 응답 스트리밍

- React는 Backend BFF의 `POST /api/ui/v1/chat/stream`을 사용한다.
- Backend→AI Server는 별도 `/stream` 경로 없이
  `POST /agent/sync/chat` 하나만 호출하며, 응답 미디어 타입은
  `application/x-ndjson`이다.
- Claude의 reasoning과 Tool 호출·결과는 AI Server 내부에서만 처리하며,
  모든 Tool continuation이 끝난 뒤 생성되는 최종 `text` 블록만 전달한다.
- `selection_box`와 `input_box`는 부분 토큰으로 보내지 않고 검증된
  `completed` 이벤트에서 완성된 단위로 전달한다.
- NDJSON 이벤트의 `sequence`는 0부터 1씩 증가하며, 각 줄은
  `streaming`, `completed`, `error` 중 하나다. 요청은 정확히 하나의
  `completed` 또는 `error` 이벤트로 종료한다.
- Backend는 모든 이벤트의 `request_id`·`message_id`·`sequence`를 검증하고
  최종 응답만 Backend DB에 assistant 메시지로 저장한다.

## EC2에서 Docker Compose로 실행

EC2 배포는 Docker Compose 방식을 권장합니다. VSCode Remote SSH로 EC2에 접속한 뒤, 저장소 루트에서 아래 순서로 실행합니다.

```bash
chmod +x scripts/docker_compose.sh
scripts/docker_compose.sh init
nano .env.agent_app
nano .env.agent_app.secret
nano .env.system_app
scripts/docker_compose.sh up
```

최소한 `.env.agent_app`의 아래 공용 값을 실제 값으로 바꿉니다. Compose 내부에서는
Backend와 AI 간 URL을 서비스 이름으로 override합니다. 활성 v1.3 채팅 경로는
Backend Patient Snapshot을 사용하며 별도 PHR 서비스 접속 설정을 사용하지
않습니다.

```bash
LLM_PROVIDER=bedrock_anthropic
LLM_MODEL_TIER=sonnet
AWS_REGION=ap-northeast-2
INTERNAL_API_TOKEN=<same-random-token-in-system-and-agent-env>
```

Bedrock bearer는 `.env.agent_app.secret`에만 설정합니다.

```bash
AWS_BEARER_TOKEN_BEDROCK=...
```

`scripts/docker_compose.sh init`은 Backend·AI common 파일과 Agent runtime
credential overlay를 생성합니다. 별도 PHR 서비스는 저장소와 실행 구성에서
제거되었습니다.
Secret overlay는 생성 시 `umask 077`을 사용하고 매 실행 전에 mode `600`으로
고정합니다.
`up`, `dev`, `restart`는 기동 뒤 `agent-app` 컨테이너 내부에서
`AGENT_SYNC_API_TOKEN`을 메모리로 읽어 `/health/generation/ready`를
인증 호출합니다. 실제 provider가 `status=ready`를 반환하지 않으면 명령이
실패하며 token은 argv나 출력에 포함하지 않습니다.

Compose 구성은 아래처럼 동작합니다.

```text
system-app    0.0.0.0:8000 -> EC2 외부 공개
agent-migrate Agent schema migration 전용 one-shot
agent-app     Docker network 내부 8001, 요청 접수
agent-worker  내부 DB queue 처리 및 callback 전송
agent-langfuse-exporter PHI-free outbox를 내부 Langfuse로 전송
data/         호스트 ./data를 /app/data로 mount
```

EC2 보안그룹은 기본적으로 `22/tcp`와 `8000/tcp`만 엽니다.
`agent_app:8001`은 외부에 열지 않습니다.

운영 명령:

```bash
scripts/docker_compose.sh status
scripts/docker_compose.sh logs
scripts/docker_compose.sh restart
scripts/docker_compose.sh down
```

EC2 안에서 VSCode로 코드를 수정하면서 바로 반영하고 싶으면 dev 모드로 실행합니다. 이 모드는 소스 전체를 컨테이너에 bind mount하고 `uvicorn --reload`를 사용합니다.

```bash
scripts/docker_compose.sh dev
```

브라우저에서는 `http://<EC2_PUBLIC_IP>:8000`으로 접속합니다.

EC2에 Docker가 없다면 Ubuntu 기준으로는 아래처럼 설치합니다.

```bash
sudo apt update
sudo apt install -y docker.io docker-compose-plugin
sudo usermod -aG docker $USER
newgrp docker
```

Amazon Linux 계열은 아래처럼 설치합니다.

```bash
sudo dnf install -y docker
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
newgrp docker
```

## EC2에서 venv로 실행

Docker 없이 실행해야 할 때만 아래 방식을 사용합니다. VSCode Remote SSH로 EC2에 접속한 뒤, 저장소 루트에서 아래 순서로 실행합니다.

```bash
chmod +x scripts/run_ec2_services.sh
scripts/run_ec2_services.sh install
scripts/run_ec2_services.sh start
```

첫 실행 스크립트는 `.env.agent_app`, `.env.agent_app.secret`,
`.env.system_app`을 생성합니다. 생성 직후 `.env.agent_app.secret`의 Bedrock
인증 값을 채운 뒤 다시 시작합니다.
Secret overlay는 생성 시와 기존 파일 재사용 시 모두 mode `600`으로 제한됩니다.

```bash
nano .env.agent_app
nano .env.agent_app.secret
nano .env.system_app
scripts/run_ec2_services.sh start
```

최소한 `.env.agent_app`에서 아래 공용 값을 실제 값으로 바꿉니다.

```bash
LLM_PROVIDER=bedrock_anthropic
LLM_MODEL_TIER=sonnet
AWS_REGION=ap-northeast-2
INTERNAL_API_TOKEN=<same-random-token-in-system-and-agent-env>
```

`.env.agent_app.secret`에는 Bedrock bearer만 설정합니다.

```bash
AWS_BEARER_TOKEN_BEDROCK=...
```

`start`, `start-agent`, `restart`, `restart-agent`도 동일한 authenticated
generation readiness gate를 통과해야 성공합니다. 필요하면
`scripts/run_ec2_services.sh verify-generation`으로 다시 확인할 수 있습니다.

서비스 URL은 `.env.system_app`, `.env.agent_app`에서 나눠 관리할 수 있습니다.

```bash
AGENT_BASE_URL=http://127.0.0.1:8001
SYSTEM_BASE_URL=http://127.0.0.1:8000
BACKEND_READ_DATABASE_URL=postgresql+psycopg://ai_reader:...@127.0.0.1:5432/backend
```

서비스 runtime, fallback, 테스트와 이관 도구에는 SQLite가 남아 있지
않습니다. Backend DB와 AI Internal DB는 모든 환경에서 PostgreSQL의 서로
다른 database/role로 구성합니다.
같은 PostgreSQL 인스턴스를 사용할 수 있지만 논리 DB와 권한은 분리합니다.
로컬 Playwright도 SQLite 파일을 생성하거나 직접 조회하지 않으며, 화면 상태는
Backend 테스트 API, 저장소 경계는 격리된 PostgreSQL 통합 테스트로 검증합니다.

v1.3 Contract Test Server는 Backend/System DB나 Agent DB를 재사용하지 않고
전용 `dranswer_contract` PostgreSQL을 사용합니다. `.env.contract.example`과
`.env.contract-migration.example`을 각각 복사해 runtime/migration URL을
분리한 뒤 선택 profile로 실행합니다.

```bash
docker compose --profile contract-test-server run --rm contract-migrate
docker compose --profile contract-test-server up -d contract-test-server
```

callback 전달이 필요한 경우에만 `CALLBACK_MODE=deliver`를 설정하고
`contract-callback-worker`를 함께 실행합니다. API와 worker는 DDL을 수행하지
않습니다.

Ubuntu 예시는 아래와 같습니다.

```bash
sudo apt update
sudo apt install -y postgresql postgresql-contrib
sudo -u postgres createuser agent_app_rw
sudo -u postgres createuser agent_migration
sudo -u postgres createdb -O agent_migration dranswer_agent
```

Compose 배포와 직접 프로세스 실행 모두 아래 Agent common 값을
`.env.agent_app`에 설정합니다. Bedrock credential은
`.env.agent_app.secret`, Backend 전용 값은 `.env.system_app`에 분리합니다.

```bash
APP_ENV=production
AGENT_DATABASE_URL=postgresql+psycopg://agent_app_rw:change-me@127.0.0.1:5432/dranswer_agent?sslmode=require
AGENT_MIGRATION_DATABASE_URL=postgresql+psycopg://agent_migration:change-me@127.0.0.1:5432/dranswer_agent?sslmode=require
AGENT_STARTUP_MIGRATIONS_ENABLED=false
AGENT_TASK_MAX_ATTEMPTS=3
AGENT_TASK_VISIBILITY_TIMEOUT_SECONDS=300
AGENT_TASK_RETRY_BASE_SECONDS=30
AGENT_TASK_RETRY_MAX_SECONDS=600
```

API나 worker를 시작하기 전에 전용 migration을 한 번 실행합니다. Runtime은
모든 환경에서 DDL을 실행하지 않으며 schema가 최신이 아니면
기동에 실패합니다.

```bash
python -m system_app.migrate
python -m agent_app.migrate
python tools/verify_testbed_contract.py \
  --scope config \
  --env-file .env.system_app,.env.agent_app
```

Docker Compose는 이를 `system-migrate → system-app`과
`agent-migrate → agent-app/agent-worker` 순서로 자동 실행합니다.
PostgreSQL이 EC2 host에 있으면 DB URL host를
`host.docker.internal`로 설정합니다. 전체 배포·검증·rollback 순서는
[`docs/POSTGRESQL_CUTOVER_RUNBOOK.md`](docs/POSTGRESQL_CUTOVER_RUNBOOK.md)를
따릅니다.

최대 연결 수는 각 프로세스의 `pool_size + max_overflow` 합입니다. 예를 들어
Agent API 2개와 worker 2개가 각각 기본 Agent pool `5 + 10`을 사용하면 AI
Internal DB 최대 연결은 60개입니다. Backend Read-only pool은 별도로 같은
방식으로 산정하고 PostgreSQL `max_connections`보다 충분히 낮게 설정합니다.

EC2 보안그룹은 기본적으로 `22/tcp`와 `8000/tcp`만 엽니다.
`agent_app:8001`과 DB 포트는 외부에 공개하지 않습니다.

레거시 PHR 프로세스는 실행하지 않습니다. 활성 의존 순서는 Backend
migration/read View 준비 → Agent migration → AI API → AI worker → Backend
Frontend 제공입니다. `agent_app`은 요청 접수 API, `agent_worker`는 DB queue
처리와 app callback을 담당합니다.

```bash
scripts/run_ec2_services.sh status
scripts/run_ec2_services.sh logs
scripts/run_ec2_services.sh restart
scripts/run_ec2_services.sh restart-agent
scripts/run_ec2_services.sh status-agent
scripts/run_ec2_services.sh logs-agent
scripts/run_ec2_services.sh stop
```

브라우저에서는 `http://<EC2_PUBLIC_IP>:8000`으로 접속합니다. 개발 중 자동 reload가 필요하면 `RELOAD=1 scripts/run_ec2_services.sh restart`를 사용합니다.

## Excel 워크북

첫 실행 시 `data/prompt_registry.xlsx`와 `data/default_notification_policies.xlsx`가 자동 생성됩니다.

정책 workbook은 `notification_policies`, `policy_boundaries`, `system_policies` 시트로 구성됩니다. 정책 값은 `DB override -> Excel exact slot -> Excel wildcard default` 순서로 해석하고, 허용 바운더리는 `policy_boundaries.policy_key`가 최종 정책의 `policy_key`와 일치하는 행으로 해석합니다. `system_policies`의 `daily_pattern_conversation_time`은 일일 패턴 결과를 바탕으로 환자에게 정책 대화를 요청하는 시각이며 기본값은 `08:30`입니다. 값을 수정할 때는 `HH:MM` 형식을 사용합니다. 정책 workbook을 수정한 뒤에는 시스템 서버 재시작 또는 `POST /admin/policies/reload`로 수동 reload할 수 있습니다. 멀티턴 대화에서 환자가 이 시간을 명확히 바꾸도록 요청하면 확인 알림을 거쳐 사용자가 선택한 뒤 DB override로 반영되며, 활성 알림 정책 패널에서 현재 값을 확인할 수 있습니다.

프롬프트 workbook 시트 구성:

- `agent_prompts`: 에이전트별 활성 버전
- `prompt_versions`: 프롬프트 본문/스키마/해시/변경 사유
- `change_log`: 버전 전환 이력
- `execution_logs`: 모든 프롬프트 호출 로그

워크북을 직접 수정해 활성 프롬프트 버전을 바꾸면, 런타임은 다음 호출부터 새 버전을 읽습니다.

## 문서

- `docs/INTERFACE_SPEC_V13_IMPLEMENTATION_STATUS.md`: 연동규격 v1.3 구현 상태와 운영 전 확인 항목
- `docs/V13_SINGLE_PATIENT_THREAD_BOUNDARY.md`: 환자당 단일 스레드와 식별자 경계
- `docs/AI_V13_FEEDBACK_POLICY.md`: 채팅 반응·의견 접수와 보존 정책
- `docs/ARCHITECTURE.md`: Backend DB, AI Server, AI 내부 DB의 소유권과 연결 구조
- `docs/AGENT_SYSTEM_MAP.md`: agent, tool, 정책 confirmation 흐름 그래프
- `docs/AGENT_APP_FEATURES.md`: `agent_app` 기능, 내부 agent, 출력 계약, 시나리오 테스트 정리
- `docs/LLM_PLAYWRIGHT_TEST_PLAN.md`: 실제 LLM 기반 Playwright 회귀 시나리오 계획
- `docs/agent_trigger_criteria_by_cancer.md`: 암종별 agent trigger 기획 정리
- `docs/simplified_cancer_care_loops.md`: 암종별 care loop 발표/논의용 그래프

## 테스트

```powershell
pytest -q
```

현재 저장소는 FastAPI 라우트, 시뮬레이션 로직, 에이전트 응답 검증, 알림 팝업 흐름에 대한 단위 테스트를 제공합니다.

정적 분석 도구는 dev dependency로 `ruff`를 사용합니다.

```powershell
ruff check .
```

운영 상태 확인은 기본 health와 상세 health를 제공합니다.

- `GET /health`
- `GET /health/details`
