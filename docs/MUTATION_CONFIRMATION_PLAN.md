# 대화 기반 DB 변경 공통 확인 계약 진행 계획

> 마지막 갱신: 2026-07-16

## 목표

사용자 대화에서 시작된 도메인 DB 생성, 수정, 삭제는 실제 변경 전에 공통 확인 카드를 거친다. 조회와 운영 기록은 제외하며, 확인 요청과 승인·취소 후 최종 안내는 항상 MultiturnChatAgent가 마무리한다.

## 현재 단계

**1단계: 공통 기반과 복약 완료 확인 흐름을 구현하고 수동 검증하는 중**

- 자동 구현과 1차 회귀 테스트는 완료했다.
- 사용자 수동 테스트에서 과거 승인 결과가 새 요청에 재사용되어, DB는 변경되지 않았는데 성공으로 답하는 회귀를 발견했다.
- 원인은 재사용되는 알림 ID를 confirmation idempotency 기준으로 사용하고, `skipped` mutation 결과를 LLM이 성공으로 해석할 수 있었던 점이다.
- 현재 trace와 대상 snapshot 기준으로 새 confirmation을 만들고, 미실행 mutation을 성공 답변으로 합성하지 못하게 수정했다.
- 관련 회귀 테스트와 전체 테스트는 통과했으며 사용자 수동 재검증을 기다리고 있다.
- 승인 후 mutation은 적용됐지만 Supervisor 최종 합성 호출이 외부 60초 timeout에 걸려 전체 실패처럼 표시되는 부분 성공 회귀도 수정했다.
- 근본 원인은 확정 결과를 일반 Supervisor tool loop에 다시 넣어 MedicationAgent 재위임과 여러 LLM 호출이 발생한 구조였다.
- 최초에는 `mutation_resolution`을 도구 없는 MultiturnChatAgent 전용 finalization 노드로 분리해 단일 작업의 LLM 호출을 한 번으로 줄였다.
- 이후 복합 요청에서 첫 mutation 승인 후 남은 작업이 유실되는 회귀를 확인했다. 예: 점심 복약 완료 승인 후 아침 복약 정정 요청이 이어지지 않았다.
- 현재는 resolved action을 다시 실행하지 않으면서 남은 작업만 전문 Agent에 위임할 수 있는 반복 StateGraph 경로로 보완했다.
- 단일 작업은 추가 Tool 호출 없이 한 번에 종료하고, 복합 작업만 필요한 만큼 이어서 처리한다.
- 전용 finalization 도입 당시 동일 confirmation 실측은 총 34.8초에서 3.7초로 줄었고, 보완 후 단일 작업 latency는 수동 재측정할 예정이다.
- pending 확인 카드에 사용자가 버튼 대신 “응”, “아니”, “진행해줘”, “취소해줘”처럼 채팅으로 답해도 처리할 수 있는 Supervisor 전용 StateGraph 분기를 구현했다.
- 카드 버튼은 LLM을 거치지 않는 기존 deterministic route를 유지하고, 자연어 답변에서만 Supervisor가 `confirm / cancel / unclear / new_request` 의미를 판정한다.
- LLM은 confirmation ID나 실행 인자를 만들 수 없으며, 서버가 알림 metadata에 저장한 pending confirmation만 기존 resolution worker로 실행한다.
- 자연어 확인 응답 구현 후 전체 테스트는 383 passed, 3 skipped이며 사용자 수동 검증을 기다리고 있다.

## 완료한 작업

- [x] `MutationConfirmation` 테이블, 상태 모델, 마이그레이션
- [x] 중앙 `ConfirmationActionRegistry`와 Tool metadata의 확인 정책
- [x] `confirmation_required` Tool 결과와 MCP/structured payload 보존
- [x] 첫 mutation에서 Tool loop 중단 및 나머지 ToolMessage pairing
- [x] Supervisor 전용 확인 질문 finalization
- [x] 확인 카드, 승인·취소 route, 비동기 worker
- [x] 승인된 복약 완료 mutation의 ToolRuntime 재실행
- [x] snapshot 재검증, stale 처리, 중복 승인 방지, 2분 lease 복구
- [x] 복약 도메인 변경과 confirmation 상태의 단일 transaction 처리
- [x] 승인·취소 후 MultiturnChatAgent continuation 및 최종 답변
- [x] 복합 요청에서 승인된 작업을 제외하고 남은 작업만 순차적으로 재개
- [x] MissedDoseAgent의 증상 없는 PRO-CTCAE 직접 호출과 원시 JSON 응답 회귀 수정
- [x] pending 확인 카드의 자연어 승인·취소·모호한 답변·새 요청 분기
- [x] 자연어 승인·취소를 기존 deterministic mutation resolution worker에 연결
- [x] new_request async continuation의 확인 판정 보존과 동시 응답 경합 방지

## 현재 수정 체크리스트

