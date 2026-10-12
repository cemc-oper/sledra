# 开发流程

在仓库根目录运行公共 Make 目标。根 Python 项目仅管理开发、文档和验证工具：`package = false` 不安装 Sledra 库，元数据 `0.0.0` 不是记录器发行版。Go/Bash 记录不依赖 Python；分析包留待 M2。

## 工具链

最低版本是开发门槛，实测版本不代表所有允许组合都已验证。

| 工具 | 最低版本 | 本地实测 |
|---|---|---|
| Python | 3.12 | 3.12.3 |
| uv | 0.11.2 | 0.11.2 |
| Go | go.mod 声明的 1.25.0 | 1.27.0 linux/amd64 |
| Bash | 5.2 | 5.2.21 |
| GNU Make | 4.0 | 4.3 |
| 平台 | 具备所需文件系统原语的 Linux | WSL2 x86_64、ext4 |

平台探针还使用 util-linux 的 findmnt，Shell 回归使用 grep。通过 `make PYTHON=/path/to/python3 ...` 选择启动解释器，`UV=/path/to/uv` 选择 uv，`UV_PYTHON=/path/to/python3` 选择项目解释器。

## 联网准备

```bash
make help
make setup
```

setup 依据 `pyproject.toml` 同步提交的 `uv.lock`，准备 dev/docs 依赖组，下载 go.mod/go.sum 固定的 Go 模块，以及摘要固定的 actionlint 1.7.11。校验器位于 `build/ci-tools/`，配方为 `.github/ci-tools.json`。使用已安装 Python/Go，不自动下载工具链；空缓存与站点代理均支持。

开发组继续固定 jsonschema 4.10.3、Ruff 0.16.9，文档组使用 Sphinx、PyData Sphinx Theme、MyST-NB，全部传递依赖由 uv.lock 锁定。`spec/requirements-test.txt` 保留直接契约脚本的窄依赖输入。更新依赖时显式修改元数据与锁文件；日常命令不改锁文件。

## 离线公共命令

```bash
make build
make check
make docs
make test
make demo
make benchmark
```

命令禁止 uv 联网、Go proxy/sum database 查询、私有模块代理绕过及工具链下载。源码离线构建需要 setup 已准备的缓存，不支持空缓存离线初始化。项目环境不存在时，uv 可从已有缓存同步。

build 在 `build/bin/` 生成 sledra、sledra-spec-check、sledra-event、sledra-spool、sledra-retention。`build/manifest.json` 记录输入、工具链、目标、参数和二进制 SHA256。采用 CGO_ENABLED=0、-trimpath、-buildvcs=false，支持无 Git 元数据的源码；输入或二进制字节变化时重建，其余情况复用。版本来源见 [CLI](cli.md#帮助与版本)。

check 检查锁文件、Python 格式/静态规则、Go 格式/vet、Bash 语法、canonical/fixture/embed 生成字节、文档链接及真实 actionlint。生成在私有副本中完成，不覆写源树。docs 执行中文文档严格构建，见[文档维护](documentation.md)。

test 先复用共享构建，再执行不使用测试缓存的 Go 回归、工程/打包/平台测试、CLI、契约/事件/context/Shell、M1 和冻结回放。辅助脚本通过 `SLEDRA_TEST_BIN_DIR` 复用工具；显式缺失二进制时报错，不偷偷重建。单独调用脚本仍支持私有目录离线构建。

demo 创建正常/失败/重试场景，在新的报告目录导出 context、Registry、finish 与 JSONL，再回放新快照。benchmark 单独保存原始纳秒样本和分位值，默认 20 对；`SLEDRA_BENCHMARK_SAMPLES=10` 可缩短测试（允许 10～100），不等于性能资格通过。

## 产物与失败报告

`build/`、`dist/`、`test-results/` 不纳入 Git。每次检查、测试、文档构建、演示、性能、打包及包验收创建唯一 `test-results/<action>-<timestamp>-<id>/`，保存阶段日志和 `summary.json`。失败停止执行、非零退出、打印阶段与日志路径并保留摘要；报告对比保护文件清单与字节。

日常运行不覆盖冻结 M1 或历史平台/性能结果，显式刷新见[工程交接](engineering-handoff.md)。规范来源核对是可选作者工作区检查，独立源码无需相邻规划目录。

源码模式依赖开发工具和缓存；[试用包](installation.md)记录仅依赖目标二进制、Bash 及示例使用的 dirname/cat/sleep，标准库 Python 结果验证独立提供。test-platform 运行单节点原语和本地双角色模拟；源码/安装工具共用[测试参数](testing-hpc.md)，HPC 模式必须显式指定共享工作父目录。CI 使用相同入口，见 [CI](ci.md)和[支持矩阵](support.md)。
