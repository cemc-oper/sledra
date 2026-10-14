# Sledra 离线分析组件

当前交付 SLD-10 的文件身份、世代和 offset 模型，独立发行名为 `sledra-analysis`，开发版本 `0.1.0.dev0`，运行仅依赖 Python 3.12+ 标准库。Go/Bash 记录器不依赖此组件。SQLite、坏行隔离、事务确认和投影留待 SLD-11～15。

从仓库根目录执行：

```bash
make setup
make -C analysis build
make -C analysis check
make -C analysis test
```

根 `make build/check/test` 同时协调该组件；`OUTPUT_DIR` 控制构建目录，wheel 和 sdist 默认为 `build/analysis/`。分析运行依赖为空；构建 backend setuptools 80.9.0 固定在元数据与独立 `uv.lock`，setup 同步并准备缓存，日常构建离线。根项目继续管理 Ruff 等开发工具，根 check 不把记录器作为 Python 依赖。

普通测试在新临时目录注入轮转与改写，不修改冻结 M1 样本，不自动创建报告。需要保存逐项结果时显式指定不存在的私有目录：

```bash
PYTHONPATH=analysis/src python3 analysis/tests/verify.py --report-dir /tmp/sledra-v11-new
```

接口、offset 使用方式与完整性限制见[文件身份与世代](../docs/source/development/source-identity.md)。
