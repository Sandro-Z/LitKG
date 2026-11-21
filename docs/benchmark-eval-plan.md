# BioRED / DiMB-RE 金标准评测：调研与实施计划

状态：阶段 0 调研结论（2026-11-21）。本文件中的所有计数均由脚本直接从下载文件统计得到；标签全集逐项附出处。数据集文件本身不进入 git（见 `data/benchmarks/`，已被根 `.gitignore` 的 `data/` 规则覆盖）。

## 1. 代码事实核对（与任务描述的差异）

| 任务描述 | 实际代码 | 处理 |
|---|---|---|
| QC 仅标准库 + SQLite，端口 8090 | 一致（`server.py` 默认 `QC_PORT=8090`、`QC_BIND=127.0.0.1`） | — |
| `normalize()` 接受 claims/nodes/documents/policy/metadata 并计算 `snapshot_hash` | 一致；documents 深拷贝后只改写 `id`、`blocks[].id`，补 `parsing_status`，**其余字段原样保留**（`external_id` 会被保留） | 无需改 `models.py` |
| `engine.py` 中 `corpus_hash=digest(graph['documents'])`、`policy_hash=digest(graph['policy'])` | 一致（`engine.evaluate`） | 金标准报告复用同一 `digest` |
| 报告声明“无人工金标准…” | 一致（`engine.evaluate` 的 `limitations`） | 不改；金标准结果单独存储、单独展示 |
| `SCORING_VERSION` / `PROMPT_VERSION` | `qc-scoring/2.0.0` / `qc-evidence/2.0.0` | 新增独立 `GOLD_EVAL_VERSION`，不动上述两者 |
| QC 测试命令 | `quality_control/tests/` 不是包，须用 `python3 -m unittest discover -s quality_control/tests`（加 `-t .` 会失败） | 新测试按同一方式组织 |
| `POST /api/import/litkg` 附加 pdftotext 页块 | 一致；但 `sources.attach_litkg_sources` 只查找 `{sha256}.pdf`，纯文本源会被标为 `missing` 并触发“原文件无法读取”限制说明 | 阶段 2 让它识别 `{sha256}.litkg-text.json`，按原 block 附加 `litkg_original` 块 |
| LitKG 输入只有 PDF | 一致：`ingestion.store_pdf/register_document` → `tasks.parse_document` → `vlm.parse_sources(source_path, parse_pdf_to_chunks)` | 阶段 2 在 `parse_document` 中按源文件后缀分派 |
| `quality_export` 导出 claims/documents/ontology | 一致；documents 字段：`id,title,source_sha256,parsing_status='partial',generation,blocks[id,text,page,page_end,section,page_spans,kind]` | 增加 `external_id`、`source_kind`，block 增加 `source_block_id/source_offset` |
| `evaluation.py` 中 recall / citation_error_rate 为 None | 一致 | 不改（LitKG 内部指标与 QC 金标准评测分离） |
| LitKG 测试命令 | `PYTHONPATH=app python -m unittest discover -s tests -v`；基线 467 个测试通过，其中 128 个需要 `LITKG_TEST_DATABASE_URL`（一次性 PostgreSQL）或 streamlit，未设置时跳过 | 新测试同样分为纯单元测试和需数据库的集成测试 |
| `page_repair.repair_document_pages` | 直接对 `source_path` 调 `fallback_pdf_text`，对纯文本源会失败 | 阶段 2 对文本源直接跳过页修复 |

### LitKG 实体 ID 字段的真实格式

