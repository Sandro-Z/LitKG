# FMT_use 评审修复与使用说明

本次以本地 `FMT_use` 代码为准。补充读取了 `outputs/LitKG-review.md`，并对照
`claim-review.csv`、`review-report.json` 和 `literature-reading-notes.md` 回查相关证据。
实际报告原句已接入固定回归；CSV 未导出的后台条件和方向仍不猜补。
证据 ZIP 不含原始 PDF，实际文章区域、基因型和微单位仍须对照页图后应用。
新增核对项及未验证范围见 [评审材料补充核对](FMT-review-evidence-followup.md)。

## 完整导出与分页

Claims 页面提供页码和每页数量，显示本页数量与筛选总数。
点击“准备完整导出 CSV / JSON”，再下载完整文件；提示为
“已导出数量/总数量”。导出不受当前列表页或图谱绘制上限影响。
导出内容对应点击时的数据库快照，继续入库后应重新准备导出。

```bash
# 请替换知识库 ID；以下文件保存在执行 curl 的机器上。
curl -f 'http://localhost:8080/knowledge-bases/2/claims?limit=100&offset=500'
curl -f 'http://localhost:8080/knowledge-bases/2/claims/export?format=json' -o claims-all.json
curl -f 'http://localhost:8080/knowledge-bases/2/claims/export?format=csv' -o claims-all.csv
curl -f 'http://localhost:8080/knowledge-bases/2/facts/export?format=json' -o facts-all.json
curl -f 'http://localhost:8080/knowledge-bases/2/facts/export?format=csv' -o facts-all.csv
```

两个导出格式均支持 `predicate`、`entity_type`、`result_status` 筛选。
JSON 顶层含 `claims`、`exported_count`、`total`、`knowledge_base_id`；
响应头另含 `X-Exported-Count`、`X-Total-Count`。CSV 保留字段并将嵌套对象编码为 JSON 字符串。
服务端在一个 repeatable-read 只读事务中按 claim ID 稳定排序，以 ID 游标遍历全部分页，
并核对实际导出数；避免并发写入造成 offset 漂移、重复或漏项。

导出保留原始 claim、文献、chunk、实体 ID，完整 `claim_json`、qualifiers、
participants、两端 process、主证据和补充证据、标题/DOI/文件/页码、复核项与状态，
以及已保存的模型、processing/catalog 版本、chunk 抽取版本和文献处理代次。
旧记录没有保存的生成信息保持缺失或 unknown，不根据当前配置猜补历史版本。

Claims 另返回 `fact_id`，可与完整 Facts 导出中的 `id/claim_id/relation_id` 对照。
Facts 导出独立遍历全部当前事实，包含真实参与者实体 ID、全部主/补充证据、
qualifiers/process、原始 claim_json、复核状态、来源和已保存版本；返回 `document_count`。
支持 `document_id`、`entity_id` 筛选，使用同样的快照与稳定 ID 分页；
Claims 保留全部原始行，Facts 的“当前”范围按已有 catalog 版本规则限定。

## 语义与图谱

- 抗体作为干预，PD-1/PD-L1 作为靶蛋白；TARGETS 从干预指向靶点。
  测量自身、招募来源误作分类等结构错误沿用候选纠错/隔离队列。
- 时间顺序、未证实的自调节、缺少联合干预参与者、相关/预测冒充因果、
  关键条件不在证据内等风险标为 `semantic_status=pending` 并生成复核项。
  默认确认图谱与证据问答排除这些候选；勾选“包含待审核候选”可查看紫色虚线。
  规则只筛查明确模式，不能代替全文语义判断，也不保证发现所有抽取错误。
- `did not affect` 和明确的 `unchanged` 统一为 `result_status=no_effect_reported`；
  AFFECTS/DIFFERS_BETWEEN 使用 `negated=false`，相应 process.direction 为 `unchanged`。
  未记录测量方面时只保留已有方向字段，不编造 process.kind。
