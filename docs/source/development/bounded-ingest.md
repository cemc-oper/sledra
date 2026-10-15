# 有界读取、隔离与重处理

SLD-12 提供 `BoundedReader`、`EventValidator` 和 SQLite 隔离／重处理接口。沿用 [source 身份与世代](source-identity.md)，正常事实与隔离都保留原始 bytes 和位置。返回的 checkpoint 仍是内存候选；SLD-13 已通过 [AtomicIngestor 与 RecoveryStore](ingest-recovery.md)提供持久检查点同事务、队列及归档后的确认／恢复。

## 行边界与位置

reader 使用已核验的 SourceLease descriptor，按固定块扫描 LF。native 段头由身份层核验后跳过；legacy 显式来源不跳头，仍带完整性未证明诊断。每个完整行先持久化事实或隔离，再计入候选位置；源在扫描／保存过程中变化或存储失败会抛出错误，不返回新位置。此前已保存的行可以重读，事实与隔离均幂等。

| 输入 | 行为 |
|---|---|
| open 完整正常行 | 保存事实及当前验证器的 known/opaque 状态，再允许候选位置越过 |
| open 无 LF 尾部 | 不解析、不隔离、不越过；待下次补齐 LF，超过行内存预算报 stuck_tail |
| sealed 无 LF 残尾 | 全部 bytes 隔离为 sealed_incomplete_tail，持久化后显式提出 sealed EOF 候选 |
| 完整坏 JSON/UTF-8/重复键 | 保存原始 bytes、原因、source key/generation/byte range，继续后面的好行 |
| 超长完整行 | 切换管理侧临时文件，流式扫描到 LF，再分块写入隔离 BLOB，不把整行载入 Python 内存 |
| 已知类型缺字段或未知枚举 | 按信封／payload 契约拒绝并隔离 |
| 未知 major | unsupported_major 隔离，不能作为当前事件解释 |
| 有效 1.x 未知类型／扩展 | 扩展保留；未知类型作为 opaque 事实保存并持久记录 unsupported semantics，不输出业务成功 |
| 隔离容量不足／SQLite 写入或提交失败 | 保留输入，抛出错误，无新候选位置；不能跳过后假装完整 |

sealed 残尾没有完整行，只有经过隔离保存的 EOF 才可使用 `checkpoint(..., sealed_tail=True)`。它必须匹配 immutable sealed_size/hash，不能用于 open；已保存残尾的位置可再次恢复。没有把残尾补 LF 或自动修复 JSON。

## 验证器与版本

分析包内含逐字节匹配 `spec/1.0.0/` 的 event.schema.json 和 event-types.json。运行使用 jsonschema 4.10.3（与既有契约工具版本一致），传递依赖由独立 analysis/uv.lock 固定；记录器仍不依赖 Python。所有 schema 在本地，运行不获取网络资源。root check/sync 和保护检查包含这两份资源，安装 wheel 同样包含它们。

`EventValidator` 检查严格 UTF-8 JSON、重复键、NFC 键碰撞、数字和深度预算，验证 1.x 信封、已知 payload、role/source kind、关联层次必需字段、事件时间、exit 条件、finish 覆盖摘要和必要 evidence。未知 1.x 类型返回 `supported=False`；其完整事件仍参与 canonical hash。

验证器标识为 `sledra-event-validation-1.0.0`。`event_validations` 按原始来源记录和验证器版本保存 known/opaque 状态；没有标记的旧 raw 事实仍只具有 SLD-11 的基本存储边界验证。known 仅表示该验证器支持事件格式与相关规则，不代表 Attempt 成功、父文档真实存在或业务完成。真实父引用关联、状态投影和站点隐私资格仍分别处理。

## 隔离保存与查询

数据库 schema 2 在 SLD-11 表之外增加 quarantine_blobs、quarantine、event_validations 和 quarantine_replays。大行先使用有界内存窗口／私有临时文件，再以 zeroblob/blobopen 分块写入 BLOB，保存全量 bytes、SHA256、尺寸、定位、原因代码、验证器版本、采集时间和 Registry pointer。写入期间再次核对摘要，流内容变化也不能提交。

