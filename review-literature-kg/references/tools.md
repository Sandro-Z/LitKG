# 工具与宿主接线

内置工具由 scripts/kg_review_tools.py 提供，全部只读本次规范输入和报告。依赖 Python 3.10+ 标准库；其受限 Schema 检查器仅支持本包所用关键字，不是通用 JSON Schema/SHACL 引擎。PDF 接入脚本另需 pypdf，纯文本不需第三方依赖。

| 工具 | 用途 | 返回重点 |
|---|---|---|
| inventory | 清点、固定快照 | 图/语料/规则哈希、规模、可读性 |
| validate_structure | 全量形式检查 | ID、引用、精确重复、显式实体类型规则 findings |
| get_claims | 分页取声明与关联实体 | claims、nodes、next_offset |
| search_sources | 全语料或指定文献词项检索 | 命中块、发现用片段、检索块数；不是语义搜索 |
| read_block | 读取完整证据块 | 原文、物理页、章节、块哈希 |
| verify_evidence | 精确片段验证 | valid、reason、block_hash |
| plan_review | 全量或固定 seed 伪随机无放回抽样 | 已选与未审 claim_id；不产生总体估计 |
| compute_metrics | 程序计算指标与门禁 | 逐标签计数、覆盖、比率、overall |
| validate_report | Schema 与交叉字段校验 | 锚点、ID、快照、指标/门禁和完整性错误 |

契约见 assets/tool-definitions.json。调用：

```json
{"name":"read_block","arguments":{"request_path":"/absolute/path/request.json","doc_id":"D1","block_id":"b1"}}
```

返回统一 envelope：`{"ok":true,"result":{...}}`；失败为 `{"ok":false,"error":{"code":"...","message":"..."}}`，退出码 1。search_sources 对 NFKC/casefold 后的空格分词做词项匹配，中文可逐次用实体别名或整词查询；没有召回不证明没有事实。检索截取片段不用于最终判断，必须补读完整块及上下文。

inventory 提供文献清单；完整块索引在 request.documents[].blocks。宿主按文献序列或块 ID 给遗漏模块分配块；不能只把检索命中的块算作所有来源。字面量类型、数值换算、图论性质、事件模式等须由规则模块或明确扩展校验；本工具不宣称实现完整本体推理。

## 服务适配约束

1. 将 tool-definitions 中 name/description/parameters 映射到选用宿主的 function/MCP 接口；这是平台中立契约，非某平台可直接上传的 Actions OpenAPI。
2. 服务端将 request_path/report_path 映射为本任务授权文件；校验路径、参数和输入大小，不向远程用户开放任意服务器路径。工具进程没有网络和图数据库写权限。
3. 宿主记录真实调用的 tool name、参数摘要、输入快照、block_hash 与返回结果。报告验证器仅能检查声明的阅读记录，不能证明模型确实读过或正确理解；宿主应将证据锚点与调用日志相连。
4. 若提供 OCR/页面视觉，保存原页、区域 bbox、识别文本与版本，确认后才提升 parsing_status。不能把自动 OCR 成功等同于核验通过。
5. 若提供 RDF/Neo4j，先只读导出并固定版本，再映射规范图。SHACL 仅运行用户允许的 shape，禁用其任意脚本和网络扩展；不是默认开启的工具。
6. 若向量/NLI/实体链接器可用，只用于候选召回或排序。最终语义判定仍要回原文。

## 建议执行顺序

调用 inventory → validate_structure → plan_review → get_claims，并用 search_sources/read_block/verify_evidence 审证据。独立遗漏抽取完成后冻结候选，再匹配图谱。保存 report.draft.json（暂不带 metrics），调用 compute_metrics 后填回 metrics，调用 validate_report。脚本不会替模型判断语义，也不会自动运行大模型循环。

模型最终 JSON 应通过完整宿主 JSON Schema 校验和这里的语义交叉字段检查。自定义 schema 使用的字段是稳定接口；跨模型结构化输出若不支持某个关键字，由适配器转换，不能删除业务约束。
