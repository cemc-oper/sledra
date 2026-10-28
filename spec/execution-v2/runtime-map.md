# P0 收口：P1/P2 实现位置与依赖边界

本文件固定源码位置、资源分发与验收入口，不新增任务清单。任务状态和 session 交接仍只在仓库外原 implementation-plan.md 维护。格式与接口分别沿用 [记录契约](README.md)、[Shell/CLI 接口](interfaces.md)，不得另建协议。P1-01 已落实 recorder/execution 的 codec/root/writer/独立资源及测试；P1-02 已落实 cmd/sledra/execution.go 的生命周期 CLI、前置分发/根隔离/帮助与版本信息、Bash owner 和 tests/shell/test_execution.py。P1-03 已扩展 prepare event/stage-start/stage-end、三个公共事件/阶段函数和相应 Go/Shell 测试；P1-04 已增加 examples/execution 的实际示例、tests/shell/test_execution_example.py 的真实业务/信号/故障对照；P2-01 已落实 analysis execution 的 codec/reader、独立 wheel 资源与共享/扫描/安装回归；P2-02 已落实独立 summary.py、精确去重/冲突、分组计数/Run 解释/阶段耗时与相应回归；P2-03/P2-04 仍未实现。

## 共享检查与格式收口

在 Sledra 根执行：

```bash
uv run --locked --offline --no-python-downloads python spec/execution-v2/verify.py
```

[verify.py](verify.py) 统一发现本目录的契约、接口范例与一致性测试；根 `make check` 和 `make test` 都调用它，失败向上传播。[cases/cases.json](cases/cases.json) 是共享输入及预期目录，目前 53 项：原 45 项内容/结果不变，新增正常接口范例对应的合成 JSONL 和 7 个 ASCII 字段换行拒绝例。Go/Python 实现各自读取同一份向量，不通过改写旧资源使测试通过。

P0-03 修正了 schema 的 `$` 可在末尾 LF 前匹配的问题：UUID、namespace、scope、参数键、分区偏移及事件名明确排除 CR/LF。业务属性的转义换行仍合法。这是落实已定 ASCII 标识规则，不改变 2.0.0 字段、单位或业务语义。全部 schema 引用保持本文件内的 `$defs`，不访问远端。

[test_consistency.py](test_consistency.py) 检查 schema 顶层/指定入口与样例一致、引用闭合、整数词法、标识换行边界，并将 Bash mock 记录的固定公开 argv 与 [合成事件范例](cases/interface-example.jsonl) 比较。比较不实现 Go 参数解析器、写入器或生产 Python reader，样例也不冒充实际跨语言产物。

以下决定已经收敛，无需在 P1 重选：2.0.0/布局 2；根 sledra.json；命名空间与固定偏移；UUIDv4；入口日期与 SHA256 首 hex 的 16 桶；single/none/multiple 与 context 缺失区别；精确十进制比较及 int64；已定字节/文本/深度边界；一次 context；owner 顺序写；prepare/append 的固定回执、0/2/3 返回码和禁写状态。Run 接口只保留 P4 边界，P1 正常运行保持独立。

## P1：记录器与 owner

