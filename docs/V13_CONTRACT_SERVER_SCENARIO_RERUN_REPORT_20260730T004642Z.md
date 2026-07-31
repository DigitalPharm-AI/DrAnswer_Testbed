# AI Agent v1.3 원격 규격 시나리오 재실행 결과

## 결론

**PASS — 초기 규격 시나리오 50/50, API 재시작 보존 6/6 통과**

| 항목 | 값 |
|---|---|
| Run ID | `20260730T004642Z` |
| 실행 시간 | 2026-07-30 09:46:56 ~ 09:48:08 KST |
| 서버 | `http://13.124.53.57:8701` |
| 릴리스 | `20260730T002607Z` |
| DB | PostgreSQL, schema version 1 |
| Callback 모드 | `hold` |
| 실패 | 0 |

## 수행 시나리오

| 영역 | 건수 | 통과 |
|---|---:|---:|
| Health, 규격 artifact, OpenAPI | 4 | 4 |
| 인증 및 무단 요청 비저장 | 6 | 6 |
| 동기 Chat, NDJSON, 멱등성, validation | 11 | 11 |
| Chat feedback, privacy, 멱등성, validation | 12 | 12 |
| Endpoint별 멱등키 분리 | 1 | 1 |
| Missed-dose event | 5 | 5 |
| Daily medication pattern analysis | 8 | 8 |
| Callback outbox 및 전송 차단 | 3 | 3 |
| **초기 합계** | **50** | **50** |
| API 재시작 후 replay·보존 | 6 | 6 |

확인된 주요 응답:

- `POST /agent/sync/chat`: HTTP 200, LF-framed NDJSON, 세 response type 및 terminal replay
- `POST /agent/async/chat_feedback`: 신규/동일 요청 HTTP 202, 변경 요청 409
- `POST /agent/async/missed-dose-events`: 신규 202, duplicate 200, 변경 요청 409
- `POST /agent/async/daily-medication-pattern-analysis`: 신규 202, duplicate 200, 변경 요청 409
- 누락·잘못된 인증 401
- strict validation 오류 400이며 FastAPI 기본 422가 외부로 노출되지 않음
- callback release는 Backend 미설정 상태에서 거부되고 delivery 시도는 발생하지 않음

## DB 및 재시작 결과

| 시점 | Request records | Callback jobs | Callback 상태 |
|---|---:|---:|---|
| 재실행 전 | 24 | 9 | `held=9` |
| 초기 50건 후 | 33 | 12 | `held=12` |
| API 재시작 및 6건 후 | 33 | 12 | `held=12` |

예상된 신규 request 9건과 callback 3건만 증가했다. 재시작 후 동일
request ID replay에서는 중복 request나 callback이 생성되지 않았다.
최종 callback 12건의 delivery attempts 합계는 0이다.

## 운영 상태

- PostgreSQL: active/enabled, `127.0.0.1:5432`, NRestarts 0
- Contract API: active/enabled, `0.0.0.0:8701`
- Callback worker: inactive/disabled
- 기존 `chat-server.service`: PID `163828`, Invocation ID
  `0a85d9e85efa493485e068d94d8c8951` 전후 동일
- 기존 포트 8766 listener 유지
- 작업 PC의 공개 `/health/ready`: HTTP 200,
  `database_ready=true`, `callback_mode=hold`, `errors=[]`
- 실행 구간 PostgreSQL/API/callback warning 이상 journal: 0건

## 주의 사항

이번 실행으로 합성 callback이 3건 추가되어 총 12건이 `held` 상태로
남아 있다. Backend 주소가 설정돼도 이 합성 callback들을 자동 또는 일괄
release하면 안 된다.

실제 Backend callback 전송, ACK, retry/timeout은 Backend 주소가 없으므로
이번 재실행 범위에 포함하지 않았다.

## 증거 파일

- [사전 상태](test-results/v13-contract-server-rerun-20260730T004642Z/contract-rerun-20260730T004642Z-ops-before.json)
- [초기 50건 상세 결과](test-results/v13-contract-server-rerun-20260730T004642Z/contract-rerun-20260730T004642Z-initial.json)
- [초기 호출 후 상태](test-results/v13-contract-server-rerun-20260730T004642Z/contract-rerun-20260730T004642Z-ops-after-initial.json)
- [재시작 후 6건 상세 결과](test-results/v13-contract-server-rerun-20260730T004642Z/contract-rerun-20260730T004642Z-post-restart.json)
- [최종 상태](test-results/v13-contract-server-rerun-20260730T004642Z/contract-rerun-20260730T004642Z-ops-final.json)
