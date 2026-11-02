# 批次取消

在“批次进度”选中排队中或处理中的批次，点击“取消批次”。服务立即持久化取消状态，已完成的文献结果和成功分段缓存保留。
独占的未完成文献停止处理；其他批次或独立重处理维护任务仍需要的共享文献继续处理，本批次的条目保持已取消。
同一批次重复取消不会再次改变文献处理代次。已结束批次返回409，不存在的批次返回404。

API：

```http
POST /ingestion-batches/{batch_id}/cancel
```

返回示例：

```json
{"batch_id":9,"status":"cancelled","already_cancelled":false,"stopped_document_ids":[12],"continued_document_ids":[13]}
```

排队任务在入口读取持久状态并跳过已取消文献。正在进行的解析或模型请求返回或超时后，任务在检查点退出。
检查点覆盖模型调用与纠错/扩容重试、缓存和结果提交、后续目录/分块任务投递；写入时加文献行锁并校验处理代次。
取消和新请求会推进处理代次，旧任务返回时不能覆盖新请求或把取消状态改回处理中/失败。
不对worker进程发送终止信号，不清空共享队列，也不依赖worker内存中的revoke列表。
因此重启worker后取消状态仍有效。已经发出的SGLang请求不保证立即释放GPU，需要等请求返回或超时；未实现模型服务专用abort API。

同一文献属于多个批次时，取消只撤销本批次的处理需求。其他批次有活动条目，或独立retry/reextract/reparse维护任务正在排队/执行时，文献继续处理。
两个共享批次并发取消通过短事务串行检查最后一份需求；这把锁不覆盖模型调用。
已结束的历史批次不会因为另一批次取消而被改为失败。
取消发生在上传阶段时，停止继续登记和投递；已存储的PDF文件保留。

恢复时，等待在途调用退出，在知识库维护界面填写具体文献ID，选择“重试失败阶段”；也可以显式重新上传该PDF。
取消批次本身保持历史状态。整个知识库的普通retry会跳过已取消文献，避免无意恢复已撤销的任务。
每个文献的成功缓存继续复用，重试无需清空缓存。

## 部署

不增加表或列：复用现有文本状态字段及pipeline_generation，无需数据库迁移。
需要同时更新API、解析worker、抽取worker和frontend。等待当前任务尽量完成后在gpu02执行：

```bash
cd ~/litkg
docker compose build api worker_parse worker_extract frontend
docker compose stop -t 600 worker_parse worker_extract
docker compose up -d --no-deps --force-recreate api worker_parse worker_extract frontend
```

需要提权时给docker命令加sudo。优雅停止的等待上限应覆盖在途任务耗时。
本轮开发验证不重启生产服务，不取消实际批次，不调用在线模型。
