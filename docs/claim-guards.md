# 确定性 claim 校验（V1）

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

## 已知未覆盖

无明确论元词形的施受事反转、跨句指代、表格列顺序、数值比较、核心发现遗漏、实体合并、方法区约束未实现。
V1 不以放宽整句关键词匹配来提高台账召回率。V2 确定性和 V3 source_refs 单独变更。
模型替换、提示词大改、本体大改、PDF 文章切分、全库 reextract 和前端变更均不在本次范围。
IDENTITY_TO 尚需身份定义和迁移评估，不新增谓词；qualifier 注册不是 V1 所需改动。
