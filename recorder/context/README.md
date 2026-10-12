# Context 与生命周期 CLI（SLD-05）

服务用调用方显式 launch/execution token 创建 Run/Attempt context。Run 请求包含已验证 DefinitionManifest、可选 ProductExpectation、明确 cycle/scope；默认参数先展开再计算 ID。服务在 SLEDRA_ROOT/documents/ 发布不可变文档，再调用 Registry。相同 token/request 复用赢家，异内容冲突；同周期新 launch token 创建不同 Run，新 execution token 创建不同 Attempt。retry_of 必须属于同一 Run/TaskInstance，分叉保留。

公共 context 为固定绝对路径、0400 权限的不可变规范 JSON。Attempt 通过 extensions["sledra.parent"].registry_id 定位确切 Run 权威记录，不按周期搜索。validate 核对摘要、Registry、父 Run/DefinitionManifest/可选 expectation、scope 参数与 retry 父级；缺失/损坏报错，不创建替代身份。Registry.Read 不为缺失 ID 创建目录。

run finish 在 finish/ 保存不可变声明及 run.finish_declared outbox。scope_ref 为 scope_ 加 Run scope 规范摘要，排序的 covered_attempt_ids 及摘要冻结覆盖集合。重复相同声明复用，同 declaration_id 异内容冲突，不同声明可并存；后来 Attempt 不改旧声明，needs_finalization 解释由 M2 负责。outbox 不是 spool 段或投影。

在 recorder/ 以 GOPROXY=off GOSUMDB=off go build -o /tmp/sledra ./cmd/sledra 构建，SLEDRA_ROOT 须为已有真实绝对目录。默认契约内嵌；资源覆盖错误见 [CLI](../../docs/source/cli.md)。

```text
sledra registry run-create --request-file run-request.json --context-out /shared/root/contexts/run.json
sledra registry attempt-create --context /shared/root/contexts/run.json --request-file attempt-request.json --context-out /shared/root/contexts/attempt.json
sledra context validate --file /shared/root/contexts/attempt.json
sledra run finish --context /shared/root/contexts/run.json --scope-file finish-request.json
```

Run 请求字段为 registry_id、launch_token、system_id、purpose、deployment_id、platform_id、cycle、scope、definition_file、可选 expectation_file、config_refs、rerun_of。Attempt 使用 task_key、parameters、execution_token、可选 retry_of、process_role。finish 使用 declaration_id、rule_version、covered_attempt_ids、reason、result（finished/abandoned/unknown）、source_id、session_id。拒绝未知字段和重复键。

成功 stdout/stderr 为空；失败 stderr 为 {"ok":false,"code":"..."}，退出 1。Registry 已发布但 context-out 失败时，使用相同 token/绝对输出路径重试，不再次调用业务。源码根执行 python3 spec/verify_context.py；Go 测试覆盖同周期 Run、retry、参数拒绝、只读/篡改/缺失 context、并发 finish、后增 Attempt 与迟到旧事件。本地证据不包含真实调度、共享/HPC、分析或业务调用。
