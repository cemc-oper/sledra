# Execution 2.0.0 Shell/CLI 接口

这是 P0-02 固定、经 P0-03 一致性核对的接口规范，与 [记录契约](README.md) 共用格式 `2.0.0`、布局 `2`。P1-01 至 P1-03 已实现 writer、CLI init-root/prepare 六种 KIND/append 和全部六个公共 Bash 函数；P1-04 的 [独立示例/真实业务对照](../../examples/execution/README.md) 已落实，阶段命令/结果及未验证范围见原实施计划。已交付部分的用法见 [生命周期说明](../../integrations/bash/execution-v2.md)，位置见 [实现映射](runtime-map.md)；不能将本文件全部接口或 P5 发行归档视为已交付。

普通脚本只需部署好的 Go 二进制、Bash 库和独立记录根。公共入口调用一次 begin，应用解析实际参数后调用 context，再按需写事件/阶段/结束。用户不生成 execution_id、event_id、stage_id、token 或请求 JSON，不配置 producer/session，不需要 Python、jq、ecFlow、takflow 或后台服务。

## 模式、安装与根配置

新库的固定源码位置为 `integrations/bash/sledra-execution.sh`（P1-02/P1-03 已实现），原 `integrations/bash/sledra.sh` 继续服务旧接口。两个入口明确分开；不把旧函数换成新版语义，也不因旧配置缺失而自动切换。

| 设置 | 新接口规则 |
|---|---|
| `SLEDRA_MODE` | 未设置、空或 `off` 关闭；`execution-v2` 启用；其他值诊断并关闭本次观测 |
| `SLEDRA_BIN` | 启用时为可执行 Go 二进制的绝对路径；不搜索 PATH 来猜安装位置 |
| `SLEDRA_ROOT` | 启用时为已有、兼容的绝对记录根，含 `sledra.json` |
| `SLEDRA_ENABLED` | 只保留旧版启用语义；新模式与 `SLEDRA_ENABLED=1` 同时出现视为歧义，关闭本次新版观测，不启动双写 |
| 旧 `SLEDRA_CONFIG` / `SLEDRA_SPEC_DIR` / context / token / Run 环境变量 | 新库不读取、不导入、不清理；存在也不成为新版前提或候选身份 |

source 新库只加载定义，不开始业务、不写记录、不改变 options/trap，也不重置当前 owner。模式、二进制路径和根在第一次 begin 冻结，后续环境修改不重新激活/转根/切模式；换配置须是一次新的作业入口。默认关闭不产生诊断、文件或 CLI 调用。

部署者一次性使用以下命令准备根（P1-02 已实现）；这不是每任务身份登记：

```bash
/opt/sledra/bin/sledra execution init-root \
    --root /shared/observability/mcv-demo \
    --system mcv --deployment demo --partition-timezone=+08:00
```

init-root 只建立契约规定的 `sledra.json`；允许不存在或空的根，根配置不可覆盖。已有相同配置为幂等成功，已有不同配置/非空且无标记/旧布局则拒绝，不合并、不迁移、不运行清理。初始化发布中的中间文件仅用于根配置的原子建立，不能扩展成每 Execution metadata 或登记请求。正常 begin 不自动“收养”缺少标记的既有根；初始化竞争/结果不确定由部署者核实，任务不因此重跑业务。

新 CLI 以首层 `execution` 子命令显式分发；不得先调用旧 `context.New`、加载旧父引用或创建 Registry 后再分发。旧 `registry`、`context validate`、`event`、`run finish` 仍是旧语义，不能用 SLEDRA_MODE 将其解释为新版。P1 应拒绝旧写命令写入带新版根标记的根，且新版根拒绝混入已知旧布局；这是根隔离，不改变合法旧根的命令含义。帮助/版本查看不要求根或旧配置。

## 公共 Bash 函数

