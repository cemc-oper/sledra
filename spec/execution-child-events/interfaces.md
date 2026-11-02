# CE-1 Shell/CLI interface contract

CE-01 contract baseline, 2026-10-30. CE-02 implements the internal Go CLI/coordinator; CE-03 implements the opt-in owner and event-only Bash APIs. CE-04 covers local cross-process fault acceptance; application, installation and target-node qualification remain separate. Data remains Execution 2.0.0/layout 2. See [offline validation](README.md).

## P01 启用与公共 API

数据格式仍为 `2.0.0`，布局仍为 `2`，软件仍为 `dev`；`CE-1` 不是软件 2.1.0，也不是新的 JSON schema 版本。只有首次 `sledra_execution_begin --child-events [原 start 参数]` 选择协调路径。参数仅可出现一次，无值；重复、`--child-events=true`、未知参数都作为首次 begin 拒绝并冻结 disabled。旧签名及默认单 owner 路径不变；已有 active/off/closed/disabled 入口不能升级或重新 begin。

增加部署配置 `SLEDRA_RUNTIME_DIR`：绝对、部署者显式指定且已验证文件系统语义、当前 UID 独占，位于记录根和业务/生成/清理树之外；不从 TMPDIR、SLEDRA_ROOT 或业务配置推断。首次 begin 冻结 mode/bin/root/runtime。未设置、不可用或不满足 P03 时停止本次观测，不回退旧 append。`SLEDRA_MODE` 的 off、execution-v2、歧义及未知值规则沿用旧接口。off 不检查路径、不访问文件、不运行 CLI、不诊断。

`SLEDRA_EVENT_CONTEXT` 是可导出的不透明 event-only 引用。协调 begin 首先清除当前入口继承的引用，只有 started 写入及 close、active 状态提交、描述原子发布和成功回执校验全部完成才发布新引用。off、输入/安装/初始化失败不发布。重复 begin 不清除自己已经发布的引用；普通旧入口也不得把继承引用当作自己的身份。source 本身不清理公开引用，允许业务先 source 后 attach。所有其他公共 SLEDRA ID/token/context 值都不是身份来源。

新增 source-only `integrations/bash/sledra-events.sh`：

| 函数 | 固定签名与规则 |
|---|---|
| `sledra_event_attach` | `[--context REF]`；未传选项使用环境；重复选项/多余参数拒绝本次。显式空值不回退环境。无引用时静默且保持未绑定，随后可显式 attach |
| `sledra_event_emit` | `NAME [TYPED_VALUES]`；沿用旧 `--string/--int/--float/--bool/--null`、精确数值、重复键和大小规则；拒绝 `execution.*`/`stage.*`；不接受原始 JSON、身份、角色、owner key 或重试选项 |
| `sledra_event_detach` | 无参数；只清除当前进程绑定，不写共享状态、不结束/删除父执行，不清除失效引用的本地记忆 |

所有公共 hook 返回 0、stdout 空，不改变 options、trap、业务 `$?`/PIPESTATUS 保存方式，不安装 trap，不运行业务。观测 CLI 的 stderr 被捕获，诊断按 P09；启用 xtrace 时敏感的私有变量赋值/CLI 调用沿用现有库的屏蔽并恢复规则。source 只定义函数，重复 source 不重置状态。私有变量使用 `_SLEDRA_CE_` 前缀，每次赋值后显式取消 export，包含 `set -a` 情况；继承带 export 的私有变量全部丢弃。原 `_SLEDRA_V2_` owner 检查保留。

绑定记录当前 BASHPID。inline 子 shell 即使继承父内存也必须在子 shell 显式 attach；BASHPID 不同的缓存不能直接 emit。external Bash 自行 source 事件库并 attach，不导出函数。事件库允许 BASH_SUBSHELL 非零的显式绑定；旧 lifecycle hook 在子 shell 中继续 no-op。父 owner 可使用原 event 函数，但协调模式下也走本协议。producer 没有 context、stage、end 或创建权限。

