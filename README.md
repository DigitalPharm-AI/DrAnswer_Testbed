# 복약 알림 POC

FastAPI 기반 `system_app`, LangGraph 에이전트 `agent_app`, 가상 `phr_app`으로 구성된 복약 알림 POC입니다.
한 명의 환자를 대상으로 복약 등록, 시간 시뮬레이션, 복약 알림/대화 알림, 미복용 후속 질문, AI 기반 알림 정책 최적화를 한 화면에서 검증할 수 있습니다.

## 구성

- `system_app`
  - SQLite 운영 상태 저장
  - 복약 이벤트 생성, 시뮬레이션 시계, 알림 센터, 대시보드 UI
  - AI 응답 검증 후 정책 적용
- `agent_app`
  - 독립 LangGraph 중심 에이전트 서버
  - `system_app`의 기존 Agent API 계약과 호환
  - 현재 실행 대상 에이전트 구현
  - 비동기 작업은 API 서버와 별도 worker(`python -m agent_app.worker_main`)가 DB queue에서 처리
- `phr_app`
  - 가상 PHR FastAPI 서버
  - PHR 환자키, 환자 복약정보, 품목별 주의사항 free text seed DB 제공
  - Agent의 `lookup_side_effect_info` Tool 조회 대상
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
Copy-Item .env.example .env
Copy-Item .env.agent_app.example .env.agent_app
Copy-Item .env.system_app.example .env.system_app
Copy-Item .env.phr_app.example .env.phr_app
```

서비스별 설정은 `.env.agent_app`, `.env.system_app`, `.env.phr_app`에 둡니다. 기존 단일 `.env`는 공용 fallback으로 계속 읽히며, 앱별 파일 값이 있으면 앱별 값이 우선합니다.
`system_app`의 agent/admin mutation API를 보호하려면 `.env.agent_app`와 `.env.system_app`에 같은 `INTERNAL_API_TOKEN` 값을 설정합니다. 개발 환경에서는 값이 비어 있으면 로컬 POC 호환성을 위해 토큰 검사를 생략하지만, `APP_ENV=production`에서는 토큰이 없으면 `system_app`과 `agent_app`이 시작되지 않습니다.

`agent_app` 필수 값:

- `LLM_PROVIDER=bedrock_anthropic`
- `LLM_MODEL_TIER=fast`
- `LLM_FAST_MODEL=global.anthropic.claude-haiku-4-5-20251001-v1:0`
- `LLM_SONNET_MODEL=global.anthropic.claude-sonnet-4-6`
- `AWS_REGION=ap-northeast-2`
- `.env.agent_app`의 `AWS_BEARER_TOKEN_BEDROCK`

선택 값:

- `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`: Bearer token 대신 IAM access key를 쓰는 경우
- `AWS_SESSION_TOKEN`: 임시 자격 증명을 쓰는 경우
- `AWS_PROFILE`: 로컬 AWS profile을 쓰는 경우
- `LLM_MAX_TOKENS=4096`
- `LLM_TEMPERATURE=0.2`
- `PRO_CTCAE_EMBEDDING_PROVIDER=auto`: PRO-CTCAE 유사도 검색에 Bedrock 인증을 우선 재사용하는 경우
- `PRO_CTCAE_EMBEDDING_MODEL=amazon.titan-embed-text-v2:0`: Bedrock embedding 모델 기본값
- `AGENT_DATABASE_URL=postgresql+psycopg://...`: EC2 단일 서버에서 agent 비동기 queue를 PostgreSQL에 저장하는 경우
- `AGENT_TASK_MAX_ATTEMPTS`, `AGENT_TASK_VISIBILITY_TIMEOUT_SECONDS`, `AGENT_TASK_RETRY_BASE_SECONDS`, `AGENT_TASK_RETRY_MAX_SECONDS`: agent 비동기 작업 재시도/lock timeout 조정값
- `AGENT_EMBEDDED_WORKER_ENABLED=false`: 운영 권장값. agent API 서버 안에서 worker thread를 띄우지 않고 별도 worker 프로세스를 사용합니다.

