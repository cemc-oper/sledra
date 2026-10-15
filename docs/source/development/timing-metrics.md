# 时间分解与不完整样本

SLD-15 在独立 `sledra-analysis` 中提供 `MetricsEngine`，从原始事实与父文档的一致 SQLite 快照生成时间区间和条件统计。使用 SLD-14 身份规则，保留所有 Attempt，包括失败、重试、未启动和缺端点；不按当前成功 Attempt 筛选历史样本。

## 发布和查询

```python
from pathlib import Path
from sledra_analysis.metrics.engine import MetricsEngine
from sledra_analysis.store.raw import RawStore

with RawStore(Path("/private/management/facts.sqlite")) as store:
    metrics = MetricsEngine(store)
    report = metrics.publish(
        report_as_of="2026-10-15T00:00:00Z",
        published_at="2026-10-15T01:00:00Z",
        cycle_window=("2026-10-09T00:00:00Z", "2026-10-10T00:00:00Z"),
    )
    history = metrics.report(report.report_id)
    snapshot = metrics.inputs(report.report_id)
    status = metrics.status()
```

先通过 AtomicIngestor 接入事实，通过 ProjectionEngine.import_documents 接入 Definition 等父文档，参见[接入事务](ingest-recovery.md)与[状态投影](state-projections.md)。`cycle_window` 可省略；存在时按 Run context 的业务 cycle 选取半开区间，执行日期不参与选样。没有计划输入时只报告 `observed_runs`，不补造未启动周期。窗口内无法确定 cycle 的 Run 单列在 unlocated_cycle_run_ids 及诊断中。

`report_as_of` 是显式 UTC 截止点，用于缺 end 样本的观测删失和端点筛选。快照保留发布时已知事实，包括截止点之后的端点；这些端点标 `after_report_as_of`，不进入完成耗时统计。它不是按历史接入时间倒查的数据库。`published_at` 单独记录发布时间。

`TimingReport` 包含 report_id、input_hash、report_as_of、published_at、watermark 和 data。data 包含 intervals、statistics、diagnostics、source_health、版本及范围说明。每个区间保留 because，指向原事件版本或父文档；外部 accounting 原始归一化输入保存在快照内。

## 区间及身份

| 指标 | 配对键和证据 | 缺失或不确定时 |
|---|---|---|
| process_execution | Run、Attempt、process_role；唯一启动 invocation 与相同 producer session 的退出 | 多 invocation、跨 session 或身份冲突不能配对；端点不全不算完整耗时 |
| stage | Run、Attempt、stage_key、interval_id | end-only 可展示声明的 duration_ns，不能把它计为完整配对样本；定义声明但无记录的阶段为 unavailable |
| dependency_wait | Run、Attempt、wait_id、selector | 不借相邻任务端点；ready/timeout/error 分别保留 |
| submission_processing | Run、Attempt、submission_instance；requested → accepted/明确 not_accepted | unknown 结果只保留观测下界和未闭合样本；迟到 accepted 可生成新报告 |
| scheduler_accounting_queue | 显式 accounting 输入绑定复合 Job 身份及 submission_instance | 保留 accepted/eligible 起点、是否包含 hold、adapter_version；无 accounting 不产生精确排队值 |
| scheduler_sampled_transition | Job 引用上的 queued/running 观察 | 输出最后 queued 观察至首次 running 观察的采样窗口和时钟质量；重复排队等顺序冲突不猜测分代；不计精确排队均值 |
| engine_observed_interval | Run、engine_instance、node_path、source/session；显式版本化状态映射 | 只称引擎观测区间；无映射保留原观察，多轮/多端点不自动挑选 |

配对不使用 recorded_at、采集顺序或临近时间。相同 event_id 的冲突版本、非法事实与错误父关联保留在 raw 和 diagnostics，不能成为有效耗时端点。完全相同语义的稳定 interval/Attempt 边界可归并，但 because 保留所有版本来源。源缺口、隔离和 pending bytes 随报告展示；完整区间不代表整个 Run 观测完整。

原生产品检查只有完成事件时展示完成时刻和 `check_interval_unavailable`。产品时效需要版本化预期 slot、确认与截止规则，本任务保持 unavailable，留给 SLD-21/22。并行区间不相加为 Run 总耗时；缺少动态依赖证据时 critical_path 为 unavailable。

## 时钟与单位

时间计算使用整数纳秒，支持完整 UTC 日期、数值时区和最多九位小数。无时区、`-00:00` 未知 offset、不可保存的精度或超出安全 duration 范围时不可计算。负墙钟差标 conflict，保留 nominal_ns；不会取绝对值或补零。

只有两个端点都明确给出 precision_ns 与 clock_error_bound_ns，且 basis 已知，才计算墙钟范围。误差取两端误差界和精度之和；零误差可输出 exact 值，非零输出 lower_ns/upper_ns。误差界覆盖差值时为 uncertain。仅有 RFC3339 时区或同一主机名不代表已同步，缺少质量信息时保留名义 UTC 差且不计均值。

