# 名称与路径迁移

SLD-28 于 2026-10-12 迁移外层开发目录与内部 Git 仓库。产品、仓库和 CLI 统一为 sledra，环境变量保留 `SLEDRA_*`。

| 历史位置或身份 | 当前位置或身份 |
|---|---|
| `observability/sladra-devel/` | `observability/sledra-devel/` |
| `observability/sladra-devel/repo/sladra/` | `observability/sledra-devel/repo/sledra/` |
| `sladra-devel/docs/observability-plan/` | `sledra-devel/docs/observability-plan/` |
| `sledra.local/recorder` | `github.com/cemc-oper/sledra/recorder` |
| 源码 `shell/sledra.sh` | `integrations/bash/sledra.sh` |
| 试用包 `share/sledra/shell/sledra.sh` | `share/sledra/integrations/bash/sledra.sh` |

工作区前缀为 `/home/wangdp/project/mcv/mcv-workflow-project/mcv-workflow-devel/`。Windows 用户在相应 WSL 路径中将 sladra-devel 与 repo\sladra 改为 sledra-devel 与 repo\sledra，并更新编辑器根目录和书签；未安装兼容软链接。

## 原迁移记录

迁移前 main 的干净提交为 `f6d527c681b8367123a1dd3e608c4ef62ee032ac`，只有普通 .git、一个 worktree，无 submodule 或相关软链接。外层不是 Git 仓库。其他项目的活动文件未引用旧名称/路径；origin 已为 `git@github.com:cemc-oper/sledra.git`，迁移未更改或访问远端。

两层目录在同一文件系统改名。编辑前 152 份既有工作区文件的 SHA256、HEAD 与工作树状态均匹配；Git 历史和既有本地文件保留。Go module/import 改为 origin 对应路径；源码说明可独立阅读，外部计划导航指向新位置。

历史证据中的旧命令、路径、module、二进制摘要保留，不改写成新执行结果。原始契约、夹具、身份、事件和冻结交接保持字节不变；前序报告的日期、工具链与平台范围仍按当时记录解释。

SLD-37 后源码和新试用包只提供右列 Bash 库路径；旧安装路径不提供别名。已有 owner 的 `source` 改用对应安装根目录下的新绝对路径。`tests/shell/` 仍按测试领域命名，不是安装库路径；旧任务证据中的 `shell/` 是当时的历史位置。

## M1.2 工程入口

根公共目标继续保留，调用方式见[开发流程](../../development/workflow.md)。调整已有自动化时核对以下映射：

| M1.1 工程约定 | M1.2 当前约定 |
|---|---|
| 根 Makefile 统一转给 `scripts/dev.py` | 根协调，recorder/docs 组件入口和专用脚本；旧分发器已移除 |
| 文档输出 `build/docs/` | 严格 HTML 输出 `build/docs/html/` |
| 自动 `test-results/` 阶段日志及通用 summary | 控制台直接输出；失败非零退出，临时现场按工具清理规则处理 |
| 依赖默认报告目录获取性能/平台数据 | 显式 `SLEDRA_BENCHMARK_OUT` 或验证工具 `--report-dir`，CI 按用途上传 |
| 顶层 `packaging/` 的三份说明 | `docs/source/development/packaging.md`、安装指南和运行指南；包内文档从这些来源导出 |

构建产物共用根 `build/`，`OUTPUT_DIR` 可覆盖；最终归档保留在 `dist/`，`DIST_DIR` 可覆盖。更新 CI 时使用相同根入口，撤销对默认测试日志或 summary 的消费。历史验证文件中的旧路径仍指当时的产物，不移动或重写历史证据。

## 中文文档布局

中文文档使用 Sphinx、PyData Sphinx Theme 和 MyST-NB。原 `docs/*.md` 指南迁入 `docs/source/`，现按 `getting-started/`、`guides/`、`development/`、`reference/` 分层组织；测试与验证、兼容性与迁移各有子目录。各章节 `index.md` 组织子页面，`docs/README.md` 保留仓库入口。

规范来源 manifest 保留在 `docs/contracts-sources.json`。文档构建与试用包来源路径随目录调整同步；包内 HPC 说明仍位于 `share/sledra/docs/testing-hpc.md`。规范权威来源和冻结测试数据保持原位置及字节。
