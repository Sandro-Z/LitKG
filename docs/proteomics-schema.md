# Proteomics：词表、通用优化与迁移

本分支基于 `main` 的 `c0d4d91`，参考 `FMT_use` 截至 `4745073` 的六个提交。
保留原有 18 种生物医学实体，新增 15 种蛋白质组学相关实体；运行时共支持
33 种实体、39 种受控关系、24 种实体过程。没有引入微生物分类、菌株、菌群、
饮食、多样性或定植/交叉喂养等专用词表。

## 从 FMT_use 迁入的通用优化

| 来源提交 | 本分支保留的行为 |
| --- | --- |
| `5f4e593` | 共享实体/谓词/过程注册表、原文校验、证据类型、组间差异与统计关联区分；领域内容改为下述蛋白质组学契约 |
| `6f333af` | 删除最后一个所属知识库时清理独占文献、缓存、历史及投影，保留共享文献和磁盘 PDF；排队中的过期任务返回 superseded，再上传独占 PDF 从头处理 |
| `f4e6ba3` | 批次按序加锁、无变化不写及死锁回滚后有限重试；汇总异常不误报解析失败；目录重复循环检测、组别表示兼容、证据引号修复、完整断言纠错约束和原文边界分段 |
| `c3615a8` | GROBID 与 PDF 全文/附录 DOI 补查、参考文献/数据/代码 DOI 排除、多个候选歧义处理 |
| `e765ec1` | 每种实体独立配色、当前子图图例、API 词表驱动筛选；配色替换为本分支的 33 种类型 |
| `4745073` | 紧凑/显式组间差异共享关系身份，重复角色的过程保留实体目标，实体合并同步更新过程目标；前端保留有不同条件的平行边，并展示条件、极性及完整限定信息 |

`scripts/repair_comparison_relations.py` 保留默认 dry-run、应用前备份、限定批次与
原始 claim/证据保留逻辑。部署脚本与四份 SHA256 清单一并同步；本次未执行部署或正式数据修复。

## 新增实体及边界

| 类型 | 用途和边界 |
| --- | --- |
| `Peptide` | 肽及有明确序列/名称的修饰肽，保留序列、末端和修饰；不能由共享肽推断唯一母蛋白 |
| `Proteoform` | 明确的蛋白质分子形式，保留变体、异构体和修饰组合；不补齐未报道的位点 |
| `ProteinGroup` | 质谱证据无法区分成员的蛋白推断组；不是单个蛋白，也不是物理复合物 |
| `ProteinFamily` | 蛋白家族/类别，与单个蛋白区分 |
| `ProteinDomain` | 结构域或片段，保留已报道父蛋白及坐标 |
| `ProteinModification` | 修饰名称；仅按原文区分生物性 PTM 与制样造成的化学修饰；不是已修饰分子 |
| `ModificationSite` | 含明确父蛋白身份的位点，例如 `AKT1 S473`；裸 `S473`/`K48` 使用关系限定字段，不能成为独立位点节点 |
| `Proteome` | 指定细胞、组织、物种或分级组分的蛋白集合 |
| `SubcellularLocation` | 亚细胞位置/细胞器；与组织、样本分开 |
| `Metabolite` | 代谢物，与 Drug/Compound 共享 Chemical 身份规则 |
| `Biospecimen` | 已采集的血清、血浆、组织样本、裂解液等材料；与来源区分 |
| `StudyGroup` | 队列、处理组、参照组；与 ProteinGroup 区分 |
| `ExperimentalModel` | 敲除小鼠、类器官等实验模型；裸物种仍为 Organism |
| `Intervention` | 基因扰动、敲低等非药物操作；药物分子仍为 Drug |
| `ClinicalOutcome` | 生存、缓解、治疗响应等临床终点；与疾病区分 |

原有 Gene、Protein、RNA、MolecularComplex、Assay、Pathway 等保持可用。
`Proteoform → Protein` 仅用于关系端点兼容，不合并身份；Peptide 和 ProteinGroup
不是 Protein 的子类型。没有标准 ID 的样本、研究组和 Proteome 按文献隔离。
ProteinGroup 和 `peptide 1` 等研究内编号始终保留文献身份，不使用成员/母蛋白 ID
作为全局身份；模型提供的这类 ID 仍保存在原始记录。

肽、蛋白质形式、结构域和位点即使附有相同母蛋白 accession，也将完整规范名称
纳入身份键与 ID 索引，避免不同序列、修饰及位点被同一 ID 合并。这是保守规则：
不同写法的等价形式可能暂时保留为多个节点，不根据模糊别名强行归并。
不执行 ProForma 的完整语法解析、序列比对或肽到蛋白推断。

## 关系和过程

