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
- 每条聚合边保留支持文献数、证据数、原文句子、章节、文件名和置信度。
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
限制和基于内容的文件去重。完整部署后还可用 Swagger 依次执行“创建知识库
→ 上传两篇文献 → 等待批次完成 → 查询统一图谱”进行端到端验证。
