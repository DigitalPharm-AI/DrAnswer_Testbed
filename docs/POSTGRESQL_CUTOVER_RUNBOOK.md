# PostgreSQL 배포·Cutover·복구 Runbook

이 문서는 PostgreSQL만 사용하는 Backend/System DB, Agent DB와 선택 배포인
Contract Test DB의 staging 및 production 배포 절차다. 이전 저장소
importer나 역이관 절차는 지원하지 않는다.

## 1. 전제

- `SYSTEM_DATABASE_URL`: Backend runtime PostgreSQL role
- `AGENT_DATABASE_URL`: Agent runtime PostgreSQL role
- `BACKEND_READ_DATABASE_URL`: 승인된 `ai_v13_*` View 전용 Read-only role
- `SYSTEM_MIGRATION_DATABASE_URL`: Backend migration role
- `AGENT_MIGRATION_DATABASE_URL`: Agent migration role
- `CONTRACT_DATABASE_URL`: Contract Test Server runtime role
- `CONTRACT_MIGRATION_DATABASE_URL`: Contract Test Server migration role
- API와 worker는 DDL을 실행하지 않고 schema를 검증만 한다.
- Agent DB가 Trace·Step·Tool 실행·token usage·비용 기록의 단일 원장이다.
- Backend DB에는 업무 데이터, callback receipt, Job과 최소 감사 데이터만
  저장한다.

## 2. 사전 승인과 백업

1. 변경 시간, 예상 중단 시간, 담당자와 rollback 승인자를 기록한다.
2. 배포 범위에 포함되는 모든 PostgreSQL DB의 backup과 PITR 상태를
   확인한다.
3. 복구 지점과 backup 식별자를 운영 기록에 남긴다.
4. migration release와 application release가 같은 schema version을
   기대하는지 확인한다.
5. DB URL, 인증서, token과 환자 데이터가 배포 로그에 노출되지 않게 한다.

## 3. 요청 중지와 Queue drain

1. Frontend와 외부 Agent 신규 요청을 maintenance 상태로 전환한다.
2. Backend scheduler의 신규 비동기 접수를 중지한다.
3. Agent worker가 `running`, `callback_sending` 작업을 끝내도록 기다린다.
4. 남은 `pending`, retry와 승인 대기 상태를 집계해 운영 기록에 남긴다.
5. active patient lock이 없고 callback outbox가 안정 상태인지 확인한다.
6. API와 worker를 순서대로 중지한다.

중단 전후의 count와 상태별 집계에는 ID, payload, 자유문 또는 암호문을
출력하지 않는다.

## 4. Migration 실행

Backend migration을 먼저 실행한다.

```powershell
python -m system_app.migrate
```

그 다음 Agent migration을 실행한다.

```powershell
python -m agent_app.migrate
```

Contract Test Server를 배포하는 환경은 마지막으로 전용 migration을
실행한다.

```powershell
python -m contract_test_server.migrate
```

세 명령은 각 전용 migration role을 사용한다. 실패하면 application을 시작하지
말고 PostgreSQL transaction 상태와 migration version을 확인한다. migration을
임의로 건너뛰거나 runtime role에 DDL 권한을 부여하지 않는다.

## 5. 권한과 Schema 검증

1. Backend와 Agent runtime role의 schema version을 확인한다.
2. `ai_backend_reader`가 모든 승인 `ai_v13_*` View를 조회할 수 있는지
   확인한다.
3. 같은 role로 Backend 원본 table SELECT와 모든 INSERT, UPDATE, DELETE,
   CREATE가 거절되는지 확인한다.
4. View column, type, nullable 조건이 `BACKEND_V13_READ_SCHEMA.json`과
   일치하는지 검사한다.
5. migration role을 제외한 role에 schema 변경 권한이 없는지 확인한다.
6. 격리 테스트 스키마의 `search_path`에는 해당 스키마만 넣고 `public`을
   함께 넣지 않는다. 그렇지 않으면 SQLAlchemy의 `checkfirst`가
   `public`의 동명 테이블을 격리 테이블로 오인할 수 있다.

## 6. 서비스 기동 순서

1. Backend/System API
2. Agent API
3. Agent worker
4. Backend scheduler와 비동기 발신
5. React Frontend

