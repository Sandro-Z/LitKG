# 金标准评测运行手册（BioRED / DiMB-RE）

本手册给出从下载金标准到在 QC 中查看 P/R/F1 的完整命令。背景、标签全集和映射分类见 [benchmark-eval-plan.md](benchmark-eval-plan.md)。

金标准评测与 QC 原有评分完全分离：结果存在 `gold_sets` / `gold_evals` 两张不可变表中，版本号为 `GOLD_EVAL_VERSION = qc-gold/1.0.0`，不改变 `machine_dimensions`、场景分、门禁或人工融合。

## 0. 前提

- LitKG 主服务（API、worker、PostgreSQL、Redis）已经运行，并且已经应用本次迁移：

  ```bash
  docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1' \
    < db/migrations/20261121_text_documents.sql
  docker compose up -d --build --no-deps api worker
  ```

- QC 服务已经启动（`docker compose -f docker-compose.qc.yml up -d --build`，默认地址 `http://127.0.0.1:8090`）。
- 凭据只通过已有机制传递：LitKG 使用自己的 `.env`，QC 使用 `QC_LLM_API_KEY_FILE`。本手册中的命令都不读取、不打印凭据。

## 1. 生成金标准数据（prepare）

```bash
python3 benchmarks/prepare.py --all --download          # BioRED train/dev/test + DiMB-RE train/dev/test
python3 benchmarks/prepare.py --bench bc8 --split train --download   # 可选：BC8 训练集（500 篇）
```

输出在 `data/benchmarks/`（已加入 `.gitignore`，不进入 git，也不重新分发）：

| 文件 | 内容 |
|---|---|
| `raw/…` | 官方原始文件（BioRED zip、DiMB-RE 固定 commit 的 JSONL） |
| `documents.<bench>-<split>.json` | QC canonical documents，供 LitKG 和任何外部系统共用 |
| `gold.<bench>-<split>.json` | 实体、关系、句子区间、标签全集、来源 sha256 |
| `manifest.json` | 下载 URL、时间、sha256、统计 |

脚本对 BioRED 的每个 mention 断言 `text[start:end] == mention`，对 DiMB-RE 的每个 span 断言能还原 token 文本，任何不一致都直接报错。

耗时：首次下载约 2.3 MB（BioRED）+ 1.6 MB（DiMB-RE），转换只需数秒。

## 2. 导入 LitKG 新知识库

先建知识库（或在前端创建），再导入纯文本文档：

```bash
curl -s -X POST http://127.0.0.1:8080/knowledge-bases \
  -H 'Content-Type: application/json' -d '{"name":"biored-test"}'
```

```bash
python3 scripts/import_text_documents.py --kb <知识库ID> --file data/benchmarks/documents.biored-test.json
```

```bash
python3 scripts/import_text_documents.py --kb <知识库ID> --file data/benchmarks/documents.dimbre-test.json
```

接口为 `POST /knowledge-bases/{kb_id}/text-documents`，请求体是 documents 数组。上限：每批 `MAX_TEXT_DOCUMENTS=200` 篇，单篇规范化后 `MAX_TEXT_DOCUMENT_BYTES=2 MiB`，请求体 `MAX_TEXT_REQUEST_BYTES=64 MiB`（环境变量可调）。每篇存为 `{sha256}.litkg-text.json`，与 PDF 同目录，按内容 hash 去重；`title`、`external_id` 写入 `documents` 表。

处理流程：`parse_document` → `parse_text_source`（不经过 GROBID，也不做页面图像发现；一个 block 对应一个 chunk，超过 12,000 字符时复用现有切块函数）→ `build_document_catalog` → `extract_chunk`。chunk 记录 `source_block_id` 与 `source_offset`。

## 3. 等待抽取完成

```bash
curl -s "http://127.0.0.1:8080/knowledge-bases/<知识库ID>/batches?limit=10"
```

所有 batch 状态为 `done` 或 `partial` 后再继续。

### 耗时与 token 量估算方法

LitKG 不保存逐请求的 usage，因此先用小样本试跑，再线性外推：

