# Execution v2 文件读取（P2-01）

本包直接读取 [2.0.0/布局 2 契约](../../../../spec/execution-v2/README.md) 的文件，没有数据库、Registry、SourceLease、投影或源码目录依赖。包顶层导入无副作用；codec 使用标准库和现有 jsonschema，schema 从 [独立包资源](../resources/execution_v2/contract.schema.json) 加载。旧 resources/contract 和旧分析入口继续服务原格式。

当前交付是 codec、显式日期窗口扫描与 Run 文件查找。[Execution 去重/冲突汇总、次数/分组/耗时](summary.md) 已由 P2-02 实现；sledra-history CLI 和默认七天窗口留在 P2-03，不将此开发 API 当作完整历史查询工具。

## 读取与源位置

在安装了当前分析包的 Python 3.12 环境中使用：

```python
from sledra_analysis.execution.reader import Fact, FileReader, ReadIssue

with FileReader("/absolute/independent/root") as reader:
    # 两端包含，按根配置的记录日期分区选择；不按业务 cycle 改窗口。
    for item in reader.iter_events("2026-10-27", "2026-10-28"):
        if isinstance(item, Fact):
            print(item.source, item.data)
        elif isinstance(item, ReadIssue):
            print(item.code, item.source)
```

FileReader 只打开已有、兼容的绝对根，校验 sledra.json/命名空间/固定分区偏移并拒绝已知旧布局。不初始化、收养或修复根。`config` 返回独立副本；使用 with/close 释放 reader 句柄；如果保存了生成器并提前停止迭代，应调用该生成器的 close 释放它仍持有的文件句柄。窗口参数必须同时给出 ISO 日期或 date；显式 `iter_events(None, None)` 才读所有保留的记录日期，没有隐式扩窗。今天执行旧 cycle 的事实仍按今天启动分区读取。

迭代结果为 `Fact(kind, data, source)` 或 `ReadIssue(code, source, record_id)`。Source 包含根相对路径、JSONL 一基行号、零基字节偏移和物理字节长度（含 LF）；整 Run 文件或目录的 line 为 None。错误只保存代码和位置，不复制原始日志/坏记录。顺序是文件系统遍历顺序，不代表时间或因果顺序。

事件文件按固定“日期/16 桶/UUID.jsonl”检查，执行 ID 必须与文件名一致；started 的入口时间必须匹配日期路径，后续事实可跨日。没有 started 的合法行仍可读，不从它的 occurred_at 重定启动分区。重复或互相冲突的合法事件全部保留，读取层不去重、不选择最后一条、不补 context/结束；P2-02 将解释这些事实。

## 行大小、活动文件与错误隔离

单对象上限 65536 字节，LF 另计；恰好 65536+LF 可读。只接受完整 LF 行，空行/CRLF/非法 JSON/Unicode/重复键/越界数值分别报告。超长物理行以有界块排空至下一 LF，报告一次 limit_exceeded 后继续；不整文件加载，不因坏行或坏文件隐藏其他合法执行。

每个文件以打开时的大小为本轮字节上界。末尾未完成行/空文件报告 incomplete_tail，不解码，不追加/修复；下一次调用从原文件重扫，LF 完成后即可读。读取中遇到截断/提前 EOF 报 source_changed，后续扫描可重试；这不是跨文件原子快照，也不承诺检测任意等长原地改写。根配置按部署不可变契约在 reader 打开时固定。

此实现使用 Linux/Unix dir_fd/no-follow 文件接口，目录和文件操作锚定只读 FD，根的每个祖先/日期/桶/文件均不跟随 symlink；非普通文件和多硬链接文件拒绝，FIFO 非阻塞打开后按类型拒绝。单桶/文件 I/O 错误与布局错误带位置返回并继续其他可读位置。无底层 I/O 的硬时延承诺，真实共享卷资格仍未验收。

## 数值与时间

`codec.validate(kind, raw)` 支持 root/run/event，返回 int/Decimal 等解码值。整数 token 为 signed int64；小数/指数 token 保留精确十进制值，binary64 转换只查范围/非零下溢，不参与身份比较。零值的指数若超出 Decimal 表示范围，仍精确保存为 Decimal 零（保留负零符号），与现有 Go 编码行为一致。不要把这些值无条件转成 float 或直接交给不支持 Decimal 的 JSON encoder。

时间按 UTC 秒/纳秒比较，不截断为 Python 微秒。`equivalent` 不将 bool 当数值，不做 NFC；`comparable` 仅归一已定义的 event/run 时间字段。没有全局 canonical 字节、摘要身份图或身份缓存。原始 token/转义写法可从 Source 指向的文件检查，不复制另一份存储。

## 合成 Run 读取与缺失引用

`iter_runs()` 读取 runs/YYYYMMDD/UUID.json；可传 Run ID 集合只打开匹配文件。`resolve_runs(ids)` 扫描所有保留的 Run 创建日期，**不受 Execution 记录窗口限制**，也不找“最新 Run”。返回 RunLookup.references 与 issues，所有候选保留原位置：

| status | 含义 |
|---|---|
| found | 已看到合法且内容一致的 Run 候选，命名空间/文件 ID/创建日期路径一致 |
| missing | 当前保留的文件范围已检查，未看到该 Run 文件 |
| conflicting | 相同 ID 的合法候选内容不同，保留全部，不选赢家 |
| unavailable | 目标文件非法/不可读，或目录遍历不完整，无法判断缺失/唯一性 |

目录级错误会保守影响所有待解析引用；可定位到某个 ID 的文件错误只影响该引用。found 只表示读到了元数据，不证明 context 的 scope/cycle 适用、真实来源或 Run 传递新鲜度。缺失/冲突/不可用不会使已经读到的 Execution 事实消失；该查找结果只在内存中临时存在，不创建查询索引或登记记录。

本期 Run 全为明确的合成测试事实。没有创建、发布、接受生产 Run 或查询 ecFlow 的接口。

## 验证与安装

[codec 测试](../../../tests/test_execution_codec.py) 执行原 53 共享样例与 9 比较向量，[reader 测试](../../../tests/test_execution_reader.py) 覆盖日期/身份/位置、坏行/长行/尾部/截断、Run 引用和只读边界。安装回归实际构建 wheel，在脱离源码的新进程中读取；禁止写入、网络与 SQLite 连接，验证旧资源字节保留和实际 Go 编码的数字边界。

在仓库根执行：

```bash
PYTHONPATH=analysis/src uv run --locked --offline --no-python-downloads python \
    -m unittest discover -s analysis/tests -p 'test_execution_*.py' -v
uv run --locked --offline --no-python-downloads python \
    -m unittest discover -s tests/execution -p test_reader_install.py -v
```

analysis 的 verify.py/all 与原 make test 会发现 codec/reader 测试，根 make test 追加 wheel 回归；根 make check 比对独立资源。sync_contracts.sync_execution 只同步两个新版目标，本轮仅在临时副本测试同步，不对真实工作区运行旧资源同步脚本。
