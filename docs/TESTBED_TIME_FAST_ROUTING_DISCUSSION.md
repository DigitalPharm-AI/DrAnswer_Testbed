# 테스트베드 시간축과 fast 모델 라우팅 논의안

- 상태: 시간 안건 결정 완료, fast 안건 보류
- 결정 상태: 시간축은 업무 시각·물리 기록 시각·sequence 분리 채택
- 범위: 테스트베드/실서비스 시간 의미, 대화 조회·표시·정렬, fast 모델 도입 검증
- 비범위: 이 문서에서 DB 스키마·조회문·공개 API를 즉시 변경하는 것, fast 라우터를 즉시 구현하는 것

## 1. 이번 논의에서 분리할 두 안건

두 문제는 관측 로그를 함께 사용하지만 서로 독립적으로 결정한다.

1. **시간 안건**: 테스트베드의 시뮬레이션 시간과 실서비스의 실제 시간이
   같은 대화 저장·조회 계약을 안전하게 사용할 수 있는가?
2. **모델 안건**: 현재 Sonnet 단일 실행을 유지할지, Sonnet reasoning off 또는
   fast 모델을 어떤 요청에 적용할지?

시간 안건의 결론이 나오기 전에는 DB 날짜 필터나 timestamp 의미를 변경하지
않는다. 모델 안건도 품질·지연 데이터를 모으기 전에는 자동 라우팅을 추가하지
않는다.

## 2. 먼저 합의할 현재 사실

### 2.1 시간 필드의 현재 역할

| 이름 | 경계 | 현재 값/용도 |
|---|---|---|
| `ChatSyncRequest.message_at` | Backend → AI 공개 v1.3 계약 | 사용자 메시지의 신뢰 시간. 테스트베드에서는 시뮬레이션 시각 |
| `ChatSyncResponse.message_at` | AI → Backend 공개 v1.3 계약 | 현재 AI 응답 생성 완료 시점인 실제 UTC 시각 |
| `chat_messages.created_at` | Backend DB | user는 요청 `message_at`, assistant는 응답 `message_at`으로 저장 |
| `chat_messages.display_at` | Backend DB 내부 | UI 날짜 구간·표시용 시각. 응답 완료 후 user와 assistant를 동일한 시뮬레이션 시각으로 맞춤 |
| `display_message_at` | Backend → React UI 계약 | UI에서 user/assistant에게 표시할 시뮬레이션 시각 |
| `sort_sequence` | Backend → React UI 계약 | DB 저장 순서에서 만든 단조 증가 UI 정렬 키 |

따라서 규격서에 직접 있는 UI 필드명은 `display_at`이 아니라
`display_message_at`이다. `display_at`은 이를 지원하는 Backend DB 내부
컬럼이다. React의 확정 메시지 정렬 기준은 시간이 아니라 `sort_sequence`다.

### 2.2 현재 대화 흐름이 어긋날 수 있는 지점

테스트베드가 2026-04-20 09:30이고 서버 실제 시간이 그보다 뒤라고 가정한다.

1. user 메시지는 `created_at=2026-04-20 09:30`으로 저장된다.
2. AI 응답은 실제 응답 완료 시각으로 반환되므로 assistant 메시지의
   `created_at`은 서버 실제 날짜가 된다.
3. Backend는 화면 표시를 위해 assistant의 `display_at`만 다시
   `2026-04-20 09:30`으로 맞춘다.
4. React 이력은 `display_at`과 `sort_sequence`를 사용하므로 화면에서는 같은
   날짜에 올바른 순서로 보일 수 있다.
5. 반면 AI read view `ai_v13_chat_messages`는 `display_at`을 노출하지 않고
   `created_at`을 노출한다.
6. AI의 최근 대화 조회는 현재 user 메시지의 `created_at` 이하만 조회한다.
   따라서 실제 날짜로 저장된 이전 assistant 답변이 조회 대상에서 빠질 수
   있다.

