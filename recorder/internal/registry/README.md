# 文件 Registry（SLD-04）

Registry.Register 接收完成的 Run/Attempt context、命名空间、父身份和调用方 token，在操作路径前校验 schema、context/token 摘要和确定性 ID。[context 服务](../../context/README.md)负责父级/scope 语义及公开 CLI；Registry 不提交业务，也不在事件产生时补身份。

权威路径为 SLEDRA_ROOT/registry/<sha256(registry_id)>/<run|attempt>/<ID前两位hex>/<id>.json，只由完整 ID 和 namespace 推导，cycle 不进入 Run key。根必须是支持 hardlink create-if-absent 和目录 fsync 的真实绝对目录。

记录是闭合字段集的规范 UTF-8 JSON，包含对象种类、registry/parent/identity、token 和规范 request 摘要、完整 context/摘要、创建时间/role、完整注册 outbox 与记录摘要；不存原 token。evt_ ID 由注册身份和类型推导，重复请求返回赢家原记录、时间及事件；没有单独可能丢失的 outbox 写入。

发布依次创建/fsync 目录链，写/fsync 同目录0600临时文件，hardlink 不替换发布，fsync 目录，删除临时名并再次 fsync。输家在读赢家前也 fsync。异请求为 conflict，权威损坏为 corrupt_record，不覆盖；发布前错误为 registry_unavailable，不确定 link 或发布后同步/确认失败为 indeterminate。观测错误只允许同 token 重查，不能重提业务。CleanupStaleTemps 清理旧孤儿临时名/旧临时硬链接，不删除权威文件。

本地 WSL/ext4 验证：

```bash
cd recorder
GOPROXY=off GOSUMDB=off go test ./internal/registry -count=1
GOPROXY=off GOSUMDB=off go test -race ./internal/registry -count=1
GOPROXY=off GOSUMDB=off go vet ./...
```

临时目录和合成输入覆盖 Run/Attempt 各100个同 token及不同 token竞争、独立进程、冲突/损坏、golden ID、全部发布故障点、进程退出和临时名清理。只说明实测本地操作，跨节点/共享卷、断电持久性、生产 context 和开销需独立证据。
