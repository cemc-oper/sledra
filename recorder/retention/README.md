# 保留夹具与运行边界（SLD-09）

sledra-retention 仅用于隔离夹具。init-fixture 在私有0700、初始空根创建标记，必须早于任何spool数据；未启用生产确认或自动清理。真实清理须等待13的raw/checkpoint提交、持久原始归档和水位。

ack-fixture 仅接收哈希spool树中完整、单链接sealed段，校验header UUID/source/session及全字节摘要，通过同目录临时/不替换发布复制至fixture-raw/<source-hash>/<uuid>.jsonl，再发布不可变acknowledgements回执。回执绑定UUID、source、摘要、sealed大小、归档位置和等于大小的offset。重复一致确认幂等，异内容冲突；这是合成持久证据，不是真实采集事务。

clean-fixture 重新读取/摘要sealed，核对回执和归档；sealed与ack年龄均须超过source-days。backlog-days为正、source-days大于backlog-days、evidence-days不少于source-days；确认已过期时拒绝。先持久cleanup intent，再检查inode/内容、只unlink该sealed并fsync，最后写完成回执。unlink后未完成的崩溃状态为不完整清理，须检查ack/archive并必要时恢复原段后再采集；不能把夹具回执当成采集完成。

隔离目录示例：

```sh
root=$(mktemp -d)
sledra-retention init-fixture --root "$root"
# 用记录器夹具在本根内生成 sealed 段。
sledra-retention ack-fixture --root "$root" --segment "$sealed" \
  --backlog-days 1 --source-days 3 --evidence-days 30
# 两种保留时钟满足后，clean-fixture 使用相同参数。
```

生产producer须用A6批准值同时配置max_spool_bytes/max_spool_files。记录器在准入和每次追加前持有 `.capacity.lock` 并扫描全 spool 常规文件（含session lock），达到上限返回spool_full，锁竞争立即writer_busy；business丢弃观测后继续业务。不为释放空间删除未确认段。共享卷flock/全树扫描正确性和延迟未验证，保留/证据窗口、backlog及备份恢复点未批准时生产清理关闭。

恢复时核对ack UUID/hash/size、归档JSONL、cleanup intent/complete，在新测试中回放归档。intent无complete且source已删时由archive重建；ack/archive缺失或不匹配则保留source并诊断。真实13恢复须从同一备份水位恢复raw和checkpoint；本地夹具不验证真实数据库、共享卷或跨节点恢复。
