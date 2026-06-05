# CopyCrab MCP Function Expansion Plan

> 목적: OpenCrab MCP의 범용 온톨로지 함수 중 CopyCrab에 필요한 기능을 선별 복제하고, BIM/모듈러 evidence pack 운영에 맞게 특화한다.  
> 기준: 불필요한 Marketplace/SaaS 중심 기능은 제외하고, 팩·문서 evidence·그래프·BIM 모듈·워크플로우·업데이트 기능을 추가한다.

---

## 1. 정의

CopyCrab은 OpenCrab의 범용 온톨로지 질의 구조를 기반으로 하되, 다음 목적에 맞춰 특화된 로컬/프로젝트형 MCP 서버다.

- BIM ontology pack 조회
- Revit IFC / Advance Steel evidence pack 분석
- 모듈, 부재, 체결재, 단면, 물량 정보 조회
- 검색 인덱스가 실패해도 evidence 문서를 직접 읽는 구조 제공
- 프로젝트별 BIM 데이터 질의 및 검토
- 향후 MES, BIM Coordination, 제작관리 시스템과 연동 가능한 MCP API 제공

핵심 원칙은 다음과 같다.

```txt
Evidence-first, Graph-assisted, BIM-specialized MCP
```

즉, CopyCrab은 질문을 받으면 먼저 evidence 문서를 직접 확인하고, 이후 검색 인덱스와 그래프를 보조적으로 사용해야 한다.

---

## 2. 현재 CopyCrab MCP 함수

현재 CopyCrab에는 아래 함수가 있다.

| 함수 | 현재 용도 | 유지 여부 |
|---|---|---:|
| `list_packs` | 사용 가능한 ontology ZIP pack 목록 조회 | 유지 |
| `list_projects` | BIM 프로젝트 및 연결된 팩 목록 조회 | 유지 |
| `search_pack` | 팩 evidence 문서 검색 | 유지, alias 추가 |
| `ask_pack_question` | 팩 evidence + graph context 기반 자연어 질의 | 유지, alias 추가 |
| `get_graph` | 팩 그래프 샘플 조회 | 유지 |

현재 한계는 명확하다.

- pack 내부 파일 목록을 직접 볼 수 없다.
- `documents/modules/` 같은 evidence entrypoint가 있어도 직접 접근할 수 없다.
- 특정 evidence 문서를 path로 읽을 수 없다.
- 모듈, 부재마크, 체결재 등 BIM 전용 API가 없다.
- OpenCrab의 node/edge/context 계층 함수가 없다.
- 검색 인덱스가 실패하면 evidence가 있어도 답변이 막힌다.

---

## 3. OpenCrab 함수 중 CopyCrab에 복제할 함수

### 3.1 복제 대상

| OpenCrab 함수 | CopyCrab 대응 함수 | 추가 방식 | 비고 |
|---|---|---|---|
| `opencrab_status` | `copycrab_status` | 신규 | 서버, 팩, 인덱스, graph 상태 진단 |
| `opencrab_search_packs` | `search_packs` | 신규 | 팩 검색 |
| `opencrab_search_documents` | `search_documents` | 신규 alias/확장 | 기존 `search_pack`의 OpenCrab 호환명 |
| `opencrab_query` | `query` | 신규 alias/확장 | 기존 `ask_pack_question`의 범용 질의명 |
| `opencrab_list_nodes` | `list_nodes` | 신규 | 그래프 노드 목록 |
| `opencrab_search_nodes` | `search_nodes` | 신규 | 그래프 노드 검색 |
| `opencrab_get_node_context` | `get_node_context` | 신규 | 특정 노드 주변 컨텍스트 |
| `opencrab_list_edges` | `list_edges` | 신규 | 그래프 엣지 목록 |
| `opencrab_list_sources` | `list_sources` | 신규 | 팩/프로젝트/source 요약 |
| `opencrab_project_run` | `project_run` | 신규 | 프로젝트 단위 질의 실행 |
| `opencrab_list_workflows` | `list_workflows` | 신규 | CopyCrab 워크플로우 목록 |
| `opencrab_run_workflow` | `run_workflow` | 신규 | 워크플로우 실행 |
| `opencrab_pack_update` | `pack_update` | 신규 | 기존 팩 업데이트 이벤트 추가 |
| `opencrab_ingest_text` | `ingest_text` | 신규 | 간단한 텍스트 evidence 추가 |

### 3.2 제외 대상

