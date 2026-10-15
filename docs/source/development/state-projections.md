# 身份关联与多维状态重建

SLD-14 提供 `ProjectionEngine`，从持久 raw 事实、父文档及 source 健康信息重建 Run、TaskInstance、Attempt 和 Job。相同输入集合、契约和规则版本得到相同 revision ID，不使用接入顺序或最新墙钟时间裁决业务状态。

## 使用与持久输入

```python
from pathlib import Path
from sledra_analysis.store.raw import RawStore
from sledra_analysis.projections.documents import DocumentRecord
from sledra_analysis.projections.engine import ProjectionEngine

with RawStore(Path("/management/facts.sqlite")) as store:
    engine = ProjectionEngine(store)
    engine.import_documents([
        DocumentRecord(definition_bytes, "definition.json", "2026-10-15T00:00:00Z"),
    ])
    revision = engine.rebuild(published_at="2026-10-15T00:01:00Z")
    run = engine.get("runs", run_id)
    historical = engine.get("runs", run_id, revision_id=revision.revision_id)
    freshness = engine.status()
```

示例变量由调用方提供；管理库必须已有接入事实。父文档使用显式 bytes 和 locator，不自动读取事件指定的路径。支持 DefinitionManifest、RunContext、AttemptContext、JobRef；`DocumentRecord.pointer="/context"` 可保存原 Registry wrapper。原始 bytes、locator、采集时间和 pointer 保存在 `projection_document_sources`，规范文档按 kind／ID／hash 保留全部版本，同 ID 不同内容不会被覆盖。整个导入批次及对应重建队列同事务提交。

已经持久接入的 Registry `run.registered`／`attempt.registered` wrapper 会在重建时提取 context；核对原 bytes 摘要、context 摘要及 wrapper 的 Run/Instance/Attempt 关联。无须另读共享 context 副本；DefinitionManifest 和后来产生的 JobRef 仍须显式导入。缺文档不补造父对象，相关观测保留为 orphan/source_unknown；迟到父文档可产生新 revision 并解除可解决的缺口。

分析包新增四份逐字节匹配 `spec/1.0.0/` 的父 schema，运行完全离线。校验 context／manifest 内容摘要、任务实例的长度编码身份、参数展开、已声明类型／枚举、scope、Job 的平台／集群／作业号／提交代次复合 ID。Run／Attempt token 原文不在公开 context 中，不声称能从其摘要重新证明原 token。信封／payload 由当前 EventValidator 重新验证，错误或未知语义不会作为确定成功依据。

## 因果、选择和生命周期

| 维度 | 重建行为 |
|---|---|
| Attempt 执行 | registered_only、started、exited、signaled、end_only、unknown、conflict；end-only 保留退出码并标 missing_start，不补 start 或 duration；start-only 标 missing_end |
| 执行角色 | 按 context 的 process_role 关联开始／退出；多 invocation 或矛盾端点不猜配对，显示冲突／关联不确定 |
| 业务阶段 | stage_key＋interval_id 独立关联，保留 start_only/end_only/success/failure/unknown/conflict；阶段失败与进程 exit 0 可以同时存在 |
| 实例当前尝试 | 根据 retry_of 图找末端；失败→退出 0 展示 recovered，退出 0→新增登记／运行展示 rerunning；全部历史 Attempt 保留 |
| 分叉 | 多末端或断链／跨实例／循环不选赢家，显示 ambiguous；不取最新成功、记录时间或事件到达顺序 |
| 显式选择 | 校验所有候选属于本实例，当前所有末端都在候选范围；新末端使旧选择失效；supersedes 链唯一末端才生效，缺父、循环、并发独立声明或复用 ID 异内容均不猜赢家 |
| Run 结束 | 按 scope_ref／rule_version 与覆盖集合包含关系解释；新增 Attempt 不在覆盖中则 needs_finalization；覆盖未知 Attempt 则 pending_association；相互矛盾或不可比较的声明保留为冲突；扩大覆盖的新声明不覆盖旧记录 |
| 引擎和 Job | 保存原始状态、观察时间、来源、人工控制操作及所有 Job 代次；多个状态保留为 multiple_observations，无可靠因果顺序时不声称已知“最新状态” |
| 产品与交付 | 按原 artifact version／rule／slot 保留独立事实，product_state 为 unknown；进程 0、引擎 Complete、调度器 DONE 不自动升级产品合格或交付确认 |

实例 `exited_zero`／`recovered` 是执行层摘要；不表示整个业务或产品交付成功。结束声明是生命周期事实，也不代表全部观测完整。不同 scope/rule 的结束声明分别展示，不合并覆盖分母。