즉 현재 확인된 문제는 단순히 “DB를 전부 읽고 오늘이 아니어서 애플리케이션에서
필터링한다”는 형태가 아니다. **DB 조회 조건 자체가 `created_at`을 대화 시간축으로
간주하지만, 테스트베드에서는 user와 assistant의 `created_at`이 서로 다른
시간축을 가질 수 있는 것**이 핵심이다.

이 현상은 시뮬레이션 시간과 실제 시간이 거의 같은 실서비스에서는 드러나지
않을 가능성이 높다. 그러나 테스트베드 전용 예외를 추가하면 Agent Server를
떼어 실사용할 때 시간 의미가 다시 달라질 수 있으므로, 수정 전에 저장 시간의
의미부터 확정해야 한다.

## 3. 시간 안건에서 결정할 질문

### T1. `created_at`은 무엇을 의미해야 하는가?

- 저장이 실제로 완료된 물리 시각(`recorded_at`)인가?
- 대화가 발생한 업무 시각(`conversation_at` 또는 `occurred_at`)인가?
- 현재처럼 레코드 종류에 따라 둘 다 될 수 있는가?

시작 제안은 **물리 시각과 업무 시각을 개념적으로 분리하는 것**이다. 이름과
마이그레이션 방식은 그다음에 결정한다.

### T2. “오늘 대화”는 어느 시계를 기준으로 하는가?

- 테스트베드: 시뮬레이션 시계의 `Asia/Seoul` 날짜
- 실서비스: 인증된 요청 시각 또는 서버 업무 시계의 `Asia/Seoul` 날짜

환경별 SQL 분기를 두기보다 `TimeContext` 또는 동등한 명시적 입력으로
“업무상 현재 시각”을 결정하고, 같은 조회 규칙에 전달할지를 논의한다.

### T3. 대화의 순서와 날짜 구간은 무엇으로 결정하는가?

- 인과 순서: 영속적인 `sort_sequence` 또는 별도 conversation sequence
- 날짜 구간: 업무 시각
- 감사·지연 분석: 물리 저장 시각

한 timestamp가 이 세 역할을 모두 맡지 않도록 하는 것이 논의의 기준이다.

### T4. AI read view가 어떤 시각을 노출해야 하는가?

아래 세 안을 비교한다.

| 안 | 내용 | 장점 | 위험 |
|---|---|---|---|
| A. 최소 수정 | read view에 대화용 시각을 추가하고 최근 대화 조회에서 사용 | 변경 범위가 작음 | `display_at`을 그대로 재사용하면 UI 의미와 Agent 의미가 결합됨 |
| B. 시간 의미 분리 | 업무 발생 시각과 물리 저장 시각을 별도 필드로 정의하고 view도 명시적으로 노출 | 테스트베드와 실서비스에 동일한 계약 적용 가능 | 스키마·이관·계약 변경 검토 필요 |
| C. 테스트베드 분기 | 테스트베드에서만 다른 조회 조건 사용 | 단기 적용이 빠름 | 실서비스 재사용성과 회귀 테스트가 이원화됨 |

논의 시작점으로는 **B를 목표 모델로 두고 A가 안전한 과도기인지 검토**한다.
C는 긴급한 테스트 차단 해소 외에는 채택하지 않는 편이 좋다.

## 4. 시간 안건에 필요한 재현 증거

DB 변경 전에 동일한 `request_id` 흐름에서 아래를 한 묶음으로 수집한다.

1. 시뮬레이션 시각과 실제 서버 시각
2. user/assistant 각각의 `created_at`, `display_at`, DB `id`
3. read view가 노출한 `created_at`
4. 최근 대화 SQL의 cutoff와 반환된 message ID 목록
5. `agent_backend_chat_context_loaded`의 `recent_chat_count`, 역할 순서,
   마지막 메시지 시각
6. React가 받은 `display_message_at`, `sort_sequence`

### 최소 재현 시나리오

1. 시뮬레이션 시계를 실제 날짜와 다른 날짜의 09:30으로 고정한다.
2. 자유 질문 → 구조화 선택 → 선택 응답 → 후속 회상 질문을 순서대로 보낸다.
3. 각 요청 사이에 실제 시간 지연을 둔다.
4. 화면 순서와 AI `recent_chat` 순서를 비교한다.
5. 같은 시나리오를 실제 시각과 시뮬레이션 시각이 같은 조건에서도 실행한다.

