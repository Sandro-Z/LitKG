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

实体先经过本地、可追溯的规范化，再确定身份：

1. 规范化标准 ID 的前缀、大小写和已支持的 URL 写法，例如
   `CHEBI_15365`、`chebi: 15365` 和 OBO URL 均归为 `CHEBI:15365`。
   同一实体可保存多个数据库 ID，实体检索返回 `identifiers` 并支持搜索这些 ID。
2. 统一 NFKC、大小写和希腊字母。Protein、Gene、CellLine 和 Pathway
   统一空白、连字符与下划线，例如 `TNF-α` / `TNF alpha`、`IL-6` / `IL6`。
3. 从每篇文献保存的原文识别“全称（缩写）”及“缩写（全称）”，把全文中
   明确定义的缩写映射到全称，跨 chunk 生效。同一篇文献里出现多个不同
   定义的缩写保持歧义，不借用其他文献的单一解释。
4. Drug / Compound 使用共同的 Chemical 身份，缓解模型类型不一致造成的
   重复节点；Gene / Protein 仍严格分开。

有 ID 的身份键为 `身份类别 + 标准 ID`，无 ID 时为
`身份类别 + 规范化名称`。`compound 1` 等没有全局身份的文献内编号按
文献隔离，即使带有 ID，也不会仅凭局部编号推断跨数据库 ID 等价。

合并会保留数字、物种、突变、修饰、异构体和化学立体构型信息。
同一数据库命名空间的不同 ID 不自动合并；含歧义的短名称不据此合并
已有节点。不同命名空间的 ID 仅在唯一名称匹配、且无已知 ID 冲突时附加。
旧节点重建时还可合并规范化名称一致、ID 不冲突的重复节点。
合并在事务中迁移关系、别名和所有证据，不修改原始 `claim_json`。

LLM 仍只能填写片段明确给出的标准 ID，未知 ID 必须为 `null`。
以上过程不调用外部实体链接服务，也不上传文献。

关系的唯一键由以下字段组成：

`subject_entity + predicate + object_entity + qualifiers + negated + speculative`

同一关系可连接多条 `relation_evidence`。图谱查询会返回
`document_count`、`evidence_count` 和 `evidence_samples`，不会因为合并而
丢失出处。

### 缓解不连通的小图

实体被拆成多个名称节点只是碎片化的一个来源。图谱的 `limit` 和筛选条件
也会隐藏连接边；主题不同、原文没有连接事实的实体则应保留独立分量。
系统不会为了提高连通率添加未经文献支持的边或强行合并相似名称。

`GET /knowledge-bases/{id}/graph/diagnostics` 统计完整知识库，不受显示数量
和图谱筛选条件影响，返回连通分量数、最大分量实体数及占比、主图外实体数、
独立实体数、自环关系数，以及分量大小分布。连通性按无向连接计算；
仅有自环的节点算独立实体；小分量定义为不超过 `5` 个节点。
普通 graph 响应的 `summary` 也包含相同指标，但只针对当前显示子图。

升级后执行本节下方的数据库迁移，再重建每个知识库即可把新规则应用于
已有 claims，无需重新上传 PDF 或重新调用 LLM：

```bash
curl -X POST http://localhost:8080/knowledge-bases/2/rebuild-graph
# 从响应中获取 task_id，然后查询进度和重建前后指标
curl http://localhost:8080/knowledge-bases/2/rebuild-graph/TASK_ID
curl http://localhost:8080/knowledge-bases/2/graph/diagnostics
```

重建响应包含 `task_id` 和 `status_url`。状态查询返回 `queued`、`processing`、
`done`、`partial` 或 `failed`，完成后的 `result` 包含 `before`、`after`、
`projected_count`、`failed_count`、缩写数量和跳过的处理中文献数量。
正在解析或抽取的文献会跳过，完成后可再次重建。重建可重复执行。
实体表在知识库之间共享；已确认等价的节点合并会更新其全局引用，保留
各知识库文献范围、所有原文证据和否定/推测/限定条件。实体与聚合关系 ID
可能改变，保存过这些 ID 的客户端应重新检索。

Streamlit 的“统一图谱 → 实体规范化与完整图谱连通性”可直接检查完整图谱、
启动重建并查看前后对比。实际改善程度以当前文献的重建报告为准。

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

### 修复已有文献的 PDF 页码

旧解析器只请求段落坐标。GROBID 正常返回 DOI 和正文时，仍可能缺少
段落坐标；一个段落跨页时，整段范围也不能精确代表某一句证据的位置。
现在请求句子与段落坐标，优先保存句子位置；缺失或只有跨页段落范围时，
使用本地 PDF 逐页文本补齐。匹配支持 PDF 连字、软连字符和断行差异，
不会把匹配失败的文字猜成某一页。

