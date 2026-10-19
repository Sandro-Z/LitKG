# LitKG

LitKG 是一个面向生物医学文献的批量知识抽取系统。它可以把一批 PDF
统一摄取到同一个知识库，利用课题组部署的大语言模型抽取化合物、分子、
蛋白、药物、基因等实体和关系，并生成可增量扩展、可追溯到原文证据的
统一知识图谱。

版本改动与历史修复记录见 [更新日志](changelog.md)。

## 项目特性

- 一次上传最多 `MAX_BATCH_FILES` 篇 PDF，并跟踪整批和每篇文献的状态。
- PDF 按 SHA-256 去重；同一文件再次加入其他知识库时直接复用抽取结果。
- 一个部署可维护多个独立知识库。
- 删除知识库时，彻底删除不再属于其他库的文献及其处理缓存、抽取结果、历史和索引；
  再次上传相同 PDF 会重新解析、生成目录并抽取关系。其他库仍在使用的文献保留，
  删除最后一个所属库时才彻底清理。原始 PDF 文件保留在磁盘，不作为处理完成缓存。
- 实体先规范化再写入全局实体表，跨文献出现的同一实体只形成一个节点。
- 相同主语、谓词、宾语、限定条件和极性的关系合并成一条边。
- 每条 claim 和图谱证据保留标题、DOI、PDF 页码、原文句子、章节和文件名。
- 可按实体名称、别名、标准 ID、主语/宾语和关系类型检索知识图谱。
- 每条聚合边保留支持文献数、证据数和置信度。
- 新文献处理完成后直接加入现有图谱，不需要全量重建。
- Celery 重投递或手动重跑同一 chunk 时不会累积重复 claim。
- 支持 `POST /documents`、单篇 claims 和单篇 graph 接口。
- 全文实体目录覆盖所有已解析 chunks，保留别名、标准 ID、位置和歧义；目录完成后再抽取关系。
- 完整事实保留过程、参与者、条件、主证据和补充证据，支持知识浏览和待复核项管理。
- 证据问答结合实体与事实检索、可选语义召回和 0–2 跳扩展，回答逐项关联原文引用。
- 目录和关系抽取支持长文本分段、截断拆分、成功分段缓存与失败续接；
  无原文依据的候选进入复核，不作为成功结果静默接受。
- 完成度与结果质量独立统计：局部候选异常保留 `done` 并提示数量，真实处理缺口使用 `partial/failed`。

处理流程：PDF → 全文目录 → chunk 关系 → 完整事实 → 派生索引/摘要。

### 数据与证据规则

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

有 ID 的身份键为 `身份类别 + 标准 ID`，身份性的物种、突变、修饰位点和异构体信息也参与区分；无 ID 时为
`身份类别 + 规范化名称`。`compound 1` 等没有全局身份的文献内编号按
文献隔离，即使带有 ID，也不会仅凭局部编号推断跨数据库 ID 等价。

合并会保留数字、物种、突变、修饰、异构体和化学立体构型信息。
同一数据库命名空间的不同 ID 不自动合并；含歧义的短名称不据此合并
已有节点。不同命名空间的 ID 仅在唯一名称匹配、且无已知 ID 冲突时附加。
旧节点重建时还可合并规范化名称一致、ID 不冲突的重复节点。
合并在事务中迁移关系、别名和所有证据，不修改原始 `claim_json`。
错误目录 ID 可按完整原文提及重新匹配；无法唯一绑定时保留 `pending_merge` 文献内独立节点。
名称同段共现不证明别名；未确认的身份不参与全局别名归并。

LLM 只能填写片段明确给出的标准 ID，未知 ID 必须为 `null`。
实体规范化不调用外部实体链接服务，也不上传文献。

关系的唯一键由以下字段组成：

`subject_entity + predicate + object_entity + qualifiers + negated + speculative`