### 시간 안건 승인 기준

- 시뮬레이션 날짜가 실제 날짜와 달라도 직전 assistant와 구조화 카드가
  `recent_chat`에서 누락되지 않는다.
- 화면 날짜 구간은 업무 시각을 따르고, 같은 구간 안의 확정 순서는
  `sort_sequence`로 안정적이다.
- 실서비스 모드에서는 시뮬레이션 전용 DB 분기 없이 동일한 계약을 사용한다.
- 물리 지연 측정용 시각은 업무 시각에 덮어써지지 않는다.
- 기존 v1.3 공개 계약 변경 여부와 DB 이관 계획이 명시된다.

## 5. fast 모델의 현재 상태

현재 구현은 요청별 fast 라우터가 아니다.

- `LLM_MODEL_TIER`의 기본값은 `sonnet`이다.
- `fast`는 기본 설정상 Claude Haiku 4.5, `sonnet`은 Claude Sonnet 5이다.
- 내부 `/agent/model-config` 변경은 **프로세스 전역 in-memory tier**를 바꾼다.
- 이 값은 요청별·환자별이 아니며 재시작 시 초기 설정으로 돌아간다.
- 여러 worker/process가 있으면 각 프로세스의 값이 달라질 수 있으므로 현재
  메커니즘을 그대로 운영 라우터로 사용하면 안 된다.
- `LLM_REASONING_ENABLED`도 전역 설정이다. 현재 코드에서는 `false`이면
  Sonnet 요청의 `thinking`/`output_config` 필드를 생략하므로 reasoning-off
  대조군을 만들 수 있다. 다만 런타임 API로 전환할 수 없고 설정 변경 후
  재시작이 필요하다.
- 현재 기본값처럼 reasoning이 켜져 있으면 Sonnet 5는 adaptive thinking,
  Haiku 4.5는 최소 budget 1,024의 extended thinking 경로를 사용한다. 따라서
  “Haiku로 바꾸면 곧바로 충분히 빨라진다”는 가정은 실측이 필요하다.

## 6. fast 논의 전에 실행할 비교 실험

모델 크기 효과와 reasoning 효과를 분리하기 위해 아래 순서로 비교한다.

| 실험군 | 모델 | reasoning | 목적 |
|---|---|---|---|
| M0 | Sonnet 4.6 | adaptive/medium | 현재 기준선 |
| M1 | Sonnet 4.6 | off | 모델을 유지한 채 reasoning 지연·품질 영향 확인 |
| M2 | Haiku 4.5 | off | 순수 fast 모델의 지연·품질 확인 |
| M3 | Haiku 4.5 | extended thinking | fast 모델에서 reasoning 비용과 품질 이득 확인 |

우선 M0와 M1을 비교한다. Sonnet reasoning off만으로 목표 응답시간과 품질을
충족하면 요청별 fast 라우터의 복잡성을 추가하지 않아도 된다. 그다음 M2를
비교하고, M3는 extended thinking이 필요한 fast 후보 요청이 실제로 있는지
확인하는 용도로 둔다.

### 공통 평가 대화

- 단순 오늘 복약 조회와 표 출력
- “먹었어”처럼 대상이 모호한 복약 기록
- 부작용 호소 → PRO-CTCAE 2단계 → 기록 확인
- 음식 후보 선택 → 식사 기록 확인
- “오늘 처음 말한 것”, “그 전에는?” 같은 장기 대화 회상
- 날짜가 섞인 과거 기록 조회
- mutation 확인·취소 및 잘못된 구조화 응답
- `recent_chat_complete=false`인 긴 대화

시간 안건의 결함이 모델 품질 평가를 오염시키지 않도록, fast A/B의 정식
판정은 대화 컨텍스트 조회 기준을 확정한 뒤 수행한다. 그 전의 결과는 latency
탐색값으로만 취급한다.

### 수집 지표

이번에 보강한 로그와 Trace를 기준으로 다음을 비교한다.

