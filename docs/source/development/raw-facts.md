# 原始事实存储与冲突

SLD-11 提供 `sledra_analysis.store.raw.RawStore`。管理端显式指定 SQLite 文件，保存完整原始 bytes、规范事件版本与来源位置。SLD-12 已提供[坏行隔离](bounded-ingest.md)，SLD-13 的[AtomicIngestor 与 RecoveryStore](ingest-recovery.md)提供持久检查点同事务、队列、归档确认与备份恢复。事件数量是不同 `event_id` 的原始事实数，不代表成功作业数或状态投影。

## 数据与查询

| 数据 | 保存与约束 |
|---|---|
| events | `event_id` 唯一，仅表示事件身份，不选取代表 payload |
| event_versions | `(event_id, canonical_hash)` 唯一，保存规范 bytes、类型及 schema_version |
| event_conflicts | 同 ID 有两个及以上规范 hash 时标记；各版本继续保留 |
| raw_blobs | 原始 SHA256 与 BLOB；保留空白、转义、键顺序、LF 和 Registry 包装 |
| source_locations | root/source/segment UUID、generation、byte range、locator 和 segment/registry 来源类型 |
| raw_records | 每个原始版本的来源关系、采集时间及 outbox pointer，外键指向规范版本与原始 BLOB |

相同 ID/hash、不同来源会追加来源；相同来源、相同原始 bytes 重传不会重复计数，原采集时间保留。仅编码不同但规范内容相同的原始 bytes 也保留各自版本。同一来源位置出现不同原始 bytes 时均保留，不覆写；位置世代的判定仍由 [SLD-10](source-identity.md) 负责。

`get_event(event_id)` 返回全部版本、每版来源以及 `conflicted`，不返回“获胜版本”。`events(conflicts_only=True)` 查询全部冲突身份；`sources(event_id, canonical_hash=None)` 可取某版或所有来源；`counts()` 分别给出身份数、规范版本数、原始来源记录数、冲突身份数。查询顺序按 ID/hash/来源位置排序，不按到达时间选择业务结果。冲突事实不能被消费者解释为确定成功。

每个 `StoredSource` 均保留 raw bytes、raw_hash、collection time、来源位置及 event_pointer。JSONL 的 byte range 精确包含该事件行；Registry 的范围覆盖整个不可变 JSON，`/outbox_event` 指定事件，规范 hash 仅计算该完整 outbox 事件，原始 bytes 仍为整个文件。

## Canonical 字节

独立标准库编码器实现冻结 `1.0.0` 规则：键与字符串 NFC、键排序、无多余空白、UTF-8、控制字符及 U+2028/U+2029 转义。精确有符号 64 位整数可用，负零规范为 0；float/NaN、重复键、NFC 键碰撞、孤立 surrogate、坏 UTF-8、过深 JSON 拒绝。JSON 中的 surrogate pair 解码为 scalar。

规范 hash 覆盖完整事件，包括 `recorded_at` 和未知扩展；采集时间是 `RawRecord.collected_at`，位于事件之外，不参与事件 hash。扩展中的任何同名采集字段仍是事件内容，会参与 hash。Raw SHA256 和 canonical SHA256 的用途分开，不因原始编码变化制造业务冲突。

