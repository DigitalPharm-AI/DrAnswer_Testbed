# Agent System Map

## 전체 구조

```mermaid
flowchart LR
  CLIENT["React Client"]
  BACKEND["Backend Server<br/>채팅 · 복약 · 식사 · 알림 정책"]
  BACKEND_DB[("Backend DB<br/>PHR 원천 · 업무 데이터")]
  AGENT["AI Server<br/>LangGraph · Tool Runtime"]
  AI_DB[("AI Internal DB<br/>Trace · 멱등성 · Tool 상태 · Queue")]
  SNAPSHOT["Patient Context Loader<br/>현재 Snapshot"]
  LLM["LLM<br/>응답 생성 · Tool 선택"]
  TOOL["고정 Tool<br/>상세 Read · 평가 · 쓰기 요청"]

  CLIENT -->|"사용자 입력"| BACKEND
  BACKEND -->|"업무 데이터 RW"| BACKEND_DB
  BACKEND -->|"POST /agent/sync/chat"| AGENT
  AGENT -->|"승인 View SELECT-only"| SNAPSHOT
  BACKEND_DB --> SNAPSHOT
  SNAPSHOT -->|"LLM-safe Snapshot"| LLM
  SNAPSHOT -->|"신뢰 식별자 · 원본 Snapshot"| TOOL
  LLM -->|"최소 의미 인자"| TOOL
  TOOL -->|"과거 상세 Query"| BACKEND_DB
  TOOL -->|"동기 쓰기 API"| BACKEND
  AGENT --> AI_DB
  LLM --> AGENT
  AGENT -->|"최종 응답"| BACKEND

  LLM -. "금지: Raw SQL · patient_id 생성 · DB 쓰기" .-> BACKEND_DB
  AGENT -. "금지: Backend DB 직접 쓰기" .-> BACKEND_DB
```

Backend DB가 환자정보와 업무 데이터의 원장입니다. AI Internal DB에는 환자
업무 원장을 복제하지 않고, AI 실행에 필요한 Trace와 상태만 저장합니다.

현재 v1.3 채팅은 Backend Patient Snapshot을 사용합니다. 과거 테스트베드의
별도 PHR 서비스·DB·환자키 발급 흐름은 제거되었으며 채팅 선행조건이 아닙니다.

## 동기 채팅 입력

```mermaid
flowchart TD
  CHAT["multiturn_chat<br/>환자 채팅"]
  VERIFY["Backend 메시지 · 환자 범위 검증"]
  LOAD["현재 Snapshot 구성"]
  SUPERVISOR["Multiturn Chat Supervisor"]
  MED["Medication Agent"]
  NUTRITION["Nutrition Agent"]
  POLICY["Policy Tool"]
  FINAL["text · selection_box · input_box"]

  CHAT --> VERIFY --> LOAD --> SUPERVISOR
  SUPERVISOR --> MED
  SUPERVISOR --> NUTRITION
  SUPERVISOR --> POLICY
  MED --> FINAL
  NUTRITION --> FINAL
  POLICY --> FINAL
  SUPERVISOR --> FINAL
```

채팅에 필요한 Tool continuation은 같은 `/agent/sync/chat` 요청, 내부 Trace,
전체 timeout 안에서 최종 응답까지 완료합니다. 중간 안내문이나 비동기 채팅
결과를 성공 응답으로 반환하지 않습니다.

## 현재 Snapshot과 상세 Read

```mermaid
flowchart LR
  DB[("Backend DB")]
  CURRENT["항상 읽는 현재 Snapshot"]
  DETAIL["필요할 때만 읽는 상세 Tool"]
  REFERENCE["환자정보가 아닌 기준정보"]

  DB --> CURRENT
  DB --> DETAIL

  CURRENT --> PROFILE["최소 프로필"]
  CURRENT --> CONDITION["활성 질환 · 치료"]
  CURRENT --> MEDICATION["오늘 복약 시간표 · 복약 이력"]
  CURRENT --> MEAL["오늘 식사"]
  CURRENT --> CURRENT_POLICY["현재 알림 정책"]

  DETAIL --> OLD_MED["과거 복약 이력"]
  DETAIL --> SIDE_HISTORY["전체 부작용 이력"]
  DETAIL --> OLD_MEAL["과거 식사 기록"]

  REFERENCE --> DRUG["의약품별 알려진 부작용"]
  REFERENCE --> FOOD["음식 영양 기준정보"]
  REFERENCE --> ONTOLOGY["온톨로지 · 개념 관계"]
```

