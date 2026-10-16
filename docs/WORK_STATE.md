# LitKG v3 开发与续接记录

更新时间：2026-10-16 UTC。任务要求：直接改当前仓库，最小完整实现；不正式部署。
本次没有读取或修改正式数据库，没有运行仓库 docker compose，没有调用真实模型。
用户提供了个人服务器 AGENTS 指令；仓库及其父目录没有找到适用的 AGENTS.md 文件。

已完成：全文目录阶段与版本门控、可恢复分段缓存、显式别名与歧义处理；共享扩展类型；
实体身份/原始提及/过程字段及完整事实参与者；PostgreSQL 双路检索和可选嵌入、
证据引用与问答；抽取式摘要、复核、旧文献维护入口；原始输出/历史快照；
幂等 SQL 升级、API 和 Streamlit 入口。保留原二元图及查询。

实现文件：catalog.py、entity_types.py、facts.py、retrieval.py、evaluation.py；
现有 tasks/graph_store/graph_query/schemas/main/parser/page_repair/frontend 扩展。
迁移仍是 db/init.sql；已有库需应用迁移，旧记录通过 maintenance 的 reproject
或 reextract 升级。两者的差异和命令见 README v3 节。

最终验证：97 项测试全部通过，耗时 10.622 秒（包含 PostgreSQL、受控模型和 Streamlit AppTest）。
compileall 和 git diff --check 均通过。覆盖全文门控、缓存续接、显式别名归并、缩写歧义、
类型、过程/条件、多个参与者角色、否定/推测、完整证据、版本隔离、并发幂等、迁移和旧记录升级。
验证只使用受控模型、合成文献和独立测试数据库。没有真实 Other 分布、真实抽取
效果对照、真实 embedding 模型测量或真实答案语义准确率结果，不能宣称性能提升。

临时环境（最终已清理数据库容器）：
- Python 虚拟环境：/tmp/litkg-test-venv（app、httpx、frontend 依赖）
- 仅本机监听的 PostgreSQL：容器 litkg-verification-20261016，127.0.0.1:55439，
  数据库/用户 litkg_test；使用公开的一次性测试值，未使用正式凭据。
  该镜像自动创建一个匿名 PostgreSQL 数据卷；核对其只由本次测试容器使用后，
  已按完整 ID 移除容器及该卷，并验证二者均不存在。未挂载正式目录或已有卷。
- 测试在随机 litkg_test_*、litkg_resolution_*、litkg_knowledge_* schema 中运行，
  tearDown 会删除；正式数据库、卷和服务不受影响。
- 所有 Celery 发布在流程测试中被捕获验证，任务函数用 .run 调用；没有启动正式 worker。

交付状态：实现和本地验证完成，当前修改保留在工作区，未提交、未推送。
没有剩余的实现阻塞项。真实模型抽取/答案语义、真实 embedding 服务、真实 PDF 小规模对照、
真实 Celery broker/worker 通信尚未实测。流程测试捕获发布调用并执行真实任务函数，
不能将此称为正式多服务部署验证。

需要在新会话继续时：
1. 阅读此记录和 README v3 节，检查 git diff，保留工作区修改。
2. 临时数据库已移除；重测时新建独立 pgvector PostgreSQL，不连接正式库。
3. 使用 /tmp/litkg-test-venv/bin/python；设置 LITKG_TEST_DATABASE_URL 后运行
   PYTHONPATH=app /tmp/litkg-test-venv/bin/python -m unittest discover -s tests -v。
   未设置测试 DSN 时数据库测试跳过，不能作为完整集成验证。
4. 有真实文献和模型后，用 app.evaluation 保存升级前后快照及人工问题标签。
   无标签的证据召回仍必须为 null；有效引用不等于答案语义正确。
5. 正式迁移/部署未执行；只能按用户后续范围操作。

参考实现仅通过公开只读 HTTP 检查：llm_wiki ingest-cache.ts、ingest.ts 和 review-utils.ts；
HyperGraphRAG operate.py 与 prompt.py。没有下载执行外部代码或整体移植。
