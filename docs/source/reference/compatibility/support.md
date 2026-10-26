# 支持与验证矩阵

当前证据覆盖合成记录和工程工具，每项仅代表实际执行范围。最低要求、交叉构建不等于兼容实测。维护者入口为 [Issues](https://github.com/cemc-oper/sledra/issues)，未承诺发行支持 SLA。

| 环境或能力 | 状态 | 证据与限制 |
|---|---|---|
| WSL2 x86_64/ext4；Go 1.27.0/1.27.2、Python 3.12.3、Bash 5.2.21、uv 0.11.2、Make 4.3 | 本地通过 | M1.1 使用 Go 1.27.0；M1.2 整体验收使用 1.27.2，含独立源码、组件与根公共入口、记录器/契约/Shell/M1 |
| WSL2 x86_64/ext4；GNU Bash 4.4.18、Python 3.12.3、现有 amd64 记录器 | 本地兼容回归通过 | 平台 probe 10 样本、Shell 对照、M1 合成流程、同机双角色模拟及 MCV forecast mock；CMA HPC 系统 Bash 4.4／共享卷／跨计算节点仍未验证 |
| Linux amd64 静态试用包，只读/含空格安装，无源码/Go/缓存/运行网络 | 本地通过 | namespace 包验收、M1 事实、Shell/原语/双角色/性能工具 |
| 显式 /dev/shm tmpfs 工作根 | 本地通过 | 10 组合成性能配对及真实 mount 报告，无 A6 批准阈值 |
| 同一 WSL 主机两个角色进程 | 工具通过 | 并发登记、相同 context、共享 spool 和 flock barrier；不代表真实跨节点 |
| Linux arm64 | 仅交叉构建 | 静态 ELF 和错误主机预检；native runtime 未验证 |
| 其他允许工具链组合 | 未验证 | 开发下限不等于全矩阵实测 |
| 中文文档 | 本地构建验证 | Sphinx + PyData Sphinx Theme + MyST-NB，zh_CN，Notebook 不自动执行；公开托管未配置 |
| GitHub Ubuntu 24.04 amd64 workflow | 静态/本地命令验证 | 真实 runner、cache/upload 和 namespace 配置未验证 |
| 真实计算节点、共享 HPC 与调度器 | 未验证 | 已有显式测试根和 Slurm 模板，须站点/节点窗口及03/25证据 |
| signal、MPI、透明 supervisor | 有限/未验证 | 仅当前 owner/Bash profile；INT 即时处理与 sledra exec 未交付 |
| A6、断电持久性和网络故障 | 未验证 | 本地性能/进程退出可见性不足以证明 |
| 公开发行、签名与完整离线升级/回滚 | 后续工作 | A7 和26独立收口 |
| 分析、查询、真实采集确认 | 尚未实现 | M2；隔离保留夹具不是采集器 |

最低/实测工具见[开发](../../development/workflow.md)，产物身份与运行依赖见[安装](../../getting-started/installation.md)，节点/mount 和命令边界见 [HPC 测试](../../guides/hpc.md)。[工程交接](../../development/engineering-handoff.md)保留兼容输入及未完成分支。
