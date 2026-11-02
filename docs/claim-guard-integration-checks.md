# P0 数据库集成复核

V1/V2 首轮未配置可丢弃数据库，因此当时数据库用例跳过。后续在 /tmp 安装独立 PostgreSQL 16 与 pgvector，
创建权限0700的临时数据/Unix socket目录，禁用TCP监听，使用合成数据运行完整测试，结束后停止实例。
未读取项目 .env，未连接正式数据库，未调用真实模型。

数据库实测发现并修复：

- 新候选的非显著结果规范化成功后，不应残留旧的 V2 冲突原因；最终检查只将仍未解决的冲突入队。
- SQL 使用实际行 speculative 字段并兼容仅含 JSON 的旧查询，与 Python 读取顺序一致。
- 审计结论限定当前 catalog 版本；重跑时可将已修正 claim 的旧 active 结论置 false，保留原始 JSON、ID 和人工审核状态。
- 稳定键避免重复复核项；dismissed 本身不解除仍有效的冲突，修正后重新检查才解除。

真实数据库测试断言了：审计前后原始 claim_json 与 ID 一致、claim_history 存在、默认图谱排除冲突、
include_pending 可查看、回滚恢复图谱、重跑不重复入队、人工决定保留、修正后旧冲突解除。

P0 合并工作区最终实测关键输出：

```text
Ran 368 tests in 23.802s
OK (skipped=5)
```

5项跳过均为可选浏览器测试；全部数据库集成测试实际运行。Node 图谱测试9项通过。
完整日志位于 /tmp/litkg-claim-audit-integration.log；正式用户数据未执行 apply。

测试启动方式先 import app，再添加 frontend 路径，以适配工作区已有 export_jobs 模块，避免其 app.py 遮蔽后端命名空间。
