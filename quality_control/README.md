# LitKG 独立质控子系统

独立 Python 标准库服务 + 浏览器前端 + SQLite。无需复用 LitKG 的 Postgres、Redis、Celery 或前端，无第三方 Python / JavaScript 运行依赖。默认只在宿主机 `127.0.0.1:8090` 提供服务。

## 独立部署与启停

在仓库根目录运行：

```bash
docker compose -f docker-compose.qc.yml up -d --build
# 浏览器打开 http://127.0.0.1:8090
docker compose -f docker-compose.qc.yml stop
docker compose -f docker-compose.qc.yml start
docker compose -f docker-compose.qc.yml down
```

需要管理员权限的主机使用 `sudo docker ...`。`down` 保留 `litkg-qc_qc_data` 数据卷，包含输入快照、逐步报告、固定样本和人工判定历史。不要用 `down -v`，除非确定要清除这些数据。端口可通过 `QC_PORT` 更改。Compose 项目名 `litkg-qc`，与 LitKG 原项目分开，没有 `depends_on`。

本机开发也可以运行，不安装依赖：

```bash
QC_DB_PATH=/tmp/litkg-qc-dev.sqlite3 python3 -m quality_control.server
```

## LLM 配置

默认不调用 LLM；可直接导入图谱、运行本体检测、进行人工抽样评审。要开启原文评审，配置兼容 `/chat/completions` 且支持 `response_format: json_object` 的接口：

- `QC_LLM_BASE_URL`：完整基础路径，例如本地模型的 `http://host.docker.internal:8000/v1`。
- `QC_LLM_MODEL`：模型名称。
- `QC_LLM_API_KEY_FILE`：容器中的凭据文件路径，例如 `/run/secrets/llm_api_key`；不需要认证的本地模型可留空。
- `QC_CONTEXT_CHARS`：单次请求 JSON 的字符上限，默认 48000；这是字符预算，不是模型 token 数，需按部署模型调整。

把凭据文件放入 `quality_control/secrets/`，确保容器 UID 10001 可读，并限制其他用户访问；不要把凭据内容写进命令行。该目录已加入忽略规则并从镜像构建上下文排除。宿主机开发也支持 `QC_LLM_API_KEY` 环境变量；Compose 默认使用文件。

将上述**非秘密配置**保存在单独 env 文件时，用 `docker compose --env-file <文件> -f docker-compose.qc.yml up -d --build`。不要在任务输出中打印完整 Compose 渲染配置。只有在前端点击“运行原文 LLM 评审”后，才向所配置接口发送当前快照的原文与 claim。

每个请求超时 120 秒，不自动重试付费请求，不跟随重定向，不保存供应商错误响应体。报告保存实际返回的 usage 汇总、模型、输入哈希、提示词版本、候选冻结哈希和引用偏移。服务重启将未结束任务标记为 `interrupted` 并保留部分结果，需要用户重新发起评审。

## 从本机 LitKG 导入

前端“导入图谱 → 本机 LitKG 实例”读取 `QC_LITKG_URL`，默认 `http://host.docker.internal:8080`。可以显式设置内部地址。用户无法通过导入请求指定任意远程 URL。

LitKG API 新增只读接口：

```text
GET /knowledge-bases/{knowledge_base_id}/quality-export
```

该接口在同一个 `REPEATABLE READ READ ONLY` 事务中导出完整 claim 集、知识库全部已保存文本块（包括没有 claim 的块）、页码/章节/块偏移、本体及版本。原 LitKG API 镜像需要更新才能提供新接口：

```bash
docker compose up -d --build --no-deps api
```

此命令只用于更新 API；本实现不会自动重启现有 LitKG 服务。可以先用通用 JSON 导入运行质控，不依赖该接口。

保存块不能证明 PDF 图像、复杂表格与附件已完整核验，因此导入的 `parsing_status` 默认是 `partial`。补齐原文与抽取范围后，可在“自动评审”复制为新快照并确认来源覆盖；原快照、历史报告和人工样本不变。

## 通用导入与可比性

支持以下 JSON 形式：

```json
{"claims":[{"subject":"A","predicate":"REL","object":"B"}]}
```

也支持 claim 数组、`triples`、`relations`、`edges`、`facts`、`s/p/o`、`head/relation/tail`、`subject_text/predicate/object_text`、三元素数组、LitKG `claim_json` 和技能的 `graph.nodes + graph.claims` 格式。未知格式拒绝导入；支持点号路径字段映射：

```json
{"subject":"extraction.head.name","predicate":"extraction.relation","object":"extraction.tail.name"}
```

保留原始输入、每行原始字段、字段映射和快照哈希。显式实体 ID 不按同名自动合并；无 ID 实体按原样文本 + 类型建立确定 ID，仍可能存在身份歧义，会在覆盖代理中提示。重复 claim / node ID 会拒绝导入，避免人工标注错位。重复事实保留；统计关系密度时另做精确语义去重。