显式选择的候选可包含历史尝试，但必须覆盖当前全部末端；因此候选只列分叉末端或列完整尝试集合都可表达。历史声明按其 supersedes 链保留，当前候选完整性只针对生效末端声明判断，后续修订可以重新选择。父引用不匹配的事实保留在 raw，并在该 revision 的 rejected_facts 中记录原因及事实 ID；迟到引用可重新解释，不回写／删除原隔离或事实。

## 修订、解释和队列水位

`reducer_version=sledra-state-reducer-1.0.0`，`rule_version=sledra-state-rules-1.0.0`。未实现的 rule_version 拒绝运行。输入摘要包含全部事件版本／当前语义分类、规范父文档、source 健康、验证器与嵌入契约摘要；不包含到达顺序、采集时间、SQL rowid、inode 或当前系统时间。

每个结论包含 `because`，引用 `event_id@canonical_hash`、文档 kind/ID/hash 或 source 健康 ID。`facts`、`input_documents`、`source_health` 是对应索引；原事件还可由 RawStore.sources 查回准确 bytes／位置，父文档来源可查询持久 document source 表。冲突版本全部列出并从确定语义推导中排除。

`rebuild` 在同一管理事务读取一致输入，发布不可变 `projection_revisions`，再推进 `projection_heads` 的消费水位。失败则 revision／head／cursor 一起回滚，raw 事实和队列保留。提交成功但响应丢失可重试复用同一 revision；迟到事实生成新 revision，旧 revision 的内容、published_at 和首次输入水位不覆盖。相同语义输入的新增来源可只推进 head 水位，首次发布的 revision 保持原样。

`status` 分别返回 source_watermark、projection_watermark 和 current/pending；尚未重建的队列不能显示为已完成。`get(kind, id, revision_id=...)` 支持 runs/instances/attempts/jobs；不传 revision 时返回当前 head 的投影，调用方应同时查看 status。published_at 由调用方显式给定，数据范围以输入集合及持久水位解释，不以不可靠的事件墙钟过滤事实。

source gap、legacy 完整性未证明、段冲突、未消费 bytes 和隔离记录在可定位的 Run 中展示，无法关联的来源保留在全局 source_health。历史诊断另列；已经补齐的半行不继续当作当前 pending bytes。observed_scope 仅表示当前已观察范围，不承诺整个业务完整。

## 存储、预算和恢复

RawStore 的 schema 仍为 3；首次显式构造 ProjectionEngine 时事务创建独立的 projection schema 1。它增加父文档输入、来源、revision 和 head 表，使用同一个管理单写者连接；未知 projection schema 拒绝，初始化失败回滚。旧 raw/quarantine/checkpoint 不修改，未迁移运营库。

默认最多 50000 个事件版本、10000 个显式文档与 Registry wrapper、64 MiB 规范事实／父文档／wrapper bytes，原始来源和隔离数量各不超过事件预算的十倍，source checkpoint 数不超过事件预算；输出最多 64 MiB。导入文档 raw／canonical 各不超过 RawStore 单条预算。超限或摘要不匹配时停止重建，保持旧 revision 与待处理水位。这些限制控制本地完整重建规模，不代表总 RSS 或 A6 预算。

当前使用全量确定性重建，队列为追加日志，尚无后台 worker／删除或性能增量优化；通过显式 rebuild 消费当前快照。SQLite 一致备份自动包含父文档、历史 revision、head 与队列；恢复后可继续重建。私有测试模拟删除派生投影后，由 raw＋父文档重建相同当前 revision；没有产品 API 自动删除已发布 revision 或业务报告。

## 验证入口与边界

```bash
make -C analysis test
uv run --project analysis --locked --offline --no-python-downloads --python /usr/bin/python3 python analysis/tests/verify.py --task SLD-14 --report-dir /tmp/sledra-state-new
uv run --project analysis --locked --offline --no-python-downloads --python /usr/bin/python3 python analysis/tests/replay_projections.py --report-dir /tmp/sledra-state-replay-new
```

38 项新增 V05/V07/V08/V09，包含固定 seed 14 的 40 轮乱序／重复／迟到前缀性质验证、3 个边界的全部 6 种排列、4 个真实子进程 SIGKILL 发布切点、父引用与原始版本冲突、消费水位失败回滚和备份恢复。冻结 M1 的 19 条事实重建为 2 Run／2 Instance／4 Attempt：失败后成功保留恢复历史，后来登记的 Attempt 使原实例 rerunning、原 Run needs_finalization。

SLD-15 时间／缺端点统计、SLD-21/22 产品规则聚合与报告、SLD-23 核心集、真实业务/HPC、远端 CI、A6/A7 仍待对应任务验收；本任务不执行自动重试、根因分析或生产清理。