事件侧第一次 attach 冻结本 BASHPID 的 mode/bin；off 冻结 off，不因后续改环境重开。启用但缺引用不冻结具体会话，允许之后显式 attach；未 attach 的 emit 不隐式初始化。attach 遇 Busy 保持未绑定，不自动再 attach；后续独立显式 attach 允许再次核验。格式/身份错误的未绑定 REF 仅拒绝本次，不借此禁用其他有效会话；已绑定会话变为不可核验时本地永久停止。

相同引用重复 attach：验证当前进程绑定，健康时幂等，不生成事件。不同引用在已有绑定时拒绝，保留旧绑定；需显式 detach。detach 后可绑定不同健康会话；同一失效会话不能复活。为保持本地空间有界，进程一旦因不确定结果/不可用控制状态失效，将本进程事件接入锁定 disabled（包括 detach/source/换环境），须新 Bash 才能重试；单条输入拒绝、Busy、无引用、不同引用误切换不触发该锁定。closed/closing 不作为可重开的绑定。

context/stage/end 的公开签名不变。相同 context 幂等；不同 context 永久禁写。stage 单活动、顺序配对、不自动补 ended；非法 stage 调用不污染健康状态。相同 end 幂等，冲突 end 保留首份。没有 implicit begin、父文件修复、身份恢复、业务重试或后台补写。

## P02 版本握手与内部命令

新命令族为 `sledra execution child-events`；下表是内部协议，不是业务调用教程。旧 `execution prepare/append/init-root` 的 0/2/3、输出和默认语义保持。新族使用独立退出码，禁止借旧码暗示 Busy 可重试。

| 命令 | 输入与权限 | 行为 |
|---|---|---|
| `capabilities --interface CE-1 --protocol 1` | 无身份 | 只输出 `CE-1<TAB>execution-child-events<TAB>1<LF>`；不访问根/运行目录；不匹配即拒绝 |
| `prepare KIND [OPTIONS]` | KIND 为 start/context/event/stage-start/stage-end/end；沿用原 kind 参数但禁止 --previous-event（共享状态独占幂等判定），非 start 的 execution-id 仅由库内部提供 | 纯编码，不访问记录/控制目录；分配一次 request_id 和完整事件；start 另外分配 session_id/owner_key；不复用旧 CLI 回执语义 |
| `open --root ROOT --runtime-dir DIR --owner-pid PID` | stdin 是一次 start prepared；首个 owner | 核对调用方 PID 身份，按 P03 创建一次会话及 started；失败不第二次调用 open；无引用时不尝试找回本次 begin |
| `attach --context REF` | event-only | 有界读取并核对描述/身份/状态；不写 JSONL、不建身份；非阻塞锁检查 |
| `bind --context REF` | event-only；与 attach 完全相同的校验 | 成功只返回 `CE-1<TAB>bound<TAB>execution_id<LF>`，供 Bash 取得已核验身份；不返回 key，不解析 Shell JSON；失败沿用完成回执 |
| `diagnostic --context REF` | 同会话 | 独立核验控制绑定并以 O_EXCL 领取一次共享诊断预算；成功返回 `CE-1<TAB>diagnostic<TAB>granted\|suppressed<LF>`，失败返回 26 完成回执并使用本地预算；不改 writer 状态 |
| `commit --context REF --role event\|owner [--owner-key KEY]` | stdin 是一份 prepared | event 只允许业务 event；owner key 和仍有效的 owner 祖先身份校验后才允许生命周期操作；内部按 P05–P07 协调 |
| `result --context REF --request-id UUID --event-id UUID --record-sha256 HEX` | 同会话，只读 | 非阻塞取得同一 writer 锁，有界查最后回执；绝不 append、清空 writing 或解除 disabled |
| `disable --context REF` | 已验证的同会话 | 单次发布禁写请求；非阻塞尝试锁以确认静止，不重试、不 append |

库在启用 child-events、attach 前做一次精确握手，冻结已核验的 SLEDRA_BIN 绝对路径；描述不能提供可执行文件路径。旧二进制不识别命令、异常退出或 malformed 回执均停止本次接入，不降级。producer 不从引用猜测另一安装。默认旧模式不增加新握手或运行目录前提。

