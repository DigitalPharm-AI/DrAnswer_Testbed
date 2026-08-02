# EC2 v1.3 Contract Test Server

이 배포는 기존 전체 Agent 서버와 분리된 wire-contract 테스트 서버다.
실제 LLM, 환자 조회, Backend 동기 쓰기 API는 실행하지 않는다.

## 현재 범위

- `POST /agent/sync/chat`
- `POST /agent/async/chat_feedback`
- `POST /agent/async/missed-dose-events`
- `POST /agent/async/daily-medication-pattern-analysis`
- AI→Backend callback outbox
  - `/api/agent/async/missed-dose-results`
  - `/api/agent/async/notification-policy-change-proposals`

기본 `CALLBACK_MODE=hold`에서는 Callback payload가 전용 PostgreSQL
데이터베이스에 영속 저장되고 HTTP 전송 시도 횟수는 0으로 유지된다.
Backend 주소를 나중에 추가해도 기존 held Callback은 자동 전송되지 않는다.

`POST /agent/sync/chat`은 `application/x-ndjson`을 실제
`StreamingResponse`로 전송한다. 신규 요청은 최종 텍스트를
`CONTRACT_STREAM_DELTA_CHUNKS`개(기본 4개)의 손실 없는 delta로 나눠
각각 별도 `streaming` 이벤트로 전송하고 마지막에 `completed` 이벤트를
보낸다. 모든 이벤트 사이는 `CONTRACT_STREAM_CHUNK_DELAY_MS`(기본
150ms)만큼 떨어지며, 네트워크에서 순차 도착을 관찰할 수 있도록
50~5000ms 범위로 제한한다. 멱등성 replay는 기존 규격대로 저장된
terminal 이벤트 한 청크만 반환한다.

## EC2 격리 경로

- 코드: `/opt/dranswer-agent-contract/releases/<release-id>`
- 현재 버전: `/opt/dranswer-agent-contract/current`
- 환경: `/etc/dranswer-agent-contract/contract.env`
- root 전용 마이그레이션 환경:
  `/etc/dranswer-agent-contract/contract-migration.env`
- 버전 환경: `/etc/dranswer-agent-contract/release.env`
- DB: 전용 PostgreSQL `dranswer_contract`
- API: `dranswer-agent-contract-api.service`
- Callback worker: `dranswer-agent-contract-callback.service`
- 포트: `8701`

## 패키지 생성

저장소 루트에서:

```powershell
powershell -ExecutionPolicy Bypass -File deploy/ec2-contract-server/prepare_bundle.ps1
```

생성된 `.tar.gz`, 같은 이름의 `.sha256`, `install_contract_release.sh`를
함께 업로드한다. 번들에는 실제 환경 파일, 토큰, SSH key가 포함되지 않는다.

## 설치 및 활성화

먼저 PostgreSQL에 runtime role과 migration role을 분리한다. 아래는
로컬 PostgreSQL 예시이며 실제 비밀번호는 별도 secret으로 생성한다.

```sql
CREATE ROLE contract_migration LOGIN PASSWORD '<migration-password>';
CREATE ROLE contract_app_rw LOGIN PASSWORD '<runtime-password>';
CREATE DATABASE dranswer_contract OWNER contract_migration;
\connect dranswer_contract
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO contract_app_rw;
ALTER DEFAULT PRIVILEGES FOR ROLE contract_migration IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO contract_app_rw;
ALTER DEFAULT PRIVILEGES FOR ROLE contract_migration IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO contract_app_rw;
```

```bash
bash /home/ec2-user/install_contract_release.sh \
  /home/ec2-user/dranswer-agent-contract-<release>.tar.gz
sudoedit /etc/dranswer-agent-contract/contract.env
sudoedit /etc/dranswer-agent-contract/contract-migration.env
bash /opt/dranswer-agent-contract/candidate/deploy/ec2-contract-server/activate.sh
```

최초 설치 시 두 개의 서로 다른 64자리 토큰이 생성되며 화면에는 출력되지
않는다. 피드백 digest용 별도 비밀키도 함께 생성된다. 설치 단계는
`candidate`만 만들며 실행 중인 `current`를 바꾸지 않는다. 활성화가 실패하면
이전 release와 서비스 상태로 rollback한다. Backend 연동 담당자에게 전달할
때만 root 권한으로 필요한 값을 조회한다.

`contract.env`에는 최소 권한 runtime URL인 `CONTRACT_DATABASE_URL`만 두고,
DDL 권한이 있는 `CONTRACT_MIGRATION_DATABASE_URL`은 root만 읽을 수 있는
`contract-migration.env`에 둔다. 활성화 과정은 서비스를 시작하기 전에
`python -m contract_test_server.migrate`를 명시적으로 실행한다. API와
callback worker는 시작 시 스키마를 만들거나 변경하지 않는다.

