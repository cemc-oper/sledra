# CLI、版本与可搬运资源

公共记录入口为 `sledra`。运行数据保存到显式 `SLEDRA_ROOT`，与安装资源分开。

## 帮助与版本

以下命令无需运行环境、producer 配置或外部契约文件：

```bash
build/bin/sledra --help
build/bin/sledra registry --help
build/bin/sledra registry run-create --help
build/bin/sledra registry attempt-create --help
build/bin/sledra context validate --help
build/bin/sledra run finish --help
build/bin/sledra event --help
build/bin/sledra --version
```

`-h` 等同 `--help`，也支持 `sledra help registry run-create`。帮助选项单独放在命令名后，说明输出到 stdout。`--version` 输出包含 `software_version`、`commit`、`build_time`、`source_sha256`、`go_version`、`goos`、`goarch` 和 `contract_version` 的 JSON。

`make build` 默认软件标识为 `dev`，写入完整 Git 提交；工作树有修改时追加 `-dirty`。构建记录输入文件摘要和 UTC 时间。`SLEDRA_VERSION` 可指定标识，`SOURCE_DATE_EPOCH` 可固定时间；复用相同构建时保留原时间。无 Git 元数据的独立源码记录 commit 为 `unknown`；普通 `go build` 的软件标识为 dev，未知提交/时间/源码摘要如实记录 unknown，工具链和目标平台仍为实际值。软件、契约和阶段编号互相独立。

## 记录命令

| 命令 | 必需参数 | 结果 |
|---|---|---|
| `registry run-create` | `--request-file FILE --context-out FILE` | 登记 Run 并发布不可变 context |
| `registry attempt-create` | `--context RUN --request-file FILE --context-out FILE` | 在显式父 Run 下登记 Attempt |
| `context validate` | `--file FILE` | 校验 context、摘要和不可变父记录 |
| `run finish` | `--context RUN --scope-file FILE` | 追加明确覆盖 Attempt 的 finish 声明 |
| `event` | `--context ATTEMPT --type TYPE` 及 payload 或类型化进程参数 | 记录绑定 context 的事件 |

记录命令均要求 `SLEDRA_ROOT`，事件还要求 `SLEDRA_CONFIG`。请求字段见源码 [context 说明](https://github.com/cemc-oper/sledra/blob/main/recorder/context/README.md)。事件支持 `--payload-file`、类型化进程参数、可选 `--event-id` 和 `--strict`；producer role 来自部署配置。

成功记录时 stdout/stderr 为空。生命周期失败返回 1，在 stderr 输出分类 JSON `{ "ok": false, "code": "..." }`。business 模式事件失败返回 0，stdout 为空，stderr 为包含 `dropped: 1` 的有界诊断；strict 模式失败返回 1。Bash 钩子最多输出一次固定诊断，并向 owner 返回 0。帮助/版本是主动信息查询的例外；没有 `sledra exec` 命令。

## 契约与路径

未设置或空的 `SLEDRA_SPEC_DIR` 使用内嵌资源，不按 cwd 查找、不解压、不写安装目录。覆盖目录必须为绝对路径，包含九份 canonical JSON 资源（含 `capabilities.json`），且准确匹配已安装契约。空白和对象键顺序可以不同；schema、目录角色/类型和能力值必须相同。不相关附加文件忽略。后续 1.x 信封可按兼容规则读取，但直接换成不同的 1.x schema 会被拒绝。

| 覆盖错误 | 分类代码 |
|---|---|
| 非空相对目录 | `contract_path_invalid` |
| 必需资源缺失或不可读 | `contract_unavailable` |
| JSON 损坏或版本、schema、目录、能力不兼容 | `contract_incompatible` |

无效覆盖不会静默回退。事件仍应用已有 business/strict 策略；这些资源错误不回显文件内容或路径，其余根目录/配置错误保持原优先级。开发用 `sledra-spec-check --spec-dir` 仍允许测试相对路径，并输出诊断细节。

在不同 cwd 直接调用时，为请求、输入、输出使用绝对路径。请求中的 `definition_file` 是调用方输入，不是安装资源；相对值仍相对当前调用目录解析。独立 owner 按脚本位置查找库、夹具和 payload，在示例目录执行，只写显式工作目录，默认用内嵌契约，可在只读安装中运行。

## 开发验证

`make check` 在私有副本中重新生成 canonical 资源，并逐字节核对两个 Go embed 目录。显式契约更新时修改 `spec/build_contract.py`，重新生成后执行 `python3 scripts/sync_contracts.py`，再审查差异和完整回归；不得独立编辑 embed。

`make test` 验证无记录环境的帮助/版本、含空格路径、移除源码契约的只读副本、不同 cwd、无效覆盖与业务输出等价性。canonical 向量和冻结 M1 handoff 保留为兼容输入。安装隔离验收见[安装](installation.md)，目标 HPC 资格另行验证。
