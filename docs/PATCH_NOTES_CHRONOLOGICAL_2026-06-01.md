# Chronological Patch Notes - Medication Reminder AI Agent POC

작성일: 2026-06-01
정렬 기준: 정확한 시각이 아니라, 기능이 쌓여 온 순서 기준

이 문서는 지금까지의 업데이트를 기능별 묶음이 아니라 시간 흐름에 가깝게 재정렬한 기록입니다.

## 1. 기본 POC 구조 구축

처음에는 복약 알림 POC를 `system_app`, `agent_app`, `phr_app` 3개 서버로 나누는 구조를 잡았습니다.

- `system_app`은 복약 계획, 시뮬레이션 시계, 복약 이벤트, 알림, 채팅, 정책 저장을 담당합니다.
- `agent_app`은 AI 판단과 agent 응답 생성을 담당합니다.
- `phr_app`은 환자 PHR key, 복용약 정보, 주의사항 기반 부작용 조회를 담당합니다.
- 한 명의 demo patient 기준으로 전체 흐름을 한 화면에서 실험할 수 있게 구성했습니다.

## 2. 복약 계획 입력과 PHR 등록 흐름 추가

그 다음에는 사용자가 복약 정보를 입력하고 PHR에 등록하는 기본 흐름을 만들었습니다.

- 약물명, 용량, 복약 일정 템플릿, 직접 시간 입력을 지원했습니다.
- 복약 계획을 만들면 해당 날짜의 dose event가 생성되도록 했습니다.
- PHR 등록을 통해 `phr_patient_key`를 발급받는 경로를 추가했습니다.
- PHR 등록 전에는 시뮬레이션을 진행하지 못하도록 막았습니다.
- 복약 정보 변경 시 PHR sync가 다시 필요하다는 상태를 표시하도록 했습니다.

## 3. 시뮬레이션 시계와 복약 타임라인 구현

복약 계획이 생긴 뒤에는 시뮬레이션 시간을 움직이며 이벤트를 확인할 수 있게 했습니다.

- `30분 진행`, `3시간 진행`, 배속 재생, 정지 기능을 추가했습니다.
- 복약 예정 이벤트가 타임라인에 표시됩니다.
- 사용자가 직접 `복용 기록`을 눌러 dose event를 `taken`으로 바꿀 수 있게 했습니다.
- 날짜별 타임라인과 캘린더 UI를 추가했습니다.

## 4. 복약 알림 생성 경로 추가

이후 복약 예정 시간에 맞춰 알림을 생성하는 기본 알림 시스템을 붙였습니다.

- 정해진 복약 시간에 `medication_alert`가 생성됩니다.
- 알림 feed API와 notification history를 구성했습니다.
- 읽은 알림, 스킵한 알림, 오늘 알림 이력을 화면에서 확인할 수 있게 했습니다.
- 알림 popup stack과 알림 상세 조회 API를 정리했습니다.

## 5. 미복용 판단과 AI 대화 알림 추가

복약 시간이 지나도 복용 기록이 없으면 미복용으로 판단하고 AI 대화를 요청하는 경로를 만들었습니다.

- grace period 이후 dose event를 missed로 판단합니다.
- 미복용 시 `conversation_alert`를 생성합니다.
- agent job을 만들어 agent server가 미복용 상황에 대한 메시지를 생성하도록 했습니다.
- 실패 시 `agent_error` 알림을 만들고, 오류 알림에서 재시도할 수 있게 했습니다.

## 6. 초기 agent 경로와 정책 제안 흐름 구성

처음 agent는 복약 패턴과 미복용 상황을 분석하고 정책 변경을 제안하는 역할을 했습니다.

- daily pattern 분석에서 반복 미복용 slot을 찾는 흐름을 만들었습니다.
- `apply_notification_policy` 형태의 정책 변경 후보를 agent가 만들 수 있게 했습니다.
- 정책 후보를 `ReminderPolicy` DB override로 반영하는 기본 경로를 만들었습니다.
- 정책 workbook과 boundary를 통해 허용 가능한 알림 횟수, 간격, 기간 등을 검증했습니다.

## 7. 정책 confirmation UX 도입

이후 정책 변경을 agent가 바로 적용하는 방식이 위험하다고 보고, confirmation 중심 구조로 바꿨습니다.

