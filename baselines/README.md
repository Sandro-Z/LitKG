# 外部系统基线（PubTator3 / Neo4j LLM Graph Builder）

在与某个 LitKG 知识库**完全相同的语料**上，用外部系统构建图谱，并导出为 QC canonical JSON，与 LitKG 横向评测。代码在本目录的 `kgb` 包中，不导入 LitKG 业务代码。只有校验步骤会只读地调用 `quality_control.models.normalize` 试跑。

| 变体 | 实体 | 关系 | 输入文本 |
|---|---|---|---|
| `pubtator3-native` | PubTator3 自由文本 NER | PubTator 预计算的文档级关系（需要 PMID） | same-text |
| `pubtator3-llm` | 同上 | 与 LitKG 相同的 LLM 按窗口抽取，有 policy 谓词清单、domain/range 校验和逐字 quote 校验 | same-text |
| `neo4j-builder-sametext` | 构建器 LLM，节点标签为 policy 实体类型 | 构建器 LLM，关系为 policy 谓词 | same-text `.txt` |
| `neo4j-builder-pdfnative` / `-no-schema` | 可选变体 | | 原 PDF / 不给 schema |

## 一条命令运行（后台、可断点续跑）

前提：LitKG 知识库已经导入 QC（`POST /api/import/litkg`），并记下 QC 图谱 ID；主机有 Python ≥ 3.11；跑 Neo4j 变体还需要 Docker Compose。

```bash
scripts/run_baselines.sh start --qc-graph <QC图谱ID> --llm-base-url http://<host>:<port>/v1 --llm-model <与LitKG相同的模型>
```

第一次运行时会提示输入 LLM API Key（输入不回显）；也可以先 `export LLM_API_KEY=…`。Key 保存在 `data/baselines/secrets/llm_api_key`（权限 600），不会出现在日志、配置或输出中。

```bash
scripts/run_baselines.sh status
```

```bash
scripts/run_baselines.sh logs
```

```bash
scripts/run_baselines.sh stop
```

```bash
scripts/run_baselines.sh down
```

`down` 会停止 Neo4j 和构建器容器。

脚本会依次完成：
1. 在 `data/baselines/venv` 创建虚拟环境，安装 `requirements.txt`（固定版本）。
2. 从 QC 下载该图谱的 `documents` 和 `policy`，原样复制，因此 `corpus_hash` 一致。
3. 生成 same-text 文本，然后依次运行 `pubtator3-native`、`pubtator3-llm`、`neo4j-builder-sametext`。
4. 跑 Neo4j 变体时，克隆固定版本的 `neo4j-labs/llm-graph-builder` v0.8.7（commit `1ade47de…3142`）到 `data/baselines/vendor/`，并用 `deploy/docker-compose.builder.yml` 启动专用 Neo4j 5.26 + APOC 和构建器后端。端口只绑定 127.0.0.1：Neo4j 为 17687，后端为 18000，可用 `KGB_NEO4J_BOLT_PORT`、`KGB_BUILDER_PORT` 修改。Neo4j 密码自动生成。这个数据库专供基线使用，每篇文档之间都会被清空。
5. 校验所有输出，生成 `data/baselines/out/stats.md`。

断点续跑：
- 再次执行 `start`（不需要参数）会跳过已产出的变体。
- PubTator 响应（按文本 sha256）、待取回的会话、LLM 响应（按提示词 + 窗口文本 sha256 + 模型）、已完成的 Neo4j 文档都缓存在 `data/baselines/cache/`，命中缓存时不会重复调用。
- `--force` 会用缓存重建输出。
- `--variants "pubtator3-native neo4j-builder-pdfnative"` 可以指定要跑的变体。
- `KGB_WORK=<目录>` 可以把工作目录放到别处，用于同时处理多个语料。

产出在 `data/baselines/out/`：`graph.<variant>.json`、`stats.md`、`run_manifest.json`。

## 导入 QC 比较

1. QC“导入图谱 → 通用 JSON”，上传 `data/baselines/out/graph.*.json`。不要再附加原文文件，否则 `corpus_hash` 会改变。
2. 对每个图谱运行“原文 LLM 评审”。所有图谱用同一个评审模型，建议与抽取模型不同。
3. 在比较页确认“语料一致：是”和“范围/本体一致：是”，再看分数。
4. 如果语料是金标准数据集，再运行“金标准评测”，选择 `pubtator3` 或 `neo4j-builder` 档案。

## 可比性与已知差异

每个输出的 `metadata.consistency` 和 `stats.md` 都列出了与 LitKG 的差异，主要有：
- PubTator 的关系来自它自己的 BioREx 模型，而 BioREx 在 BioRED 上训练过，所以在 BioRED 上的分数偏乐观。
- 构建器的切块和提示词与 LitKG 不同；`temperature` 固定为 0，`max_tokens` 不可设置。
- PubTator 自由文本接口未就绪时返回 HTTP 400，而不是文档写的 404。

细节见 `docs/pubtator3_api_notes.md` 和 `docs/neo4j_schema_notes.md`。

## 开发与测试

```bash
cd baselines && PYTHONPATH=. KGB_QC_PATH=.. python -m unittest discover -s tests -v
```

也可以不用脚本，直接调用 CLI：`PYTHONPATH=baselines python -m kgb.cli --config <config.toml> prep|run|validate|stats`。配置样例见 `config.example.toml`。