数据库保存 canonical 编码版本 `sledra-canonical-1.0.0`、application ID 与 schema version 3。未知数据库或编码版本拒绝打开；既有 schema 1/2 只在显式 upgrade=True 时补齐隔离／事务表及队列，旧事实与字节不变，见[升级边界](ingest-recovery.md#schema-与验证)。编码版本来自契约，不用阶段编号替代。已通过冻结 golden 的事件摘要、Unicode/类型/负零向量和既有同 ID 冲突反例。

## 使用接口

运行需要 Linux、Python 3.12+ 及 Python 内置 SQLite 3.37+（STRICT 表）；SLD-12 校验依赖单独锁定的 jsonschema 4.10.3，记录器不依赖它。本地实测 SQLite 3.45.1。

```python
from pathlib import Path
from sledra_analysis.ingest.source import SourceKey
from sledra_analysis.store.raw import RawRecord, RawStore, SourceLocation

# raw 为已读取的完整事件行，key/generation 由 source 身份层给出
key = SourceKey("deployment-root", "node-a", "segment-uuid")
location = SourceLocation(key, 0, byte_start, byte_start + len(raw), locator)
record = RawRecord(raw, location, "2026-10-14T00:00:00Z")
with RawStore(Path("/configured/management/facts.sqlite")) as store:
    result = store.ingest([record])
    event = store.get_event(event_id)
    # event.versions 为全部内容与来源；必须检查 event.conflicted
```

`ingest()` 在写入前验证输入及预算，然后把事件身份、规范版本、冲突标记、原始 BLOB、来源关系写入同一事务。验证失败或 SQLite 写入/提交失败时整批不写入；调用方保留输入并重试或交后续隔离。提交失败不能返回成功。源文件权限与业务执行不由本模块控制。

存储边界检查 JSON、身份、1.x 信封基本结构和时间；不替代完整 payload、producer role、父引用或业务语义校验。未知有效 1.x 类型和扩展保持不透明，不推导成功。未知 major 与坏记录目前返回 `RecordError`，SLD-12 的 BoundedReader 可保存原 bytes／原因到持久隔离，调用方不能忽略错误并越过输入。

## 单写者、预算与部署边界

RawStore 在打开期间持有数据库 inode 的非阻塞 advisory flock；同路径、hard-link 别名和另一进程均不能通过本 API 成为第二写者。锁占用抛出 `WriterBusy`，关闭释放锁，fork 后不能复用连接。SQLite 外部客户端不自动遵守此 advisory 协议，所有管理写者须通过统一入口；这不是跨节点部署资格。管理数据库路径和父目录是可信配置，不允许打开期间替换数据库文件；最终文件符号链接和非普通文件拒绝。

新库以 0600 创建，使用外键、STRICT 类型、唯一键和范围约束；冲突标记由版本插入触发器维护。显式使用 DELETE journal、FULL synchronous，不根据共享 spool 位置自动启用 WAL。只在管理侧单写者使用，任务节点不开库；共享/HPC 上的 SQLite 锁、journal 与备份恢复仍需独立验证。

默认单条 raw/canonical 上限各 1 MiB、批次最多 1024 条、raw 总量 16 MiB，JSON 深度最多 64。构造 RawStore 可调整三个字节/条数预算；这些是本地保护值，不是 A6 批准性能预算。超过预算返回错误，不能丢弃后假装完整；流式长行和隔离区容量策略见 SLD-12 的 bounded reader。

## 验证

```bash
make -C analysis test
PYTHONPATH=analysis/src python3 analysis/tests/verify.py --task SLD-11 --report-dir /tmp/sledra-v09-new
PYTHONPATH=analysis/src python3 analysis/tests/replay_store.py --report-dir /tmp/sledra-raw-replay-new
```

24 项 V09 覆盖真实 SQLite 的 19 条冻结事实、重复/排列、来源定位、重开、payload/recorded_at/扩展冲突、golden 编码、数据库约束、单写者、限额、写失败和读锁导致的提交失败。组件入口同时回归 20 项 SLD-10 V11 和 28 项 SLD-12 V10/V16。显式报告必须使用新私有目录；普通测试只打印结果，不改冻结输入。

本页的独立 ingest 证明事实批次一致性；需要源检查点／隔离／队列同事务时使用 SLD-13 AtomicIngestor。确认与恢复见对应开发页，SLD-14 已提供[版本化状态重建](state-projections.md)，SLD-15/23 和生产清理资格仍未完成。

`replay_store.py` 仅回放冻结合成样本，显式报告目录保留 `facts.sqlite`、22 个原始输入与 `replay.json`；重复/乱序写入和重开后的事实数固定为 19。未指定目录时使用临时库，成功后清理，不作为生产采集服务。
