# Sledra 中文文档

Sledra 保存工作流的原始运行事实，提供独立 Go 记录器、Bash owner 钩子和可搬运的 Linux 试用包。当前 M1、M1.1 和 M1.2 的既定本地范围已通过验收，工程入口见[工程交接](development/engineering-handoff.md)。M2 已完成文件身份层与[原始事实存储](development/raw-facts.md)，状态投影与真实采集确认待后续任务。

首次使用从[开始使用](getting-started/index.md)选择试用包或源码方式；已有环境后，按[指南](guides/index.md)配置记录与运行验证工具。参与项目维护请阅读[开发](development/index.md)，查询架构、接口、契约和兼容性请阅读[参考](reference/index.md)。

要了解具体怎样使用，先读[独立示例详解](guides/standalone.md)，从请求 JSON、owner 脚本走到 context、事件和 finish；再读[在脚本中接入记录](guides/recording.md)，逐步调用 CLI 并确定自己的钩子位置。

```{toctree}
:titlesonly:
:maxdepth: 2

getting-started/index
guides/index
development/index
reference/index
```
