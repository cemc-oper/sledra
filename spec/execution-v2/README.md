# Execution 2.0.0 记录与布局契约

本目录是简化方案 P0 的规范成果。格式版本固定为 `2.0.0`，布局版本为整数 `2`；两者独立于软件发行版本。P1-01 的 [Go 记录核心](../../recorder/execution/README.md) 已实现 codec/root/writer；P1-02 的 [CLI/Shell 生命周期](../../integrations/bash/execution-v2.md) 已可用；业务事件/阶段、查询和真实 Run 接入仍待后续任务。旧 `spec/1.0.0`、旧命令、旧样例及历史结果保留原义。

P0-02 的公共函数、类型化参数、owner/失败状态与新旧分发已固定在 [Shell/CLI 接口规范](interfaces.md)；其中范例仍包含待 P1-03 实现的事件/阶段，P0 mock 检查不代表完整 P1 运行时验收。

P0-03 的 [共享检查与 P1/P2 实现位置](runtime-map.md) 固定后续模块、资源与依赖边界；新阶段沿用本契约，不重新选择格式。

[contract.schema.json](contract.schema.json) 是独立、无远程引用的 JSON Schema Draft 2020-12 资源；入口为 `$defs.root`、`$defs.run`、`$defs.event`，顶层接受这三种记录之一。[check.py](check.py) 补充 JSON 字节边界、时间和冲突语义，只用于离线契约验收。它不导入旧 recorder、Registry、context、Manifest、spool 或分析模块，不读取业务目录、登记身份、创建记录根或建立数据库。P1/P2 分别在自己的语言中实现本契约，复用本目录的共享向量；不把此检查器作为记录节点的 Python 依赖。

## 根与物理布局

部署设置中的 `SLEDRA_ROOT` 为独立绝对路径，不位于会被业务重建或清理的目录中。根的唯一配置文件名为 `sledra.json`，内容见 [root.json](cases/root.json)：

```json
{"format_version":"2.0.0","layout_version":2,"namespace":{"system":"mcv","deployment":"synthetic"},"partition_timezone":"+08:00"}
```

`system` 和 `deployment` 各为 1–64 个 ASCII 小写字母、数字、下划线、点或短横线，首字符为字母或数字。这对值限定所有 Execution 的查询语境，不从 task、目录名、环境变量或 Run 反推。一个根只承载一个命名空间；不同根即使 ID 或 cycle 相同也不能无条件合并。Run 自带同样的命名空间，必须与根一致才可参与该根的确定 Run 分组。

分区时区采用固定数值偏移 `±HH:MM`，范围 `-14:00` 至 `+14:00`；零偏移只写 `+00:00`。不依赖系统本地时区、IANA tzdata 或夏令时变化，配置建立后不可原地修改。时间展示默认用该固定偏移，查询比较使用 UTC 时间点。应用可先将自己的时区转换成带偏移的 cycle；记录器不推测业务日期。

```text
$SLEDRA_ROOT/sledra.json
$SLEDRA_ROOT/executions/YYYYMMDD/b/execution_id.jsonl
$SLEDRA_ROOT/runs/YYYYMMDD/run_id.json
```

这里 `b` 是 `SHA256(execution_id 的 UTF-8 字节)` 的首个小写十六进制字符，只有 `0`–`f` 的固定 16 桶，按需创建。Execution 日期由进入公共入口的时间按根分区时区计算，一次固定；后来补充 context、补跑历史 cycle、跨午夜结束都不换文件。开始事件丢失也不能用后续事件时间重定分区。Run 日期用 `created_at` 按相同偏移计算，不分桶。精确路径向量见 [layout.json](cases/layout.json)。

每个 Execution 仅一个 JSONL 主文件，无每执行 metadata、session、segment、open/sealed 协议、按 cycle 的副本或软链接。每行一个 UTF-8 JSON 对象，以单个 LF 结束；不写 BOM、空行、CRLF 或跨行 JSON。Run 与根是单 JSON 对象，可带尾部空白但计入大小限制。Run 文件不可覆盖为状态汇总。