| 关系 | 含义 |
| --- | --- |
| `PEPTIDE_OF` | 原文明示的肽到蛋白/未消歧蛋白组映射 |
| `PROTEOFORM_OF` | 具体形式到母蛋白 |
| `MEMBER_OF` / `PART_OF` / `SUBCLASS_OF` | 分别表示家族归属、物理组成、类别包含，不代替肽映射或形式归属 |
| `HAS_MODIFICATION` / `HAS_SITE` | 分子带有某修饰或有父蛋白身份的位点 |
| `PHOSPHORYLATES` / `DEPHOSPHORYLATES` | 明示酶到蛋白/肽底物的磷酸化/去磷酸化关系 |
| `ACETYLATES` / `DEACETYLATES` | 乙酰化/去乙酰化 |
| `METHYLATES` / `DEMETHYLATES` | 甲基化/去甲基化 |
| `UBIQUITINATES` / `DEUBIQUITINATES` | 泛素化/去泛素化 |
| `GLYCOSYLATES` / `CLEAVES` | 糖基化或剪切，已报道产物放 product 参与者 |
| `DIFFERS_BETWEEN` | 被测实体相对于参照组的差异；必须有两组、指标和方向，object 是焦点组 |
| `CORRELATED_WITH` / `ASSOCIATED_WITH` | 统计相关或关联，不自动改写为机制 |
| `BINDS_TO` / `INTERACTS_WITH` | 原文明示直接结合或生物学互作，保留方法和 interaction_type |
| `MEASURED_BY` / `SAMPLED_FROM` / `LOCATED_IN` | 测量方法、样本来源或定位 |

其余生物医学受控谓词见 `GET /predicate-types`。逆关系名和对称性仅描述语义，
不据此生成额外边。未注册谓词及明确不兼容的类型/证据进入现有候选隔离与有限纠错。

`process` 附着在实际被测实体，保留方面与 increased/decreased/unchanged/unspecified
方向。支持表达、分泌、活性、丰度、浓度、生产、降解、转化、定位、占有率、各类修饰、
剪切、稳定性、周转、溶解度和聚集。修饰分子的身份仍留在名称，不能只保留变化过程。

提示词要求区分肽强度、总蛋白丰度、修饰肽水平、位点占有率及活性；保留已报道的
位点、编号背景、定位概率、定量方法、归一化、总蛋白校正、fold change 尺度与分母、
p/q 值和 FDR。共丰度、共沉淀或通路富集不能自动证明直接结合或通路激活。
这些依赖原文理解的要求不能仅靠类型检查证明，仍需要真实样本评估和人工复核。

预测位点、互作和功能可以按原文记录，但必须保留
`evidence_type=computational_prediction`、`knowledge_level=predicted`；
不能改写为实验验证。实验、观察、网络共现、作者陈述及设计未说明分别保留。

## 接口、缓存及已有数据

- `GET /entity-types` 返回共享实体定义；`GET /predicate-types` 返回版本
  `proteomics-v1`、端点约束、过程、限定字段、证据类别及类型父级。
- 继续使用现有模型 API、请求格式、分段预算及部署依赖；无需新 SQL 迁移。
- 处理版本为 `litkg-v3.4-proteomics`，目录为 `catalog-v4-proteomics`，
  关系版本为 `claims-v5-controlled-proteomics`；提示词版本也与 main/FMT_use 区分。
  旧领域缓存不能代替新词表抽取；同一契约下的成功片段与旧分段计划仍可续接复用。
- 已处理文献使用维护接口的 `reextract` 才能生成新领域目录和关系，
  `retry` 只修复缺口，`reproject`/`derived` 不会补抽新实体。
- DOI 补齐需要重新解析原 PDF；只重抽已有 chunks 无法获取新的 PDF 元数据。
- 原始 claim_json、历史及出处继续保留。本次没有触发已有文献重处理。

## 验证与依据

263 项完整回归通过，无跳过；包含一次性 PostgreSQL 15/pgvector 的关系身份、
独占/共享文献删除、重传重新处理、并发/死锁恢复，以及 API、Streamlit、
DOI、本地证据修复和新增蛋白质组学契约检查。模型、外部 HTTP 和 Celery 发布均使用
受控替身，日志为 `/tmp/litkg-proteomics-full.log`。

历史失败归档 `tests/fixtures/batch22_failures.json` 仅用于通用解析/证据/缓存回归；
保留原文和原输出以维持可追溯性，不代表运行时支持其微生物类型。
归档中的 diversity 过程明确拒绝，原分支的分类单元/菌株专用测试没有迁入。

术语边界参考 [IUPAC proteoform 定义](https://goldbook.iupac.org/terms/view/09774)、
[HUPO-PSI PSI-MOD](https://github.com/HUPO-PSI/psi-mod-CV) 和
[ProForma 2.0 原始论文](https://arxiv.org/abs/2109.11352)。这里的类型及关系是项目契约，
不宣称与这些标准或其他本体逐项等价。标准 ID 仍只能来自原文；新增本地
MOD/UNIMOD/MS 格式规范化，不进行外部实体链接或上传文献。

尚未调用真实模型、GROBID 或真实蛋白质组学 PDF 评估覆盖率/准确率，
也没有部署、重启正式服务或修改正式数据库。
