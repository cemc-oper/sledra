# Sledra

Sledra 为工作流保存可追溯的运行事实。当前提供独立 Go 记录器、Bash 生命周期钩子、版本化 JSON 契约、合成示例和 Linux 本地试用包。

M1 独立记录与 M1.1 工程化的既定本地范围已通过验收。离线分析、查询、真实采集确认及透明 `sledra exec` supervisor 尚未实现；M2 为后续分析方向，阶段范围和顺序随新增工程需求及使用反馈调整。

## 项目结构

| 目录 | 用途 |
|---|---|
| [recorder](recorder/context/README.md) | Go 记录器：Registry、context、事件、spool 和隔离保留夹具 |
| [shell](shell/README.md) | 由明确的生命周期 owner 调用的 Bash API |
| [spec](spec/README.md) | 1.0.0 契约草案、生成脚本和合成正反例 |
| [examples](examples/standalone/README.md) | 独立示例与 HPC 站点模板 |
| [tests](docs/source/development/testing/local.md) | 契约、Go、Shell、M1 和本地文件系统回归 |
| [docs](docs/README.md) | Sphinx + PyData Sphinx Theme + MyST-NB 中文文档 |

安装资源与运行数据分开放置。Registry、context、document、finish 和 spool 共用显式的 `SLEDRA_ROOT`。当前证据覆盖 WSL/ext4 和明确登记的 tmpfs 测试；共享/HPC 部署仍需验证目标文件系统。

## 源码快速开始

先安装[开发工具链](docs/source/development/workflow.md#工具链)，再从仓库根目录执行：

```bash
make setup
make build
make check
make test
make demo
```

`make setup` 是显式联网步骤，支持空依赖缓存。日常命令使用已准备的缓存离线运行，不提交 HPC 作业。构建产物位于 `build/bin/`，每次检查、测试或演示的日志与摘要保存在独立的 `test-results/` 子目录。

具体使用方式见[独立示例详解](docs/source/guides/standalone.md)：说明请求 JSON、owner/payload 分工、钩子顺序、失败路径以及生成文件的核对。随后按[脚本接入说明](docs/source/guides/recording.md)逐步调用 CLI，并确定自己业务脚本中的生命周期边界。

## 安装与试用

```bash
make package
make test-package
```

[安装指南](docs/source/getting-started/installation.md)说明试用包校验、用户目录安装、独立运行和隔离验收。[CLI 指南](docs/source/reference/cli.md)说明帮助、版本、默认内嵌契约及兼容资源覆盖。`make help` 显示公共入口。

## 中文文档

```bash
make setup
make docs
python3 -m http.server --directory build/docs/html 8000
```

在浏览器打开 `http://localhost:8000`。文档源码位于 `docs/source/`，HTML 生成到 `build/docs/html/`；构建将警告视为错误，不自动执行 Notebook。文档依赖只用于开发，不成为 Go/Bash 记录器的运行依赖。仅构建文档的依赖安装方法见[文档维护](docs/source/development/documentation.md)。

[文档首页](docs/source/index.md)按四个顶层章节组织：

| 章节 | 阅读目标 |
|---|---|
| [开始使用](docs/source/getting-started/index.md) | 安装试用包或从源码运行第一个示例 |
| [指南](docs/source/guides/index.md) | 理解独立示例、接入脚本、配置记录与验证工具 |
| [开发](docs/source/development/index.md) | 开发、测试、维护文档及核对工程交接 |
| [参考](docs/source/reference/index.md) | 查询架构、CLI、契约及兼容性资料 |

问题联系入口为 [Issues](https://github.com/cemc-oper/sledra/issues)。

真实 HPC、跨计算节点、批准的性能预算和公开发行资格仍未验证。原始契约和冻结 M1 样本继续保持兼容性约束。历史名称与路径见[迁移说明](docs/source/reference/compatibility/migration.md)。
