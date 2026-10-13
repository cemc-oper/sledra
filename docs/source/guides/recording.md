# 在脚本中接入记录

当前 Sledra 由显式 owner 调用。先读[独立示例详解](standalone.md)理解完整控制流，再按本页将相同边界接入自己的本地脚本。Sledra 不自动扫描任务、不替你启动业务，也尚未向 takflow 生成模板注入钩子。

## 准备接入需要的材料

| 材料 | 谁提供、怎样使用 |
|---|---|
| 生命周期 owner | 明确谁发起一次 Run、启动任务尝试、接收退出码、声明结束；在该控制流中插入钩子 |
| DefinitionManifest | 显式定义 task key、参数类型/默认值、stage 和依赖；Run scope、Attempt 参数都引用它 |
| Run 请求 | 业务 System/部署/平台/周期、冻结任务 scope、发起令牌，及 definition 的绝对路径 |
| Attempt 请求 | scope 内的 task key/参数、本次 execution token、process role、可选 retry_of |
| producer 配置 | 记录端的名称/版本/角色/source/session；并发 writer 分配不同 session |
| 数据与 context 路径 | 已有可信的绝对 `SLEDRA_ROOT`，以及每个 Run/Attempt 固定且独立的 context 路径 |
| finish 请求 | 声明编号、实际覆盖的 Attempt ID、结束原因和 result；由 owner 根据已知范围生成 |

