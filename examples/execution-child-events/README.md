# CE-03：父入口与独立业务子进程

[owner.sh](owner.sh) 使用 [Bash owner API](../../integrations/bash/execution-v2.md)，显式选择 `--child-events`，对完整 [business.sh](business.sh) 仅调用一次。inline 用子 shell source 完整业务脚本，external 启动独立 Bash；两者均自行 source [事件库](../../integrations/bash/sledra-events.sh)、attach、按实际业务点 emit，然后 detach。业务脚本可直接运行；缺引用、off 或缺事件库时保留五条合成业务输出。

运行前提供本次构建的 Go 二进制、已初始化记录根，以及部署者预先创建的独立私有控制目录。runtime 必须 mode 0700，所有祖先可信且不可被其他用户写入，不得放在记录根或业务清理树。`/tmp`（包括 sticky 目录）不符合本实现的祖先权限检查。下面只在本地用户缓存的新目录演示；部署者须在实际控制挂载（本地或共享）验证原子替换、稳定 inode 与非阻塞锁，示例不构成 HPC 资格。

```bash
# Sledra 源码根；Go 环境已就绪。
make -C recorder OUTPUT_DIR=/absolute/private/build build
export SLEDRA_BIN=/absolute/private/build/bin/sledra
work=$(mktemp -d "$HOME/.cache/sledra-ce-example.XXXXXXXX")
mkdir -m 700 "$work/runtime"
export SLEDRA_ROOT="$work/records"
export SLEDRA_RUNTIME_DIR="$work/runtime"
export SLEDRA_MODE=execution-v2
"$SLEDRA_BIN" execution init-root --root "$SLEDRA_ROOT" \
    --system synthetic --deployment local --partition-timezone=+00:00
bash examples/execution-child-events/owner.sh inline
bash examples/execution-child-events/owner.sh external
bash examples/execution-child-events/business.sh
```

前两次各产生独立 Execution：started、context、五个 `data.ready`（lead 为 0/3/6/9/12）、ended。最后一次没有父引用，只有原业务输出。发生 Busy 时对应观测事实可能缺失，不等待、不重发、不在末尾补录。owner 示例自行保存 EXIT 结果；只有它实际收到 TERM 才记录 signal 15，子进程返回 143 仍是 exit_code 143。库不安装 trap、不执行回调或业务。

可用 `SLEDRA_BASH_LIBRARY`、`SLEDRA_EVENTS_LIBRARY` 指定各自绝对库路径；owner 的 CE 实现还要求 `sledra-events.sh` 与 `sledra-execution.sh` 同目录。此处是源码示例；独立安装和包兼容验收属于 CE-06。runtime 目录不自动清理；不要删除活跃会话或 writer.lock。部署清理边界见[运行时契约 P09](../../spec/execution-child-events/runtime.md)。

[公共库真实回归](../../tests/shell/test_execution_child_events.py)已接入 `make test`，包括独立进程、幂等/类型、set -a、Busy、回执故障、诊断和业务/信号对照。可单独运行 `uv run --locked --offline --no-python-downloads python -m unittest discover -s tests/shell -p test_execution_child_events.py -v`。跨进程写入切点/SIGKILL 的完整接受矩阵属于 CE-04，GFS、安装、HPC 和发行分别属于后续阶段。
