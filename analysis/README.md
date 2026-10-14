# Sledra 离线分析组件

当前交付 SLD-10 文件身份、世代和 offset 模型，SLD-11 SQLite 原始事实、去重与冲突存储，以及 SLD-12 有界读取、校验／隔离和重处理，独立发行名为 `sledra-analysis`，开发版本 `0.1.0.dev0`，运行使用 Python 3.12+、SQLite 标准库，以及固定 jsonschema 4.10.3；传递依赖由 analysis/uv.lock 锁定。Go/Bash 记录器不依赖此组件。存储需要 Python 内置 SQLite 3.37+ 与 Linux flock；检查点事务确认和投影留待 SLD-13～15。

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

原始 bytes、规范 hash、冲突和来源查询见[原始事实存储](../docs/source/development/raw-facts.md)。默认组件测试覆盖 20 项 V11、24 项 V09 与 28 项 V10/V16；`--task SLD-10` 可只回归身份层，`--task SLD-11` 可只验证存储，`--task SLD-12` 可只验证读取／隔离。

行预算、隔离查询、重处理与 schema 1 的显式升级见[有界读取与隔离](../docs/source/development/bounded-ingest.md)。本地事实／隔离持久化不代表检查点同事务或真实确认。