最低 Bash 版本为 4.3。所有观测函数向业务 owner 返回 **0**，stdout 为空。故障只影响观测；库不启动、重试或重提业务命令，不调用业务 callback。脚本仍在自己的正常控制流中执行原命令，先保存 `$?`、`PIPESTATUS` 或信号，再调用 hook。

| 函数 | 签名与结果 |
|---|---|
| `sledra_execution_begin` | `[--task TASK] [ENGINE_OPTIONS]`；公共脚本入口在业务配置解析前调用；自动产生一个 Execution 和开始事件 |
| `sledra_execution_context` | `--task TASK --cycle-kind KIND [--cycle TIME] [--scope SCOPE] [TYPED_VALUES]`；一次最终上下文，参数来自实际生效值，`run_id:null` |
| `sledra_execution_event` | `NAME [TYPED_VALUES]`；每次调用是一个新业务事实，自动分配 event_id/occurred_at；保留名不能从该入口伪造 |
| `sledra_stage_begin` | `NAME`；自动生成阶段区间 ID，owner 保存；同名可多次顺序开始 |
| `sledra_stage_end` | `--exit-code N` 或 `--signal N`；使用本 owner 当前阶段，不要求调用方传 ID/阶段请求文件 |
| `sledra_execution_end` | `--exit-code N` 或 `--signal N`；记录入口 owner 保存的真实结果，相同结果重复调用不追加，不同结果拒绝并保留首份 |

没有隐式 begin：未进入公共入口的子命令或事件不能自己创建 Execution。未 begin 时调用其他 hook 不写文件，关闭模式静默，其他情况最多一次诊断，不占用随后真正入口的创建点。begin 后配置失败允许只有 started/ended，没有 context。已知初始 task 可省略而编码为 null，最终 task 则必需。context 必须显式指定 single/none/multiple；single 必须有 cycle，none/multiple 禁止 --cycle，不默认补日期或 00 时。未传 scope 为 null；公共接口在 P1 不接受 --run-id，不读取继承 Run 候选。

ENGINE_OPTIONS 为 `--engine-kind KIND`、`--engine-service SERVICE`、`--engine-node NODE`、`--engine-alias true|false`。完全省略表示 engine:null；使用任一项时 kind 必需，缺少 service/node 分别为 null，未传 alias 则省略。这些是应用显式提供的来源，不读取 ECF_NAME 等变量猜来源；P0–P2 示例不使用真实 ecFlow 识别。

TYPED_VALUES 反复使用下面的选项，每次以独立 argv 传递，不能拼成 Shell 命令或 JSON 字符串：

| 选项 | 类型规则 |
|---|---|
| `--string KEY VALUE` | 原样字符串，包括空字符串；正确引用保留空格、换行、Unicode、美元符号和命令替换字面文本 |
| `--int KEY VALUE` | JSON 整数字面量及 signed int64 边界，与记录契约一致 |
| `--float KEY VALUE` | 含小数点或指数的 JSON 数值，有限范围、精确十进制保留；不接受 NaN/Infinity |
| `--bool KEY true\|false` | JSON 布尔，大小写精确匹配 |
| `--null KEY` | 显式 JSON null，区别于缺少该参数 |

context 中这些值进入 parameters，业务事件中进入 attributes。重复 KEY 即使跨不同类型选项也拒绝，不采用 last-wins。未知选项、重复单值选项、缺值、多余位置参数均拒绝；负数值按所在选项的固定参数位置消费，不当作另一个 option。首版 Shell 支持标量属性；开放 JSON 契约中的嵌套对象/数组仍可读取，不因此增加 JSON 请求文件、任意代码求值或一套通用对象构造 DSL。

结束字段沿用契约范围，exit_code 0–255、signal 1–127，恰好一个。第一版不自动填 duration_ns：独立 Go CLI 进程的 time.Now 单调时钟部分不能直接序列化后互减。后续查询可用完整墙钟端点提供带依据的估算，不把未知耗时补为零。

