# PDF 图像知识抽取

原有 GROBID / pypdf 文本解析、文本模型、目录建立与关系抽取保持原有路线。可选 VLM 分支为 PDF 每页建立独立图像 chunk，直接将页面图像发送给视觉模型，最后复用 `prepare_claims`、实体目录归一化、`store_claim`、facts 和关系投影，合并到该文献所属的同一知识图谱。

## 启用与部署

默认 `VLM_ENABLED=false`，旧配置继续使用纯文本流程。将 `.env.vlm.example` 中的设置加入现有 `.env`，保留已有文本模型配置。视觉服务不需要 API key；本分支直接使用 HTTP 请求，不传认证头，也不继承系统代理或 `.netrc`。

```dotenv
VLM_ENABLED=true
VLM_BASE_URL=http://10.181.9.172:20019/v1
VLM_MODEL=Qwen3.8-Flash-Next-NVFP4
```

部署由管理员执行。新增依赖固定为 `PyMuPDF==1.26.7`；需要重建并更新 api、worker_parse、worker_extract 镜像。无需修改数据库结构、监听端口或文本模型参数。

```bash
docker compose build api worker_parse worker_extract
docker compose up -d --no-deps api worker_parse worker_extract
```

容器通过现有 `env_file: .env` 读取视觉设置。新上传文献自动启用；已完成的文献须重新解析 PDF，才能生成图像任务。仅重试原有文字 chunk 不会补出图像分支。重新解析沿用原有归档和处理代次机制。

## 行为和来源

- 默认 `VLM_PAGE_MODE=all`：逐页渲染，覆盖嵌入图片、矢量图表、扫描页和多面板。完整页面保留图注、坐标轴与图例；含图筛选模式 `figures` 可降低调用量，但采用启发式，可能多选或漏选页面。
- 每页独立排队，文本 chunk 与视觉 chunk 共用现有 extract 队列。VLM 失败只使该 chunk 失败；正文可继续写入，文献最终显示 `partial`。图像发现失败也保存独立失败 chunk，不能静默算作完整成功。
- 图像任务不进入文本目录模型；视觉结果通过现有文献实体目录解析。同名且身份明确的实体复用目录身份，新增或不明确实体沿用现有 `pending_merge` / review 机制，不强行合并。
- 视觉输出包含 `observations` 观察句数组和 `claims`。每条 Claim 用 `observation_index` 引用观察句，服务器绑定证据，再执行现有参与者、证据包含、谓词及条件校验。`observations` 是 **模型对图像的描述，不是 PDF 原文引句**；这些校验验证结构和描述内一致性，不能独立证明读图正确。图像元数据由服务器赋值，不进入原有文本模型的输出 schema。
- 实测模型会把“整体包含部件”反写为“整体 PART_OF 部件”。视觉分支对此类观察句与关系方向明确矛盾的候选增加了拦截，保留原候选进入 review，不自动反转关系。此规则并非通用的事实正确性证明。
- 图像证据有 `evidence.modality="pdf_image"`，及 `image_source`：原 PDF SHA-256、渲染 PNG SHA-256、PDF 实际页码、渲染版本、尺寸、页面边界与渲染参数。页码由服务器确定。`source_refs` 同步保留图像定位信息，描述中的字符偏移标记为 `basis="vlm_visual_description"`，与正文 `chunk_text` 区分。
- 文本和图片的相同关系由原有统一投影归并，分别保留来源 Claim，避免图像证据覆盖正文证据。图像读取结果保存在现有 `processing_cache` 的 `vlm` stage；缓存键包括文献目录版本、PDF 与页面定位、模型、端点、提示和渲染设置。重试已成功页面复用缓存。模型失败、JSON 错误、输出截断以及缺少视觉描述不会缓存为成功空结果。
- `VLM_RENDER_DPI` 默认 150，`VLM_MAX_IMAGE_EDGE` 默认 2400；会等比例限制图像尺寸。修改渲染参数后重新解析文献。`VLM_TIMEOUT_SECONDS` 默认 300，`VLM_MAX_OUTPUT_TOKENS` 默认 8192。提高输出长度后重试可重新请求，而不会复用旧预算缓存。

图像定位元数据存放在 chunk 现有 JSONB `page_spans` 的 `image_source` 扩展字段，不需要数据库迁移。推理时从原 PDF 重现渲染，并核对 PDF 校验和；不会将大型 base64 图像写入日志或数据库。关闭 `VLM_ENABLED` 可阻止新建图像分支及后续 VLM 调用，已保存的图像 Claim 保留。

页码修复保留图像 chunk 与 Claim 的渲染来源，不用 PDF 文本重新对齐视觉描述。原文本审查工具的 `reextract` 会跳过图像 chunk 并记录原因；图像重新抽取使用现有 chunk 重试入口，由任务分派至 VLM 分支。图像描述也不能通过“PDF 原文纠错”接口改写为正文。

当前视觉服务不支持 `response_format` 的约束解码。请求通过提示限定 JSON，服务端严格解析与校验，不发送 `response_format=json_object/json_schema`。

## 验证

使用已安装 `app/requirements.txt` 与 pytest 的 Python 环境：

```bash
PYTHONPATH=app:tests python -m pytest -q tests/test_vlm.py tests/test_parser.py tests/test_claim_source_refs.py
```

部署后用含图表的 PDF 建立新的测试文献，检查正文 chunk 与 `PDF image page N (VLM)` chunk 都有处理结果；检查图像 Claim 的 `extraction_model`、`evidence.modality`、实际页码及 `source_refs[].image_source`。图谱应同时含有正文事实和图像事实，正文与图像证据可分别回溯。视觉模型的读图准确性仍需对照原页面抽样复核，尤其是细小坐标、误差线和复杂分组。