프론트의 `AI 모델` 패널에서 `Fast`를 선택하면 `LLM_FAST_MODEL`, `Sonnet`을 선택하면 `LLM_SONNET_MODEL`이 에이전트 호출에 사용됩니다.
`AWS_BEARER_TOKEN_BEDROCK`를 쓸 때는 Bedrock Converse API를 호출합니다. 서울 리전(`ap-northeast-2`)에서는 Claude Haiku 4.5/Sonnet 4.6을 Global Cross-Region inference로 호출하므로 `global.`로 시작하는 model ID를 기본값으로 둡니다.
AWS 콘솔의 Bedrock Model access에서 Anthropic 모델 접근 권한을 먼저 활성화해야 합니다.

4. 가상 PHR 서버 실행

```powershell
uvicorn phr_app.main:app --host 127.0.0.1 --port 8002 --reload
```

5. 에이전트 서버 실행

LangGraph 에이전트 API 서버:

```powershell
uvicorn agent_app.main:app --host 127.0.0.1 --port 8001 --reload
```

Docker Compose에서는 `agent_app.main:app`가 `agent-app` 서비스의 8001 포트에서 실행되고, 비동기 작업 처리는 `agent-worker` 서비스가 담당합니다.

6. 시스템 서버 실행

```powershell
uvicorn system_app.main:app --host 127.0.0.1 --port 8000 --reload
```

7. 에이전트 worker 실행

비동기 daily pattern, missed dose, chat continuation, push/clinician callback을 처리합니다.

```powershell
python -m agent_app.worker_main
```

8. 브라우저에서 `http://127.0.0.1:8000` 접속

## 주요 시나리오

1. 복약 정보를 입력한다.
2. `설정 완료 및 PHR 등록`을 눌러 현재 복약 계획을 PHR에 전송하고 `phr_patient_key`를 발급받는다.
3. `30분 진행`, `3시간 진행`, `60분/초`, `30분/초` 등의 배속 재생으로 시뮬레이션 시간을 움직인다.
4. 정해진 시각에 `medication_alert`가 발생한다.
5. 알림 횟수, 간격, 미복용 판단 시간, 알림 문구는 `data/default_notification_policies.xlsx`의 기본 정책과 환자별 DB override를 해석해 결정된다.
6. 미복용 AI 알림은 `AI가 대화를 요청합니다.` 형식으로 뜨고, 환자는 `채팅 열기`로 이동해 일반 멀티턴 채팅에서 답변한다.
7. 환자 응답에 부작용 의심 표현이 있으면 Agent가 PRO-CTCAE 자기보고식 문항을 채팅에 표시하고, 필요하면 `phr_patient_key`로 PHR을 조회해 복용 중인 품목의 주의사항과 비교한다.
8. 다음날 일일 패턴 대화 요청 시각이 되거나 `오늘 패턴 분석` 버튼을 누르면 일일 패턴이 Agent로 전달되고 정책이 조정될 수 있다.

## EC2에서 Docker Compose로 실행

EC2 배포는 Docker Compose 방식을 권장합니다. VSCode Remote SSH로 EC2에 접속한 뒤, 저장소 루트에서 아래 순서로 실행합니다.

```bash
chmod +x scripts/docker_compose.sh
scripts/docker_compose.sh init
nano .env.agent_app
nano .env.system_app
scripts/docker_compose.sh up
```

최소한 `.env.agent_app`의 아래 값은 실제 값으로 바꿉니다. Compose 내부에서는 URL을 서비스 이름으로 자동 override하므로 `AGENT_BASE_URL`, `SYSTEM_BASE_URL`, `PHR_BASE_URL`은 로컬 기본값 그대로 둬도 됩니다.

```bash
LLM_PROVIDER=bedrock_anthropic
LLM_MODEL_TIER=fast
AWS_REGION=ap-northeast-2
AWS_BEARER_TOKEN_BEDROCK=...
INTERNAL_API_TOKEN=<same-random-token-in-system-and-agent-env>
```

`scripts/docker_compose.sh init`은 `.env`, `.env.agent_app`, `.env.system_app`, `.env.phr_app`이 없으면 각각의 example에서 생성합니다.

Compose 구성은 아래처럼 동작합니다.

