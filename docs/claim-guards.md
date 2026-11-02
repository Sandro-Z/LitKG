# 确定性 claim 校验与导出（P0：V1–V3）

校验复用现有 `semantic_status=pending` 和复核队列。新抽取在 `check_semantics` 中检查；
旧记录批量检查只增加 `claim_audit` 复核结论，保留 claim ID、原始 claim_json 和 evidence。
默认确认图谱排除活动校验冲突；`include_pending=true` 仍可查看。
审核队列的 resolved/dismissed 决定不会自行解除有效冲突，需修正断言后重新检查。

规则模块 `app.claim_guards` 为纯函数，不调用模型或数据库。每条结论包含 rule_id、命中词、字段与 needs_review 动作。

|规则|触发条件|边界|
|---|---|---|
|V1-ANTI-ATTRIBUTE|has/is/are 肯定归属 anti-X，目标 activity 却 decreased|普通炎症 decreased 不触发|
|V1-SUBJECT-OPERATOR|inhibition/absence/lack of 裸主语，且未编码主语 decreased|保留完整事件或明确主语下降不触发|
|V1-EVENT-OPERATOR|inhibition 事件 leads to/cause 结果，事件自身却 decreased|事件 increased 不触发|
|V1-PASSIVE-AGENT|AFFECTS 主语为 were protected against 的受事|主动保护不触发|
|V1-SENSITIVITY-AGENT|AFFECTS 的主语 is sensitive to 宾语|反向真实影响不触发|
|V1-EXCEPTION|All but N (...) 中明确排除的实体被存为阳性|例外以外实体、显式阴性不触发|
|V1-EXCLUDED-OBJECT|but not/rather than/except 后的明确宾语被存为阳性|其他宾语、已编码阴性不触发|
|V1-NO-RELATION|no relationship ... identified，两个实体同句，阳性关联|肯定关联、正确 negated 不触发|
|V1-UNASSESSED-PREDICTION|insufficient ... evaluate/test ...，两实体同句，却肯定 PREDICTS|已完成评估不触发|
|V1-NEGATED-PREDICATE|实体-否定助动词-对应动作词-实体，仍为阳性|作用于其他主语或已 negated 不触发|
|V1-CONTAINS-REVERSED|S contains O，却 PART_OF(S,O)|O contains S 不触发|
|V1-CONTAINER-REVERSED|O in S，却 PART_OF(S,O)|S in O 不触发|

## 预览、应用与回滚

```bash
PYTHONPATH=app python -m app.claim_audit --knowledge-base-id 26 --dry-run --output /tmp/kb26-audit-plan.json
# 检查统计、逐文献计划与 findings 后，显式应用；按文献事务，失败不撤销之前已完成的文献。
PYTHONPATH=app python -m app.claim_audit --apply-plan /tmp/kb26-audit-plan.json
# 每个文献返回独立 run_id，沿用 scoped_repair_runs、claim_history 及并发状态检查。
PYTHONPATH=app python -m app.claim_audit --rollback RUN_UUID
PYTHONPATH=app python -m app.claim_audit --rollback RUN_UUID --apply
```

预览使用 repeatable-read 只读事务；应用重新核对数据快照和规则输出，只写复核结论及既有修复审计。
复核项按 document/version/claim_id 稳定去重并保留人工决定。
共享文献的结论影响所有包含它的知识库；命令不会调用模型、重抽或删除 claim。
需要项目既有 `scoped_repair_runs` 迁移，不新增 SQL 表或迁移。

## 夹具验证

```bash
PYTHONPATH=app:tests python -m unittest test_claim_guards -v
PYTHONPATH=app python scripts/check_claim_guard_fixtures.py --audit-dir audit --output /tmp/kb26-guard-report.json
PYTHONPATH=app:tests python -m unittest discover -s tests -v
node tests/test_graph_view.cjs
```

21 条 D1 最小回归来自提供的审计包；不修改源数据。完整审计 JSON 留在 audit，不作为运行依赖。
报告分别给出 D1 指定场景、79 条 CONTRADICTED 台账、1175 条缺陷集和300 条 SUPPORTED 对照。
命中率表示风险筛查覆盖，不表示已自动纠正科学结论。

V1 实测：D1 21/21（100%）；SUPPORTED 0/300（0%）；缺陷集 40/1175；
CONTRADICTED 台账 10/79（12.7%），台账整体 90% 目标未达到，不作为完成该目标的声明。
远程完整 unittest：`Ran 349 tests ... OK (skipped=83)`；Node 图谱 9/9。
83 项数据库/浏览器测试因未配置可丢弃数据库和浏览器而跳过，新增真实数据库审计回滚用例也在其中。
远程已有 frontend/export_jobs.py，直接 README 测试命令有3项导入失败；本次测试启动时先加载后端命名空间，再添加前端路径：

