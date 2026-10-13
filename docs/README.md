# Sledra 中文文档

文档采用 Sphinx、PyData Sphinx Theme 和 MyST-NB，源码位于 [source/](source/index.md)。从仓库根目录运行：

```bash
make setup
make docs
python3 -m http.server --directory build/docs/html 8000
```

访问 `http://localhost:8000`，或直接打开 `build/docs/html/index.html`。构建产物不纳入版本控制。

| 顶层章节 | 内容 |
|---|---|
| [开始使用](source/getting-started/index.md) | 试用包安装、源码快速开始 |
| [指南](source/guides/index.md) | 独立示例详解、脚本接入、记录配置、本地与 HPC 验证工具 |
| [开发](source/development/index.md) | 开发流程、测试与验证、文档维护、工程交接 |
| [参考](source/reference/index.md) | 架构、CLI、契约来源、兼容性与迁移 |

开发中的“测试与验证”分为本地回归和 CI；参考中的“兼容性与迁移”分为支持矩阵、版本规则和路径迁移。各章节首页再导航到具体页面。

具体操作从[独立示例详解](source/guides/standalone.md)开始：理解文件与输入、调用顺序、成功/失败行为和输出核对，再按[在脚本中接入记录](source/guides/recording.md)练习 CLI 与已有 owner 的接入。

组件 README 保留在各源码目录，提供实现细节；试用包自带中文运行说明。真实共享/HPC 资格尚未验证。