| OpenCrab 함수 | 제외 사유 |
|---|---|
| `opencrab_search_marketplace` | CopyCrab은 로컬/프로젝트형 BIM evidence pack MCP이므로 Marketplace 검색은 우선순위 낮음 |

Marketplace 기능은 CopyCrab이 독립 SaaS/팩 유통 기능을 갖기 전까지 제외한다.

---

## 4. CopyCrab에 새로 필요한 핵심 함수

OpenCrab 함수 복제만으로는 부족하다. CopyCrab에는 BIM evidence pack을 직접 다루는 함수가 별도로 필요하다.

### 4.1 Evidence 직접 접근 함수

#### `list_pack_documents`

팩 내부 문서 경로를 직접 조회한다.

```ts
list_pack_documents(args: {
  pack_id: string;
  prefix?: string;
  suffix?: string;
  limit?: number;
}): {
  pack_id: string;
  prefix?: string;
  suffix?: string;
  count: number;
  documents: string[];
}
```

예시 호출:

```json
{
  "pack_id": "advance-steel-samcheok-bldg-b-bm25-evidence-pack",
  "prefix": "documents/modules/",
  "suffix": ".md",
  "limit": 100
}
```

기대 결과:

```json
{
  "pack_id": "advance-steel-samcheok-bldg-b-bm25-evidence-pack",
  "prefix": "documents/modules/",
  "count": 24,
  "documents": [
    "documents/modules/1-05-ST.md",
    "documents/modules/RF-TRUSS.md"
  ]
}
```

이 함수는 검색 인덱스와 무관하게 ZIP 파일 내부 경로를 직접 읽어야 한다.

---

#### `read_pack_document`

팩 내부 evidence 문서를 path 기준으로 직접 읽는다.

```ts
read_pack_document(args: {
  pack_id: string;
  path: string;
  max_chars?: number;
}): {
  pack_id: string;
  path: string;
  content: string;
  truncated: boolean;
}
```

예시 호출:

```json
{
  "pack_id": "advance-steel-samcheok-bldg-b-bm25-evidence-pack",
  "path": "documents/modules/RF-TRUSS.md"
}
```

필수 조건:

- BM25 인덱스에 없어도 읽혀야 한다.
- `documents/modules/`, `documents/assembly_marks/`, `documents/fasteners/`, `documents/indexes/` 모두 접근 가능해야 한다.
- 보안상 pack root 밖의 경로 접근은 차단해야 한다.

---

### 4.2 BIM/모듈러 전용 함수

#### `list_modules`

팩의 전체 모듈 목록을 반환한다.

```ts
list_modules(args: {
  pack_id: string;
}): {
  pack_id: string;
  module_count: number;
  modules: Array<{
    module_id: string;
    module_type?: string;
    assembly_count?: number;
    single_part_count?: number;
    element_count?: number;
    total_weight_kg?: number;
    evidence_path?: string;
  }>;
}
```

처리 순서:

1. `list_pack_documents(prefix="documents/modules/", suffix=".md")`
2. 파일명에서 `module_id` 추출
3. 각 문서 내용에서 `module_type`, `assembly_count`, `single_part_count`, `total_weight_kg` 추출
4. 부족한 값은 graph `ModuleType` 또는 `Module` 노드에서 보조 추출
5. 정렬 후 반환

필수 acceptance:

- 삼척 팩은 `module_count = 24` 반환
- 여주 팩은 제작/유닛 모듈 22개 반환
- 현장공사분/Workset은 필요 시 옵션으로 포함

---

#### `get_module`

특정 모듈 evidence를 조회한다.

```ts
get_module(args: {
  pack_id: string;
  module_id: string;
}): {
  pack_id: string;
  module_id: string;
  module_type?: string;
  summary?: string;
  assembly_count?: number;
  single_part_count?: number;
  element_count?: number;
  total_weight_kg?: number;
  by_part_category?: Record<string, unknown>;
  by_section?: Record<string, unknown>;
  evidence_path?: string;
  evidence_content?: string;
}
```

처리 순서:

1. `documents/modules/{module_id}.md` 직접 읽기
2. 없으면 `search_documents(module_id)`
3. 없으면 `search_nodes(module_id)`
4. 최종 fallback으로 `get_graph` 샘플 사용

---

#### `list_assembly_marks`

부재 마크 문서 목록을 반환한다.

```ts
list_assembly_marks(args: {
  pack_id: string;
  limit?: number;
}): {
  pack_id: string;
  count: number;
  assembly_marks: Array<{
    mark: string;
    evidence_path: string;
  }>;
}
```