- claim 的 `claim_json.subject/object` 为 `schemas.Entity`：`text, type, normalized_id, directory_id, mention, uncertainty_reason, process, pending_merge`。导入 QC 后，`normalize()` 对无显式 `id` 的端点按 `(text, types)` 生成 `text-<hash>` 节点 ID，节点保留上述字段，因此 **`normalized_id` 出现在 QC 节点上**；行级的 `subject_normalized_id / object_normalized_id` 保留在 `claim.original` 中。
- `normalization.normalize_external_id` 只把 5 类前缀规范化为 `NS:LOCAL`：`CHEBI:<int>`、`PubChem:<int>`、`UniProt:<ACC>`、`NCBIGene:<int>`、`DrugBank:DB<digits>`；其他值仅去除冒号两侧空白（例如 `MESH:D003924`、`mesh : D003924` → `mesh:D003924`）。
- **关键限制**：`catalog.py` 只在原文中能找到该标识符（`source_has_identifier`）时保留 `normalized_id`，否则在 repair 中置空。BioRED/DiMB-RE 摘要正文几乎不含 MeSH/NCBI Gene 编号，因此 LitKG 图谱在 `id` 对齐模式下的端点覆盖率预计接近 0，`mention` 模式是 LitKG 的主要可比口径；PubTator3 等带标准 ID 的外部系统可同时使用两种模式。

## 2. 数据集实测

### 2.1 BioRED

来源：`https://ftp.ncbi.nlm.nih.gov/pub/lu/BioRED/`（README 声明 Public Domain，“United States Government Work”）。

| 文件 | sha256 |
|---|---|
| `BIORED.zip` | `c3032230bd89d22a0923d0df6ae943bc8ea37fba7e42dafa7a8dec21bac02d47` |
| `README.txt` | `ceffff95c61b4c47319b50969052211ab0976c73e8cbf816e23a93ea1173e8e7` |
| `BioRED_Annotation_Guideline.pdf` | `e2cf33a9fab76124351c609d3b5fd704e3a8a64fcad2fb8b48227c2aa92feb22` |
| `BioRED/Train.PubTator` | `472cf3a38aa1732c08918e561a86a28d331b80ecddc7f77b3de02cc705181b81` |
| `BioRED/Dev.PubTator` | `504f8c8be806606d519b6c360f3164ff89dbfa5610f603bafe662ab2605bca76` |
| `BioRED/Test.PubTator` | `91d478f1e67d310f49d7797e3ea731c97ac1d8f6275245fde5b9dd12c4551c16` |

划分规模（从 PubTator 文件统计）：

| 划分 | 文档 | 实体 mention | 关系 |
|---|---|---|---|
| train | 400 | 13,351 | 4,178 |
| dev | 100 | 3,533 | 1,162 |
| test | 100 | 3,535 | 1,163 |

实体类型全集（数据文件第 5 列；与指南 “(1)–(6)” 一致）：`GeneOrGeneProduct`、`DiseaseOrPhenotypicFeature`、`ChemicalEntity`、`OrganismTaxon`、`SequenceVariant`、`CellLine`。

关系类型全集（数据文件关系行第 2 列，train∪dev∪test）：`Association`、`Positive_Correlation`、`Negative_Correlation`、`Bind`、`Cotreatment`、`Comparison`、`Conversion`、`Drug_Interaction`。
- 指南 “Guideline of the relation types / Table 4” 的非方向类型列还出现 `Cause`（Variant–Disease），但三个数据划分中 **没有任何 `Cause` 关系**；`Conversion` 出现在数据（train 3、test 1）中，但指南表 4 未给定义。
- 关系为文档级、无方向的实体 ID 对（数据行：`PMID  Type  ID1  ID2  Novelty`，5 列）；指南 “Co-occurrence in the same sentence is not required”。

Novelty 取值（关系行第 5 列）：`Novel`、`No`（指南 “Guideline of the novel triage”）。本评测不评 novelty。

ID 命名空间（指南 Table 2 及数据实测）：
- Gene：NCBI Gene 纯数字（如 `6331`）；一个 mention 可有多个 ID，用逗号分隔（如 `1543,1548,1565,1571`）。
- Disease：MEDIC，即 MeSH（`D######`、`C######`）优先，少量 `OMIM:######`。
- Chemical：MeSH（`D…`/`C…`）；无法规范化时为 `-`。
- Species：NCBI Taxonomy 纯数字（如 `9606`）。
- Variant：dbSNP `rs…`，否则 tmVar 组件串（如 `p|SUB|V|1763|M`、`c|SUB|G|CODON1763|A`）。
- CellLine：Cellosaurus `CVCL_…`；不匹配时为 `-`。
- 实测：三个划分中关系行引用的 ID 全部能在同文档某个 mention 的 ID 集合（按逗号拆分）中找到（缺失数 0）。

