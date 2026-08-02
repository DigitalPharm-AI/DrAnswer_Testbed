# DA_drug 전체 보안 점검 보고서

- 점검일: 2026-08-01
- 범위: `system_app` Backend/BFF, `frontend` React UI, `agent_app` AI Server, 공통 계약·DB 경계, Docker/EC2 배포 설정, CI 의존성 관리
- 방식: 소스 정적 검토, 현재 로컬 서비스의 비인증 접근·응답 헤더 확인, Python/Node 의존성 취약점 조회
- 제한: AWS Security Group/IAM/RDS 암호화/TLS 종료 장비의 실제 설정, 운영 계정 권한, 외부 침투 테스트와 퍼징은 이번 점검 범위에 포함되지 않았다. 비밀값 자체는 열람하거나 보고서에 기록하지 않았다.

## 1. 요약

현재 코드에서 하드코딩된 운영 비밀정보, 알려진 Python/Node 의존성 취약점, 명백한 SQL 문자열 조립, React DOM XSS sink는 발견되지 않았다. AI Server의 Backend DB 조회도 읽기 전용 트랜잭션·뷰·권한 검증으로 방어되어 있고, 쓰기 Tool은 환자 식별자 주입·승인 키·인자 스키마·멱등성 검증을 사용한다.

반면 지금의 `system_app` UI API는 인증 없이 환자 상태를 읽고 변경하며 AI 채팅을 호출할 수 있다. 또한 요청 빈도·동시성·실제 비용 예산을 차단하는 실행 시점 보호가 없다. 서비스 간 HTTPS와 PostgreSQL TLS는 이번 수정으로 production에서 fail-closed가 적용되었다. 따라서 **현재처럼 단일 사용자 로컬 테스트베드로만 사용하면 위험이 제한적이지만, 남은 High 항목을 해결하기 전에는 외부 네트워크나 실제 환자 환경에 그대로 배포해서는 안 된다.**

| 심각도 | 건수 | 핵심 내용 |
|---|---:|---|
| Critical | 0 | 즉시 악용 가능한 운영 비밀 유출이나 원격 코드 실행은 확인되지 않음 |
| High | 2 | UI API 무인증, LLM 비용/자원 고갈 보호 부재 |
| Medium | 5 | 상세 상태·문서 노출, 브라우저 보안 헤더 부재, 약한 토큰 허용, 일부 임시 데이터 평문 저장, 배포 스크립트 argv 비밀 노출 |
| Low | 2 | root 컨테이너·비재현 Python 설치, CI 취약점 감사 부재 |

### 우선 조치 순서

1. 외부 노출 전에 UI API 전체에 사용자 인증·환자 권한 검사를 추가하고 테스트베드 제어 API를 개발 환경으로 제한한다.
2. UI 채팅과 AI 경계에 환자/사용자/IP 단위 rate limit, 동시 실행 제한, 요청 본문 상한, 실제 토큰·비용 예산 차단을 적용한다.
3. 배포 환경에서 TLS 종료 장비·인증서·보안 그룹이 코드의 production HTTPS/TLS 불변조건과 일치하는지 확인한다.
4. 상세 상태·OpenAPI 문서를 보호하고 CSP·clickjacking·Host 검증을 추가한다.
5. 운영 데이터 암호화와 배포 공급망을 강화한다.

## 2. High

### SEC-001 — Backend/UI API가 인증 없이 환자 데이터 조회와 상태 변경을 허용함

**근거**

