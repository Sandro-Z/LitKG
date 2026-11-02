# Catalog 输出截断与恢复

`310 input characters` 是当前正文片段的字符数，不是模型上下文token数，也不包括目录提示词、类型定义、文献标题和章节。
`max_model_len=262144` 是服务报告的上下文容量，不会把客户端的4096 token输出请求自动扩大。

## 有界容量重试

普通目录请求使用 `CATALOG_MAX_OUTPUT_TOKENS`（默认继承 `LLM_MAX_OUTPUT_TOKENS`，后者默认4096）。
收到 `finish_reason=length` 后先按原策略拆分输入，保留原文字符覆盖、重叠和绝对偏移。
无法继续拆分时，按4096→8192→16384这样的序列重试；每次从原文重新生成完整目录，不把截断JSON送去语义纠错。

上限 `CATALOG_OUTPUT_RETRY_MAX_TOKENS` 默认 `max(CATALOG_MAX_OUTPUT_TOKENS, 16384)`。
必须大于等于起始预算；与起始预算相等可关闭预算升级。自定义上限不要求是2的幂，最后一次精确使用该上限。
较长、仍可拆分的片段不会先尝试大预算；正常成功请求不会新增模型调用。
预算包含推理和最终正文的生成消耗，具体以服务返回的usage为准；上限必须在服务接受范围内。
服务拒绝请求、连接错误等不会被当作截断继续升级预算。

例如，在本机编辑配置文件时仅添加或修改以下非认证参数，不必输出整个配置文件：

```dotenv
CATALOG_MAX_OUTPUT_TOKENS=4096
CATALOG_OUTPUT_RETRY_MAX_TOKENS=16384
```

已验证成功的目录片段和已保存的拆分树使用兼容的缓存键；仅重试失败文献即可续接。
达到上限仍截断时仍明确失败，错误包括实际尝试预算，以及最后响应的 `content_chars`、`reasoning_chars`，
服务提供时另有 `completion_tokens` 和 `reasoning_tokens`。未提供的token统计不伪造为0。
推理字符数为0不代表服务没有内部推理；parser未分离推理时，正文中也可能包含推理标签。
该改动不改变目录提示词、实体类型、原文校验、P0/P1规则或数据库结构。

## Qwen3.6 与 SGLang

Qwen3.6-35B-A3B-FP8支持通过请求 `chat_template_kwargs.enable_thinking=false` 关闭thinking。
LitKG已经在请求中发送此参数；仅增加 `--reasoning-parser qwen3` 用于分离推理和正文，不会关闭thinking。
Qwen官方模型卡推荐SGLang至少0.5.10，实际参数以部署版本的 `python -m sglang.launch_server --help` 为准。

在已有启动命令中保留模型路径、GPU并行度、地址、认证和其他参数，只按需调整这些项：

```bash
--context-length 262144 \
--reasoning-parser qwen3 \
--default-chat-template-kwargs '{"enable_thinking":false}'
```

`--default-chat-template-kwargs` 需要当前安装版本支持。它设置请求未指定时的默认值；当前LitKG请求已经显式禁用thinking。
已有262144上下文时无需为本错误继续加大该值。输入提示加输出预算需放得进模型实际接受的上下文。
`--max-total-tokens` 是内存池token容量，`--max-running-requests` 是并发数，都不是单次回答的输出上限。
不要把调整这两个参数当作提高 `max_tokens` 的替代方案。

参考：[Qwen官方FP8模型卡](https://huggingface.co/Qwen/Qwen3.6-35B-A3B-FP8)、
[SGLang Qwen3.6部署说明](https://github.com/sgl-project/sgl-cookbook/blob/main/docs/autoregressive/Qwen/Qwen3.6.md)、
[SGLang服务参数](https://docs.sglang.io/docs/advanced_features/server_arguments)。

## 部署与失败续接

代码改动需要重建并重建容器才能生效。以下命令在gpu02的 `~/litkg` 执行；需要提权时为docker命令加sudo：

```bash
cd ~/litkg
git branch --show-current  # 应为FMT_use
docker compose build api worker_parse worker_extract
docker compose stop -t 600 worker_parse worker_extract
docker compose up -d --no-deps --force-recreate api worker_parse worker_extract
```

先等待当前批次任务尽量完成再停worker；实际优雅停止等待上限应覆盖任务耗时。
不用重建frontend，不用数据库迁移，不用清空缓存，不用重新上传PDF。
生效后在原批次使用失败重试/恢复入口，只续接失败文献，保留成功的目录片段和已完成文献。
先选一篇原来失败的文献验证；若仍失败，读取新的计数和实际尝试预算检查模型服务，避免直接反复重跑整批。

本次开发验证使用受控响应和隔离PostgreSQL；未重启生产容器、调用在线模型或修改生产数据库。