文本拼接：PubTator 偏移以 `title + " " + abstract` 为坐标系（abstract 偏移 = 标题长度 + 1）。阶段 1 对每个 mention 断言 `text[start:end] == mention_text`。

BioCreative VIII（`https://ftp.ncbi.nlm.nih.gov/pub/lu/BC8-BioRED-track/`）：
- `BC8_BioRED_Subtask1_PubTator.zip`（sha256 `af4e46bd…5404cd`）含 `bc8_biored_task1_train.pubtator`（500 篇，5,340 条关系）和 `bc8_biored_task1_val.pubtator`（1,000 篇，**0 条关系**，只有实体）。
- `BC8_BioRED_Subtask1_Test_Set.zip`（sha256 `d2e3d390…7d1abbfd7979`）含 10,000 篇测试文档，**0 条关系**（金标准未公开发布）。
- 结论：BC8 测试集不能作为金标准；`prepare.py` 支持 `bc8-train`，对无关系的文件直接报错，不生成空金标准。

### 2.2 DiMB-RE

来源：`https://github.com/ScienceNLP-Lab/DiMB-RE`，commit `03012c98dfcce0e66a7409469f8382799591d6e6`（2025-02-17）。仓库根目录**没有许可证文件**（只有子目录 `PL-Marker/LICENSE` 属于第三方代码），因此仅本地下载、记录 sha256，不重新分发。

使用 README 推荐的 `data/DiMB-RE/ner_reduced_v6.1_trg_abs_result/`：

| 划分 | sha256 | 文档 | 关系 | 跨句关系* |
|---|---|---|---|---|
| train | `5a69edfa7db7ee4013000030cd44faaf6ca8b8e77475be0a2bc1f4b01e56d3cb` | 139 | 3,536 | 438 |
| dev | `ddcd739d34615bb752f26279fff231e293ef9b7849fbf15aea662c79dcb020fc` | 19 | 285 | 29 |
| test | `8a9d7d1ab7be155a1702a3d27932afafe829d301d975f4d663df22e0ed1475dc` | 37 | 539 | 48 |

\* 关系存放在第 i 句的列表中，但 head 或 tail 的 token 不在第 i 句内。`ner_reduced_v6.1_trg_abs`（仅摘要）的 dev/test 与上表文件字节相同，train 为 109 篇。

字段：`doc_key, sentences, ner, triggers, relations, triplets`（数据实测与 README 一致）。`ner` 为 `[start, end, type]`，start/end 为**文档级、闭区间** token 下标；`relations` 为 `[h_start, h_end, t_start, t_end, type, factuality]`，方向为 Agent→Theme；实测所有关系端点 span 都在 `ner` 中。JSONL 中**没有标准化 ID**（`brat_v2` 的 BRAT 文件含 9,796 行 `N` 归一化记录，未纳入本期）。

实体类型全集（`ner` 第 3 列，全部划分）：`Nutrient`、`Food`、`DietPattern`、`Metabolite`、`Microorganism`、`Physiology`、`Measurement`、`Chemical`、`Disease`、`Gene`、`Enzyme`、`DiversityMetric`（12 类；README 称论文标注了 15 类，`ner_reduced` 为缩减后的集合，例如 BRAT 中的 `Population` 不在其中）。