第一版 owner 同时只持有一个未结束阶段，不支持嵌套或并行 stage。活动阶段上再次 begin、无活动阶段 end 均为调用拒绝；重复同名阶段必须先 end 再 begin。execution_end 不补造任何缺失的 stage.ended，配置失败或强杀也不补造 context/结束。

## owner 状态与继承边界

公共 begin 是每次真正进入任务脚本的唯一创建点，不能放在每个子命令或函数 hook 内。状态仅保存在 owner 的未导出 Bash 变量/数组中，前缀 `_SLEDRA_V2_`，不写状态文件，不从每条事件反向加载旧上下文。

| 状态/调用 | 必需行为 |
|---|---|
| 第一次 begin，模式关闭 | 冻结 off；后续调用静默 no-op，不通过改变环境在同一次执行中重新启用 |
| 第一次 begin，启用成功 | 冻结 owner BASHPID、根、二进制、execution_id 和入口时间，写一个 started，进入 active |
| 第一次 begin 失败，包括输入拒绝 | 锁定 disabled；即使尚未分配 ID，也不允许同一次入口稍后 begin 再分配身份或补写一个较晚的开始 |
| 重复 source / 重复 begin | 保留当前状态；active/closed/disabled/off 都不新建、不再写 started，也不依据新参数重置 |
| active → 第一次合法 context | 成功后缓存完整 context 事件；相同属性重传 no-op；不同属性诊断并永久禁写，原 context 保留 |
| active 中的单条非法/超长输入 | 确认尚未接触执行文件时拒绝本条，保留 active；不改变已接受 context 或活动 stage，调用方可用合法值继续 |
| active → 成功 end | 缓存结束事实，关闭写入；相同结束为幂等，不同结束诊断，不追加任何后续业务事实 |
| 根/配置/工具失败，短写、I/O 结果不确定、回执异常、子 CLI 异常退出 | 进入 disabled；本 owner 后续任何 hook 不调用 append，不尝试修复或新建 ID；新的真实脚本启动才能重新尝试 |
| 同一脚本再次执行或正常新 Bash 公共入口 | 清除继承身份，生成新 ID；脚本路径、try number 或相同 cycle 都不构成复用理由 |

owner 不能只信任继承的 execution_id 或一个环境布尔值。新库初始化时必须识别并丢弃带 export 属性的内部状态；已加载进程内的状态只有在未导出且 owner BASHPID 匹配时才可复用。这样包括 `exec bash ...` PID 不变但环境继承旧 ID 的情况，也不能冒充原 owner。外部脚本直接改写私有变量不属于受支持 API；任何公共 SLEDRA_EXECUTION_ID/context/token 值都不是本次身份来源。

`( ... )`、命令替换、pipeline 中的 Bash 副本不拥有父进程写权：BASH_SUBSHELL 非零或 BASHPID 与当前 owner 不符时，hook 静默 no-op，不创建新 Execution、不写父文件。所有支持的记录调用回到 owner 顺序执行。独立 `bash child.sh` 若显式进入自己的公共入口则是另一 Execution；普通业务子进程不会自动建身份。首版不导出函数/私有状态为通用子进程记录服务，也不声称支持同文件多写者。

## Go CLI 与 Shell 之间的小型内部接口

以下协议固定 P1 所需边界，不是用户逐条手工记录的教程。CLI 不驻留服务。准备和追加分开，使 owner 能在写入前保留一份完整事实；没有每事件请求文件或分段 spool。

```text
sledra execution prepare KIND [OPTIONS]
sledra execution append --root ROOT --execution-id UUID --entry-time TIME [--create]
```

KIND 仅为 `start`、`context`、`event`、`stage-start`、`stage-end`、`end`。prepare 只分配/编码/校验，不写根/执行文件或查询其他运行；不知道 Run 或业务 cycle 也能构造 start。start 使用 begin 的参数并自动分配 execution_id；其余种类要求 Shell 内部传 `--execution-id`。context/event 沿用公共函数参数；stage-start 使用 `--stage NAME` 并自动产生 stage_id；stage-end 由 owner 补 `--stage NAME --stage-id UUID`；end 与 stage-end 接受结果参数。所有 ID 都由内部填充，业务方不传。