| 位置 | 所属任务与最小职责 |
|---|---|
| 新增 `recorder/execution/codec.go`、`codec_test.go` | P1-01：2.0.0 解码/校验/编码、UUID、精确数字与内容比较；直接执行共享向量。仅使用标准库及现有 JSON Schema 依赖，不调用旧 event.New/speccontract.New/context.New |
| 新增 `recorder/execution/root.go`、`writer.go` 及同目录测试 | P1-01：独立根初始化/核对、入口日期路径、不可覆盖创建和单执行顺序追加；注入短写/close 不确定/坏尾部；无 session、全根扫描配额或每执行 metadata |
| 新增 `recorder/execution/resources.go`、`contract/contract.schema.json` | P1-01：嵌入独立新版 schema，保证任意 cwd/只读安装可用；字节来源只为 spec/execution-v2/contract.schema.json，不覆盖旧 bundled contract |
| 新增 `recorder/cmd/sledra/execution.go`、`execution_test.go` | P1-02/P1-03：init-root、prepare 六种 KIND、append，严格结果码/回执/输入限额；Go 主入口先分发 execution，再进入旧分支；通用错误输出保留旧命令退出 1 的既有语义 |
| 修改 `recorder/cmd/sledra/main.go`、`information.go`，必要时 `internal/buildinfo` | P1-02：前置分发、独立帮助、新版错误码；旧帮助主题/命令/contract_version=1.0.0 保留。新版格式/布局信息以新增 execution_format_version/execution_layout_version 字段单列，不把软件版本当格式版本 |
| 新增 `integrations/bash/sledra-execution.sh` | P1-02/P1-03：六个已定公共函数；未导出 owner 状态、重复 source/begin、防继承和 subshell 规则、一次 context、当前阶段、幂等结束；在本进程记住 disabled，不能只依赖某次 CLI 的 poisoned 变量 |
| 新增 `tests/shell/test_execution.py`、`test_execution_example.py`，扩充 `recorder/execution/*_test.go` | P1-02 至 P1-04：真实 Bash/Go 记录、options/trap 不变、实际重启/exec 身份、context/结束/阶段、诊断限额与跨 CLI 故障禁写；测试根均为临时目录 |
| 新增 `examples/execution/standalone.sh`、`README.md` | P1-04：实际可运行的独立闭环，由本次接口范例发展而来；无需 takflow/ecFlow/旧 launcher/手工身份；成功/配置失败/退出/信号/观测故障分列 |

P1 以新版校验器和真实 JSONL 断言验收，不等待 P2 查询。已有 Go Makefile 会发现新包；根 make test 已接入新的 Shell API 与示例测试，旧 tests/shell/verify.py 保留原义。源码示例和测试只在临时根执行，不运行 examples/generated/generate.py。对照中的业务命令只能执行一次，不因观测故障补跑。

P1 的新调用链应为 `新 Bash owner → sledra execution → recorder/execution → 本执行文件`。同一二进制保留旧命令 import 不代表触发旧机制；须验证新分支不调用旧构造器、不建立 registry/documents/spool 目录。必要时使用局部 writer I/O 注入测试，不能为了测试复用旧 session/spool 框架。共享小型原语需确实中立；新建 UUID 和 SHA256 的标准库调用无需引入整包旧依赖。

旧布局识别只检查根下一层已知旧入口（registry、documents、contexts、spool），不递归扫描全部根；非空无根标记也不被 init-root 收养。旧写命令遇新版根标记则在创建旧服务之前拒绝；合法旧根原语义不变。不要把这点根隔离扩展为历史迁移器。

## P2：现有分析包中的直接读取

| 位置 | 所属任务与最小职责 |
|---|---|
| 新增 `analysis/src/sledra_analysis/execution/__init__.py`、`codec.py` | P2-01：轻量包，初始化无副作用；新版 JSON/数字/时间/边界校验；只按需复用中立纯函数，不导入 ingest.validation 或 store.canonical 来取得一个函数 |
| 新增同目录 `reader.py` | P2-01：指定记录日期窗口的 16 桶扫描、逐完整行读取与位置/错误报告；尾部半行留待重读，坏文件不掩盖其他合法记录；不修复源文件 |
| 新增同目录 `summary.py` | P2-02：按文件去重、context/end 冲突、可见数/开始数、完整参数/cycle/Run 分组、阶段与估算耗时；不创建 ProjectionEngine、SourceLease、RawStore、快照或 SQLite |
| 新增同目录 `cli.py`，修改 `analysis/pyproject.toml` | P2-03：`sledra-history` console entry 指向 `sledra_analysis.execution.cli:main`；记录日期与业务 cycle 分离，可读/机器输出带覆盖窗口和不完整性说明 |
| 新增 `analysis/src/sledra_analysis/resources/execution_v2/contract.schema.json` | P2-01：独立资源命名空间；package-data 只追加该目录，不替换旧 resources/contract；资源内容与 canonical schema 相同 |
| 新增 `analysis/tests/test_execution_codec.py`、`test_execution_reader.py`、`test_execution_summary.py`、`test_execution_cli.py` | P2-01 至 P2-03：共享向量、坏行/尾部、今天补跑旧 cycle、不同参数/分辨率、合成 Run/alias、未知/缺失/冲突口径；接入 analysis/tests/verify.py 和原分析测试入口 |
| 新增 `tests/execution/test_roundtrip.py`，扩充 `examples/execution/README.md` | P2-04：真实 Go/Bash 写文件后用 Python 直接查询，删除查询输出后可重建；不以 P0 的 mock 或合成范例代替跨语言运行 |