关系类型全集（`relations` 第 5 列）：`INCREASES`、`DECREASES`、`AFFECTS`、`IMPROVES`、`WORSENS`、`CAUSES`、`PREVENTS`、`PREDISPOSES`、`INTERACTS_WITH`、`ASSOCIATED_WITH`、`POS_ASSOCIATED_WITH`、`NEG_ASSOCIATED_WITH`、`HAS_COMPONENT`（13 类，与 README “13 relation types” 一致）。定义见 `PURE/icl/prompts/desc_guid_1shot_021424.txt`。其中 `INTERACTS_WITH`、`ASSOCIATED_WITH`、`POS/NEG_ASSOCIATED_WITH` 是双向类型（同文件说明）。

Factuality 取值（`relations` 第 6 列）：`Factual`、`Negated`、`Unknown`（与 `PURE/icl/verbalizer.json` 的三种模板一致：断言 / “do(es) not” / “may”）。

文本重建：JSONL 只有分词结果。阶段 1 用单个空格拼接 token（句间也用单个空格），记录每个 token 的字符区间和每句的字符区间。**重建文本与原排版不同**（例如 `APG - 157`、`curcumin ' s`）。

## 3. 映射草案

映射档案放在 `quality_control/gold_profiles/*.json`；每条规则带 `review` 字段：

- `confirmed`（确定）：官方定义可直接对应。
- `needs_review`（需核对）：语义接近但边界不同，默认参与评测，报告中逐条计数。
- `TODO`：没有把握，**默认不应用**，命中次数单独报告。
- `unsupported`：明确无对应，被排除并计数。

评测请求可以用 `review_levels` 只启用 `confirmed`。

### 3.1 LitKG 谓词 → BioRED 关系类型

| 分类 | LitKG 谓词（+ 条件） | BioRED | 依据 |
|---|---|---|---|
| 确定 | `CORRELATED_WITH` + `association_direction=positive` | `Positive_Correlation` | LitKG 定义“sign belongs in association_direction”；BioRED 名称即相关符号 |
| 确定 | `CORRELATED_WITH` + `association_direction=negative` | `Negative_Correlation` | 同上 |
| 确定 | `ASSOCIATED_WITH` | `Association` | 两者均为无机制、无方向关联 |
| 确定 | `BINDS_TO` | `Bind` | 指南表 4：Gene–Gene Bind、Chemical–Gene Receptor → Bind |
| 需核对 | `TREATS` | `Negative_Correlation` | 指南表 4 Chemical–Disease “Treatment → Negative_Correlation”；LitKG TREATS 主语还可为 Intervention/Diet |
| 需核对 | `CAUSES` | `Positive_Correlation` | 指南表 4 Chemical–Disease “Induce → Positive_Correlation”；Variant–Disease “Cause” 未出现在数据中 |
| 需核对 | `PREVENTS` | `Negative_Correlation` | 指南 Disease–chemical：“drops down the susceptibility” |
| 需核对 | `CORRELATED_WITH` + `association_direction=unspecified` | `Association` | BioRED：无法归入正/负相关时用 Association |
| 需核对 | `INTERACTS_WITH` | `Association` | 指南表 4 Gene–Gene “Regulation/Modification → Association”，但 LitKG 的定义更窄 |
| 需核对 | `ACTIVATES` | `Positive_Correlation` | 指南表 4 Gene–Gene Upregulation、Chemical–Chemical Increase |
| 需核对 | `INHIBITS` | `Negative_Correlation` | 指南表 4 Gene–Gene Downregulation、Chemical–Chemical Inhibition |
| 需核对 | `AFFECTS` + `object.process.direction=increased` | `Positive_Correlation` | 同上，作用于对象方面 |
| 需核对 | `AFFECTS` + `object.process.direction=decreased` | `Negative_Correlation` | 同上 |
| TODO | `AFFECTS`（其他方向） | `Association`? | 不确定是否满足 BioRED “explicit statements” 要求 |
| TODO | `TRANSFORMS` | `Conversion`? | 指南未定义 Conversion |
| TODO | `TARGETS` | `Bind` 或 `Association`? | 语义不确定 |
| 不支持 | `SUBCLASS_OF` | — | 指南：“DO NOT annotate the is_a association” |
| 不支持 | `MEMBER_OF, PART_OF, DIFFERS_BETWEEN, PRODUCES, CONSUMES, ENCODES, CATALYZES, COLONIZES, CROSS_FEEDS, COMPETES_WITH, INFECTS, PREDICTS, MEASURED_BY, SAMPLED_FROM, LOCATED_IN, PARTICIPATES_IN` | — | BioRED 无对应类型 |
| 不支持（BioRED 侧） | — | `Cotreatment, Drug_Interaction, Comparison, Conversion` | LitKG 无对应谓词；这些 gold 关系仍计入“关系对”召回分母，并在按类型结果中单独列出 |

