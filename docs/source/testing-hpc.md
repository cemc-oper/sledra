# 本地与 HPC 测试参数

源码与 Linux 试用包提供相同标准库 Python 工具。TEST_HOME 指向源码根或安装的 share/sledra，SLEDRA 指向已有 native 二进制，支持任意 cwd。测试需 Python >=3.12、Bash >=5.2、dirname/cat/sleep；Shell 还需 grep，单节点原语需 Linux flock/fsync/hardlink、findmnt 等工具。显式 bin 不需 Go，未提供 bin 的源码调用使用共享覆盖或独立离线构建。

| 参数 | 含义 |
|---|---|
| --bin | 已有 native sledra；缺失/错误架构失败，不重建 |
| --spec-dir | 兼容契约覆盖，默认工具旁资源 |
| --work-root | 被测卷上已有父目录；HPC 必须显式提供 |
| --report-dir | 新报告目录，不覆盖已有结果 |
| --keep-work | 保留成功的唯一工作子目录；失败始终保留 |
| --mode local/hpc | 测试意图，不代表平台资格；默认 local |

每次只创建自己拥有的唯一子目录，保存 Registry/context/spool/request/基线产品和临时文件，记录绝对 context 路径。成功清理只删除该子目录，保留父根、其他运行和报告；测试不使用生产数据。权限/依赖预检失败时尽可能在新的可创建报告目录写 summary；语法错误或报告目录不可写/已存在时可能无法创建报告。

```bash
python3 "$TEST_HOME/tests/m1/verify.py" --bin "$SLEDRA" \
  --work-root "$TEST_PARENT" --report-dir "$REPORT_PARENT/m1-new" --keep-work
python3 "$TEST_HOME/tests/m1/replay.py" "$TEST_HOME/tests/m1/results/handoff.json" \
  --bin "$SLEDRA" --work-root "$TEST_PARENT" --report-dir "$REPORT_PARENT/replay-new"
python3 "$TEST_HOME/tests/shell/verify.py" --bin "$SLEDRA" \
  --work-root "$TEST_PARENT" --report-dir "$REPORT_PARENT/shell-new"
python3 "$TEST_HOME/tests/platform/probe.py" --bin "$SLEDRA" --samples 10 \
  --work-root "$TEST_PARENT" --report-dir "$REPORT_PARENT/primitives-new" --keep-work
python3 "$TEST_HOME/tests/m1/benchmark.py" --bin "$SLEDRA" --samples 10 \
  --work-root "$TEST_PARENT" --report-dir "$REPORT_PARENT/timing-new" --keep-work
```

计算节点单节点测试加 --mode hpc，不回退节点 /tmp；probe 的 --parent 仍是 work-root 别名。报告保存实际 mountinfo、架构、主机、工具版本、源码清单或安装 manifest 摘要、二进制 SHA/version、命令、样本和原因。性能保留原始纳秒及分位值，无批准预算不判 A6 通过；进程崩溃不证明断电持久性。压缩包校验和需单独保留，解包树不能反推原 archive。

## 双角色与共享存储

先本地运行：

```bash
python3 "$TEST_HOME/tests/platform/cross_node.py" simulate --bin "$SLEDRA" \
  --work-root "$TEST_PARENT" --report-dir "$REPORT_PARENT/simulation-new" \
  --samples 8 --timeout 60 --keep-work
```

本地只计工具通过，real_cross_node 为 unverified。--omit-role b --timeout 1 注入失联；并发调用须分别使用新报告目录，工作树与身份独立。

真实节点先使用 cross_node.py prepare --mode hpc 和公共路径，保留准备树及打印的 run_directory。在不同节点以相同绝对准备路径、可见安装/spec/native bin 分别启动：

```bash
python3 "$TEST_HOME/tests/platform/cross_node.py" worker --run-dir "$RUN_DIR" --role a
python3 "$TEST_HOME/tests/platform/cross_node.py" worker --run-dir "$RUN_DIR" --role b
```

两个 worker 退出后，即使失败也收集：

```bash
python3 "$TEST_HOME/tests/platform/cross_node.py" collect --run-dir "$RUN_DIR" \
  --report-dir "$REPORT_PARENT/collection-new" --timeout 60
```

角色只能独占领取一次，有界等待 readiness/registration/lock barrier，并发重复相同 Run/Attempt 请求，比较身份、校验同一绝对 context，写共享 spool。A 持锁，B 必须观察排斥并在释放后取得锁。收集检查两份回执、context 字节/路径、恰好两份 Registry 记录和过程事件；回执保存节点、mount、二进制、命令和样本。收集保留工作树，失联/失败非零退出并纳入已收到的结果。不同 node/machine ID 只为这些操作提供跨节点证据，完整 HPC 资格仍单列；仅 prepare 的执行状态为 unverified。

站点模板位于源码 examples/hpc/ 或安装 share/sledra/examples/hpc/，分开准备、审查/提交与收集，scheduler/partition/account 由配置提供。当前仅 Slurm 渲染，准备工具不提交作业。实际测试保留 allocation/job/node 信息，在计算节点按站点窗口执行。本地开发未访问 HPC；03/25负责平台资格，32只交付工具和本地验证。
