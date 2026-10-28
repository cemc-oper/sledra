# 独立 Execution 记录核心（P1-01）

本包实现 [Execution 2.0.0 契约](../../spec/execution-v2/README.md) 的 Go codec、部署根、入口日期/16 桶布局和顺序 writer。它不导入旧 context、Registry、event、spool，也不依赖 Python、Manifest、producer/session 或数据库。P1-02 的 [CLI/Shell owner](../../integrations/bash/execution-v2.md) 已实现生命周期；旧 `sledra` 命令保留原语义，只有显式 execution 子命令进入本包。

## 接口与职责

| 接口 | 行为 |
|---|---|
| `NewCodec`、`Codec.Validate` | 从独立嵌入 schema 建立 root/run/event 校验；JSON 字节、精确数值、重复键、Unicode、时区/时间边界单独校验，无远程资源加载 |
| `Codec.Encode`、`Decode` | 编码/解码 JSON 值树；数值保留 json.Number 的原始十进制文本；对象键不做 NFC；Encode 输出不含终止 LF |
| `Codec.NewStart`、`NewEvent`、`NewID` | 自动分配 UUIDv4/发生时间；开始可无 task/engine/cycle/Run；同一事实重传复用返回 bytes，新事实才重新分配 |
| `EqualJSON`、`Equivalent` | 对已解码/已校验内容作精确数字比较；Equivalent 仅规范已定义的时间字段，不建立 canonical 摘要身份图 |
| `InitRoot`、`OpenRoot`、`Root.Config/Close` | 部署级不可覆盖发布/读取 sledra.json；已有相同配置幂等，拒绝不同配置、混合旧布局、缺标记非空根和 symlink；固定配置不从业务变量推断 |
| `Root.Create` | 接受 prepared started bytes，按该入口时间/新 ID 定位并以 O_EXCL 创建文件，立即写开始；同 ID 不覆盖旧文件 |
| `Root.OpenWriter` | 只按给定冻结身份/入口时间打开已有文件；缺失不重建，半行/空尾不修复，不查询最新执行 |
| `Writer.Append/Close/Path` | 一次验证后单次追加完整 LF 行；路径不随事件时间/cycle 改动；校验拒绝不污染文件，I/O/短写/坏尾永久禁写该对象，Close 幂等保留错误 |
| `ReadRecord`、`Code` | 单对象有界读取/固定错误分类；不处理整份历史文件，也不打印输入或环境 |

编码输入是 map[string]any/[]any JSON 值树，标量支持 nil、bool、string、json.Number、int/int64/uint64、float32/float64。精确整数/小数使用 json.Number 或 int64；调用方已经舍入过的 float64 无法恢复原精度。编码前有有界树遍历，最终输出仍严格受契约 65536 字节限制；追加时再增加一个 LF。

## 写入边界

一个 Writer 只支持一个 owner 顺序使用；不同 Execution 可并发，分别创建文件。文件操作锚定 os.Root，拒绝特殊文件、symlink 目录/文件和硬链接的执行文件；没有每执行 metadata、session lock、全根递归配额扫描或 segment 协议。仅查询当前文件尾字节来拒绝坏尾。初始化的临时文件只用于原子发布根配置，返回后不保留。

Append 成功表示一次完整写调用写入了包含 LF 的全部 bytes。Close 的结果必须继续检查；完整写后仍报错、短写或关闭失败都视为结果不确定。不自动重试，不承诺 fsync/断电持久性或任意共享卷 I/O 故障下的硬时延上限。文件失败后原始 bytes 保留，不原地修复或删除历史。

**对象内 disabled 不是跨 CLI owner 状态。** 重新打开完整尾部的文件不会知道此前是否发生过“整行已写但返回错误”。P1-02 已在 Shell owner 保存禁写状态并禁止再次追加；不能绕过 owner 只调用 OpenWriter 就宣称恢复安全。一次 context、结束幂等由 P1-02 owner 管理，阶段状态由 P1-03 owner 管理；本包提供比较/校验原语，不从文件扫描重建一套登记状态。

P0 的 Run 格式可校验，但本包没有 Run 创建、候选接受或 current_run 发布接口。测试与正常写入都是独立 Execution。

## 验证

在 recorder 目录执行 `make check`、`make test`，会包含本包。共享的 53 个接受/拒绝/冲突样例及 9 个精确比较向量由 Go 测试直接读取；编码后再次校验及等价比较。布局向量、32 执行并发、跨午夜与三种 cycle_kind 晚到、不可覆盖、权限/特殊文件、短写/坏尾/close 不确定分别有回归。

[测试用 Go producer](testdata/write.go) 实际写独立记录，[Python 对照](../../tests/execution/test_writer_contract.py) 使用未改动的 P0 校验器检查两次启动的文件、数值精度、上下文和旧文件保留。它不使用 Shell owner 或 P2 reader，不是 P2-04 完整闭环，也不是实际业务执行示例。根 make test 已纳入该项，单独运行：

```bash
uv run --locked --offline --no-python-downloads python -m unittest discover -s tests/execution -p test_writer_contract.py -v
```

新版 schema 位于本包独立 contract 目录，与 spec/execution-v2/contract.schema.json 字节一致；根 make check 中的资源检查只追加新版比对，保留旧生成/保护范围不变。实际进度和验收记录仍在仓库外同一份实施计划中。
