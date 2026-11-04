# 抽取时的 Web Research 与 MCP

PDF 解析仍由 Celery parse worker 调用 GROBID；失败时回退 pypdf。模型只收到解析后的文本。
本功能在实体目录、关系抽取及其纠错请求中增加可选的 MCP 工具循环：

`LLM → search_web → SearXNG → read_web_page → LLM → 原有 JSON → 原文校验`

模型按需搜索，默认最多 3 轮、6 次工具调用。搜索后须读取原网页才可使用其信息。
查询只发送实体名称或短问题，不上传整篇 PDF 或整个段落。网页文本是不可信数据。
网页读取限制为公开 HTTP/HTTPS、标准端口；固定已检查的 DNS 地址并逐跳检查重定向，
阻断内网、云元数据、凭据 URL、压缩/过大响应和非文本内容。响应进入模型前过滤认证材料。

## 证据与实体 ID

- 网页用于术语理解、消歧、标准 ID 候选，不能补造 PDF 中缺少的关系、条件、别名或引用。
- 原有 `normalized_id` 规则不变：该字段只能来自 PDF 片段。
- `propose_entity_link` 接收原文 mention、标准 ID、已读页面 URL 和逐字引用。
  客户端验证 mention 属于当前 PDF 输入、引用属于本轮读取的页面；结果单独保存为
  `external_identifier_candidate`，身份匹配仍为 `unreviewed`，不会自动合并或写入图谱。
- URL、读取时间、页面正文/截断状态、工具错误和 ID 候选保存在现有 `processing_cache`
  与 `processing_attempts` 中，无 SQL 迁移。成功缓存复用时一并恢复记录。
- `GET /web-research` 查看开关与预算；`GET /documents/{id}/web-research?limit=20`
  查看最近的搜索记录。原有未启用模式继续复用原缓存；启用后的配置签名隔离缓存。
  更新搜索引擎配置或刷新网页结果时提高 `WEB_RESEARCH_REVISION` 后重新抽取。

## 配套自建部署

需要拥有本项目 Docker 操作权限的账号。准备脚本只查询官方 Registry 元数据，生成
固定 digest 的镜像引用及本机随机密钥，写入权限 0600 的 `.web-research.env`，不显示值。
现有配置存在时保留，不覆盖。

```bash
cd /home/zhanghaiyang/litkg
python3 scripts/prepare_web_research.py
docker compose --env-file .env --env-file .web-research.env \
  -f docker-compose.yml -f docker-compose.web-research.yml config --quiet
docker compose --env-file .env --env-file .web-research.env \
  -f docker-compose.yml -f docker-compose.web-research.yml up -d --build searxng research_mcp
```

先验证搜索与原网页可达，再在现有批次空闲后更新 API 和 extraction worker：

```bash
docker compose --env-file .env --env-file .web-research.env \
  -f docker-compose.yml -f docker-compose.web-research.yml up -d --build api worker_extract
```

两个新增服务均不映射宿主端口。MCP 仅在容器网络中提供
`http://research_mcp:8000/mcp`；SearXNG 仅由 MCP 服务访问。
SearXNG 沿用默认搜索引擎，JSON 格式已启用，实际可用引擎取决于服务器网络。
若启用后 MCP 初始化失败，抽取明确失败以便重试；单次搜索/读页失败作为工具错误返回，
模型仍可使用 PDF 证据完成抽取。模型后端可返回原生 OpenAI `tool_calls`。当前配置模型的
合成探针在强制调用时返回完整 JSON 数组 `[{"name":"search_web","parameters":{"query":"..."}}]`，
在自动调用时返回 Qwen3-Coder 的完整 `<tool_call><function=...><parameter=...>` 块。
客户端严格支持这两种已观察到的格式；只解析模型完整消息，不从说明文字、网页或 PDF 中提取并执行工具命令。

禁用与回滚：用基础 compose 文件重新创建 `api worker_extract`，或设置
`WEB_RESEARCH_ENABLED=false`；保留搜索审计记录。无需删除数据库或重新解析 PDF。
项目原始代码备份由本次安装流程保存在项目 `tmp/web-research-backup-*` 中。

## 作为通用 MCP 服务

安装项目依赖（固定 `mcp==1.30.0`），设置 `SEARXNG_URL`，运行：

```bash
PYTHONPATH=app SEARXNG_URL=http://127.0.0.1:8888 python -m app.research_mcp --stdio
```

也可运行默认的 Streamable HTTP：默认只监听 `127.0.0.1:8000`。
提供 `search_web`、`read_web_page`、`propose_entity_link` 三个工具。
ID 候选的 PDF 及已读页面交叉验证在 LitKG 客户端执行；通用客户端必须自行验证，
服务器返回的 proposal 本身不是身份已确认或已应用到图谱的证明。

## 验证

```bash
PYTHONPATH=app:tests python -m unittest test_web_research -q
```

单元测试使用合成 PDF 文本、受控 LLM/MCP 响应和网页替身，不访问真实文献或搜索引擎。
MCP 握手和真实搜索可用性需另行验证；不能把离线测试通过等同于线上抽取质量提升。

实现参考：[MCP Python SDK v1.30.0](https://github.com/modelcontextprotocol/python-sdk/tree/v1.30.0)、
[SearXNG Search API](https://docs.searxng.org/dev/search_api.html)。
