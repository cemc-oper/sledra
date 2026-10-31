# execution-child-events/1 runtime contract

CE-01 contract baseline, 2026-10-30. CE-02 implements the Go components and CE-03 implements public Bash integration; full CE-04 cross-process fault acceptance remains pending. See [interface](interfaces.md) and [vectors](README.md).

## P03 身份、文件与资源边界

首版限 Linux 同机、同 UID、同 PID namespace、有可读 procfs 的 Bash >=4.3。节点键固定 `/proc/sys/kernel/random/boot_id` 加 PID namespace inode；owner 键为 UID、owner BASHPID、`/proc/PID/stat` 第 22 字段 starttime、session_id。open 核对 owner 为当前 CLI 的 Bash 祖先进程（允许库捕获回执使用的进程替换子进程，逐级核验 ancestry，不仅比较直接 PPID），非 zombie，UID/namespace 一致。以后每次 attach/commit/result 重新核对 owner PID/starttime/boot/namespace，不用 hostname、`kill -0` 或旧环境 ID 替代；PID 重用、重启、owner 死亡均拒绝，不补 ended。procfs 不可核验即 fail closed，不猜测。

PID/starttime 不区分同 PID 的 exec。本期支持 owner 保持原 Bash 直到收尾，业务 inline/external 由它启动；owner 通过 exec 替换自身不在有效生命周期支持范围。新的 Bash 公共 begin 一律清除导出身份、使用新 key/新 Execution；继承旧引用只能申请 event-only，不能成为 owner。不能宣称能检测任意同 PID 同启动时刻的 exec，或原子证明 owner 在检查后仍存活。A03 必须分开验证“PID 重用拒绝”“新入口新身份”“继承无 owner 权限”与此限制。

运行目录由部署者事先建立，UID 匹配，mode 0700；部署须确认是真正节点本地文件系统（不在 NFS/Lustre/共享卷上），不能仅凭绝对路径或 hostname 判断。所有路径组件不允许 symlink、其他用户可写组件或不可信挂载。begin 只在其中以随机 UUIDv4 建立新 0700 目录。当前 UID 的根路径及上级目录仍属可信部署边界，不是对同 UID 恶意写者的沙箱。runtime、session、writer.lock 的 device/inode 固定；每次 open 与路径的 lstat/fstat 对照，不跟随链接，不接受特殊文件、硬链接或锁 inode 替换。lock 0600，创建后不 rename/unlink。记录根锚定原 os.Root 校验，根路径/设备 inode/根配置绑定固定；文件缺失、替换、坏尾、根不兼容拒绝，不重建。

固定控制目录内容：

```text
$SLEDRA_RUNTIME_DIR/<session_id>/
    event-context.json   # 不可变 event-only 描述
    state.json           # writer 锁内原子替换
    writer.lock          # 单 Execution 的唯一追加锁 inode
    closing              # 完整、不可变的第一次合法 end 请求
    disabled             # 零字节、单调停止请求
    diagnostic           # 零字节、单调诊断预算
```

只有创建者初始化时可建上述目录/lock；日常缺文件不自动创建 state/描述/lock。closing/disabled/diagnostic 是可选门闩，不存在是正常值；存在但类型/权限不符是控制故障，不能当成“不存在”。状态/描述/closing 使用本地同目录临时普通文件，完整写和 close 后才发布。state 用 rename 原子替换；这不是 fsync/掉电原子事务。描述及 closing 用 Linux renameat2(RENAME_NOREPLACE) 不可覆盖发布完整文件，拒绝替换现存终名；运行目录若不支持此原语则初始化拒绝，不回退为边写边发布或 link 临时双硬链接；disabled/diagnostic 为零字节 O_EXCL 创建，已有合法文件幂等。临时文件仅操作期间存在，不是请求队列或恢复源；失败保留原有终名。任何 close/rename/清理错误必须分类，不以之后成功覆盖先前不确定结果。

REF 的 wire 形式固定为 `sledra-ce1:<绝对 session 目录>/event-context.json`；业务将其视为不透明字符串。解析只接受此 scheme、规范绝对路径、固定末段、session UUID，无 `.`/`..`、NUL/控制字符；不做 glob、搜索最新会话或执行引用内容。