```text
system-app    0.0.0.0:8000 -> EC2 외부 공개
agent-app     Docker network 내부 8001, 요청 접수
agent-worker  내부 DB queue 처리 및 callback 전송
phr-app       Docker network 내부 8002
data/         호스트 ./data를 /app/data로 mount
```

EC2 보안그룹은 기본적으로 `22/tcp`와 `8000/tcp`만 엽니다. `agent_app:8001`, `phr_app:8002`는 같은 Docker network 내부 통신용이므로 외부에 열지 않아도 됩니다.

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

첫 실행 때 `.env`, `.env.agent_app`, `.env.system_app`, `.env.phr_app`이 없으면 example에서 생성됩니다. 생성 직후에는 `.env.agent_app`의 Bedrock 인증 값을 채운 뒤 다시 시작합니다.

```bash
nano .env.agent_app
nano .env.system_app
scripts/run_ec2_services.sh start
```

최소한 `.env.agent_app`에서 아래 값은 실제 값으로 바꿉니다.

```bash
LLM_PROVIDER=bedrock_anthropic
LLM_MODEL_TIER=fast
AWS_REGION=ap-northeast-2
AWS_BEARER_TOKEN_BEDROCK=...
INTERNAL_API_TOKEN=<same-random-token-in-system-and-agent-env>
```

서비스 URL은 `.env.system_app`, `.env.agent_app`에서 나눠 관리할 수 있습니다.

```bash
AGENT_BASE_URL=http://127.0.0.1:8001
SYSTEM_BASE_URL=http://127.0.0.1:8000
PHR_BASE_URL=http://127.0.0.1:8002
```

agent 비동기 작업 queue는 SQLite 기본값으로도 개발 실행이 가능하지만, EC2에서 운영성 있게 쓰려면 같은 인스턴스의 PostgreSQL을 권장합니다. Ubuntu 예시는 아래와 같습니다.

```bash
sudo apt update
sudo apt install -y postgresql postgresql-contrib
sudo -u postgres createuser da_drug
sudo -u postgres createdb -O da_drug da_drug_agent
sudo -u postgres psql -c "ALTER USER da_drug WITH PASSWORD 'change-me';"
```

그 뒤 `.env.agent_app`에 아래처럼 설정합니다.

```bash
AGENT_DATABASE_URL=postgresql+psycopg://da_drug:change-me@127.0.0.1:5432/da_drug_agent
AGENT_TASK_MAX_ATTEMPTS=3
AGENT_TASK_VISIBILITY_TIMEOUT_SECONDS=300
AGENT_TASK_RETRY_BASE_SECONDS=30
AGENT_TASK_RETRY_MAX_SECONDS=600
```

Docker Compose로 agent 컨테이너를 띄우면서 PostgreSQL은 EC2 host에 둘 경우에는 `127.0.0.1` 대신 아래처럼 설정합니다.

```bash
AGENT_DATABASE_URL=postgresql+psycopg://da_drug:change-me@host.docker.internal:5432/da_drug_agent
```

EC2 보안그룹은 기본적으로 `22/tcp`와 `8000/tcp`만 엽니다. `agent_app:8001`, `phr_app:8002`는 같은 인스턴스 내부 통신용이므로 외부에 열지 않아도 됩니다.

실행 스크립트는 `phr_app -> agent_app -> system_app -> agent_worker` 순서로 서비스를 띄웁니다. `agent_app`은 요청 접수 API, `agent_worker`는 DB queue 처리와 app callback을 담당합니다. 상태와 로그는 아래처럼 확인합니다.

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

- `docs/ARCHITECTURE.md`: 시스템/에이전트/PHR 분리 구조
- `docs/AGENT_SYSTEM_MAP.md`: agent, tool, 정책 confirmation 흐름 그래프
- `docs/AGENT_APP_FEATURES.md`: `agent_app` 기능, 내부 agent, 출력 계약, 시나리오 테스트 정리
- `docs/LLM_PLAYWRIGHT_TEST_PLAN.md`: 실제 LLM 기반 Playwright 회귀 시나리오 계획
- `docs/PATCH_NOTES_CHRONOLOGICAL_2026-06-01.md`: 주요 업데이트 시간순 패치노트
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
