# CE-1 目标节点预检与现场验收

本流程由用户在 HPC 手动分段执行，分析侧核验回传证据。预检、本地测试或登录节点运行均不代表 CE-07/A15 通过。旧 P3 试点、suite 和安装继续留档；本轮使用独立安装、共享测试根、独立私有 runtime 和隔离 suite。

## 1. 确定本次现场

先回传主机/内核/Bash、调度器工具、当前 allocation 信息及本次独立共享控制目录。不导出整个环境，不回传 ECF_PASS、令牌或其他凭证。明确本次分区、wckey/account、资源和时限；不能从旧配置推断当前提交范围。

需要落实四个绝对路径：独立的试用安装、已有的独立共享测试父目录、预创建的私有 runtime（本次与记录根均在共享文件系统，使用分离目录）、新建的报告目录。runtime 必须 0700、同 UID，所有祖先不可被其他用户写入且不含 symlink；sticky `/tmp` 也不合格。runtime 还必须位于业务、归档、清理、记录、安装和证据树之外。显式选择的控制挂载须验证稳定 inode、原子 rename/renameat2(NOREPLACE)、O_EXCL 和非阻塞 flock，跨节点可见性单独验证。一个 Execution 的 owner/producers 仍在同一节点，共享路径不改变 boot/PID 身份限制。

GFS 验收另外需要真实 program/input/static 配套路径、日期/时次/时区、代表产品与字段规则、三状态独立输出目录、ecFlow server/port 和本次隔离 suite。旧 P3 历史回执不证明新的 CE-1 路径已运行。

## 2. 运行只采集信息的预检

[child-events-preflight.sh](child-events-preflight.sh) 从源码或试用包任意 cwd 执行，不需要 Python、Go、jq 或 ecFlow。除 Bash >=4.4 外需要 Linux procfs 和 GNU/coreutils 的 date、uname、id、stat、sha256sum、cmp、df、cat、mkdir，以及 util-linux findmnt。

在现场路径明确且新安装已解包后调用；四个变量必须由操作者显式赋值，不自动套用旧 P3：

```bash
bash "$CE07_INSTALL/share/sledra/examples/hpc/child-events-preflight.sh" \
    "$CE07_INSTALL" "$CE07_SHARED_PARENT" "$CE07_RUNTIME_DIR" "$CE07_NEW_REPORT_DIR"
```

工具只新建报告目录，拒绝覆盖已有报告或向安装/runtime 内写报告。核对安装的 SHA256SUMS、两类库、可执行二进制及精确 CE-1/协议1 capability 回执；检查 runtime 权限链，采集节点/内核/UID、boot ID、PID namespace、collector proc stat、挂载选项、可用字节和 inode。不会初始化记录根、创建控制会话、修改 runtime、提交或取消作业、删除文件。失败时保留已生成报告；返回非零后先处理失败，不继续后续阶段。

成功报告也明确输出 `a15=unverified`、`storage_semantics=unverified`、`shared_visibility=unverified`。预检没有执行业务 API，也没有证明原子替换、竞争、取消或产品正确性。容量信息仅作采样，不通过填满共享卷验证 ENOSPC；故障测试必须在另行约定的受限空间完成。

## 3. 计算节点上的 CE-1 功能与文件系统证据

以本轮隔离 allocation 的计算节点为准，本轮 runtime 与记录均使用共享文件系统中的独立目录，不使用节点特定的存储路径；每个 Execution 仍以随机 session 和节点身份隔离。保留实际挂载和路径。先按现场确认的路径新建记录根，再运行完整 [owner/business 示例](../execution-child-events/owner.sh) 的 inline/external。清除旧模式开关 `SLEDRA_ENABLED` 和继承的 `SLEDRA_EVENT_CONTEXT`，显式提供 mode、binary、root 和两类库路径。每种健康调用预期一条 Execution，依次 started/context、lead 0/3/6/9/12 的五条事实、ended；业务输出与 off 对照。

现场验收必须继续收集以下证据，不能凭预检或通用旧版 probe 勾选：

| 项目 | 必需证据 |
|---|---|
| 身份与存储 | allocation/job/node、mount、UID/boot/namespace、描述绑定、记录配置及安装摘要；跨节点可见性、共享控制目录部署依据与跨节点引用拒绝结果 |
| 原子状态替换 | 实际 CE-1 运行的 descriptor/lock inode 固定、state revision/phase 变化及完整 JSON；并发读写未产生混合快照 |
| 锁与结束 | 同一 Execution 多 producer 竞争的 Busy 回执与零重试、已接纳事实完整且不交错、关闭后无新事实、不同 Execution 互不串锁 |
| TERM/KILL/父死亡 | 仅本轮测试 PID/进程组的操作记录；父实际信号与子143分列；KILL 不伪造 ended；陈旧引用失败且不重建目录/执行 |
| 调度器取消 | 仅本轮新建测试作业的 job ID、提交配置、取消命令、调度器最终状态/退出码、节点日志、记录和业务回调；不取消历史/他人作业 |
| 容量与维护 | runtime 专用容量/监测/负责人；受限空间或配额故障的独立依据；停止新入口后核验 owner/boot/starttime、非阻塞独占原锁，旧引用先失效再清理，记录字节不变。TTL/closed 单独不足以允许清理 |

`tests/platform/probe.py` 和 `cross_node.py` 主要验旧记录流程，不能替代本表的 CE-1 实际状态/锁/身份验收。进程崩溃不证明掉电持久性；不能对共享卷做未限定的填盘或清理。

## 4. GFS / ecFlow 与回传

真实 GFS 输入/产品和真实 ecFlow 回执分开记录。off/on/fault 保持同一业务配置与输入，独立目录，比较实际次数、argv、退出结果、业务日志、产物与回调；initial 只运行一次完整脚本，0/3 就绪后 6 失败时当时已落盘的两条事件保留。保持当前 enabled+external 生成限制，不以 standalone external 成功解除它。产品字段规则未落实的部分继续 unverified。

只回传允许共享的安装/源码摘要、精确命令/退出码、作业与节点元数据、记录与脱敏回调、场景报告和已结束合成会话的必要取证副本。不要复制真实凭证或整个业务环境。计算节点原始结果、后续旧 reader 查询和业务数据检查各自有明确来源，成功/失败/未执行分列。所有适用现场项有证据后才可判 CE-07/A15；正式发行与部署资格仍单列。