仅更新镜像不会重新解析已经入库的文献。保留现有 claims 和图谱，
无需重新上传或调用 LLM，可以从原始 PDF 修复页码。先更新代码并构建
API 和两个 worker，再预览指定知识库的变更：

```bash
docker compose up -d --build api worker_parse worker_extract
docker compose exec -T api python -m app.page_repair --knowledge-base-id 1 --dry-run
```

确认预览结果后去掉 `--dry-run` 应用修复（将 `1` 换成实际知识库 ID）：

```bash
docker compose exec -T api python -m app.page_repair --knowledge-base-id 1
```

也可以用 `--document-id 123` 只修复单篇。命令只读取已保存的本地 PDF，
更新 chunks 的页码/位置和 `claim_json.evidence` 的 `page`、`page_end`，
保留原文、claim ID、实体、关系和证据链接。按文献事务提交，可重复运行；
正在处理或被其他事务锁定的文献返回 `skipped_active`，稍后重试。
文献可能属于多个知识库，修复该文献的出处也会更新这些知识库的显示。

报告包含 `changed_chunk_count`、`changed_claim_count`、`located_claim_count`
和 `unresolved_claim_count`。无法定位的旧证据保留原字段并计入 unresolved；
PDF 文件丢失返回 `failed`，没有可提取文字返回 `no_pdf_text`。扫描 PDF
若没有文字层，需要先做 OCR。此修复不新增数据库字段。

实体规范化升级同样需要执行 `db/init.sql`：新增文献的 `abbreviation_map`、
`entity_identifiers` 和 `graph_rebuild_jobs`。应用迁移并重新构建 API / worker / frontend
后，使用重建图谱接口更新已有实体即可。缩写来源是已经保存的 chunks；
如果旧片段没有保留定义所在的原文，则仍需要重新解析该文献才能使用定义。

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
  与期刊印刷页码可能不同。优先使用句子坐标，缺失时与本地 PDF 逐页文字
  匹配；句子跨页、重复出现在多页或只能确定片段范围时保留已知页码范围。
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
├── graph_diagnostics.py # 完整图谱与显示子图的连通性统计
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
新增规范化测试覆盖希腊字母与名称格式、文献缩写歧义、局部编号隔离、
不同 ID 写法与并发写入，以及旧图合并后的连通性和证据保留。
设置 `LITKG_TEST_DATABASE_URL` 为独立、可丢弃的 PostgreSQL 测试库后，
同一命令还会运行真实数据库集成测试。测试库需支持 pgvector 和 pg_trgm；
测试会在随机临时 schema 中验证解析落库、知识库隔离、分页和幂等迁移，
结束后删除该 schema。未设置此变量时跳过数据库集成测试。
完整部署后还可用 Swagger 依次执行“创建知识库
→ 上传两篇文献 → 等待批次完成 → 查询统一图谱”进行端到端验证。

## v3：全文目录、完整事实与证据问答

### 全文目录鲁棒性修复（2026-10-16）

目录先将 mention/evidence 锚定到真实输入：只处理已有的 Unicode、大小写和空白排版差异，
不进行模糊名称匹配，不删除身份性的数字、物种、突变或连字符。无依据的 aliases 会被移除，
证据可替换为包含已验证提及的连续原文；原始输出、修复前 JSON 和字段修复记录均保留，
并产生 `catalog_repair` 复核项。核心提及或标准 ID 无法验证时仍显式失败。

目录 JSON/原文校验失败时，会携带具体错误与上次输出纠正重试。
`CATALOG_VALIDATION_ATTEMPTS` 默认 `3`（总调用次数，限制为 1–5）；
传输错误仍使用原有的有限退避。纠正响应不能用空目录掩盖失败。
缓存保存清洗后的结果，并将目录规则和提示词版本加入键；旧版目录缓存不会被误用。
已完成的有效分段继续可恢复，全部目录通过前仍不会发布关系抽取任务。
本次无需数据库结构迁移。

部署：`bash scripts/deploy_catalog_robustness.sh`。脚本先构建，在临时 PostgreSQL 容器中
运行针对性和数据库回归，通过后更新 API/两个 worker 并验证代码摘要、健康与 worker 响应。
测试数据库使用独立网络和 tmpfs，不挂载正式数据或 `.env`，退出时清理测试资源。
追加 `--retry-failed` 可在验证部署成功后，为知识库 14 中仍失败的文献 25–29 排队 `retry`。
脚本需要 Docker 权限；普通账号会通过 sudo 交互认证。

新增处理顺序为 `PDF → 全文目录 → chunk 关系 → 完整事实 → 派生索引/摘要`。
目录识别覆盖所有已解析 chunks，长片段有重叠分段，包含文章标题和章节标题；
GROBID 正文中的图表、列表和附录也保留。参考文献列表不作为本文实验事实。
扫描 PDF 仍需文字层或 OCR。不会对长文档取前 N 字后宣称处理完成。

