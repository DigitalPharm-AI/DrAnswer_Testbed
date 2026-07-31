# AI Agent v1.3 EC2 PostgreSQL 전환 결과

## 1. 결론

**완료 — EC2 Contract Test Server를 SQLite에서 PostgreSQL 16으로 무손실 전환했다.**

- 현재 릴리스: `20260730T002607Z`
- 공개 endpoint: `http://13.124.53.57:8701`
- 실제 DB: PostgreSQL `16.14`
- DB listener: `127.0.0.1:5432` 전용
- SQLite 15 request / 6 callback을 PostgreSQL로 import했고 count·상태·digest parity가 모두 일치했다.
- v1.3 규격 회귀 50/50, 재시작 보존 6/6, SQLite 경로 제거 후 재시작 보존 재검증 6/6이 통과했다.
- 최종 DB는 24 request / 9 callback이며 callback은 모두 `held`, 전체 delivery attempts 합계는 0이다.
- 기존 `chat-server.service`와 포트 8766은 PID·Invocation ID·restart count가 전환 전후 동일하다.

Backend 주소와 인증정보는 여전히 미설정이며 callback worker도
`inactive/disabled`로 유지했다.

## 2. 최종 구성

| 항목 | 최종 상태 |
|---|---|
| EC2 | `13.124.53.57`, Amazon Linux 2023 |
| Contract API | `0.0.0.0:8701`, active/enabled |
| 릴리스 | `20260730T002607Z` |
| 릴리스 archive SHA-256 | `fdcd63caddae399ac027a0ed2f07f1a2be6bff4c944f594915f5103e784c5f35` |
| PostgreSQL | `16.14`, active/enabled |
| PostgreSQL listener | `127.0.0.1:5432` |
| Schema | version `1` |
| Runtime role | `contract_app_rw` |
| Migration role/DB owner | `contract_migration` |
| Callback mode | `hold` |
| Callback worker | inactive/disabled |
| 기존 8766 서비스 | active/enabled, 영향 없음 |

PostgreSQL runtime URL은 app service group만 읽는 환경 파일에, DDL 권한이
있는 migration URL은 root 전용 환경 파일에 분리했다. 두 role은 서로 다른
64자리 hex 비밀번호를 사용하며 비밀번호와 API token은 실행 로그나 증거
파일에 출력하지 않았다.

## 3. 상태 보존 전환 결과

### SQLite 최종 snapshot

API와 callback worker를 중지한 뒤 최종 snapshot을 만들었다.

| 항목 | 값 |
|---|---:|
| Request records | 15 |
| Callback jobs | 6 |
| Callback 상태 | `held=6` |
| Snapshot SHA-256 | `623d687fe83976ce76b791fdb8aba8f8b6600f17ef81d7e90b639ebab61e8733` |
| Request digest | `18b1378c29e07fb60878d2876c201ccb6cf61bc56d35109fa74c05906d54e4e8` |
| Callback digest | `2623e63a8ccd28f14efb3e832cf4f6ce3bf12f78d89ff5bc029909bd5a2c486a` |

실행 중 만든 사전 snapshot과 API 중지 후 만든 최종 snapshot의 hash, count,
상태 및 digest가 같아 snapshot 사이에 추가 쓰기가 없었음도 확인했다.

### PostgreSQL import

Fresh DB에 schema version 1을 만든 뒤 하나의 PostgreSQL transaction으로
모든 행을 import했다.

- source/target request count 일치
- source/target callback count 및 상태 일치
- request digest 일치
- callback payload hash와 전체 callback digest 일치
- parity 검증 실패 시 transaction을 commit하지 않도록 구성
- `in_progress` callback이 있으면 cutover를 거부하도록 구성

Import 직후 PostgreSQL baseline도 15 request / 6 callback(`held=6`)이었다.

### Role 및 권한 gate

- migration/runtime role이 서로 다르고 같은 `dranswer_contract` DB를 사용한다.
- runtime role은 schema `USAGE`와 필요한 table `SELECT/INSERT/UPDATE`를 갖는다.
- runtime DML canary는 transaction rollback으로 검증해 테스트 행을 남기지 않았다.
- runtime `CREATE TABLE`은 PostgreSQL SQLSTATE `42501`로 거부됨을 확인했다.
- 두 role 모두 superuser, CREATEDB, CREATEROLE 속성이 없다.
- host 인증은 두 role과 해당 DB에 대해 `scram-sha-256`을 사용한다.

## 4. 규격 회귀 및 보존 테스트

| 영역 | 건수 | 통과 | 실패 |
|---|---:|---:|---:|
| Health, 규격 artifact, OpenAPI | 4 | 4 | 0 |
| 인증 및 무단 요청 비저장 | 6 | 6 | 0 |
| 동기 Chat 및 NDJSON | 11 | 11 | 0 |
| Chat feedback | 12 | 12 | 0 |
| Endpoint별 멱등키 분리 | 1 | 1 | 0 |
| Missed-dose event | 5 | 5 | 0 |
| Daily medication pattern analysis | 8 | 8 | 0 |
| Callback outbox 및 전송 차단 | 3 | 3 | 0 |
| **초기 회귀 합계** | **50** | **50** | **0** |
| API 재시작 후 멱등·보존 | 6 | 6 | 0 |
| SQLite 경로 제거 후 재시작 재검증 | 6 | 6 | 0 |

