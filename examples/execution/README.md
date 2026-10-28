# 独立 Execution 示例与 P1 验收

[standalone.sh](standalone.sh) 使用真实 [Bash API](../../integrations/bash/execution-v2.md) 和 Go CLI。它在配置检查前自动建立 Execution，随后记录最终 context、一个命令阶段、业务事件和已观察到的结果。无需身份请求、Run、producer/session、Python、jq、ecFlow 或分析服务；不执行生成器，也不读取 MCV 业务目录。

从 Sledra 源码根执行以下命令。Go 及现有依赖须已准备好；示例只在新临时目录写入：

```bash
make -C recorder build
work=$(mktemp -d)
export SLEDRA_BIN="$PWD/build/bin/sledra"
export SLEDRA_ROOT="$work/records"
"$SLEDRA_BIN" execution init-root --root "$SLEDRA_ROOT" \
    --system synthetic --deployment local --partition-timezone=+08:00
export SLEDRA_MODE=execution-v2
bash examples/execution/standalone.sh /bin/echo 'business output'
find "$SLEDRA_ROOT" -type f
```

默认从源码相对位置加载库；脱离源码时设置 `SLEDRA_BASH_LIBRARY` 为安装好的 sledra-execution.sh 绝对路径，Go 二进制和 SLEDRA_ROOT 也用绝对路径。源码示例及库可复制到只读安装目录执行；P5 试用归档资源尚未更新，本次验收不宣称旧归档已提供该示例。重复运行相同命令会生成不同 Execution，并保留之前的文件。

默认 `SLEDRA_MODE` 未设置/空/off 时关闭观测。启用仅需上面的模式、二进制和根；库须可 source。正常 stdout/stderr 来自原业务命令；观测故障最多增加一条固定 JSON 诊断，不回显路径或业务属性。成功落盘的是 Sledra 自身事件，不复制业务 stdout/stderr、脚本或产物。

## 应用参数与结果所有权

此泛型命令示例明确采用 `EXAMPLE_CYCLE_KIND=none`，不是从缺少日期推断“无时次”。可设置 `EXAMPLE_CYCLE_KIND=single` 与实际 `EXAMPLE_CYCLE`（完整带时区时间）演示有时次执行，或显式设 `multiple`；none/multiple 禁止 cycle。25 km、member 01 是固定合成参数，无真实 MCV 字段映射或 Run 接受。早期业务 cycle 也不会改变今天启动的记录分区。

`EXAMPLE_CONFIGURE` 可指向一个应用配置检查可执行文件，它在 begin 之后、context 和业务之前运行。失败时记录 started/ended，不伪造 context、阶段或业务启动。未传 COMMAND 和非法 cycle 组合也在该阶段返回 2。`EXAMPLE_MESSAGE` 为业务事件字符串，按原 argv 编码；不求值命令替换字面文本。

脚本自己启用 errexit/pipefail 并安装 EXIT/TERM handler；库不安装 handler。原命令 `"$@"` 保持顶层执行，handler 第一条保存原始 `$?`，随后才调用 hooks。阶段只在业务启动后由 owner 以已保存的 rc/signal 结束。`EXAMPLE_CALLBACK_LOG` 可选地写本地 callback 顺序；`EXAMPLE_CALLBACK_EXIT_ZERO=1` 演示 callback 完成后外层 exit 0，但观测结束及 callback 内容仍使用之前保存的真实结果。

TERM 策略由此示例 owner 明确选择：记录 signal 15、callback signal:TERM/exit:143，并退出 143（可选 callback-zero 策略仍可改变最外层状态）。测试向隔离的 owner/业务进程组发送 TERM，使用可协作终止的业务 fixture。示例不实现通用信号转发；若只向正在等待非协作子进程的 Bash 发 TERM，handler 可能推迟到子进程返回。没有进程组/MPI/调度器适配保证。KILL 无法运行 handler，测试只保留已观察事实，不补造阶段或执行结束。

## 故障与验收边界

[实际示例回归](../../tests/shell/test_execution_example.py) 比较关闭和启用观测时的退出码、原 stdout/stderr、实际配置/业务调用次数、argv、产物和 callback 顺序；另外比较缺工具、真实权限拒绝、根不兼容、短尾、整行写入后报错、close 报错回执、坏尾、异常回执等故障。wrapper 注入与真实权限测试分列，不冒充共享文件系统 I/O 资格。

不确定故障后同一个 owner 不再调用 CLI 或重新分配身份，原始字节保留；新脚本启动才重新尝试。测试使用缓存事件和 P0 校验器/直接断言，不依赖未实现的 P2 查询，也不尝试修复历史。多个 Execution 并发各写自己的文件。所有业务和观测测试根均在临时目录。

在源码根运行新路径回归：

```bash
uv run --locked --offline --no-python-downloads python -m unittest discover \
    -s tests/shell -p 'test_execution*.py' -v
```

开发验收需要 Go、Python 和已有 jsonschema 依赖；这些不是记录节点依赖。根 make test 已纳入新测试，P1 全阶段命令、结果和限制保存在仓库外同一简化方案实施计划及 p1-evidence.md。P2 将实现直接文件查询；真实 Run 传递、MCV/takflow 接入和 HPC 平台资格仍属后续范围。
