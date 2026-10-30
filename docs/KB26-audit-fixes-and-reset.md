# KB 26 评审修复与保留 PDF 的重处理

本次修改针对可复现的生成和投影问题，不会自动改写生产库中的科学结论，也不会自动清空数据。

## 修改内容

- 结果状态只读取实际终点：AFFECTS/ACTIVATES/INHIBITS 的 object，DIFFERS_BETWEEN 的 subject。其他角色的 unchanged、关系逻辑否定、未测试、未检出不再自动等同于零效应。未测试/未检出另存 result_interpretation，并进入待复核。
- 类型明确的 product/substrate/mediator 等 qualifier 实体先提升为 participants，再执行原文与目录校验；允许多个同角色产物。只有名字、没有类型的产品不会被猜成化合物。
- wild-type/WT、AH18、strain 1、this site 等未解析局部标签按文献隔离；全称菌株仍可跨文献共享。全局名称匹配不会吸收文献局部节点。
- 旧 claim 重建投影时执行当前校验，并持久化结果状态、否定和处理版本。硬错误保留原端点和证据，标 pending；默认图谱不把它作为已通过检查的边。
- 没有实体目录的旧文献保留原身份信息，不会因缺目录把原有实体整体拆成文献局部节点。变更前的完整记录保存在 claim_history；实体和关系列表可显式使用 include_pending=true 检查待复核记录。
- 方法误作生物过程、倒置的结果/干预角色、错误的 expression 锚点、respectively 配对，以及未知来源/缺证据设计进入待复核。accepted 仅表示通过现有规则，不是语义蕴含证明或人工核准。
- 抽取提示增加事件角色、真实比较臂、方法、并列配对、局部身份、输入排版风险约束；处理/目录/抽取版本更新，避免继续命中旧缓存。
- 增加 reparse 维护模式：从已保存 PDF 重新解析，重建目录，再抽取。reextract 则继续复用已经保存的文本，适用于已人工校正源文本的文献。

μ 丢失、表格错列、相邻文章混入等仍须对照 PDF 确认具体位置；本次没有全局替换 mg/mM，也没有用正则猜补科学关系。已有 app.review_repair 支持带页码/区域和依据的局部源文本修正及回滚。

## 1. 部署源码修复

在 gpu02 执行（当前 SSH 用户没有 Docker socket 权限，需要有权限的终端执行 sudo）：

```bash
cd /home/zhanghaiyang/litkg
sudo docker compose up -d --build api worker_parse worker_extract frontend
```

镜像内包含源码，单纯修改宿主仓库或 restart 旧容器不会更新代码。本次不需要数据库结构迁移。

## 2. 清空知识库的全部活动 claim，保留 PDF

下面以知识库 26 为例。documents、成员关系、PDF 文件、chunks、实体目录和 claim_history 快照保留；活动 claims 及其 Fact、参与者、证据关联删除，受影响的派生索引失效，未再被引用的投影清理。不会触发新的 LLM 抽取。

先等上传/抽取/维护任务结束，维护期间不要提交新上传或任务。预览默认不写数据库：

```bash
cd /home/zhanghaiyang/litkg
sudo docker compose exec -T api python -m app.claim_reset --knowledge-base-id 26
```

确认预览中的 document_ids、claim_count、other_affected_knowledge_bases 后，暂停工作进程并执行清空：

```bash
sudo docker compose stop worker_parse worker_extract
sudo docker compose exec -T api python -m app.claim_reset --knowledge-base-id 26 --apply
sudo docker compose start worker_parse worker_extract
```

若存在共享文献，默认拒绝清空。物理 claim 按文献全局存储，删除共享文献的 claim 会同时影响其他知识库；确实接受预览列出的共享影响时，显式使用：

```bash
sudo docker compose exec -T api python -m app.claim_reset --knowledge-base-id 26 --apply --include-shared
```

若命令失败，事务回滚，修复阻塞原因后再执行；不要通过删除锁或强制清空 Redis 来跳过检查。命令会检查 broker、活动 lease、文献/分块锁和待执行维护任务，并提升 generation/epoch，避免旧抽取消息写回。若保留的任务状态本身已失联，先使用项目已有的任务恢复功能处理。

清空后再次运行默认预览，应得到 claim_count=0。claim_history 是恢复快照，不属于活动知识图谱，也不会因本命令删除。

## 3. 只重处理已存在的部分 PDF

无需重新上传。先从文献列表取得要处理的 document_id，前端“维护方式”可以逐篇选择“从原始 PDF 重新解析并抽取”；也可调用 API。以下 236、239 只是示例，替换为实际选中的 ID：

```bash
for doc in 236 239; do
  curl -fsS -X POST http://127.0.0.1:8080/knowledge-bases/26/maintenance \
    -H 'Content-Type: application/json' \
    -d "{\"mode\":\"reparse\",\"document_id\":$doc}"
done
```

响应包含 task_id 和 status_url，通过返回的 status_url 查询每个任务。只给所选 ID 建任务，其他已清空文献的 PDF 会留在库里，活动 claim 仍为空。

若已经逐页修正过保存的文本，改用 reextract，以保留这些校正：

```bash
curl -fsS -X POST http://127.0.0.1:8080/knowledge-bases/26/maintenance \
  -H 'Content-Type: application/json' \
  -d '{"mode":"reextract","document_id":236}'
```

reparse 会用 PDF 的重新解析结果覆盖保存文本；reextract 重建目录并强制重新抽取所有保存分块。失败续跑用 retry；仅校验并重建旧记录用 reproject，不会补回遗漏的科学事实。

局部重处理不需要先清空整个知识库。正常重处理会在新结果成功后替换相应分块的活动 claim；失败时保留旧记录/历史快照。清空后已主动删除的活动 claim 只能从历史快照恢复或重新抽取。

共享 PDF 的重处理同样会更新所有包含它的知识库。并发点击同一文献的多个维护任务没有收益，按返回的任务状态等待完成。
