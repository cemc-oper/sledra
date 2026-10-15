# 接入事务、确认与恢复

SLD-13 提供 `AtomicIngestor` 与 `RecoveryStore`。管理端使用一个 `RawStore` 写者；事实、隔离、持久检查点与重建队列在同一 SQLite 事务提交。SLD-14 已提供[显式状态重建](state-projections.md)与消费水位；队列存在不表示已经重建，时间统计仍待 SLD-15。

## 接入入口与边界

```python
from pathlib import Path
from sledra_analysis.store.raw import RawStore
from sledra_analysis.ingest.transaction import AtomicIngestor
from sledra_analysis.store.recovery import RecoveryStore

with RawStore(Path("/management/facts.sqlite")) as store:
    ingest = AtomicIngestor(store, Path("/shared/sledra"), "deployment-root")
    result = ingest.scan("spool/session.sealed.jsonl", collected_at="2026-10-15T00:00:00Z")
    recovery = RecoveryStore(Path("/management/recovery"))
    receipt = recovery.acknowledge(
        store, Path("/shared/sledra"), "deployment-root",
        "spool/session.sealed.jsonl", acknowledged_at="2026-10-15T00:01:00Z",
    )
```

路径和 source_root_id 由可信管理配置给出；示例需要事先存在的源和管理目录。库和恢复域不放在任务节点。默认文件 0600、恢复目录 0700，既有目录的访问权限由部署方确认。恢复域必须由同一管理写者拥有，完整持久保存，不能用临时目录替代运营归档。

每次 `scan` 从数据库恢复全部 source generation，重新检查同一打开 descriptor；在外层 `BEGIN IMMEDIATE` 内调用有界 reader。raw、canonical 版本、冲突、验证标记、隔离 BLOB 和队列先写入，随后保存最后完整记录位置，再 `COMMIT`。存储层独立检查新位置以前的区间都有 raw/quarantine 覆盖，拒绝空洞和同 generation 后退。reader 的固定行／块／扫描预算保持有效；每批默认最多 1024 个记录、64 MiB 扫描 bytes。

open 半行保留在已提交边界之后。sealed 残尾完整隔离后才允许 EOF；只有 native 段头的空 sealed 段也可保存经过验证的头末位置。存储、源验证或提交失败，整批 facts/quarantine/checkpoint/queue 一起回滚；提交成功但响应丢失时，重启从库恢复并幂等重读。源替换、截断和 legacy 完整性限制沿用[身份模型](source-identity.md)。源诊断合并保存在对应检查点；同 UUID 封口冲突持久标记各版本并阻止后续扫描和确认。

`BoundedReader.process` 仍可独立使用，此时逐条写入并只返回候选位置；需要上述保证时必须使用 `AtomicIngestor.scan`。`transaction()` 仍拒绝公共嵌套；只有协调器的内部写作用域能加入外层接入事务。

## Registry 与持久队列

`ingest.registry(locator, source_id=..., collected_at=...)` 通过 no-follow descriptor 读取整个 immutable Registry JSON，保存原 wrapper、`/outbox_event` pointer 和已处理摘要，同事务提交。坏文件或超预算文件完整流式隔离；无法保存则不标已处理。采集前后核对同一文件 metadata stamp，第二次有界读取同时复核摘要。重复摘要不再写事实；同 namespace/locator 出现不同 bytes 时保留新来源世代、各摘要及冲突版本，返回 `registry_immutable_conflict`。此接口不修改登记文件或触发业务登记。

`projection_work_queue` 由 SQLite trigger 写入，分别记录新事件 canonical 版本、隔离记录、验证版本；source gap/冲突、半行增长及检查点变化也会加入 source 工作项，避免没有新事件时漏重建健康状态；冲突的每个版本都会进入队列。相同对象／修订重复写入幂等，隔离重处理的事实、验证、队列和审计一起提交。`projection_work(after_id=0, limit=1000)` 按 work_id 分页，最多 10000 项；当前是追加日志，尚无后台投影 worker 或自动删除。SLD-14 reducer 已另外持久保存自己的消费／投影水位，不能把最大 queue_id 当作已完成投影。

`watermark()` 只允许在事务外读取，返回数据库 lineage、最后 ingestion commit_id 和 queue_id。非协调器的 raw/reprocess 写入仍有事务队列保证，由 queue_id 描述其水位。source checkpoint 行绑定对应 ingestion commit_id；Registry 摘要同样绑定。lineage 在库创建／显式升级时生成，恢复保留它，不能把不同数据库的 commit 数字混用。

## 原始归档与确认

`acknowledge` 只接受已提交完整 EOF 的、实际 `.sealed.jsonl` 段。open、部分消费、缺持久 sealed 摘要、源变化或 UUID 冲突均拒绝。顺序为：

