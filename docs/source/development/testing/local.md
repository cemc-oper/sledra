# 本地回归

首次显式联网准备，此后从仓库根目录离线执行：

```bash
make setup
make check
make docs
make test
```

工具链、缓存、构建复用和报告位置见[开发](../workflow.md)。check 在临时副本验证生成字节、锁文件、格式/静态检查、文档链接及 actionlint；docs 用严格模式构建中文站点；test 复用 Go 工具并覆盖工程、包完整性、平台、契约/事件/context、可搬运 CLI、Shell、M1 和冻结回放。

每次执行创建独立 `test-results/`，失败非零退出并标明阶段。保护文件逐项比较，普通命令不覆写 `tests/m1/results/` 或 `tests/platform/results/`。

## 单项检查

setup 后可使用锁定环境运行特定脚本，并复用已有二进制：

```bash
make build
SLEDRA_TEST_BIN_DIR="$PWD/build/bin" uv run --locked --offline python spec/verify.py
SLEDRA_TEST_BIN_DIR="$PWD/build/bin" uv run --locked --offline python tests/cli/verify.py
SLEDRA_TEST_BIN_DIR="$PWD/build/bin" uv run --locked --offline python tests/shell/verify.py
SLEDRA_TEST_BIN_DIR="$PWD/build/bin" uv run --locked --offline python tests/m1/replay.py tests/m1/results/handoff.json
```

未提供 `SLEDRA_TEST_BIN_DIR` 时，工具在私有临时目录用已准备 Go 缓存构建；显式指定但缺失工具时报错。已有 jsonschema 的 Python 仍可直接执行脚本，默认推荐锁定 uv 环境。

## 包验收

`make package` 构建包，`make test-package` 在无源码/Go、OS 断网和只读安装环境验收已有产物，并验证记录不需 Python。系统隔离依赖、包选择和报告保留见[安装](../../getting-started/installation.md)。交叉目标须在匹配硬件运行后才能计为通过。

## 演示、性能与平台

```bash
make demo
make benchmark
make test-platform
```

演示与性能报告写入新目录，不替换历史数据。平台探针仅验证指定工作文件系统的原语；保存实际 mount 和节点证据。HPC/安装参数与报告边界见 [HPC 测试](../../guides/hpc.md)。本地双角色模拟不代表真实跨节点资格；test 另覆盖独立目录/清理、失联、预检等故障。

## 权威来源核对

作者工作区可额外执行：

```bash
uv run --locked --offline python scripts/check_docs.py --plan-dir ../../docs/observability-plan
```

独立源码运行无需相邻规划目录；该可选检查核对已记录上游摘要，见[契约来源](../../reference/contracts.md)。[CI](ci.md)复用公共命令并保存报告；远端 Actions 和 HPC 状态见[支持矩阵](../../reference/compatibility/support.md)。
