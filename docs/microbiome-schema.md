# FMT_use：肠道微生物组词表（第一轮）

本轮只扩展实体、关系和实体过程语义。沿用原有 OpenAI 兼容的
`chat.completions` 模型 API、调用参数、输出预算和抽取流程；不引入模型、
外部实体链接服务或依赖，不新增数据库表/字段。新增信息通过原有
`Entity.process`、`Claim.qualifiers` 和 `participants` 保存。

## 实体类型

共享定义见 `app/app/entity_types.py`，通过 `GET /entity-types` 获取，前端筛选自动读取。
原有 18 种生物医药类型全部保留，包括 `Other`，新增：

| 类型 | 边界 |
| --- | --- |
| MicrobialTaxon | 细菌、古菌、真菌、原生生物、病毒/噬菌体等分类单元；阶元保留在名称/上下文中 |
| MicrobialStrain | 具体菌株或分离株，保留菌株编号，与物种分开 |
| MicrobialCommunity | microbiota、virome、mycobiome、定义菌群或合成群落 |
| Microbiome | 原文明示的微生物组，区别于单纯的群落成员集合 |
| SequenceFeature | ASV、OTU 等文献内序列特征，不能自动当作菌种 |
| Genome | 基因组、MAG 或基因组集合，与菌株和单个基因分开 |
| Metabolite | 短链脂肪酸、胆汁酸、吲哚衍生物等代谢物 |
| Diet | 饮食模式或喂养方案 |
| DietaryComponent | 膳食成分、营养素、纤维；明确化学分子仍优先 Compound/Metabolite |
| Intervention | FMT、益生菌/益生元/合生元治疗、非药物干预；具体益生菌菌株用 MicrobialStrain，药物仍用 Drug |
| Biospecimen | 粪便、肠内容物、活检、血清等采集样本，与解剖来源分开 |
| StudyGroup | 队列、比较组、供体/受体组、治疗组 |
| ExperimentalModel | 无菌小鼠、类器官、肠道模拟器等实验模型；宿主物种本身仍用 Organism |
| Environment | 栖息环境；宿主解剖部位仍用 AnatomicalStructure/Tissue |
| ClinicalOutcome | 缓解、复发、生存等临床结局，与疾病分开 |
| MicrobialFunction | 功能能力或基因功能；具名通路仍用 Pathway |
| DiversityMetric | Shannon、Bray-Curtis 等多样性/丰富度/均匀度指标，数值放 qualifiers |

ASV/OTU、研究组、样本和 `MAG_1` 等局部基因组标签在无标准 ID 时按文献隔离，
避免不同文献的“controls”“ASV1”意外合并。菌株与物种、群落与分类单元保持不同身份。
Metabolite 与 Drug/Compound 共用 Chemical 身份规则，避免同一化学分子因类型细化重复建点。
类型继承只用于关系端点约束，不能据此把菌株合并到物种。

## 受控谓词注册表

唯一注册表为 `app/app/predicate_registry.py`。`GET /predicate-types` 返回定义、
同义表达、主语/宾语允许类型、对称性、逆关系名称、必填限定字段、证据类别及过程词表。
逆关系名称是元数据，不是另一套可输出谓词，也不会自动产生反向边。
本地抽取校验与提示词均使用同一注册表。

| 关系组 | 基础谓词 | 关键约束 |
| --- | --- | --- |
| 分类与组成 | SUBCLASS_OF、MEMBER_OF、PART_OF | 分类包含、群落成员、物质组成分开；不从成员关系推断机制 |
| 统计关联 | ASSOCIATED_WITH、CORRELATED_WITH | 相关符号放 association_direction，不转成激活/抑制 |
| 组间差异 | DIFFERS_BETWEEN | 焦点组、参照组、指标、方向均须保存，两个研究组必须可定位原文 |
| 代谢与功能 | PRODUCES、CONSUMES、TRANSFORMS、ENCODES、CATALYZES | 预测功能保留 predicted；TRANSFORMS 的产物作为 product 参与者 |
| 调控与干预 | AFFECTS、ACTIVATES、INHIBITS | AFFECTS 的方面和方向放在宾语 process；激活/抑制用于明确机制 |
| 生态作用 | COLONIZES、CROSS_FEEDS、COMPETES_WITH、INFECTS | 不接受共现网络或功能预测作为机制证据；CROSS_FEEDS 的物质用 mediator 参与者 |
| 预测与测量 | PREDICTS、MEASURED_BY、SAMPLED_FROM | 预测结局与性能单独保存；测量与采样不等于定植或因果 |
| 原有生物医药关系 | BINDS_TO、INTERACTS_WITH、TREATS、PREVENTS、CAUSES、TARGETS、LOCATED_IN、PARTICIPATES_IN | 与微生物组关系共用同一注册表及证据检查 |

