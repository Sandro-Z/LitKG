# P1：比较、实体与角色卫生

在已提交P0基础上按V4→V5→V6实施，不部署、不执行正式库apply。继续使用现有pending、复核队列、只读审计预览和历史/回滚；不增加表、模型服务或前端。

## V4：比较与数值

改动：app/app/claim_guards.py、claim_audit.py、tests/test_claim_comparison.py、tests/fixtures/kb26_comparison_cases.json、本说明和changelog.md。

|规则|触发条件|动作|边界|
|---|---|---|---|
|V4-COMPARISON-ARMS|DIFFERS_BETWEEN缺比较/参照组，或明确more/less...than比较缺比较槽|needs_review|more than 50%阈值不当作两臂比较；已有comparison槽保留|
|V4-SAME-GROUP|两个组规范化后相同|needs_review|不同组不触发|
|V4-NUMERIC-UNBOUND|明确数值比较中至少两个测量值，有数值未存入qualifiers/participants|needs_review|排除图号、引文、实体/组编号、p/p-value/n；条件值不当作效应量|
|V4-NUMERIC-DIRECTION|组和metric明确，每一种数值配对都与方向相反|needs_review|混合方向、CI/range、异单位/缺单位不推断方向，不做单位换算|
|V4-PRODUCT-ROLE|CONSUMES的product被明确列在consumes both X and Y中，没有产物断言|needs_review|有明确produce和真实产物不触发|

数值检查针对明确的比较测量，不要求把引用编号、图号或统计p值绑定成效应量。
11条真实夹具包括23319–23321正确四槽、23332–23334丢比较槽、23372/23373数值反向、23364产物槽交叉绑定，以及16242/20857已保留参照组的反例。
后两条的审计文字称“参照组丢失”，但提供的实际qualifiers已有参照组，不能只凭问题编号误报。

V4阶段最终实测：完整PostgreSQL回归376项、5项可选浏览器跳过，26.946秒。累计P0+V4缺陷集182/1175，台账40/79，SUPPORTED误报0/300，D1保持21/21。

## 验证命令

```bash
PYTHONPATH=app:tests python -m unittest test_claim_comparison -v
PYTHONPATH=app python scripts/check_claim_guard_fixtures.py --audit-dir audit --output /tmp/p1-guard-report.json
# 配置可丢弃PostgreSQL+pgvector后，完整回归使用P0已验证的命名空间启动方式。
PYTHONPATH=app:tests python -c "import app,sys;sys.path.append('frontend');import unittest;unittest.main(module=None,argv=['unittest','discover','-s','tests','-v'])"
node tests/test_graph_view.cjs
```

全部批量入口继续默认dry-run。旧记录审计只写复核结论，不改原始claim_json、证据或claim ID。
