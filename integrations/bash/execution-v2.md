# 独立 Execution 记录 API（P1-02/P1-03）

源码入口为 [sledra-execution.sh](sledra-execution.sh)，使用 Bash 4.3 及以上版本和编译后的 `sledra` Go 二进制。只需这两项及已初始化的独立记录根；记录节点不需要 Python、jq、ecFlow 或旧 producer/context 配置。

当前已实现全部六个公共函数，以及 CLI init-root、prepare 六种 KIND 和 append。P1-04 的 [可执行独立示例](../../examples/execution/README.md) 已提供真实业务/故障/信号对照；[P0 接口范例](../../spec/execution-v2/interface-examples/standalone.sh) 保留为规范范例，P0 mock 结果保留原义，P1 阶段证据由原实施计划汇总。全部签名和错误语义沿用 [2.0.0 冻结接口](../../spec/execution-v2/interfaces.md)。

## 部署和启用

管理侧先用本仓库 recorder 的构建入口生成二进制。选择业务清理目录之外的绝对根，一次性初始化，例如：

```bash
/opt/sledra/bin/sledra execution init-root \
    --root /shared/observability/example \
    --system synthetic --deployment local --partition-timezone=+08:00
```

该命令只建立不可覆盖的 sledra.json；相同设置幂等，不同设置、混合旧布局或非空无标记目录被拒绝。部署路径只是说明，本轮验证均用临时目录。本次不调整或验收 P5 试用归档，使用前须由部署者提供本次构建的二进制和源码库。

业务 owner 的最小调用如下；实际业务仍在原位置运行，结果由 owner 自行保存：

```bash
export SLEDRA_MODE=execution-v2
export SLEDRA_BIN=/opt/sledra/bin/sledra
export SLEDRA_ROOT=/shared/observability/example
source /opt/sledra/share/sledra/integrations/bash/sledra-execution.sh
sledra_execution_begin --task configuration

# 应用在这里解析并使用自己的实际参数。
sledra_execution_context --task configuration --cycle-kind none \
    --string resolution 25km --int members 1 --bool optional false

# 这里演示由 owner 已保存的真实结果，不运行或重试任何业务命令。
business_rc=0
sledra_execution_end --exit-code "$business_rc"
```

有业务时次时用 `--cycle-kind single --cycle TIME`；处理多时次时显式用 `multiple`。`none`/`multiple` 不传 cycle；未记录 context 就是尚未确定。参数支持重复的 --string/--int/--float/--bool/--null，空字符串和 null 不同，数字按契约精确保留。参数键重复或选项非法时拒绝本条；不接受 --run-id、用户指定的 execution_id 或 previous-event。

## 业务事件和顺序阶段

公共函数为 `sledra_execution_event NAME [TYPED_VALUES]`、`sledra_stage_begin NAME`、`sledra_stage_end --exit-code N` 或 `--signal N`。在同一个已 begin 的 owner 中使用，例如：

```bash
sledra_stage_begin forecast
# 原业务命令留在自己的控制流；owner 先保存其真实结果。
sledra_execution_event forecast.progress \
    --int step 9007199254740993 --float fraction 0.10000000000000001 \
    --bool ready false --null optional --string message '预报进度'
sledra_stage_end --exit-code "$business_rc"

# 同名阶段可在上一段结束后再次开始，自动生成不同 stage_id。
sledra_stage_begin forecast
sledra_stage_end --signal 15
```

业务事件使用同一 typed 参数编码器，值直接进入 attributes；属性键按 JSON 键边界检查，不套用 context 参数键的命名规则。`execution.*` 和 `stage.*` 是保留名称，不能从业务 event 入口伪造。每次公共 event 调用都是新事实，即使名称和属性完全相同，也分配新 event_id。

同一 owner 只有一个活动阶段，不支持嵌套/并行。已有阶段再次 begin、没有活动阶段就 end 均拒绝；非法结果或超长输入不会替换已接受的阶段/上下文。阶段名/ID 在 start 追加成功后保存，end 追加成功后清除，重复 source 不丢失它们。结束只接受 exit_code 0–255 或 signal 1–127，恰好一个，不补 duration_ns；execution_end 不补造未闭合的 stage.ended，closed 后阶段/业务钩子无写入。

