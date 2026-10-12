# Sledra 本地试用包

包提供Linux记录器、Bash owner钩子、合成示例与可选验证工具。本地试用不是公开发行，项目许可未决；已有第三方许可位于share/sledra/licenses，manifest记录实际构建、平台及payload。

## 运行合成owner

记录需Bash>=5.2和目标bin，示例另需dirname/cat/sleep；不需Go、Python、缓存、源码或网络。解包后设置绝对安装目录并使用新的可写work目录：

```bash
install_dir=/absolute/path/to/sledra-dev-linux-amd64
work_dir=$(mktemp -d "${TMPDIR:-/tmp}/sledra-trial.XXXXXX")
mkdir "$work_dir/observability"
"$install_dir/bin/sledra" --help
"$install_dir/bin/sledra" --version
STANDALONE_WORK_DIR="$work_dir" SLEDRA_BIN="$install_dir/bin/sledra" \
  SLEDRA_ENABLED=1 "$install_dir/share/sledra/examples/standalone/run.sh"
printf '记录文件： %s\n' "$work_dir"
```

脚本支持任意cwd，按自身位置找库/配置/输入。安装可只读，数据只写显式work；新目录中PAYLOAD_MODE=fail返回7，SLEDRA_ENABLED=0关闭观测但运行相同payload。context/Registry/spool/finish是原始事实，不是分析。

## 验证试用包

校验和识别字节变化，不是签名或发布者证明。包根执行：

```bash
sha256sum -c SHA256SUMS
```

可选完整验证只需Python>=3.12标准库，无pip/uv依赖。设已有可写test_parent和安装外新report_dir：

```bash
python3 "$install_dir/share/sledra/test-tools/verify.py" \
  --work-root "$test_parent" --report-dir "$report_dir"
```

核对manifest、ELF/链接、version、正常/失败Shell基线等价、M1/冻结与新快照回放、canonical bundle与19条过程/outbox事件。失败非零，保留摘要/命令；唯一work子目录保留供检查。直接校验不隔离主机，源码test-package提供OS断网及无源码/Go/缓存证明。

bin/sledra是唯一记录入口，test-tools/bin辅助验证不是生产服务，不含retention cleaner。架构标签仅为格式，实际执行查acceptance，交叉目标须native运行后验收，真实共享/HPC另测。

路径和输出说明见share/sledra/docs/runtime.md。公共M1/Shell/性能/平台工具在share/sledra/tests，共享work/bin/report与站点准备见share/sledra/docs/testing-hpc.md及examples/hpc，本地模拟不代表HPC。

软件dev、契约草案和阶段修订独立，无批准公开发行。联系入口为https://github.com/cemc-oper/sledra/issues。manifest与share/sledra/licenses/THIRD_PARTY.json标明内容/依赖；native amd64已有本地证据，真实HPC、native arm64、完整升级回滚仍待验证。
