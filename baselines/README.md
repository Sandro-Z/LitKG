# 外部系统基线：PubTator3 / Neo4j LLM Graph Builder

两个互相独立的一键脚本。输入是**与 LitKG 相同的一批 PDF** 和**一个 LLM API**，它们调用对比系统自己的服务或代码，输出 QC 可导入的图谱 JSON。整个过程不依赖 LitKG 或 QC 服务。生成后按 QC 子系统的方法评审：在 QC“导入图谱 → 通用 JSON”中上传，再运行“原文 LLM 评审”。

两个脚本都在后台运行，前台只显示进度，按 Ctrl-C 只会停止观看，不会停止任务。中断后重新执行同一条命令即可断点续跑。LLM Key 从 `$LLM_API_KEY` 读取，没有设置时会提示输入（不回显），保存在工作目录的 `secrets/llm_api_key`（权限 600），不会写入日志或输出。

## PubTator3

```bash
scripts/baseline_pubtator3.sh --pdfs <PDF目录> --llm-base-url http://<host>:<port>/v1 --llm-model <模型>
```

- 用 pypdf 逐页提取文本，清理规则与 LitKG 的 `fallback_pdf_text` 一致；安装了 fontTools，以便正确解码期刊 PDF 中常见的 CFF 字体。pypdf 防解压炸弹的资源上限放宽为默认值的 8 倍；个别无法解码的页会被跳过并计数（`pages_skipped`），不再导致整篇 PDF 被丢弃。修改提取逻辑后重跑，会自动重新提取语料。
- 实体：调用 PubTator3 自由文本 NER（官方接口，限速 3 次/秒）。
- 关系有两种来源：
  - **native**：从 PDF 中提取 DOI，经 NCBI E-utilities 解析为 PMID，再取 PubTator3 预计算的文档级关系。没有 DOI/PMID 的 PDF 只有实体，没有关系。
  - **llm**：把 PubTator 实体和 LitKG 谓词清单交给你提供的 LLM 抽取关系，并校验实体编号、谓词、domain/range 和逐字证据。
- 输出：`data/baselines/pubtator3/out/graph.pubtator3-native.json` 和 `graph.pubtator3-llm.json`。

## Neo4j LLM Graph Builder

需要 Docker Compose。请以普通用户身份运行，**不要**在命令前加 `sudo`。如果当前用户没有 Docker 权限，脚本会自己用 `sudo docker`，并在前台提示输入密码。用 `sudo` 运行整个脚本会改用系统 Python（通常缺少 venv 支持），工作目录也会变成 root 所有。

```bash
scripts/baseline_neo4j_builder.sh --pdfs <PDF目录> --llm-base-url http://<host>:<port>/v1 --llm-model <模型>
```