- agent가 정책 후보를 만들면 바로 적용하지 않고 confirmation 알림을 생성합니다.
- chat 안에 현재 정책과 변경 후 정책 비교 테이블을 표시합니다.
- 사용자가 `늘리기`, `줄이기`, `현행 유지하기` 등을 선택한 뒤에만 정책이 반영됩니다.
- 여러 slot 정책이 동시에 들어와도 slot별로 후보와 audit이 남도록 정리했습니다.

## 8. 시스템 정책 변경도 confirmation으로 전환

notification policy뿐 아니라 system policy도 직접 적용하지 않도록 정리했습니다.

- `daily_pattern_conversation_time` 변경 요청을 `apply_system_policy` tool 후보로 처리하게 했습니다.
- agent가 system policy 후보를 만들면 system app이 confirmation card를 생성합니다.
- 사용자가 `적용하기`를 선택해야 `SystemPolicyOverride`가 생성됩니다.
- `현행 유지`를 선택하면 override가 생성되지 않습니다.
- 직접 적용 endpoint는 내부 토큰이 있어도 `410`으로 거부하도록 바꿨습니다.

## 9. 채팅 UX 정리

정책과 미복용 대화가 늘어나면서 chat UX도 함께 정리했습니다.

- Chat 버튼이나 `채팅 열기`를 누르면 채팅 내용이 새로고침되도록 했습니다.
- agent 대화 입력에서 `Enter`는 전송, `Alt+Enter`는 줄바꿈으로 처리했습니다.
- policy confirmation 응답 JSON이 사용자 채팅에 그대로 보이는 문제를 숨김 처리했습니다.
- pending agent response 상태가 chat 안에서 보이도록 했습니다.

## 10. 멀티턴 회상 문제 확인

사용자가 “나 아까 문제가 있다고 했었나?”, “아까 뭔말했더라”라고 물었을 때 agent가 `요청을 확인했습니다.`라고 답하는 문제가 드러났습니다.

- 원인은 recent chat은 전달되지만 일반 회상 intent를 자연어로 처리하는 경로가 약했기 때문입니다.
- 처음에는 deterministic recall helper를 고려했습니다.
- 이후 deterministic 회상 전용 경로를 없애고, 멀티턴 agent가 일반 대화와 회상을 자연어로 처리하는 방향으로 수정했습니다.
- tool이 필요 없는 대화는 일반 응답을 반환하도록 agent 역할을 확장했습니다.

## 11. 부작용 질문과 PHR 조회 흐름 정리

그 다음에는 “속이 메스꺼운데 약 때문일까?” 같은 질문을 부작용 tool 경로로 처리하도록 정리했습니다.

- agent가 `lookup_side_effect_info` tool을 선택해 PHR 주의사항을 먼저 조회합니다.
- PHR은 `phr_patient_key` 기준으로 복용 중인 품목의 주의사항을 확인합니다.
- 관련 가능성이 있으면 `AE_pro_ctcae`가 이어서 실행됩니다.
- 사용자에게는 “현재 복용약 주의사항과 관련 가능성이 있어 더 정확히 기록하기 위해 PRO-CTCAE 문항을 준비했다”는 뉘앙스가 포함되도록 응답 요약을 개선했습니다.

## 12. PRO-CTCAE 문항 응답 기록 추가

부작용 질문 이후 PRO-CTCAE 문항을 chat 안에서 답할 수 있게 했습니다.

- PRO-CTCAE workbook에서 symptom term과 한국어 symptom명을 매칭합니다.
- exact, similarity, embedding 기반 매칭을 지원합니다.
- 매칭된 문항과 응답 선택지를 chat action card로 표시합니다.
- 사용자가 문항별 응답을 선택하면 metadata에 기록됩니다.
- 모든 문항 응답이 완료되면 부작용 기록 완료로 간주합니다.

## 13. 부작용 기록 후 안전 확인 플로우 추가

부작용이 기록된 환자에게 계속 복약 알림을 보내는 것이 적절한지에 대한 논의 후, deterministic safety prompt를 추가했습니다.

