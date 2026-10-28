# Execution 汇总与统计（P2-02）

[summary.py](summary.py) 直接消费 [reader](README.md) 的合法 Fact/ReadIssue。沿用 2.0.0/布局 2 和精确比较，无数据库、旧投影、登记、索引或额外配置。P2-03 的 [sledra-history CLI](cli.md)、默认窗口和文本/JSONL 输出已实现，沿用本文统计口径。

## API

```python
from sledra_analysis.execution.reader import FileReader
from sledra_analysis.execution.summary import group_counts, iter_summaries

with FileReader("/absolute/independent/root") as reader:
    counts = group_counts(
        iter_summaries(reader, "2026-10-27", "2026-10-28"),
        namespace=reader.config["namespace"],
        by=("task", "parameters", "cycle"),
    )
    print(counts.total, counts.groups, counts.uncertain, counts.read_issues)
```

`iter_summaries(reader, record_from, record_through)` 与 reader 的显式窗口含义一致，返回 ExecutionSummary 或单独 ReadIssue。按物理文件消费，一次只归约一份 Execution 的事件；文件中的错误附在该 summary.read_issues，没有合法事实的文件仅返回错误，不创造可见执行。提前停止保存的生成器应 close。`summarize(records)` 也可直接归约同一个 Execution 的已校验记录，不重新解析源文件；混入另一 ID/Run 被拒绝。调用者不得修改返回的 data/attributes 等记录字典。

ExecutionSummary 包含 execution_id、events、start/context/end、duration、stages、conflicts、read_issues。Event 保存原解码 data 和全部源位置 sources；相同 event_id、相同完整内容仅一份 Event，重复行的位置全部保留。同 ID 异内容则保留每个变体，标记 conflicting_event_id，不按时间选择赢家。精确数值、已定义时间归一、布尔/字符串区别和不做 NFC 均沿用 codec；不生成 canonical 字节或身份摘要。

start/context/end 为 Selection，status 是 missing/unique/conflicting，records 保留所有对应事件。只有 unique 的 attributes 才返回一份可用值；缺失或冲突时为 None，源事实仍可检查。不同 ID 的等价 context 仍是唯一上下文；不同最终内容标记 conflicting_context，task/parameters/cycle/Run 确定分组全部不可用，不能回退到 started 的 task。入口来源 engine/alias 保留在 start.records，不猜原节点、父执行或重试关系。

相同结束 attributes 不冲突，结果/声明时长有差异则 conflicting_end；相同结果但不同结束时间保留事实并将墙钟差标为有歧义。不把最后一次退出零视为最终成功；退出零只表示观察到该退出码，不代表产品交付或整轮完成。end.status=missing 表示“未观察到结束”，不表示“运行中”。event_id 碰撞涉及生命周期事实时，其 Selection 保守标为 conflicting，不从碰撞中选择可用 context/结果。

## 开始数、可见数和分组

`group_counts(summaries, *, namespace, by=("task", "parameters", "cycle", "run"), runs=None)` 一次处理**同一 reader/根**的结果。namespace 应取 reader.config；不能混合不同根后按相同 ID 无条件合并。返回 Grouping：

| 字段 | 含义 |
|---|---|
| total.visible | 有任意合法事实的不同 execution_id 数 |
| total.started | 上述 ID 中存在至少一份合法 execution.started 的数量，即使开始事实自身有冲突，也只计一次 |
| total.missing_start | 可见但没有开始事实的数量；visible = started + missing_start |
| groups | 每项含 key 和相同三项 Counts，仅含当前维度能确定的成员 |
| uncertain | 当前分组维度无法确定的可见/开始/缺开始数量；不混入已知 null/none/multiple |
| duplicate_execution_ids | 相同 ID 出现在多份文件，按 ID 只计一次，业务归属保守不确定 |
| read_issues | 所有文件/目录读取问题；合法事实可见不表示全历史无缺失 |

每个 key 包含 namespace 和 scope。默认 by 保留最终 task、**完整** parameters、cycle_kind/归一 cycle 和确定 Run；显式 by 可仅选择这四个固定维度的子集，例如 task+parameters 或 cycle 或 run。scope 始终保留，避免无意混合不同业务范围。参数键顺序不影响分组，所有键参与；参数缺失、null、字符串、布尔、数值彼此区分，只有数值按精确数学值相等（25/25.0 相同）。不同分辨率/成员不会在默认分组中被丢字段合并。key 中保留一份等价的源参数/时间表示，不承诺输出 canonical 字节。