### 3.2 LitKG 实体类型 → BioRED 实体类型（用于 mention 查词典时的类型约束和实体对类别）

| 分类 | LitKG | BioRED |
|---|---|---|
| 确定 | `Gene`, `Protein` | `GeneOrGeneProduct` |
| 确定 | `Disease` | `DiseaseOrPhenotypicFeature` |
| 确定 | `Compound`, `Drug`, `Metabolite` | `ChemicalEntity` |
| 确定 | `Organism`, `MicrobialTaxon` | `OrganismTaxon` |
| 确定 | `Mutation` | `SequenceVariant` |
| 确定 | `CellLine` | `CellLine` |
| 需核对 | `Phenotype` | `DiseaseOrPhenotypicFeature` |
| 需核对 | `MicrobialStrain` | `OrganismTaxon` |
| TODO | `DietaryComponent`, `ClinicalOutcome`, `RNA` | ? |
| 不支持 | 其他 LitKG 类型 | — |

### 3.3 LitKG 谓词 → DiMB-RE 关系类型

| 分类 | LitKG 谓词（+ 条件） | DiMB-RE | 依据（DiMB-RE 定义文件） |
|---|---|---|---|
| 确定 | `CORRELATED_WITH` + positive | `POS_ASSOCIATED_WITH` | “Positive correlation” |
| 确定 | `CORRELATED_WITH` + negative | `NEG_ASSOCIATED_WITH` | “negative correlation” 示例 |
| 确定 | `ASSOCIATED_WITH` | `ASSOCIATED_WITH` | “An unspecified relationship, a correlation” |
| 确定 | `INTERACTS_WITH` | `INTERACTS_WITH` | “Acts, functions, or operates together with” |
| 确定 | `PREVENTS` | `PREVENTS` | “Stops, hinders or eliminates” |
| 确定 | `CAUSES` | `CAUSES` | “Brings about a condition or an effect” |
| 需核对 | `CORRELATED_WITH` + unspecified | `ASSOCIATED_WITH` | — |
| 需核对 | `AFFECTS` + `object.process.direction=increased` | `INCREASES` | “Make greater in size, amount…” |
| 需核对 | `AFFECTS` + `object.process.direction=decreased` | `DECREASES` | “Make smaller…” |
| 需核对 | `AFFECTS`（unspecified/unchanged/无 process） | `AFFECTS` | “The nature of the effect is not explicit” |
| 需核对 | `PART_OF`（主客体互换） | `HAS_COMPONENT` | “one entity is a constituent part of another” |
| TODO | `TREATS` | `IMPROVES`? | IMPROVES 描述“质量/状态改善”，不等同治疗 |
| TODO | `ACTIVATES` / `INHIBITS` | `INCREASES` / `DECREASES`? | LitKG 要求显式机制 |
| 不支持 | 其余 LitKG 谓词 | — | — |
| 不支持（DiMB-RE 侧） | — | `WORSENS`, `PREDISPOSES`, `IMPROVES` | LitKG 无对应谓词 |

Factuality（需核对）：`negated=true` → `Negated`；`speculative=true` 或 `qualifiers.modality` ∈ {`speculative`,`hypothesis`,`possible`} → `Unknown`；其他 → `Factual`。

