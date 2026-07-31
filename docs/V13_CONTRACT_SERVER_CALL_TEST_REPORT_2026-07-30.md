# AI Agent 규격서 1.3 EC2 서버 호출 테스트 결과

> 이 문서는 SQLite 릴리스 `20260729T142559Z`의 전환 전 기준선이다.
> 현재 EC2는 PostgreSQL 릴리스 `20260730T002607Z`로 전환됐으며 최신 결과는
> [PostgreSQL 전환 리포트](V13_CONTRACT_SERVER_POSTGRES_CUTOVER_REPORT_2026-07-30.md)를
> 기준으로 한다.

## 1. 결론

**PASS — 규격 적합성 및 재시작 보존 테스트 56/56 통과, 외부 접근성 확인 2/2 통과**

현재 EC2 테스트 서버는 규격서 1.3의 네 개 AI Agent API에 대해 정상 요청, 인증 실패, 입력 검증, 멱등 재호출, 충돌 응답 및 콜백 보류 동작을 만족한다. API 서비스 재시작 뒤에도 처리 결과와 콜백 작업이 보존되었고, 기존 `chat-server.service`에는 영향이 없었다.

Backend 주소와 인증정보가 아직 없으므로 실제 Backend callback 전송은 수행하지 않았다. 생성된 callback은 의도한 대로 모두 `held`, `attempts=0` 상태이며 callback worker도 비활성 상태로 유지했다.

## 2. 테스트 대상

| 항목 | 값 |
|---|---|
| 테스트 일시 | 2026-07-30 08:13:29 ~ 08:15:06 KST |
| EC2 | `13.124.53.57` |
| 공개 포트 | `8701` |
| 테스트 URL | `http://13.124.53.57:8701` |
| 배포 릴리스 | `20260729T142559Z` |
| 릴리스 경로 | `/opt/dranswer-agent-contract/releases/20260729T142559Z` |
| 배포 압축본 SHA-256 | `fd1820631ebd32e0f2adad18d6cf5d3ddd2f70527f39b208843a63896f4a2cb4` |
| 실제 저장소 | SQLite |
| Callback 모드 | `hold` |
| Backend callback 설정 | 미설정 |

검증 기준은 배포 서버가 제공하는 v1.3 Chat, 비동기 약물 이벤트, Backend callback 규격 파일과 OpenAPI이다. 제공되는 세 규격 파일이 배포 릴리스 내부 파일과 byte 단위로 동일한지도 확인했다.

## 3. 수행 방법

1. 작업 PC에서 EC2 공개 주소의 `/health`, `/health/ready`를 호출해 포트 `8701`의 외부 접근성을 확인했다.
2. 인증 토큰을 외부로 반출하지 않기 위해 인증이 필요한 규격 호출은 EC2 내부에서 실제 HTTP listener `127.0.0.1:8701`로 수행했다.
3. 테스트 전후에 systemd 상태, listener, 릴리스, DB 레코드 및 callback outbox 상태를 수집했다.
4. AI Agent API 서비스만 재시작한 뒤 기존 request ID로 재호출하여 멱등 결과와 callback 보존을 다시 확인했다.
5. 테스트 시간대의 API 및 callback 서비스 journal에서 warning 이상 로그를 확인했다.

모든 요청에는 합성 ID와 합성 데이터만 사용했으며, 결과 파일에는 API 토큰·Authorization 값·callback 본문을 기록하지 않았다.

## 4. 테스트 결과 요약

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
| 재시작 후 보존 | 6 | 6 | 0 |
| **합계** | **56** | **56** | **0** |

별도로 작업 PC에서 수행한 공개 주소 smoke test도 `/health`와 `/health/ready` 모두 HTTP 200으로 통과했다. Ready 응답의 `callback_delivery_ready=false`는 Backend 미설정에 따른 의도된 `hold` 상태이다.

## 5. Endpoint별 확인 결과

| Endpoint | 확인한 정상/재호출 결과 | 확인한 오류 결과 |
|---|---|---|
| `POST /agent/sync/chat` | HTTP 200, LF-framed NDJSON, `text`·`selection_box`·`input_box`, 동일 요청은 저장된 terminal event만 sequence 0으로 재생 | 400 입력/Accept/JSON, 401 인증, 409 멱등 충돌 |
| `POST /agent/async/chat_feedback` | 신규 및 동일 정규화 요청 HTTP 202, reaction-only·text-only·동시 입력 | 400 빈 값/길이/시간/ID/추가 필드, 401 인증, 409 멱등 충돌 |
| `POST /agent/async/missed-dose-events` | 신규 HTTP 202 `accepted`, 동일 요청 HTTP 200 `duplicate` | 400 ID/추가 필드, 401 인증, 409 멱등 충돌 |
| `POST /agent/async/daily-medication-pattern-analysis` | 신규 HTTP 202 `accepted`, 동일 요청 HTTP 200 `duplicate` | 400 빈/중복/비배열 patient ID·날짜·추가 필드, 401 인증, 409 멱등 충돌 |