CE-03 补齐上述内部 bind/diagnostic 专用传输；原 attach、九字段完成回执、prepared、CE-1/协议版本及 CE-01 向量保持不变。bind 复用 attach 的一次非阻塞锁检查，只暴露 execution ID；diagnostic 不等 writer 锁、不改 JSONL/状态 revision，已有合法 diagnostic 时明确 suppressed。两种专用成功行均由 Bash 有界且精确匹配；异常使用本进程预算，不把输出当作业务或 owner 权限。

prepared 为一个 LF 结尾的九字段 TSV：`1, request_id, execution_id, event_id, auxiliary, session_id, owner_key, record_sha256, event_json`（这里的逗号表示字段分隔，实际分隔为 TAB）。UUID 均为旧契约的小写 UUIDv4；auxiliary 对 start 为入口 UTC 时间，对 stage-start 为 stage_id，其余为 `-`；session_id、owner_key 仅 start 有值，其余为 `-`。owner_key 为 Go CSPRNG 产生的 32 随机字节的小写 hex，state 仅保存 SHA256。record_sha256 是 event_json 原始 UTF-8 bytes（不含 LF）的 SHA256 小写 hex，内部只用于核查同一请求。event_json 是无原始 TAB/CR/LF 的单行 JSON，复用原 codec。Bash 按固定字段/总长/ID/单行检查，不 eval/source。一次 emit 只 prepare 一次、commit 一次；事件 occurred_at 是该 hook 的 prepare 时刻，不在 tail 补录。Bash 不自行拼 JSON。

完成回执为一行九字段 TSV：`1, result, code, request_id, event_id, revision, phase, stop, auxiliary`。revision 是有界非负十进制整数或 `-`；无对应 ID 为 `-`；phase 为 P04 枚举或 `unavailable`；stop 为 `none/local/requested/quiescent`。open 成功的 auxiliary 是 REF，其他为 `-`。attach/result/disable 不需要 prepared，其 request/event 字段无值时为 `-`。result 成功确认必须回显请求的两个 ID 和原提交 revision。所有 code 为本文件规定的固定 ASCII 枚举，不含输入。exit/status 不匹配、额外字段、CR/NUL、缺失/多余 LF、超长或未知枚举均视为回执损坏。能力/prepare 成功 rc=0；它们的拒绝/失败也使用下表的新码，但 prepare 的成功输出仅为 prepared。

| 新退出码 | result / 典型 code | JSONL 与公共处理 |
|---|---|---|
| 0 | `committed/ok`、`noop/same_context`、`noop/same_end`、`attached/ok`、`confirmed/ok`、`capable/ok`（capabilities 使用上面的固定专用行） | commit 已完整追加并 close、状态已提交；noop 无 JSONL 追加；confirmed 只确认先前提交，不表示当前会话可继续写 |
| 20 | `rejected/usage, invalid_value, duplicate_key, limit_exceeded, reserved_event_name, role_denied, stage_order, reference_switch` | 确认本条未触及 JSONL；不禁用健康共享会话；首次 begin 输入失败仍本地 disabled |
| 21 | `busy/writer_busy` | 本操作未获准、无意图/追加；不自动重试、不永久污染健康会话；合法 end 可能已经发布 closing，见 P07 |
| 22 | `closed/closing, closed, conflicting_end` | 无新追加；冲突保持首个 end，诊断但不覆盖；不是恢复/重发许可 |
| 23 | `disabled/session_disabled, indeterminate, control_unavailable, stale_writing, conflicting_context, disable_publish_failed` | 永久停止本进程；stop 字段区分仅本地、已发布请求、已确认全局静止；不从 disabled 恢复 |
| 24 | `unknown/result_unknown` | 本次结果不可证明，不再 prepare/commit；执行一次 disable，且锁定本地 disabled |
| 25 | `incompatible/protocol_mismatch, installation_incompatible` | 无写入；关闭本次接入 |
| 26 | `unavailable/invalid_reference, identity_mismatch, owner_gone` | 无写入；本地拒绝，不借一个错误引用禁用其他会话 |

失败 stdout 仍为上述有界完成回执；无法安全生成回执时 stdout 空，CLI stderr 最多 256 字节固定 JSON code。hook 永不透传内部 stdout/stderr。SIGKILL、未知退出码或输出被截断不是可靠的“未写入”证明，按 P06 处理。Busy 可在下一次独立业务事实再尝试，但不能重发丢掉的事实。

