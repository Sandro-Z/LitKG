# 生产部署：金标准评测、纯文本导入与外部基线

适用分支：`feat/gold-benchmark-eval`。所有数据库改动都是**新增列和新增表**，不修改、不删除已有数据。旧代码在新表结构上仍然可以运行，因此回滚只需要换回旧镜像。

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

在开发机推送分支（这一步尚未执行）：

```bash
git push origin feat/gold-benchmark-eval
```

在生产机：

```bash
git fetch origin && git checkout feat/gold-benchmark-eval && git pull --ff-only
```

如果生产环境固定跟踪 `FMT_use`，先在开发机把分支合并进 `FMT_use`（或通过 PR 合并），然后在生产机执行 `git pull --ff-only`。

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

应输出 `['litkg', 'neo4j-builder', 'pubtator3']`。在浏览器中打开 QC，侧栏应出现“金标准评测”。

QC 通过 `QC_LITKG_UPLOAD_DIR`（默认 `./data/uploads`）只读挂载 LitKG 的上传目录，以便识别纯文本原文；通过 `QC_GOLD_HOST_DIR`（默认 `./data/benchmarks`）只读挂载金标准目录，网页上直接列出服务器上的 `gold.*.json` 供导入。如果生产环境改过这两个目录，这两个变量也要同步修改。

## 5. 准备金标准数据（需要外网）

```bash
python3 benchmarks/prepare.py --all --download
```

数据写入 `data/benchmarks/`（不进 git）。如果生产机不能访问外网，就在别的机器上生成，然后把整个 `data/benchmarks/` 拷贝过来。之后的完整评测流程见 `docs/benchmark-eval.md` 的第 2–6 节：建知识库 → 导入文本 → 等待抽取 → 导入 QC → 运行金标准评测。

## 6. 外部基线（可选）

需要 Docker Compose、Python ≥ 3.11，以及能访问 GitHub、Docker Hub、Hugging Face 和 NCBI 的外网。在 LitKG 仓库根目录执行：

```bash
scripts/run_baselines.sh start --qc-graph <QC图谱ID> --llm-base-url http://<host>:<port>/v1 --llm-model <与LitKG相同的模型>
```

脚本会提示输入 Key（也可以先设置 `LLM_API_KEY`），然后在后台运行。用 `scripts/run_baselines.sh status` 查看进度。中断后再次执行 `start` 即可续跑。结束后用 `scripts/run_baselines.sh down` 释放 Neo4j 和构建器容器。

资源提示：构建器镜像首次构建需要下载数 GB，内存建议预留 6 GB 以上。它使用独立的 Docker Compose 项目 `litkg-baselines`，端口只绑定 127.0.0.1（17687、18000），不会影响 LitKG 和 QC。

## 7. 回滚

```bash
git checkout <部署前的提交> && docker compose up -d --build --force-recreate --no-deps --timeout 330 api worker_parse worker_extract frontend && docker compose -f docker-compose.qc.yml up -d --build
```

- 新增的数据库列都可以为空，旧代码会忽略它们，不需要回滚迁移。
- 回滚后，已导入的纯文本文档无法再重新解析，因为旧代码只认 PDF；已有的 chunk 和 claim 不受影响。
- QC 的两张新表会被旧版本忽略。
- 如需彻底恢复，可用第 0 步的备份，用 `pg_restore` 和 tar 还原。
