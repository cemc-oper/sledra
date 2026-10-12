# M1 本地阶段交接

源码根setup后使用test执行回归/冻结回放、demo生成新快照/回放、benchmark保存新性能样本，见[开发](../../docs/source/development.md)。单项脚本需要Go与锁定Python测试依赖，只用合成输入、私有临时目录和本地卷，不提交job、启动分析/数据库或删除既有spool。

```sh
PYTHONDONTWRITEBYTECODE=1 python3 tests/m1/verify.py
PYTHONDONTWRITEBYTECODE=1 python3 tests/m1/replay.py tests/m1/results/handoff.json
PYTHONDONTWRITEBYTECODE=1 python3 tests/m1/benchmark.py --samples 20
```

verify统一调用时复用SLEDRA_TEST_BIN_DIR，独立调用则离线私有构建。创建正常Run和首Attempt失败、retry_of第二Attempt成功的Run；逐项对比无观测基线stdout/stderr、退出码、产品和一次payload。每个Run有不可变finish；retry finish后再登记仅注册Attempt，证明旧声明不变，needs_finalization留M2解释。核对outbox/过程类型、context父ID、完整LF及固定路径。

[handoff.json](results/handoff.json)冻结保存Registry/context/document/finish/spool相对路径、SHA256、原UTF-8、期望身份和冲突例来源。replay逐摘要恢复新树，用当前记录器验证6个context、11过程及8outbox；open段无真实采集确认。该raw输入用于未来10～13，不证明分析/采集已实现。

benchmark交替启用/关闭顺序，每对新目录断言业务输出字节相同。[历史性能](results/overhead-local.json)有20组纳秒样本和p50/p95/p99/max，包括完整Shell增量、Run/Attempt登记、单次event及首次event。含Go启动/fsync成本，只代表本地实测，无A6批准阈值，CPU/RSS、真满盘和跨节点延迟另测。

仅维护者显式要求并按[工程交接](../../docs/source/engineering-handoff.md)审查后刷新：verify的--handoff-out与benchmark的--out先指向新候选目录，批准后同步历史摘要引用。新UUID/时间会改变handoff摘要，即使断言不变。公共[工作/报告参数](../../docs/source/testing-hpc.md)记录实际卷，夹具platform/deployment只是合成身份。