- PRO-CTCAE 응답 완료 후 LLM과 무관하게 고정 안내를 표시합니다.
- 안내에는 병원 내원 권고와 의료진 상담 권고가 포함됩니다.
- 사용자는 `알림 유지하기` 또는 `알림 모두 끄기` 중 하나를 선택합니다.
- 같은 source chat message에 대해 safety prompt가 중복 생성되지 않도록 막았습니다.
- 이미 처리된 prompt에 재응답해도 중복 처리되지 않게 했습니다.

## 14. 알림 전체 끄기 기능 확장

처음에는 부작용 이후 복약 알림을 끌지 말지를 묻는 수준이었고, 이후 범위를 명확히 넓혔습니다.

- `알림 모두 끄기`는 복약 시간 알림만이 아니라 아래를 모두 중지합니다.
- 복약 시간 알림
- 추가 복약 알림
- 미복용 AI 대화 알림
- daily pattern 정책 제안
- 수동 `오늘 패턴 분석`
- 상태는 `SystemPolicyOverride`의 `side_effect_reminder_suppressed`로 저장합니다.

## 15. 알림 전체 끄기/켜기 UI 추가

알림 전체 끄기 상태를 safety prompt에서만 바꿀 수 있으면 불편하므로, 활성 정책 패널에 별도 토글을 추가했습니다.

- 활성 알림 정책 패널에 `복약/AI 알림 전체 상태` 카드를 추가했습니다.
- 현재 상태가 켜짐/꺼짐으로 표시됩니다.
- `알림 전체 끄기`, `알림 전체 켜기` 버튼을 제공합니다.
- 꺼짐 상태에서는 `오늘 패턴 분석` 버튼이 비활성화됩니다.

## 16. Daily Pattern rolling window 적용

daily pattern은 처음에는 하루 단위 또는 7일 누적 방식이 혼재되어 있었고, 이후 rolling window로 정리했습니다.

- 7일이 지나기 전에도 매일 daily pattern 확인은 실행될 수 있습니다.
- 2일치 데이터만 있으면 2일치를 모두 봅니다.
- 10일치 데이터가 있으면 최근 7일만 봅니다.
- payload에 `window_start_date`, `window_end_date`, `window_days`, `observed_day_count`를 포함했습니다.
- daily pattern cursor는 같은 날짜를 반복 처리하지 않도록 진행됩니다.

## 17. Daily Pattern 정책 제안 조건 보수화

rule-based fallback이 1회 미복용만으로 정책 후보를 만드는 문제가 있어 조건을 강화했습니다.

- `observed_day_count < 2`이면 정책 후보를 만들지 않습니다.
- slot별 `missed_count >= 2`이면 후보를 만들 수 있습니다.
- 또는 `scheduled_count >= 3 and miss_rate >= 0.5`이면 후보를 만들 수 있습니다.
- 이 변경은 LLM 경로가 아니라 rule-based/local fallback의 과한 제안을 줄이기 위한 것입니다.

## 18. Tool Calling 우선 구조로 리팩터링

부작용 감지, 복약 완료, 정책 변경이 keyword rule로 먼저 처리되는 부분을 줄이고 agent/tool 판단을 우선하도록 정리했습니다.

- `RuleBasedProvider`의 부작용 keyword 기반 tool_call 생성을 제거했습니다.
- `RuleBasedProvider`의 복약 완료 keyword 기반 `mark_dose_taken` 생성을 제거했습니다.
- system app의 미실행 tool_call fallback 직접 적용을 제거했습니다.
- 복약 완료는 실제 `mark_dose_taken` tool result가 있을 때만 DB에 반영됩니다.
- 정책 변경은 실제 tool 후보가 있어도 confirmation 전에는 적용되지 않습니다.

## 19. Direct apply API 방어

정책 직접 적용 경로가 아직 API surface에 남아 있어 안전장치를 추가했습니다.

- `/api/agent/policies/apply`는 `410 policy_apply_requires_confirmation`을 반환합니다.
- `/api/agent/system-policies/apply`는 `410 system_policy_apply_requires_confirmation`을 반환합니다.
- agent tool executor에서도 policy direct HTTP call 경로를 제거했습니다.
- policy tool은 모두 deferred result로만 처리됩니다.

## 20. Reset 동작 보정

알림 전체 끄기 상태가 reset 이후에도 남는 문제가 발견되어 수정했습니다.