二元关系键还包含参与者条件；不同时间、物种、细胞系、过程、否定和推测
不会合成同一结论。完整事实保留逐来源断言、参与者和全部补充证据。
同一关系可连接多条 `relation_evidence`。图谱查询会返回
`document_count`、`evidence_count` 和 `evidence_samples`，不会因为合并而
丢失出处。

全文目录识别覆盖文章标题、章节标题和已解析正文中的图表、列表及附录，
参考文献列表不作为本文实验事实。扫描 PDF 需要文字层或 OCR。
目录跨段仅按明确身份和唯一别名/缩写归并；冲突 ID 和多义缩写保持分离。
关系必须由当前 chunk 支持，目录漏项和不可靠的过程拆分进入复核。
实体类型通过 `GET /entity-types` 获取；未知类型保留 Other 和原因。

`Proteomics` 分支保留原有生物医学实体，扩展肽、蛋白质形式、蛋白推断组、
家族/结构域、修饰/位点、蛋白组、亚细胞位置、样本与研究组。
`GET /predicate-types` 提供共享受控关系、过程及证据类型；丰度差异、相关、预测与
修饰机制分别表达，保留位点、异构体、定量方法和推断歧义。
详见 [蛋白质组学词表与迁移说明](docs/proteomics-schema.md)。复用现有模型 API，
不新增依赖或 SQL 迁移；已有文献需 `reextract` 才能使用新词表，DOI 补齐需重新解析 PDF。

原始模型输出和提及会保留；页码由服务器定位，不能用模型生成的页码代替。
重新解析或替换前保存 claim_history，派生索引和摘要可重建，不能替代原文证据。
仅当前目录版本的抽取进入当前图谱或问答；历史记录需要按维护说明重新抽取。

## 使用方法

以下 Docker 示例使用 API 地址 `http://localhost:8080`。知识库、文献、批次和实体 ID
均以实际响应为准，示例 ID 仅用于展示调用方式。

### 快速启动

在仓库根目录创建或更新 `.env`，填写实际的数据库和模型服务配置：

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

# 实体密集文献可采用较小分段；按模型服务容量调整输出预算
CATALOG_SEGMENT_CHARS=3000
CATALOG_MIN_SEGMENT_CHARS=500
CATALOG_MAX_OUTPUT_TOKENS=4096
CLAIMS_SEGMENT_CHARS=3000
CLAIMS_MIN_SEGMENT_CHARS=500
CLAIMS_MAX_OUTPUT_TOKENS=8192
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

### 数据库初始化与升级

全新 PostgreSQL volume 会自动执行 `db/init.sql`。已有 volume 不会再次
运行 Docker 初始化脚本；先备份目标库，再执行幂等迁移：

```bash
docker compose cp db/init.sql postgres:/tmp/litkg-init.sql
docker compose exec postgres sh -lc \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 --single-transaction -f /tmp/litkg-init.sql'
```

已有数据库还需执行处理租约表的增量迁移：

```bash
docker compose exec -T postgres sh -lc \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 --single-transaction' \
  < db/migrations/20261017_processing_leases.sql
```

代码或环境变量更新后重建并重新创建应用容器；仅 `restart` 不会重新读取 `.env`：

```bash
docker compose up -d --build --force-recreate --no-deps --timeout 330 api worker_parse worker_extract frontend
```

迁移后，已有 claims 可通过下方的重建或 `reproject` 维护更新投影；
需要全文实体目录及过程语义时使用 `reextract`。仅重建图谱不会补齐解析元数据。
各版本的迁移背景、部署脚本及历史修复说明见 [更新日志](changelog.md)。

### API 示例

#### 1. 创建知识库

```bash
curl -X POST http://localhost:8080/knowledge-bases \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Kinase literature",
    "description": "课题组激酶相关文献"
  }'
```

#### 2. 批量导入文献

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

#### 3. 查询批次状态

```bash
curl http://localhost:8080/ingestion-batches/1
```

批次状态可能为：

- `queued`：已登记，等待 worker；
- `processing`：至少一篇正在解析或抽取；
- `done`：所有有效输入均成功；
- `partial`：存在真实处理缺口，或部分输入文件失败/被拒；目录和关系候选隔离单独统计；
- `failed`：没有文献成功完成。