stage.ended 原生 duration_ns 作为 `producer_duration` 独立保留；仅配对完整时进入该口径的条件统计，不声称它必然来自单调时钟。显式测量与有界墙钟范围不一致时保留 wall_duration_disagrees；优先口径和两种证据都可审查。

同单调时钟可优先提供可选分析扩展，放在两个端点的**事件信封** extensions 中：

```json
{"sledra.timing": {"version": "1", "clock_id": "CLOCK_MONOTONIC", "monotonic_ns": 123456789}}
```

只有 version=1、字段严格符合上述格式、source_id/session_id/非空 boot_id 相同且 clock_id 相同，才可相减；seq 可以不同。未知版本、负单调差、跨 boot/session 或与同时声明的 duration_ns 不一致均不产生完整耗时。该扩展是显式分析输入约定，现有 Go/Bash 记录器没有自动输出它；没有扩展时不会推断单调时钟。单调值可在墙钟跳变时计算，但墙钟 conflict 仍显示。

## 外部 accounting 和引擎映射

`publish(accounting=[record])` 接受以下严格 version=1 的归一化记录，额外字段拒绝：

```json
{
  "version": "1", "adapter_version": "site-parser-1",
  "run_id": "run_...", "job_ref_id": "job_...",
  "submission_instance": "submit-a", "interval_id": "queue-a",
  "queue_basis": "eligible", "includes_hold": false,
  "start": {"occurred_at": "2026-10-14T23:59:58Z", "time_quality": {"basis": "source_log", "precision_ns": 0, "clock_error_bound_ns": 0}},
  "end": {"occurred_at": "2026-10-15T00:00:00Z", "time_quality": {"basis": "source_log", "precision_ns": 0, "clock_error_bound_ns": 0}},
  "because": ["event_id@canonical_hash"]
}
```

because 必须引用已接入、同 Run/Job 的 known 原始事件版本；Job 文档必须解析至 Attempt 和当前提交世代。重复 identity 的不同记录在整个发布事务中拒绝。调用方负责从 accounting 原始证据归一化端点、时钟界和 hold 语义；接口保存并校验关联，不把一条 running 轮询自动认证为 accounting。当前仅有合成 adapter 测试，没有站点 scheduler parser。

引擎状态使用 `publish(engine_mapping={"version": "site-map-1", "submitted": ["submitted"], "active": ["active"]})` 显式定义。映射和版本进入输入摘要；两类非空状态列表必须互斥。原始大小写与值严格匹配。计算使用 payload.observation_time，引擎完成不会生成进程、产品成功或交付事实。

## 样本与统计

每个分层同时给 total、pairing_counts、outcome_counts、missing_start、missing_end、unavailable、right_censored、incomplete，以及时钟质量、耗时口径和诊断计数。start-only 的删失表示“截至此时没有观察到 end”，不证明进程仍在运行；已知 start 时间且不晚于截止时点才计删失。时钟界足够时另列截至 report_as_of 的 observed_lower_bound_ns，绝不混入完成耗时。

均值、中位数和 p95 只对完整配对且存在 exact/显式 duration、端点不晚于截止时点的样本计算。成功、失败和 unknown 结果都在这个条件内；报告同时列 n 和 failure_n。p95 使用 nearest-rank；均值和中位数使用字符串 numerator/denominator 的精确有理数，避免累计纳秒溢出或浮点舍入。n=0 时统计量为 null。

分层键保留指标、阶段或 selector、purpose、definition_ref、config_refs、platform_id、task_key、参数及 metrics 规则版本。accounting 另含 queue_basis、includes_hold、adapter_version，引擎另含 mapping 版本；不同口径不自动合并或排名。

## 持久化和验证

首次构造 MetricsEngine 增量创建 timing schema 1，raw schema 3 与 SLD-14 projection schema 1 不变。报告和完整输入快照不可变，head 与消费水位同事务提交；重复输入和截止点复用原报告及原发布时间，迟到事实生成新 ID。重新发布可以更新 head 消费位置，报告中的原始水位不改写。独立于状态 head，不会替状态消费者确认队列。

沿用 ProjectionEngine 的事实数、文档数、输入/输出预算；accounting 输入也受计数、单条和总字节预算限制。超限、SQLite COMMIT 失败或崩溃保持旧 head；恢复后可重放。Backup 自动包含报告、快照和 head。`status()` 的 current 仅指 raw 队列水位一致，不证明源文件已完整采集，也不自动推进 report_as_of；新截止时点需显式 publish。

```bash
uv run --project analysis --locked --offline python analysis/tests/verify.py --task SLD-15 --report-dir /tmp/sledra-timing-new
uv run --project analysis --locked --offline python analysis/tests/replay_metrics.py --report-dir /tmp/sledra-timing-replay-new
```

V07/V22 回归覆盖跨日、纳秒、时钟界、缺端点、失败/右删失、并行、提交未知、轮询与 accounting 区分、迟到修订、性质重放、SQLite busy、SIGKILL 和备份恢复。回放同时保留 22 份冻结 M1 bytes；其时钟界未知时不凭空输出精确耗时。真实站点时钟、scheduler accounting、HPC、性能资格及 SLD-23 核心收口仍需后续验证。
