# sledra-history 直接文件查询（P2-03）

命令来自现有 sledra-analysis Python 3.12+ 包，入口为 [cli.py](cli.py) 的 main；直接复用 [reader](README.md) 和 [汇总/统计](summary.md)。只需已有兼容的绝对记录根与配置好的查询环境，不运行导入、数据库、登记服务或查询索引。格式仍为 2.0.0/布局 2。

## 安装与开始查询

将当前 analysis wheel 安装到具备固定 jsonschema 4.10.3 依赖的查询环境后使用生成的 `sledra-history`。通常的 wheel 安装可以由 pip/uv 安装依赖；离线安装须预先准备这些依赖。源码开发环境已配置时，也可以在仓库根运行 `uv run --project analysis --locked --offline --no-python-downloads sledra-history ...`，或在可导入当前包的环境中运行 `python -m sledra_analysis.execution.cli ...`。不需要记录节点安装 Python。

以下命令只读取已有根；SLEDRA_ROOT 应为部署配置的独立绝对路径，命令不会创建/修复根或记录：

```bash
# 默认：根分区时区的今天及此前 6 天，两个端点都包含。
sledra-history --root "$SLEDRA_ROOT"

# 记录日期与业务 cycle 分开；今天补跑旧 cycle 应搜索今天的记录分区。
sledra-history --root "$SLEDRA_ROOT" \
  --record-from 2026-10-27 --record-through 2026-10-28 \
  --cycle 2024-07-01T00:00:00+08:00

# 参数为精确 JSON 标量；引号和类型有意义，全部给定条件取 AND。
sledra-history --root "$SLEDRA_ROOT" --task main.forecast \
  --parameter resolution_km=25 --parameter 'member="01"'

# 显式扫描全部仍保留的记录日期，以流式 JSONL 输出。
sledra-history --root "$SLEDRA_ROOT" --all-record-dates --format jsonl

# 事件目标时次与执行主周期是不同查询；同时给事件名时必须是同一事件满足条件。
sledra-history --root "$SLEDRA_ROOT" --all-record-dates \
  --event forecast.input --target-cycle 2024-07-01T00:00:00+08:00
```

记录日期必须同时给 --record-from/--record-through，格式 YYYY-MM-DD；与 --all-record-dates 互斥。cycle/execution_id/run_id 等业务条件**不会自动扩大记录窗口**。按 ID 无结果时仍显示实际搜索范围；显式全量也只表示仍保留的文件，不表示所有真实启动都被记录。

## 过滤与分组

| 参数 | 语义 |
|---|---|
| --task、--scope | 精确匹配唯一最终 context，缺失/冲突时不匹配，不用入口 task 替代 |
| --parameter KEY=JSON | 可重复，匹配给定键的子集；要求所有键存在且精确相等。完整参数仍参与默认分组，不因过滤只写一个键而丢掉其他参数 |
| --cycle | 匹配最终 single cycle 的同一时间点，保留 UTC 纳秒精度；不能同时指定 none/multiple |
| --cycle-kind single/none/multiple | 查询明确周期类型；missing/conflicting context 不属于 none |
| --run-id UUID | 过滤最终 context 已记录的引用，结果仍展示 missing/conflicting/unavailable/scope/cycle 不适用等状态；有引用不等于确定 Run 分组或生产来源证明 |
| --execution-id UUID | 在当前记录窗口定位执行，不要求存在最终 context |
| --event NAME | 要求存在该业务事件，不接受保留的 execution/stage 事件名 |
| --target-cycle RFC3339 | 要求业务事件明确给出该合法目标时次，不改写执行主 cycle，不把 multiple 复制计数 |
| --group-by DIMENSIONS | 默认 task,parameters,cycle,run；仅允许这四个维度的非空、不重复子集。namespace/scope 始终保留，显式减少维度才会合并对应参数/周期/Run |

ID 为既定小写 UUIDv4。参数保留布尔/数值/字符串/null 区别、完整大整数和精确小数；25/25.0 相等，"25" 与 25、缺键与 null 不等。重复参数键、非标量、越界值、无效时间/日期在扫描前拒绝。自定义名字/路径/属性仅作为数据输出，控制字符转义，不执行字符串内容。

同 ID 跨多文件属于布局归属异常：逐文件明细保留，但确定 context 过滤不匹配；按 ID 查询可以检查全部原事实，总数只计一次，业务分组进入 uncertain。context 冲突同样不会被归入某一确定 cycle/Run；可去掉 context 条件并加 --execution-id 检查 sources。

