# Bash 薄接口（SLD-08）

在明确的 Bash 生命周期 owner 中 source [sledra.sh](sledra.sh)。库只定义函数，不安装或替换 option、trap、payload、callback。设置 SLEDRA_ENABLED=1 及绝对 SLEDRA_BIN/SLEDRA_ROOT/SLEDRA_CONFIG；可选 SLEDRA_SPEC_DIR 未设或为空时用内嵌契约，非空必须为绝对兼容目录。关闭时函数无副作用。身份调用设置 SLEDRA_RUN_ACTIVE/SLEDRA_ATTEMPT_ACTIVE，失败关闭后续观测；身份和事件钩子始终向 owner 返回 0。每个 source 的 shell 最多输出一次不含 payload 的固定 stderr 诊断，无 stdout；不能据此保证内核 I/O 的硬延迟上限。

```bash
source /path/to/sledra.sh
sledra_run_begin /path/to/run-request.json /shared/contexts/run.json
sledra_attempt_begin /shared/contexts/run.json /path/to/attempt-request.json /shared/contexts/attempt.json
sledra_attempt_started invocation-1 forecast_program
sledra_event_file stage.started /path/to/stage-start.json
# 在原 owner 控制流中只执行一次原 payload。
# 在已有 EXIT/signal handler 中先保存 $? 或信号：
sledra_attempt_exited "$saved_rc" forecast_program
sledra_attempt_signaled "$saved_signal_number" forecast_program
sledra_run_finish /shared/contexts/run.json /path/to/finish-request.json
```

started/exited/signaled 钩子向 Go 传递类型化参数，Shell 不拼 JSON。其他事件采用 producer 管理的 JSON 文件。owner 保持 option/trap/callback，先保存 $? 或 PIPESTATUS，在已知生命周期点调用；库不通过 if、|| 或命令替换包装业务执行。未交付透明 sledra exec；任意 pipeline、MPI、进程组及即时 INT 转发尚未验证，注册或观测失败不能重跑业务。

[独立示例](../examples/standalone/README.md)比较关闭/开启观测在 set -e/-u、pipefail、已有 trap、pipeline/background wait、成功/非零、TERM/HUP/KILL 下的行为。本地 WSL/ext4 结果不等于 ecFlow/HPC 脚本或共享卷资格。