- 未显著差异使用 `result_status=not_significant`，不能推导为已证明没有效应。
  保留已报告的估计方向，未报告时为 unspecified。`negated` 仍用于真正否认关系的断言。
  复杂、多结果的证据需要复核；程序不通过整段关键词匹配重写其方向。
- 列表/图谱的 `result_status` 筛选与显示读取同一套状态约定；
  兼容旧 `negated AFFECTS` 与 `unchanged` 编码；原始 negated 字段保留，`display_negated` 提供统一展示语义。
  “不包含否定”仍保留未见变化与未显著结果。仅 CSV 未提供方向并不能证明后台方向错误。
- `source_kind=primary|cited|background|unknown` 表示来源归属；`speculative` 只表示推测。
  旧记录默认 unknown，保留其已有 speculative 值。研究组、样本与 OTU 的文献局部身份规则保持不变。
- 联合干预继续使用 participants（如 `co_intervention`），供/受体、模型、饮食、
  用药、时间、比较组、指标和方向继续保存在 facts、participants、process 和 qualifiers 中。
  不将联合方案拆成单药结论。

图谱显示当前关系数、筛选总数和截断提示；绘制上限仍由滑块配置。
单边和平行边均显示简短方面、方向和比较对象。否定、未显著差异及待审核分别标识；
全部条件、参与者和出处在悬停信息及“关系详情”卡片中查看。
“关系显示范围”提供全部、生物学、方法/样本（MEASURED_BY、SAMPLED_FROM）关系。
图接口新增参数：`relation_layer=all|biological|method_sample`、`include_pending=false`、`result_status`。
可用 `document_id` 或前端文献 ID 筛选限定研究范围，仍受当前知识库成员关系约束。
边标签显示证据等级；实验、观察/共现、计算预测、作者断言、未知分别使用对应边型，不把未知画成实验等级。
summary 新增 `displayed_relation_count`、`matched_relation_count`、`truncated`；原字段保留。

标记复核项 resolved/dismissed 只记录审核决定，不自动改写事实或提升 pending 候选为已确认关系。
需修正证据/条件并再次预览应用；有原文明确支持的自调节仍允许通过。

## 输入质量定位、校正与定向修复

新解析会定位可疑 `1/1`、`ob/1`、含 mg/mM 的数值及潜在相邻文章起点，保留上下文、字符区域和页码，
进入现有 input_quality 复核队列。这些都是候选提示：正常比例、毫克和毫摩尔不能自动改为基因型或微单位。
旧文献可用下面的预览检查已保存 chunks，无需重新上传。
跨 chunk 的多个显式 DOI 标记也会定位为文章边界候选；参考文献可能造成同类提示，不能自动切除或改变来源归属。

先应用幂等迁移；此迁移只增加修复审计表，不更新现有事实：

```bash
docker compose exec -T postgres sh -lc \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 --single-transaction' \
  < db/migrations/20261021_scoped_repairs.sql
```

普通预览不写数据库，也不调用模型。保存的计划包含原始快照、输入问题、增删改及依据，文件权限为 0600：

```bash
docker compose exec -T api python -m app.review_repair \
  --document-id 31 --mode repair --output /tmp/doc31-repair.json
docker compose exec -T api python -m json.tool /tmp/doc31-repair.json

# 也可以只检查同一篇文献内的指定 claims
docker compose exec -T api python -m app.review_repair \
  --claim-id 101 --claim-id 102 --mode repair --output /tmp/claims-repair.json
```

`repair` 只做已有记录的结果语义规范化和风险检查；无法确定的角色不自动猜改，
结构错误的旧记录保留原始实体和证据并标 pending。疑似问题及原候选写入复核队列。
`reproject` 只重建已保存事实及投影，不补回从未抽出的事实，也不调用模型。
`reextract` 在预览阶段显式调用配置的原有模型服务，再做相同的证据/语义检查；应用计划时不重复调用模型。
文献范围覆盖其已保存 chunks；claim 范围只请求复核指定候选，不把整个 chunk 的其他结论一并覆盖。
重抽沿用当前目录，目录外提及保持独立、可复核；它不等于重新解析 PDF 或重新建立目录。

