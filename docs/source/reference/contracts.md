# 契约来源

版本化说明见源码 [spec/README.md](https://github.com/cemc-oper/sledra/blob/main/spec/README.md)、`spec/1.0.0/`、合成向量及组件 README。契约 1.0.0 是开发草案，与软件发行或阶段编号分开。

## 权威来源

对象与事件语义的作者权威来源仍是工作区 `observability/sledra-devel/docs/observability-plan/03-information-contracts.md`，设计决策位于相邻 `adr/`。这些文档不是独立源码的运行或阅读依赖。仓库指南描述当前实现，不建立第二套独立演化的权威定义。

`docs/contracts-sources.json` 保存本批指南已复核的来源路径和 SHA256，只记录来源，不是第二份契约。作者工作区运行：

```bash
python3 scripts/check_docs.py --plan-dir /absolute/path/to/docs/observability-plan
```

摘要不匹配时审查上游变化，必要时一起更新受影响指南、schema、生成脚本和 golden，再记录已复核的摘要。检查只能发现版本漂移，不能证明语义等价。语义修改后运行[完整回归](../development/testing/local.md)，不能只重生成 manifest 来绕过审查。

2026-10-13 的来源复核仅涉及任务按阶段分目录后的证据链接调整；契约与 ADR 正文语义未变，来源 manifest 已同步记录新摘要。原始机器契约和冻结输入未重新生成。

## 机器契约与兼容性

schema 修改从 `spec/` 的 canonical 生成脚本开始。显式重新生成后执行 `python3 scripts/sync_contracts.py` 同步两个 Go embed 目录；check 在私有副本中核对清单和字节，不独立编辑 embed。规范身份字节与兼容矩阵以 `spec/README.md` 为准。名称、路径与文档迁移不改变 schema、身份、事件或冻结 handoff 字节。
