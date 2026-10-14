# CI 与本地复现

GitHub Actions 在 main push、pull request 和手动 dispatch 时运行。配置使用 Ubuntu 24.04 amd64、Python 3.12.3、Go 1.27.0、uv 0.11.2，第三方 Actions 固定完整提交。配置与本地等价命令已验证，真实远端 Actions 仍未执行；平台区别见[支持矩阵](../../reference/compatibility/support.md)。

## 共享入口

准备工具链和 namespace 依赖后，从源码根目录执行与 CI 相同的命令：

```bash
make setup
make check
make docs
make test
make test-platform
make demo
make package
make test-package
```

缓存命中仍执行 setup。手动 `cold_cache=true` 跳过 Go/uv 恢复，从新的 runner 缓存路径准备依赖。setup 可以联网；后续项目命令禁止依赖查询，工作流校验不下载工具。OS 断网由 test-package 单独证明，不能从离线依赖参数推断。

未来一次性的 hosted runner 会安装 bubblewrap/util-linux，在 AppArmor sysctl 存在时启用非特权 namespace；这不修改本地或 HPC 策略。隔离不支持时失败，不回退主机运行。包验收还要求系统 `/usr/bin/python3` >=3.12。

check 调用 `scripts/check_ci.py` 和固定的 actionlint 1.7.11，配方为 `.github/ci-tools.json`。缺失或损坏缓存提示 setup；可显式使用 `python3 scripts/check_ci.py --binary /absolute/path/to/actionlint`，报告保留覆盖来源和摘要。关闭可选 shellcheck/pyflakes 集成，避免工具是否安装改变门槛；Bash 语法另行检查。docs 调用[严格中文文档构建](../documentation.md)。

## 权限与证据

workflow 仅 contents:read，关闭持久化 checkout 凭据，不使用仓库 secret，不发布发行版或执行 HPC 作业。阶段失败停止后续检查；always 上传步骤保留 14 天的构建与包元数据，不上传普通日志、自动报告、压缩包或二进制。显式保存的性能或平台资格数据须在对应作业中指定输出并配置上传。远端缓存、上传、namespace bootstrap 和 Actions 编排仍未验证；取消或 bootstrap 失败可能使证据不完整。

实际远端验证需要运行链接和环境记录。静态检查、本地命令或 Ubuntu runner 均不代表真实 HPC 资格。本次没有 dispatch、push 或修改 branch protection。

## 更新输入

更新固定提交前，审查上游 [checkout](https://github.com/actions/checkout)、[setup-python](https://github.com/actions/setup-python)、[setup-go](https://github.com/actions/setup-go)、[setup-uv](https://github.com/astral-sh/setup-uv)、[upload-artifact](https://github.com/actions/upload-artifact)和 [actionlint](https://github.com/rhysd/actionlint) 的发布变更。保留 tag 到完整提交及 archive 摘要的来源，验证实际 workflow。更新校验器时同时修改版本和两个 native archive 摘要，重新 setup 并执行负例；更换工具链须补支持矩阵的实际证据。
