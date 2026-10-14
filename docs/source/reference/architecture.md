# 记录架构

Sledra 采用一个产品、一个仓库、多个组件的结构：Go 实现记录，Bash 将显式钩子接入已有 owner，JSON 契约定义身份和事件。Python 当前负责开发、文档和测试工具；离线分析组件属于 M2。

生命周期 owner 使用显式 launch token 创建 Run，再用 execution token 为任务实例创建 Attempt。[Registry](https://github.com/cemc-oper/sledra/blob/main/recorder/internal/registry/README.md)原子发布不可变权威记录及注册 outbox。相同请求重复提交复用身份，同 token 异内容为冲突；新的业务尝试使用新 token，可明确关联重试父级。

[context 服务](https://github.com/cemc-oper/sledra/blob/main/recorder/context/README.md)在调用方选择的绝对路径发布不可变 context。消费者校验确切父记录和摘要，不通过“某周期最新 Run”猜测身份。context 绑定的事件调用校验关联、编码规范字节，再追加到 producer session 的 spool。

[Bash API](https://github.com/cemc-oper/sledra/blob/main/integrations/bash/README.md)只定义函数。owner 保留自己的选项、trap、callback 和 payload 执行，先保存原始退出码或信号，再记录事实。身份失败会关闭后续观测；事件失败遵循 business/strict 模式。重试观测不得重跑 payload。

Run finish 是单独的声明，冻结显式覆盖的 Attempt 集合。之后仍可登记新 Attempt，但不得改写旧声明。完整性/成功分析、真实采集确认和生产保留策略尚未实现；精确身份字节、事件权威及版本规则见[契约来源](contracts.md)和源码 `spec/README.md`。

Registry、context、document 和 spool 共用 `SLEDRA_ROOT`。跨节点消费者须在已验证的共享文件系统上使用相同的绝对 context 路径；本地 WSL/ext4 和 tmpfs 结果不代表共享/HPC 部署资格。