描述 JSON 所有键闭合、禁止重复，字段为：`protocol:"execution-child-events"`、`version:1`、`interface:"CE-1"`、`role:"event-only"`、`session_id`、`execution_id`、`entry_time`、`uid`、`boot_id`、`pid_namespace_inode`、`owner_pid`、`owner_start_ticks`、`runtime_path`、`runtime_device`、`runtime_inode`、`session_device`、`session_inode`、`lock_device`、`lock_inode`、`root_path`、`root_device`、`root_inode`、`root_config`。root_config 原样符合旧 `$defs.root`。device/inode/start_ticks/namespace_inode 用规范非负十进制字符串（避免跨语言数值舍入），uid 为 0..4294967295、pid 为 1..2147483647 的整数，version 固定为 1；boot_id 为内核规范小写带横线 UUID 字符串并逐字节比对，entry_time 沿用原 UTC 时间规则。device 为 0..18446744073709551615，inode/start_ticks 为 1..18446744073709551615 的规范十进制字符串（零只写 0，其他禁止前导零）。描述不含 owner_key、二进制路径、凭证或业务参数。state 存描述原始 bytes 的 SHA256 以拒绝错误描述；这只是内部绑定，不新增记录身份算法。

state JSON 同样闭合：`protocol, version, session_id, execution_id, descriptor_sha256, owner_key_sha256, revision, phase, record_device, record_inode, context, active_stage, end, pending, last_result`。phase 为 creating/active/writing/closed/disabled；closing/停止请求是门闩叠加状态，不依赖旧 state 快照。record_device/record_inode 在 creating 和 writing(start) 时可为 null，started 写入成功后提交 active 时固定为非空规范十进制字符串；其后不可替换。context/end 为 null 或已接受完整事件的 base64；active_stage 为 null 或 `{name,stage_id}`；pending 为 null 或 `{request_id,kind,event_id,record_base64,record_sha256,auxiliary,target_phase}`；last_result 为 null 或 `{request_id,event_id,record_sha256,revision,result,code,phase}`。hash 均为 64 位小写 hex；base64 严格按 RFC4648 标准、带 padding，无空白，解码后用未改 codec 校验。kind 枚举与 prepare 一致。revision 为 0..9223372036854775807，不能回绕；耗尽即禁写。creating 的 revision=0，start 的 writing 意图 revision=1、第一份已提交 started 的 revision=2。每次状态替换递增；不存在时不推断。pending 仅在 writing 非空，target_phase 只能 active/closed（end 对应 closed）；正常 active/closed 的 pending 必须为空，closed 必须有 end，active 不得有 end。context 必须为同 Execution 的 execution.context，end 必须为 execution.ended，active_stage 的名称/ID 沿用旧契约并与 stage 操作核对；last_result 只允许 committed/ok 或 noop/same_context/same_end，revision 不得高于 state revision。其他组合均视为控制损坏。

closing JSON 字段固定为 `protocol,version,session_id,request_id,event_id,record_sha256,record_base64`；只容纳首个合法 owner end 的完整 prepared 事件，结束属性依原 codec 核验。disabled/diagnostic 不装入业务文本。单次内存/文件上限如下（UTF-8 字节，先限制读取再解码，超限不截断）：

| 对象 | 上限 |
|---|---:|
| 旧事件 JSON、不含 LF | 65536；旧字段/深度/集合/数值限制全部继续适用 |
| prepared，含 LF | 66048 |
| 完成回执，含 LF | 4096 |
| 描述，含可选尾部 LF | 16384 |
| state，含可选尾部 LF | 524288 |
| closing，含可选尾部 LF | 131072 |
| root/runtime 的绝对路径 | 每个 1024；REF 2048 |
| 公共参数/内部输入总 argv payload | 131072；单字段仍受原边界 |
| 会话内最终控制文件数 | 6；正常状态发布最多一个 state 临时名，closing/描述初始化各最多一个临时名 |

state 最多三份完整事件（context/end/pending，每份 base64 最多 87384 字节）加 stage/最后回执及身份，不随总事件数增长。last_result 只存一份，无每事件文件/回执数据库。CE-01 必须构造各上限及 +1 向量，不能通过调大旧 JSONL 限额解决控制状态过大。未启用 CE 时不新增路径/argv 限制。

## P04 完整状态与优先级

有效状态按“身份/控制可核验 → disabled 门闩或 state.disabled → closed → closing 门闩 → writing/active/creating”判定。state 缺失/损坏不是 active；获取 writer 锁后发现 writing 表示前任没有正常提交，直接发布 disabled，不尝试重放 pending。未获锁看到 writing 是可能的活跃 writer，返回 Busy，不能仅凭快照禁用。