1. 语料规模（由 `prepare.py` 输出计算）：BioRED test 100 篇，共 167,959 字符（单篇最长 2,700）；DiMB-RE test 37 篇，共 76,746 字符（单篇最长 2,944）；DiMB-RE train 139 篇，共 620,750 字符，其中 17 篇超过 12,000 字符，会被切成多个 chunk。
2. 输入 token 的粗估沿用 LitKG 自己的 `token_estimate = len(text) // 3`。每篇摘要至少经过 1 次 catalog 调用和每个 chunk 1 次 extraction 调用；加上提示词、目录和重试，单篇输入约为 `2 × (提示词 token + 文本字符/3)`。提示词 token 用 `app/app/llm.py` 中 `build_extraction_messages` 的渲染结果长度 / 3 估计。
3. 实测：先导入 5 篇（`jq '.[:5]' documents.biored-test.json > pilot.json`），在模型服务端的 usage 统计或网关日志中读取总 token 和耗时，乘以 `总篇数 / 5`。
4. QC 原文评审的 token 可在评审报告的 `usage` 字段中直接读取（QC 客户端会汇总接口返回的 usage）。

## 4. 导入 QC

前端“导入图谱 → 本机 LitKG 实例”，或：

```bash
curl -s -X POST http://127.0.0.1:8090/api/import/litkg \
  -H 'Content-Type: application/json' -d '{"knowledge_base_id": <知识库ID>, "name": "LitKG · biored-test"}'
```

导出中的文本文档带有 `external_id`（例如 `biored:15485686`）和 `source_kind: "text"`。QC 会识别 `{sha256}.litkg-text.json`，把原 block 附加为 `litkg_original` 块，并把该文献标记为 `complete`。这些文本就是完整原文，不是 PDF 渲染结果。

## 5. （可选）QC 原文评审

在“自动评审”页运行“原文 LLM 评审”。建议评审模型与抽取模型不同，以减少自评偏差。只有存在已完成的 LLM 评审时，金标准报告才会计算 QC 校准（混淆表、P(SUPPORTED|TP)、P(SUPPORTED|FP)、逐文档 Spearman）。

## 6. 运行金标准评测

前端“金标准评测”页：

1. 导入 `gold.biored-test.json`（类型 BioRED）或 `gold.dimbre-test.json`（类型 DiMB-RE）。导入后不可修改，相同内容按 hash 去重。
2. 选择图谱、金标准集、映射档案（`litkg` / `pubtator3` / `neo4j-builder`）、对齐模式，以及是否包含“需核对”规则。
3. 点击“运行金标准评测”。比较表列出同一金标准集上的所有评测，并显示金标准 hash 与对齐文档集合是否一致。点击一行可查看分类型指标；“下载 FP / FN 错误样本”导出 claim、证据 quote 和对应金标准。

等价 API：

```bash
curl -s -X POST http://127.0.0.1:8090/api/gold-sets -H 'Content-Type: application/json' \
  --data-binary @<(python3 -c 'import json;print(json.dumps({"kind":"biored","name":"biored-test","payload":json.load(open("data/benchmarks/gold.biored-test.json"))}))')
```

```bash
curl -s -X POST http://127.0.0.1:8090/api/graphs/<graph_id>/gold-evals -H 'Content-Type: application/json' \
  -d '{"gold_set_id":"<gold_set_id>","profile":"litkg","modes":"both","review_levels":["confirmed","needs_review"],"bootstrap":1000}'
```

```bash
curl -s http://127.0.0.1:8090/api/graphs/<graph_id>/gold-evals
```

```bash
curl -s http://127.0.0.1:8090/api/gold-evals/<eval_id>/errors
```

其他接口：`GET /api/gold-sets`、`GET /api/gold-profiles`、`GET /api/gold-evals?gold_set_id=…`、`GET /api/gold-evals/<eval_id>`。

评测是同步计算，只读图谱快照。本机实测：BioRED test 上 id 与 mention 两种模式加 1000 次 bootstrap 约 0.2 秒；BioRED train 约 0.7 秒。

### 报告内容