目录保存文档内稳定 ID、规范名、统一类型、别名、原文明确给出的标准 ID、
位置、歧义和不确定原因。跨段只按明确身份、明确且唯一的别名/缩写归并；
同一命名空间冲突 ID 和多义缩写保留分离。目录提交完成前不会发布关系任务。
关系仅能使用当前 chunk 的证据和提及；目录漏项可追加，并进入待复核。
旧版文献的目录状态是 `legacy`，重新投影不会伪装成已完成全文实体识别。

类型由 `app/app/entity_types.py` 统一管理，经 `/entity-types` 提供给前端；新增
CellType、Tissue、AnatomicalStructure、RNA、MolecularComplex、BiologicalProcess、
Phenotype。未知类型保留 Other 和原因。当前工作区未提供真实 PDF，未访问正式文献库
做分布校准；新增类型依据任务要求，真实 Other 分布需使用下面的评估入口。

实体 `text` 表示身份，`mention` 保留完整原始提及；实体上的 `process` 分别保存
`expression`、`secretion`、`activity` 及方向。剂量、时间、数值等保存在 qualifiers；
`participants` 保存 cell_line、species、assay 等带角色的实体。同一角色可有多个实体。
身份性的突变、修饰、异构体、物种标签和立体构型不会按过程词删除。
无法可靠拆分时保留名称、原文并进入复核，不通过强制归类降低 Other。

`facts` 保存逐来源断言，`fact_participants` 复用全局规范化实体 ID，
`fact_evidence` 保存主证据和全部 additional_evidence。相同内容的断言可共享
`fact_key` 和原有聚合 relation，但仍保留各自来源。二元关系键还包含参与者条件，
不同时间、物种、细胞系、过程、否定和推测不会合成同一结论。旧 graph/relations
接口仍可用；当前目录版本以外的旧抽取不进入当前图谱或问答。

原始模型结构保存在 processing_attempts 和 processing_cache；claim_json 保留
模型名称及原始提及，页码由服务器定位。重新解析或替换前保存 claim_history，
包含原始 chunk、标题、DOI、文件名和页码位置。派生内容可删除重建，不能替代证据。

### 迁移与旧文献升级

以下命令供人工在选定环境执行；本次开发没有对正式库执行迁移或启动正式服务。
先对目标库做备份，再使用已有的 `db/init.sql` 幂等迁移。独立测试库可直接执行：

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 --single-transaction -f db/init.sql
```

既有 Docker 环境也可沿用前文的 `docker compose cp/exec` 迁移方式。
重新应用迁移不会把已属于其他知识库的文献自动加入 default。

```bash
# 无需抽取 LLM：从已有记录重建实体/完整事实；不会恢复旧记录未保存的过程语义
curl -X POST http://127.0.0.1:18080/knowledge-bases/2/maintenance \
  -H 'Content-Type: application/json' \
  -d '{"mode":"reproject","document_id":31}'

# 需要抽取 LLM：用全部已保存原文建立目录，再重新抽取所有 chunks
curl -X POST http://127.0.0.1:18080/knowledge-bases/2/maintenance \
  -H 'Content-Type: application/json' \
  -d '{"mode":"reextract","document_id":31}'

# 失败后续接；已经成功的分段复用内容与处理版本缓存
curl -X POST http://127.0.0.1:18080/knowledge-bases/2/maintenance \
  -H 'Content-Type: application/json' -d '{"mode":"retry","document_id":31}'

# 只更新索引和摘要；省略 document_id 时维护该知识库的文献
curl -X POST http://127.0.0.1:18080/knowledge-bases/2/maintenance \
  -H 'Content-Type: application/json' -d '{"mode":"derived"}'