| 当前有效状态 | 输入/触发 | 下一状态与结果 | 允许 JSONL 行数 |
|---|---|---|---:|
| 未 begin/off | 任意非 begin hook 或重复 off begin | off/no-op；无文件/CLI/诊断 | 0 |
| 未 begin/on | 首次合法 CE begin | creating → writing(start) → active；完整成功才发布引用 | 1 started |
| creating/首次 begin | 参数、握手、任意初始化/回执故障 | 本地 disabled，已建会话尽力禁用；无引用、不再 open | 0 或不确定的 started |
| active | 合法 owner/event 操作且锁可得 | P05 的最终门闩组检查通过后获准；writing → active，提交成功 | 1 |
| active | 参数/角色/保留名/大小/stage 顺序拒绝 | 保持 active，rejected | 0 |
| active | 非 end 操作锁 Busy | 保持 active，busy，不 prepare/commit 重发 | 0 |
| active | 相同 context | 锁内等价比较，更新有界最后结果，noop | 0 |
| active | 不同 context | disabled 请求并在锁内确认；保留首份 | 0 |
| active/writing(event) | 第一个合法 end | 先发布 closing；已获准 writer 可收尾，end 只非阻塞取锁 | 见 P07 |
| writing + closing/disabled 请求 | 当前持锁操作 | 不清门闩；尚未调用 write 时复查，发现 disabled 就停止；已写/在途按 P05/P07 | 至多已获准的一行 |
| 任意 writing | 写/close/状态提交失败或持锁者死亡 | disabled；writing 保留也是永久拒绝证据，不重放 | 0/半行/整行，结果不确定 |
| closing，无 writing，无 disabled | 相同 owner end，确定尚未尝试 end | 使用 closing 缓存原事件，writing(end) → closed | 1 ended |
| closing | event/context/stage/attach 新绑定 | closed/closing，无新接纳 | 0 |
| closing | 不同 owner end | closed/conflicting_end，保留 closing 首份 | 0 |
| closed | 相同 owner end | noop/same_end；不追加、不重开 | 0 |
| closed | 不同 end / 其他写请求 | conflicting_end / closed | 0 |
| disabled 或禁写请求待静止 | 所有写请求、重复 attach/source | disabled；只有 result 只读核查可确认历史提交 | 0 新获准行 |
| 任意 | 身份失效/描述或 lock 丢失替换/无法核验 | 本地 unavailable；可验证同会话控制失败按 P08 尝试禁写 | 0 本次新行 |
| 任意 | 回执丢失/异常 | 单次 result；confirmed 继续按当前共享状态；其余一次 disable，永久本地停止 | 0 核查行 |

重复 begin 总是保持原入口状态；disabled 与 closed 都不允许从成功事件、旧快照、result 或重新 OpenWriter 回到 active。closed 后再有 disabled 请求不删除 ended；它可阻止所有后续写，但不改写已经确认的业务事实。诊断预算与历史提交结果不因终态重置。

## P05 提交顺序与并发线性化

只锁本 session 的 writer.lock，Linux advisory 独占非阻塞 flock；所有 CE 的 start/context/event/stage/end/结果核查共用此锁。锁 fd 不跨 CLI 生命周期保留，内核在崩溃时释放。没有全根扫描、全局锁、网络服务或新 Registry。

1. 参数/typed 值/角色/完整编码先有界验证；纯 prepare 不写任何执行/控制文件。
2. 校验同机身份、固定描述/目录/锁 inode，非阻塞取锁；未获锁不发布普通事件意图。合法 end 的 closing 例外见 P07。
3. 锁内读 state，检查门闩、根及 record inode、context/stage/end 约束；遗留 writing 禁用。完成上述校验后，固定先读 disabled、再读 closing；两者都明确不存在时才获准（end 只允许匹配自身 closing）。这是最终门闩组检查。门闩单调不删除，因此成功组检查可**线性化在该组第一次不存在的读取时刻**；第二次读取若看到门闩或报错，整个组拒绝。不能把第二次读取时刻当成跨文件原子快照。获准范围只包括这一个完整 prepared。
4. 原子写入 phase=writing、完整 pending、原缓存和新 revision；rename/close 有任何失败不得开始 JSONL write。可能已发布的 writing 不回滚为 active。
5. 再检查 disabled；如出现则不开始数据写，保持停止。否则使用原 Root/Create 或 OpenWriter + Append 原语，恰好一次完整 LF Write，核验长度和 error，再 Close 并核验；start 仅 O_EXCL，其他仅打开已有文件。
6. 数据和 close 都确认成功后，锁内更新已接受缓存、清除 pending、写入本操作 last_result 和 phase active 或 closed，revision 递增。门闩在 state 之外，任何状态提交都不能清除它；state.active 与 closing/disabled 同时存在时门闩优先。
7. 释放锁后返回回执；回执投递错误不再执行数据操作。若已经看到 disabled 请求，只能报告历史提交结果及 stop，不声称可恢复 active。

