# LLM-Based Playwright Test Plan

이 계획은 fake agent가 아니라 실제 `agent_app` + Bedrock provider를 붙여 브라우저에서 검증할 시나리오를 정의합니다. 목적은 LLM 문장 품질보다 “LLM이 올바른 tool을 선택하고, system_app이 안전하게 반영하는지”를 확인하는 것입니다.

## 공통 원칙

- 각 시나리오는 새 임시 SQLite DB와 고유 포트를 사용합니다.
- LLM 응답 문구 전체 일치는 피하고, 구조화 결과와 DB 상태를 우선 검증합니다.
- 정책 tool은 직접 적용이 아니라 confirmation 생성까지를 LLM 경로 성공으로 봅니다.
- 복약 완료는 `mark_dose_taken` tool result가 있을 때만 DB 상태 변경을 성공으로 봅니다.
- 부작용은 `lookup_side_effect_info` 후 positive 결과에서 `AE_pro_ctcae`와 deterministic 안전 안내가 이어지는지 봅니다.

## Scenario Matrix

| ID | 목적 | 입력 흐름 | 핵심 검증 |
| --- | --- | --- | --- |
| LLM-01 | 일반 멀티턴 회상 | 부작용 질문, PRO-CTCAE 응답 후 “아까 뭔말했더라” | tool 없이도 최근 대화 맥락을 언급하고 fallback 문구로 빠지지 않음 |
| LLM-02 | 부작용 tool chain | “속이 메스꺼운데 약 때문일까?” | `lookup_side_effect_info` 실행, positive면 `AE_pro_ctcae` 생성, 안전 안내 선택지 노출 |
| LLM-03 | 부작용 후 알림 끄기 | LLM-02 후 “알림 모두 끄기” 선택 | `side_effect_reminder_suppressed=true`, 복약 알림/미복용 AI/daily pattern/수동 분석 중지 |
| LLM-04 | 알림 다시 켜기 UI | LLM-03 후 활성 정책 패널의 “알림 전체 켜기” | `side_effect_reminder_suppressed=false`, 수동 분석 버튼 재활성화 |
| LLM-05 | 복약 완료 tool | 미복용 알림 후 “아침 약 먹었어 기록해줘” | LLM이 `mark_dose_taken` 선택, callback 성공 후 dose status가 `taken` |
| LLM-06 | 알림 정책 후보 | “아침 알림을 2번 10분 간격으로 늘려줘” | LLM이 `apply_notification_policy` 선택, 직접 적용 없이 policy confirmation 생성 |
| LLM-07 | 시스템 정책 후보 | “일일 패턴 확인을 아침 7시로 바꿔줘” | LLM이 `apply_system_policy` 선택, 직접 적용 없이 system policy confirmation 생성 |
| LLM-08 | rolling daily pattern | 2일, 7일, 10일 복약 이력 구성 | payload는 최근 최대 7일을 반영하고, 충분한 패턴일 때만 정책 confirmation 생성 |
| LLM-09 | 전체 알림 꺼짐 guard | suppress 상태에서 clock advance와 수동 분석 | medication alert, missed-dose agent job, daily pattern job, manual analysis call 미생성 |
| LLM-10 | 직접 apply endpoint 방어 | 내부 토큰 포함 direct apply POST | `/api/agent/policies/apply`, `/api/agent/system-policies/apply` 모두 410 |

## Assertion Strategy

- UI: 버튼/선택지/채팅 카테고리 존재 여부만 확인합니다.
- DB: `Notification`, `ChatMessage`, `DoseEvent`, `ReminderPolicy`, `SystemPolicyOverride`, `AgentDecisionAudit`, `AgentJob`를 직접 확인합니다.
- Logs: 실패 시 각 서비스 stdout/stderr, agent trace id, Playwright screenshot을 산출물 폴더에 저장합니다.
- LLM variability: 같은 의미의 문구는 정규식/부분 문자열로 검증하고, tool name과 DB side effect를 기준으로 pass/fail을 결정합니다.

## 실행 구조