- 전체 응답시간, 첫 토큰 시간, 모델 호출 합계, Tool 호출 합계
- 호출별 `model_id`, thinking 유형, reasoning effort
- input/output/cache token과 추정 비용
- Tool 선택 정확도와 불필요한 Tool 호출 수
- 구조화 카드 상태 전이 정확도
- 사용자 발화 회상 정확도
- mutation confirmation 누락 또는 우회 여부
- 공개 응답에 내부 reasoning이 노출되지 않는지
- timeout, 재시도, fallback 횟수

실험을 반복 가능하게 만들려면 향후 로그에 `experiment_id`,
`routing_policy_version`, `routing_reason`, `fallback_count`를 추가하는 안을
별도로 승인한다. 현재 로그만으로도 모델·reasoning·latency 비교는 가능하지만,
정식 A/B 집계에는 실험 식별자가 있는 편이 안전하다.

## 7. 요청별 라우팅을 논의할 때의 후보 경계

아래는 정책 확정이 아니라 실험용 가설이다.

### fast 후보

- 컨텍스트가 완전하고 날짜가 명시된 단순 read-only 조회
- 정해진 카드에 대한 값 정규화·형식화
- Tool 결과를 짧게 설명하는 저위험 답변

### Sonnet 고정 후보

- 메스꺼움 등 부작용·안전 관련 발화
- 대상 약물이나 시점이 모호한 복약 발화
- 쓰기 의도 판단과 확인 카드 생성
- 장기 대화 회상 또는 상충하는 대화 정정
- `recent_chat_complete=false` 또는 Snapshot 일부가 unavailable인 요청
- 첫 모델 시도에서 schema/tool-call 검증이 실패한 요청

설문 다음 문항, 승인 결과 저장처럼 이미 결정적 상태 머신이 처리할 수 있는
단계는 fast LLM으로 보내기보다 LLM 호출 자체를 하지 않는 현재 원칙을
유지한다.

## 8. 라우팅 선택지

| 안 | 구조 | 장점 | 위험 |
|---|---|---|---|
| R0. Sonnet 단일 | 현재처럼 모든 생성 요청을 Sonnet으로 처리 | 단순하고 일관됨 | 지연·비용 최적화 한계 |
| R1. Sonnet reasoning 차등 | 모델은 유지하고 요청별 reasoning만 조정 | 품질 변화 요인을 줄임 | provider 인스턴스와 설정을 요청별로 분리해야 함 |
| R2. 규칙 기반 tier | 요청 특성과 안전 등급으로 fast/Sonnet을 사전 선택 | 설명 가능하고 재현 가능 | 잘못된 사전 분류 위험 |
| R3. fast 우선 + Sonnet fallback | fast 실패·불확실 시 Sonnet 재호출 | 평균 비용 절감 가능 | 최악 지연 증가, 중복 Tool/쓰기 방지 필요 |

검토 순서는 `R0의 M1 실험 → R1 → R2`가 적절하다. R3는 fallback 전에
Tool side effect가 전혀 실행되지 않았음을 보장할 수 있을 때만 검토한다.

## 9. 결정 회의에서 확정할 체크리스트

### 시간

- [ ] `created_at`, 업무 발생 시각, 표시 시각의 정의
- [ ] “오늘”과 날짜 페이지 경계의 clock source
- [ ] AI read view에 필요한 시간·순서 필드
- [ ] 테스트베드와 실서비스가 같은 쿼리를 사용해야 하는지
- [ ] 공개 계약 변경 여부 및 DB 이관 방식
- [ ] 재현 시나리오와 승인 테스트

### 모델

- [ ] 목표 p50/p95 첫 토큰·전체 응답시간
- [ ] 안전·정확도 하한과 허용 가능한 회귀율
- [ ] M0~M3 실험 횟수와 평가 데이터셋
- [ ] reasoning을 전역이 아닌 요청별 설정으로 만들지 여부
- [ ] fast 허용/금지 요청 분류
- [ ] fallback 조건과 side-effect 중복 방지 방식
- [ ] multi-worker에서 일관된 라우팅 설정 배포 방식

## 10. 제안하는 논의 순서