prepare 成功 stdout 恰好一个 LF 终止的三字段 TSV 回执：

```text
execution_id<TAB>auxiliary<TAB>event_json<LF>
```

start 的 auxiliary 是该事件 occurred_at（UTC/Z 的入口时间）；stage-start 是新 stage_id；其他为单个 `-`。所有字段非空，event_json 是已经校验的单行 UTF-8 JSON，控制字符转义后无原始 TAB/CR/LF。整份回执（含 LF）最多 65792 字节，其中 JSON 仍受 65536 字节边界；固定前缀 ID/时间格式必须检查，异常或多行回执禁写。Shell 用固定分隔符的 read/数组解析，**禁止 eval/source CLI 输出**，不解析任意 JSON 键为 Shell 代码。

owner 在内存中缓存入口时间/ID、当前待写 event_json、至多一份已接受 context 事件、一份结束事件和活动阶段名/ID；这就是全部必要状态，不引入 session、摘要身份图、Manifest 或额外状态文件。后续 context/end prepare 可由库内部传 `--previous-event JSON`：CLI 按记录契约比较相关 attributes，相同则返回原事件（原 ID/时间/内容），不同分别返回 conflicting_context/conflicting_end。previous-event 的 execution_id/name 必须匹配当前调用；不能作为通用覆盖字段或修订接口。公共调用方不提供该参数。

append 从 stdin 接收且只接收一条 `event_json + LF`，有界读取并校验；Shell 用 here-string 或等价的受控输入传递，不把业务命令放进 pipeline/命令替换。追加路径仅由冻结的 root/entry-time/execution-id 计算，核对事件的 execution_id 与参数一致。`--create` 仅用于 start，以不可覆盖创建保证 ID 冲突不能截断旧文件；之后只打开已存在文件追加，不自动重建丢失文件。检查坏尾部时只需读取当前文件尾部，不修复、不扫描整个根。prepare/append 都不加载旧父 context 或要求 producer 配置。

| CLI 退出码 | stdout/stderr 与 owner 处理 |
|---|---|
| 0 | prepare 为上述回执；append/init-root 成功 stdout 空。append 表示完整 LF 行写入并成功 close，不宣称电源故障持久性或 fsync 保证 |
| 2 | usage、参数/单条编码校验拒绝，保证没有改变执行文件；stdout 空，stderr 一条不含输入的 `{"ok":false,"code":"..."}`，最多 256 字节。owner 保留此前合法状态 |
| 3 | 根不兼容/不可用、context/end 冲突、I/O/短写/close 失败、坏尾部或结果不确定等终止观测；stdout 空，同样有界诊断。owner 永久禁写；未知非零退出/信号也按此处理 |

context 冲突即使发生于写入前仍使用 3，因为继续按旧 context 写新业务事实可能误分类；原事实不被覆盖。结束冲突保持 closed，同样不再追加。准备阶段的单条输入拒绝不自动污染文件；append 的任何 I/O 错误均保守禁写，不能只在某次 Go 进程内设置 poisoned 标志。

Shell 必须在自身进程更新 disabled/closed 状态，再返回业务控制流。正常钩子无 stdout，子 CLI stderr 被库接收/抑制，由库向 owner stderr 最多输出一次固定诊断 `{"ok":false,"code":"execution_observation_unavailable"}`（不含参数、路径、环境或原始事件），不把观测错误改成业务 rc。最多一次诊断不代表写入只有一次失败，也不保证内核阻塞的硬延迟上限。

第一版不自动重试可能写入过的事件：保留 pending 后禁写，比拼接半行安全。相同事实若在已证明安全的编码重试/测试中重传，必须重用缓存的完整事件，不重新调用 start/event 分配 ID；相同完整行重复的读取去重由 P2 负责。不提供以重新执行业务来“补观测”的路径。每次独立业务 event 调用是新事实，不能靠内容相似自动合并。