- `tools/playwright_adherence_pattern_matrix.js`는 deterministic `rule_based` 환경에서 미복용 채팅 A/B/C/D/E 패턴, 시간대별 streak, clinician escalation 숨김, suppression guard, `/chat/system` 멀티턴 경로를 검증합니다.
- `tools/playwright_complex_adherence_scenarios.js`는 3개의 복합 시나리오를 검증합니다. C 우선순위와 suppression guard, D 의료진 escalation 중복 방지, E 혼합 slot 저조 패턴과 `/chat/system` 답변 경로를 한 흐름 안에서 확인합니다.
- `tools/playwright_persona_policy_lab.js`는 metadata 기반 persona reaction simulator로 7일 알림 tone 전환을 검증합니다. 10개 persona 반응군이 `tone_policy.tone_key`에 따라 다르게 반응하고, 실패 tone 전환, 성공 tone 재사용, 대화 우선 전략, opt-out guard, 무반응 탐색 권고를 확인한 뒤 persona별 추천 tone, 회피 tone, 전략을 `recommendations.json`과 CSV로 산출합니다.
- `tools/playwright_persona_policy_challenge_lab.js`는 통과 목적이 아니라 known gap을 드러내는 challenge lab입니다. 현재는 다른 페르소나의 성공 이력이 다음 페르소나의 tone 선택을 오염시키는 문제를 expected failure로 기록합니다.
- `tools/run_playwright_isolated.ps1`은 8100/8101/8102 포트와 `C:\tmp\da_drug_playwright\<run-id>` 임시 DB로 `rule_based` 격리 환경을 띄운 뒤 adherence matrix, complex adherence scenarios, persona policy lab, bug hunt, weekly pattern matrix를 순서대로 실행합니다.
- 기존 `tools/playwright_real_llm_tool_paths.js`는 LLM-02, LLM-05, LLM-06의 기본형을 담당합니다.
- `tools/playwright_real_llm_regression_matrix.js`는 LLM-01, LLM-03, LLM-04, LLM-07, LLM-09, LLM-10 중심의 회귀 matrix를 담당합니다.
- `tools/playwright_real_llm_demo_video.js`는 실제 시연에 가까운 end-to-end 흐름을 녹화합니다.
- `tools/run_real_llm_demo_video.ps1`은 임시 SQLite DB와 고유 포트로 `phr_app`, `agent_app`, `system_app`을 띄운 뒤 demo video 스크립트를 실행하고 종료합니다.
- 전체 real LLM 테스트는 비용과 시간 때문에 기본 pytest에 묶지 않고, 명시적 명령으로 실행합니다.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\run_playwright_isolated.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\run_playwright_isolated.ps1 -SkipAdherenceMatrix -SkipComplexScenarios -SkipBugHunt -SkipWeeklyMatrix
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\run_playwright_isolated.ps1 -SkipAdherenceMatrix -SkipComplexScenarios -SkipPersonaPolicyLab -SkipBugHunt -SkipWeeklyMatrix -RunPersonaChallengeLab
node tools/playwright_real_llm_tool_paths.js
node tools/playwright_real_llm_regression_matrix.js
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\run_real_llm_demo_video.ps1
```

## 산출물

- 실행 결과는 `outputs/playwright/<run-id>/` 아래에 저장합니다.
- `summary.json`: pass/fail, 주요 evidence, 실행 대상 URL
- `report.md`: 사람이 읽기 쉬운 실행 리포트
- `recommendations.json`, `persona_policy_recommendations.csv`: persona policy lab의 persona별 추천 tone/전략 산출물
- `videos/*.webm`: regression matrix 또는 demo video 녹화 파일
- 실패 시 `*-failed.png` 또는 `failed-demo-state.png`로 마지막 화면을 남깁니다.

## 권장 실행 주기

- 작은 backend 변경: pytest만 실행
- agent prompt/tool contract 변경: LLM-02, LLM-05, LLM-06, LLM-07 실행
- 알림/정책/suppression 변경: LLM-03, LLM-04, LLM-08, LLM-09, LLM-10 실행
- 시연 준비: demo video script 실행
- 릴리즈 전: 전체 LLM matrix와 demo video script 실행