- [`system_app/routes/ui_api.py:74`](system_app/routes/ui_api.py#L74)의 UI 라우터에는 인증 dependency가 없다.
- 동일 라우터에서 복약 시나리오 적용(`:120`), 전체 초기화(`:208`), 시계 진행(`:287`), 재생·정지(`:348`, `:369`), 복약 상태 기록(`:389`)을 수행한다.
- [`system_app/routes/ui_patient_routes.py:50`](system_app/routes/ui_patient_routes.py#L50)도 인증 없이 영양·정책·알림·대화 이력을 조회하고, 정책 변경(`:86`), 알림 확인(`:164`), AI 동기/SSE 채팅(`:202`, `:213`)을 허용한다.
- [`system_app/routes/ui_feedback.py:60`](system_app/routes/ui_feedback.py#L60)의 피드백 API도 동일한 공개 경계에 있다.
- [`system_app/main.py:354`](system_app/main.py#L354)에서 이 라우터들이 별도 보호 없이 등록된다.
- [`docker-compose.yml:153`](docker-compose.yml#L153)은 `system-app` 포트를 호스트 전체 인터페이스에 게시할 수 있는 기본 매핑을 사용한다.
- 로컬 실행 서비스에서도 `/api/ui/v1/dashboard`가 인증 없이 HTTP 200을 반환하는 것을 확인했다.

**영향**

접근 가능한 네트워크의 사용자가 환자 복약·식사·알림·대화 데이터를 조회하거나 변경하고, 기록·정책·시뮬레이션 상태를 조작하며 AI 호출 비용을 발생시킬 수 있다. 현재 고정 `patient_id` 구조에서는 한 사용자의 데이터가 그대로 노출된다.

**권고 수정**

- UI API에 로그인 세션 또는 검증된 OIDC/JWT를 적용하고, 매 요청에서 인증 주체와 `patient_id` 권한을 서버가 매핑한다.
- CSRF 방어가 포함된 `HttpOnly`, `Secure`, `SameSite` 세션 쿠키를 사용하거나, SPA Bearer 방식이면 토큰을 브라우저 저장소에 장기 보관하지 않는다.
- `scenario`, `reset`, `clock`, `play/pause` 같은 테스트베드 제어 API는 `APP_ENV`가 개발/테스트일 때만 라우팅하거나 별도의 관리자 권한으로 분리한다.
- 단순히 난수 public ID를 아는지 여부를 권한 검사로 사용하지 않는다.

**임시 완화책**

수정 전에는 `system-app`을 `127.0.0.1`에만 바인딩하고, Docker 포트도 `127.0.0.1:${SYSTEM_PORT}:8000`으로 제한하며 방화벽에서 외부 접근을 차단한다.

**오탐 가능성/조건**

완전히 격리된 단일 사용자 로컬 테스트베드라면 의도된 설계일 수 있다. 그러나 Docker 기본 포트 게시 또는 프록시를 통해 다른 호스트에서 접근 가능해지는 순간 High 위험이다.

### SEC-002 — AI 채팅의 빈도·동시성·본문 크기·실제 비용을 실행 시점에 제한하지 않음

**근거**

- 인증 없는 UI 채팅 진입점은 [`system_app/routes/ui_patient_routes.py:202`](system_app/routes/ui_patient_routes.py#L202)와 `:213`이다.
- UI 메시지는 [`system_app/ui_contracts.py:99`](system_app/ui_contracts.py#L99)에서 2,000자로 제한하지만, HTTP 본문 자체의 byte 상한은 애플리케이션/프록시에서 강제하지 않는다.
- Backend→AI 계약의 [`shared/chat_contracts.py:36`](shared/chat_contracts.py#L36)은 `message`에 `min_length=1`만 있고 최대 길이가 없다.
- [`system_app/main.py:322`](system_app/main.py#L322)와 [`agent_app/main.py:147`](agent_app/main.py#L147)에는 rate limit, 요청 크기 제한, 전역/환자별 동시 실행 제한 middleware가 없다.
- [`shared/settings.py:164`](shared/settings.py#L164)의 일일/평가 비용 예산은 [`system_app/services/health.py:65`](system_app/services/health.py#L65)에서 상태 정보로만 표시된다. `shared/readiness_budget.py`의 비용 계산도 실행 요청을 거부하는 경로에 연결되지 않았다.
- Tool 한 실행의 호출 수 예산은 존재하지만, 모델 요청 횟수나 사용자 요청 빈도를 제한하지는 않는다.

**영향**

반복 요청이나 큰 본문, 느린 SSE 연결로 Backend/AI worker/DB 연결을 고갈시키고 Bedrock 비용을 증가시킬 수 있다. 무인증 UI와 결합하면 외부 비용 유발 공격이 가능하다.

**권고 수정**

- 신뢰 경계별로 제한을 둔다: reverse proxy의 body/connection/time limit, Backend의 사용자·환자·IP 단위 rate limit, AI Server의 환자별 동시 실행 1개 및 전체 semaphore/queue 한도.
- `ChatSyncRequest.message`에도 명시적인 문자·UTF-8 byte 상한을 적용하고, Content-Length가 없거나 초과하면 파싱 전에 거부한다.
- 하루 토큰/비용을 Agent DB 단일 원본으로 누적하고, soft warning과 별도로 hard budget 초과 시 새 생성 요청을 429/503으로 차단한다.
- 재시도는 동일 `request_id`의 저장 결과 재사용으로 제한하고, 새 `request_id`를 만드는 자동 반복에 별도 한도를 둔다.

**임시 완화책**

API Gateway/ALB/Nginx 등에서 요청 크기, 요청률, 동시 연결, SSE idle timeout을 먼저 제한하고 Bedrock 계정 예산 경보를 설정한다.

**오탐 가능성/조건**

완전한 loopback 환경에서는 공격 가능성이 낮지만, UI 버그·자동화 루프·Backend 탈취만으로도 비용/자원 고갈은 발생할 수 있다.

## 3. 이번 수정으로 해결된 항목

### SEC-003 — production 서비스 간 HTTPS와 PostgreSQL TLS 강제

**조치 상태: 코드 완료, 인프라 TLS 종료 확인 필요**

**반영 내용**

- [`shared/settings.py`](shared/settings.py)는 production의 `SYSTEM_BASE_URL`과 `AGENT_BASE_URL`에 HTTPS를 강제한다.
- Backend→AI와 AI→Backend의 일반 업무 API는 하나의 `AGENT_SYNC_API_TOKEN`을 양방향 Bearer 토큰으로 사용한다. 더 넓은 운영 권한을 가진 `INTERNAL_API_TOKEN`은 서비스 토큰과 분리하고, 두 값이 같으면 시작을 거부한다.
- Agent·Backend runtime DB, read-only DB와 migration DB는 production에서 `sslmode=require`, `verify-ca`, `verify-full` 중 하나가 없으면 시작하지 않는다.
- Agent API·worker, Backend API와 양쪽 migration CLI가 각자의 transport/DB 검증을 startup 전에 호출한다.
- [`deploy/ec2-agent/validate_env.py`](deploy/ec2-agent/validate_env.py)는 EC2 환경의 Backend URL이 HTTPS인지, 세 PostgreSQL URL에 TLS 모드가 있는지 사전 검사한다.
- 개발·테스트베드의 localhost/Docker HTTP는 그대로 허용하여 현재 로컬 실행을 깨뜨리지 않는다.

**남은 운영 게이트**

- EC2 Agent unit의 8701 listener는 TLS 종료 장비 뒤의 내부 origin으로 남아 있다. 실제 배포 시 ALB/NLB+TLS, reverse proxy 또는 mTLS service mesh를 배치하고 8701 직접 접근을 Backend/프록시 보안 그룹으로 제한해야 한다.
- HTTPS 인증서 검증을 우회하지 않고, 가능하면 PostgreSQL은 `sslmode=verify-full`과 사설 CA를 사용한다.
- `X-Forwarded-*`를 사용하는 경우 신뢰한 프록시 IP만 허용한다.

## 4. Medium

### SEC-004 — 상세 health와 API 문서가 공개되어 내부 구성을 노출함

**근거**

- [`system_app/routes/health.py:17`](system_app/routes/health.py#L17)의 `/health/details`에는 인증이 없다.
- [`system_app/services/health.py:13`](system_app/services/health.py#L13)은 DB 상태·버전, Agent URL/worker 상태, 비용 설정, workbook 절대 경로, migration 정보를 반환한다.
- [`agent_app/main.py:317`](agent_app/main.py#L317)의 `/health/ready`도 component별 상태를 공개한다.
- 두 FastAPI 앱은 기본 docs/OpenAPI를 비활성화하지 않았다([`system_app/main.py:144`](system_app/main.py#L144), [`agent_app/main.py:147`](agent_app/main.py#L147)). 로컬 서비스에서 `/docs`와 `/openapi.json`이 인증 없이 HTTP 200임을 확인했다.

**영향**

공격자가 서비스 구조, 내부 경로, DB/worker 상태와 전체 API 표면을 빠르게 열거해 후속 공격을 정교화할 수 있다. 직접적인 데이터 유출보다 공격 난이도를 낮추는 정보 노출이다.

**권고 수정**

- 외부 health는 `{status: "ok"}` 수준으로 제한한다.
- `/health/details`, `/health/ready`, docs/OpenAPI는 내부 운영 인증 또는 관리자 네트워크에서만 허용한다.
- production에서 `docs_url=None`, `redoc_url=None`, 필요하면 `openapi_url=None`을 설정한다.

### SEC-005 — 브라우저 보안 헤더와 Host 검증이 없음

**근거**

- [`system_app/main.py:322`](system_app/main.py#L322)의 유일한 공통 응답 middleware는 cache 방지 헤더만 추가한다.
- 코드에서 `TrustedHostMiddleware`, CSP, `frame-ancestors`/`X-Frame-Options`, `X-Content-Type-Options`, `Referrer-Policy`, `Permissions-Policy` 설정을 찾지 못했다.
- 로컬 응답에서도 위 보안 헤더가 없음을 확인했다.
- 긍정적으로 CORS wildcard 설정은 없고, React는 same-origin `/api/ui/v1`만 호출한다.

**영향**

UI가 iframe에 삽입되어 사용자의 복약 기록·정책 변경을 클릭하도록 유도될 수 있고, 향후 렌더링 결함이 생겼을 때 CSP 방어층이 없다. Host 헤더 기반 오동작 방어도 없다.

**권고 수정**

- 최소 `Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'`를 실제 빌드에 맞춰 조정한다.
- `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, 제한적인 `Permissions-Policy`와 Trusted Host 목록을 설정한다.
- HSTS는 HTTPS 종료 지점에서만 추가하고, TLS가 보장되기 전 HTTP 응답에 성급히 넣지 않는다.

### SEC-006 — 런타임은 서비스 토큰의 존재만 확인하여 약한 토큰을 허용함

**근거**

- [`shared/settings.py:261`](shared/settings.py#L261), `:267`, `:276`은 공백이 아닌지만 확인하고 토큰 길이·엔트로피·placeholder를 검사하지 않는다.
- EC2 전용 validator는 [`deploy/ec2-agent/validate_env.py:144`](deploy/ec2-agent/validate_env.py#L144)에서 32자 이상과 토큰 분리를 검사하므로 EC2 표준 배포는 더 안전하다.
- Docker/direct uvicorn 등 validator를 거치지 않는 실행 경로에서는 한 글자 토큰도 startup을 통과할 수 있다.

**영향**

운영자가 약한 값을 설정하면 추측 공격으로 Agent 동기 API, Backend write API 또는 내부 운영 API 권한을 획득할 수 있다.

**권고 수정**

- `Settings`의 production validation에서 최소 32바이트의 CSPRNG 토큰과 placeholder 거부를 강제한다. Backend↔AI 공통 서비스 토큰과 내부 운영 토큰은 서로 다르게 유지한다.
- 토큰은 `SecretStr`로 보유하고 키 ID/이전 키를 이용한 무중단 rotation 절차를 문서화한다.

### SEC-007 — Trace는 암호화되지만 일부 Agent 운영 상태에 환자 데이터가 평문으로 저장됨

**근거**

- [`agent_app/persistence/models.py:30`](agent_app/persistence/models.py#L30)의 비동기 작업은 `payload_json`, callback context, 오류를 평문 `Text`로 최대 30일 저장한다(`:49-63`).
- [`agent_app/persistence/models.py:82`](agent_app/persistence/models.py#L82)의 동기 요청은 `patient_id`, `message_id`, `response_json`을 평문으로 저장한다(`:92-105`).
- [`agent_app/persistence/models.py:108`](agent_app/persistence/models.py#L108)의 환자 lock도 원문 `patient_id`를 보유한다.
- 반면 run trace의 임상 evidence, feedback, survey/selection state에는 AES-GCM 암호화와 patient hash가 적용되어 있어 이 문제는 암호화 체계 전체가 아니라 운영/idempotency 테이블의 격차다.

**영향**

Agent DB dump, 과도한 DBA 권한, 백업 또는 스토리지 유출 시 짧은 보존 기간이라도 환자 식별자와 응답 내용이 노출될 수 있다.

**권고 수정**

- RDS/storage/backup 암호화를 필수화하고 DB 역할·감사 정책을 최소 권한으로 운영한다.
- idempotency에 필요 없는 원문 payload/응답은 hash 또는 최소 필드만 저장한다.
- 재응답 저장이 필요하면 기존 evidence encryption과 동일한 envelope 암호화를 적용하고 환자 lock key는 keyed hash로 바꾼다.
- 현재 1일/30일 retention cleanup이 실제 worker/운영 job에서 주기적으로 실행되는지 모니터링한다.

**오탐 가능성/조건**

암호화된 디스크, 엄격한 DB IAM/role과 백업 정책이 실제로 적용되어 있으면 잔여 위험은 낮아진다. 이번 점검에서는 해당 인프라 상태를 확인하지 못했다.

### SEC-008 — Contract Server 설치 시 생성한 서비스 토큰이 잠시 프로세스 argv에 노출됨

**근거**

- [`deploy/ec2-contract-server/install_release.sh:85`](deploy/ec2-contract-server/install_release.sh#L85)는 서비스 토큰을 생성한 뒤 `:95-103`의 `sed` 명령 문자열에 직접 삽입한다. 실행 중 동일 호스트의 프로세스 관찰 권한이 있으면 argv에서 읽을 가능성이 있다.
- 반대로 Bedrock 설치 스크립트는 [`deploy/ec2-agent/install_bedrock_token.sh:64`](deploy/ec2-agent/install_bedrock_token.sh#L64)처럼 stdin과 mode 0600 파일만 사용하므로 안전한 구현 예가 이미 있다.

**영향**

공유 호스트 또는 프로세스 가시성이 넓은 환경에서 contract/control 토큰이 노출되어 테스트 경계를 조작할 수 있다.

**권고 수정**

- 토큰을 command argv에 넣지 말고 root-only 임시 파일/stdin을 통해 환경 파일을 원자적으로 작성한다.
- 설치가 끝난 뒤 shell 변수 해제, umask 077, 환경 파일 mode/owner 재검증을 수행한다.

**오탐 가능성/조건**

단독 root 관리 호스트이며 `/proc`가 제한되어 있으면 악용 가능성은 낮다. 주 시스템보다 contract 테스트 서버 배포 경로에 한정된 문제다.

## 5. Low

### SEC-009 — Docker 런타임이 root이고 Python 의존성 설치가 비재현적임

**근거**

- [`Dockerfile:11`](Dockerfile#L11) 이후 별도 비root `USER`를 만들지 않아 앱이 root로 실행된다.
- [`Dockerfile:19`](Dockerfile#L19)은 광범위한 `>=` 버전의 [`requirements.txt:1`](requirements.txt#L1)을 설치한다.
- EC2 배포에는 pinned `requirements.agent.lock`이 있고 Frontend는 `package-lock.json`+`npm ci`를 사용하므로 일부 경로는 이미 재현 가능하다.

**영향**

애플리케이션 취약점 악용 시 컨테이너 내부 권한이 불필요하게 크고, 빌드 시점에 따라 검증하지 않은 Python 버전이 설치될 수 있다.

**권고 수정**

- 전용 UID/GID를 만들고 필요한 runtime 경로만 소유시킨 뒤 `USER`로 전환한다.
- 검증된 lock을 Docker에도 사용하고 가능하면 hash 검증(`--require-hashes`)을 적용한다.
- Compose/Kubernetes에서 read-only rootfs, `no-new-privileges`, capability drop과 제한된 tmpfs를 적용한다.

### SEC-010 — CI에 의존성 보안 감사가 없음

**근거**

- [`.github/workflows/testbed-boundary-contract.yml:52`](.github/workflows/testbed-boundary-contract.yml#L52)는 Python 설치, `:82`는 `npm ci`를 수행하지만 `pip-audit`/OSV/`npm audit` 또는 lock 업데이트 검증 단계가 없다.
- 이번 점검 시 공식 advisory 기준으로 Frontend `npm audit`은 215개 의존성에서 0건, 현재 Python 환경 `pip-audit`도 알려진 취약점 0건이었다.

**영향**

현재 취약점은 없지만 이후 새 advisory가 공개되어도 자동으로 배포를 막지 못한다.

**권고 수정**

- CI에 `pip-audit`과 `npm audit --omit=dev` 또는 Dependabot/OSV Scanner를 추가한다.
- 결과를 SBOM과 release manifest에 연결하고, High/Critical만 차단하는 초기 정책으로 시작해 예외 만료일을 관리한다.

## 6. 확인된 양호한 보안 통제

- `system_app/security.py`와 `agent_app/security.py`는 토큰을 `hmac.compare_digest`로 비교한다.
- Backend→AI 채팅과 AI→Backend 쓰기는 하나의 `AGENT_SYNC_API_TOKEN`을 공유하고, 권한 범위가 더 넓은 `INTERNAL_API_TOKEN`은 별도로 강제한다.
- 내부 HTTP 클라이언트는 고정 base URL을 사용하고 `httpx.AsyncClient(trust_env=False)`로 환경 프록시를 신뢰하지 않는다.
- Agent의 Backend DB reader는 PostgreSQL read-only transaction, 허용 view, schema/role privilege를 startup/readiness에서 검증하고 고정 SQL+bind parameter를 사용한다.
- Tool 스키마는 `additionalProperties: false`이며, 모델이 만든 `patient_id` 등 기술 인자를 거부하고 서버가 환자·메시지·멱등성 값을 주입한다.
- 쓰기 Tool은 승인 action/args와 approval key를 결합하고, PRO-CTCAE 등 선행 상태와 중복 실행을 검증한다.
- Trace/feedback/설문 상태는 AES-GCM과 random nonce를 사용하고, 일반 로그는 임상 텍스트·환자 ID·비밀 marker를 redaction한다. private reasoning 원문은 Trace에 저장하지 않는다.
- React assistant 메시지는 `react-markdown`의 `skipHtml`을 사용하고 raw HTML plugin이나 `dangerouslySetInnerHTML`을 사용하지 않는다.
- Frontend API는 same-origin 고정 경로이고 SSE frame 크기·sequence·request ID를 검증한다. wildcard CORS도 없다.
- `.gitignore`와 `.dockerignore`는 실제 `.env`를 제외하고 example만 허용한다. 추적 파일에서 AWS access key나 private key 서명은 발견되지 않았다.
- 현재 Node/Python 의존성 advisory 조회 결과는 0건이다.

## 7. 배포 전 최소 보안 게이트

- [ ] UI 인증과 환자별 authorization 테스트
- [ ] 테스트베드 reset/clock/scenario API의 production 비활성화 또는 관리자 권한
- [ ] Backend/Agent rate limit·동시성·본문 byte 상한·Bedrock hard cost budget
- [ ] HTTPS/mTLS 및 PostgreSQL `verify-full` 또는 동등한 인증서 검증
- [ ] 최소 공개 health, 상세 health/docs 내부화
- [ ] CSP·frame-ancestors·nosniff·Referrer-Policy·TrustedHost
- [ ] 32바이트 이상 서비스 토큰과 rotation 리허설
- [ ] RDS/backup 암호화와 Agent 운영 테이블의 최소화/필드 암호화
- [ ] non-root container와 pinned/hash-verified Python lock
- [ ] CI dependency audit와 보안 회귀 테스트