각 단계에서 다음 단계로 넘어가기 전에 health와 readiness를 확인한다.

- Backend DB 연결과 schema
- Agent DB 연결과 schema
- Backend Read-only DB 연결과 View 계약
- Backend Write API 연결
- worker heartbeat와 queue claim

## 7. Canary 검증

다음 흐름을 서로 다른 `request_id`로 한 번씩 실행한다.

1. 동기 채팅 NDJSON `streaming → completed`
2. Snapshot과 상세 Tool Read
3. 기록 변경 제안→사용자 승인→Backend 동기 쓰기
4. 알림 정책 변경 제안→사용자 승인→Backend 동기 쓰기
5. 미복용 접수→Agent worker→Backend Callback→후속 대화
6. 일일 패턴 분석→08:30 정책 제안→사용자 승인
7. 채팅 반응과 자유 의견 접수

확인 항목:

- 동일 요청 replay가 중복 메시지나 업무 쓰기를 만들지 않는다.
- Backend 원본 table에 Agent role의 직접 쓰기가 없다.
- Trace는 Agent DB에만 생성된다.
- 사용자 승인 전에는 기록·정책 변경이 없다.
- 공개 응답에 내부 Trace, Job, 승인 key와 DB 식별자가 없다.

## 8. 데이터 무결성 증적

배포 전후 다음 집계를 비교한다.

- table별 row count와 상태별 count
- FK orphan 수
- 공개 ID와 주요 연관 행의 비식별 digest
- pending/running/retry/terminal queue 수
- callback receipt와 Backend Job 상태
- Trace/Step/Tool 연관 행 수

리포트에는 count, digest와 상태만 남기고 환자 식별자, 메시지, payload,
암호문을 기록하지 않는다.

### 완료된 로컬 기준선

- Agent PostgreSQL: 8행, count/digest 불일치 0건
- Backend/System PostgreSQL: 14,702행, count/digest 불일치 0건
- Agent로 병합된 Backend Trace: Trace 31건, Step 86건
- 병합 당시 Agent 원장 총계: Trace 51건, Step 145건
- 현재 복원 검증 Agent 원장 총계: Trace 53건, Step 153건
- Trace 이관 검증 receipt: `true`
- Backend 레거시 Trace·PHR 테이블: 0개
- 프로젝트 내 SQLite 데이터 파일: 0개
- 최종 복원 검증 백업:
  `dranswer_agent_20260729_v13_clean_final.dump`,
  `dranswer_system_20260729_v13_clean_final.dump`,
  `dranswer_contract_20260729_v13_clean_final.dump`
- Contract Test DB 이관·복원 검증: request 1건, callback 0건,
  schema version 1, request hash 불일치 0건

## 9. 관찰과 트래픽 재개

1. 최소 canary가 통과한 뒤 제한된 트래픽부터 재개한다.
2. DB 연결 오류, pool 사용량, query latency, lock wait와 deadlock을
   관찰한다.
3. queue depth, 가장 오래된 pending 작업, worker heartbeat와 callback
   재시도를 확인한다.
4. 오류율과 latency가 기준을 넘으면 신규 요청을 다시 차단하고 rollback
   판단 절차로 이동한다.
5. staging에서는 최소 24시간 관찰 후 production 승인을 요청한다.

## 10. Rollback과 복구

- application 문제이고 schema가 하위 호환이면 직전 application release로
  되돌린다.
- 데이터 또는 schema 문제이면 신규 요청을 차단하고 PostgreSQL
  snapshot/PITR 복구를 수행한다.
- 복구 시점을 확정한 뒤 Backend DB와 Agent DB의 queue·멱등 상태 차이를
  평가한다.
- worker를 시작하기 전에 stale lock과 callback retry를 점검한다.
- canary에서 queue 중복 처리, 요청 멱등성과 Backend write 멱등성을 다시
  검증한다.
- 이전 저장소나 다른 DB 엔진으로의 fallback·역이관은 수행하지 않는다.

## 11. 배포 완료 기록

다음 정보를 변경 기록에 남긴다.

- application version과 migration version
- backup/PITR 복구 지점
- readiness와 canary 결과
- 비식별 count/digest 비교 결과
- 오류율·latency·queue 관찰 결과
- 트래픽 재개 시각과 승인자
- rollback 여부와 후속 작업
