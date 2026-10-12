# M1.5 工程交接

M1.5 已交付统一名称和独立说明、锁定 setup、共享构建/检查/回归入口、可搬运 CLI 与内嵌契约、静态试用包及隔离验收、参数化源码/安装测试、节点工具、CI 配置和问题/PR 模板。本地工程出口通过；远端 CI、真实 HPC、性能和发行资格保持独立分支，见[支持矩阵](support.md)。四份根协作文档按用户要求暂不考虑。

README 和组件/示例/测试/试用包说明现为中文，指南迁入 `docs/source/`；文档使用 Sphinx、PyData 主题、MyST-NB，以 `make docs` 严格构建。该文档调整不改变 M1 记录语义或启动 M2。

## 可复用输入

- 分别运行 setup、build、check、docs、test、test-platform、demo、package、test-package。公共入口保存阶段日志、失败摘要与保护字节检查，CI 复用这些命令。
- 包资源与运行 Registry/context/spool 分开；保留实际 archive 校验和、安装 manifest 和二进制版本。源码/安装测试可指定 bin、spec、工作父根和新报告目录。
- 契约仍为 1.0.0 开发草案。身份推导、规范字节、outbox、不可变 finish、显式 owner hook 和业务失败隔离仍是 M1 不变量。
- 兼容输入为 `tests/m1/results/handoff.json`、canonical 正反例和现有 replay 断言。

冻结 handoff SHA256：

```text
5c6c2bf40af052eedaf0edeb4ea02119f6d899824e086c8e26e210a436e282ef
```

快照有 22 份原始文件、2 Run、4 Attempt、6 context、11 过程事件和 8 outbox；正常/失败/重试 payload 状态为 0/7/0。finish 后仅注册的 Attempt 不在旧重试 Run 的不可变覆盖集合内，不能据此虚构已完成处理。

## M2 边界

M2 可开始合成本地开发，使用唯一 SLD-10/11 任务卡；实际实施时再创建独立 analysis Python package，目前尚不存在。先回放冻结输入，再实现 source UUID/generation 和 raw 存储、去重、冲突保留；12/13 后续补隔离与事务检查点。真实确认和09生产清理依赖13，夹具确认不是数据库事务。

普通测试不替换冻结 handoff。维护者显式要求刷新时，需要新旧摘要、原始身份/事件审查及 M2 输入再评估；候选先生成到新报告目录，审查后同步全部摘要引用。平台、性能和发行条件限制各自分支，不阻断 M2 合成本地开发。

## 待验证分支

远端 Actions 需要真实运行链接与环境证据。真实跨节点/HPC 需要站点参数及计算节点窗口。A6 需要批准预算和目标测量；A7 仍属后续许可/发行决定。SLD-26 扩展到分析产物、离线升级/回滚与备份恢复。真实业务材料、集成和 M4 尚未完成。联系入口为 [Issues](https://github.com/cemc-oper/sledra/issues)。
