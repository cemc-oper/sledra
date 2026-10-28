# 独立 Execution 记录／查询示例与 P0–P2 验收

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

开发验收需要 Go、Python 和已有 jsonschema 依赖；这些不是记录节点依赖。根 make test 已纳入新测试，P1 全阶段命令、结果和限制保存在仓库外同一简化方案实施计划及 p1-evidence.md。P2 已实现直接文件查询和下述实际安装闭环；真实 Run 传递、MCV/takflow 接入和 HPC 平台资格仍属后续范围。

## P2：实际写入到已安装 CLI 的查询闭环

P0–P2 已完成本地验收。沿用上面已初始化的临时 `$work/records`、SLEDRA_BIN/ROOT/MODE；记录侧仍只有 Go 和 Bash，不增加 Run 或旧登记配置。以下两次实际启动以同一旧业务时次运行，每次自动生成新 Execution：

```bash
for invocation in 1 2; do
    EXAMPLE_CYCLE_KIND=single EXAMPLE_CYCLE=2024-07-01T00:00:00+08:00 \
        bash examples/execution/standalone.sh /bin/echo 'independent business run'
done

# 使用已经配置好固定依赖的开发查询环境，构建并安装真实 wheel 到临时目录。
query_python=$(uv run --locked --offline --no-python-downloads python \
    -c 'import sys; print(sys.executable)')
(cd analysis && uv build --wheel --offline --no-python-downloads --out-dir "$work/wheels")
query_wheels=("$work"/wheels/*.whl)
uv pip install --offline --no-python-downloads --no-deps --python "$query_python" \
    --target "$work/query" "${query_wheels[0]}"

# 管理侧只读已有根；此处 PYTHONPATH 仅指向临时安装目录，不指向源码。
PYTHONPATH="$work/query" "$query_python" "$work/query/bin/sledra-history" \
    --root "$SLEDRA_ROOT" --all-record-dates --cycle 2024-07-01T00:00:00+08:00 \
    --parameter resolution_km=25 --format jsonl > "$work/history.jsonl"
cat "$work/history.jsonl"

# 只删除本例新建的查询输出，再从记录文件重建，不删除或修复源记录。
rm -- "$work/history.jsonl"
PYTHONPATH="$work/query" "$query_python" "$work/query/bin/sledra-history" \
    --root "$SLEDRA_ROOT" --all-record-dates --cycle 2024-07-01T00:00:00+08:00 \
    --parameter resolution_km=25 --format jsonl > "$work/history.jsonl"
```

这两次旧 cycle 的执行仍在实际入口日期分区，不进入 2024 年记录目录；查询应看到两个不同 ID，visible/started 均为 2，Run 均 unlinked。首次 none 示例不匹配此 single cycle。可以改用默认七天或明确的 --record-from/--record-through 窗口；按 ID 查不到也不自动扩窗。[完整查询说明](../../analysis/src/sledra_analysis/execution/cli.md) 定义覆盖范围、过滤、精确数值、未知/冲突、输出与退出码。--no-deps 复用当前已配置的 jsonschema 4.10.3 及其依赖，不证明空缓存离线安装可用；完整环境准备与 P5 发行资格分开。

[跨语言闭环回归](../../tests/execution/test_roundtrip.py) 实际构建 Go API producer、Go CLI 和 wheel，复制 Bash 库/示例到临时安装位置，在脱离源码的进程中运行已安装 console script。真实写入案例只用独立 Execution：

- 两次 Go API 写入保留独立文件，查询保留大整数、精确小数和 Unicode/换行。
- Bash 关闭/启用观测的业务 argv、实际调用次数、stdout/stderr 与原始失败事实保持一致；callback 外层退出零不覆盖已保存的失败码；配置失败没有伪造 context/业务启动。
- 同一业务重新启动生成新 ID；同名阶段按不同 stage_id 配对，多时次目标事件不会变成多个执行。
- 真实 KILL 留下缺结束/缺阶段端点，不补造运行中状态或耗时。
- 在测试自己创建的临时记录中注入重传、半行、坏 JSON、超长行和 context/结束冲突；合法其他执行仍可查，补齐尾部后可重读，所有变体和位置可检查。故障注入不冒充真实共享卷故障资格。
- 查询进程禁止文件写入、网络/SQLite 连接，未导入旧存储/投影；根清单/字节/mtime 不变，删除查询输出后只靠原 JSONL 重建相同结果。

Run/alias/3 km 与 25 km 分组另用明确的 synthetic-records 根验证：同 cycle 两 Run、同 Run 多执行、独立 alias、无时次辅助执行不伪造 cycle。该测试直接构造合成事实，不调用真实 Run 创建/发布、候选来源接受或 ecFlow API，不能当作生产接入证据。

单独运行本闭环；根 make test 也自动发现它：

```bash
uv run --locked --offline --no-python-downloads python -m unittest discover \
    -s tests/execution -p test_roundtrip.py -v
```

## 当前支持范围与停止边界

| 能力 | 当前范围 |
|---|---|
| 记录 | 2.0.0/布局 2，固定 16 桶，公共 owner 顺序写入，独立 Execution；Linux/WSL、Bash 5.2、Go 1.27、Python 3.12 开发环境已测 |
| 查询 | 已安装 Python 包直接只读文件，日期窗口/业务过滤、精确分组/缺失/冲突/源位置、阶段/声明与墙钟耗时、可重建输出；没有数据库/索引 |
| Run/alias/分辨率 | 仅格式及合成读取/关联解释/分组；生产 begin_run、可靠来源/新鲜度、current_run 发布及真实 ecFlow alias 识别尚未交付 |
| 兼容 | 旧 1.0.0 契约/命令/受保护样例/用户历史保留原义，新旧根隔离；不原地迁移、自动清理或全面删除旧实现 |
| 尚未验收 | Bash 4.4 新 API、共享文件系统/跨节点/HPC 性能与资格、MPI/通用 supervisor、P5 试用归档/默认指南收敛、空环境完整离线依赖安装 |
| 后续接入 | MCV/takflow 仍等待 25 km 接口/入口时序/业务回归/版本范围稳定及后续授权；P0–P2 完成不证明这些条件已满足 |

本轮在 P0–P2 停止。Loki/OTel/Prometheus、真实 Run 传递、历史迁移、自动清理和查询索引均不纳入当前交付；旧试用归档与历史 M1–M3 资格结论不被本地闭环替换。
