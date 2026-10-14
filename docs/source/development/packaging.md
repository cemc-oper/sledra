# 本地试用包打包

在仓库根目录运行 `make package`，使用与开发构建相同的配方，生成 Linux 试用归档、独立 manifest 和校验和，放在 `dist/`。`make test-package` 验证已有产物，不重建缺失或损坏的包。安装及运行步骤见[本地试用包安装](../getting-started/installation.md)与[试用包运行配置](../guides/runtime.md)。

```text
sledra-<software-label>-linux-<arch>/
├── bin/sledra
├── README.md
├── manifest.json
├── SHA256SUMS
└── share/sledra/
    ├── integrations/bash/
    ├── spec/{1.0.0,fixtures/1.0.0}/
    ├── examples/standalone/
    ├── docs/{runtime.md,testing-hpc.md}
    ├── licenses/
    ├── scripts/test_tools.py
    ├── tests/m1/{verify.py,replay.py,results/handoff.json}
    └── test-tools/{verify.py,package_tools.py,bin/}
```

包根 `README.md` 从安装指南的用户部分生成；`share/sledra/docs/runtime.md` 从运行配置页复制，HPC 测试页也从文档树复制。它们只引用包内资源。归档还包含 test session、M1/Shell/性能、原语、双角色与 Slurm 准备工具；依赖和边界见[HPC 验证工具](../guides/hpc.md)。包内所有文件进入 manifest 与 `SHA256SUMS`，归档有独立的 `.tar.gz.sha256`。

安装校验器复用显式预编译二进制运行 M1。只有 `bin/sledra` 是记录入口；`test-tools/bin/` 中的 spec-check/event 是验证辅助，不包含 retention/spool 工具。包不带 Go 源码、缓存、虚拟环境、analysis 或 Python 第三方库。

默认软件标识 `dev`；支持 amd64/arm64，逐个 ELF 核对架构及无 PT_INTERP/DT_NEEDED，另核对实际静态构建结果。manifest 的运行状态先标未验证，验收报告记录 native 执行；交叉构建不算运行资格。

manifest 记录文件大小、权限与 SHA256；`SHA256SUMS` 包含 manifest，独立 `.tar.gz.sha256` 覆盖整个归档。归档的 owner 与时间规范化；输入和缓存来源不变时字节可重复。`SOURCE_DATE_EPOCH` 固定内嵌构建时间。同标识的新内容会替换本地忽略产物，这不是签名发行。

第三方许可文本来自实际链接模块和 Go 工具链；manifest 如实记录项目许可未决。不复制旧 reference 实现。完整发行、升级回滚与真实 HPC 验收属于后续工作。