### 3.4 LitKG 实体类型 → DiMB-RE（仅用于报告，不参与 span 匹配）

需核对：`Diet`→`DietPattern`；`DietaryComponent`→`Nutrient`/`Food`；`MicrobialTaxon/MicrobialStrain/MicrobialCommunity`→`Microorganism`；`Metabolite`→`Metabolite`；`Compound/Drug`→`Chemical`；`Disease`→`Disease`；`Gene`→`Gene`；`Protein`→`Enzyme`；`DiversityMetric`→`DiversityMetric`。TODO：`Physiology`、`Measurement`（LitKG 无直接类型）。

### 3.5 外部系统档案

- `pubtator3.json`：节点 `xrefs` 为 PubTator 概念 ID；规则既覆盖 LitKG 谓词（baselines/kgb 已映射到 policy 谓词），也覆盖 `qualifiers.original_predicate` 中的 PubTator 原始关系类型（`positive_correlate` 等，在 baselines/docs/pubtator3_api_notes.md 中核实 API 后才能从 TODO 升级）。
- `neo4j-builder.json`：节点无标准 ID，只能用 `mention` 模式；谓词规则同 LitKG。

## 4. 评测口径（摘要）

- 文献对齐优先级：`external_id` → `title`（NFKC+casefold+合并空白后精确相等）→ `id`。只评对齐上的文档；报告未对齐数量与覆盖率。
- BioRED：文档级无序 ID 对；micro P/R/F1（仅对、对+类型、按类型、按实体对类别）、按文档 macro、文档级 bootstrap 95% CI（1000 次，固定种子）。否定或推测 claim 排除并计数。预测对只来自两端都对齐到金标准实体的 claim；端点对齐覆盖率和歧义数一并报告。
- DiMB-RE：以 claim `source_refs` 的偏移或 quote 定位金标准句子；端点 strict（规范化文本相等）/ relaxed（字符区间重叠）两档；关系类型 P/R/F1；已匹配关系的 factuality 准确率。
- QC 校准：仅当图谱已有完成的 QC LLM 运行时计算混淆表、P(SUPPORTED|TP)、P(SUPPORTED|FP) 和逐文档 Spearman 相关。

## 5. 改动文件清单（计划）

| 文件 | 改动 |
|---|---|
| `.gitignore` | 显式加入 `data/benchmarks/` |
| `benchmarks/prepare.py` | 新增：下载/读取官方文件，生成 `documents.*.json`、`gold.*.json`、`manifest.json` |
| `benchmarks/tests/test_prepare.py` | 新增：手写 PubTator/DiMB-RE 夹具，验证偏移 |
| `db/init.sql`、`db/migrations/20261121_text_documents.sql` | 新增 `documents.external_id`、`chunks.source_block_id`、`chunks.source_offset` |
| `app/app/ingestion.py` | 新增 `store_text_document`；`register_document` 支持 `title/external_id` |
| `app/app/parser.py` | 新增 `parse_text_source` |
| `app/app/tasks.py` | `parse_document` 按源类型分派；写入新列 |
| `app/app/page_repair.py` | 文本源跳过页修复 |
| `app/app/main.py`、`app/app/schemas.py` | 新增 `POST /knowledge-bases/{id}/text-documents` |
| `app/app/quality_export.py` | documents 增加 `external_id/source_kind`，blocks 增加来源偏移 |
| `scripts/import_text_documents.py` | 新增 CLI |
| `tests/test_text_documents.py` | 新增 |
| `quality_control/sources.py` | 识别 `{sha256}.litkg-text.json` |
| `quality_control/gold.py`、`quality_control/gold_profiles/*.json` | 新增金标准模块与映射档案 |
| `quality_control/store.py`、`server.py`、`static/*` | `gold_sets/gold_evals` 表、API、“金标准评测”页 |
| `quality_control/tests/test_gold.py` | 新增夹具测试 |
| `docs/benchmark-eval.md` | 运行手册 |
