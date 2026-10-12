# 源码快速开始

在本地使用 Go 记录器和合成 Bash payload。先安装[开发工具链](development.md#工具链)。源码准备需要联网，日常命令离线使用已准备的缓存。

## 准备依赖

从仓库根目录执行：

```bash
make help
make setup
```

setup 同步锁定的 Python 开发/文档工具，下载固定 Go 模块并准备带摘要校验的 actionlint。已安装工具链且可联网时，依赖缓存可以为空。Python 仅用于开发与验证，记录本身只需目标二进制和 Bash。

## 构建与验证

```bash
make build
make check
make docs
make test
make demo
```

二进制位于 `build/bin/`；日志与摘要位于独立的 `test-results/` 子目录。demo 创建隔离的正常和失败/重试 Run，将业务输出及一次 payload 执行与关闭观测的基线对比，再导出、回放新的快照。已有冻结输入保持不变。

## 手动运行 owner 示例

完成 `make build` 后，执行：

```bash
repo_dir=$(pwd -P)
demo_dir=$(mktemp -d "${TMPDIR:-/tmp}/sledra-demo.XXXXXX")
mkdir "$demo_dir/observability"
STANDALONE_WORK_DIR="$demo_dir" SLEDRA_ROOT="$demo_dir/observability" \
  SLEDRA_BIN="$repo_dir/build/bin/sledra" SLEDRA_ENABLED=1 \
  "$repo_dir/examples/standalone/run.sh"
printf '示例文件：%s\n' "$demo_dir"
```

示例按脚本位置查找资源。`product.txt`、`executions.log` 和 `callbacks.log` 保存业务结果及一次调用的证据。`observability/` 下的 `contexts/`、`registry/`、`spool/` 和 `finish/` 保存原始事实，不生成分析结论。每次手动运行使用新目录；直接调用记录器时使用默认内嵌契约和显式绝对输入/运行路径，见[配置](configuration.md)。

单项检查见[本地测试](testing-local.md)，报告与依赖模式见[开发](development.md)。脱离源码试用请按[安装指南](installation.md)执行 `make package` 和 `make test-package`。
