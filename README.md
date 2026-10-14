# LitKG

LitKG 是一个面向生物医学文献的批量知识抽取系统。它可以把一批 PDF
统一摄取到同一个知识库，利用课题组部署的大语言模型抽取化合物、分子、
蛋白、药物、基因等实体和关系，并生成可增量扩展、可追溯到原文证据的
统一知识图谱。

## v2 的核心能力

- 一次上传最多 `MAX_BATCH_FILES` 篇 PDF，并跟踪整批和每篇文献的状态。
- PDF 按 SHA-256 去重；同一文件再次加入其他知识库时直接复用抽取结果。
- 一个部署可维护多个独立知识库。
- 实体先规范化再写入全局实体表，跨文献出现的同一实体只形成一个节点。
- 相同主语、谓词、宾语、限定条件和极性的关系合并成一条边。
- 每条 claim 和图谱证据保留标题、DOI、PDF 页码、原文句子、章节和文件名。
- 可按实体名称、别名、标准 ID、主语/宾语和关系类型检索知识图谱。
- 每条聚合边保留支持文献数、证据数和置信度。
- 新文献处理完成后直接加入现有图谱，不需要全量重建。
- Celery 重投递或手动重跑同一 chunk 时不会累积重复 claim。
- 原有的 `POST /documents`、单篇 claims 和单篇 graph 接口保持兼容。

```mermaid
flowchart LR
    B["导入批次"] --> KB["知识库"]
    KB --> D1["文献 A"]
    KB --> D2["文献 B"]
    D1 --> C1["Chunks / Claims"]
    D2 --> C2["Chunks / Claims"]
    C1 --> E["规范化实体"]
    C2 --> E
    C1 --> R["聚合关系"]
    C2 --> R
    R --> P["逐文献原文证据"]
```

## 数据合并规则

实体使用以下稳定键：

1. 有 `normalized_id` 时：`实体类型 + normalized_id`；
2. 没有 `normalized_id` 时：`实体类型 + NFKC/大小写/空白规范化后的名称`。

因此，`Aspirin` 和 `ASPIRIN` 会合并；`MET` Gene 与 `MET` Protein 不会
误合并。LLM 只会使用片段中明确出现的标准数据库 ID，未知 ID 必须填
`null`，所以没有外部 ID 时只能做确定性的名称合并。后续接入 ChEBI、
PubChem、UniProt、NCBI Gene 等实体链接器后，只需让抽取结果填入
`normalized_id`，或在
`app/app/normalization.py` 前增加实体链接步骤，数据库结构无需改变。

关系的唯一键由以下字段组成：

`subject_entity + predicate + object_entity + qualifiers + negated + speculative`

同一关系可连接多条 `relation_evidence`。图谱查询会返回
`document_count`、`evidence_count` 和 `evidence_samples`，不会因为合并而
丢失出处。

## 快速启动

可复制 `.env.example` 为 `.env`，再填写实际的数据库和模型服务配置：

```dotenv
POSTGRES_USER=litkg
POSTGRES_PASSWORD=change-me
POSTGRES_DB=litkg
DATABASE_URL=postgresql://litkg:change-me@postgres:5432/litkg
REDIS_URL=redis://redis:6379/0
GROBID_URL=http://grobid:8070
LLM_BASE_URL=http://your-llm-server/v1
LLM_API_KEY=EMPTY
LLM_MODEL=your-model

# 可选
MAX_BATCH_FILES=200
MAX_UPLOAD_BYTES=104857600
PARSE_WORKER_CONCURRENCY=4
EXTRACT_WORKER_CONCURRENCY=2
```

启动：

```bash
docker compose up --build
```

- API 与 Swagger：<http://localhost:8080/docs>
- Streamlit：<http://localhost:8501>
- GROBID：<http://localhost:8070>

`EXTRACT_WORKER_CONCURRENCY` 应根据 LLM 服务可承受的并发量设置。增加 API
或 worker 副本不会改变数据合并语义，实体和关系的数据库 upsert 是并发
安全的。

## 从旧版数据库升级

全新 PostgreSQL volume 会自动执行 `db/init.sql`。已有 volume 不会再次
运行 Docker 初始化脚本，需要手动执行一次幂等迁移：

```bash
docker compose cp db/init.sql postgres:/tmp/litkg-init.sql
docker compose exec postgres sh -lc \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f /tmp/litkg-init.sql'
```

迁移会把旧文献加入 `default` 知识库。已有 claims 还需投影到统一实体和
关系表：

```bash
curl http://localhost:8080/knowledge-bases
curl -X POST \
  http://localhost:8080/knowledge-bases/1/rebuild-graph
```

请使用第一个请求中 `slug=default` 的实际 ID，不要在非全新数据库中假定
它一定为 `1`。重建任务只补充规范化投影，不会重新调用 LLM。