기본 entrypoint:

```txt
documents/assembly_marks/*.md
```

---

#### `get_assembly_mark`

특정 assembly mark evidence를 조회한다.

```ts
get_assembly_mark(args: {
  pack_id: string;
  mark: string;
}): {
  pack_id: string;
  mark: string;
  evidence_path?: string;
  content?: string;
  parsed?: Record<string, unknown>;
}
```

---

#### `get_fasteners`

볼트/앵커 evidence를 구조화해서 반환한다.

```ts
get_fasteners(args: {
  pack_id: string;
}): {
  pack_id: string;
  bolt_quantity?: number;
  anchor_quantity?: number;
  bolts_by_spec?: Array<Record<string, unknown>>;
  anchors_by_spec?: Array<Record<string, unknown>>;
  evidence_path?: string;
}
```

기본 entrypoint:

```txt
documents/fasteners/model_fasteners.md
```

삼척 acceptance:

```txt
bolt_quantity = 1011
anchor_quantity = 314
```

---

#### `get_section_weight_index`

단면별 수량, 길이, 중량 인덱스를 반환한다.

```ts
get_section_weight_index(args: {
  pack_id: string;
}): {
  pack_id: string;
  sections: Array<{
    section: string;
    count?: number;
    total_length_m?: number;
    total_weight_kg?: number;
  }>;
  evidence_path?: string;
}
```

기본 entrypoint:

```txt
documents/section_weight_index.md
```

---

## 5. 최종 CopyCrab MCP 함수 목록

### 5.1 Core

```txt
copycrab_status
list_packs
search_packs
list_projects
project_run
```

### 5.2 Evidence

```txt
search_pack
search_documents
list_pack_documents
read_pack_document
query
ask_pack_question
```

### 5.3 Graph

```txt
get_graph
list_nodes
search_nodes
get_node_context
list_edges
list_sources
```

### 5.4 BIM / Modular

```txt
list_modules
get_module
list_assembly_marks
get_assembly_mark
get_fasteners
get_section_weight_index
```

### 5.5 Workflow

```txt
list_workflows
run_workflow
```

### 5.6 Update / Ingest

```txt
pack_update
ingest_text
```

### 5.7 제외

```txt
search_marketplace
```

---

## 6. Retrieval 우선순위

CopyCrab은 BIM 질문에 대해 다음 순서로 접근해야 한다.

```txt
1. Direct evidence path access
2. Evidence document list/read
3. Evidence document search
4. BIM-specialized parser
5. Graph node search
6. Graph context lookup
7. Graph sample fallback
8. Natural-language answer generation
```

금지할 패턴:

```txt
BIM evidence entrypoint가 있는데 곧바로 get_graph 샘플링으로 넘어가는 방식
```

권장 패턴:

```txt
질문: 삼척 모듈 목록 리스트업

1. list_pack_documents(prefix="documents/modules/")
2. read_pack_document(each module document)
3. parse module_id/module_type/counts
4. list_modules 결과 반환
5. 부족한 값만 graph에서 보조
```

---

## 7. 함수별 구현 우선순위

### Phase 1 — Evidence 접근 복구

최우선 구현 대상:

```txt
copycrab_status
list_pack_documents
read_pack_document
search_documents
```

목표:

- ZIP 내부 evidence 문서에 직접 접근
- 검색 인덱스 실패 시에도 문서 확인 가능
- 삼척 `documents/modules/` 목록 조회 가능

---

### Phase 2 — BIM 전용 조회

구현 대상:

```txt
list_modules
get_module
list_assembly_marks
get_assembly_mark
get_fasteners
get_section_weight_index
```

목표:

- 삼척/여주 모듈 목록 확정 추출
- 모듈 단위 수량/중량/부재 정보 조회
- 볼트/앵커/단면 인덱스 조회

---

### Phase 3 — Graph API 확장

구현 대상:

```txt
list_nodes
search_nodes
get_node_context
list_edges
list_sources
```

목표:

- OpenCrab 스타일의 그래프 탐색 제공
- 특정 노드 주변 관계 조회
- BIM ontology graph 디버깅 가능

---

### Phase 4 — Project / Workflow / Update

구현 대상:

```txt
project_run
list_workflows
run_workflow
pack_update
ingest_text
```

목표:

- 프로젝트 단위 BIM pack 질의
- 정형 검토 워크플로우 실행
- 팩 업데이트 이벤트 기록
- 간단한 텍스트 evidence 추가