内部命令的业务名称紧随 KIND：`execution prepare event NAME --execution-id UUID [TYPED_VALUES]`；stage-start 使用 `--stage NAME` 自动分配 ID，stage-end 由 owner 传入已保存的 `--stage NAME --stage-id UUID` 和结果。stage-start 的 TSV auxiliary 为新 stage_id，其他已有字段和限额不变。公共调用方不提供这些 ID、previous-event、请求 JSON 或重试参数。

owner 保留完整 pending 事件与其 auxiliary。测试在已知成功写入后重传缓存 bytes，证明同一事实保留 ID/时间/内容；再次调用公共 event 会分配新事实。真实故障一旦可能写入就禁写，保留 pending，不自动重试或重新 prepare；不要把测试中的安全重传当作故障恢复入口。

## owner 与失败处理

source 只加载函数定义，begin 才冻结设置并建立一次 Execution；重复 source/begin 不重置。默认模式关闭时无文件、CLI 调用或诊断；与 `SLEDRA_ENABLED=1` 同时启用则禁用新版并诊断。设置在首次 begin 冻结，修改环境不会重启或转根。

私有 `_SLEDRA_V2_` 变量仅存于当前 BASHPID，均去除 export 属性，兼容调用方 set -a。继承的导出状态会丢弃，包含 PID 不变的 exec bash；正常新 Bash begin 生成新 ID。子 Shell、命令替换及 pipeline 副本不拥有写权，静默 no-op。公共函数返回 0、stdout 空，不安装 trap 或改变 shell options。

context 成功后保存完整事件；相同参数/数值/时间点重传不追加，不同 context 永久禁写。end 成功后关闭，同一结果幂等，不同结果诊断并保留首份。end 不补 context，也不补造缺失阶段。owner 必须先保存真实业务退出码、PIPESTATUS 或信号，再调用 hook；库不执行 callback。

prepare 的单条预写校验失败保留 active。根/工具故障、坏尾、短写、close 或结果不确定、异常退出/回执会保守禁写；后续 hook 不再追加或重建文件。一次 begin 的输入失败同样锁定 disabled。诊断每个 owner 最多一条固定 JSON，不含参数或路径；诊断输出本身仍可能失败。

内部 prepare 回执最多 65792 字节，Shell 用有界 read 和固定 TSV 解析，保留并检查最终 LF，拒绝 NUL/多行/额外字段，不执行返回文本。append 只接收一条 JSON+LF，JSON 本体上限 65536 字节。正常追加检查 close 成功；不重试可能写入的事实，不宣称断电持久性或任意文件系统故障下的硬时延界限。

旧 [sledra.sh](sledra.sh) 及旧命令保留原语义。新入口在构造旧服务前分发；旧命令拒绝带 sledra.json 的根，旧 event 仍保留默认 best-effort/--strict 的返回策略。新路径只写 sledra.json 与 Execution JSONL，无登记、session、metadata、Run 创建或数据库。

## 验证入口

[真实 Bash/Go 测试](../../tests/shell/test_execution.py) 编译临时二进制并在临时根运行，使用 P0 校验器断言实际记录，不依赖 P2 reader。根 make test 已纳入它；单独执行：

```bash
uv run --locked --offline --no-python-downloads python -m unittest discover -s tests/shell -p 'test_execution*.py' -v
```

运行测试的开发环境需要 Go、Python 及现有 jsonschema 依赖；这不是记录节点依赖。P1-02/P1-03 已覆盖 owner 生命周期、业务事件/阶段、记录边界及跨 CLI 禁写，P1-04 增加了实际示例的命令次数/argv/产物/callback、TERM/KILL 和观测故障对照；HPC/真实工作流及 P5 归档仍未验收。

## CE-1 同机子进程事件