```bash
docker compose exec -T api python -m app.review_repair \
  --document-id 31 --mode reproject --output /tmp/doc31-projection.json
docker compose exec -T api python -m app.review_repair \
  --document-id 31 --mode reextract --output /tmp/doc31-reextract.json
```

需校正原始输入时，准备 JSON 数组，针对每个受影响 chunk 给出零起始的 `[start,end)` 字符区域。
`original` 必须逐字匹配，`replacement` 为修正值；空字符串表示排除已确认的相邻文章区域。
必须附 `page` 或 `region` 和非空 `basis`，建议记录 PDF 图像、图注或经核对的正文位置。
下例只是格式示例，不能直接作为 4441022a.pdf 的真实坐标：

```json
[
  {"chunk_id":80,"start":120,"end":123,"original":"1/1","replacement":"+/+","page":3,"basis":"人工核对 PDF 第3页基因型原文"},
  {"chunk_id":80,"start":200,"end":204,"original":"5 mg","replacement":"5 μg","page":3,"basis":"人工核对 PDF 第3页方法段单位"},
  {"chunk_id":80,"start":300,"end":304,"original":"2 mM","replacement":"2 μM","page":3,"basis":"人工核对 PDF 第3页浓度单位"}
]
```

```bash
docker compose cp corrections.json api:/tmp/corrections.json
docker compose exec -T api python -m app.review_repair \
  --document-id 31 --mode reextract --corrections /tmp/corrections.json \
  --output /tmp/doc31-corrected.json
```

输入校正只允许文献范围的 reextract，确保受影响的旧断言被重新评估；不能只改文字而保留失配证据。
重叠 chunks 中的同一错误须分别核对并列出区域。原文与修正值、页码/区域及依据都保存在计划和审计表。
无法确定时不填修正值，保持 pending；抽取验证失败时保留旧记录，含未解决输入校正的计划不能应用。

检查计划后显式应用；命令本身不支持全库重抽，也不会自动应用：

```bash
docker compose exec -T api python -m app.review_repair --apply-plan /tmp/doc31-repair.json
# 返回 run_id；替换下方 RUN_UUID。回滚默认也只预览。
docker compose exec -T api python -m app.review_repair --rollback RUN_UUID
docker compose exec -T api python -m app.review_repair --rollback RUN_UUID --apply
```

应用在一个事务中再次校验原文、claim 快照、处理版本及范围，保存 claim_history 和修复审计；
生成新 claim 时记录新 ID 与原 ID 的对应计划，不能承诺重抽后 ID 不变。
回滚恢复原 claim ID、JSON、chunk 原文/位置、facts、参与者、证据和复核项；
有后续写入、处理代次或审核决定变化时拒绝覆盖。其他维护若已删除相关实体/聚合关系，
精确回滚会因引用缺失而失败并保持事务原状，应从备份恢复或重新制定修复计划。
派生索引/摘要记录 dirty 标记；应用或回滚后按原有 `derived` maintenance 更新当前文献。
共享文献仍影响所有包含它的知识库。执行前应按现有运维流程备份目标数据库。

## 测试与验证范围

```bash
PYTHONPATH=app:tests python -m unittest discover -s tests -v
node tests/test_graph_view.cjs
PYTHONPATH=app python -m app.evaluation \
  --semantic-cases tests/fixtures/fmt_review_semantics.json --output /tmp/fmt-semantic-gate.json
# 显式指向可丢弃 PostgreSQL（需要 vector、pg_trgm）；测试建立并清理随机 schema
LITKG_TEST_DATABASE_URL=postgresql://... PYTHONPATH=app:tests \
  python -m unittest test_review_integration -v
```

新增测试使用固定原文、固定模型响应，验证 500 条以上完整导出与嵌套字段、分页空页总数、
语义角色/联合干预/合法自调节、阴性结果存储与筛选、图谱状态与条件、精确文本校正、
定向预览、事务应用、过期计划拒绝及回滚。实际模型抽取效果未运行；
报告原证据的定向规则已验证；生产数据修复效果及真实 PDF 页面上的校正依据未验证。