P1 应在显式的新模式中校验/建立根配置；新旧根必须隔离。已存在的根缺少 `sledra.json`、存在旧布局、或版本/命名空间/时区不兼容时，必须有有界明确诊断，停止本次观测写入；不能自动把旧根“升级”或静默转入旧路径。新根的初始化及 Shell/CLI 分发细节由 P0-02 定案。这里不新增每任务配置、身份请求文件或初始化服务。

## 记录与公共生命周期

所有对象的核心字段闭合；业务扩展只进入业务事件的 `attributes`，不增加公共信封字段。

| 记录 | 必需字段与含义 |
|---|---|
| 事件信封 | `format_version`、`event_id`、`name`、`occurred_at`、`execution_id`、`attributes` |
| Run | `format_version`、`run_id`、`namespace`、`scope`、`created_at`、`cycle_kind`、`cycle`、`creator_execution_id` |

所有 ID（含 stage_id）是小写、带短横线的 RFC 4122 UUIDv4，版本位为 4、variant 为 8/9/a/b。由 owner 自动分配，重新进入任务公共入口必须新建 Execution；同进程重复 source、多个钩子或顺序业务命令沿用本次身份。PID、节点、try number、作业号都不是身份，用户不提供 token。样例中的 ID 全是合成值，不证明真实启动或 Run 创建。

事件名为点分的小写 ASCII 标识，各段首字符为字母、其余为字母/数字/下划线，总长最多 128。`execution.*`、`stage.*` 保留且仅接受下表名称；其他名字无需预登记 catalog、producer 或角色。`attributes` 始终为对象。

| name | attributes（除标为可选外均必需） |
|---|---|
| `execution.started` | `task`（已知逻辑名或 null）、`engine`（对象或 null） |
| `execution.context` | `task`、`parameters`、`scope`（字符串或 null）、`cycle_kind`、`cycle`（时间或 null）、`run_id`（UUID 或 null） |
| `execution.ended` | `exit_code` 或 `signal`，恰好一个；可选 `duration_ns` |
| `stage.started` | `stage`、`stage_id` |
| `stage.ended` | `stage`、`stage_id`、`exit_code` 或 `signal`；可选 `duration_ns` |
| 其他业务事件，如 `forecast.progress` | 开放 JSON 属性，受下述大小/数值/文本限制 |

task 最长 256 个字符，stage 最长 128 个字符，均非空并受 UTF-8 字节上限限制。engine 包含 `kind`（1–64 字符）、`service`（null 或 1–256 字符）、`node`（null 或 1–1024 字符），可选 `alias` 为布尔或 null。来源不明用 null，不能靠节点截断或名称猜 alias；P0–P2 的来源值全部为合成测试，不实现 ecFlow 识别。最终 task 可以比开始时更准确，开始字段只是入口观察，不覆盖最终 context。

parameters 的键为 ASCII snake_case（`[a-z][a-z0-9_]{0,63}`），值为 JSON 标量（字符串、数值、布尔或 null）；应用提供全部已生效的业务参数，不要求 Manifest 声明、默认值登记或 TaskInstance 身份。完整参数对象参与分组；`"25"` 与 `25`、参数缺失与值 null 都不同，分辨率只是其中一个参数。不能随意丢字段合并业务实例。

scope 是应用定义的稳定运行范围名称，1–128 个 ASCII 字母、数字、下划线、点或短横线，首字符为字母/数字。独立执行可以有 scope，也可以为 null；`run_id` 非空时 scope 必须非空。Run 有固定 scope 和 cycle 描述，只表示一轮创建事实，没有预期任务清单、结束覆盖或产品交付证明。P0 只定义 Run 格式，P2 只读取合成记录；P1 正常写入保持 `run_id:null`，不实现 begin_run、候选自动接受或 current_run 发布。

