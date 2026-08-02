# 식약처 부작용 참조 DB — Agent/Trace DB 운영

## 구조

식약처 허가사항에서 산출한 부작용 참조 데이터는 Backend/System DB가
아니라 `AGENT_DATABASE_URL`이 가리키는 Agent/Trace PostgreSQL DB에
저장한다. 환자의 활성 복약 목록은 기존의 신뢰된 Backend Snapshot에서
읽지만, 약물 품목 해석과 부작용 허가사항 조회는 Agent 내부에서 수행한다.

주요 객체는 다음과 같다.

- `mfds_import_runs`
- `mfds_drug_products`
- `mfds_label_documents`
- `mfds_product_label_documents`
- `mfds_label_sections`
- `mfds_document_label_sections`
- `mfds_adverse_reactions`
- `mfds_section_adverse_reactions`
- `mfds_drug_aliases`
- `mfds_product_adverse_reaction_view`
- `mfds_adverse_reaction_embeddings`
- `agent_pro_ctcae_alias_embeddings`

원문 압축 XML을 포함해 기존 SQLite 산출물의 핵심 테이블을 모두
복제한다. 런타임은 `mfds_product_adverse_reaction_view`를 통해 근거 문장,
추출 신뢰도와 검수 상태를 함께 조회한다.

## Migration

```powershell
$env:DA_DRUG_SERVICE = "agent_app"
$env:DA_DRUG_ENV_FILE = ".env.9000"
.\.venv\Scripts\python.exe -m agent_app.migrate
```

## 데이터 적재

```powershell
$env:DA_DRUG_SERVICE = "agent_app"
$env:DA_DRUG_ENV_FILE = ".env.9000"
.\.venv\Scripts\python.exe -m scripts.import_mfds_adverse_reactions `
  "C:\path\to\mfds_adverse_reactions.sqlite"
```

Importer는 SQLite를 읽기 전용으로 열고 Agent DB의 참조 테이블을 한
트랜잭션에서 교체한다. 테이블별 건수, SQLite FK, PostgreSQL 근거문장
offset과 테스트베드 별칭을 검사하며 실패하면 전체 적재를 rollback한다.

## Cohere/pgvector 의미 검색 구축

정확히 같은 정규화 용어는 외부 모델 호출 없이 즉시 조회한다. 정확
일치가 없을 때에만 Tokyo 리전의 Cohere Embed Multilingual v3로 query
embedding을 생성하고, HNSW cosine 상위 후보를 Haiku 구조화 출력으로
동의어인지 보수적으로 검증한다. 검증된 reaction ID만 현재 활성 복약
품목과 교집합한다. Pro-CTCAE의 정확 별칭 묶음은 `메스꺼움`·`구역`·`오심`
같이 같은 임상 개념이 MFDS에서 서로 다른 reaction ID인 경우를 확장하는
데 사용한다.

```powershell
$env:DA_DRUG_SERVICE = "agent_app"
$env:DA_DRUG_ENV_FILE = ".env.9000,.env.agent_app.secret"
.\.venv\Scripts\python.exe -m scripts.backfill_agent_embeddings --target all
.\.venv\Scripts\python.exe -m scripts.backfill_agent_embeddings --target all --check
```

백필은 model ID와 원본 버전을 기준으로 이미 완료된 행을 건너뛰고 batch
단위로 commit하므로 중단 후 그대로 재실행할 수 있다. 마지막 check는
누락 행이 있으면 non-zero로 종료한다. 임베딩 테이블의 입력 원문,
모델, 리전, 차원, 원본 버전과 SHA-256은 Agent DB에 함께 보존한다.

## 테스트베드 검증

테스트베드의 성분명 복약은 다음 정상 허가 품목으로 해석한다.

| 테스트베드 약물 | ITEM_SEQ | 검증 증상 |
|---|---:|---|
| 메트포르민 500mg | 200401015 | 발진 |
| 암로디핀 5mg | 200610660 | 근육통 |
| 수니티닙 50mg | 200606182 | 설사 |
| 레트로졸 2.5mg | 200108765 | 어지러움/현기증 |

```powershell
$env:DA_DRUG_SERVICE = "agent_app"
$env:DA_DRUG_ENV_FILE = ".env.9000"
.\.venv\Scripts\python.exe -m scripts.verify_testbed_adverse_reactions
```

## 판정 의미

양성 결과는 환자의 증상 표현이 식약처 허가사항의 부작용 후보와
일치한다는 뜻이며 약물과 증상의 인과관계를 확정하지 않는다. 현재
산출물의 검수 상태는 `CANDIDATE_NOT_CLINICALLY_REVIEWED`이므로 근거
문장과 상태를 응답에 포함하고, 양성 결과는 기존 PRO-CTCAE 평가로
연결한다.