- 克隆固定版本 `neo4j-labs/llm-graph-builder` v0.8.7（commit `1ade47de…3142`），用 `deploy/docker-compose.builder.yml` 启动它的后端和一个专用 Neo4j 5.26 + APOC。端口只绑定 127.0.0.1：Neo4j 为 17687，后端为 18000。
- 把每个 PDF 原样上传给构建器，由构建器用自己的 PDF 解析和 LLM 抽取。schema 为 LitKG 的实体类型和谓词。每个 PDF 处理完都会存检查点，日志逐篇显示进度（`[12/79] xxx.pdf: done in 95s …`）。
- 构建器会处理每篇 PDF 的全文。上游默认只处理非 neo4j.com 用户每篇文档的前 10,000 token，脚本通过 `MAX_TOKEN_CHUNK_SIZE` 取消了这个限制，并写入图谱的 `metadata.consistency`。
- **启动前自检**：后台任务开始前，脚本先在构建器容器里依次检查分词器（tiktoken）、嵌入模型、Neo4j、一次普通 LLM 请求，再用 LitKG schema 对一句测试文本做一次抽取，走的是构建器自己的代码路径。构建器的 API 出错时只返回笼统的 “Failed To Process File … or LLM Unable To Parse Content”，自检则会打印真实原因和处理建议（Key 会被打码）。任何一项失败，后台任务都不会启动。
- **不需要 Key 的 LLM 服务**：提示输入 Key 时直接回车即可。构建器使用的 OpenAI 客户端（openai 2.x）不接受空 Key，会在发请求前就报 “Missing credentials”，所以脚本此时传给构建器占位 Key `EMPTY`；没有开启鉴权的服务（如未设 `--api-key` 的 vLLM/SGLang）会忽略它。
- **运行时不联网**：构建器切块用 tiktoken，它第一次使用时会从 openaipublic.blob.core.windows.net 下载词表；嵌入模型也来自 Hugging Face。脚本在构建镜像时就把 tiktoken 词表缓存进镜像（会走 `--proxy`），运行时设置 `HF_HUB_OFFLINE=1`，抽取过程不再需要外网。
- **单篇失败不影响整体**：单篇 PDF 抽取失败会重试一次；仍失败就跳过，记入 `out/failed.neo4j-builder-pdfnative.json` 和图谱的 `metadata.failed_documents`，其余 PDF 照常处理。此时图谱视为不完整，重跑同一条命令只会重试失败的那几篇。连续 3 篇失败说明是构建器、Neo4j 或 LLM 的问题而不是 PDF 本身，任务会停下。查看构建器记录的真实原因：`scripts/baseline_neo4j_builder.sh docker-logs | grep -A40 'File Failed in extraction'`。
- 输出：`data/baselines/neo4j-builder/out/graph.neo4j-builder-pdfnative.json`。
- 用完后执行 `scripts/baseline_neo4j_builder.sh down` 关闭容器。
- `scripts/baseline_neo4j_builder.sh docker-logs` 查看 Neo4j 和构建器容器的日志（会自动带上所需的环境变量）。
- **磁盘空间**：Docker 数据目录（通常是 `/var/lib/docker`，常在根分区）要放构建器镜像（数 GB）和 Neo4j 数据。启动前脚本会检查这里的剩余空间，少于 `KGB_MIN_FREE_GB`（默认 5 GB）就停止。磁盘写满时 Neo4j 会进入“需要重启”的故障状态，之后拒绝所有事务；脚本发现已在运行的 Neo4j 连不上时会先重启它，日志中出现 `No space left on device` 会立即报错退出，而不是空等 20 分钟。Neo4j 只保留最新一个事务日志文件，以减少占用。
- **schema 模式**：如果 LitKG 本体按 domain/range 展开后的三元组不超过 `max_schema_triples`（默认 1000），就把全部三元组交给构建器强制约束。LitKG 本体实际会展开成 12,476 个三元组，而构建器（LangChain `LLMGraphTransformer`）会把三元组清单原样放进每次请求的工具 schema，约 15 万 token。此时自动改用 `nodes+instructions` 模式：实体类型仍通过 `allowedNodes` 强制约束；谓词名、定义和 domain/range 通过构建器自带的 `additional_instructions` 字段传入，约 1.1K token，只作指导不强制。违反 domain/range 的关系照常保留，并在 `stats.md` 中计数（`domain_range_violations`）。这一差异会写入图谱的 `metadata.consistency`。

**无法访问 Hugging Face 时**：构建器的上游 Dockerfile 会在构建镜像时下载嵌入模型 `sentence-transformers/all-MiniLM-L6-v2`（约 90 MB）。这一步不能跳过，因为抽取接口 `/extract` 每次都会加载这个模型（`create_chunk_vector_index` 和 `create_chunk_embeddings` 无条件调用 `load_embedding_model`）。它只用于生成 chunk 向量，不影响抽取出的实体和关系。以下三种方式任选其一，选项会保存在工作目录中，之后重跑不必再写：

```bash
scripts/baseline_neo4j_builder.sh --pdfs … --llm-base-url … --llm-model … --proxy http://<代理主机>:<端口>
```