1. 위 2.2의 시간축 불일치를 실제 로그와 DB 행으로 한 번 재현한다.
2. `created_at`의 의미와 목표 시간 모델(T1~T4)을 결정한다.
3. 시간 수정 범위와 테스트를 별도 작업으로 확정한다.
4. 동일 평가 대화로 Sonnet M0/M1을 먼저 비교한다.
5. 필요할 때 Haiku M2/M3를 추가한다.
6. 품질·지연 기준을 통과한 뒤에만 요청별 라우팅(R1/R2)을 설계한다.

이 순서를 따르면 테스트베드의 컨텍스트 누락을 fast 모델의 품질 문제로
오판하거나, 모델 지연을 해결하려다 시간·DB 계약까지 동시에 바꾸는 일을
피할 수 있다.

## 11. 이슈 본문/댓글용 요약

> 테스트베드 시간 문제와 fast 모델 문제는 분리해서 결정한다. 현재 확인된
> 시간 문제는 UI 정렬 자체가 아니라, 시뮬레이션 시각으로 저장된 user
> `created_at`과 실제 응답 시각으로 저장된 assistant `created_at`이 섞인 상태에서
> AI recent-chat 쿼리가 현재 user의 `created_at`을 cutoff로 사용하는 데 있다.
> React는 별도 `display_at`과 `sort_sequence`를 사용하므로 화면은 정상처럼
> 보여도 AI 컨텍스트에서는 이전 assistant가 누락될 수 있다. 실서비스에서는
> 시뮬레이션 시각과 실제 시각이 비슷해 재현되지 않을 수 있으므로 테스트베드
> 전용 SQL 분기를 바로 넣지 않고, 업무 발생 시각·물리 저장 시각·표시 시각의
> 계약을 먼저 확정한다.
>
> fast는 현재 요청별 라우팅이 아니라 프로세스 전역 tier 전환이다. 먼저 현재
> Sonnet adaptive reasoning과 Sonnet reasoning off를 동일 시나리오로 비교하고,
> 그래도 목표 지연을 충족하지 못할 때 Haiku reasoning off/extended thinking을
> 비교한다. 시간 컨텍스트 누락이 해결되기 전 fast 결과는 latency 탐색값으로만
> 사용한다. 정식 라우팅은 품질·안전·p95 기준과 multi-worker 설정 배포 방식을
> 확정한 뒤 별도 작업으로 진행한다.

## 12. 현재 코드 근거 위치

- 테스트베드 시뮬레이션 시각을 채팅 `message_at`으로 만드는 경로:
  `system_app/routes/ui_api.py::_sync_ui_chat`
- user/assistant의 `message_at`을 각각 DB `created_at`에 저장하는 경로:
  `system_app/services/backend_chat_service.py::persist_user_message`,
  `persist_assistant_response`
- AI 응답 `message_at`을 실제 UTC 완료 시각으로 만드는 경로:
  `shared/chat_contracts.py::chat_sync_response`
- assistant의 UI용 `display_at`을 시뮬레이션 시각으로 다시 맞추는 경로:
  `system_app/routes/ui_api.py::_set_display_message_at`
- UI 날짜 조회와 정렬 데이터 생성 경로:
  `system_app/services/ui_view_service.py::chat_history_page`,
  `_bounded_chat_history_rows`, `_chat_message_view`
- React의 확정 메시지 정렬 경로:
  `frontend/src/App.tsx::compareHistoryMessages`
- AI read view의 현재 노출 컬럼:
  `shared/backend_read_contract.py::BACKEND_READ_VIEW_DEFINITIONS`
- AI recent-chat의 `created_at` cutoff 쿼리:
  `agent_app/tools/backend_query.py::BackendQueryTools.validate_chat_message`
- 전역 model tier와 reasoning 설정:
  `agent_app/providers/config.py`, `shared/settings.py`
- 모델별 Bedrock reasoning 필드 구성:
  `agent_app/providers/bedrock.py::reasoning_request_fields`
- 이번 비교에 사용할 요청·모델·Tool 지연 로그:
  `agent_app/routes/chat.py::_log_sync_chat_completed`