1. 从已验证 descriptor 流式复制**含段头的完整原字节**至 `raw/<sha256>.jsonl`；核对摘要，以同目录临时文件、文件 fsync、无覆盖 hard-link 发布和目录 fsync 保持不可变。
2. 在新管理事务中记录 archive key/generation、hash、尺寸、locator 与提交水位。
3. 使用 SQLite backup API 创建一致快照，包含 raw、隔离、冲突、检查点、archive ledger、队列和 lineage。核对 quick_check/FK，按内容 hash 发布 `backups/<sha256>.sqlite`，再发布关联水位的 JSON manifest。
4. 再次验证原归档和快照覆盖该检查点后，发布 `acknowledgements/<identity_hash>.json`；文件和目录同步后返回。相同确认重试验证原回执并同步目录，不覆盖确认时间或内容。

回执 kind 为 `sledra_ingest_ack`，包含 source key/generation、sealed hash/size、committed offset、checkpoint commit、raw locator、backup manifest、lineage/commit/queue 水位及确认时间。它与旧 Go 隔离夹具的 `fixture_ack` 不可互换。确认文件在显式恢复域，尚无向任务侧共享确认目录分发的自动服务。

任一发布切点失败时保留源；最多留下可核对的不可变归档、快照或未发布的临时文件，重试能继续。`verify_receipt(receipt)` 是本地确认消费边界，会复核原 bytes、快照摘要、水位、checkpoint 与 archive ledger。API 不删除源、归档、备份或回执，不自动启用保留策略。SLD-09 的本地衔接测试仅在私有合成目录验证回执后显式模拟源移除；Go fixture 清理器继续只接受自己的隔离夹具，生产清理、窗口及 A6 资格仍待部署验收。

## 备份与恢复

`recovery.backup(store)` 仅在无活动事务时创建上述一致快照。备份和原始归档必须同受恢复策略保护，保持所有已发布确认；不能只备份 offset 表。`recovery.restore(manifest, new_path)` 先验证快照 bytes、水位、quick_check/FK，并检查恢复域内**全部已发布回执**及其 raw/archive/checkpoint 覆盖。落后于任何确认或属于其他 lineage 的快照会被拒绝，避免把已清源的数据从恢复状态中消失。目标必须是新文件，已有库不覆盖。

恢复后从同一库取得检查点，继续读取尚存源；归档段可以恢复到受控源目录，UUID 相同则重读幂等。恢复函数不执行作业、提交调度任务或业务重试。最新快照之后的未确认输入由原 spool 重新接入；恢复到某个快照并不承诺保留其后的、已经从外部丢失的未归档输入。

恢复域必须包含所有已发布回执及其引用归档／快照。离线程序无法推断被遗漏的外部确认，丢失恢复域不属于可证明无损的恢复。暂存文件仅在可确认没有活跃写者时由站点运维处理；当前没有自动回收。每次确认做完整 SQLite 快照是正确性优先的本地实现，备份时间、空间增长和归档清理需后续预算与发行任务验证。

## schema 与验证

新库为 schema 3；既有 Sledra schema 1 或 2 默认拒绝打开，仅显式 `RawStore(path, upgrade=True)` 在事务中增加缺失表／trigger、为已有事实／隔离／验证回填队列并更新版本。旧 raw bytes、canonical 版本、来源和冲突不变。失败则回滚 schema／版本／回填；未迁移任何运营数据库。升级前先保留一致备份，降级与完整发行演练仍归 SLD-26。

```bash
make -C analysis test
uv run --project analysis --locked --offline --no-python-downloads --python /usr/bin/python3 python analysis/tests/verify.py --task SLD-13 --report-dir /tmp/sledra-v12-new
uv run --project analysis --locked --offline --no-python-downloads --python /usr/bin/python3 python analysis/tests/replay_recovery.py --report-dir /tmp/sledra-recovery-new
```

33 项新增 V12/V21、既有 72 项回归；19 个真实子进程 SIGKILL 切点覆盖 raw/quarantine/queue/checkpoint 写入、commit 前后、Registry 和归档／快照／ack 发布。SQLite 页数限额产生真实 SQLITE_FULL；archive fsync ENOSPC/EIO 为故障注入，未填满宿主磁盘。合成现场从 19 条冻结事实出发，增加冲突、opaque 和坏行／残尾；确认后显式模拟源移除，恢复的事件集合、版本、冲突、隔离和队列一致，旧备份拒绝，原归档重读新增记录为 0。

上述结果限本地 Linux/WSL，未验证真实掉电、共享/HPC 跨节点锁与 fsync、生产确认分发／清理、远端 CI 或 A6/A7，M2 尚未收口。