```bash
scripts/baseline_neo4j_builder.sh --pdfs … --llm-base-url … --llm-model … --hf-endpoint https://<HF镜像地址>
```

```bash
scripts/baseline_neo4j_builder.sh --pdfs … --llm-base-url … --llm-model … --embedding-model-dir /path/to/all-MiniLM-L6-v2
```

- `--proxy` 只用于克隆构建器仓库和构建镜像；LLM 调用和运行期的容器都不走代理。代理地址保存在 `build_options.env`（权限 600），不会打印。
- `--embedding-model-dir` 指向在能联网的机器上下载好的模型目录，需要包含 `config.json` 和 `*.safetensors`（或 `pytorch_model.bin`）。例如在有网的机器上执行 `huggingface-cli download sentence-transformers/all-MiniLM-L6-v2 --local-dir all-MiniLM-L6-v2`，再拷贝到服务器。
- 构建器使用的 Dockerfile 由脚本根据固定版本的上游 Dockerfile 生成（`Dockerfile.kgb`），只改了模型下载那一步。NLTK 数据下载改为失败也不中断构建，因为它只被聊天评估和 GCS 数据源使用。构建器的抽取代码没有任何修改。
- 嵌入模型很小（2200 万参数）。容器内本来就没有 GPU，它始终在 CPU 上运行，每个 chunk 只需毫秒级时间，相比 LLM 调用可以忽略。


## 通用命令与选项

Python：脚本会依次尝试 `$PYTHON`、`python3`、`python3.13`/`3.12`/`3.11`、`python`，使用第一个满足“≥ 3.11 且带 venv/ensurepip”的解释器。如需指定，例如用 conda 环境里的 Python：`PYTHON=$(which python) scripts/baseline_….sh …`。损坏的虚拟环境（没有 pip，或其实不是虚拟环境）会被自动重建。

两个脚本都支持：
- `status`：查看是否在运行、最近的日志和已生成的图谱。
- `logs`：持续查看日志。
- `stop`：停止后台任务。
- `--detach`：启动后立即返回，不在前台观看进度。
- `--force`：用缓存或检查点重建输出。
- `--name <语料名>`：图谱名称中使用的语料名，默认为 PDF 目录名。

用 `KGB_WORK=<目录>` 可以把工作目录放到别处，例如同时处理多批 PDF。

每次运行还会产出：
- `out/stats.md`：节点数、claim 数、锚定率、未映射谓词、LLM 用量、失败计数，以及随机 20 条 claim。
- `out/run_manifest.json`：版本、配置（不含凭据）、提示词和映射的 hash。

## 与 LitKG 的可比性

- **本体一致**：`policy` 直接取自 LitKG 的实体类型和谓词注册表，与 LitKG 快照导入 QC 后的 `policy` 相同，QC 比较页会显示“范围/本体一致：是”。
- **语料不一致**：文献 `id` 为 PDF 的 sha256，与 LitKG 的 `source_sha256` 相同，但文本块来自各基线自己的 PDF 解析，不是 LitKG 的 chunk。因此 QC 比较页会显示“语料不一致”。逐图谱的原文评审不受影响。
- **两个基线之间语料一致**：两个脚本从同一批 PDF 生成完全相同的 `documents`，彼此的 `corpus_hash` 一致。
- 其他差异会写入每个图谱的 `metadata.consistency` 和 `stats.md`。例如：
  - PubTator 的关系模型 BioREx 在 BioRED 上训练过。
  - 构建器的切块和提示词与 LitKG 不同，`temperature` 固定为 0。

接口实测细节见 `docs/pubtator3_api_notes.md` 和 `docs/neo4j_schema_notes.md`。

## 开发与测试

```bash
cd baselines && PYTHONPATH=. KGB_QC_PATH=.. python -m unittest discover -s tests -v
```

也可以直接使用底层 CLI：`PYTHONPATH=baselines python -m kgb.cli --config <config.toml> corpus|prep|run|validate|stats`。