可上传 TXT、Markdown 或 `documents` JSON。TXT/Markdown 文献 ID 为文件名去扩展名，默认块 ID 为 `text`；与 claim 的 `doc_id` 对齐。不提供文献 ID 时语义评审检索全部导入文本。PDF 可先通过原 LitKG 解析，或使用 `review-literature-kg/scripts/ingest_sources.py` 转为 documents JSON；本子系统不假称内置 PDF/OCR/视觉评审。

规范来源示例：

```json
{"documents":[{"id":"paper-1","parsing_status":"complete","blocks":[{"id":"b1","page":2,"section":"Results","text":"Actual source text."}]}]}
```

只在确实完整覆盖原抽取范围时声明 `complete`。引用使用 `doc_id/block_id/start/end/quote`；偏移为 Unicode 码点、左闭右开。也接受 LitKG `document_id/chunk_id/sentence/offset`。引用必须满足精确片段相等；缺失或歧义保持未定位，人工页面显示告警与已有原文。

`examples/graph-a.json` 和 `graph-b.json` 是同一人工构造语料上的两个示例，后者故意遗漏限定条件和反转否定，方便测试比较工作流。它们不是科学事实或金标准。

## 固定评审流程

方法来自仓库的 `review-literature-kg`，提示词固定在 `prompts.py`，不是让评审模型自行决定流程。

1. **接入**：规范化并保存不可变图谱/语料/策略快照。
2. **全量本体检测**：悬空引用、显式实体/关系类型及继承、domain/range、属性类型、单位/数值范围、互斥类别、必填、唯一性、基数。仅执行用户提供的显式约束，不猜领域规则。
3. **独立重抽取**：仅给模型原文块和 scope，不给图谱/置信度，提取原子候选、名词短语、谓词和否定表述。逐条验证引用后冻结候选集及哈希。
4. **保真与量表**：逐 claim 对照原文，使用六类判定。默认提供全部关联文献；长文超预算时先取锚点、词项命中及邻块并披露检索范围，不能据部分检索判 `UNSUPPORTED`。没有证据或标签前提不满足时降为 `INSUFFICIENT_EVIDENCE`。领域专门化和粒度可独立补评：缺失有效标量时，使用 `RUBRIC` 提示词补充带证据的子项量表；缺失子项保留未定。数字字符串和单个完整 JSON 代码块兼容解析，证据仅在已发送原文中精确或仅空白差异唯一匹配时恢复锚点，不接受改写或多义引用。
5. **反向匹配**：冻结候选与图谱分批匹配，保存 `matched / misrepresented / missing / ambiguous / out_of_scope`。只有全部批次明确排除对应表示才可判 missing；已有但表达错误只记 misrepresented，避免重复算漏抽。分批无表示和无法消歧是不同状态。
6. **汇总**：计算多维分、场景适用性、支持率和覆盖；错误/未审/未知范围单列。逐块/逐 claim/逐候选保存进度，异常退出保留部分结果。

`SUPPORTED/PARTIALLY_SUPPORTED` 必须有 supports；`CONTRADICTED` 必须有 contradicts；`SOURCE_CONFLICT` 需要至少两个来源及两种证据角色；`UNSUPPORTED` 必须完整读取相关来源，不能以未检出代替反证。无来源不产生通过评分。

## 评分定义

六个维度均为 0–100：

| 维度 | 定义 |
|---|---|
| 正确性 | 已审严格支持率 `SUPPORTED / R`；R 包含所有已审未定标签，部分支持单列；若无任何可判定结果则为空 |
| 完整性 | 候选命中率 `matched / (matched + misrepresented + missing)`；没有 scope 时报告“已提供原文候选覆盖暂定分”，无有效候选分母才为空 |
| 领域专门化 | 证据量表均值：身份具体程度35%、术语25%、类型25%、领域范围适配15%；0严重缺失、50部分保留、100原文充分支持 |
| 粒度 | 原子表达、必要限定、极性/模态、数值/单位细节各25%；原文未报告数字时不因没有数字扣分 |
| 本体合规 | `100 × (1 − 失败约束次数 / 实际执行约束次数)`；不含独立悬空引用检查；无本体约束为空 |
| 覆盖代理 | 可用的名词短语表示、候选关系保留、否定候选保留、关系密度代理的均值；仅作线索 |

关系密度代理为 `100 × min(r, 1/r)`，r 是去重图谱声明数 / 去重重抽取候选数；只有来源完整且范围明确时计算。重复事实的原文证据合并到候选，避免重复提高分母。实体合并/拆分仅保存有来源证据的疑点，不自动合并或删除实体。

场景四项主权重严格采用用户表格，高=3、中=2、低=1：

| 场景 | 领域专门化 | 粒度 | 完整性 | 正确性 |
|---|---|---|---|---|
| 提取特定研究信息 | 3 | 3 | 1 | 2 |
| 研究领域综述 | 3 | 3 | 2 | 3 |
| 深入理解论文 | 2 | 2 | 1 | 3 |
| 复现研究结果 | 2 | 2 | 2 | 3 |
| 相关文献检索 | 1 | 1 | 3 | 1 |
| 文献推荐 | 1 | 1 | 3 | 1 |
| 判断文献相关性 | 2 | 1 | 2 | 2 |