单篇文献还可能处于 `parsing`、`extracting` 或 `partial`。部分 chunk 失败时显示 `partial`。
全文片段处理完成后，个别候选被拒、绑定失败或待复核仍为 `done`，并保留警告与数量。

#### 4. 查询统一图谱

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

#### 5. 浏览知识库内容

```bash
curl "http://localhost:8080/knowledge-bases/2/documents?limit=100"
curl "http://localhost:8080/knowledge-bases/2/claims?predicate=BINDS_TO"
curl "http://localhost:8080/knowledge-bases/2/entities?query=EGFR"
curl "http://localhost:8080/knowledge-bases/2/batches"
```

所有列表接口都支持有界的 `limit`；claims、entities 和文献列表支持
`offset`，便于大知识库分页。

claims 的知识库接口和单篇接口均包含以下出处字段：

- `title`、`doi`：当前文献的标题和 DOI。DOI 优先使用 GROBID 文献头、PDF 元数据，
  缺失时检索 GROBID 全文及链接目标，再检索 PDF 所有页的正文、附录和补充材料；
  GROBID 页码完整时也会补查缺失 DOI。排除可识别的参考文献、数据集和代码 DOI，
  支持 DOI 链接、裸 DOI、常见换行及尾随标点；多个候选优先采用明确标为本文的 DOI，
  仍有歧义则返回 `null`。更新后已有文献需重新解析 PDF 才能补齐缺失的 DOI。
- `page`、`page_end`：证据所在的 PDF 起止页，按 PDF 页序号从 `1` 开始，
  与期刊印刷页码可能不同。优先使用句子坐标，缺失时与本地 PDF 逐页文字
  匹配；句子跨页、重复出现在多页或只能确定片段范围时保留已知页码范围。
- `section`、`table_or_figure`：证据章节和图表标记。
- `document_id`、`filename`、`chunk_id`：用于追溯对应文献和片段。

标题、DOI 或页码缺失时返回 `null`。页码由解析器确定，模型输出的页码
不作为来源。图谱和关系检索接口的 `evidence_samples` 同样包含这些出处信息。
Streamlit 的 Claims 表格、图谱证据提示和关系检索结果可直接查看出处。

#### 6. 检索实体与关系

实体检索使用 `GET /knowledge-bases/{id}/entities`，按名称、别名或标准数据库
ID 做大小写不敏感的子串匹配。关系检索使用
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

### 证据问答与可选语义检索

检索分别召回实体与完整条件事实，结合文本匹配和可选语义召回，再沿参与者关联
进行 0–2 跳有界扩展。引用直接回查当前知识库的事实和证据，组装原文上下文；
不会把派生摘要当成原文。答案逐项引用；引用 ID 无效或缺失时返回原文证据及错误提示。
没有证据时返回 insufficient_evidence；生成失败返回 evidence_only，不伪装为正常回答。
LLM 是否正确理解证据仍需实际评估，引用有效不等于语义蕴含已由程序证明。

```bash
curl -X POST http://localhost:8080/knowledge-bases/2/ask \
  -H 'Content-Type: application/json' \
  -d '{"question":"该处理对目标蛋白的分泌有何影响？","hops":1,"limit":12,"generate":true}'
```

响应含 answer、statements、citations、facts 和 warnings。citations 包含文献 ID、
标题、文件名、DOI、PDF 起止页、chunk、原文句子、上下文、条件、否定和推测。
未能定位的 DOI/页码仍为 null。`generate=false` 只检索展示证据，不调用答案 LLM。
Streamlit 提供“证据问答”和“知识浏览与维护”入口。

Embedding 默认关闭；启用时使用独立配置：

```dotenv
EMBEDDING_BASE_URL=http://your-embedding-server/v1
EMBEDDING_MODEL=your-embedding-model
EMBEDDING_API_KEY=EMPTY
# 同名模型权重或提示策略更新时可改变此值以失效抽取缓存
LLM_REVISION=2026-10-16
```