exit_code 为整数 0–255；signal 为整数 1–127，记录 owner 保存的业务结果，不能用外层错误 handler 的零退出替换。duration_ns 为 0–9223372036854775807 的整数，表示同 owner 可比单调时钟测得的时长，不填写墙钟估算值。查询用墙钟差估算时必须另标依据，允许负差，缺端点不补造时长。退出零只表示记录的进程结果。

## Cycle、时间、重传与冲突

| 最终 context 状态 | 编码 |
|---|---|
| 尚未确定或配置解析失败 | 没有 `execution.context`；禁止把它编码成 `cycle_kind:none` |
| 唯一业务时次 | `cycle_kind:single`，cycle 为完整带偏移时间 |
| 无业务时次 | `cycle_kind:none`，cycle 必须显式 null |
| 一次执行面向多个时次 | `cycle_kind:multiple`，cycle 必须显式 null |

时间为 Gregorian 年 0001–9999 的 RFC3339：`YYYY-MM-DDTHH:MM:SS`，可选 1–9 位小数秒，再接 `Z` 或 `±HH:MM`。必须大写 T/Z，有秒和确定偏移；拒绝无时区、`-00:00`（未知偏移）、闰秒、无效日期、offset 小时 >23 或分钟 >59。字段 `occurred_at`/`created_at`/`cycle` 比较时归一到 UTC 秒和纳秒，不截断到 Python 微秒。不会因分区时区不同改变 cycle 时间点；只给日期不自动补 00 时。

业务属性中的 `target_cycle` 若作为目标时次查询条件，遵守上述时间语法；普通开放属性校验不推测字符串含义。目标时次不替换执行主 cycle，multiple 不复制计数为多次执行。

同一个 Execution 只确定一次最终 context，缺少 context 允许其他事实独立可见。写入器应拒绝第二份不同 context 并保留第一份；查询若读到历史冲突必须保留所有源事实，将 task/parameters/cycle/Run 的确定分组标为不确定，不能按最后一条重归类。完全相同的 context（即使 event_id 不同）不形成第二个业务上下文。

event_id 标识一份事件事实。新事实新 ID；重试保持原 ID、发生时间和完整内容。比较忽略 JSON 空白、键顺序、转义写法，数字按精确十进制数值比较（1、1.0、1e0 相同；布尔不等于数字），已定义的时间字段按 UTC 纳秒比较；其他字符串按解码后的 Unicode scalar 序列精确比较，不做 NFC 或大小写归一化。该比较只用于冲突/分组，不生成全局 canonical 字节或摘要身份链。

同 ID 异完整内容是 `conflicting_event_id`；不同最终 context 是 `conflicting_context`；多份不同结束 attributes 是 `conflicting_end`（包括结果或声明时长不一致）。这些事实各自可能格式合法，冲突不是丢弃源记录的理由。相同结束 attributes 不冲突；同名阶段可用不同自动 stage_id 多次开始/结束，配对使用 execution_id+stage_id，缺端点显示未知。时间排序不构成重试父链或“最终成功”选择。

Run 相同 ID 同完整内容是重传，异内容是 `conflicting_run_id`，不得覆盖。仅 ID 被引用但 Run 不存在时，Execution 仍合法可见，查询显示缺失引用；Run 冲突或命名空间/scope 不一致不能作为确定关联依据。真实 Run 来源/新鲜度不能由格式验证证明；其接受矩阵及来源校验仍按设计，在 P4 实现。

## JSON 字节与资源边界

| 项目 | 2.0.0 固定限制 |
|---|---|
| 单事件/Run/根配置 | 最多 65536 字节；事件不含单个终止 LF，其余空白计入 |
| 每个解码后字符串 | 最多 4096 UTF-8 字节；字段字符上限可更小 |
| 每个对象键 | 最多 128 UTF-8 字节 |
| 每个对象/数组 | 最多 128 成员/元素 |
| 容器深度 | 最多 16，顶层对象为第 1 层；标量不增加层数 |
| JSON 整数字面量 | 有符号 64 位，-9223372036854775808 至 9223372036854775807；不能先经 float64 |
| 带小数点/指数的数值 | 合法 JSON 十进制，binary64 范围内有限值，非零下溢为零时拒绝；保存/比较时保留十进制精确值 |