- [x] 최신 DB 상태와 Agent trace를 대조해 false success 원인 확인
- [x] confirmation idempotency를 실행 trace와 현재 snapshot 기준으로 변경
- [x] 동일 snapshot의 pending proposal은 기존 confirmation으로 중복 방지
- [x] confirmation 대상 mutation의 `skipped/error` 결과를 정상 성공으로 합성하지 못하게 차단
- [x] 승인 후 Supervisor 호출 timeout을 LLM timeout보다 길게 분리
- [x] finalization 실패 시 이미 적용된 mutation과 카드 상태를 `applied`로 보존
- [x] 승인 결과를 일반 신규 요청 routing과 구분하는 Supervisor 전용 mutation-resolution 경로 구현
- [x] resolved action은 재위임하지 않고, 남은 작업만 좁힌 delegation payload로 전달
- [x] 단일 작업은 Tool 0회, 복합 작업은 반복 StateGraph에서 다음 전문 Agent 작업 수행
- [x] `node_timings_ms.mutation_resolution_llm`과 node 완료 로그 추가
- [x] 복합 복약 요청 회귀 테스트: 점심 변경 1회 실행 후 아침 정정 지원 여부를 순차 확인
- [x] Agent graph 회귀 테스트 실행: 52 passed
- [x] 관련 단위·통합 회귀 테스트 실행: 69 passed
- [x] 자연어 confirmation 관련 Agent·callback·route 회귀 테스트 실행
- [x] 전체 pytest 실행: 383 passed, 3 skipped
- [ ] 사용자 수동 확인: 질문 카드 표시, 승인 전 미변경, 승인 후 반영
- [x] 1단계 기준 구현 커밋: `58b5a87`
- [ ] 자연어 확인 응답 수동 확인 후 별도 커밋

## 이후 단계

1. **영양 선호도**
   - `upsert_nutrition_preference_fact` 확인 계약 적용
   - 알레르기 저장 승인 후 추천 요청 continuation 검증
2. **영양 CRUD**
   - 식사·음식 생성, 수정, 삭제 확인 계약 적용
   - `/chat/food-confirm`을 proposal 완성 단계로 변경
   - 파생 영양 요약 갱신을 동일 transaction에 포함
3. **부작용 기록**
   - assessment의 숨은 자동 기록 제거
   - 부작용 기록 mutation Tool 추가
   - 설문 완료와 기록 확인 순서 정리
4. **정책·안전 설정**
   - 정책과 부작용 알림 안전 설정을 공통 server_action 확인 흐름으로 통합
5. **전체 감사**
   - 대화 기원 mutation registry 누락 검사
   - read Tool의 숨은 write 검사
   - 직접 DB 변경 우회 경로를 테스트로 차단

## 1단계 수동 합격 기준

- “아침약 먹었어” 같은 발화 후 바로 DB가 바뀌지 않는다.
- Supervisor가 복약 시간, 약, 변경 전후 상태를 담은 확인 카드를 보여준다.
- 승인 전 dose event는 기존 상태를 유지한다.
- 취소하면 DB가 바뀌지 않고 Supervisor가 취소 결과를 안내한다.
- 승인하면 한 번만 기록되고 Supervisor가 실제 결과를 확인한 뒤 안내한다.
- 한 발화에 작업이 여러 개면 첫 확인 처리 후 이미 완료된 작업은 반복하지 않고 남은 작업을 이어간다.
- 남은 작업이 현재 지원되지 않으면 전문 Agent 확인 후 Supervisor가 제한을 정확히 안내한다.
- 같은 버튼을 중복 클릭해도 mutation이 다시 실행되지 않는다.
- 카드가 pending일 때 “응” 또는 “진행해줘”라고 채팅하면 버튼 승인과 동일한 작업이 한 번만 실행된다.
- 카드가 pending일 때 “아니” 또는 “취소해줘”라고 채팅하면 DB를 바꾸지 않고 Supervisor가 취소 결과를 안내한다.
- 모호한 채팅 답변에는 카드가 pending으로 남고 Supervisor가 적용 또는 취소 의사를 다시 묻는다.
- 카드 답변이 아닌 새 요청은 이전 카드만 superseded 처리한 뒤 기존 Supervisor 흐름으로 처리한다.
- 과거 승인 이력이 있어도 현재 DB가 미복용이면 새 확인을 요구한다.
- Tool이 실행되지 않았거나 실패하면 기록 성공이라고 답하지 않는다.

## 작업 원칙

- 단계별 자동 테스트와 사용자 수동 확인을 거친 뒤, 사용자가 요청할 때만 커밋한다.
- 새 domain-answer fallback과 rule-based 자연어 routing을 추가하지 않는다.
- 모든 mutation 실행은 permission-aware ToolRuntime 또는 등록된 deterministic server action을 통한다.
- 변경 진행 중 이 문서의 현재 단계, 발견된 회귀, 테스트 결과, 다음 작업을 계속 갱신한다.
- 이번 작업과 무관한 `.gitignore`, 공개 실행 스크립트, 영상 도구 파일은 수정 범위에서 제외한다.

## 변경 기록

| 날짜 | 내용 | 상태 |
|---|---|---|
| 2026-07-15 | 공통 confirmation 기반 및 복약 완료 1차 구현 | 수동 검증 중 |
| 2026-07-15 | MissedDoseAgent의 잘못된 PRO-CTCAE 호출과 원시 JSON 응답 수정 | 자동 테스트 완료 |
| 2026-07-15 | 과거 승인 결과 재사용으로 인한 복약 false success 원인 확인 및 수정 | 자동 테스트 완료, 수동 검증 대기 |
| 2026-07-16 | 승인 결과의 일반 tool loop 재진입과 MedicationAgent 재위임 제거 | 실측 34.8초 → 3.7초, 자동 테스트 완료 |
| 2026-07-16 | finalization 실패 시 applied mutation과 카드 상태 보존 | 자동 테스트 완료, 수동 검증 대기 |
| 2026-07-16 | 복합 요청 승인 후 남은 작업 유실 수정 | Agent graph 47 passed, 수동 검증 대기 |
| 2026-07-16 | pending 확인 카드 자연어 승인·취소와 새 요청 분기 구현 | 전체 383 passed, 3 skipped, 수동 검증 대기 |