## 独立脚本与 callback 边界

[standalone.sh](interface-examples/standalone.sh) 是可做 Bash 语法与控制流检查的接口范例，部署后只需源库路径、SLEDRA_MODE/BIN/ROOT。它不生成身份、请求 JSON 或 producer 配置；其中合成业务 cycle/分辨率只作说明，不代表 MCV 接入。**P1-03 已交付范例所需的全部 API；此文件仍作为 P0 规范范例，其 mock 结果保留原义。P1-04 已另行交付真实独立示例与业务/故障对照，不改写本范例或 mock 的历史结果。**

范例 source 后立即 begin，再执行配置检查。业务命令在顶层原控制流执行，不能变成 `if run_business`、`run_business || ...`、`$(run_business)` 或交给 Sledra launcher。退出 handler 首先保存原 rc，signal handler 保存实际信号；库不安装 trap。已有 ecFlow callback 继续由业务 owner 按原顺序调用，允许外层仍 exit 0，但传给 end 的必须是此前保存的真实业务 rc/signal。

范例的 TERM handler 自己选择退出 143，并记录信号 15；这是该范例的既有 owner 策略，不宣称通用信号转发/进程组/MPI 支持。KILL 无法执行 hook，只能留下缺结束事实。未进入公共入口的安装/source 失败没有 Execution；部署库本身须存在，不能把该范围冒充已观测。P1 的缺二进制/无权限等观测失败仍须保留业务执行基线。

## begin_run 保留边界

未来受控入口函数名保留为 `sledra_begin_run --scope SCOPE --cycle-kind KIND [--cycle TIME]`，仅 P4 实现。它必须在最终 context 前完成创建结果判定；同 Execution 重入复用已分配 Run ID，新 Execution 重新经过创建点形成新 Run。成功时后续 context 关联自建 Run，失败时允许独立 context；不得先确认无 Run 再补第二份 context。

普通 alias 无创建职责，Run 接受还需可靠来源、命名空间/scope/cycle 适用性及旧值失效证据。P1 不提供成功空壳、不发布 current_run、不开放 --run-id 规避校验；P2 的 Run/alias 仅用合成样例。这个保留名称不是允许在当前范例调用它。

## P0 检查与后续验收

运行 `uv run --locked --offline --no-python-downloads python spec/execution-v2/verify.py`。其中 [test_interfaces.py](test_interfaces.py) 用临时 Bash mock 检查范例的调用参数、配置前 begin、正常/非零退出、配置失败、TERM、原 argv/输出/callback 及单次业务调用；故障 mock 仅兑现“hook 返回 0”这项接口前提，不实现身份、上下文、禁写或 JSONL。[一致性检查](test_consistency.py) 将实际范例调用参数与合法的合成事件对照，不把合成流当作生产输出。

| 场景 | P0-02 定案；实际运行时验收位置 |
|---|---|
| source 两次、begin 两次、继承旧 ID、exec 后 PID 相同 | 上述 owner 规则已定；P1-02/P1-04 验证真实库和不同文件身份 |
| context 相同/不同、结束幂等、闭合后事件 | 上述状态与 previous-event 比较已定；P1-02 验证真实 CLI/库 |
| 阶段重复、非法/超长属性 | 上述签名/类型与单活动阶段已定；P1-03 验证实际编码和配对 |
| 短写、不确定写、坏尾部及后续 CLI | owner 禁写与错误码已定；P1-01/P1-04 故障注入，不能以 mock 范例通过代替 |
| 新旧模式、根隔离、不进入旧机制 | 分发位置与旧基线已定；P0-03 核对实现清单，P1 验证真实调用链 |

旧基线继续是旧命令参数/返回/输出、`integrations/bash/sledra.sh` 的 options/trap/业务执行所有权、已有 M1–M3 结果和 72 个受保护资源；当前不重写旧脚本、生成结果或业务回调来适配新版。本任务不提前实现 P1，不以当前旧 CLI 不识别新命令为阻塞。