小数允许常见有限浮点属性，如 0.125、-2.5e-3；禁止 NaN、Infinity、-Infinity、溢出和非零下溢。整数字面量超 64 位明确拒绝，不静默转 float 或字符串；64 位内大于 2^53 的整数精确接受。`9007199254740993.0` 也须精确保留，不可变成 `9007199254740992`；Go 使用 json.Number、Python 使用 Decimal 或等价方法，只有范围检查/明确的数值运算可转 binary64。数值 -0 与 0 同值。公共 exit/signal/duration 字段必须是整数字面量，不能用小数/指数或布尔绕过类型。

仅接受 UTF-8，无 BOM；有效 surrogate 转义对解码为一个 scalar，孤立 surrogate、非法 UTF-8 和 NUL（包括键及转义形式）拒绝。其他转义控制字符作为有界业务值允许；事件中的物理 LF 仅用作分隔符。重复对象键在任何层拒绝，包含 `"a"` 和 `"\u0061"` 的解码重复；不依赖解析器的 last-wins。不同 Unicode 规范形式不自动合并，因此不引入归一化后的隐藏键覆盖。

JSON Schema 无法独立验证的 UTF-8 字节数、重复键、整数词法、精确十进制、时间及冲突必须由实现补充。Schema 接受不代表整个契约接受。基础错误分类为 `invalid_json`、`duplicate_key`、`invalid_unicode`、`invalid_number`、`number_out_of_range`、`limit_exceeded`、`schema_invalid`、`invalid_time`、`invalid_timezone`、`unsupported_version`；检查器另有 `incomplete_tail`、`execution_mismatch`、`input_unavailable`。

第一版 writer 只支持同一 owner 顺序追加。短写/可能留下半行后禁止本 Execution 后续追加；不原地修复。reader 只接受完整 LF 行，尾部半行单列未完成、下次重读，不制造结束事实。以上是 P1/P2 的实现约束，此处的 stream 检查是共享样例的严格 oracle，不是生产流式 reader，也不设置整份历史文件的 64 KiB 上限。

## 共享样例与验证

[cases.json](cases/cases.json) 列出单记录接受/拒绝和合法记录间的冲突预期；[test_contract.py](test_contract.py) 执行目录向量、完整生命周期（两次同名阶段）、未确定/none/multiple、浮点/大整数、Unicode、重传/冲突以及动态边界检查。它们位于旧保护脚本的 `spec/fixtures` 范围之外，不改旧冻结资源。

在 Sledra 根执行（已有开发环境的 jsonschema 4.10.3，无网络）：

```bash
uv run --locked --offline --no-python-downloads python spec/execution-v2/verify.py
uv run --locked --offline --no-python-downloads python -m unittest discover -s spec/execution-v2 -v
uv run --locked --offline --no-python-downloads python spec/execution-v2/check.py stream spec/execution-v2/cases/lifecycle.jsonl
uv run --locked --offline --no-python-downloads python spec/execution-v2/check.py run spec/execution-v2/cases/run-single.json
```

单记录命令退出 0 表示通过、1 表示拒绝，诊断不回显原始值。stream 中的格式错误退出 1；格式合法的冲突流退出 0 并在 `conflicts` 明列冲突，不代表确定的业务分组可用。P0 的 Python 参考校验通过不能宣称跨语言实际读写已验收；Go 编码在 P1、Python 直接查询和跨语言闭环在 P2 验证。

verify.py 为根 make check/test 共用的 P0 检查入口，还运行 schema/样例/接口的一致性检查。P0-03 为 ASCII 标识增加显式 CR/LF 排除，防止正则 `$` 对尾部 LF 的宽松匹配；业务属性的合法转义换行不受影响。原 45 项共享样例保留，追加 8 项后共 53 项，格式/布局版本不变。