```

响应包含 task_id 和 status_url；`GET status_url` 返回持久化状态和逐篇结果。
模型输出错误、截断、无效引用或原文校验失败会记录失败，不能变成成功空列表。
队列发布失败同样可见。`retry` 可恢复队列中断后遗留的 queued/extracting 状态；
任务锁及版本检查会阻止同时写入或写入过期结果。
同一 PDF 的文献记录可由多个知识库共享，更新该记录会更新所有包含它的知识库。

```text
GET  /knowledge-bases/{kb}/documents/{doc}/catalog
GET  /knowledge-bases/{kb}/documents/{doc}/processing
GET  /knowledge-bases/{kb}/facts?entity_id=12&limit=100&offset=0
GET  /knowledge-bases/{kb}/browse?kind=overview|document|entity|fact
GET  /knowledge-bases/{kb}/reviews?status=pending
POST /knowledge-bases/{kb}/reviews/{review}?status=resolved&note=...
```

复核支持 pending/resolved/dismissed；标记复核不自动改写证据。复核项包括目录歧义、
漏项、Other、不可靠的过程拆分，以及相同参与者/条件下的相反极性候选冲突。
不同条件的结果不会因此被判为冲突。浏览页面采用有来源链接的抽取式摘要，
文献/实体页面展示最多 20 条断言，并提供完整事实分页入口；这不是抽取截断。

### 检索与问答

检索分别召回实体与完整条件事实，结合文本匹配和可选语义召回，再沿参与者关联
进行 0–2 跳有界扩展。引用直接回查当前知识库的事实和证据，组装原文上下文；
不会把派生摘要当成原文。答案逐项引用；引用 ID 无效或缺失时返回原文证据及错误提示。
没有证据时返回 insufficient_evidence；生成失败返回 evidence_only，不伪装为正常回答。
LLM 是否正确理解证据仍需实际评估，引用有效不等于语义蕴含已由程序证明。

```bash
curl -X POST http://127.0.0.1:18080/knowledge-bases/2/ask \
  -H 'Content-Type: application/json' \
  -d '{"question":"该处理对目标蛋白的分泌有何影响？","hops":1,"limit":12,"generate":true}'
```

响应含 answer、statements、citations、facts 和 warnings。citations 包含文献 ID、
标题、文件名、DOI、PDF 起止页、chunk、原文句子、上下文、条件、否定和推测。
未能定位的 DOI/页码仍为 null。`generate=false` 只检索展示证据，不调用答案 LLM。
Streamlit 增加“证据问答”和“知识浏览与维护”入口。

Embedding 默认关闭；启用时使用独立配置：

```dotenv
EMBEDDING_BASE_URL=http://your-embedding-server/v1
EMBEDDING_MODEL=your-embedding-model
EMBEDDING_API_KEY=EMPTY
# 同名模型权重或提示策略更新时可改变此值以失效抽取缓存
LLM_REVISION=2026-10-16
CATALOG_SEGMENT_CHARS=12000
```

更换 embedding 端点/模型后运行 derived 维护。向量保存在现有 PostgreSQL pgvector，
仅比较同一端点/模型且维度相同的向量。嵌入失败有 warnings，并保留文本检索。
实体/事实索引按内容及处理版本缓存；变更只刷新相关文献及其原有/新增实体。
配置的模型服务会接收所需原文；仅使用适合该文献数据访问范围的端点。

### 本机临时启动示例

先提供独立的临时 PostgreSQL、Redis 和模型服务环境变量，不复用正式数据库。
安装依赖后可在不同终端运行（不注册为服务）：

```bash
export PYTHONPATH=app
export UPLOAD_DIR=/tmp/litkg-v3-uploads
# DATABASE_URL / REDIS_URL / LLM_BASE_URL / LLM_MODEL 指向测试服务
uvicorn app.main:app --host 127.0.0.1 --port 18080
celery -A app.celery_app worker -Q parse,extract --concurrency=2 --loglevel=INFO
API_BASE_URL=http://127.0.0.1:18080 streamlit run frontend/app.py \
  --server.address 127.0.0.1 --server.port 18501
```

### 验证与小规模对照

```bash
# 安装 app/requirements.txt、httpx；前端测试另需 frontend/requirements.txt
PYTHONPATH=app python -m unittest discover -s tests -v
# 使用可丢弃测试库启用真实 SQL 集成测试；每组使用临时 schema 并清理
LITKG_TEST_DATABASE_URL=postgresql://... PYTHONPATH=app \
  python -m unittest discover -s tests -v

# 在有真实文献和人工标注时生成评估快照；默认不调用答案 LLM
PYTHONPATH=app python -m app.evaluation --knowledge-base-id 2 \
  --queries /tmp/labelled-questions.json --output /tmp/after.json \
  --baseline /tmp/before.json
```

标注文件是 JSON 数组，例如
`[{"question":"目标蛋白的分泌变化？","expected_claim_ids":[99]}]`，也支持
expected_document_ids。评估输出类型/Other 分布、同名候选重复实体、证据召回和
可定位引用错误。无人工标签时召回为 null；同名候选不代表已经确定重复。
`--generate-answers` 显式启用真实答案 LLM。原文支持程度需要人工逐项核对，
不能用引用格式有效替代。当前验证范围及续接信息见 [开发状态](docs/WORK_STATE.md)。

实现参考只限相关机制：
[llm_wiki 的内容缓存](https://github.com/nashsu/llm_wiki/blob/main/src/lib/ingest-cache.ts)、
[来源约束与派生页面](https://github.com/nashsu/llm_wiki/blob/main/src/lib/ingest.ts)、
[HyperGraphRAG 的实体/完整事实召回和原文组合](https://github.com/LHRLAB/HyperGraphRAG/blob/main/hypergraphrag/operate.py)。
没有移植其图数据库、桌面应用或代理系统。
