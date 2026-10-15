# M2 核心故障回归

从仓库根目录运行 `make test-conformance`；根 `make test` 也执行同一门槛。通过 `CONFORMANCE_REPORT_DIR` 指定新目录可保留逐项矩阵、原始输入、SQLite、SIGKILL切点和差分结果。

范围、复现和失败定位的权威说明见[核心故障回归门槛](../../docs/source/development/testing/conformance.md)。只验证本地合成核心集，M3/M4及真实平台分支另列unverified。
