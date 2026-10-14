# M1.2 工程交接

M1.1 已交付统一名称和独立说明、锁定 setup、共享构建/检查/回归入口、可搬运 CLI 与内嵌契约、静态试用包及隔离验收、参数化源码/安装测试、节点工具和 CI 配置。M1.2 已完成组件构建、控制台输出、打包文档归并、Bash 接入目录迁移及整体验收。既定本地工程出口通过；远端 CI、真实 HPC、性能和发行资格保持独立分支，见[支持矩阵](../reference/compatibility/support.md)。四份根协作文档按用户要求暂不考虑。

README 和组件/示例/测试/试用包说明现为中文，指南迁入 `docs/source/`；文档使用 Sphinx、PyData 主题、MyST-NB，以 `make docs` 严格构建。该文档调整不改变 M1 记录语义或启动 M2。

M1.1 为原 M1.5 工程化阶段的新编号。既定验收保留，新增工程工作按反馈拆分；后续阶段可插入、拆分、合并或调整顺序，不因本地出口通过自动开始 M2。阶段编号与软件、契约版本无关。

## 可复用输入

- 分别运行 setup、build、check、docs、test、test-platform、demo、benchmark、package、test-package。公共入口直接显示过程，失败返回非零，并检查保护文件；默认不保存阶段日志或通用摘要，CI 复用这些根命令。
- 根 Makefile 协调公共任务，recorder/analysis/docs 组件可独立调用；`OUTPUT_DIR` 统一控制根及组件产物位置，支持含空格路径。默认使用根 `build/bin/`、`build/docs/html/` 和分架构的 `build/package/linux-<arch>/`，最终包在 `dist/`，可用 `DIST_DIR` 覆盖。Go 构建使用同一配方且不启动 Python/uv。
- 验证工具通过显式 `--report-dir` 保存平台/安装资格数据；性能原始样本通过 `SLEDRA_BENCHMARK_OUT` 保存。demo、平台和包验收使用临时现场，成功清理、失败打印并保留路径。历史 `test-results/` 保留历史含义。
- 安装、运行及打包权威说明位于 `docs/source/`；包内说明由此导出，仅引用包内资源。Bash 库在源码 `integrations/bash/sledra.sh`、安装包 `share/sledra/integrations/bash/sledra.sh`，旧路径不提供别名，见[迁移表](../reference/compatibility/migration.md)。
- 包资源与运行 Registry/context/spool 分开；保留实际 archive 校验和、安装 manifest 和二进制版本。源码/安装测试可指定 bin、spec、工作父根和新报告目录。
- 契约仍为 1.0.0 开发草案。身份推导、规范字节、outbox、不可变 finish、显式 owner hook 和业务失败隔离仍是 M1 不变量。
- 兼容输入为 `tests/m1/results/handoff.json`、canonical 正反例和现有 replay 断言。

冻结 handoff SHA256：

```text
5c6c2bf40af052eedaf0edeb4ea02119f6d899824e086c8e26e210a436e282ef
```

快照有 22 份原始文件、2 Run、4 Attempt、6 context、11 过程事件和 8 outbox；正常/失败/重试 payload 状态为 0/7/0。finish 后仅注册的 Attempt 不在旧重试 Run 的不可变覆盖集合内，不能据此虚构已完成处理。

## M2 边界

M2 已实施 SLD-10～12，创建独立 `analysis/` Python package、组件入口与 V11 测试，并纳入根 build/check/test。冻结身份回放及本地世代/冲突验证结果见[文件身份与世代](source-identity.md)。SLD-11 已交付[SQLite raw 事实、去重与冲突查询](raw-facts.md)，真实数据库回放保存 19 条过程/outbox 事实，V09 24 项通过；SLD-12 已完成[有界读取／隔离与重处理](bounded-ingest.md)，28 项 V10/V16 通过；13 检查点同事务仍待实施。真实确认和09生产清理依赖13，内存位置与夹具回放不是数据库事务。

普通测试不替换冻结 handoff。维护者显式要求刷新时，需要新旧摘要、原始身份/事件审查及 M2 输入再评估；候选先生成到新报告目录，审查后同步全部摘要引用。平台、性能和发行条件限制各自分支，不阻断 M2 合成本地开发。

## 待验证分支

远端 Actions 需要真实运行链接与环境证据。真实跨节点/HPC 需要站点参数及计算节点窗口。A6 需要批准预算和目标测量；A7 仍属后续许可/发行决定。SLD-26 扩展到分析产物、离线升级/回滚与备份恢复。真实业务材料、集成和 M4 尚未完成。联系入口为 [Issues](https://github.com/cemc-oper/sledra/issues)。
