# 本地试用包安装

Linux 试用包可在源码树外运行，只需目标记录器和 Bash。完整结果验证可选 Python，无需 Go、uv、依赖缓存或运行时网络。安装资源与运行数据分开；这是本地试用交付，公开发行及项目许可尚未决定。

## 构建与验收

从完成 setup 的源码目录执行：

```bash
make package
make test-package
```

package 使用[离线开发构建](../development/workflow.md)，三份工具位于 `build/package/linux-<arch>/bin/`。`dist/` 输出 `sledra-<软件标识>-linux-<arch>.tar.gz`、`.tar.gz.sha256`、独立 `.manifest.json` 和 `latest-package.json`。包内有 `manifest.json` 与 `SHA256SUMS`。默认软件标识 dev，与契约 1.0.0 分开。

默认目标为已安装 Go 工具链的 Linux 架构；指定交叉目标、标识和时间的示例为：

```bash
GOOS=linux GOARCH=arm64 SLEDRA_VERSION=0.0.0-trial SOURCE_DATE_EPOCH=0 make package
```

支持 linux/amd64、linux/arm64 构建。实际二进制必须是匹配架构的静态 ELF64，既无 PT_INTERP，也无 DT_NEEDED；这与 CGO_ENABLED=0 配置分开核验。交叉编译不代表运行通过。当前 native WSL2/ext4 amd64 有本地证据；真实共享/HPC、其他目标与升级资格仍待验证。

`make test-package` 验证已存在的最新包，不重建。选择其他产物：

```bash
SLEDRA_PACKAGE_ARCHIVE=/absolute/path/to/sledra-dev-linux-amd64.tar.gz make test-package
```

相邻目录必须保留匹配的 `.tar.gz.sha256`。错误架构在执行前拒绝；缺失或损坏产物非零退出并保留报告。打包需已准备的 Go 缓存，安装验收不使用这些缓存。

<!-- trial-package:start -->

## 用户目录安装

将包和校验和复制到用户目录，先校验再解压：

```bash
cd /path/to/downloads
sha256sum -c sledra-dev-linux-amd64.tar.gz.sha256
mkdir -p "$HOME/.local/opt"
tar -xzf sledra-dev-linux-amd64.tar.gz -C "$HOME/.local/opt"
install_dir="$HOME/.local/opt/sledra-dev-linux-amd64"
(cd "$install_dir" && sha256sum -c SHA256SUMS)
"$install_dir/bin/sledra" --version
"$install_dir/bin/sledra" --help
```

每份产物选择新的安装位置，不覆盖已有安装。校验和识别内容损坏，不证明发布者身份。manifest 记录软件/提交/源码/构建信息、架构、实际链接、payload 大小/权限/摘要、依赖与试用状态。第三方已有许可文本位于 `share/sledra/licenses/`。

只有 `bin/sledra` 是记录入口；`share/sledra/test-tools/` 下的 sledra-spec-check 和 sledra-event 用于验证。隔离夹具的保留清理工具不随包发放，包也不带 Go 源码、模块缓存、虚拟环境或分析组件。

## 在其他目录运行

合成示例需要 Bash >=5.2 和 dirname/cat/sleep。记录无需 Python；安装可以只读，工作目录必须可写：

```bash
work_dir=$(mktemp -d "${TMPDIR:-/tmp}/sledra-trial.XXXXXX")
mkdir "$work_dir/observability"
STANDALONE_WORK_DIR="$work_dir" SLEDRA_BIN="$install_dir/bin/sledra" \
  SLEDRA_ENABLED=1 "$install_dir/share/sledra/examples/standalone/run.sh"
printf '记录文件：%s\n' "$work_dir"
```

脚本按自身位置查找资源，默认契约内嵌。新目录中设置 `STANDALONE_PAYLOAD_MODE=fail` 可得到退出码 7；`SLEDRA_ENABLED=0` 使用相同 owner/payload 但关闭观测。context、Registry、spool 和 finish 是事实，不是分析结果；直接调用配置见[运行配置](../guides/runtime.md)与 [CLI](../reference/cli.md)。

安装资源中的 `run.sh` 管理生命周期，`payload.sh` 只执行业务，`fixtures/` 提供身份与 producer 输入。如何登记 Run/Attempt、保存退出码、声明 finish，以及逐项读取结果，见[独立示例详解](../guides/standalone.md)。包内也可直接阅读 `share/sledra/examples/standalone/README.md`。

## 验证安装结果

完整验证只需 Python >=3.12 的标准库，不需 jsonschema、Ruff、uv 或 pip：

```bash
test_parent=$(mktemp -d "${TMPDIR:-/tmp}/sledra-package-test.XXXXXX")
report_dir="$test_parent/report"
python3 "$install_dir/share/sledra/test-tools/verify.py" \
  --work-root "$test_parent" --report-dir "$report_dir"
```

校验器检查完整性、版本、原生架构，对比正常/失败 Shell 的 stdout/stderr、callback、product 和一次 payload 执行，复用 M1 正常/失败/重试、冻结/新快照回放，以包内 Go 工具验证规范 bundle 和 19 条过程/outbox 事件。报告、命令与新 handoff 位于安装之外；失败时非零退出并说明原因。工作目录唯一且保留，只清理本次创建的目录。

直接运行校验器不隔离主机。源码侧 `make test-package` 还要求 bubblewrap、ldd、系统 `/usr/bin/python3` >=3.12，以及可用的非特权 user/network namespace。它只挂载只读解包目录、新可写数据及选定系统工具/库；原源码、Go 和缓存不可见，PATH 受限且网络无路由。先在不挂载 Python 的环境记录，再以标准库 Python 验证结果；隔离不支持时明确失败，没有主机运行回退。根命令使用临时验收目录，成功后清理，失败时保留现场；直接调用校验器可用 `--report-dir` 保存报告。

试用包也包含公共 M1/Shell/平台/性能工具和站点模板，详见 [HPC 测试](../guides/hpc.md)。隔离回归额外使用 grep/findmnt，属于测试依赖。SLD-26 的完整离线发行、升级/回滚与真实 HPC 资格继续单独验收。

<!-- trial-package:end -->
