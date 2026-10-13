# 指南

已有记录器后，先理解 owner 怎样登记身份、启动业务和记录退出，再配置自己的运行目录与 producer，按目标环境核对结果。

[独立示例详解](standalone.md)串起输入 JSON、Shell 调用、正常/失败路径和输出核对。[在脚本中接入记录](recording.md)给出可逐步执行的 CLI 流程，以及已有 owner 的钩子位置、令牌、重试和结束覆盖管理。记录配置说明环境变量及安装资源与运行数据的分离；本地与 HPC 验证工具说明测试参数与平台流程。

```{toctree}
:titlesonly:
:maxdepth: 1

standalone
configuration
recording
hpc
```
