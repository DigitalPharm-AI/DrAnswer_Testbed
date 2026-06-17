# 암종별 단순화 Agentic AI Care Loop

신장암과 유방암을 각각 별도 Mermaid 구성도로 분리했습니다.

- 신장암 Mermaid 원본: [simplified_kidney_cancer_care_loop.mmd](simplified_kidney_cancer_care_loop.mmd)
- 유방암 Mermaid 원본: [simplified_breast_cancer_care_loop.mmd](simplified_breast_cancer_care_loop.mmd)

## 신장암

```mermaid
%%{init: {"theme": "base", "flowchart": {"htmlLabels": true, "nodeSpacing": 80, "rankSpacing": 110, "diagramPadding": 30, "useMaxWidth": false}, "themeVariables": {"fontFamily": "Malgun Gothic, Noto Sans KR, Arial, sans-serif", "fontSize": "26px", "primaryTextColor": "#111827", "lineColor": "#64748B"}}}%%
flowchart LR
  subgraph PatientSide["환자 App / Web"]
    direction TB
    PatientInput["환자 입력<br/>채팅, 복약, 인바디, 식이"]
    PatientNotice["환자 중재<br/>알림, 피드백, 문진"]
  end

  subgraph Collect["A1. 수집"]
    direction TB
    CollectMedication["복약/부작용 수집<br/>복약 기록<br/>부작용 채팅<br/>PRO-CTCAE"]
    CollectKidney["신장암 특화 수집<br/>인바디 부종 수치<br/>단백질/나트륨/칼로리/지방<br/>식이·측정 누락"]
  end

  subgraph Store["데이터 저장"]
    direction TB
    RecordStore["환자 기록 저장소<br/>채팅 / 복약 / 부작용"]
    KidneyStore["신장암 모니터링 저장소<br/>인바디 / 식이 / 부종 이벤트"]
    PolicyStore["알림 정책 저장소"]
  end

  subgraph Analyze["A2. 분석"]
    direction TB
    CommonAnalysis["공통 분석<br/>부작용 감지<br/>미복용 원인<br/>복약 패턴"]
    EdemaCaution["부종 주의 분석<br/>0.39 <= 측정값 <= 0.4<br/>환자 알림 대상"]
    EdemaRisk["부종 위험 분석<br/>측정값 > 0.4<br/>의료진 알림 대상"]
    NutritionRisk["식이 위험 분석<br/>단백질/나트륨 등<br/>환자 cut off 초과"]
  end

  subgraph Mediate["A3. 중재"]
    direction TB
    PatientIntervention["환자 중재<br/>PRO-CTCAE 문진<br/>복약/측정 알림<br/>식이 조절 제안"]
    PolicyIntervention["알림 정책 변경<br/>미복용 패턴 기반<br/>환자 확인 후 적용"]
    ClinicianIntervention["의료진 중재<br/>부종 위험 알림<br/>상태 리뷰 요청"]
  end

  subgraph ClinicianSide["의료진 Web"]
    direction TB
    ClinicianDashboard["대시보드<br/>부작용, 복약 순응도<br/>부종/식이 위험"]
    ClinicianAction["의료진 판단<br/>정책 승인/수정<br/>추가 조치 판단"]
  end

  PatientInput --> CollectMedication
  PatientInput --> CollectKidney

  CollectMedication --> RecordStore
  CollectKidney --> KidneyStore
  CollectKidney --> PolicyStore

  RecordStore --> CommonAnalysis
  KidneyStore --> EdemaCaution
  KidneyStore --> EdemaRisk
  KidneyStore --> NutritionRisk
  PolicyStore --> CommonAnalysis

  CommonAnalysis --> PatientIntervention
  CommonAnalysis --> PolicyIntervention
  EdemaCaution --> PatientIntervention
  NutritionRisk --> PatientIntervention
  EdemaRisk --> ClinicianIntervention

  PatientIntervention --> PatientNotice
  PolicyIntervention --> PatientNotice
  PolicyIntervention --> PolicyStore
  ClinicianIntervention --> ClinicianDashboard
  ClinicianDashboard --> ClinicianAction
  ClinicianAction --> PolicyStore

  PatientNotice -.반응/추가 입력.-> PatientInput
  ClinicianAction -.정책/판단 반영.-> Mediate

  classDef patient fill:#FFE4E6,stroke:#E11D48,stroke-width:2px,color:#881337,font-size:26px,font-weight:700
  classDef collect fill:#E0F2FE,stroke:#0284C7,stroke-width:2px,color:#0C4A6E,font-size:26px,font-weight:700
  classDef store fill:#FEF3C7,stroke:#D97706,stroke-width:2px,color:#78350F,font-size:26px,font-weight:700
  classDef analyze fill:#DCFCE7,stroke:#16A34A,stroke-width:2px,color:#14532D,font-size:26px,font-weight:700
  classDef mediate fill:#F3E8FF,stroke:#9333EA,stroke-width:2px,color:#581C87,font-size:26px,font-weight:700
  classDef clinician fill:#FFEDD5,stroke:#EA580C,stroke-width:2px,color:#7C2D12,font-size:26px,font-weight:700

  class PatientInput,PatientNotice patient
  class CollectMedication,CollectKidney collect
  class RecordStore,KidneyStore,PolicyStore store
  class CommonAnalysis,EdemaCaution,EdemaRisk,NutritionRisk analyze
  class PatientIntervention,PolicyIntervention,ClinicianIntervention mediate
  class ClinicianDashboard,ClinicianAction clinician

  style PatientSide fill:#FFF1F2,stroke:#E11D48,stroke-width:3px,color:#881337,font-size:28px,font-weight:bold
  style Collect fill:#F0F9FF,stroke:#0284C7,stroke-width:3px,color:#0C4A6E,font-size:28px,font-weight:bold
  style Store fill:#FFFBEB,stroke:#D97706,stroke-width:3px,color:#78350F,font-size:28px,font-weight:bold
  style Analyze fill:#F0FDF4,stroke:#16A34A,stroke-width:3px,color:#14532D,font-size:28px,font-weight:bold
  style Mediate fill:#FAF5FF,stroke:#9333EA,stroke-width:3px,color:#581C87,font-size:28px,font-weight:bold
  style ClinicianSide fill:#FFF7ED,stroke:#EA580C,stroke-width:3px,color:#7C2D12,font-size:28px,font-weight:bold
  linkStyle default stroke:#64748B,stroke-width:2px
```