独立门闩的发布不等待 writer，故不能把“文件已存在”当成在途 syscall 已停止的证明。停止请求发布前已越过接纳点的 writer 算唯一在途操作；它可能在请求发布前后进入内核，步骤 5 的复查缩短窗口，但检查与 Write 不是原子指令。**全局禁写确认点**为 disabled 已发布且调用方取得 writer 锁、确认无未完成 writer（或持锁 writer 已收尾/禁用）后；stop=quiescent 才表示此后没有新 Write。stop=requested 只表示不再接纳新操作，尚不保证在途已退出。测试须分别统计接纳、意图、Write 开始/返回、Close、门闩请求和静止确认，不能把请求后的一个已获准 write 当作新接纳，也不能用“可能已在内核”掩盖未知操作。

closing 同理：以门闩发布关闭新接纳，先前已接纳事件先于 ended；end 与事件共用锁保证 ended 后不会出现 CE 业务追加。最终门闩组检查的线性化点与门闩发布之间的已获准操作是有定义的在途边界；不承诺撤销该操作，不声称 CPU 检查和文件 I/O 形成跨文件事务。任意 I/O 故障下没有硬延迟/fsync/掉电持久性保证。

## P06 回执丢失与有界结果核查

prepare/open 不重试。prepare 回执异常时尚无 commit，不触及 JSONL；当前接入保守停止。open 回执异常时不发布引用、不查找随机目录、不再次 begin；可能留下只有 started 的不完整事实。

对已知 REF 且已经调用 commit 的请求，Bash 保存完整 prepared 及 prepare 返回的 request_id/event_id/record_sha256；不调用外部 Python/jq。result 从 stdin 接收原 prepared，内部重算 JSON 摘要并与三个选项和 prepared 内字段核对，拒绝把别的操作结果当成自己的。不能让业务提供身份/摘要、从最后一行猜测或重新 prepare。

commit rc/回执可信时直接处理。退出/回执不可信时，恰好一次 result，不循环、不等待锁、不搜索 JSONL：

| 核查观察 | 结果与后续 |
|---|---|
| 独占锁内 last_result 的 request_id/event_id/hash 全匹配、是已提交操作或已提交 noop 结果，且无遗留 writing | confirmed：原始成功提交可证明；不重复 append。仍分别服从当前 closing/closed/disabled；历史成功不能解除终态 |
| last_result 被后续操作覆盖、无匹配结果、存在遗留 writing、state 无法读、超限或格式错误 | unknown；一次 disable；本地永久停止；不能从文件尾完整猜成功或未写 |
| 锁正 Busy | 本次查询无法证明结果，按 unknown；一次 disable，不再查询 |
| result 输出/rc 丢失、损坏、不匹配 | 同 unknown；不递归核查 result，不再次 commit |
| 已 disabled，last_result 精确匹配且无 writing | 只允许确认该历史结果；会话保持 disabled |

last_result 只有一个槽位。没有全局“exactly once”服务，不接受用户重试 token；普通事实必须遵守每份 prepared 只 commit 一次；唯一例外是 P07 的 owner end 在已证明未获准/未尝试 ended 时，由后续显式相同 end 使用首次 closing 再次竞争锁。这不允许重写已尝试的 ended。重复公共 context/end 用语义幂等规则，不是重试未确认 commit。旧请求在回执被覆盖后再次直接调用 commit 属协议违约，不能用有界缓存声称能阻止任意重放。

## P07 关闭门闩与重复 end

合法 owner end 先有界核验结果、角色和完整编码，非法 end 不创建 closing。第一次 end 将完整 prepared 原子不可覆盖发布为 closing；若已有合法 closing，比较结束 attributes（旧精确等价规则），相同取回并复用其原始事件、ID/time/request_id，不重新生成结束事实；不同直接 conflicting_end。owner 的私有缓存保留首次 end，重复调用先比较结果；不能把两个不同 end 都当首次。

