# 会话 spool（SLD-07）

spool.Open 用 SHA256 路径创建 source/Run/task/Attempt 目录，并取得非阻塞 session flock。每个 session 一次只向一个 .open.jsonl 追加内嵌 codec 的规范字节，首行为带 UUID 的版本化 header。轮转同步后以同目录不替换 hardlink 发布 sealed，再删除 open 名；中途崩溃可留下同 inode/UUID 两个名字，读取须按段身份去重。持锁恢复时核对同 inode 和段头后删除遗留 open 名，绝不续写 sealed inode；同名冲突保留两份证据并返回 spool_unavailable。完整超龄 open 在恢复时封口，半行仍保留并隔离，后来 writer 使用新段；不删除未确认段。

事件按SLD-06限制，段/session按 segment_bytes、session_bytes、segments 限制。零值采用合成本地默认1MiB、64MiB、64段、五分钟，不是站点预算。max_spool_bytes/max_spool_files 同时设置启用全树容量保护，同时为零则关闭。Stats 的成功/丢弃/字节/新段计数只存进程内存。business 每个失败CLI输出不含payload的分类诊断，strict 返回错误；持久诊断尚未提供。[确认保留工具](../retention/README.md)仅限夹具。文件0600、目录0700，身份路径不暴露原 task key 或产品路径。

sledra event 使用已有可信 SLEDRA_ROOT 和部署 JSON SLEDRA_CONFIG，固定 producer_role/name/version、source_id、session_id，可选上述容量值。并发 producer 采用不同 session。默认内嵌契约；覆盖规则见 [CLI](../../docs/source/reference/cli.md)，role 不按事件覆写。

```bash
sledra event --context /trusted/attempt.json --type attempt.started \
  --payload-file /trusted/payload.json --event-id UUID
sledra event --context /trusted/attempt.json --type attempt.started \
  --payload-file /trusted/payload.json --event-id UUID --strict
```

命令校验 Attempt及父级，先完整编码再追加，stdout为空。同 event-id 再调用会生成新时间，不是原字节重传。重试者须保存完整信封，通过 sledra-spool append --config FILE --input EVENT.json [--strict] [--seal] 重放；底层只校验事件与role，不核对父context，因此原信封必须来自已验证producer。省略ID仅为本次生成；Shell示例不承诺不确定追加后的持久重试。

本地测试覆盖锁冲突、session复用、并发source、轮转、合成限制、非法/超大输入、半行、根软链接和错误模式；另以真实 SIGKILL 验证 link/unlink 切点恢复及夹具归档确认。只读/I/O故障为注入，不等于真实满盘/跨节点/HPC 或掉电耐久性。同步内核I/O可能超过用户态超时，目标资格依03/25。
