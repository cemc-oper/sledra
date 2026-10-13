# 独立示例详解

本页从输入、脚本控制流到输出文件解释一次 Sledra 记录过程。先完成[源码快速开始](../getting-started/quickstart.md)或[试用包安装](../getting-started/installation.md)，再按下面步骤运行和核对。记录本身需要 Bash 和 `sledra`，后面的 JSON 阅读脚本使用可选的标准库 Python。

示例代表一个明确管理任务生命周期的 **owner 脚本**：它知道何时发起 Run、启动任务、收到退出码以及声明运行结束。Sledra 钩子在这些位置保存事实；业务执行仍由 owner 控制。当前没有 takflow/ecFlow 集成，也没有自动发现任务或分析运行结果的服务。

## 示例由哪些文件组成

源码位于 `examples/standalone/`，试用包中对应 `share/sledra/examples/standalone/`。以下链接指向仓库源码；安装用户可在上述包内目录直接阅读同名文件。

| 文件 | 作用 |
|---|---|
| [run.sh](https://github.com/cemc-oper/sledra/blob/main/examples/standalone/run.sh) | owner：设置路径、加载 Shell 库、安装自己的 handler、调用记录钩子并启动 payload |
| [payload.sh](https://github.com/cemc-oper/sledra/blob/main/examples/standalone/payload.sh) | 合成业务：打印输出、写一次执行日志、运行 pipeline/background wait，成功时生成产品 |
| `fixtures/definition.json` | 描述任务 `main.forecast`、阶段 `forecast`、参数类型及默认值 |
| `fixtures/expectation.json` | 合成周期和产品预期；当前只保存和引用，不执行产品检验或交付判定 |
| `fixtures/run-request.json` | 登记哪个 System/部署/周期、运行哪些任务，以及本次发起令牌 |
| `fixtures/attempt-request.json` | 登记哪一个任务实例的本次执行令牌、参数和进程角色 |
| `fixtures/process-config.json` | 进程事件 producer，角色为 `process_owner` |
| `fixtures/stage-config.json` | 阶段事件 producer，角色为 `stage_owner`，使用独立 session |
| `fixtures/stage-start.json`、`stage-end.json` | `forecast` 阶段开始和结束的 payload，共用 interval ID |
| `fixtures/finish-request.json` | 成功路径的结束声明，明确覆盖这次合成 Attempt |

Shell 库在源码 `shell/sledra.sh`，安装后在 `share/sledra/shell/sledra.sh`。加载它只定义函数，不安装 trap、不改 Shell 选项、不执行 payload。

## 先理解身份和输入

### Run 表示一次发起

Run 请求使用以下合成值：

```json
{
  "registry_id": "registry-synthetic-1",
  "system_id": "synthetic.model",
  "purpose": "test",
  "deployment_id": "wsl-sandbox",
  "platform_id": "wsl2",
  "cycle": {
    "cycle_kind": "forecast",
    "cycle_time": "2026-10-09T16:00:00Z"
  },
  "launch_token": "standalone-launch-1",
  "definition_file": "fixtures/definition.json",
  "expectation_file": "fixtures/expectation.json",
  "scope": [
    {
      "task_key": "main.forecast",
      "parameters": {"lead_seconds": 0, "member": "01"}
    }
  ]
}
```

`cycle_time` 是被描述的业务周期，不是当前执行时间。`scope` 明确本次运行的任务和参数；`launch_token` 区分真实发起，同周期的新发起也应使用新令牌。定义中的 `domain` 默认值为 `global`，登记时展开到 context。

`run.sh` 先切换到自身目录，因此请求中的相对 definition/expectation 路径可解析。直接调用 CLI 时应使用绝对路径，详见[在脚本中接入记录](recording.md)。

### Attempt 表示一次任务尝试

```json
{
  "task_key": "main.forecast",
  "parameters": {"lead_seconds": 0, "member": "01"},
  "execution_token": "standalone-exec-1",
  "process_role": "forecast_program"
}
```

Attempt 通过显式 Run context 关联父 Run。任务必须在该 Run 的 scope 内，参数类型和值必须符合定义。`member` 是字符串 `"01"`，`lead_seconds` 是整数秒；不能随意改成整数成员号或 `"0h"`。

重复登记同一令牌和请求复用身份；同一令牌换内容会冲突。一次真正的重试使用新 `execution_token`，并以 `retry_of` 指向前一次 Attempt。

### producer 决定事件来源和角色

进程配置如下：

```json
{
  "producer_name": "standalone-shell",
  "producer_role": "process_owner",
  "producer_version": "0.1.0",
  "source_id": "standalone-node",
  "session_id": "standalone-process"
}
```

阶段配置的 role 为 `stage_owner`，session 为 `standalone-stage`。因此进程事件与阶段事件分开写入各自 session 的 spool；不能给 process producer 直接发送 stage 事件。`source_id` 标识来源，`session_id` 标识一次写入会话；并发 producer 应使用不同 session。

## 运行一次成功场景

从已经构建的源码根目录运行：

```bash
repo_dir=$(pwd -P)
sledra_bin="$repo_dir/build/bin/sledra"
example_dir="$repo_dir/examples/standalone"
work_dir=$(mktemp -d "${TMPDIR:-/tmp}/sledra-use.XXXXXX")
mkdir "$work_dir/observability"

STANDALONE_WORK_DIR="$work_dir" SLEDRA_ROOT="$work_dir/observability" \
  SLEDRA_BIN="$sledra_bin" SLEDRA_ENABLED=1 STANDALONE_PAYLOAD_MODE=success \
  "$example_dir/run.sh"
printf '工作目录：%s\n' "$work_dir"
```

安装包用户先按安装指南设置绝对 `install_dir`，再将前三行改为：

```bash
sledra_bin="$install_dir/bin/sledra"
example_dir="$install_dir/share/sledra/examples/standalone"
```

后续创建工作目录、运行与核对步骤相同。安装资源可以只读，运行数据只写 `work_dir`。`SLEDRA_ENABLED=1` 启用 Shell 钩子；`SLEDRA_BIN` 是实际记录器，`SLEDRA_ROOT` 是已创建的记录根。`run.sh` 自动选择合成 producer 配置，无需另设 `SLEDRA_CONFIG`。

本教程假定未另外设置 `SLEDRA_CONFIG`、`SLEDRA_SPEC_DIR` 或故障注入变量；已有自定义值会影响结果。示例使用固定令牌及结束覆盖集合，每次创建新目录隔离试验。不同试验目录中的相同合成 ID，不代表真实业务中发生了同一运行。

成功时，终端输出为：

```text
payload-start
pipeline
payload-end
```

这些是 payload 的 stdout，Sledra 的成功记录调用不向业务 stdout 添加内容。

## run.sh 按什么顺序工作

| 顺序 | owner 的动作 | Sledra 保存什么 |
|---|---|---|
| 1 | 确定脚本和工作目录，加载 `sledra.sh` | 此时不写记录 |
| 2 | 设置自己的 EXIT/TERM/INT/HUP handler | Shell 库不接管 handler |
| 3 | `sledra_run_begin` | 验证 definition/expectation，登记 Run，发布 `contexts/run.json` |
| 4 | `sledra_attempt_begin` | 按父 Run 登记 Attempt，发布 `contexts/attempt.json` |
| 5 | `sledra_attempt_started` | `attempt.started`，携带 invocation ID 与进程角色 |
| 6 | 切换为 stage 配置，调用 `sledra_event_file stage.started` | `stage.started`，携带 stage key 与 interval ID |
| 7 | `./payload.sh &`，保存 PID，`wait` | 业务只启动一次；payload 自己打印、写日志和产品 |
| 8 | 正常返回后以 stage 配置记录结束 | `stage.ended`，payload 中 `result` 为 `success` |
| 9 | EXIT handler 先保存原退出码，再记录退出；退出码为 0 时声明 finish | `attempt.exited`、成功路径的 `run.finish_declared` |

关键调用位于真实业务边界，摘自现有 owner：

```bash
sledra_run_begin fixtures/run-request.json "$run_context"
sledra_attempt_begin "$run_context" fixtures/attempt-request.json "$attempt_context"
sledra_attempt_started standalone-invocation-1 forecast_program

SLEDRA_CONFIG=$example_dir/fixtures/stage-config.json \
    sledra_event_file stage.started fixtures/stage-start.json

./payload.sh &
payload_pid=$!
wait "$payload_pid"
payload_pid=

SLEDRA_CONFIG=$example_dir/fixtures/stage-config.json \
    sledra_event_file stage.ended fixtures/stage-end.json
```

`run_context`、`attempt_context` 在前面已指定为记录根下的固定绝对路径。摘录用于解释调用位置，不是可单独运行的完整脚本；完整选项和 handler 以 `run.sh` 为准。

### payload 做了什么

payload 不调用 Sledra。它先打印 `payload-start`，向 `executions.log` 追加一行 `run`，执行 `printf 'pipeline\n' | cat`，再启动并等待一个短暂后台 sleep。默认 success 模式写 `product.txt` 并打印 `payload-end`。这些动作同时测试 pipeline、后台等待和业务输出是否因观测改变。

### EXIT handler 为什么先保存退出码

`trap 'on_exit "$?"' EXIT` 在进入 handler 时保存原退出码。handler 先取消自身 EXIT trap，避免递归，再调用 `sledra_attempt_exited "$original_rc" forecast_program`。成功时追加 finish，随后写业务 callback，最后 `exit "$original_rc"`，保留业务结果。

Shell 钩子向 owner 返回 0，以隔离记录失败；这不保证记录成功。若登记失败，active 状态关闭，后续关联事件跳过，业务仍照原控制流执行。因此不能只看脚本退出码为 0 就认定观测完整。

## 核对运行结果

### 先检查业务没有被重复执行

```bash
cat "$work_dir/executions.log"
cat "$work_dir/callbacks.log"
cat "$work_dir/product.txt"
```

预期分别为一行 `run`、一行 `exit:0` 和一行 `synthetic product`。`executions.log` 只有一行表明本例 payload 只启动一次；callback 是 owner 写入的业务日志，不是 Sledra 事件。

### 再校验身份文件

```bash
SLEDRA_ROOT="$work_dir/observability" \
  "$sledra_bin" context validate --file "$work_dir/observability/contexts/run.json"
SLEDRA_ROOT="$work_dir/observability" \
  "$sledra_bin" context validate --file "$work_dir/observability/contexts/attempt.json"
```

成功时命令无输出、退出码为 0。校验会核对 context 摘要及确切父记录；读取 JSON 看到 ID 只是阅读，不能代替此项校验。

### 最后查看原始事实

```text
工作目录/
├── executions.log            # payload 启动次数
├── callbacks.log             # owner 的业务 callback
├── product.txt               # 合成业务产品
└── observability/
    ├── documents/            # 不可变任务定义、产品预期
    ├── contexts/             # 本例的 run.json、attempt.json
    ├── registry/             # Run/Attempt 权威记录，内含注册 outbox
    ├── spool/                # producer session 的过程事件 JSONL 和锁文件
    └── finish/               # 明确覆盖 Attempt 的不可变结束声明及 outbox
```

下面的阅读脚本打印关联 ID、事件类型和结束覆盖集合，不修改文件，也不启动分析服务：

```bash
python3 - "$work_dir/observability" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
run = json.loads((root / "contexts/run.json").read_text())
attempt = json.loads((root / "contexts/attempt.json").read_text())
print("Run:", run["run_id"])
print("Attempt:", attempt["attempt_id"])
print("父 Run 一致:", attempt["run_id"] == run["run_id"])
print("参数:", attempt["parameters"])
for path in sorted((root / "spool").rglob("*.jsonl")):
    for line in path.read_text().splitlines():
        record = json.loads(line)
        if record.get("record_kind") == "segment_header":
            continue
        print("事件:", record["event_type"], record["association"], record["payload"])
for path in sorted((root / "registry").rglob("*.json")):
    print("注册 outbox:", json.loads(path.read_text())["outbox_event"]["event_type"])
for path in sorted((root / "finish").rglob("*.json")):
    record = json.loads(path.read_text())
    print("结束覆盖:", record["outbox_event"]["payload"]["covered_attempt_ids"])
PY
```

本例应有 **1 Run、1 Attempt、4 条过程事件、2 条注册 outbox 和1条 finish outbox**。四条过程事件为 `attempt.started`、`stage.started`、`stage.ended`、`attempt.exited`；退出 payload 中 `exit_code` 为 0。Run/Attempt ID 不需手工推导，直接读取已校验 context；`domain: global` 是展开的默认值。

注册事件在 Registry 记录的 `outbox_event` 中，finish 事件在 finish 记录中，不在 spool 文件里。每个 JSONL 的首行是段头，不能当事件。目录遍历顺序不等于多个 producer 的发生顺序，当前也没有合并后的分析时间线。

finish 表示 owner 对明确 Attempt 集合的结束声明；它不证明产品质量、业务交付成功或远端已接收。spool 中的 open 段亦不代表已经采集确认，不能自行删除。

## 失败和关闭观测时有什么区别

保持前面的 `sledra_bin`、`example_dir`，使用一个新的目录运行失败模式。外层仅捕获独立脚本的退出码，payload 内的 `set -e` 仍由其自身 Bash 进程执行：

```bash
failed_dir=$(mktemp -d "${TMPDIR:-/tmp}/sledra-fail.XXXXXX")
mkdir "$failed_dir/observability"
if STANDALONE_WORK_DIR="$failed_dir" SLEDRA_ROOT="$failed_dir/observability" \
  SLEDRA_BIN="$sledra_bin" SLEDRA_ENABLED=1 STANDALONE_PAYLOAD_MODE=fail \
  "$example_dir/run.sh"; then
    failed_rc=0
else
    failed_rc=$?
fi
printf '退出码：%s；目录：%s\n' "$failed_rc" "$failed_dir"
```

| 检查项 | success、观测开启 | fail、观测开启 | success、观测关闭 |
|---|---|---|---|
| owner 退出码 | 0 | 7 | 0 |
| payload 次数 | 1 | 1 | 1 |
| 产品 | 存在 | 不生成 | 存在 |
| callback | `exit:0` | `exit:7` | `exit:0` |
| Run/Attempt | 已登记 | 已登记 | 不登记 |
| 过程事件 | start、stage start/end、exit | start、stage start、exit（exit code 7） | 不写 |
| finish | 明确覆盖本 Attempt | 本例不声明 | 不写 |

失败时 `wait` 返回 7，owner 的 `set -e` 进入 EXIT handler，因此跳过正常路径的 `stage.ended`。没有结束事件不能直接解释为“阶段成功”。`run.sh` 的“只有成功才声明 finish”是此合成 owner 的策略，其他 owner 若要声明 abandoned/unknown，须提供对应真实依据和覆盖集合。

关闭观测时在**另一个新目录**使用 `SLEDRA_ENABLED=0` 运行相同脚本；其余参数不变。业务输出、产品和 callback 应与成功基线一致，预先创建的 observability 目录保持空。TERM/HUP 的转发与重新抛出有本地回归；KILL 无尾事件，即时 INT、MPI 和任意进程组透明性尚未验收。

## 重试为什么不能直接重跑同一个目录

本例固定 `launch_token`、`execution_token` 和 finish 覆盖 ID，只用于单次合成试验。在同一根目录再跑同样请求会复用原身份，不会自动创建新 Attempt；重新发事件也不能冒充原信封重传。

真正的任务重试应保留原 Run context，换新 execution token 和 Attempt context 路径，并设置 `retry_of`。`make demo` 的 M1 工具按这个过程创建一个正常 Run，以及另一个包含失败和成功重试的 Run：先记录失败 Attempt，随后新 Attempt 关联 `retry_of`，成功后冻结两次尝试的 finish 覆盖集合；再登记一个晚到 Attempt，证明旧声明没有被修改。工具在新目录中保存并回放快照，不更新冻结输入。

接入已有脚本、管理令牌和编写结束声明的方法见[在脚本中接入记录](recording.md)。