```bash
sudo grep '^AGENT_SYNC_API_TOKEN=' \
  /etc/dranswer-agent-contract/contract.env
```

상태 확인:

```bash
curl -fsS http://127.0.0.1:8701/health
curl -fsS http://127.0.0.1:8701/health/ready
sudo systemctl status dranswer-agent-contract-api.service
sudo journalctl -u dranswer-agent-contract-api.service -n 100 --no-pager
sudo journalctl -u dranswer-agent-contract-api.service -f
```

API unit은 Uvicorn access log를 활성화한다. 정상 호출마다 client IP,
HTTP method, path, status code가 systemd journal에 기록되며 Bearer token과
요청/응답 Body는 기록하지 않는다.

규격 원본:

- `/contracts/v1.3/chat.json`
- `/contracts/v1.3/async-medication.json`
- `/contracts/v1.3/backend-callbacks.json`

## 기존 SQLite 상태를 보존해 전환할 때

`contract_test_server.migrate`는 PostgreSQL 스키마만 생성하며 SQLite 행을
자동으로 복사하지 않는다. 기존 멱등 기록과 callback outbox를 보존해야
하면 다음 안전 경계를 적용한다.

1. `provision_postgres.sh`는 **fresh PostgreSQL cluster**에 서로 다른
   migration/runtime role과 전용 DB를 만든다. 기존 role이나 DB가 있으면
   재사용하지 않고 중단한다.
2. callback worker와 API를 모두 중지한 뒤
   `migrate_sqlite_state.py snapshot`으로 최종 SQLite snapshot을 만든다.
3. migration role로 `python -m contract_test_server.migrate`를 실행하고,
   runtime role에는 `contract_schema_version`의 `SELECT`와
   `request_records`, `callback_jobs`의 `SELECT, INSERT, UPDATE`만 부여한다.
4. `migrate_sqlite_state.py import`로 단일 PostgreSQL transaction 안에서
   데이터를 복사한다. source/target count, callback 상태와 비식별 digest가
   모두 같지 않으면 commit하지 않는다.
5. `migrate_sqlite_state.py verify-roles`로 runtime DML rollback canary,
   migration/runtime role 분리와 runtime `CREATE TABLE` 거부를 확인한다.
6. `activate_postgres_cutover.sh <release-id> <backup-directory>`로 candidate를
   우선 loopback에만 기동한다. 규격 회귀가 통과한 뒤에만 `0.0.0.0:8701`로
   공개한다.

PostgreSQL에 신규 request나 callback이 하나라도 기록된 뒤에는 SQLite로
역전환하지 않는다. 이 시점 이후 rollback은 PostgreSQL backup/PITR 또는
PostgreSQL 호환 application forward-fix로 수행한다. 레거시 SQLite 파일은
삭제 대신 root read-only 증적으로 보존하고 활성 환경에서는
`CONTRACT_DB_PATH`를 제거한다.

## Backend 주소가 정해진 뒤

`/etc/dranswer-agent-contract/contract.env`에서 다음 값을 변경한다.

```dotenv
CALLBACK_MODE=deliver
BACKEND_CALLBACK_BASE_URL=https://backend.example
# 수신 인증과 Callback 모두 기존 AGENT_SYNC_API_TOKEN을 사용한다.
```

HTTP 테스트 주소가 꼭 필요할 때만
`ALLOW_INSECURE_BACKEND_HTTP=true`를 사용한다. 설정 후 API와 worker를
활성화한다.

```bash
bash /opt/dranswer-agent-contract/current/deploy/ec2-contract-server/activate.sh
```

과거 held Callback은 여전히 자동 발송되지 않는다. 별도
`TEST_CONTROL_TOKEN`으로 `GET /_test/callbacks`에서 대상을 확인하고,
선택한 `callback_request_id`만
`POST /_test/callbacks/{callback_request_id}/release`로 해제한다.

Callback URL은 환경 파일의 고정 origin과 코드의 allowlist 경로로만
조합된다. 요청 본문에서 URL을 지정할 수 없고 redirect를 따라가지 않는다.

## 네트워크 안전 경계

현재 API는 EC2 `8701`의 plain HTTP 테스트 endpoint다. 합성 ID와 합성
메시지만 사용하고 Security Group의 `8701/tcp` source를 Backend 테스트
서버 IP/CIDR 또는 Security Group으로 제한해야 한다. 실제 환자 데이터나
실제 메시지를 보내기 전에는 ALB/Nginx/Caddy 등에서 TLS를 종료하고 HTTPS만
허용해야 한다. Callback outbound는 기본적으로 HTTPS origin만 허용한다.
