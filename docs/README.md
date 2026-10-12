# Sledra 中文文档

文档采用 Sphinx、PyData Sphinx Theme 和 MyST-NB，源码位于 [source/](source/index.md)。从仓库根目录运行：

```bash
make setup
make docs
python3 -m http.server --directory build/docs/html 8000
```

访问 `http://localhost:8000`，或直接打开 `build/docs/html/index.html`。构建产物不纳入版本控制。

| 文档 | 内容 |
|---|---|
| [快速开始](source/quickstart.md) | 准备源码、构建和执行合成示例 |
| [安装](source/installation.md) | Linux 试用包、校验和及隔离验收 |
| [架构](source/architecture.md) | 组件分工、身份与原始记录 |
| [配置](source/configuration.md) | 环境变量、运行目录和 producer 配置 |
| [CLI](source/cli.md) | 帮助、版本、记录命令与错误分类 |
| [本地测试](source/testing-local.md) | 回归、冻结输入、演示和性能测量 |
| [HPC 测试](source/testing-hpc.md) | 指定二进制、共享目录与节点报告 |
| [开发](source/development.md) | 工具链、锁定依赖、公共命令和失败报告 |
| [文档维护](source/documentation.md) | 中文内容、MyST 编写和严格构建 |
| [CI](source/ci.md) | 公共质量门槛与远端验证边界 |
| [支持矩阵](source/support.md) | 实测环境和未验证分支 |
| [版本规则](source/versioning.md) | 软件、契约和阶段编号 |
| [工程交接](source/engineering-handoff.md) | M1.5 出口与 M2 输入 |
| [契约来源](source/contracts.md) | 规范权威来源与同步检查 |
| [迁移说明](source/migration.md) | 历史名称和路径映射 |

组件 README 保留在各源码目录，提供实现细节；试用包自带中文运行说明。真实共享/HPC 资格尚未验证。