同位置、原始 hash、原因和验证器版本重传幂等；同 bytes 在不同位置共享 BLOB 并各自保留来源。原因仅为有界机器代码，不复制带原始 payload 的校验器异常文本。新管理库为 0600，报告目录为私有 0700；原始隔离可能含敏感值，不向任务侧发布。

`quarantines(limit=1000, after_id=0)` 分页查元数据，`quarantine_record(id)` 查单条，`quarantine_chunks(id, chunk_size=65536)` 流式取完整 bytes。`quarantine_bytes(id, max_bytes=...)` 只允许显式预算内的物化，默认使用 store 的单条记录预算，超长 BLOB 不会悄悄读入内存。

`reprocess(id, validator, attempted_at=...)` 使用原 bytes、原位置和原采集时间，追加重处理审计记录。仍无效则 retained；新验证器可识别时保存事实和 known/opaque 标记，结果为 accepted/opaque；事实和审计同事务。原隔离 bytes 和原因始终保留，不覆盖、不删除、不触发业务重试。accepted 是接纳原始事实，不是业务成功。超出记录预算或 sealed 残尾当前继续保留，不能通过重处理修补字节；原始块仍可导出供后续版本审查。

## 预算与旧库升级

默认行内存／正常 raw/canonical 各 1 MiB、chunk 64 KiB、每次最多 1024 个记录和 64 MiB 源 bytes。reader 达到记录／扫描预算会停在前一个已保存边界；部分扫描记录报 scan_partial_record，不能当真实 EOF。默认单隔离 BLOB 64 MiB、隔离 BLOB 总量 512 MiB；预算按唯一 BLOB bytes 计算，不含 SQLite/journal/临时文件开销。超出容量只能扩容或保留源重试，不允许丢弃原始内容。管理侧临时文件位于显式数据库父目录，不要求计算节点本地盘。

SLD-12 初始引入 schema 2；当前新库为 schema 3，见[事务升级](ingest-recovery.md#schema-与验证)。已有 schema 1/2 默认拒绝打开，必须显式使用：

```python
with RawStore(existing_management_path, upgrade=True) as store:
    ...
```

升级只在确认的 Sledra schema 1/2／canonical 编码版本上同事务增加表、队列和更新版本，不改旧事实、bytes、来源或冲突。升级失败保留原 schema；旧程序不支持 schema 3，不把此开发升级机制解释为备份、降级或生产发行资格。SLD-26 后续验证完整发行／回滚，SLD-13 已验证本地备份水位。当前验证只使用私有测试库，未升级运营数据库。

## 开发入口

从仓库根目录执行：

```bash
make setup
make -C analysis check
make -C analysis test
uv run --project analysis --locked --offline --no-python-downloads --python /usr/bin/python3 python analysis/tests/verify.py --task SLD-12 --report-dir /tmp/sledra-v10-new
uv run --project analysis --locked --offline --no-python-downloads --python /usr/bin/python3 python analysis/tests/replay_quarantine.py --report-dir /tmp/sledra-quarantine-new
```

显式报告必须使用不存在的新目录；普通测试与 demo 使用私有临时现场。demo 保留源、facts.sqlite 和 replay.json，可查看坏行 bytes／位置、open 补尾、sealed 残尾和 accepted/retained 重处理结果，使用的 checkpoint 仅在内存目录中。

本地 28 项 V10/V16、20 项 V11、24 项 V09 共 72 项通过。8 MiB 大行测试在 1 KiB 行窗口／4 KiB chunk 下，Python tracemalloc 峰值低于 2 MiB，且逐块取回的尺寸与 SHA256 完全一致；该数字不包含 SQLite 原生内存、页缓存或总 RSS，不代表 A6 性能资格。SLD-13 的本地事务、逐崩溃点恢复和真实 ack 结果见对应开发页；真实共享/HPC、生产清理与 M2 核心集仍待后续验收。
