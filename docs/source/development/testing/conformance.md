# M2 核心故障回归门槛

SLD-23 的 M2 核心集组合 Go 记录器、190项分析组件回归、29项端到端故障场景、4项门槛反例和6项显式 Shell owner 对照。独立执行入口为 `make test-conformance`；根 `make test` 也执行同一门槛，再运行工程、包、平台harness、契约、CLI和M1回归。分析组件仍可单独运行 `make -C analysis test`。

## 运行和保留证据

工具和缓存准备方式沿用[开发流程](../workflow.md)。从仓库根目录执行：

```bash
make setup
make test-conformance
make test CONFORMANCE_REPORT_DIR="$PWD/build/sld-23-new"
```

默认使用私有临时现场，成功清理、失败保留并显示路径，不自动创建通用报告树。`CONFORMANCE_REPORT_DIR` 必须是不存在的新目录；建议位于忽略的 build 目录或仓库外。含空格路径受支持。显式报告目录保留 validation.json、matrix.json、go-test.jsonl、TestSession 的 summary/commands，以及 work/core 中的输入、实际数据库、归档／备份、恢复库和差分文件。

复用已构建二进制也可直接运行：

```bash
SLEDRA_TEST_BIN_DIR="$PWD/build/bin" uv run --project analysis --locked --offline python tests/conformance/verify.py --report-dir /tmp/sledra-core-new
```

没有显式二进制时沿用 TestSession 的本地离线构建；显式指定但缺失不会静默重建。当前门槛只接受 local 模式，不能用它启动 HPC 故障注入。报告锁定本次实际源码集合 SHA256、Git HEAD、Go/Bash/Python/SQLite、二进制及规范摘要；测试期间源码变动会拒绝通过。未运行其他仓库的业务代码，跨仓库版本资格留待对应接入任务。

## 端到端场景

私有 fixture 从冻结 M1 handoff 的22份原始 bytes 恢复19条事实，校验每份摘要。历史 open writer 在私有副本中不存在，因此只修改副本 locator 为 sealed，内容不变；这不是生产 open 段封存策略。manifest 保存原路径、测试路径、SHA256以及每个 event_id@canonical_hash 的 byte_start/byte_end 和 JSON pointer。

`flow.py` 将 AtomicIngestor、ProjectionEngine、MetricsEngine、RecoveryStore 的真实接口连成一条链：接入全部 Registry/outbox 和段 → 状态重建 → 时间报告 → 逐段原始归档／一致备份／确认 → 覆盖全部回执的最终备份。整个链只操作刚创建的私有目录。

19个子进程 SIGKILL 切点覆盖：

| 部分 | 切点 |
|---|---|
| ingest | after_records、after_checkpoint、before_commit、after_commit |
| projection | after_input_snapshot、after_revision、after_work_cursor、before_projection_commit、after_projection_commit |
| metrics | after_input_snapshot、after_report、after_head、after_commit |
| recovery | after_raw_archive、after_archive_commit、after_snapshot、after_backup_manifest、before_ack、after_ack |

每个切点必须实际得到 `-SIGKILL`，保存 killed-at.json；先检查恢复前 SQLite 完整性、外键及已有回执，随后按 seed 23 打乱并重复输入。接入前提交失败不留下事实／检查点；恢复后的事实版本集合、两类派生 ID/data和检查点偏移必须与无故障基线完全相同。每个恢复结果再做一致备份与新库恢复。

另外验证源确认后仅删除私有段，恢复备份及从 raw archive 重建空数据库；过旧快照、损坏归档及备份失败不能获得有效恢复或新回执。缺尾、坏 UTF-8 和冲突版本给出具体 byte range，不能静默跳过。状态和时间 head 可独立滞后接入水位，重建后收敛。

新通用 mock 只模拟一次接受，随后本地接受事件提交失败；payload.started 已在接受观察之前接入。重放证据不会再次调用提交动作，迟到 Job 正确关联。实际 Shell 对照只执行仓库合成 payload，在分析库尚不存在时分别运行关闭／启用观测的成功和失败样本；stdout、stderr、退出码、执行次数、callback顺序及产品 bytes 相同，再离线接入并查询。没有 scheduler 提交或生产业务执行。

## 矩阵和判定

`matrix.py` 是版本化的 V01–V22 核心断言清单。每行有 core.scope、core.status、必需用例ID及实际状态，还有 extension.status 和未验证原因。必须执行当前用例，不读取前序任务的 pass 标志作为本轮结果。Go 测试以 `-count=1 -json` 运行，包级失败同样阻止通过。

缺少必需用例、skip、失败、未结束或重复身份都不能通过；4项门槛反例验证这些路径。所有 Python/Go 套件也必须成功，源码及冻结输入摘要保持一致。任何非矩阵映射用例失败仍会使整个命令非零退出。

`core_matrix_pass=true` 只表示每行声明的本地核心子范围通过。整行包含尚未运行的接入／平台分支时保留 status=unverified，`full_matrix_pass` 始终为 false；不能把22个核心子范围的通过改称 V01–V22 全量通过。测试报告还保留 Shell 的 rc/callback对照、诊断摘要及事件类型。

已有19个接入、4个状态发布和4个时间发布进程切点继续执行；新增19个端到端切点及1个备份失败前置切点，报告分别列出，避免重复冒充独立故障覆盖。SIGKILL不能证明硬件掉电耐久性。

## 失败定位和边界

validation.json 的 cases 定位用例与工作目录；inputs.json 列出每条预期事实的原始位置，event-diff.json 列 missing/extra集合，fault-locations.json 给坏行／pending range，killed-at.json 给实际切点。result.json 保留实际两类投影和回执；mock-submit-result.json、business-diff.json 对应提交及业务对照。报告记录关键保留文件摘要，源 bytes 与原始事实仍可复查。

真实 takflow/ecFlow/MCV 回调、历史许可样本、产品预期分母／交付规则、生产确认分发与清理、真实共享FS／节点丢失／高延迟、HPC、远端CI、A6/A7另验。当前 Bash 对照确认 INT 在既有 profile 中延迟，**即时 INT 未获资格**；测试保留这一限制，不能把后续强制KILL当 INT 处理成功。通用 supervisor 仍未提供。SLD-20/27专门适配不属于首期核心门槛。
