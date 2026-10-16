# LitKG v3 开发与续接记录

## 2026-10-16 目录候选隔离与维护续接修复（最新状态）

基线 HEAD：17a86d9（之前为 accdc9c、fbc0f67）。本次用户提供了生产诊断，未读取
正式库、未调用真实模型、未修改 .env、未执行部署脚本或更新正式容器。

已确认并修复的路径：
- 模型将原文 Beckman Coulter 写成 Beckman Coulson，名称不存在是实际幻觉，不能模糊接受。
  目录逐项保留有原文依据的实体，将无依据核心名称/标准 ID 的候选隔离为 catalog_rejection，
  保留完整候选、错误、chunk、偏移及精确输入。审计状态 partial，原始输出保留。
  全部非空候选都无效仍纠正后失败；JSON/调用/最小段截断失败不伪装为空成功。
- 隔离/字段修复元数据随 successful processing_cache 保存，后续分段失败后续接不丢失复核项。
  文档目录策略版本 catalog-v3-quarantine；模型输入契约与缓存键继续兼容 17a86d9 的
  catalog-v2-grounded-repair 缓存，本地重新校验后复用已完成分段及分割计划。
- catalog/processing API 新增 catalog_quality：排除候选数量、待复核数量、质量状态；
  前端显示警告。catalog_status=done 仅表示全文分段阶段已完成，不承诺没有被排除的候选。
- maintain_knowledge_base 同步循环原来首个 chunk 失败就跳出，后面的 queued 没有 Celery 任务，
  导致文献 extracting、共享批次 processing。现在逐 chunk 捕获并保留失败，继续其余 chunks，
  最终文献/批次转为 done/partial/failed；结果有逐 chunk 状态。busy 不假装完成，superseded
  停止旧代次。用日志形态（1 failed + 多 queued）和两个共享批次复现验证恢复。
  日志未给文献 25 首个失败 chunk 的具体错误，不能声称已经查明该模型失败的原始原因。
- README 提供用户上线和对 KB 14 文献 25–29 显式 retry 的命令；旧 --retry-failed 选项
  只覆盖 failed/partial，不能恢复 extracting 的文献 25。不要先 reextract 更换代次。
  部署脚本增加前端更新、容量测试，校验清单包含 main.py；本次没有运行部署脚本。

验证：132 项全部通过，12.721 秒，包含独立 PostgreSQL、模型替身和 Streamlit AppTest。
覆盖坏候选隔离/证据/跨知识库隔离、全无效响应失败、缓存中断续接/17a86d9 缓存兼容、
目录先于关系门控、首 chunk 失败后继续、共享批次终结、只重试失败 chunk、busy/过期代次。
compileall、bash -n、git diff --check、sha256sum -c scripts/catalog-runtime.sha256 均通过。

临时数据库：litkg-quarantine-check-20261016，完整容器 ID
73f3647125c6ff946fe1ecafbe545a80749fd744ecf119cf06d709cbf37b7e32，
仅监听 127.0.0.1:55443，数据 tmpfs，无生产数据挂载；已核对完整 ID、标签、
端口及 Mounts=[] 后移除，并通过 docker ps -a 按完整 ID 筛选确认不存在。
Python 测试环境仍为 /tmp/litkg-test-venv。

交付：无需 SQL 迁移。修改保留工作区，未提交或推送，未正式部署，旧任务也未排队。
真实 Qwen 输出质量、真实文献覆盖率与真实 Celery/broker 端到端尚未实测；测试只使用
用户给出的拼写场景/合成原文和模型替身。隔离不会补造真实实体，仍可能有目录漏项待人工复核。

## 2026-10-16 目录输出截断修复（历史记录）

工作区以 accdc9c（原文约束修复）、fbc0f67（全文目录/完整事实）为基线。
当前 .env 的 LLM_MAX_OUTPUT_TOKENS=4096，未显式设置目录分段大小，使用 12000 字符默认值。
只读取了容量相关配置，没有显示密钥；未改 .env，未操作正式服务或真实模型。

修复：
- finish_reason=length 单独抛出 ModelOutputTruncatedError，不能接受被截断的完整外观 JSON。
- 截断不再进入相同输入的语义纠正重试；对当前片段带重叠二分，保持全文覆盖和绝对位置。
- 分割计划使用 processing_cache 的 catalog_split 阶段，成功子段仍用 catalog 阶段缓存。
  retry 跳过已知超限父段和已成功子段；原有有效结果缓存键保持兼容。
- CATALOG_MAX_OUTPUT_TOKENS 单独控制目录输出，默认继承 LLM_MAX_OUTPUT_TOKENS；
  CATALOG_MIN_SEGMENT_CHARS 默认 500。达到阈值仍超限时明确失败并提示服务端容量检查。
- 保留原文校验、目录完成门控、原始输出/失败记录。无数据库迁移。
- README 已提供配置、容器重建和 retry 命令，scripts/catalog-runtime.sha256 已同步。

验证：独立临时 PostgreSQL + 受控模型，完整 123 项测试通过（9.655 秒）；
compileall、git diff --check、运行文件 sha256 校验通过。未测试真实 LLM 输出容量。
测试容器 litkg-capacity-check-20261016，端口仅本机 55441，数据使用 tmpfs；已核对完整 ID 后清理并验证不存在。
Python 环境继续使用 /tmp/litkg-test-venv。没有运行部署脚本、没有构建或重启正式容器。

待用户上线：README“目录输出截断修复”包含 .env 建议值、应用重建及失败文献 retry；
不能将本次临时验证描述成正式环境已恢复。工作区补丁未提交或推送。

## 初版实现历史记录

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
