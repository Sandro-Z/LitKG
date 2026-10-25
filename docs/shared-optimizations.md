# 通用优化回流范围与升级说明

以 main 的 c0d4d91 为基线，从 FMT_use 的 5f4e593 至 1644fe0 按功能提取。
只迁入跨议题的处理、校验、维护和展示能力；FMT_use 分支保持原样。

| 来源 | 回流内容 |
| --- | --- |
| 5f4e593 | 受控谓词框架、端点类型约束、process 与参与者投影、比较组/样本的文献局部身份 |
| 6f333af | 删除最后所属知识库后的缓存清理、共享文献保留、活动任务与重跑保护 |
| f4e6ba3 | 批次锁序和死锁处理、派生索引失败隔离、重复目录检测、原文引号转义、比较组格式兼容、切块边界及缓存续接 |
| c3615a8 | DOI 规范化、PDF 元数据与正文回退、页码回归 |
| e765ec1、4745073 | 稳定实体配色、关系条件/方向显示、比较关系身份规范化及显式修复工具 |
| 3475ceb、c5eaa8a | 自动/层次/环形整理、位置恢复、白底完整 PNG、移除应用像素限制、编码失败提示 |
| 1644fe0 | Claims 分页与完整 Claims/Facts 导出、结果/来源状态、待复核隔离、图谱分层/文献筛选/总数、输入定位、限定范围修复/审计/回滚 |

main 沿用基础生物医学类型，仅增加共享流程需要的 StudyGroup、Biospecimen、
Intervention。process 保留跨议题的 expression、secretion、activity、abundance、
prevalence、growth、production、degradation、conversion、concentration。
不引入微生物专用类型、菌株推断、生态谓词、定植/多样性过程、FMT 专用提示词或
FMT 联合抗体规则。历史 batch22 fixture 仅回放 JSON、原文校验、切块和续接故障，
其词汇不构成运行时类型或领域规则；FMT 评审证据集留在 FMT_use。

受控类型与谓词分别可由 `/entity-types`、`/predicate-types` 查询。
main 使用 general 版本标识；Proteomics 保留自己的类型、谓词、提示词、
processing/catalog 版本和蛋白形式、序列、位点及母蛋白 accession 身份保护。
growth/prevalence 是两分支共用过程，蛋白质组学特有过程继续保留。
版本变化不会自动重抽旧文献；需要补齐历史内容时显式运行 reextract。

## 导出与图谱

Claims 页支持分页、总数和完整下载。以下 ID 仅为格式示例，需替换为实际知识库：

```bash
curl -f 'http://localhost:8080/knowledge-bases/2/claims?limit=100&offset=500'
curl -f 'http://localhost:8080/knowledge-bases/2/claims/export?format=json' -o claims-all.json
curl -f 'http://localhost:8080/knowledge-bases/2/claims/export?format=csv' -o claims-all.csv
curl -f 'http://localhost:8080/knowledge-bases/2/facts/export?format=json' -o facts-all.json
```

完整导出在 repeatable-read 只读快照内按 ID 遍历，并核对总数；保留原始 claim、
事实/参与者 ID、限定条件、process、全部证据、出处、复核和已保存版本。
缺失的历史生成信息保持 unknown。Claims 与 Facts 支持 predicate、entity_type、
result_status 筛选；Facts 另支持 document_id、entity_id。

result_status 区分 observed_change、no_effect_reported、not_significant、unknown；
未显著差异不能推导为无效。source_kind 与 speculative 分开，旧记录保留兼容读取。
semantic_status=pending 的候选默认不进入确认图谱或证据问答，可显式勾选查看。
图谱显示当前/匹配总数和截断提示，支持文献及生物学/方法样本关系筛选。

## 存量数据库升级与定向修复

合并代码不执行部署、数据修复或重抽。已有数据库需在使用修复工具前应用新增审计表迁移：

```bash
docker compose exec -T postgres sh -lc \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 --single-transaction' \
  < db/migrations/20261021_scoped_repairs.sql
```

普通 repair/reproject 预览不调用模型、不写数据库；reextract 预览会调用已配置模型。
范围必须是单篇文献或同篇的指定 claims，不能隐式全库重抽。

```bash
docker compose exec -T api python -m app.review_repair \
  --document-id 31 --mode repair --output /tmp/doc31-repair.json
docker compose exec -T api python -m app.review_repair --apply-plan /tmp/doc31-repair.json
# 用上一步返回的 run_id 替换 RUN_UUID；回滚默认也只预览
docker compose exec -T api python -m app.review_repair --rollback RUN_UUID
docker compose exec -T api python -m app.review_repair --rollback RUN_UUID --apply
```

计划文件权限为 0600。应用事务会核对原文、claim 快照、代次、版本和范围，保存历史与
审计；过期计划或后续写入会阻止覆盖。输入校正必须包含精确字符区域、原值、修正值、
页码/区域及人工依据，只允许文献范围 reextract；未解决的提取失败保留旧记录。
回滚恢复原 claim ID、原文、事实、参与者、证据及复核状态；相关引用已被其他维护删除时
会拒绝应用。执行正式修复前应备份目标数据库。共享文献影响所有包含它的知识库。
应用/回滚后用原有 derived maintenance 更新当前文献的索引和摘要。

## 本地验收

```bash
PYTHONPATH=app:tests python -m unittest discover -s tests -v
node tests/test_graph_view.cjs
# 显式指向可丢弃 PostgreSQL 后，数据库集成测试才会运行
LITKG_TEST_DATABASE_URL=postgresql://... PYTHONPATH=app:tests \
  python -m unittest test_review_integration test_graph_integration test_knowledge_integration -v
```

固定模型与合成文献用于验证存储、隔离、缓存、完整导出和修复事务，不能代表真实模型
抽取准确率。PNG 仍受浏览器自身 Canvas 与内存能力限制。
