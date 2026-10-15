# 离线事件编码（SLD-06）

event.New(installedRole) 编译内嵌 1.0.0 信封与24类事件目录。Encode/Validate 使用同一校验路径，输出规范 JSON 加一个 LF；Go 测试逐字节核对 embed 与 canonical。NewID 生成 UUIDv4，重传须保存并重用完整事件字节；稳定 evt_ ID 由注册/source owner 生成。

校验器核对信封、已知 payload schema、部署 role、source kind、关联层次、时间/exit/finish、大小、UTF-8 与重复键。未知事件带 attempt_id 时同样要求 task_instance_id。错误去除原始校验细节，拒绝明显敏感键、控制字符和原始绝对/home/traversal/file:// 路径；`engine.state_observed.payload.node_path` 的前导 `/` 作为逻辑节点路径允许，文件定位字段继续按路径策略检查。保留未知 1.x 事件和 namespaced extensions 为不透明规范 JSON。已知 payload 是闭合 schema；producer 仍负责允许值脱敏和受控 locator，字段拒绝列表无法发现所有值中的秘密。

Validate 不读取 Run/Attempt/Job/Artifact/Expectation 父文档。依赖父级的身份绑定须经 bundle checker 或记录器的 context 验证；不透明事件不代表成功，2.x 拒绝。编码不持久化，写入和 business/strict 策略由 [spool](../spool/README.md)负责。

独立命令属于离线编码阶段，与 sledra event 记录命令分开：

```bash
cd recorder
GOPROXY=off GOSUMDB=off go build -o /tmp/sledra-event ./cmd/sledra-event
/tmp/sledra-event validate -config /path/to/installed-role.json -input /path/to/event.json
/tmp/sledra-event encode -config /path/to/installed-role.json -input /path/to/event.json -output-dir /path/to/existing-dir
```

配置由部署提供，如 {"producer_role":"process_owner"}，不允许每次事件任意覆写 role。encode 在已有目录原子创建 0600 的 <event_id>.json，相同字节重试成功，异内容为 conflicting_event_id。stdout 为空，失败 stderr 单条分类 JSON、非零退出、不含输入内容。输出文件不是 spool 或采集确认，目标目录须可信且路径不能含目录软链接。

源码根运行 python3 spec/verify_event.py 比较 Go/Python 字节，并用同一组事件向量核对 Go codec、Python 契约辅助器和 Python ingest 的基础语义分类。记录端另有限制事件/载荷大小及敏感字段和路径；管理侧接入保留原始事实或隔离 bytes，不把这两层策略当成完全相同的 validator。