초기 회귀로 저장돼야 하는 request 9건과 callback 3건만 증가해 최종
24 request / 9 callback이 됐다. 두 차례 재시작 보존 검증에서는 count가
증가하지 않았다.

작업 PC에서 공개 주소의 `/health`와 `/health/ready`를 다시 호출해 모두
HTTP 200을 확인했다. 최종 readiness는 다음과 같다.

- `release=20260730T002607Z`
- `database_ready=true`
- `callback_mode=hold`
- `callback_delivery_ready=false`
- `errors=[]`

## 5. 서비스 격리와 자원

| 서비스 | 최종 상태 | PID | 비고 |
|---|---|---:|---|
| `postgresql.service` | active/enabled | 276503 | NRestarts 0 |
| `dranswer-agent-contract-api.service` | active/enabled | 278588 | NRestarts 0 |
| `dranswer-agent-contract-callback.service` | inactive/disabled | 0 | 외부 전송 차단 |
| `chat-server.service` | active/enabled | 163828 | 전환 전과 동일 |

`chat-server.service`의 Invocation ID
`0a85d9e85efa493485e068d94d8c8951`과 포트 8766 listener가 유지됐다.
최종 listener는 8701/8766 공개 bind와 PostgreSQL 5432 loopback bind다.

1GB EC2에서 최종 available memory는 약 418MB, swap 사용량은 1MB였다.
전환·회귀 구간의 PostgreSQL/API/callback journal에서 warning 이상 항목은
0건이었다.

## 6. Backup과 rollback 경계

원격 root 전용 backup 경로:

`/var/backups/dranswer-agent-contract/pre-postgres-20260730T001818Z`

이 경로에는 다음이 보존돼 있다.

- SQLite 온라인 snapshot과 API 중지 후 최종 snapshot
- 전환 전 runtime env, migration env, release env
- 전환 전 systemd unit
- SQLite→PostgreSQL import 및 role 검증 결과
- 검증 완료 후 PostgreSQL custom-format dump

PostgreSQL dump:

- 파일: `dranswer_contract_20260730T003200Z.dump`
- SHA-256: `4b105f54cd9c9a8163d7538dd3bcd84be590c9aa3630cee357ffc11f95fde482`
- `pg_restore --list` 검증: PASS

PostgreSQL에 신규 테스트 record가 생성된 뒤에는 SQLite 역전환을 금지했다.
활성 env에서 `CONTRACT_DB_PATH`를 제거했고, 원본 SQLite 파일은 삭제하지
않고 `root:root`, mode `0400`으로 동결했다. 이후 복구는 PostgreSQL dump,
PITR 또는 PostgreSQL 호환 forward-fix를 사용해야 한다.

## 7. 남은 범위와 주의점

- Backend 주소가 없으므로 실제 callback 전송, ACK, retry/timeout은 아직
  검증하지 않았다.
- 현재 9개의 `held` callback은 모두 합성 테스트 데이터다. Backend가
  연결돼도 자동 또는 일괄 release하지 말고 필요한 callback ID만 명시적으로
  선택해야 한다.
- 공개 endpoint는 아직 plain HTTP다. 실제 환자 데이터나 운영 token을
  사용하기 전에 source 제한과 HTTPS 종단이 필요하다.
- PostgreSQL dump catalog는 검증했지만 별도 DB로 실제 restore하는 훈련과
  EC2 reboot 복구 테스트는 이번 범위에 포함하지 않았다.
- 동시성·부하·DB 장애 주입과 장시간 안정성 테스트도 별도 단계다.

## 8. 추가된 전환 도구

- [SQLite snapshot/import/role gate](../deploy/ec2-contract-server/migrate_sqlite_state.py)
- [Fresh PostgreSQL provisioning](../deploy/ec2-contract-server/provision_postgres.sh)
- [상태 보존 cutover activation](../deploy/ec2-contract-server/activate_postgres_cutover.sh)

## 9. 증거 파일

- [SQLite 최종 snapshot](test-results/v13-contract-server-postgres-2026-07-30/contract-pg-sqlite-final-snapshot.json)
- [PostgreSQL import parity](test-results/v13-contract-server-postgres-2026-07-30/contract-pg-import.json)
- [PostgreSQL role 검증](test-results/v13-contract-server-postgres-2026-07-30/contract-pg-role-verification.json)
- [회귀 전 PostgreSQL baseline](test-results/v13-contract-server-postgres-2026-07-30/contract-pg-ops-before.json)
- [초기 규격 50건](test-results/v13-contract-server-postgres-2026-07-30/contract-pg-conformance-initial.json)
- [초기 규격 후 DB 상태](test-results/v13-contract-server-postgres-2026-07-30/contract-pg-ops-after-initial.json)
- [첫 API 재시작 후 6건](test-results/v13-contract-server-postgres-2026-07-30/contract-pg-conformance-post-restart.json)
- [첫 API 재시작 후 DB 상태](test-results/v13-contract-server-postgres-2026-07-30/contract-pg-ops-after-restart.json)
- [SQLite 경로 제거 후 6건](test-results/v13-contract-server-postgres-2026-07-30/contract-pg-conformance-post-cleanup-restart.json)
- [최종 PostgreSQL-only 상태](test-results/v13-contract-server-postgres-2026-07-30/contract-pg-ops-final-post-cleanup.json)