另外，本体权重=正确性权重×0.5，覆盖代理权重=完整性权重×0.5。综合评分为六维加权均值；缺项保留 null，`scenarios.*.score` 报告已测维度的评分并标明 complete/partial 状态及已测权重，`overall` 仅在完整报告中有值。每个维度的 `dimension_details` 提供公式、分子/分母、评估覆盖、原因及子指标；无证据不能靠默认分补齐。适用性工程阈值为高80、中65、低50；任一主要维度低于门槛为“不适用”，缺项或评审不完整为“待验证”。门槛与 LLM 分数未做外部校准，可在 `models.py/scoring.py` 调整后升级规则版本。

门禁独立于分数：悬空引用等 blocker 为 FAIL，已确认语义/本体/范围内遗漏问题为 NEEDS_REVISION，其余未完成为 INCOMPLETE，全部完成才为 PASS。不把候选当人工金标准，不宣称全图 precision/recall/F1。雷达图缺项不补零，比较页面同时核对语料、范围/本体、模型和提示词版本。

## 多人、多轮人工评价与最终得分

每轮固定一个均匀无放回样本，规模 `min(N, 10 + ceil(5×uncertainty))`，最多15条。可“新增随机评审轮次”，每轮保留名称、seed、claim列表与创建时间，跨轮样本可以重叠。历史单样本和旧版评审自动、幂等迁移为第1轮，不删除原ID、时间、备注或判定。评审员名称作为本机工作台中的稳定标识，请持续使用相同代号；这里没有登录账号或多用户权限系统。

每次提交都追加保存评审员、轮次、claim、判定、备注和时间：

- 同一人、同一claim跨轮只取其最新意见，旧意见保留在历史中，不重复加票。
- 不同人的意见独立计入，正确=100、部分正确=50、错误=0；证据不足不并入数值分母，并单列未定。
- 先计算每条claim的多人等权均值，再对不同claim等权平均得到人工分 H，避免某条claim因评审人数多而过度加权。分歧会明确显示。
- 所有轮次中的有效意见均参与汇总。每轮也保留单独统计和原始提交记录，新增轮次不覆盖旧轮次。

默认基础人工权重为30%，可在快照 `policy.human_weight` 中设为0–1。实际权重 `w = 基础权重 × 已判定唯一claim数 / 所有轮次抽样claim并集大小`。正确性融合分 `C = (1-w)×机器正确性 + w×H`，然后代入各场景原有权重更新综合分。其他维度不被人工抽查替代。只有人工分而没有机器分时，正确性明确标记为“仅人工样本”，不据此宣称全图验证通过。

例如机器正确性100、人工分0、全部抽样claim均已判定，则融合正确性70；如果只判定一半抽样claim，有效权重15%，融合正确性85。不同人同一claim分别给100与0时，该claim人工分为50；同一人重复提交不会增加其权重。

API、比较页面及导出在读取时按最新人工记录重新计算，不需要再次调用LLM。报告同时保留 `machine_dimensions`、融合后的 `dimensions`、`machine_scenarios`、`human` 权重/完成度/人数/分歧、`score_revision`；数据库内的原机器评审结果保持原样。人工判断有错或分歧会影响门禁。严格一致支持率及 Wilson 95% 区间仍单独提供，评分是工程加权指标，不能解释为经过校准的全图准确率。

## 空分数诊断与旧报告

旧版分数链路过度依赖可选标量和完整抽取响应，未给范围又会直接隐藏完整性。新版分开处理各指标，保留有效候选和证据，并在缺少领域/粒度值时单独补评。普通本体重跑会复用最近一次完成的LLM原文报告，而不会把五个证据维度清空。

旧报告读取时会用已保存证据重新汇总评分，并接入所有人工记录，不发起额外LLM调用。此前没有保存的量表、失败响应或从未锚定的证据不能凭空恢复，需要重新运行原文LLM评审。页面逐维度展示不可评分的具体原因，不把任务执行结束等同于质量验证完成。

## API 与验证

主要接口：`GET /api/graphs`、`POST /api/graphs`、`POST /api/import/litkg`、`POST /api/graphs/{id}/runs`、`GET /api/runs/{id}`、`POST /api/graphs/{id}/sample`、`GET/POST /api/graphs/{id}/rounds`、`GET /api/graphs/{id}/claims/{claim_id}`、`POST /api/graphs/{id}/reviews`、`GET /api/graphs/{id}/export`。

```bash
python3 -m unittest discover -s quality_control/tests -v
node --check quality_control/static/app.js
docker compose -f docker-compose.qc.yml config --quiet
```

测试使用人工构造文本与确定性假模型验证流程，不调用真实 LLM。真实模型的质量和供应商兼容性需部署后验证。单进程队列、本机工作台支持多人标注身份；不包含账号认证、分布式 worker 和任意 SHACL/OWL 推理。源码使用标准库，容器以非 root 用户运行、只读根文件系统、独立数据卷和仅本机端口。