更换 embedding 端点/模型后运行 derived 维护。向量保存在现有 PostgreSQL pgvector，
仅比较同一端点/模型且维度相同的向量。嵌入失败有 warnings，并保留文本检索。
实体/事实索引按内容及处理版本缓存；变更只刷新相关文献及其原有/新增实体。
配置的模型服务会接收所需原文；仅使用适合该文献数据访问范围的端点。

### 知识浏览与维护

示例中的知识库 ID `2`、文献 ID `31` 均需替换为实际 ID。
先通过 `GET /knowledge-bases` 查询知识库，不要假定 default 的 ID 一定为 `1`。

```bash
# 无需抽取 LLM：从已有记录重建实体/完整事实；不会恢复旧记录未保存的过程语义
curl -X POST http://localhost:8080/knowledge-bases/2/maintenance \
  -H 'Content-Type: application/json' \
  -d '{"mode":"reproject","document_id":31}'

# 需要抽取 LLM：用全部已保存原文建立目录，再重新抽取所有 chunks
curl -X POST http://localhost:8080/knowledge-bases/2/maintenance \
  -H 'Content-Type: application/json' \
  -d '{"mode":"reextract","document_id":31}'

# 失败后续接；已经成功的分段复用内容与处理版本缓存
curl -X POST http://localhost:8080/knowledge-bases/2/maintenance \
  -H 'Content-Type: application/json' -d '{"mode":"retry","document_id":31}'

# 只更新索引和摘要；省略 document_id 时维护该知识库的文献
curl -X POST http://localhost:8080/knowledge-bases/2/maintenance \
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

`retry` 复用已完成目录、分段和有效关系，只续接失败部分及待解决候选；确认排除的候选不再纠错。
纠错支持 repaired（修复成功）、excluded（确认无效并排除）、unresolved（仍待解决），
保留原候选、纠错输出及人工复核历史。空响应不能代替明确结案。
`reextract` 会重新建立全文目录并调用 LLM 重抽所有 chunks。
`derived` 和 `reproject` 不会续接模型抽取。按返回的 `status_url` 查询最终结果，
并通过文献的 `/processing` 查看 chunks、兼容的 `catalog_quality` 及 `processing_quality`。
文献列表、批次明细和前端同步显示 `rejected_candidates`、`excluded_candidates`、
`repair_count`（修复记录数）和 `pending_reviews`；这些质量数量不改变完成度。
`catalog_status=done` 不代表没有被隔离的候选，`needs_review` 表示需要复核。

### 图谱诊断与重建

实体被拆成多个名称节点只是碎片化的一个来源。图谱的 `limit` 和筛选条件
也会隐藏连接边；主题不同、原文没有连接事实的实体则应保留独立分量。
系统不会为了提高连通率添加未经文献支持的边或强行合并相似名称。

`GET /knowledge-bases/{id}/graph/diagnostics` 统计完整知识库，不受显示数量
和图谱筛选条件影响，返回连通分量数、最大分量实体数及占比、主图外实体数、
独立实体数、自环关系数，以及分量大小分布。连通性按无向连接计算；
仅有自环的节点算独立实体；小分量定义为不超过 `5` 个节点。
普通 graph 响应的 `summary` 也包含相同指标，但只针对当前显示子图。

完成数据库迁移后，可以从已有 claims 重建图谱，无需重新上传 PDF 或重新调用 LLM：

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

### 修复文献 PDF 页码

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
若没有文字层，需要先做 OCR。该命令不新增数据库字段。

### 单篇文献接口

```bash
curl -X POST http://localhost:8080/documents \
  -F "file=@single-paper.pdf"

curl http://localhost:8080/documents/1
curl http://localhost:8080/documents/1/claims
curl http://localhost:8080/documents/1/graph
```

单篇上传会在 `default` 知识库中创建一个单项批次。

### 本机临时运行

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