本次出处信息升级也使用上述幂等迁移：新增 `documents.doi` 和 chunks 的
页码、段落位置字段。旧版 pypdf 生成的 `Page N` 片段可自动补齐页码；
其他旧文献若缺少 DOI 或页码，请重新上传相同 PDF 并设置
`force_reprocess=true`，重新解析和抽取。仅重建图谱不会补齐解析元数据。

## API 示例

### 1. 创建知识库

```bash
curl -X POST http://localhost:8080/knowledge-bases \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Kinase literature",
    "description": "课题组激酶相关文献"
  }'
```

### 2. 批量导入文献

```bash
curl -X POST \
  http://localhost:8080/knowledge-bases/2/documents \
  -F "files=@paper-a.pdf" \
  -F "files=@paper-b.pdf" \
  -F "files=@paper-c.pdf" \
  -F "batch_name=2026-07 kinase import" \
  -F "force_reprocess=false"
```

响应包含批次 ID、每个输入文件的 `document_id`、是否为新文献、是否进入
处理队列以及拒绝原因。已有且已完成的相同 PDF 会立即显示为 `done`，
不会重复调用模型。

### 3. 查询批次状态

```bash
curl http://localhost:8080/ingestion-batches/1
```

批次状态可能为：

- `queued`：已登记，等待 worker；
- `processing`：至少一篇正在解析或抽取；
- `done`：所有有效输入均成功；
- `partial`：部分成功，部分失败或被拒绝；
- `failed`：没有文献成功完成。

单篇文献还可能处于 `parsing`、`extracting` 或 `partial`。旧版把“部分
chunk 失败”误标为 `done` 的问题已修复。

### 4. 查询统一图谱

```bash
curl \
  "http://localhost:8080/knowledge-bases/2/graph?\
limit=500&min_document_count=2&evidence_limit=5&\
include_negated=false&include_speculative=false"
```

可选筛选参数：

- `predicate=INHIBITS`
- `entity_type=Protein`
- `min_document_count=2`
- `include_negated=false`
- `include_speculative=false`

返回结构：

```json
{
  "knowledge_base_id": 2,
  "nodes": [
    {
      "id": 12,
      "label": "EGFR",
      "type": "Protein",
      "normalized_id": null
    }
  ],
  "edges": [
    {
      "id": 8,
      "source": 4,
      "target": 12,
      "predicate": "INHIBITS",
      "document_count": 7,
      "evidence_count": 9,
      "confidence": 0.93,
      "evidence_samples": [
        {
          "document_id": 31,
          "filename": "paper-a.pdf",
          "title": "Kinase inhibition study",
          "doi": "10.1234/example",
          "page": 3,
          "page_end": 3,
          "section": "Results",
          "sentence": "Exact evidence sentence...",
          "confidence": 0.96
        }
      ]
    }
  ],
  "summary": {
    "node_count": 2,
    "relation_count": 1,
    "represented_document_count": 7
  }
}
```

### 5. 浏览知识库内容

```bash
curl "http://localhost:8080/knowledge-bases/2/documents?limit=100"
curl "http://localhost:8080/knowledge-bases/2/claims?predicate=BINDS_TO"
curl "http://localhost:8080/knowledge-bases/2/entities?query=EGFR"
curl "http://localhost:8080/knowledge-bases/2/batches"
```

所有列表接口都支持有界的 `limit`；claims、entities 和文献列表支持
`offset`，便于大知识库分页。

claims 的知识库接口和单篇接口均新增以下顶层字段，并保留原有字段：

- `title`、`doi`：当前文献的标题和 DOI。DOI 优先从 GROBID 文献头提取，
  排除参考文献；PDF 回退解析使用元数据或首页显式标注的 DOI。
- `page`、`page_end`：证据所在的 PDF 起止页，按 PDF 页序号从 `1` 开始，
  与期刊印刷页码可能不同。段落跨页或无法精确匹配句子时保留已知页码范围。
- `section`、`table_or_figure`：证据章节和图表标记。
- `document_id`、`filename`、`chunk_id`：用于追溯对应文献和片段。

标题、DOI 或页码缺失时返回 `null`。页码由解析器确定，模型输出的页码
不作为来源。图谱和关系检索接口的 `evidence_samples` 同样包含这些出处信息。
Streamlit 的 Claims 表格、图谱证据提示和关系检索结果可直接查看出处。

### 6. 检索实体与关系

实体检索沿用 `GET /knowledge-bases/{id}/entities`，按名称、别名或标准数据库
ID 做大小写不敏感的子串匹配。关系检索新增
`GET /knowledge-bases/{id}/relations`：