缺 context 与 none/multiple 分开；unique none/multiple 可进入各自分组，只有 single 带执行主 cycle。`by=("cycle",)` 可查看执行自身周期，不会把 Run cycle 写入无时次辅助执行。统计只保留每 ID 的开始标志、分组键和错误位置，不保留全历史业务事件；单执行的事件/位置仍需内存，调用者若 list(iter_summaries(...)) 则主动保留这些明细。

合法布局每个 Execution 只有一个文件。跨文件重复 ID 是布局归属问题；每份原文件的 summary 明细保留，group_counts 的总数去重并单列，不把另一份文件的 context 当作更新。完全损坏、只有半行或空文件均不能凭文件名增加可见数。

## 合成 Run 的已记录关联

`associate_run(summary, lookup)` 解释已写入最终 context 的 run_id；lookup 必须是**同一 reader**的 `resolve_runs(ids)` 结果。不自动寻找最新 Run，也不新建或接受生产候选。可先收集引用 ID 再调用 reader.resolve_runs，然后按文件重扫以统计；或在小样例中保留 summaries。Run 查找仍跨保留的创建日期，与 Execution 窗口独立。

| status | 含义 |
|---|---|
| context_missing/context_conflicting | 没有可用于关联的最终上下文 |
| unlinked | 明确的最终 run_id:null，可作为独立执行分组 |
| not_loaded | 调用者没有提供该 ID 的查找结果，不能当作缺失 |
| missing/conflicting/unavailable | 沿用 reader 的引用结果，保留 reference 与源位置 |
| scope_mismatch/cycle_mismatch | 元数据与最终上下文不适用，不能确定进入 Run 分组 |
| associated | 已记录引用与可读元数据在 scope/cycle 上一致；不是生产来源/新鲜度证明 |

命名空间/文件路径有效性由同根 reader 校验。single Run 与 single 执行要求同一时刻，允许显式关联的 none 辅助执行；none Run 只对应 none；multiple Run 的已记录批处理关联可以展示 single/none/multiple，仍要求 scope 一致。这仅解释合成最终事实，不能证明应用确实配置了辅助/批处理范围，也不执行 P4 的来源、scope 配置授权或传递接受流程。alias 的空 Run 保持独立，不靠相同 cycle/节点名改写关联。

选择 run 维度时，缺失/冲突/不可用/不适用/未加载引用进入 uncertain；选择 task/parameters/cycle 而不含 run 时，已有明确上下文仍可参与这些分组。关联问题不能使 Execution 从 total 消失。RunLookup.issues 保留 Run 扫描问题，Grouping.read_issues 保留 Execution 扫描问题，调用者展示时同时呈现。

## 阶段、耗时和目标时次

stages 按本执行的 stage_id 配对，name 不作为配对身份；同名阶段不同 ID 是不同区间，乱序也不靠时间猜配对。每个 Stage 保留 start/end Selection、duration、conflicts。缺端点不借用其他区间；同 ID 名称不一致、开始/结束 attributes 冲突显式报告，区间耗时未知。

Duration 的 declared_ns 与 wall_ns 分开：前者只取已记录 duration_ns，不推断其时钟来源；后者仅在起止明确且各只有一个已定义时间点时，按 UTC 秒/纳秒相减。完整端点可以只有墙钟差而无声明时长。缺端点或冲突时两项均为 None（原结束声明仍保存在源事件）；时间有歧义时 wall_ns=None。负墙钟差保留负数并报告 negative_wall_time，不钳为零，不覆盖声明耗时。不同耗时字段不互相校准或补造。

`events_for_target_cycle(summary, cycle)` 只选择业务事件中带合法 RFC3339 target_cycle 且时间点相同的事实；普通属性中的无效/非字符串值不自动解释，原事件仍保留。调用参数必须是有效时间。此函数不重写 summary.context，不复制可见执行计数，不把 multiple 变成多个 single；业务目标过滤与执行主 cycle 分组是独立入口。

[汇总回归](../../../tests/test_execution_summary.py) 包含冻结流向量、精确分组、合成两轮 Run/多执行/alias/辅助/多时次、缺失/冲突与坏文件隔离、重复文件、阶段/耗时边界、统计释放事件以及禁写/禁连接新进程。现有实际 wheel 回归同时验证已安装 summary，不依赖源码、旧模块或数据库。所有 Run/engine/alias 值均为合成测试，未接入 ecFlow 或真实 Run 发布。
