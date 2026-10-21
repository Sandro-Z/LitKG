# 补充评审材料后的核对

已阅读 `outputs/LitKG-review.md`，按相关问题查阅 `claim-review.csv`、
`review-report.json` 和 `literature-reading-notes.md`。原审查范围为 500 条导出声明、7 篇来源，
并不等于 17 篇文献的全库声明；本次没有重做整轮文献评审。

此前分页导出、图谱数量/截断提示、facts/participants、局部身份隔离、
定向 dry-run/审计/回滚均继续沿用。发现并补齐以下具体缺口：

|评审证据/范围|补充行为|确定性验证|
|---|---|---|
|4779 的抗体与靶点反向|结构隔离之外，原有候选纠错现在允许经过证据检查的 Drug/Intervention→Protein TARGETS 反向修复；仍不能删参与者、条件或随意替换实体|固定初次错误响应和纠错响应，保留细胞模型；正确抗体关系正例|
|4503 的 failed to influence；4656/4657 的 had no effect 和图号|按含实际终点的分句统一阴性表达；图号、物种缩写、小数不直接当作句末；自然生长阴性与阻断后阳性分别保留|使用报告原句及正反分句对照，检查存储状态和方向|
|4799 的 after；4800 的手术及 subsequently|补充临床事件时序风险；不把为 X 手术、随后 Y 改成 X 导致 Y|原证据置 pending；有明确因果原句的合法关系仍可通过|
|4820 的 FMT and anti-PD-1；4790/4688 的患者或受者反向作用|检查协调联合治疗表达与具名 co_intervention；患者响应/退出不变患者施加治疗效果|同一证据，缺少联合参与者进入复核，参与者完整的正例保留|
|4781、4808、4713 等实体/作用者/采样语义偏移|增加治疗疗效与靶蛋白、改变的菌与作用者、采样背景与疾病关联等复核；拓展一次抽取提示词|原句反例；不自动猜改主客体或科学结论|
|细胞标志、分类层级、机构与小鼠组、供受者、药物混杂及作者假说|提示词明确多个指标/时间、ORR和获益分母、引用归属、marker表达不等于细胞数量、局部身份和语义待复核标记|既有条件保留测试；`semantic:` 待复核原因不会被静默放行；真实模型效果未测|
|4542/4543/4637/4804 未定比较|保持未判定，不因 CSV 未包含 direction/comparator 就填造后台字段或判为反转|4 个 observable-only 案例，不纳入确定性通过率分母|
|推荐路线要求 claims 与 facts 的完整审计|新增独立完整 Facts CSV/JSON 导出、文献数、真实 fact/claim/relation/participant entity ID、证据和版本；Claims 增加 fact_id|601 条完整事实跨页导出、ID 集合、联合参与者及全部证据一致性；已有 1,107 条 claims 导出测试保留|
|单边证据等级、按研究显示|边标签与边型区分实验、观察/共现、预测、作者断言和未知；新增文献范围筛选|显示测试与跨知识库文献隔离测试|
|D06 ob/1；D07 两篇 DOI；D15 单位|补上 ob/1 定位、跨 chunk 显式 DOI 冲突定位；保留逐区域人工校正入口|nature4441021a/4441022a 边界候选、ob/+、500/100 μg 和 100 μM 的固定局部校正；正常文本和未校正 20 mg 保留|

回归数据在 `tests/fixtures/fmt_review_semantics.json`。其中 20 个明确测试输入的案例使用
报告原始 evidence_text，保留 source_anchors 和来源文件；端点类型、process 和候选修正是
显式的测试输入，不能据此声称原数据库字段就是这些值。生产 claim ID 仅作为证据出处记录，
运行规则不按 ID 分支，也不使用整句替换纠错。另有 4 个 CSV 信息不全的案例明确保持 unassessed。

运行本地语义回归，不需要数据库或模型：

```bash
PYTHONPATH=app python -m app.evaluation \
  --semantic-cases tests/fixtures/fmt_review_semantics.json \
  --output /tmp/fmt-semantic-gate.json
```

这检查固定候选/证据能否触发预期保留、隔离或复核，不能衡量真实模型的语义蕴含能力，
不能推出全库 precision/recall/F1。没有把报告中的 117 条非完全支持声明全部声称为已自动纠正；
其中部分仅因导出未展示条件而被判部分支持，必须回查完整 Facts 后决定。

本轮仅修改本地代码、测试和说明，没有运行生产库修复、没有重抽全库、没有调用实时模型。
`outputs` 及 ZIP 不含原 PDF，无法将 D07 的真实分栏区域坐标或 D06/D15 的页图校正直接应用到保存 chunks；
应先依使用说明生成限定文献/claim 的 repair 预览，核对实际原文与完整条件，再显式应用。
Facts 导出与本轮补充无需额外 SQL 迁移；应用修复计划仍依赖之前新增的 scoped_repair_runs 迁移。