- `version`、`snapshot_hash`、`corpus_hash`、`policy_hash`、`gold_hash`、`profile.hash`、`review_levels`、`modes`。
- `alignment`：文献对齐按 `external_id` → 规范化标题 → `id` 的顺序进行；报告覆盖率、未对齐数量、各对齐方式的计数，以及 `aligned_gold_documents_hash`（用于判断不同图谱的覆盖范围是否一致）。
- BioRED（按模式 `id` / `mention`）：文档级无序 ID 对；micro P/R/F1 分为仅关系对和关系对加类型两档，并给出各关系类型、各实体对类别、按文档 macro 平均（附 n），以及固定种子 `20261121` 的 1000 次文档级 bootstrap 95% CI。否定或推测 claim 先排除，`claims.negated_excluded` / `speculative_excluded` 记录排除数。端点对齐报告 `resolved/total`、`ambiguous`、`unresolved`、`no_identifier`。
- DiMB-RE（`span` 下分 `strict` / `relaxed`）：claim 先按 `source_refs` 的块偏移（LitKG 文本 chunk 的 `source_offset`）或 quote 定位到金标准句子；端点 strict 要求规范化文本相等，relaxed 要求字符区间重叠；报告关系类型与关系对两档 P/R/F1、各类型指标、已匹配关系的 factuality 准确率（附 n 和混淆表）及句子定位率。双向类型（`INTERACTS_WITH`、`ASSOCIATED_WITH`、`POS/NEG_ASSOCIATED_WITH`）允许主客体互换。
- `rule_hits`：每条映射规则的命中次数，以及 TODO / 审核级别排除的数量。

### 对齐方式的上限（由代码计算）

`benchmarks/mention_upper_bound.py` 直接用金标准构造一张“完美”图谱后再评测（节点 label 取第一个 mention，aliases 取全部 mention，xrefs 取金标准 ID）：

```bash
python3 benchmarks/mention_upper_bound.py data/benchmarks/gold.biored-test.json
```

本机结果：

| 划分 | 模式 | 端点对齐 | 歧义端点 | 关系对召回上限 |
|---|---|---|---|---|
| biored-test | id | 2280/2280 | 0 | 1140/1163 = 0.980 |
| biored-test | mention | 1850/2280 | 430 | 824/1163 = 0.709 |
| biored-dev | id | 2294/2294 | 0 | 1146/1161 = 0.987 |
| biored-dev | mention | 2042/2294 | 252 | 926/1161 = 0.798 |

id 模式的缺口来自 LitKG 无对应谓词的 `Cotreatment/Comparison/Drug_Interaction/Conversion`。mention 模式的缺口来自金标准中“同一名称对应多个 ID”的歧义（例如一个 mention 标注了多个 NCBI Gene ID），按规定计为歧义、不计命中。因此 mention 模式的召回不应与 1.0 比较，而应与这里的上限比较。

## 7. 外部系统如何复用

1. 读取同一份 `data/benchmarks/documents.<bench>-<split>.json`，用外部系统（PubTator3、Neo4j LLM Graph Builder 等）在其中的 `blocks[].text` 上抽取。
2. 产出 QC canonical 图谱：`nodes`（建议带 `xrefs`，例如 `MESH:D003924`、`NCBIGene:6331`）、`claims`（`document_id` 和 `source_refs` 指向原 `id/block_id`，quote 必须是 block 中的逐字片段）。`documents` 原样带入，不要修改，也不要再在 QC 导入时附加原文件，否则 `corpus_hash` 会变化。可参考独立项目 `kg-baselines/`。
3. 在 QC“导入图谱 → 通用 JSON”中上传。
4. 在“金标准评测”页选择同一金标准集和对应档案（`pubtator3` 或 `neo4j-builder`），并排比较。比较表中的“金标准一致”和“覆盖一致”都应为“是”。

## 8. 已知限制

- **mention 对齐的上限**：只做规范化字面匹配（NFKC、casefold、合并空白、去首尾标点），同义改写、缩写展开、指代和上下位概念都无法对齐；同名多 ID 计为歧义。BioRED test 上完美抽取的关系对召回上限为 0.709（见上表）。
- **LitKG 的 id 模式覆盖率**：LitKG 只在原文中出现该标识符时保留 `normalized_id`，摘要正文通常不含 MeSH/NCBI 编号，因此 LitKG 图谱的 id 模式端点覆盖率预计很低。PubTator3 等带标准 ID 的系统更适合 id 模式。
- **DiMB-RE 文本重建**：token 以单个空格拼接（如 `APG - 157`、`curcumin ' s`），与原排版不同；抽取系统看到的是重建文本。JSONL 不含标准化 ID，因此 DiMB-RE 只做 span 级匹配。约 12%（test 48/539）的关系跨句，评测窗口取相关句子的并集。
- **novelty 未评测**；BioRED 的 `Cotreatment/Drug_Interaction/Comparison/Conversion` 在 LitKG 档案中没有对应谓词，只计入召回分母。
- **BC8 测试集**没有公开关系标注，不能作为金标准。
- 映射档案中的 `needs_review` 规则默认参与评测；如需只用确定规则，取消勾选“包含需核对”。TODO 规则始终不参与，命中数在 `rule_hits` 中报告。