默认调用继续使用上面的单 owner 路径。首次 `sledra_execution_begin --child-events [原 start 参数]` 显式选择协调路径。CE-1 为 Bash 接口版本，内部控制协议为 `execution-child-events` 版本 1，记录格式为 Execution 2.0.0、布局版本为 2。相对于原单 owner 接口，CE-1 增加 opt-in、同机引用和三个事件钩子。owner 独占 context/stage/end，原 JSONL 格式和 reader 兼容规则保持原义。[接口契约](../../spec/execution-child-events/interfaces.md) · [完整父子示例](../../examples/execution-child-events/README.md)。

部署者须提供绝对 `SLEDRA_RUNTIME_DIR`，预建当前 UID 的 0700 目录，确认显式选定的本地或共享 FS 通过实际用法所需的原子替换和非阻塞锁验收，祖先可信，且位于记录根、业务和清理树之外。库不推断 TMPDIR，不创建缺失 runtime，不退回旧 append。CE 模式要求同一安装中的 `sledra-execution.sh`、相邻的 `sledra-events.sh` 和通过精确握手的 Linux 二进制。运行依赖为 Bash 4.3+、Linux procfs 和记录器。已完成独立试用安装和混装回归，目标平台范围见[支持矩阵](../../docs/source/reference/compatibility/support.md)。

```bash
source /absolute/install/sledra-execution.sh
sledra_execution_begin --child-events --task synthetic.initial
# 只有完整 started 和控制发布成功后才 export SLEDRA_EVENT_CONTEXT。
sledra_execution_context --task synthetic.initial --cycle-kind none
(
    source /absolute/install/sledra-events.sh
    sledra_event_attach
    # 原业务留在原控制流；在实际事实发生处调用。
    sledra_event_emit data.ready --int lead 0
    sledra_event_detach
)
# 独立 Bash 必须自行 source/attach，不依赖导出的函数。
# owner 先保存业务退出码或信号，再使用原 end 签名。
sledra_execution_end --exit-code 0
```

事件库只定义函数，重复 source 不重置绑定。`sledra_event_attach [--context REF]` 不传选项时用环境引用，显式空值不回退；无引用、off、未 attach 的 emit 静默且不调用 CLI。首次 attach 冻结当前 BASHPID 的 mode/bin，缺引用允许稍后显式 attach。重复相同 attach 再校验、健康时幂等；不同引用拒绝切换，需先无参数 detach。detach 只清当前绑定，不结束父执行、不删文件；控制不可用或不确定结果引起的本进程 disabled 不能经 detach/source/换环境恢复。

`sledra_event_emit NAME [TYPED_VALUES]`沿用 typed 编码和精确数值规则，只允许业务事件；`execution.*`/`stage.*` 及身份/role/raw JSON 选项拒绝本条。inline 子 shell 必须显式 attach，旧 lifecycle hook 保持子 shell no-op；external 自行加载库。所有私有 `_SLEDRA_V2_`/`_SLEDRA_CE_` 状态去除 export 属性，即使 set -a；继承的私有导出状态丢弃，BASHPID 不同不能使用继承绑定。

CE owner 的 context/event/stage/end 全部走协调层。相同 context 幂等、冲突禁写；阶段顺序规则保留；首次合法 end 缓存在 owner 并发布 closing。Busy 不重发普通事件，只有后续显式相同 end 可以在协议允许时使用首次 prepared 再争取锁。相同已提交 end 幂等，不同结果保留首份。

每份普通事实只 prepare 一次、commit 一次。损坏/缺失/CLI 信号退出的 commit 回执触发一次只读 result；不能证明时调用一次 disable 并永久停止本进程，不补录、不修复。正常 off、缺引用、Busy 和正常 closed 不诊断；其他失败共享 diagnostic O_EXCL 预算，健康控制目录每 Execution 至多一行固定 JSON，无法使用共享状态时退化为每 BASHPID 至多一行。诊断不含路径、引用、key、typed 值或凭证。公共 hook 返回 0、stdout 空，保留调用方 options/trap；xtrace 仅在内部观测调用期间屏蔽并恢复，调用方自己写出的敏感 hook 参数仍受 Bash 原始跟踪规则约束。
