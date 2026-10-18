# 批次 22：失败回放与最小修复

## 核对范围

证据包：`litkg-evidence-20261018T021506663389Z.tar.gz`，SHA-256：
`2ef44cd13b9adbb6c65b2cac998323233bc71a4e5a70d04e87aa8751e683e2e0`。
读取了 git 状态、原始 worker/PostgreSQL 日志、数据库 JSONL、原始模型输出与复核候选。

包内 HEAD 为 `5f4e593`，当时记录了未提交的删除/重跑相关修改。
本次开始时实际分支是 `FMT_use`，HEAD 已为后续提交 `6f333af`，
已跟踪文件无未提交差异，证据包为未跟踪文件。以当前提交为基线修复，
没有 reset、回退或覆盖后续提交；没有提交、推送、部署或操作正式数据库。

## 根因与修改

| 已确认缺陷 | 原始证据 | 修改边界 |
| --- | --- | --- |
| 并发汇总死锁被误报为解析失败 | PostgreSQL 两方均执行整批 `UPDATE ingestion_batch_items`；文献 118 在 `update_document_status(..., parsing)` 内的汇总同步抛出 `DeadlockDetected`，尚未调用 PDF 解析器 | `batch_status.py` 先锁批次行，多批次按 ID 升序；状态/错误/计数无变化不写。worker 死锁退出整个事务后用新连接重试，API 用保存点回滚后重试；均最多 3 次。`tasks.py` 将派生汇总失败单独记录，保留已提交的文献状态，继续真实解析/抽取 |
| Catalog 重复生成 | attempt 1261 因 `finish_reason=length` 失败；截断前 41 个完整对象全为 Bifidobacterium，36 个对象完全相同 | 提示词要求同一实体/提及只列一次，覆盖全部唯一实体后闭合 JSON。完整 JSON 中显著重复循环也会触发现有有限纠错/失败。截断继续走原有分段机制，绝不接受截断前缀或提高 token 上限冒充修复 |
| DIFFERS_BETWEEN 表示不一致 | 59 条缺 comparison_group 的复核中，56 条实际提供的是实体对象；另有重复组、标签/参与者不一致、证据缺失 | 新输出明确使用字符串标签并给出可校验示例。兼容旧的 Entity 形状对象时，只将其已有 mention/text 转为标签，保留实体参与者并记录修复；未知附加条件不丢弃。后续仍检查 StudyGroup 类型、两组不同、焦点对象、原文可定位和方向一致。不猜补组别 |
| 未转义引号及证据/边界错误 | attempt 1304/1306 在未转义的 `"homeostatic"` 处 JSON 失败；Rs/NR 有只出现在片段其他上下文、未包含在已选证据中的情况；chunk 1536 尾部为 `efficac`，另有模型引述 `effic` | 提示词明确 JSON 转义与必要条件的证据选取。只对能逐字定位当前输入的 evidence 字符串做引号转义，保留原始输出和修复偏移，随后严格解析整个 JSON。新 PDF chunk、初始分段及递归重叠边界优先选原句/空白，保持原文及完整覆盖。不能自动补全断词、引用未提供的相邻片段或删除组别条件 |
| 分类单元/菌株及 MEMBER_OF 混用 | 目录把 B. longum、B. adolescentis 标为 MicrobialStrain。attempt 1283 原候选为 `OTU 559627 SUBCLASS_OF B. longum`，复核中的 MEMBER_OF 是后续错误纠正；attempt 1334 用 MEMBER_OF 连接片段中的 `hed taxa` 与 Firmicutes/Actinobacteria | 无菌株身份后缀的裸双名按现有 MicrobialTaxon 词表规范化，保留真实菌株编号。明确 taxonomy 与 community membership 的提示。OTU 分类注释不能靠换成 MEMBER_OF 放行；两个错误版本仍拒绝。没有放宽端点约束或添加谓词 |

纠错原先只检查参与者角色数量和限定字段是否存在，未比较实体及条件值，
可能把片段中的另一事实当作原候选的修复。现在同时约束主语、宾语、参与者身份、
谓词含义、方向、过程、极性及已提供的条件值；证据可以改为支持同一断言的原文。
新回归明确拒绝换实体、换组别、翻转方向、改剂量或删除必要参与者。
两项旧测试曾把 Imaginary protein 换成 IL-6/AKT1 视作成功，现改为修复同一断言的证据，
保留它们原本测试的缓存、手动排除和失败续接目标。

### 缓存及隔离

不清理缓存、不更换全局抽取代次。新边界用于没有旧计划的输入；
发现已有成功片段或旧分段计划时，按原偏移及 v1 分段树继续复用。
新分段树的策略标记保存在已有 processing_cache JSON 中，不增加表或字段。
边界变更不会使已成功叶子全部失效；原文校验仍逐候选执行。

组别格式兼容不代表 56 条断言都正确：真实样本中仍有 Rs/NRs 合一、
焦点对象不匹配、必要参与者未落在证据内等错误。这些继续隔离，
没有通过清空复核、强制 done 或改写为其他事实降低报错数。

## 实际验证

- 完整回归：**226 项通过，无跳过**。使用已有 Python 测试环境及独立 PostgreSQL 15/pgvector 测试容器；真实模型、外部 HTTP 和 Celery 发布使用测试替身。
- 并发：6 个文献 worker 加 2 个批次轮询者，两个共享批次，验证完成状态及无变化同步不产生新的行版本/更新时间。
- 回滚：真实 PostgreSQL 抛出 SQLSTATE 40P01，验证 worker 回滚后换连接；API 保存点回滚保留调用方先前写入，再在同连接重试。另验证连续死锁最多重试 3 次。
- 原始输出：1304/1306 各恢复 12 个结构合法候选、各记录 4 处原文支持的引号修复，raw 保持原样；原文不匹配时仍失败。这只证明 JSON/原文结构，不证明每个生物学断言正确。
- 重复循环：用 1261 的原始截断输出验证不接受前缀；另用其中 41 个完整对象测试完整 JSON 重复循环，有限失败且不污染其他成功缓存。
- 59 条组别失败及其他真实候选、旧分段树/成功叶子复用、必要条件、错误谓词、chunk 1536 断词、原始页码偏移与覆盖范围均有回归。

样本在 `tests/fixtures/batch22_failures.json`，含原包摘要和 attempt/review ID；
测试位于 `tests/test_batch22_regressions.py`、`tests/test_batch_status.py`
及 `tests/test_knowledge_integration.py`。完整测试日志在本机
`/tmp/litkg-batch22-regression.log`，保存点/并发补充日志在
`/tmp/litkg-batch22-concurrency.log`。
临时数据库仅监听本机、使用 tmpfs 且没有正式卷挂载；验证后已停止、删除并核对容器不存在。

## 未验证项

没有调用真实模型重跑批次 22，没有验证部署后的实际覆盖率或吞吐。
请求输出预算、API 和依赖保持不变。日志中的 requested max_tokens 和客户端
enable_thinking 设置不能证明模型服务实际输出上限、上下文上限或 thinking 状态。
没有自动修正文献 118 的正式状态、重抽全库、重解析旧 PDF 或合并跨文献实体。
已有缓存的旧边界可能仍带有截断上下文；这些候选继续隔离，需要用户另行选择小范围验证，
不能声称正式批次已经恢复或所有复核项已经解决。
