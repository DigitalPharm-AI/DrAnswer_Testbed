# EC2 계약 테스트 데모 서버 보안 검토

- 검토일: 2026-08-02 KST
- 대상 EC2: `13.124.53.57`
- Agent 계약 테스트 API: `http://13.124.53.57:8701`
- 배포 릴리스: `20260731T062442Z-access-log`
- 검토 방식: 로컬 소스 검토, EC2 읽기 전용 설정 점검, 비침투적 외부 HTTP 확인

## 요약

현재 EC2 전체 상태는 인터넷 공개 데모로 안전하다고 보기 어렵다. 가장 큰
위험은 Agent 계약 서버 `8701`이 아니라 같은 인스턴스의 기존
`chat-server.service`가 사용하는 `8766` 포트다. 이 서비스는 외부에서 접근
가능하고, 인증 없이 `/chat` 요청을 받아 실제 Bedrock 호출로 연결하며 root로
실행된다. 또한 요청과 대화 내용을 그대로 journal에 기록한다.

`8701` 계약 테스트 서버 자체는 Bearer 인증, 비-root 실행, 강한 systemd
격리, 로컬 전용 PostgreSQL, 분리된 DB 역할과 비밀 파일 권한 등 기본 보호가
잘 적용되어 있다. 그러나 평문 HTTP이므로 Bearer Token과 요청 데이터가 전송
구간에서 보호되지 않고, 요청 크기·빈도·DB 보존 한도가 없다. 따라서 합성
데이터를 이용하고 Security Group을 제한한 단기 계약 테스트로만 사용해야
하며 실제 환자 데이터에는 사용하면 안 된다.

## High

### DR-SEC-001 — 인증 없는 root Bedrock 서버가 8766에 공개됨

- Rule ID: `FASTAPI-AUTH-001`, `FASTAPI-LIMITS-001`에 준하는 서비스 인증·자원 제한 문제
- Severity: **High — 즉시 조치 권고**
- Location:
  - EC2 `/etc/systemd/system/chat-server.service`: `User=root`, `ExecStart=/app/chat/.venv/bin/python src/chat_server.py`
  - EC2 `/app/chat/src/chat_server.py:258-259`: 전역 메모리 세션 저장소
  - EC2 `/app/chat/src/chat_server.py:331-338`: 무인증 `/chat`, 제한 없는 `Content-Length` 읽기
  - EC2 `/app/chat/src/chat_server.py:344-365`: 사용자 지정 `model_id`, 요청·대화 전체 로그
  - EC2 `/app/chat/src/chat_server.py:412`: 실제 Bedrock 스트리밍 호출
- Evidence:
  - `0.0.0.0:8766`에서 수신 중이며 외부 `/health`가 `200`을 반환했다.
  - 인증 헤더 없이 `/chat`에 빈 JSON을 보내면 인증 실패가 아니라 입력 검증
    `400`을 반환해, 네트워크 단계의 인증이 없음을 확인했다. Bedrock 호출을
    발생시키는 유효 메시지는 보내지 않았다.
  - 소스에서 Bearer/API key 검증이 없고 `model_id`도 요청자가 선택할 수 있다.
  - `logger.info("[data] %s", data)`와 conversation 로그가 메시지·환자 ID·대화
    내용을 journal에 저장한다.
  - `systemd-analyze security` 결과는 `9.6 UNSAFE`, 서비스는 root이며
    `NoNewPrivileges`, `ProtectSystem`, `PrivateTmp` 등이 비활성 상태다.
- Impact: 네트워크 접근자는 AWS Bedrock 비용을 발생시키고, 동시 요청·대용량
  요청·무제한 세션으로 메모리를 고갈시키며, 실제 데이터가 들어오면 journal에
  민감 정보가 축적될 수 있다. 애플리케이션 취약점 발생 시 root 권한 때문에
  같은 호스트의 8701 비밀정보와 DB까지 영향 범위가 확장된다.
- Fix:
  1. 더 이상 필요하지 않으면 `8766` Security Group 인바운드를 먼저 제거하고
     `chat-server.service`를 중지·비활성화한다.
  2. 필요하면 별도 비-root 서비스 계정으로 분리하고 8701 수준의 systemd
     hardening을 적용한다.
  3. Bearer Token 또는 mTLS를 필수화하고, 허용 모델 ID 고정, 요청 크기·빈도·
     동시성·세션 수·세션 TTL 제한을 적용한다.
  4. 요청 Body와 conversation 전체 로그를 제거하거나 비식별·요약 로그로
     교체한다.