```bash
PYTHONPATH=app:tests /tmp/litkg-claim-audit-venv/bin/python -c "import app,sys;sys.path.append('frontend');import unittest;unittest.main(module=None,argv=['unittest','discover','-s','tests','-v'])"
```

## V2：确定性一致性

|规则|触发条件|动作与边界|
|---|---|---|
|V2-HEDGE-RESULT|speculative=true，或唯一终点分句中终点前有 may/might/could/suggest/proposed/thought/likely/potentially，且 observed_change|新候选降 unknown；旧记录入队。终点后的机制推测、明确观测后的解释性 likely due to 不借用|
|V2-SIGNIFICANCE|明确非显著，或可靠解析的同臂 p>0.05 / p=0.091，缺显著性标注|新候选 outcome 标 not_significant，其余谓词用既有 result_interpretation；保留估计方向，仅记录明确 p_value。p不在[0,1]、丢指数或混合显著性不猜|
|V2-UNMEASURED-NULL|无法测量/未评估，仍 observed_change/no_effect/unchanged|新候选 unknown、not_assessed，unchanged 改 unspecified；另一比较臂不借用|
|V2-TREND-SIGNIFICANCE|trend 配 observed_change，缺 p_value 或显著性标注|只入队，不从 trend 编造 p 或非显著结论|
|V2-RESOLVED-NULL|明确 no change/doesn't change 等，仍 unresolved_negative|新候选按既有 no_effect_reported 约定规范化；旧记录入队，证据保持原样|

仅新增允许的 p_value qualifier 注册；既有 result_interpretation 增加 speculative、not_significant 值。
Python 与 SQL 对 speculative=true 的 observed_change 均读为 unknown；正确阴性和未显著结果保持各自语义。
旧记录批量应用始终只入队，`would_downgrade=0` 表示本命令不改写既有 JSON。
V2 夹具包含 14967、17636–17638、23336、19626 六个真实 D2 场景，另有每项规则的反例。
V1+V2 实测：D1 21/21；D2 六场景均命中；SUPPORTED 0/300；缺陷集119/1175，覆盖36篇文献；
台账12/79（15.2%）。V2-HEDGE-RESULT 69、V2-SIGNIFICANCE 10、V2-UNMEASURED-NULL 1、
V2-RESOLVED-NULL 1（规则可在同一 claim 重叠）。完整 unittest 359 项、83项跳过；Node 9/9。

## V3：出处导出

`app.provenance.claim_source_refs` 统一为 Claims 列表/完整导出与 Facts 列表/完整导出生成 `source_refs`。
CSV 沿用嵌套字段的 JSON 字符串编码；旧客户端仍可读取原有字段。

每个引用包含 document_id、chunk_id、page/page_end、section、sentence、kind、title、doi；
kind 区分 primary/additional，已有 table_or_figure 也保留。每条补充证据独立定位，不借主证据的页码。
有保存的 chunk 文本且能唯一匹配时，增加 offset.start/end（零起始、左闭右开、Unicode 码点，基准 chunk_text）。
唯一性不足时不填偏移；缺页码保留 null。当前库未持久化原始 block ID，因此不编造 block。
文本和 page_spans 仅用于服务器定位，导出不会包含新引入的内部全文字段，也不采用模型给出的 start/end。

新增7项回归覆盖主/补充证据、未知来源、歧义偏移、解析页码范围、JSON/CSV兼容与 Facts 导出。

## 最终 P0 验证

独立 PostgreSQL 16 + pgvector 上完整 unittest：

```text
Ran 368 tests in 23.802s
OK (skipped=5)
```

仅5项可选浏览器测试跳过，数据库集成已实际运行；Node图谱9/9，git diff --check通过。
数据库实测后的修正与复核项见 [P0数据库集成复核](claim-guard-integration-checks.md)。
临时实例只用本用户私有Unix socket，禁用TCP监听，测试结束后已停止。
本轮只对KB26审计夹具运行预览，未访问正式KB26数据库，也未执行生产 apply、重抽、部署或推送。

## 后续与范围

无明确论元词形的施受事反转、跨句指代、表格列顺序、数值比较、核心发现遗漏、实体合并、方法区约束未实现。
V1 不以放宽整句关键词匹配来提高台账召回率。V1–V3 分别提交；本轮不扩展可选 P1。
模型替换、提示词大改、本体大改、PDF 文章切分、全库 reextract 和前端变更均不在本次范围。
IDENTITY_TO 尚需身份定义和迁移评估，不新增谓词；qualifier 注册不是 V1 所需改动。