Run 元数据扫描全部保留的**创建日期**，独立于 Execution 记录窗口。输出的 Run 范围是“当前扫描窗口中所有引用，以及显式 --run-id”，包括未命中其他业务条件的引用；不是所有 Run 或该窗口内创建轮次数量。没有匹配 Execution 也可展示所请求 Run 的元数据。Run 问题不能隐藏已记录的执行；RunLookup 状态解释沿用汇总接口，P2 的 Run/alias 样例均为合成，不实现 begin_run/发布/来源接受。

## 输出、数量与退出码

默认 text 带 Coverage、逐执行状态、原始事件/来源、阶段与分组、最终 Summary。end=not_observed 表示“未观察到结束”，不表示仍运行；context 冲突与 ended 冲突不选择最后一条。退出零是退出事实，不是产品交付或完整工作流成功。

--format jsonl 每行一个对象，按实际遍历顺序流式输出：

| type | 内容 |
|---|---|
| coverage | 输入格式/布局、namespace、根分区时区、窗口模式/端点、过滤和分组条件、有限窗口标志、snapshot:false |
| issue | executions 或 runs 范围的错误代码、根相对路径/行号/字节位置，不复制坏记录；两遍观察到的相同问题只报告一次 |
| run | 查找的 ID、状态及全部合法候选的内容/源位置，不选“最新” |
| execution | 逐文件汇总、入口/最终上下文/结束状态、Run 解释、耗时/阶段/冲突和去重事件的全部 sources；事件条件另给 matched_event_ids，明细仍保留全部事实 |
| group | 当前过滤结果按所选维度的确定分组和 visible/started/missing_start |
| summary | scanned、matched、uncertain 数量、窗口中未确定 context 的 ID、重复文件 ID、问题数、不完整性原因、no_matches 以及重复的覆盖范围 |

scanned 是第一遍看到的不同执行数，matched 是第二遍符合所有过滤条件的数量；各自的 visible 是任意合法事实可见数，started 是其中存在开始事实的数，missing_start 是差值。uncertain 是已匹配但当前分组归属不确定的数量；例如 --execution-id 可以找到没有 context 的执行。两遍间成员发生变化会显式报告，scanned 和 matched 不强制相等。execution_rows 是逐文件明细行数，异常重复文件时可大于 matched.visible。

JSON 数值保持精确，不将 Decimal 转 float 或偷偷编码成字符串；消费方应使用 Decimal/精确数值解析，避免自身损失大整数/小数精度。业务原值和事件时间保留原表示，cycle_display 另以根时区展示九位纳秒；超出 Gregorian 展示年范围时该展示字段为 null，原 cycle 仍在。Group 也提供 cycle_display。输出是查询结果，不是新录入格式或 canonical 字节协议。

| 退出码 | 含义 |
|---|---|
| 0 | 完成查询，可能无匹配，也可能缺少开始/context/结束或有时钟质量提示；必须检查 summary.incomplete 和覆盖范围 |
| 1 | 完成但看到读取错误、事实/文件冲突、无法确定的 Run 引用或两遍间成员变化；仍输出可用事实和最终 summary |
| 2 | 参数无效、根不兼容/不可读或查询 I/O 失败。参数错误为标准用法诊断；根/I/O 错误为 stderr 上固定代码，不回显原记录 |

有限窗口本身不意味着查询失败；incomplete_reasons 描述所观察到的数据问题，finite_window 描述覆盖范围，两者分别检查。扫描问题覆盖整个所选窗口，不能因业务过滤或没有匹配而忽略。缺阶段端点、上下文缺失、声明耗时与墙钟差、负墙钟差均按原汇总语义显示。

CLI 先逐文件收集紧凑 ID/Run 引用和布局异常，再读取引用元数据，最后逐文件重扫并输出明细/分组。内存不保存全历史事件，只有当前执行及 ID/分组/问题信息；没有临时查询数据库、结果缓存、checkpoint 或源文件修复。两遍均使用各文件打开时大小上界，**不是跨文件原子快照**，不保证检测任意追加/等长改写；活动半行可以在后续查询补齐后读取。需要判断数据完整性时同时看 coverage、issue 和最后的 summary，不能将观察状态当作产品交付证明。

[CLI 回归](../../../tests/test_execution_cli.py) 使用临时合成记录；实际 wheel/console entry 测试复用已有运行依赖，在临时安装目录脱离源码执行，并禁止查询写入、网络或 SQLite。新鲜环境完整依赖解析、真实共享卷资格和 Go/Bash 实际写入后的查询闭环分别属于环境准备/后续 P2-04 或平台验收，不由合成例子替代。