- Mitigation: 즉시 종료가 어렵다면 Security Group source를 담당 개발자 고정
  IP 또는 VPN CIDR로 제한하고, Bedrock 사용량 알람과 예산 제한을 적용한다.
- False positive notes: 외부에서 실제 Bedrock 호출은 수행하지 않았지만, 공개
  라우트와 Bedrock 호출 연결은 서버 소스로 직접 확인했다.

### DR-SEC-002 — 8701 Bearer 인증 트래픽이 평문 HTTP로 전송됨

- Rule ID: `OWASP Transport Layer Security`, `FASTAPI-AUTH-002`
- Severity: **High — 실제 데이터 사용 전 필수 조치**
- Location:
  - [README.md](../deploy/ec2-contract-server/README.md#L173): plain HTTP 테스트 endpoint 명시
  - [dranswer-agent-contract-api.service](../deploy/ec2-contract-server/systemd/dranswer-agent-contract-api.service#L14): Uvicorn이 직접 `0.0.0.0:8701` 수신
  - [main.py](../contract_test_server/main.py#L312): Bearer Token 검증
- Evidence:
  - `http://13.124.53.57:8701/health`는 외부에서 `200`이다.
  - 동일 포트 HTTPS handshake는 실패했다.
  - 정상 API는 `Authorization: Bearer <TOKEN>`과 patient/message 식별자를 HTTP로
    전달한다.
- Impact: 클라이언트와 EC2 사이의 네트워크 경로를 관찰하거나 변조할 수 있는
  주체가 Bearer Token과 요청 내용을 탈취·재사용할 수 있다.
- Fix: ALB/Nginx/Caddy 등에서 TLS를 종료해 HTTPS만 허용하고 HTTP 직접 접근을
  차단한다. 그 후 현재 공유 Token을 회전한다.
- Mitigation: TLS 적용 전에는 Security Group source를 Backend 테스트 서버 또는
  지정 개발자/VPN IP로 제한하고 합성 데이터만 사용한다.
- False positive notes: 전용 VPN이나 사설망 보호가 별도로 있다면 경로 위험은
  줄지만, 이번 점검 위치에서 공인 주소로 직접 접근 가능했다. AWS 계정 권한이
  없어 Security Group CIDR 자체는 조회하지 못했다.

## Medium

### DR-SEC-003 — 8701에 요청·호출·저장량 상한과 프로세스 자원 한도가 없음

- Rule ID: `FASTAPI-LIMITS-001`, OWASP API4:2023
- Severity: **Medium**
- Location:
  - [chat_contracts.py](../shared/chat_contracts.py#L43): 일반 text 메시지에 `max_length` 없음
  - [async_v13_contracts.py](../shared/async_v13_contracts.py#L24): 환자 ID 배열에 최대 개수 없음
  - [storage.py](../contract_test_server/storage.py#L145): 요청·callback 영속 테이블
  - [dranswer-agent-contract-api.service](../deploy/ec2-contract-server/systemd/dranswer-agent-contract-api.service#L14): edge/body/rate limit 없음
- Evidence:
  - 리버스 프록시나 애플리케이션 request body limit/rate limit이 없다.
  - `daily-medication-pattern-analysis`는 입력 환자 수만큼 callback 행을 만들 수 있다.
  - 배포 소스에 보존기한 cleanup 또는 purge가 없다.
  - systemd 런타임은 `MemoryMax=infinity`, `CPUQuota=infinity`다.
  - 현재 DB는 약 8 MB이며 `request_records=77`, `callback_jobs=24`다.
- Impact: 유효한 공유 Token을 가진 사용자 또는 Token 탈취자가 대용량·고빈도
  요청으로 메모리·CPU·DB를 소진해 데모를 중단시킬 수 있다.
- Fix: edge에서 body/rate/concurrency limit, Pydantic 문자열·배열 최대값,
  callback batch 최대값, 데이터 TTL·정리 작업, systemd `MemoryMax`/`CPUQuota`를
  추가한다.
- Mitigation: Security Group source 제한과 짧은 테스트 기간 운영, DB 크기 알람을
  적용한다.
- False positive notes: 모든 state-changing API에는 Bearer 인증이 있어 무인증
  공격보다 Token 보유·탈취 시나리오가 중심이다.

### DR-SEC-004 — 하나의 장기 공유 Bearer Token으로 호출자 구분·개별 폐기가 불가능함

- Rule ID: `FASTAPI-AUTH-001`, `FASTAPI-AUTH-002`
- Severity: **Medium**
- Location:
  - [config.py](../contract_test_server/config.py#L52): 단일 `AGENT_SYNC_API_TOKEN`
  - [main.py](../contract_test_server/main.py#L312): 모든 Agent API가 동일 Token 검증
- Evidence: Token은 충분한 길이와 상수시간 비교를 사용하지만 만료, 발급 대상,
  scope 또는 key ID가 없다. 접근 로그에는 IP만 남아 개발자 개인을 식별하지
  못한다.
- Impact: 개발팀 중 한 곳에서 Token이 유출되면 전체 호출 권한이 노출되고,
  특정 개발자만 폐기하거나 호출 주체를 확정하기 어렵다.
- Fix: 최소한 소비자별 API key를 발급·해지할 수 있게 하고 key ID만 로그에
  남긴다. 장기적으로 짧은 수명의 JWT 또는 mTLS를 고려한다.
- Mitigation: 전달 채널을 제한하고 정기 회전하며, 유출 의심 시 즉시 전체 Token을
  교체한다.
- False positive notes: 소수 인원의 단기 데모에서는 운영 복잡도를 고려해 단일
  Token을 임시 허용할 수 있다.

### DR-SEC-005 — 호스트 방어 계층이 일부 비활성 상태임

- Rule ID: `OWASP API8:2023 Security Misconfiguration`
- Severity: **Medium**
- Location: EC2 OS 및 SSH 런타임 설정
- Evidence:
  - SELinux는 `Permissive`다.
  - `PermitRootLogin without-password`로 root 공개키 로그인이 허용된다.
  - `dnf-automatic.timer`는 비활성 상태다.
  - 호스트 firewalld/nftables 규칙은 확인되지 않았으며, 외부 경계는 Security
    Group에 의존한다.
- Impact: 애플리케이션 또는 SSH key가 침해되었을 때 추가 격리와 자동 패치가
  제공할 수 있는 방어 효과가 줄어든다.
- Fix: 애플리케이션 호환성 확인 후 SELinux Enforcing, `PermitRootLogin no`, 보안
  업데이트 운영 절차 또는 자동화, 최소 인바운드 규칙을 적용한다.
- Mitigation: EC2 관리자 key를 최소 인원으로 제한하고 CloudTrail/SSM/로그 알람을
  운영한다.
- False positive notes: AWS Security Group 규칙은 조회 권한 부족으로 직접 검증하지
  못했다. AWS 관리형 패치 정책이 별도로 존재할 수도 있다.

### DR-SEC-006 — 배포 릴리스와 현재 작업 트리의 callback 비밀 분리 방식이 다름

- Rule ID: Secret trust-boundary separation
- Severity: **Medium — 다음 배포 전 확인 필요**
- Location:
  - [callbacks.py](../contract_test_server/callbacks.py#L76): 현재 작업 트리는 수신용
    `agent_sync_api_token`을 Backend callback에도 사용
  - [config.py](../contract_test_server/config.py#L52): 현재 작업 트리에 별도
    `backend_api_token` 설정이 없음
- Evidence:
  - 현재 EC2 릴리스의 `config.py`와 `callbacks.py` 해시는 작업 트리와 다르다.
  - 배포 릴리스에는 별도 `BACKEND_API_TOKEN`이 있고 callback 전송에도 이를
    사용하므로 현재 서버는 이 문제에 영향받지 않는다.
  - 현재 작업 트리로 다시 번들을 만들면 양방향 신뢰 경계가 하나의 Token으로
    합쳐진다.
- Impact: 향후 callback 대상 Backend에 수신용 Agent API Token이 전달되어 한쪽
  시스템 침해가 반대 방향 호출 권한 침해로 이어질 수 있다.
- Fix: 다음 배포 전에 별도 `BACKEND_API_TOKEN` 설정과 중복 비밀 거부 검증을
  유지하고 회귀 테스트를 추가한다.
- Mitigation: 현재처럼 `CALLBACK_MODE=hold`와 callback worker 비활성 상태를
  유지한다.
- False positive notes: 현재 배포 릴리스는 비밀이 분리되어 있고 callback도
  비활성 상태이므로 즉시 노출된 취약점은 아니다.

## Low

### DR-SEC-007 — 8701 문서·서버 정보·임의 Host header가 공개됨

- Rule ID: `FASTAPI-OPENAPI-001`, `FASTAPI-HOST-001`
- Severity: **Low**
- Location:
  - [main.py](../contract_test_server/main.py#L300): 기본 `/docs`, `/openapi.json` 활성
  - [main.py](../contract_test_server/main.py#L860): 규격 JSON 공개 제공
- Evidence:
  - 외부 `/docs`, `/openapi.json`, 계약 JSON이 모두 `200`이다.
  - `Host: attacker.invalid` 요청도 `/health`에서 `200`이다.
  - 응답에 `server: uvicorn`이 노출되고 readiness는 release/DB/callback 상태를
    제공한다.
- Impact: 공격자가 엔드포인트와 프레임워크·운영 상태를 더 쉽게 파악할 수 있다.
- Fix: 규격 문서 공개가 불필요하면 비활성화하거나 IP 제한하고,
  `TrustedHostMiddleware` 또는 edge Host 검증을 적용한다.
- Mitigation: 실제 비밀이나 환자 데이터는 문서·health에 포함하지 않는다.
- False positive notes: 개발팀 계약 테스트 목적상 규격 공개는 의도된 기능이며,
  Host 값을 보안 결정이나 callback URL 생성에 사용하지 않아 직접 영향은 낮다.

## 확인된 보호장치

- 네 개 `/agent/*` API 모두 Bearer 인증을 요구하며 무인증 호출은 `401`이다.
- OpenAPI에도 네 API 모두 `AgentSyncBearer` security requirement가 있다.
- `/_test/callbacks`는 별도 Test Control Token을 요구하고 schema에서 숨겨져 있다.
- Agent Token 검증은 `hmac.compare_digest`를 사용한다.
- 8701 서비스는 `dranswer-contract` 비-root 계정, 빈 capability set,
  `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp` 등을 사용한다.
  `systemd-analyze security` 결과는 `3.8 OK`다.
- 배포 파일은 root 소유이고 서비스 계정이 수정할 수 없다.
- 환경 파일은 `640 root:dranswer-contract`, migration 환경은 `600 root:root`다.
- PostgreSQL은 `127.0.0.1:5432`에만 바인딩되고 SCRAM 인증을 사용한다.
  runtime/migration 역할 모두 superuser·createdb·createrole 권한이 없다.
- callback은 현재 `hold`, worker는 inactive/disabled다. 배포 릴리스는 별도 Backend
  Token, HTTPS 기본 요구, 고정 callback path allowlist, redirect 금지를 적용한다.
- 계약 모델은 unknown field를 거부하고 공개 ID 형식을 제한한다. DB 쿼리는
  SQLAlchemy bind parameter를 사용한다.
- 접근 로그에서 Authorization/Bearer/환경 Token 패턴은 발견되지 않았다.

## 운영 관찰 및 제한사항

- 2026-07-31 이후 8701 접근 로그에는 loopback과 두 개의 외부 공인 IP가 있었다.
  이번 점검 요청도 포함되어 있으며 IP만으로 개발자 개인을 식별할 수 없다.
- AWS CLI 권한이 없어 Security Group의 실제 CIDR을 조회하지 못했다. 다만 8701과
  8766 모두 점검 위치에서 외부 접근이 가능했다.
- 침투 테스트, 대용량 부하 테스트, 실제 Bedrock 요청은 수행하지 않았다.
- Python dependency CVE 자동 점검은 `pip-audit`이 설치되어 있지 않아 이번 범위에
  포함하지 않았다.
- 점검 과정에서 서버 설정이나 서비스를 변경하지 않았다.

## 권고 조치 순서

1. **즉시:** 8766 인바운드를 차단하고 불필요하면 `chat-server.service`를 중지한다.
2. **당일:** 8701 Security Group source를 개발팀/VPN/Backend 테스트 서버로 제한한다.
3. **실데이터 전:** HTTPS를 적용하고 기존 Agent Token을 회전한다.
4. **다음 배포 전:** body/rate/cardinality/retention/systemd 자원 제한과 소비자별
   Token을 추가한다.
5. **호스트 정비:** 8766 비-root화, SELinux/SSH/패치 정책을 강화한다.
6. **배포 정합성:** 현재 작업 트리의 callback 비밀 분리를 배포 릴리스 수준으로
   복구한 후에만 새 번들을 만든다.

## 참고 기준

- [OWASP Transport Layer Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Transport_Layer_Security_Cheat_Sheet.html)
- [OWASP API4:2023 Unrestricted Resource Consumption](https://owasp.org/API-Security/editions/2023/en/0xa4-unrestricted-resource-consumption/)
- [OWASP API8:2023 Security Misconfiguration](https://owasp.org/API-Security/editions/2023/en/0xa8-security-misconfiguration/)
- [Uvicorn proxy/forwarded header settings](https://www.uvicorn.org/settings/)
- [Starlette TrustedHostMiddleware](https://www.starlette.io/middleware/#trustedhostmiddleware)