---

## 8. Acceptance Tests

### Test 1 — MCP 상태 확인

호출:

```json
{}
```

함수:

```txt
copycrab_status
```

기대 결과:

- status = ok
- 등록된 tool 목록 반환
- pack count 반환
- project count 반환

---

### Test 2 — 삼척 모듈 문서 목록

호출:

```json
{
  "pack_id": "advance-steel-samcheok-bldg-b-bm25-evidence-pack",
  "prefix": "documents/modules/",
  "suffix": ".md"
}
```

함수:

```txt
list_pack_documents
```

기대 결과:

- count = 24
- `documents/modules/*.md` 경로 24개 반환
- 검색 인덱스가 비어도 동작

---

### Test 3 — 삼척 전체 모듈 목록

호출:

```json
{
  "pack_id": "advance-steel-samcheok-bldg-b-bm25-evidence-pack"
}
```

함수:

```txt
list_modules
```

기대 결과:

- module_count = 24
- 전체 module_id 반환
- module_type 가능한 범위 내 반환
- evidence_path 포함

---

### Test 4 — RF-TRUSS 모듈 조회

호출:

```json
{
  "pack_id": "advance-steel-samcheok-bldg-b-bm25-evidence-pack",
  "module_id": "RF-TRUSS"
}
```

함수:

```txt
get_module
```

기대 결과:

- module_id = RF-TRUSS
- module_type = TRUSS
- evidence_path = `documents/modules/RF-TRUSS.md`
- 가능한 경우 assembly_count, single_part_count, total_weight_kg 반환

---

### Test 5 — 삼척 체결재 조회

호출:

```json
{
  "pack_id": "advance-steel-samcheok-bldg-b-bm25-evidence-pack"
}
```

함수:

```txt
get_fasteners
```

기대 결과:

- bolt_quantity = 1011
- anchor_quantity = 314
- bolt spec별 수량 반환
- anchor spec별 수량 반환

---

### Test 6 — 검색 인덱스 실패 우회

상황:

```txt
search_documents("documents/modules") 결과가 비어 있음
```

호출:

```txt
list_pack_documents(prefix="documents/modules/")
read_pack_document(path="documents/modules/RF-TRUSS.md")
```

기대 결과:

- 검색 결과가 없어도 ZIP 내부 문서 직접 접근 성공

---

## 9. 구현 메모

### 9.1 ZIP pack 접근

CopyCrab은 pack_id를 기준으로 ZIP 파일 경로를 찾아야 한다.

권장 내부 함수:

```python
def resolve_pack_path(pack_id: str) -> Path:
    ...
```

문서 목록:

```python
with zipfile.ZipFile(pack_path) as zf:
    names = zf.namelist()
    docs = [n for n in names if n.startswith(prefix) and n.endswith(suffix)]
```

문서 읽기:

```python
with zipfile.ZipFile(pack_path) as zf:
    content = zf.read(path).decode("utf-8")
```

### 9.2 경로 보안

다음 패턴은 차단해야 한다.

```txt
../
..\\
absolute path
null byte
```

허용 경로는 pack 내부 relative path만 가능하다.

### 9.3 Parser

모듈 문서는 다음 순서로 파싱한다.

1. YAML frontmatter
2. Markdown heading
3. key-value table
4. JSON code block
5. filename fallback

### 9.4 Backward Compatibility

기존 함수는 제거하지 않는다.

```txt
search_pack
ask_pack_question
get_graph
list_packs
list_projects
```

새 함수는 기존 함수의 alias 또는 확장으로 추가한다.

```txt
search_pack -> search_documents alias
ask_pack_question -> query alias
```

---

## 10. 최종 목표

CopyCrab은 OpenCrab의 유용한 MCP 함수 구조를 복제하되, 단순 복제가 아니라 BIM evidence pack 운영에 맞게 아래 능력을 가져야 한다.

```txt
1. 팩을 찾는다.
2. 팩 내부 evidence 문서를 직접 나열한다.
3. 특정 evidence 문서를 직접 읽는다.
4. 모듈/부재/체결재/단면 정보를 구조화해서 반환한다.
5. 부족한 정보는 그래프에서 보조한다.
6. 프로젝트 단위 질의와 워크플로우 실행으로 확장한다.
```

즉, CopyCrab의 정체성은 다음과 같다.

```txt
OpenCrab-compatible BIM Evidence MCP
```

또는 더 짧게:

```txt
BIM evidence-first OpenCrab fork
```
