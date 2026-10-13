# 记录配置

记录器内嵌默认契约，不需要源码资源或安装目录写权限。直接调用时，为输入与运行数据提供显式绝对路径。[帮助与版本](../reference/cli.md#帮助与版本)无需环境配置。

首次使用先按[独立示例详解](standalone.md)理解配置如何进入 owner 和 producer；自己的 Run/Attempt 请求及调用顺序见[脚本接入说明](recording.md)。本页集中列出环境配置。

| 变量 | 当前用途 |
|---|---|
| `SLEDRA_ENABLED` | Bash 钩子仅在值为 `1` 时启用；关闭后无副作用 |
| `SLEDRA_BIN` | Bash API 使用的绝对二进制路径 |
| `SLEDRA_ROOT` | 已存在、可信的真实目录，保存 Registry、context、document、finish 和 spool |
| `SLEDRA_SPEC_DIR` | 可选的绝对兼容契约目录；未设置或为空时使用内嵌资源 |
| `SLEDRA_CONFIG` | 事件 producer 的绝对 JSON 配置路径 |

producer 配置固定 `producer_role`、`producer_name`、`producer_version`、`source_id` 和 `session_id`。并发 producer 使用不同 session。可选分段、session 与全树容量限制见源码 [spool 说明](https://github.com/cemc-oper/sledra/blob/main/recorder/spool/README.md)。独立示例的 process/stage 配置是合成输入，不是生产预算。

安装资源与运行数据分开。Run/Attempt context 在同一运行根目录内采用固定的不可变绝对位置。请求字段与错误分类见 [CLI](../reference/cli.md)及源码 [context 说明](https://github.com/cemc-oper/sledra/blob/main/recorder/context/README.md)。成功的生命周期调用不输出 stdout；business 模式事件错误尽力记录诊断，strict 模式返回错误。

独立示例按脚本位置查找 Shell 库、配置和夹具，写入显式的 `STANDALONE_WORK_DIR`。输入资源默认相对该示例目录；直接调用者须自己管理输入路径。HPC 测试还须提供共享卷上的独立、可写 `SLEDRA_ROOT`，不能写入安装目录。