## 유방암

```mermaid
%%{init: {"theme": "base", "flowchart": {"htmlLabels": true, "nodeSpacing": 80, "rankSpacing": 110, "diagramPadding": 30, "useMaxWidth": false}, "themeVariables": {"fontFamily": "Malgun Gothic, Noto Sans KR, Arial, sans-serif", "fontSize": "26px", "primaryTextColor": "#111827", "lineColor": "#64748B"}}}%%
flowchart LR
  subgraph PatientSide["환자 App / Web"]
    direction TB
    PatientInput["환자 입력<br/>채팅, 복약, 인바디, 식이"]
    PatientNotice["환자 중재<br/>알림, 피드백, 문진"]
  end

  subgraph Collect["A1. 수집"]
    direction TB
    CollectMedication["복약/부작용 수집<br/>복약 기록<br/>부작용 채팅<br/>PRO-CTCAE"]
    CollectBreast["유방암 특화 수집<br/>인바디 측정/재측정<br/>부종 지속 기간<br/>식이·측정 누락"]
  end

  subgraph Store["데이터 저장"]
    direction TB
    RecordStore["환자 기록 저장소<br/>채팅 / 복약 / 부작용"]
    BreastStore["유방암 모니터링 저장소<br/>인바디 / 재측정 / 부종 이벤트"]
    PolicyStore["알림 정책 저장소"]
  end

  subgraph Analyze["A2. 분석"]
    direction TB
    CommonAnalysis["공통 분석<br/>부작용 감지<br/>미복용 원인<br/>복약 패턴"]
    EdemaDetect["부종 감지 분석<br/>cut off 초과 + 3일 미만<br/>다음날 재측정 대상"]
    EdemaFollow["부종 추적 분석<br/>cut off 초과 지속 + 3일 이상<br/>의료진 내원 판단 대상"]
    DietFeedbackRisk["식이 피드백 분석<br/>영양 성분 cut off 초과<br/>낮은 위험도 피드백"]
  end

  subgraph Mediate["A3. 중재"]
    direction TB
    PatientIntervention["환자 중재<br/>PRO-CTCAE 문진<br/>복약/측정 알림<br/>식이 피드백"]
    RetestIntervention["재측정 중재<br/>알림 정책 변경<br/>다음날 인바디 재측정"]
    ClinicianIntervention["의료진 중재<br/>부종 지속 알림<br/>내원 판단 요청"]
  end

  subgraph ClinicianSide["의료진 Web"]
    direction TB
    ClinicianDashboard["대시보드<br/>부작용, 복약 순응도<br/>부종 추적 상태"]
    ClinicianAction["의료진 판단<br/>정책 승인/수정<br/>내원 권고"]
  end

  PatientInput --> CollectMedication
  PatientInput --> CollectBreast

  CollectMedication --> RecordStore
  CollectBreast --> BreastStore
  CollectBreast --> PolicyStore

  RecordStore --> CommonAnalysis
  BreastStore --> EdemaDetect
  BreastStore --> EdemaFollow
  BreastStore --> DietFeedbackRisk
  PolicyStore --> CommonAnalysis

  CommonAnalysis --> PatientIntervention
  EdemaDetect --> RetestIntervention
  DietFeedbackRisk --> PatientIntervention
  EdemaFollow --> ClinicianIntervention

  PatientIntervention --> PatientNotice
  RetestIntervention --> PatientNotice
  RetestIntervention --> PolicyStore
  ClinicianIntervention --> ClinicianDashboard
  ClinicianDashboard --> ClinicianAction
  ClinicianAction --> PolicyStore

  PatientNotice -.반응/추가 입력.-> PatientInput
  ClinicianAction -.정책/판단 반영.-> Mediate

  classDef patient fill:#FFE4E6,stroke:#E11D48,stroke-width:2px,color:#881337,font-size:26px,font-weight:700
  classDef collect fill:#E0F2FE,stroke:#0284C7,stroke-width:2px,color:#0C4A6E,font-size:26px,font-weight:700
  classDef store fill:#FEF3C7,stroke:#D97706,stroke-width:2px,color:#78350F,font-size:26px,font-weight:700
  classDef analyze fill:#DCFCE7,stroke:#16A34A,stroke-width:2px,color:#14532D,font-size:26px,font-weight:700
  classDef mediate fill:#F3E8FF,stroke:#9333EA,stroke-width:2px,color:#581C87,font-size:26px,font-weight:700
  classDef clinician fill:#FFEDD5,stroke:#EA580C,stroke-width:2px,color:#7C2D12,font-size:26px,font-weight:700

  class PatientInput,PatientNotice patient
  class CollectMedication,CollectBreast collect
  class RecordStore,BreastStore,PolicyStore store
  class CommonAnalysis,EdemaDetect,EdemaFollow,DietFeedbackRisk analyze
  class PatientIntervention,RetestIntervention,ClinicianIntervention mediate
  class ClinicianDashboard,ClinicianAction clinician

  style PatientSide fill:#FFF1F2,stroke:#E11D48,stroke-width:3px,color:#881337,font-size:28px,font-weight:bold
  style Collect fill:#F0F9FF,stroke:#0284C7,stroke-width:3px,color:#0C4A6E,font-size:28px,font-weight:bold
  style Store fill:#FFFBEB,stroke:#D97706,stroke-width:3px,color:#78350F,font-size:28px,font-weight:bold
  style Analyze fill:#F0FDF4,stroke:#16A34A,stroke-width:3px,color:#14532D,font-size:28px,font-weight:bold
  style Mediate fill:#FAF5FF,stroke:#9333EA,stroke-width:3px,color:#581C87,font-size:28px,font-weight:bold
  style ClinicianSide fill:#FFF7ED,stroke:#EA580C,stroke-width:3px,color:#7C2D12,font-size:28px,font-weight:bold
  linkStyle default stroke:#64748B,stroke-width:2px
```