这是项目内部词表，不声明与 Biolink 谓词一一映射。实体方面与变化方向的拆分参考
Biolink 的 [aspect qualifier](https://biolink.github.io/biolink-model/subject_aspect_qualifier/)
及 [direction qualifier](https://biolink.github.io/biolink-model/object_direction_qualifier/)。

### 证据与兼容规则

`qualifiers.evidence_type` 支持 `experimental`、`observational`、
`computational_prediction`、`cooccurrence`、`author_statement`、`not_reported`。
仅按当前片段标注，不根据文献主题或模型常识猜测。预测功能必须同时标明
`knowledge_level=predicted`，不能冒充实验观测；明确的 observational/cooccurrence
证据不能挂到要求机制证据的关系。

新抽取中的未注册谓词、明确不兼容的端点类型/证据或不完整的组间差异，
沿用原有逐候选隔离与有限纠错。缺少可选证据设计信息，或除组间差异外的
必填限定信息无法可靠获得时，保存 `review_reasons`，不编造、不增加专门的模型调用。
因此这些已保存但待复核的断言不代表已满足完整注册表契约。

兼容模型已有的 `REDUCES`、`INCREASES`、`UP_REGULATES`、`DOWN_REGULATES` 等表达：
仅在新抽取校验时规范到 `AFFECTS`，将方向放进已有宾语过程；过程未知时保留
`qualifiers.object_direction` 并标记方面缺失。正/负相关同义表达规范到
`CORRELATED_WITH + association_direction`。不从方向动词猜测丰度、活性或机制。
旧库记录不会在读取时被强制改写，旧谓词仍可按原名查询。

## 实体过程

保留 `expression`、`secretion`、`activity`，新增：

`abundance`、`prevalence`、`growth`、`colonization`、`production`、`degradation`、
`conversion`、`concentration`、`diversity`。

过程附着到实际改变的实体。例如菌的丰度、丁酸浓度、群落多样性分别附着到菌、
丁酸和群落实体。`direction` 仍为 `increased|decreased|unchanged|unspecified`。
`mention` 保留原文，`text` 保存实体本身；菌株编号、物种和修饰等身份信息不得被拆掉。
具名生物过程/功能和多样性指标本身仍可作为实体，不能一律删除为限定字段。

```json
{
  "subject": {"text": "FMT", "type": "Intervention", "mention": "FMT"},
  "predicate": "AFFECTS",
  "object": {
    "text": "Bacteroides", "type": "MicrobialTaxon",
    "mention": "relative abundance of Bacteroides",
    "process": {"kind": "abundance", "direction": "increased"}
  },
  "qualifiers": {"metric": "relative abundance", "evidence_type": "experimental"},
  "evidence": {"sentence": "FMT increased relative abundance of Bacteroides."},
  "confidence": 0.9
}
```

以上为合成格式示例，不是文献结论。实际只有片段提供实验依据时才标 experimental。
宿主、样本、肠道部位、饮食、供体/受体和方法继续用具名参与者及原有限定字段表达。
投影的 `qualifiers.processes` 用 `role` 标明主语/宾语；辅助参与者另带 `entity_id`，
即使两个参与者角色相同，也能区分哪个实体的哪个方面改变。不同过程、方向和上下文
会产生不同关系键；原文措辞差异不应拆分同一事实。

## 应用范围与已有文献

本轮修改代码和提示词，不自动重启服务、不批量重跑已有文献。更新应用镜像后，
新文献直接使用新词表。缓存版本已更新，防止旧抽取掩盖新增概念。

要补齐 demo 已有文献的实体/关系，需要按 README 的维护接口执行 `reextract`；
`reproject` 或图谱重建只能利用已经保存的断言，不能补回从未抽取的实体或关系。
重新抽取仍调用原模型 API，调用量取决于文献与分段数量。无需为本轮增加 SQL 迁移；
旧部署本身已有的迁移要求不变。

离线回归：

```bash
PYTHONPATH=app:tests python -m unittest test_microbiome test_normalization test_knowledge \
  test_claim_robustness test_claims_robustness test_catalog_robustness test_catalog_capacity -q
```

测试使用合成文本和受控模型响应，不发送本地文献到模型或第三方服务。
