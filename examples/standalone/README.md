# 独立合成记录示例

本例用本地 Bash owner 显式调用 Go 记录器，展示一次任务从登记到结束的全过程。它不启动 takflow、ecFlow、scheduler、分析服务或真实 MCV。[run.sh](run.sh)管理生命周期，[payload.sh](payload.sh)执行业务，[fixtures/](fixtures/)提供合成任务和身份输入。

## 准备和运行

源码根完成 `make setup`、`make build` 后，使用新的可写目录：

```bash
repo_dir=$(pwd -P)
work_dir=$(mktemp -d "${TMPDIR:-/tmp}/sledra-example.XXXXXX")
mkdir "$work_dir/observability"
STANDALONE_WORK_DIR="$work_dir" SLEDRA_ROOT="$work_dir/observability" \
  SLEDRA_BIN="$repo_dir/build/bin/sledra" SLEDRA_ENABLED=1 \
  STANDALONE_PAYLOAD_MODE=success "$repo_dir/examples/standalone/run.sh"
printf '工作目录：%s\n' "$work_dir"
```

试用包用户使用 `bin/sledra` 和 `share/sledra/examples/standalone/run.sh` 的绝对路径；不需要源码、Go 或 Python。脚本按自身位置寻找 Shell 库、夹具和 payload，并在该示例目录执行，只向工作目录写入。

## 输入怎样进入记录器

`run-request.json` 描述 `synthetic.model`、`wsl-sandbox`、固定合成周期、任务 scope、definition/expectation 文件及 `standalone-launch-1` 发起令牌。definition 声明 `main.forecast` 的 member/lead_seconds 参数和 domain 默认值。

`attempt-request.json` 指定该 Run 内 `main.forecast` 的成员 `"01"`、预报秒数 `0`、`standalone-exec-1` 执行令牌和 `forecast_program` 角色。Run/Attempt 登记时由记录器推导身份，owner 指定 context 的固定绝对路径。

`process-config.json` 以 process_owner 角色发送进程事件；`stage-config.json` 以 stage_owner 角色和独立 session 发送阶段事件。`stage-start.json`、`stage-end.json` 共享 interval ID。`finish-request.json` 明确覆盖这个固定合成 Attempt，不通过“最新 Attempt”猜测结束范围。

## 脚本怎样记录一次执行

| 位置 | 调用或动作 | 保存的事实 |
|---|---|---|
| owner 初始化 | source `integrations/bash/sledra.sh`，设置自己的 options/trap | 库仅定义函数 |
| Run 发起 | `sledra_run_begin` | definition/expectation、Run Registry 和 Run context |
| 任务尝试 | `sledra_attempt_begin` | 父 Run 下的 Attempt Registry 和 Attempt context |
| payload 前 | `sledra_attempt_started`、stage_owner 的 stage.started | 启动与阶段开始事件 |
| 业务执行 | 启动一次 `payload.sh`，保存 PID 并 wait | 打印 stdout、追加一次执行日志、生成合成产品 |
| 正常返回 | stage_owner 的 stage.ended | 阶段结束事件 |
| EXIT handler | 先保存原退出码，调用 `sledra_attempt_exited` | 退出事件；成功时声明 finish；业务 callback 和原退出码保留 |

payload 本身不调用 Sledra：它测试 pipeline 和 background wait，并按 success/fail/hang 模式生成产品、退出 7 或等待信号。owner 执行业务，记录钩子不重跑 payload。

## 核对文件与失败路径

成功时 `executions.log` 为一行 `run`，`callbacks.log` 为 `exit:0`，`product.txt` 为 `synthetic product`。`observability/contexts/` 保存 run/attempt JSON，Registry 保存不可变权威记录及注册 outbox；spool 保存四条过程事件，finish 保存明确覆盖集合及结束 outbox。

JSONL 首行是段头，不能算事件；注册/finish outbox 不在 spool 中。成功应有 1 Run、1 Attempt、4 条过程事件、2 条注册 outbox、1 条 finish outbox。`sledra context validate --file ...` 用于校验 context 和父记录；脚本退出码为 0 不能单独证明观测完整。

在新目录设置 `STANDALONE_PAYLOAD_MODE=fail` 时，payload 退出 7，wait 触发 owner 的 EXIT handler；callback 为 `exit:7`，没有产品、stage.ended 或本例的成功 finish，但仍记录 attempt.exited 的退出码。设置 `SLEDRA_ENABLED=0` 时业务照常执行，记录根保持空。

本例 token 和 finish 覆盖 ID 固定，每次试验使用新目录。真实重试要沿用父 Run，使用新 execution token、新 Attempt context 和 retry_of，再为实际覆盖集合生成 finish 请求。`SLEDRA_CONFIG` 可覆盖配置；`STANDALONE_CONTEXT_OVERRIDE` 是测试故障注入，不是生产兜底。

库不接管业务 handler。当前 TERM/HUP 转发与重新抛出有本地回归，KILL 没有尾事件，即时 INT、MPI、任意进程组以及真实 HPC/业务 callback 尚未验收。

逐步命令、JSON 阅读脚本与各调用的完整解释见[独立示例详解](https://github.com/cemc-oper/sledra/blob/main/docs/source/guides/standalone.md)；自行准备输入、使用 CLI 和接入已有脚本见[脚本接入说明](https://github.com/cemc-oper/sledra/blob/main/docs/source/guides/recording.md)。