Python 新入口使用已有分析包、同一依赖环境，不建新的服务项目。只有 codec 使用标准库/现有 jsonschema，reader/summary/CLI 调用新版 codec；包顶层不初始化任何旧存储。P0 的 check.py 是开发参考检查器，不能成为安装后的路径依赖；P1/P2 可以参考其中纯函数，但运行时代码必须可独立安装，并执行同一组样例。

查询入口固定为 `sledra-history --root ROOT`。P2 默认扫描根分区时区“今天及此前 6 天”的有限记录日期窗口，提供 `--record-from YYYY-MM-DD --record-through YYYY-MM-DD` 的含端点窗口以及显式 `--all-record-dates`；有限与全量模式互斥。cycle/execution_id/run_id 过滤不能静默扩大窗口；无结果也展示实际范围。这里只固定默认扫描边界，不增加索引、实时 tail 服务或另一套配置协议。

Run 元数据直接读取 `runs/创建日期/run_id.json`；有 Execution 引用但文件缺失或冲突时仍保留执行可见性，单列关联不确定。只有 run_id 时可在保留的 Run 日期目录按文件名查找，不能把 Execution 窗口误当 Run 创建窗口或查询最新 Run。样例是合成的，不调用创建/发布服务。按 Run 展示无时次辅助执行不伪造其自身 cycle，target_cycle 事件过滤也不替代主周期分组。

## 资源、兼容与后续验证

新嵌入资源出现时，扩展既有 `scripts/check_contracts.py`，单独比对 canonical 2.0.0 与新增 Go/Python 资源字节；保留全部旧生成与资源清单检查，不为了混装而扩大旧目录的允许文件集。同步仅触及上述两个新目标。P0-03 不提前复制尚未使用的 runtime 资源，也不修改 scripts/guard_protected.sh 的保护范围。

`spec/1.0.0`、`spec/fixtures`、旧 bundled contract、受保护样例和历史结果继续服务旧路径。旧 CLI、Shell 与 M1/M3 证据不被重命名成 Execution 结果，旧 Attempt 计数不当作实际启动次数。P0 本轮复验旧格式/命令保护；以后每阶段还要检查实际新调用链，P0 不宣称写入/查询运行时已通过。

P1/P2 使用现有 make build/check/test 设施；P2 wheel 的 console entry 和 package-data 是正常可安装查询能力，不是提前制作 P5 试用发行包。`scripts/package_trial.py` 的归档资源、安装试用验收、真实共享文件系统与运行平台资格仍属后续发行范围，当前不改旧包的 contract_version 或宣称新版已包含在试用包。

P1-04 必须完成真实失败禁写和业务对照，P2-04 必须完成实际跨语言读写。P3 的 25 km 稳定门槛、P4 的 Run 来源/失效、P5 的平台证据继续有效。当前没有阻断 P1 开发的格式/接口未决项；目标文件系统时延、外部工作流接入与 Run 可靠传递仍是后续验收问题，不能用本次契约检查替代。
