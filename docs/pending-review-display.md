# 待审核知识的展示

图谱浏览、关系查询和问答检索默认包含当前版本中已存储的待审核知识。审核状态保留，图谱边会显示“待审核”。

严格查询：图谱/关系/实体接口传 `include_pending=false`；问答请求 JSON 传 `"include_pending": false`。前端可以取消“包含待审核”复选框。

问答返回的 facts 和 citations 保留 semantic_status、review_status、audit_pending、review_reasons、source_kind。包含待审项时 warnings 提供说明。

fmt 接入默认允许展示待审项；项目配置 `WGS_KNOWLEDGE_USAGE_INCLUDE_PENDING=false` 可切回严格过滤。报告子图优先放置正文引用，再在原有 6 条边/12 个节点上限内补充相关知识；待审标记保留。

部署（在具备 Docker 权限的 gpu02 终端执行）：

```bash
sudo docker compose --project-directory /home/zhanghaiyang/litkg -f /home/zhanghaiyang/litkg/docker-compose.yml up -d --build --no-deps api frontend
```

修改不会自动覆盖已有报告；新报告或重新生成的子图使用新规则。
