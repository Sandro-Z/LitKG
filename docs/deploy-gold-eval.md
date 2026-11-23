# 生产部署：金标准评测、纯文本导入与外部基线

适用分支：`FMT_use`。所有数据库改动都是**新增列和新增表**，不修改、不删除已有数据。旧代码在新表结构上仍然可以运行，因此回滚只需要换回旧镜像。

- 生产环境已经部署过上一版（有“金标准评测”页）：只需按 **A 节** 升级 QC。
- 首次部署：按第 0–7 节。

## A. 仅升级 QC：金标准评测并入评审

这一版只改 QC 服务（`quality_control/`）：金标准评测不再是单独的页面和按钮，每次评审自动用服务器上的全部金标准评测对齐的文献，合并的 precision / recall 计入正确性和完整性。没有数据库迁移，LitKG 主服务（API、worker、前端、PostgreSQL）不需要重启。

**A1. 确认没有正在运行的 QC 评审。** 重建容器会把运行中的评审标为 `interrupted`，需要重新发起（已付费的请求不会自动重发）。下面的命令应输出 `[]`：

```bash
curl -s 'http://127.0.0.1:8090/api/graphs?view=overview' | python3 -c 'import json,sys; print([g["name"] for g in json.load(sys.stdin)["graphs"] if (g.get("latest_run") or {}).get("status") in ("queued","running")])'
```

**A2. 记下当前版本并备份 QC 数据卷**（数据卷名一般是 `litkg-qc_qc_data`，可用 `sudo docker volume ls | grep qc_data` 确认）：

```bash
cd /home/zhanghaiyang/litkg && git rev-parse --short HEAD
```

```bash
sudo docker run --rm -v litkg-qc_qc_data:/data -v "$PWD":/backup alpine tar czf /backup/backup-qc-$(date +%F).tgz -C /data .
```

**A3. 更新代码：**

```bash
git pull --ff-only
```

**A4. 确认金标准文件在服务器上**（QC 只读挂载 `QC_GOLD_HOST_DIR`，默认 `./data/benchmarks`）：

```bash
ls -l data/benchmarks/gold.*.json
```

没有文件时生成（需要外网；不能上外网就在别的机器生成后拷贝整个 `data/benchmarks/`）：

```bash
python3 benchmarks/prepare.py --all --download
```

**A5. 只重建并重启 QC 服务。** 必须使用与当前部署相同的环境变量来源（同一个 `--env-file`，或同样的 `.env` / 命令行变量），否则 `QC_LLM_BASE_URL`、`QC_LLM_MODEL` 等配置会在重建时丢失：

```bash
sudo docker compose -f docker-compose.qc.yml up -d --build quality
```

如果生产环境一直用两个 Compose 文件一起启动 QC（`CONCURRENCY.md` 中的写法），用等价命令：

```bash
sudo docker compose -f docker-compose.yml -f docker-compose.qc.yml up -d --build --no-deps quality
```

金标准目录不是 `./data/benchmarks` 时，在同一条命令前加 `QC_GOLD_HOST_DIR=<目录>`（`sudo` 默认丢弃环境变量，所以写成 `sudo QC_GOLD_HOST_DIR=<目录> docker compose ...`）。

**A6. 验证：**

```bash
curl -fsS http://127.0.0.1:8090/api/config | python3 -c 'import json,sys; c=json.load(sys.stdin); print(c["gold_version"], "| LLM:", c["llm_configured"], c["model"], "| gold files:", c["gold_files"], "| gold dir:", c["gold_directory_configured"])'
```

应输出 `qc-gold/1.1.0`，LLM 配置与升级前相同，`gold files` 不少于 1，`gold dir` 为 `True`。在浏览器中打开 QC：侧栏不再有“金标准评测”；“自动评审”页多了“金标准评测”卡片，“图谱比较”页多了“金标准 P / R / F1”列。

**A7. 让已有图谱得到金标准结果。** 旧报告没有金标准部分。在“自动评审”页选择图谱，点击“仅运行本地约束检测”即可补上：不调用模型，会复用最近一次 LLM 评审的证据做校准。之后每次“运行原文 LLM 评审”都会自动包含金标准评测。升级后的第一次评审会导入服务器上的金标准文件（train 划分需要几秒），之后按文件大小和修改时间跳过已导入的文件。

只有图谱文献与金标准对齐（`external_id` 如 `biored:15485686`，或标题、文献 ID 相同）时，金标准才计入评分；其他图谱的评分与升级前完全相同。所以要得到金标准评分，图谱必须建在 `data/benchmarks/documents.*.json` 的文献上：LitKG 用第 2–3 节的纯文本导入，基线用 `baselines/` 的脚本。

**A8. 回滚：**

```bash
git checkout <A2 记下的提交> && sudo docker compose -f docker-compose.qc.yml up -d --build quality
```

没有表结构变化：旧版本忽略报告中的 `gold` 字段，新版本写入 `gold_evals` 的结果仍会出现在旧版的“金标准评测”页。回滚后用 `git checkout FMT_use` 回到分支。

## 0. 部署前

```bash
cd /path/to/LitKG && git status --short
```

工作区应当干净。先备份 PostgreSQL 和 QC 数据卷：

```bash
docker compose exec -T postgres sh -lc 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > backup-litkg-$(date +%F).dump
```