closing 发布失败：不写 ended，尝试 disabled 请求并停止本地；不得返回成功关闭。closing 已发布后才非阻塞取 writer 锁。Busy 返回 21/closing，保留 closing；不等待业务、不后台补写。允许随后**显式**相同 end 再尝试取锁，前提是独占锁读到 pending 为空且从未开始 writing(end)、会话无 disabled；使用 closing 原缓存。writing(event) 崩溃转 disabled，不可因 closing 再尝试。writing(end) 遗留、不确定或回执丢失一律按 P06，不能重写 ended。

第一次 ended 成功提交后，closed 保存完整 end；相同 end noop。关闭不为活动 stage 补结束，不重新执行 script；关闭 Busy 或失败可能留下没有 ended 的不完整事实。父 TERM、子返回 143、真正父收到 signal、KILL 各自保留业务原义；Sledra 不推断 signal、不负责进程组转发。

## P08 门闩失败、控制损坏与 old writer

可验证同会话发生 I/O/短写/Close/意图或提交不确定、遗留 writing、context 冲突时，执行一次 disabled 创建。若当前持 writer 锁，不清除 writing、不修复尾部；成功建立门闩且没有活跃 writer 时可报告 quiescent。未持锁时只发布 requested 并非阻塞试锁一次；Busy 不循环。状态不可用时不写“重置后的 active”。

门闩创建失败时，本进程立即停止；持锁方尽力原子提交 phase=disabled 一次，原 writing 也阻止后继持锁者；任一失败不回写 active。若 disabled 门闩和 state 都无法发布且原状态仍健康，则只有 stop=local，code=disable_publish_failed，**不能证明其他 producer 已停止**。不提供错误的全局禁写成功回执；此环境不满足共享故障保证，必须由部署修复后用新入口重新建立会话。后续支持方每次都会重验控制文件，但这不是任意存储失效下的全局停止保证。CE-04 必须复现此双发布失败，并报告范围，不将其标成全局禁写通过。

本协议依靠所有支持 writer 遵守统一锁/门闩。CE owner 的 context/stage/end/event 以及 producer 只调用新 commit；生成器/公共库不能向应用暴露或导出旧内部 append 所需的身份参数。新 commit 拒绝原始事件流/生命周期角色伪装；CE-03 的分派测试必须证明不存在新旧写路径混用。

旧二进制与显式手工 `execution append`/直接 OpenWriter/同 UID 自写文件不理解 CE 运行目录，也无法从不变的 2.0.0 JSONL 区分它；既无持久标记又要保留旧路径原义时，不能声称会在内核阻止这种绕行。它们**不是 CE 支持 writer**。禁止将旧工具用于 CE 活跃 Execution，安装/握手拒绝混装，不将 event-only 公开 API 变成旧 append 桥接。若未来要求强制拦截任意旧二进制，必须另立数据根能力标记/权限隔离/代理方案并升级契约，不在 CE-1 暗加 xattr、record sidecar 或旧根标记。A01 的“混装拒绝”指受支持 API 的精确握手拒绝，不声称 sandbox。

## P09 诊断、部署和清理

健康共享控制目录中通过 diagnostic O_EXCL 保证至多一个固定 stderr 行：`{"ok":false,"code":"execution_observation_unavailable"}`。单条拒绝、终态冲突、安装/控制/写入失败消耗同一预算；Busy、正常 off、无引用、未绑定 emit、正常 closed/noop 不诊断。diagnostic 发布失败时每个 BASHPID 最多一次退化诊断，不能声称执行级恰好一次；本地预算不因 detach/source 清零。不能打印 REF、key、路径、typed 值、事件、环境、ECF_PASS 或 CLI 原文。stderr 失败不影响业务。诊断预算不决定写入是否合法。

首版不自动清理、不实现恢复/cleanup CLI。关闭/detach 不删控制目录。部署者预先约定 runtime 专用容量、监测和维护责任；不存在可用容量时本次 begin 失败并保持业务原结果。不能通过自动删除活跃目录腾挪空间。清理在停止新 CE 启动的维护窗口按 boot/PID/starttime 验证无有效 owner，对该 session writer 锁非阻塞独占成功后才可删除；先使引用失效，再清除目录，不影响任何 JSONL。年龄/TTL/closed 标记独自不足以证明可删；不得删除或更换活跃锁 inode。不能证明无活跃使用者就保留；跨 boot 的目录仍须在维护窗口验证不可能有活跃锁。旧引用永不因清理而重建。

平台资格由 CE-07 固定节点本地 FS、权限、procfs、原子 rename（含 NOREPLACE）/flock、共享记录根行为和维护流程。本地设计/测试不代表 HPC 通过。临时安装只在 CE-06 的隔离目录，保留现有试用/正式安装；目标部署和发布须独立授权。