`available`, `not_found`, `not_supported`, `unavailable` 상태를 구분하며,
일부 정보가 없다는 이유로 전체 PHR이 미등록됐다고 답하지 않습니다.

## 부작용 평가와 저장

```mermaid
flowchart TD
  INPUT["환자 증상 표현"]
  SELECT["Medication Agent가 평가 Tool 선택"]
  MODEL_ARGS["LLM 인자<br/>symptom_text · 증상 시점 표현"]
  TRUSTED["AI Server 주입<br/>patient_id · 복약 Snapshot · message/record/version"]
  MATCH["Tool Runtime<br/>복약 후보 매칭 · 의약품 기준정보 평가"]
  AMBIGUOUS{"약 후보가 하나로 결정되는가?"}
  CHOICE["selection_box로 약 후보 확인"]
  PRO["PRO-CTCAE 문항"]
  CONFIRM["기록 확인"]
  WRITE["Backend 동기 쓰기 API"]
  DB[("Backend DB<br/>부작용 기록")]

  INPUT --> SELECT --> MODEL_ARGS --> MATCH
  TRUSTED --> MATCH
  MATCH --> AMBIGUOUS
  AMBIGUOUS -- "아니오" --> CHOICE --> MATCH
  AMBIGUOUS -- "예" --> PRO --> CONFIRM --> WRITE --> DB
```

부작용 Tool은 별도 PHR 환자키로 환자 약을 다시 조회하지 않습니다. 같은 채팅
요청에서 구성한 신뢰 Snapshot을 재사용하고, 환자가 과거 시점을 명시해 현재
Snapshot만으로 부족할 때만 환자 범위가 고정된 상세 Read Tool을 실행합니다.
평가 결과 저장은 AI DB나 별도 PHR DB가 아니라 Backend 동기 쓰기 API를 통해
Backend DB에 반영합니다.

## 정책과 기록 변경

```mermaid
flowchart TD
  REQUEST["사용자 변경 요청"]
  TOOL["LLM Tool 선택<br/>업무 의미 인자만 생성"]
  INJECT["AI Server<br/>환자 · 공개 ID · version 주입"]
  CONFIRM["필요한 사용자 확인"]
  API["Backend v1.3 동기 쓰기 API"]
  VALIDATE["Backend<br/>인증 · 멱등성 · version · 업무 규칙"]
  SAVE[("Backend DB")]
  RESULT["권위 있는 쓰기 결과"]

  REQUEST --> TOOL --> INJECT --> CONFIRM --> API --> VALIDATE --> SAVE
  SAVE --> RESULT
```

## 중요한 규칙

- LLM은 증상 원문·시점처럼 사용자 표현에 가까운 최소 인자만 생성합니다.
- `patient_id`, 메시지 ID, 기록 ID, version은 AI Server가 신뢰 컨텍스트에서
  주입하며 모델 Tool 인자로 노출하지 않습니다.
- 모든 Tool 입력 스키마는 정의되지 않은 추가 속성을 허용하지 않습니다.
- 과거 상세 조회는 고정된 parameterized query만 사용합니다.
- Backend Read 전체 장애 또는 환자 범위 검증 실패 시 LLM을 호출하지 않습니다.
- 일부 Snapshot 도메인만 사용할 수 없으면 해당 정보를 요구하는 개인화 판단과
  Tool 실행만 제한합니다.
- 업무 쓰기는 Backend 동기 API를 통하며, AI Server는 Backend DB에 직접 쓰지
  않습니다.
- 사용자 확인이 필요한 변경은 확인 전에 적용 성공으로 표현하지 않습니다.
- 다음 채팅 요청에서는 Snapshot을 새로 읽습니다. 같은 요청에서 쓰기가 발생한
  뒤에는 쓰기 응답을 권위 있는 최신 결과로 사용합니다.
