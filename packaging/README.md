# 本地试用包

make package用共享构建配方生成Linux试用tar.gz、独立manifest和摘要，位于dist/。test-package验证已有产物，不重建缺失/损坏包。使用方法见[安装](../docs/source/installation.md)。

```text
sledra-<software-label>-linux-<arch>/
├── bin/sledra
├── README.md
├── manifest.json
├── SHA256SUMS
└── share/sledra/
    ├── shell/
    ├── spec/{1.0.0,fixtures/1.0.0}/
    ├── examples/standalone/
    ├── docs/runtime.md
    ├── licenses/
    ├── scripts/test_tools.py
    ├── tests/m1/{verify.py,replay.py,results/handoff.json}
    └── test-tools/{verify.py,package_tools.py,bin/}
```

安装校验器以显式预编译bin复用M1。仅bin/sledra是记录入口，测试helper为spec-check/event，省略retention/spool；不带Go源码、缓存、虚拟环境、analysis或Python第三方库。

默认dev，支持amd64/arm64构建，逐ELF核对架构及无PT_INTERP/DT_NEEDED，独立验证CGO_ENABLED=0实际结果。manifest运行状态先标未验证，acceptance记录实际native执行，交叉构建不算资格。

manifest记录文件size/mode/SHA；SHA256SUMS包含manifest，独立.tar.gz.sha256覆盖archive。archive owner/时间规范化，输入/缓存来源不变时字节可重复；SOURCE_DATE_EPOCH固定内嵌时间。同标识新内容会替换本地忽略产物，不是签名发行。

第三方现有许可文本来自实际链接模块和Go工具链，manifest如实记录项目许可未决；不复制旧reference实现。完整发行/升级回滚/HPC属后续工作。包还包含test session、M1/Shell/性能、原语、双角色、Slurm准备工具，依赖及边界见[HPC指南](../docs/source/testing-hpc.md)，全部纳入完整性校验。
