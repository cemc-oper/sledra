# 文件身份与世代

SLD-10 提供 `sledra_analysis.ingest.source`，作为后续采集器的文件身份层。本组件读取 source、建立位置模型并诊断缺口；位置模型本身还没有持久检查点或真实采集确认；SLD-11 已提供独立的[SQLite 事实批次事务](raw-facts.md)。它不修改、封口或删除源。

## 身份与位置

`SourceKey` 为 `source_root_id + source_id + segment_uuid`，路径仅为 locator。部署方显式指定稳定的 root namespace，不用主机 inode 充当跨挂载身份。`Checkpoint` 保存 generation、协议版本、全部发现路径、device/inode 提示、header 字节数与 SHA256、committed_offset 前最多 4096 字节 anchor、上次尺寸与末尾历史窗口、封口尺寸与全段摘要以及 scan_epoch。相同 locator 换 UUID 是新 key，generation 从 0 开始；同 key 检出截断或改写时 generation 递增，旧位置保留。

`SourceTracker` 为单 reader 内存目录，可由调用方保存的 `Checkpoint` 集合恢复。`open()` 返回上下文限定的 `SourceLease`，扫描、读取、位置核验均使用同一 descriptor。按根目录逐层 no-follow 打开，拒绝越根、符号链接和非普通文件；扫描或读取期间 stat/摘要发生变化时抛出 `SourceChanged`，调用方丢弃当前批次、保留旧位置并重扫。权限、短读与 IO 错误也不能被解释为已结束。

段头限 65536 字节并要求 LF；仅识别当前 `1.0.0` 段协议，拒绝缺头、重复键和不支持版本，不能自动退化为 legacy。SLD-12 已负责[事件坏行、版本隔离和有界读取](bounded-ingest.md)，不以本模块的段头检查替代。

| 情形 | 行为 |
|---|---|
| 同 UUID、header/anchor/历史窗口不变、尺寸增长 | 同 generation，从已提交 offset 读取 |
| rename 或 open→sealed hard-link 双名字 | 同 generation，记录 locator；旧 descriptor 可继续读 |
| 相同 sealed UUID、全段 SHA256 相同、inode 不同 | 副本，同 generation 与 offset |
| 新 UUID 替换路径 | 新 key，从 0；旧 open 或未读完的源报告 source_gap |
| 尺寸缩小或 anchor/历史窗口改变 | 新 generation 从 0，source_gap 和截断/改写诊断 |
| 同 UUID 头部冲突、另一 inode 的 open 副本无法证明一致、sealed 改写 | 保存两版身份指纹，标 conflict，resume_offset 为 None，禁止正常推进 |
| 封口残尾没有 LF | sealed_incomplete_tail；不能在半行末提出 checkpoint |

UUID 冲突会标记该 key 的所有版本；重新扫描或恢复内存目录不能解除冲突。封口全段摘要流式计算，内存窗口有界；open 段按 producer 只追加、UUID 不复用的协议检查。open UUID 相同而 inode 不同的候选采用保守冲突诊断；可以证明全段一致的 sealed 副本正常归并。诊断与历史位置须由未来存储层一起保存，不能只消费 `resume_offset` 而丢弃诊断。

## 调用约定

安装 wheel 后可直接导入；源码开发使用 `PYTHONPATH=analysis/src`：

```python
from pathlib import Path
from sledra_analysis.ingest.source import SourceTracker

tracker = SourceTracker(Path("/configured/shared/root"), "deployment-source-root")
with tracker.open("spool/example.open.jsonl") as source:
    decision = source.decision
    if decision.resume_offset is not None:
        raw = source.read(decision.resume_offset, 4096)
        # 事件 reader 找到完整行末 offset；未来 store 与 raw 同事务保存
        # proposed = source.checkpoint(complete_line_end)
        # store.commit(raw, proposed, decision.diagnostics)
        # tracker.remember(proposed)
```

`checkpoint(offset)` 默认提出完整行边界的位置；SLD-12 可对已隔离的 immutable sealed 残尾显式提出 EOF 位置，不能后退、越过扫描尺寸或在冲突源上推进。`remember()` 只是安装调用方状态，不代表提交；扫描自身不会推进 committed_offset。SLD-11 已实现独立事实去重与批次事务，SLD-12 已提供持久隔离；SLD-13 的 [AtomicIngestor](ingest-recovery.md)已实现检查点与事实／隔离／队列同事务，并由 RecoveryStore 提供原始归档后的确认与备份恢复。

## Legacy 完整性限制

旧日志必须显式传 `legacy_source_id`。bootstrap 身份采用该 namespace 与首次发现路径摘要，后续通过已发现路径、inode 提示、头部和历史窗口关联 rename 与世代。每次返回 `legacy_source_integrity_unproven`，包括看似正常的追加和恢复。

可检测的同长或截断再增长改写会创建新世代；位于所有采样窗口之外、同长且头尾不变的改写无法普遍检测，mtime 变化也不能证明中间字节被替换。V11 保留该反例：世代可能不变，但诊断必须存在。无法承诺任意 copy-truncate 无损；调用方应把这种源显示为完整性未证明。文件已经短到不能提供完整段头时保留旧位置并报错，等待恢复或后续隔离，不猜测 UUID。

## 验证边界

`make -C analysis test` 回放全部 22 个冻结输入摘要，验证 6 个 open 段的 UUID、11 条过程事件字节及重复 offset；新临时现场覆盖轮转、副本、截断再长、inode 提示复用、冲突恢复、半行、路径拒绝和扫描竞态。inode 重用通过提示模拟，真实共享/HPC、远程 FS 属性缓存、检查点事务和清理确认尚未验证；SQLite 原始事实验证见 SLD-11。
