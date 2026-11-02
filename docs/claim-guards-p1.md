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

## V5：实体文本与确定格式

改动：claim_guards.py、normalization.py、graph_store.py、tests/test_entity_hygiene.py、test_entity_hygiene_integration.py、五条真实实体夹具及本说明/更新日志。

|规则|触发条件|动作|边界|
|---|---|---|---|
|V5-ENTITY-CLAUSE|实体text出现明确有限谓词，如is produced/increases/inhibits|needs_review|production等名词化过程不作为完整句谓词|
|V5-ENTITY-LENGTH|两端或辅助实体text超过20词|needs_review|20词以内不按长度触发，不截短实体|
|V5-TRUNCATED-SLOT|省略号、悬空连接词，或与原句对齐后明显在单词中间截断|needs_review|完整词、常规单复数变化不触发，不猜补原文|

归一化合并只处理确定的格式：sodium butyrate / sodium-butyrate / sodium_butyrate、gut microbiota分隔符、NAD+ / NAD +。
保护菌株、Genome、SequenceFeature、Other的标点；保护立体构型；保留数字、物种、变体、文献局部身份和标准ID冲突。
不通过编辑距离合并IFN/INF、utyrate/butyrate、bloodbrain/blood brain，也不跨Gene/Protein合并。

新别名键为有限的索引查找键，不使用全库正则扫描。历史格式节点仍通过既有upsert_entity(reconcile_existing=True)→_reconcile_exact_names→_merge_entities事务合并。
审计命令本身仍只写复核结论，不执行实体合并；新抽取和显式既有图谱重建使用相同归一化机制，没有新建合并路径。
数据库测试检查了claim ID/原始JSON保留、全部关系证据/事实证据/参与者与process引用迁移、冲突ID不合并、失败事务回滚和重复调用幂等。

19417/19418/19419/19421/19430五条实体句子污染使用真实夹具。23370/23371在提供包中assay文本实际完整，截断“or transf”用合成正反例验证，不伪称真实夹具仍有该截断。

V5阶段实测：完整PostgreSQL回归386项、5项可选浏览器跳过，27.350秒；累计缺陷集191/1175、台账40/79、SUPPORTED误报0/300、D1 21/21。

## 验证命令

## V6：自环、方法区与角色

改动：claim_guards.py、claim_semantics.py、claims.py、claim_audit.py、review_repair.py、graph_store.py、predicate_registry.py；
新增test_claim_structure.py、test_claim_structure_integration.py和12条真实结构夹具；更新两项既有回归的V6政策/新增查询预期。

|规则|触发条件|动作|边界|
|---|---|---|---|
|V6-SELF-LOOP|实际两端ID相同，或无ID时同名同类；同名DIFFERS_BETWEEN两端也需复核|needs_review|已知不同ID、Gene/Protein同名不当作实体自环；明确自调节仍按本轮政策复核，不删除|
|V6-METHOD-RESULT|服务器Methods/Materials/Statistical analysis中observed_change，或已标记method_description|新候选降unknown并保留方法风险；旧记录只入队|Results中仅提到methods不触发；服务器章节优先于模型字段|
|V6-CONTROL-AGENT|生物学断言以uninoculated/blank controls、标准品或reagent为施动者|needs_review|healthy controls以及作为参照参与者的对照不触发|
|V6-METHOD-INTENT|明确to evaluate whether/used to assess/tested for/normalized to等目的或操作，未报告结果|needs_review|有found/showed等明确结果不按目的句处理|
|V6-DISCOURSE-EFFECT|Assay施动者的have reported on文献叙述被当作生物效应|needs_review|不把真实Intervention当作研究方法|

实际ID检查同时接入写入、重投影和既有实体合并事务，覆盖不同名称归入同一节点及合并新产生的自环。
批量预览、应用复核和回滚使用相同的服务器章节/端点ID上下文；原始claim_json、句子和ID不由审计命令修改。
仅给既有result_interpretation增加method_description值，不新增result_status枚举或表；Python与SQL均按unknown读取。

完整PostgreSQL实测关键输出：

```text
Ran 398 tests in 27.369s
OK (skipped=5)
```

5项仅为可选浏览器测试，数据库集成均实际运行。验证包括Methods审计/回滚、模型章节不能覆盖服务器章节、不同名称同一实际ID、格式合并产生自环以及原始JSON保留。

累计P0+P1夹具预览：267/1175命中，涉及51篇文献；台账44/79（55.7%），仍未达到90%；SUPPORTED误报0/300；D1 21/21、D2六场景保持通过。
审计附件未保存section，section_known=0，因此附件统计不包含依赖章节的Methods规则覆盖；该规则由带服务器章节的单元/真实数据库测试验证，不能声称已在8758条生产claim上测得同样效果。

## 最终运行与未覆盖

本轮未部署、未推送、未访问正式数据库，也未执行历史实体合并或全库重建。原有.gitignore修改和未跟踪文件保留。
全部批量审计仍默认dry-run；apply只写复核结论。格式合并复用原事务，但只在相应既有写入/显式重建路径被调用时发生。
既有重投影对重复claim的处理未在本轮重写；没有自动触发历史重建，应先按现有流程检查重复项与共享影响。

未覆盖：隐含施受事/中介符号、缺失核心发现、跨句指代、无法绑定的混合数值、单位换算、拼写纠错、文章边界和完整ontology advisory治理。
未换模型、重写提示词或前端，不为连通率强行合并，不静默删改审计对象。

## 验证命令

```bash
PYTHONPATH=app:tests python -m unittest test_claim_comparison -v
PYTHONPATH=app python scripts/check_claim_guard_fixtures.py --audit-dir audit --output /tmp/p1-guard-report.json
# 配置可丢弃PostgreSQL+pgvector后，完整回归使用P0已验证的命名空间启动方式。
PYTHONPATH=app:tests python -c "import app,sys;sys.path.append('frontend');import unittest;unittest.main(module=None,argv=['unittest','discover','-s','tests','-v'])"
node tests/test_graph_view.cjs
```

全部批量入口继续默认dry-run。旧记录审计只写复核结论，不改原始claim_json、证据或claim ID。