추가 확인 사항:

- OpenAPI의 공개 Agent route가 위 네 개뿐이며 Bearer 인증이 선언되어 있다.
- 규격 오류가 FastAPI 기본 422로 노출되지 않고 규격의 400/401/409 오류 envelope로 변환된다.
- 같은 `request_id`라도 endpoint path가 다르면 서로 독립된 멱등 범위를 갖는다.
- Feedback 자유 텍스트는 request 저장소에 평문으로 남지 않으며, request digest는 단순 SHA가 아닌 keyed digest이다.
- 인증 실패 요청은 request 저장소를 변경하지 않는다.
- 생성된 규격 유효 callback 3건은 `held`, `attempts=0`으로 저장되고, Backend 설정 없이 release를 시도하면 거부된다.

## 6. 저장·재시작·서비스 격리 결과

| 시점 | Request records | Callback jobs | Callback 상태 |
|---|---:|---:|---|
| 테스트 전 | 6 | 3 | `held=3` |
| 초기 50건 수행 후 | 15 | 6 | `held=6` |
| API 재시작 및 재검증 후 | 15 | 6 | `held=6` |

초기 테스트에서 신규로 저장되어야 하는 요청 9건과 callback 3건만 증가했다. API 재시작 후 동일 요청을 재생해도 레코드나 callback이 중복 생성되지 않았다.

| 서비스 | 테스트 전 | 테스트 후 | 판정 |
|---|---|---|---|
| `dranswer-agent-contract-api.service` | active/enabled, PID `255206` | active/enabled, PID `272371` | 의도한 재시작 성공 |
| `dranswer-agent-contract-callback.service` | inactive/disabled | inactive/disabled | 외부 전송 차단 유지 |
| `chat-server.service` | active/enabled, PID `163828` | active/enabled, PID `163828` | 영향 없음 |

`chat-server.service`의 Invocation ID도 전후 동일했고, 포트 `8766` listener가 유지됐다. AI Agent 테스트 서버의 포트 `8701` listener는 새 API PID로 정상 복구됐다. 테스트 시간대 warning 이상 journal 항목은 0건이었다.

## 7. 이번 단계에서 수행하지 않은 항목

다음 항목은 실패가 아니라 현재 단계의 의도된 미검증 범위다.

- 실제 Backend callback 전송, Backend ACK 및 callback 재시도/timeout
- Backend 소유권 조회가 필요한 404 시나리오
- 장애 주입이 필요한 500/503/504 시나리오
- 동시성·부하·장시간 안정성 테스트
- 실제 LLM 응답 품질 및 의료 내용 평가

Backend URL과 인증정보가 확정되면 callback worker를 활성화하기 전에 위 callback 전송 시나리오를 별도 단계로 수행해야 한다.

## 8. 발견 사항과 주의점

### 배포본과 현재 작업 소스의 저장소 차이

이번 결과는 EC2에 실제 배포된 릴리스 `20260729T142559Z`의 **SQLite 구현**에 대한 인증 결과이다. 현재 로컬 작업 소스는 `CONTRACT_DATABASE_URL` 기반의 **PostgreSQL 전용 구현**으로 변경되어 있으므로, 이 리포트를 현재 로컬 HEAD의 PostgreSQL 구현 인증으로 해석하면 안 된다.

Backend 연동 전에 다음 중 하나를 명시적으로 선택해야 한다.

1. 현재 SQLite 릴리스를 임시 contract test 기준선으로 동결한다.
2. PostgreSQL 배포와 migration을 완료한 새 릴리스에 동일한 56건을 다시 수행한다.

### 네트워크 보안

현재 공개 endpoint는 TLS 없는 HTTP이다. 실제 환자정보나 운영 토큰을 사용하기 전에는 보안그룹 source 제한, HTTPS 종단 및 운영 secret 관리가 필요하다. 이번 테스트에는 합성 데이터만 사용했다.

## 9. 증거 파일

- [초기 50건 상세 결과](test-results/v13-contract-server-2026-07-30/contract-conformance-initial.json)
- [재시작 후 6건 상세 결과](test-results/v13-contract-server-2026-07-30/contract-conformance-post-restart.json)
- [테스트 전 운영 스냅샷](test-results/v13-contract-server-2026-07-30/contract-ops-before.json)
- [초기 호출 후 운영 스냅샷](test-results/v13-contract-server-2026-07-30/contract-ops-after-initial.json)
- [재시작 후 운영 스냅샷](test-results/v13-contract-server-2026-07-30/contract-ops-after-restart.json)