- `reset_simulation_state`가 `SystemPolicyOverride`도 삭제하도록 바꿨습니다.
- 이제 전체 리셋 후에는 `side_effect_reminder_suppressed` 상태도 초기화됩니다.
- 이 동작에 대한 pytest 회귀 테스트를 추가했습니다.

## 21. Agent legacy 제거

새 `agent_app` 경로가 안정화된 뒤 legacy 경계를 정리했습니다.

- `agent_app_old_legacy` 폴더를 삭제했습니다.
- legacy DB fixture와 legacy-only tests를 제거했습니다.
- 새 agent app이 legacy package를 import하지 않는지 확인하는 테스트를 유지했습니다.
- 문서도 LangGraph native agent 기준으로 갱신했습니다.

## 22. 실제 LLM Playwright smoke test 추가

실제 Bedrock LLM 경로가 tool을 제대로 선택하는지 확인하기 위해 Playwright smoke test를 추가했습니다.

- `tools/playwright_real_llm_tool_paths.js`
- 검증 시나리오:
  - 멀티턴 정책 요청 -> `apply_notification_policy` -> confirmation
  - 부작용 질문 -> `lookup_side_effect_info` -> `AE_pro_ctcae`
  - 복약 완료 보고 -> `mark_dose_taken`
- 최근 실행 결과는 `3 passed, 0 failed`였습니다.

## 23. 실제 LLM regression matrix 추가

smoke test가 3개 시나리오만 커버한다는 지적 이후, 더 넓은 matrix를 추가했습니다.

- `tools/playwright_real_llm_regression_matrix.js`
- 검증 시나리오:
  - 일반 멀티턴 회상
  - 부작용 기록 후 알림 전체 끄기/켜기
  - 시스템 정책 변경 confirmation
  - direct apply endpoint 410 방어
  - 알림 전체 꺼짐 상태에서 자동 복약/미복용 알림 차단
  - 알림 전체 꺼짐 상태에서 수동 daily pattern 분석 차단
- 초기 실행에서 테스트 assertion 문제와 prompt 유도 문제가 드러났습니다.
- assertion은 locator 기반 disabled check로 수정했습니다.
- system policy request 문구는 `daily_pattern_conversation_time`과 `apply_system_policy`를 명확히 포함하도록 조정했습니다.
- 최종 결과는 `6 passed, 0 failed`입니다.

## 24. Pytest 회귀 테스트 확장

기능 변경마다 pytest도 추가/수정했습니다.

- 정책 직접 적용 endpoint rejection
- side effect safety prompt 중복 방지
- 알림 유지/전체 끄기 선택 처리
- suppression 상태에서 manual daily pattern 차단
- reset 시 suppression override 삭제
- rule-based daily pattern fallback 조건
- LangGraph native agent의 legacy import 금지
- policy confirmation UI와 hidden JSON reply 처리

최근 전체 결과:

- `146 passed, 2 skipped`

## 25. 문서화 업데이트

마지막으로 현재 구조와 테스트 방법을 문서화했습니다.

- `AGENT_APP_FEATURES.md`
  - agent app 기능, tool calling, output contract 정리
- `AGENT_SYSTEM_MAP.md`
  - system/agent/tool 흐름과 confirmation 구조 정리
- `LLM_PLAYWRIGHT_TEST_PLAN.md`
  - 실제 LLM regression matrix, demo video 실행법, 산출물 정리
- `PATCH_NOTES_CHRONOLOGICAL_2026-06-01.md`
  - 시간 흐름 기준 요약

## 최종 요약

처음에는 복약 알림과 AI 대화가 가능한 POC에서 출발했습니다. 이후 미복용 AI 알림, 정책 제안, PHR 부작용 조회, PRO-CTCAE 기록, 정책 confirmation, 부작용 이후 알림 안전 확인, rolling daily pattern, 실제 LLM Playwright 검증까지 순서대로 쌓였습니다.

가장 큰 방향 전환은 세 가지입니다.

- 정책은 agent가 직접 적용하지 않고 항상 사용자 confirmation을 거친다.
- 의료/복약 관련 판단은 keyword rule보다 LLM + tool execution 결과를 우선한다.
- 부작용이 기록된 뒤에는 복약 알림 지속 여부를 deterministic safety UX로 반드시 확인한다.
