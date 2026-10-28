# Sledra 离线分析组件

新版 [Execution v2 文件读取](src/sledra_analysis/execution/README.md) 已完成 P2-01：独立 codec、显式日期窗口扫描与合成 Run 查找，无数据库/旧 ingest 前置。P2-02 的 [Execution 汇总与统计](src/sledra_analysis/execution/summary.md) 已实现；查询 CLI 仍待 P2-03。以下 SLD-10–15 继续描述旧分析路径。

当前交付 SLD-10 文件身份、世代和 offset 模型，SLD-11 SQLite 原始事实、去重与冲突存储，SLD-12 有界读取、校验／隔离和重处理，以及 SLD-13 接入事务、持久队列、原始归档／确认和备份恢复，SLD-14 身份关联与多维状态投影，以及 SLD-15 时间分解与条件统计，独立发行名为 `sledra-analysis`，开发版本 `0.1.0.dev0`，运行使用 Python 3.12+、SQLite 标准库，以及固定 jsonschema 4.10.3；传递依赖由 analysis/uv.lock 锁定。Go/Bash 记录器不依赖此组件。存储需要 Python 内置 SQLite 3.37+ 与 Linux flock；SLD-15 已提供时间配对、时钟质量与不完整样本统计。

从仓库根目录执行：

```bash
make setup
make -C analysis build
make -C analysis check
make -C analysis test
```

根 `make build/check/test` 同时协调该组件；`OUTPUT_DIR` 控制构建目录，wheel 和 sdist 默认为 `build/analysis/`。Go/Bash 记录器不引入分析依赖；构建 backend setuptools 80.9.0 固定在元数据与独立 `uv.lock`，setup 同步并准备缓存，日常构建离线。根项目继续管理 Ruff 等开发工具，根 check 不把记录器作为 Python 依赖。

普通测试在新临时目录注入轮转、改写与 SQLite 写入故障，不修改冻结 M1 样本，不自动创建报告。需要保存逐项结果时显式指定不存在的私有目录：

```bash
uv run --project analysis --locked --offline --no-python-downloads --python /usr/bin/python3 python analysis/tests/verify.py --task SLD-12 --report-dir /tmp/sledra-v10-new
uv run --project analysis --locked --offline --no-python-downloads --python /usr/bin/python3 python analysis/tests/replay_quarantine.py --report-dir /tmp/sledra-quarantine-new
```

接口、offset 使用方式与完整性限制见[文件身份与世代](../docs/source/development/source-identity.md)。

原始 bytes、规范 hash、冲突和来源查询见[原始事实存储](../docs/source/development/raw-facts.md)。默认组件测试覆盖 20 项 V11、24 项 V09、28 项 V10/V16 、33 项 V12/V21 、38 项状态重建与 47 项时间统计回归；`--task SLD-10` 可只回归身份层，`--task SLD-11` 可只验证存储，`--task SLD-12` 可只验证读取／隔离，`--task SLD-13` 可验证事务／确认／恢复，`--task SLD-14` 可验证状态重建，`--task SLD-15` 可验证时间统计。

行预算、隔离查询、重处理与 schema 1 的显式升级见[有界读取与隔离](../docs/source/development/bounded-ingest.md)。SLD-13 的事务入口、持久队列与确认／恢复见[接入事务与恢复](../docs/source/development/ingest-recovery.md)。普通 BoundedReader 仍只返回位置候选；AtomicIngestor 才提供检查点同事务。

保存事务恢复合成现场：

```bash
uv run --project analysis --locked --offline --no-python-downloads --python /usr/bin/python3 python analysis/tests/replay_recovery.py --report-dir /tmp/sledra-recovery-new
```

SLD-14 的父引用解析、因果重试／选择、结束声明修订和投影水位见[状态重建](../docs/source/development/state-projections.md)。rebuild 不依赖到达顺序，不从引擎或进程状态推断产品交付。

SLD-15 的区间接口、单调/UTC 时钟、accounting 与采样区分、条件统计和不可变报告见[时间分解与不完整样本](../docs/source/development/timing-metrics.md)。显式保存回放可运行 `uv run --project analysis --locked --offline python analysis/tests/replay_metrics.py --report-dir /tmp/sledra-timing-new`。

SLD-23 的[核心故障回归](../docs/source/development/testing/conformance.md)通过根 `make test-conformance` 联合运行本组件全部回归、端到端故障、Go和Shell对照；普通 `make -C analysis test` 保持组件独立入口。