```bash
docker run --rm -v litkg-qc_qc_data:/data -v "$PWD":/backup alpine tar czf /backup/backup-qc-$(date +%F).tgz -C /data .
```

如果还有正在处理的批次，等它们结束，或在低峰期部署。worker 重建时最多等待 330 秒。

## 1. 获取代码

在生产机（跟踪 `FMT_use`）：

```bash
git checkout FMT_use && git pull --ff-only
```

## 2. 数据库迁移（幂等，可重复执行）

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 --single-transaction' < db/migrations/20261121_text_documents.sql
```

确认三个新列已经存在：

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At' <<'SQL'
SELECT table_name || '.' || column_name FROM information_schema.columns
WHERE column_name IN ('external_id', 'source_block_id', 'source_offset') ORDER BY 1;
SQL
```

应输出 `documents.external_id`、`chunks.source_block_id`、`chunks.source_offset`。

## 3. 更新 LitKG 主服务

```bash
docker compose up -d --build --force-recreate --no-deps --timeout 330 api worker_parse worker_extract frontend
```

验证：

```bash
curl -s http://127.0.0.1:8080/health
```

```bash
curl -s http://127.0.0.1:8080/openapi.json | grep -o '/knowledge-bases/{knowledge_base_id}/text-documents'
```

新接口的上限可以在 `.env` 中调整（可选）：`MAX_TEXT_DOCUMENTS=200`、`MAX_TEXT_DOCUMENT_BYTES=2097152`、`MAX_TEXT_REQUEST_BYTES=67108864`。纯文本源文件与 PDF 存在同一目录（`./data/uploads`），文件名为 `{sha256}.litkg-text.json`。

## 4. 更新 QC 服务

QC 启动时会自动创建 `gold_sets` 和 `gold_evals` 两张表，不需要手工迁移：

```bash
docker compose -f docker-compose.qc.yml up -d --build
```

验证：

```bash
curl -s http://127.0.0.1:8090/health
```

```bash
curl -s http://127.0.0.1:8090/api/gold-profiles | python3 -c 'import json,sys; print([p["name"] for p in json.load(sys.stdin)["profiles"]])'
```

应输出 `['litkg', 'neo4j-builder', 'pubtator3']`。在浏览器中打开 QC，“自动评审”页应有“金标准评测”卡片；金标准评测随每次评审自动运行（见 A6、A7）。

QC 通过 `QC_LITKG_UPLOAD_DIR`（默认 `./data/uploads`）只读挂载 LitKG 的上传目录，以便识别纯文本原文；通过 `QC_GOLD_HOST_DIR`（默认 `./data/benchmarks`）只读挂载金标准目录，网页上直接列出服务器上的 `gold.*.json` 供导入。如果生产环境改过这两个目录，这两个变量也要同步修改。

## 5. 准备金标准数据（需要外网）

```bash
python3 benchmarks/prepare.py --all --download
```

数据写入 `data/benchmarks/`（不进 git）。如果生产机不能访问外网，就在别的机器上生成，然后把整个 `data/benchmarks/` 拷贝过来。之后的完整评测流程见 `docs/benchmark-eval.md` 的第 2–6 节：建知识库 → 导入文本 → 等待抽取 → 导入 QC → 运行评审（自动包含金标准评测）。

## 6. 外部基线（可选，独立工具）

与金标准评测和 QC 无关。输入是一批与 LitKG 相同的 PDF 和一个 LLM API，输出 QC 可导入的图谱。两个系统分开运行：

```bash
scripts/baseline_pubtator3.sh --pdfs <PDF目录> --llm-base-url http://<host>:<port>/v1 --llm-model <模型>
```

```bash
scripts/baseline_neo4j_builder.sh --pdfs <PDF目录> --llm-base-url http://<host>:<port>/v1 --llm-model <模型>
```

- 需要 Python ≥ 3.11 和外网：PubTator3 需要访问 NCBI；Neo4j 构建器需要访问 GitHub、Docker Hub 和 Hugging Face。
- 第二个脚本需要 Docker Compose。构建器首次构建要下载数 GB，Docker 数据目录所在分区至少留 5 GB 空闲（脚本会检查），内存建议预留 6 GB 以上。它使用独立的 Compose 项目 `litkg-baselines`，端口只绑定 127.0.0.1（17687、18000），不影响 LitKG 和 QC。
- 无法访问 Hugging Face 时，给 Neo4j 脚本加 `--proxy`、`--hf-endpoint` 或 `--embedding-model-dir` 三者之一（构建镜像时需要一个约 90 MB 的嵌入模型）。
- 详见 `baselines/README.md`。

## 7. 回滚

```bash
git checkout <部署前的提交> && docker compose up -d --build --force-recreate --no-deps --timeout 330 api worker_parse worker_extract frontend && docker compose -f docker-compose.qc.yml up -d --build
```

- 新增的数据库列都可以为空，旧代码会忽略它们，不需要回滚迁移。
- 回滚后，已导入的纯文本文档无法再重新解析，因为旧代码只认 PDF；已有的 chunk 和 claim 不受影响。
- QC 的两张新表会被旧版本忽略。
- 如需彻底恢复，可用第 0 步的备份，用 `pg_restore` 和 tar 还原。
