# 输入契约

用户只需提供：①生成图谱（含节点、边及事实属性，优先可机读导出），②生成图谱时实际使用的文献原文/补充材料。接入阶段生成 request.json；不要要求用户先人工为全部文献标注证据。

可选：ontology、抽取范围/排除项、生成器版本和提示词、原始引用、人工标注。缺本体保留结构形式检查，缺 scope 保留保真检查和候选缺口，但完整性维度未完成。

## 规范格式

`schema_version: "1.0.0"`；graph 包含 nodes、claims；documents 包含全文块；policy 包含 scope、ontology。

- node：id、label、type（未知可 null）、aliases。名称/别名/类型作为实体元数据检查；外部实体标识、药效等事实性属性须映为额外 claim，不能塞进元数据绕过评审。
- claim：id、subject、predicate、object={kind:entity|literal,value,datatype}、qualifiers={negated,modality,context}、source_refs。modality 为 asserted/possible/hypothetical/unknown；context 是字符串键值，保存物种、人群、条件、时间、比较组、单位等。未声明的值为 null/unknown，不由接入器脑补。
- document：id、title、version、source_file、source_sha256、parsing_status=complete|partial|unreadable、blocks。source_sha256 是原文件字节哈希，可缺省为 null；标题/版本不能编造。
- block：id、page（物理页，非 PDF 为 null）、section、kind=text|table|figure_caption|other、text。最大 20,000 code points；超过时拆为稳定子块并保存偏移映射。不同版本不可复用同一 doc_id。
- source_ref：doc_id、block_id、start、end、quote、role。start/end 为块内 Unicode code point 偏移；0 起点、右端不含。quote 是锚定文本精确片段，不能用改写后的话。
- policy.scope：原抽取任务的自然语言范围，缺失用 null；ontology 可 null，或显式 node_types 和 predicates（domain/range）。词表之外的名称只在该显式本体契约下判错。

原图若含 n 元关系，保持事件/声明实体及参数角色，不强压成丢失参数的二元边。RDF blank node、URI、named graph、literal datatype/language 要在适配映射中保留；Neo4j 属性须明确哪些是事实。未实现自动转换器时由宿主提供并校验转换前后计数和映射。

示例与完整字段约束见 assets/request.schema.json。所有 additionalProperties 默认 false，context 键值是显式允许的扩展区。不能无声丢字段；新字段需新版本或可追溯映射。

## 原文接入

ingest_sources.py 接收 manifest.json：

```json
{"documents":[{"id":"D1","file":"/absolute/path/paper.pdf","title":"实际标题","version":null}]}
```

输出 documents 数组，可组合进 request.json。TXT/Markdown 按段分块；PDF 按物理页保留文本后分块，但默认 partial：还需核对双栏顺序、表格、图像、公式与补充材料。无文字页标为待 OCR，不能当无事实。视觉核验后才能人工更新 parsing_status。语义文献覆盖不依赖固定字符窗口；截断证据须补读邻块。

运行快照包含 graph/corpus/policy 的规范 JSON SHA-256。原始导出与适配映射留在工作目录，不修改原件；复跑缓存键至少包含这三种哈希和规则版本。