任务定义不能仅靠修改一个 task key 就成立：定义文档的 `definition_id`、内容摘要、引用、scope 和参数要一起满足契约。当前没有从任意 Shell/takflow 自动生成这些输入的用户命令。先用现有合成定义验证接入方式；自有任务定义按[契约来源](../reference/contracts.md)及[spec 说明](https://github.com/cemc-oper/sledra/blob/main/spec/README.md)编制和校验，再替换业务输入。

产品预期可不提供。提供时必须与 Run 的 System/部署/用途/周期一致；记录器只保留引用，尚不执行产品检验、发布或接收判定。

## 用 CLI 手动走通记录链路

下面仍使用合成任务定义，但将请求放到新的工作目录、用绝对输入路径，并创建本次独立的令牌。这样可以看到 Shell 钩子底下实际调用了什么。

### 准备输入

从完成构建的源码根执行。Python 仅用于本教程准备 JSON 和读取 ID，不是 Go 记录器的运行依赖：

```bash
repo_dir=$(pwd -P)
sledra_bin="$repo_dir/build/bin/sledra"
fixture_dir="$repo_dir/examples/standalone/fixtures"
case_dir=$(mktemp -d "${TMPDIR:-/tmp}/sledra-manual.XXXXXX")
mkdir "$case_dir/observability" "$case_dir/inputs"
export SLEDRA_ROOT="$case_dir/observability"

python3 - "$fixture_dir" "$case_dir" <<'PY'
import json
import sys
from pathlib import Path

fixtures, case = map(Path, sys.argv[1:])
run = json.loads((fixtures / "run-request.json").read_text())
run["launch_token"] = case.name + "-launch"
run["definition_file"] = str(fixtures / "definition.json")
run["expectation_file"] = str(fixtures / "expectation.json")
attempt = json.loads((fixtures / "attempt-request.json").read_text())
attempt["execution_token"] = case.name + "-execution-1"
for name, value in [("run-request", run), ("attempt-request", attempt)]:
    (case / "inputs" / (name + ".json")).write_text(json.dumps(value) + "\n")
PY
```

Run 请求确定运行范围，Attempt 请求确定本次尝试。相同 Run 请求重试登记时保留原 token；真正新发起或新尝试才分配新 token。令牌由 owner 在首次调用前生成并保存，不在每次登记重试时临时重新生成。

### 登记 Run 和 Attempt

```bash
"$sledra_bin" registry run-create \
  --request-file "$case_dir/inputs/run-request.json" \
  --context-out "$SLEDRA_ROOT/contexts/run.json"
"$sledra_bin" registry attempt-create \
  --context "$SLEDRA_ROOT/contexts/run.json" \
  --request-file "$case_dir/inputs/attempt-request.json" \
  --context-out "$SLEDRA_ROOT/contexts/attempt-1.json"
"$sledra_bin" context validate --file "$SLEDRA_ROOT/contexts/attempt-1.json"
```

成功无 stdout/stderr，context 是结果传递文件，不能用 `run_id=$(sledra ...)` 获取身份。Attempt 登记必须接收明确的父 Run context，事件必须接收明确的 Attempt context；不能按周期查找“最新 Run”。

身份调用失败时应先检查分类错误，停止该记录链路或关闭观测，不能拿不完整身份继续发事件。登记结果不确定时，只重试相同 token/请求，不重跑业务。公共 context 不可变；同一 Run/Attempt 使用原固定路径，新对象使用新路径。

### 记录启动、执行业务、记录退出

```bash
export SLEDRA_CONFIG="$fixture_dir/process-config.json"
"$sledra_bin" event --context "$SLEDRA_ROOT/contexts/attempt-1.json" \
  --type attempt.started --invocation-id manual-invocation-1 \
  --process-role forecast_program --strict

if STANDALONE_EXECUTION_LOG="$case_dir/executions.log" \
  STANDALONE_PRODUCT="$case_dir/product.txt" STANDALONE_PAYLOAD_MODE=success \
  "$repo_dir/examples/standalone/payload.sh"; then
    saved_rc=0
else
    saved_rc=$?
fi

"$sledra_bin" event --context "$SLEDRA_ROOT/contexts/attempt-1.json" \
  --type attempt.exited --process-role forecast_program --exit-code "$saved_rc" --strict
printf '业务退出码：%s\n' "$saved_rc"
```

业务只在这里启动一次。`saved_rc` 是业务结果，记录命令的退出码不能替换它。此手动演示使用 `--strict` 暴露记录错误；实际 Bash owner 通常通过薄钩子隔离观测故障，下一节解释调用方式。这里没有 stage producer，所以只有两条进程事件。

`attempt.started` 在调用业务前记录，代表 owner 的启动边界，不证明程序已经完成内部初始化。启动失败、信号或丢失尾事件需保留实际证据，不能根据缺失记录补造正常退出。

### 生成明确的结束覆盖集合

前面的请求更换了 execution token，Attempt ID 已变化，因此不能直接套用示例固定的 finish 请求。读取实际 context，准备一个新的结束声明：

```bash
python3 - "$case_dir" <<'PY'
import json
import sys
from pathlib import Path

case = Path(sys.argv[1])
attempt = json.loads((case / "observability/contexts/attempt-1.json").read_text())
finish = {
    "declaration_id": case.name + "-finish-1",
    "rule_version": "1",
    "covered_attempt_ids": [attempt["attempt_id"]],
    "reason": "manual synthetic completion",
    "result": "finished",
    "source_id": "manual-owner",
    "session_id": case.name + "-owner",
}
(case / "inputs/finish-request.json").write_text(json.dumps(finish) + "\n")
PY
"$sledra_bin" run finish --context "$SLEDRA_ROOT/contexts/run.json" \
  --scope-file "$case_dir/inputs/finish-request.json"
printf '记录目录：%s\n' "$case_dir"
```

此例的 success payload 已结束，owner 对这一个 Attempt 声明 finished。真实 owner 应根据自己的结束策略决定声明时点、result 和覆盖集合，不能将退出码 0 等同产品交付成功。相同声明 ID/内容可重复调用；同 ID 改内容会冲突。后续有新 Attempt 时，原声明不会变化，需要新的声明保留新覆盖范围。

## 在已有 owner 中放置 Shell 钩子

源码使用 `shell/sledra.sh`，安装包使用 `share/sledra/shell/sledra.sh`。显式设置 `SLEDRA_ENABLED=1`、绝对二进制/记录根/producer 配置路径，再 source 库。

下面是插入位置的示意，变量和已有 handler 由 owner 提供，**不是独立完整脚本**：

```bash
source "$sledra_shell"
sledra_run_begin "$run_request" "$run_context"
sledra_attempt_begin "$run_context" "$attempt_request" "$attempt_context"
sledra_attempt_started "$invocation_id" forecast_program

# 此处保持原有业务执行、wait、pipeline 和 callback 控制流。
# 原有退出 handler 首先保存 $?，随后使用已保存的值：
sledra_attempt_exited "$saved_rc" forecast_program

# 由 Run owner 按结束策略准备覆盖集合后调用：
sledra_run_finish "$run_context" "$finish_request"
```

| 已知生命周期点 | 调用 | owner 的责任 |
|---|---|---|
| 本次整体运行发起 | `sledra_run_begin` | 冻结 scope，保存本次 launch token，发布固定 Run context |
| 任务一次尝试开始前 | `sledra_attempt_begin` | 指定父 Run、新 execution token 与 Attempt context |
| 准备启动业务 | `sledra_attempt_started` | 提供本次 invocation ID，业务仍只启动一次 |
| 明确阶段边界 | `sledra_event_file stage.started/stage.ended` | 用 stage_owner 配置及 JSON payload，保持 interval ID 一致 |
| 已知普通退出 | `sledra_attempt_exited` | 先保存原退出码，再记录，最后返回原业务结果 |
| 已知信号退出 | `sledra_attempt_signaled` | 提供真实信号编号，原 handler 继续处理转发与重新抛出 |
| owner 决定结束 | `sledra_run_finish` | 提供准确覆盖集合，不用任务退出码代替运行/交付判断 |

Run owner 和任务 owner 分属不同 shell 时，接收明确 Run context 的任务 shell 在 source 后、调用 `sledra_attempt_begin` 前设置 `SLEDRA_RUN_ACTIVE=1`，表示使用已有 Run，而非再次创建 Run。其余环境也必须显式传递；该 active 标志只是 Shell 调用门槛，不会跳过 Go 对父 context 的校验。完成 Attempt 登记后由库设置 `SLEDRA_ATTEMPT_CONTEXT` 和 active 状态，事件才会使用该 context。Task owner 不应擅自替整体运行声明结束。

这些钩子始终向 owner 返回 0，不能用 `$?` 判断观测是否成功。Run/Attempt 登记成功时设置 `SLEDRA_RUN_ACTIVE`、`SLEDRA_ATTEMPT_ACTIVE`；身份失败关闭后续观测，故障最多发出一次固定 stderr 诊断。需要验收完整性时，核对 context、Registry、spool 和 finish；需要定位错误时用同一输入直接运行严格 CLI。

库不替换原 trap/选项。修改退出 handler 时第一步保存 `$?` 或 `PIPESTATUS`；记录和 callback 后返回原结果。不要为观测降级增加第二次 payload 执行，也不要把原有业务函数或 source 的业务脚本放进新的 `if`、`||` 或命令替换，改变 Bash errexit 语义。前面的手动 CLI 示例捕获的是独立 Bash payload 进程。

## 管理真实重试和并发

真正的任务重试沿用父 Run、task key 和参数，分配新 execution token、新 Attempt context，并将 `retry_of` 设为原 Attempt ID。同一个登记请求的重试保持旧 token；两者不能混淆。同周期重新发起整次运行则分配新的 launch token；若需表达与前一 Run 的关系，使用请求的 `rerun_of`。

producer 的 session 也按 writer 实例管理，不把同一 session 同时交给多个并发 producer。process 与 stage 使用各自允许的角色，不按单条事件任意改角色。容量限制和目录要求见[记录配置](configuration.md)。

重发 `sledra event` 会产生新的事件时间/信封，不能保证不确定追加后的字节级重传。未来采集的同 ID 去重也不能替代完整信封重传；此 Shell API 尚未提供持久事件重试队列。

## 接入后怎样判断可用

先在新的本地工作目录分别运行关闭/开启观测的正常和失败场景，核对业务退出码、stdout/stderr、callback、产品与 payload 次数保持一致，再核对身份和事件。随后模拟二进制缺失、配置错误、context 损坏，确认观测失败不重跑或改变业务。

本地支持的完整参考是独立示例及现有 Shell 回归。共享/HPC 挂载、调度器、MPI 和信号 profile 按目标环境另验；不能把新增钩子自动解释为 takflow 集成或生产资格。完整结果阅读方法见[独立示例详解](standalone.md#核对运行结果)，验证工具见[本地与 HPC 验证工具](hpc.md)。
