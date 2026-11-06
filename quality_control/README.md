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
4. **保真**：逐 claim 对照原文，使用六类判定。默认提供全部关联文献；长文超预算时先取锚点、词项命中及邻块并披露检索范围，不能据部分检索判 `UNSUPPORTED`。没有证据或标签前提不满足时降为 `INSUFFICIENT_EVIDENCE`。
5. **反向匹配**：冻结候选与图谱分批匹配，保存 `matched / misrepresented / missing / ambiguous / out_of_scope`。只有全部批次明确排除对应表示才可判 missing；已有但表达错误只记 misrepresented，避免重复算漏抽。分批无表示和无法消歧是不同状态。
6. **汇总**：计算多维分、场景适用性、支持率和覆盖；错误/未审/未知范围单列。逐块/逐 claim/逐候选保存进度，异常退出保留部分结果。

`SUPPORTED/PARTIALLY_SUPPORTED` 必须有 supports；`CONTRADICTED` 必须有 contradicts；`SOURCE_CONFLICT` 需要至少两个来源及两种证据角色；`UNSUPPORTED` 必须完整读取相关来源，不能以未检出代替反证。无来源不产生通过评分。

## 评分定义

六个维度均为 0–100：

| 维度 | 定义 |
|---|---|
| 正确性 | 已审严格支持率 `SUPPORTED / R`；R 包含所有已审未定标签，部分支持单列；若无任何可判定结果则为空 |
| 完整性 | 明确范围内候选命中率 `matched / (matched + misrepresented + missing)`；无 scope 或无候选分母为空 |
| 领域专门化 | LLM 证据评分均值：0 泛称/无法定位、50 领域类别、100 明确且原文支持的身份/类型 |
| 粒度 | LLM 证据评分均值：0 核心语义缺失、50 有关键限定缺口、100 全部必要条件保留 |
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

另外，本体权重=正确性权重×0.5，覆盖代理权重=完整性权重×0.5。综合分为六维加权均值；缺项保留 null，仅另报已测权重下的暂定分。完整报告才能给出最终综合分。适用性工程阈值为高80、中65、低50；任一主要维度低于门槛为“不适用”，缺项或评审不完整为“待验证”。门槛与 LLM 分数未做外部校准，可在 `models.py/scoring.py` 调整后升级规则版本。

门禁独立于分数：悬空引用等 blocker 为 FAIL，已确认语义/本体/范围内遗漏问题为 NEEDS_REVISION，其余未完成为 INCOMPLETE，全部完成才为 PASS。不把候选当人工金标准，不宣称全图 precision/recall/F1。雷达图缺项不补零，比较页面同时核对语料、范围/本体、模型和提示词版本。

## 人工评价

每张不可变快照首次创建样本时，随机生成并保存 seed，均匀无放回抽取 `min(N, 10 + ceil(5×uncertainty))`，最多15条。整体不确定性只改变样本数，不改变单条入样概率。样本保存后不重抽；评审只能写入样本内 claim。

人工页面展示 claim、限定条件、来源文献/页码/章节/块、全文和已验证片段高亮。支持正确、部分正确、错误、证据不足四种判定，保存评审员、时间、备注和追加改判历史。人工支持率与 Wilson 95% 区间单独报告；未定和未审单列，不与 LLM 结果混合加权。最多15条只适于轻量抽查，不能证明全图准确率。

## API 与验证

主要接口：`GET /api/graphs`、`POST /api/graphs`、`POST /api/import/litkg`、`POST /api/graphs/{id}/runs`、`GET /api/runs/{id}`、`POST /api/graphs/{id}/sample`、`GET /api/graphs/{id}/claims/{claim_id}`、`POST /api/graphs/{id}/reviews`、`GET /api/graphs/{id}/export`。

```bash
python3 -m unittest discover -s quality_control/tests -v
node --check quality_control/static/app.js
docker compose -f docker-compose.qc.yml config --quiet
```

测试使用人工构造文本与确定性假模型验证流程，不调用真实 LLM。真实模型的质量和供应商兼容性需部署后验证。单进程队列、单用户本机界面；不包含多用户认证、分布式 worker 和任意 SHACL/OWL 推理。源码使用标准库，容器以非 root 用户运行、只读根文件系统、独立数据卷和仅本机端口。