```bash
# 先找到实体及其 ID
curl "http://localhost:8080/knowledge-bases/2/entities?query=EGFR&entity_type=Protein"

# 按实体关键词检索相关关系
curl "http://localhost:8080/knowledge-bases/2/relations?query=EGFR&limit=50&offset=0"

# 精确查找某个实体作为宾语的抑制关系
curl "http://localhost:8080/knowledge-bases/2/relations?entity_id=12&direction=incoming&predicate=INHIBITS"

# 同时指定主语与宾语名称（也支持别名、标准 ID）
curl "http://localhost:8080/knowledge-bases/2/relations?subject=gefitinib&object=EGFR"

# 将检索结果展示成子图
curl "http://localhost:8080/knowledge-bases/2/graph?query=EGFR"
```

关系检索参数：

| 参数 | 用途 |
| --- | --- |
| `query` | 匹配两端实体的名称、别名、标准 ID，或关系类型 |
| `entity_id` | 精确匹配任一端实体 ID |
| `direction` | `both`（默认）、`outgoing`（作为主语）、`incoming`（作为宾语）；后两者必须提供 `entity_id` |
| `subject_entity_id` / `object_entity_id` | 精确指定主语 / 宾语实体 ID |
| `subject` / `object` | 分别按主语 / 宾语名称、别名或标准 ID 匹配 |
| `predicate` / `entity_type` | 筛选关系类型 / 任一端实体类型 |
| `min_document_count` | 最少支持文献数，默认 `1` |
| `include_negated` / `include_speculative` | 是否包含否定 / 推测关系，默认 `true` |
| `evidence_limit` | 每条关系返回的证据样本数，默认 `3`，最大 `20` |
| `limit` / `offset` | 分页，默认 `100` / `0`，`limit` 最大 `1000` |

多个筛选条件同时生效，搜索中的 `%` 和 `_` 按普通字符处理。
所有关系、支持文献计数和证据样本均限定在当前知识库内。

```json
{
  "knowledge_base_id": 2,
  "relations": [
    {
      "id": 8,
      "subject": {"id": 4, "text": "Gefitinib", "type": "Drug", "normalized_id": null},
      "predicate": "INHIBITS",
      "object": {"id": 12, "text": "EGFR", "type": "Protein", "normalized_id": null},
      "qualifiers": {},
      "negated": false,
      "speculative": false,
      "confidence": 0.93,
      "max_confidence": 0.96,
      "document_count": 7,
      "evidence_count": 9,
      "evidence": "Exact evidence sentence...",
      "evidence_samples": [
        {
          "claim_id": 99,
          "document_id": 31,
          "filename": "paper-a.pdf",
          "title": "Kinase inhibition study",
          "doi": "10.1234/example",
          "page": 3,
          "page_end": 3,
          "section": "Results",
          "table_or_figure": null,
          "sentence": "Exact evidence sentence...",
          "confidence": 0.96
        }
      ]
    }
  ],
  "total": 1,
  "limit": 100,
  "offset": 0
}
```

`total` 为分页前匹配关系总数，超出末页时仍返回准确的总数。
`GET /knowledge-bases/{id}/graph` 也支持 `query`、`entity_id` 和 `direction`，
返回现有 `nodes` / `edges` 结构。

## 旧版接口兼容

```bash
curl -X POST http://localhost:8080/documents \
  -F "file=@single-paper.pdf"

curl http://localhost:8080/documents/1
curl http://localhost:8080/documents/1/claims
curl http://localhost:8080/documents/1/graph
```

单篇上传现在会在 `default` 知识库中创建一个单项批次。

## 代码结构

```text
app/app/
├── ingestion.py       # PDF 校验、内容寻址存储、文献去重登记
├── normalization.py   # 实体、谓词、限定条件和 claim 稳定键
├── graph_store.py     # 实体、关系和证据的并发安全 upsert
├── graph_query.py     # 单篇/跨文献统一图谱查询与证据聚合
├── provenance.py      # 证据句子页码定位与出处字段
├── batch_status.py    # 批次状态汇总
├── tasks.py           # 解析、抽取、增量投影和旧数据重建任务
└── main.py            # 知识库、批次、文献、claims、实体和图谱 API
```

## 测试

Linux/macOS：

```bash
PYTHONPATH=app python -m unittest discover -s tests -v
```

PowerShell：

```powershell
$env:PYTHONPATH="app"
python -m unittest discover -s tests -v
```

测试覆盖实体规范化、关系/claim 指纹、文件名安全处理、PDF 校验、上传大小
限制、文件去重、DOI 提取、跨页证据定位、关系检索和 API 参数校验。
设置 `LITKG_TEST_DATABASE_URL` 为独立、可丢弃的 PostgreSQL 测试库后，
同一命令还会运行真实数据库集成测试。测试库需支持 pgvector 和 pg_trgm；
测试会在随机临时 schema 中验证解析落库、知识库隔离、分页和幂等迁移，
结束后删除该 schema。未设置此变量时跳过数据库集成测试。
完整部署后还可用 Swagger 依次执行“创建知识库
→ 上传两篇文献 → 等待批次完成 → 查询统一图谱”进行端到端验证。
