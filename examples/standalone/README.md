# 独立合成记录示例（SLD-08）

示例只用Bash、Go sledra和本地目录，不启动分析、scheduler、ecFlow/takflow，不执行真实MCV。[run.sh](run.sh)保留已有EXIT/TERM/HUP/INT handler及callback，登记Run/Attempt、记录start、只运行一次[payload.sh](payload.sh)、记录end/exit，成功时声明finish。payload生成合成产品，测试pipeline/background wait，可返回7或等待signal；Shell库不安装handler或执行业务。

源码根运行：

```bash
(cd recorder && GOPROXY=off GOSUMDB=off go build -o /tmp/sledra-example ./cmd/sledra)
mkdir -p /tmp/sledra-example-work/observability
STANDALONE_WORK_DIR=/tmp/sledra-example-work \
SLEDRA_ROOT=/tmp/sledra-example-work/observability \
SLEDRA_BIN=/tmp/sledra-example SLEDRA_ENABLED=1 \
  examples/standalone/run.sh
python3 tests/shell/verify.py
```

脚本按自身位置查找库/输入/payload，默认内嵌契约，支持不同cwd和只读资源树，只写显式work目录；保留并校验显式SLEDRA_SPEC_DIR。每次使用新目录；SLEDRA_ENABLED=0为关闭基线。token与finish夹具的覆盖Attempt是固定合成值，改变身份须同步finish。SLEDRA_CONFIG用于配置故障，STANDALONE_CONTEXT_OVERRIDE是合成注入，不是生产兜底；PAYLOAD_MODE=fail返回7，hang等待signal，日志只在STANDALONE_WORK_DIR。

当前profile支持显式owner钩子、成功/失败及测试过的TERM/HUP重新抛出。KILL不生成尾事件；非交互Bash background-wait中INT不能即时中断，未作为部署资格。没有透明sledra exec，不确定start后的启动失败、进程组转发和errexit透明性需独立验证。真实共享/HPC、调度和业务callback仍未测。
