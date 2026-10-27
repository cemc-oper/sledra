# 独立 Execution 生命周期（P1-02）

源码入口为 [sledra-execution.sh](sledra-execution.sh)，使用 Bash 4.3 及以上版本和编译后的 `sledra` Go 二进制。只需这两项及已初始化的独立记录根；记录节点不需要 Python、jq、ecFlow 或旧 producer/context 配置。

当前已实现 begin/context/end、CLI init-root/prepare start/context/end/append。业务事件及阶段函数待 P1-03，完整独立示例和故障/信号对照待 P1-04；[P0 接口范例](../../spec/execution-v2/interface-examples/standalone.sh) 仍依赖尚未交付的事件/阶段函数，不能直接当作运行示例。全部签名和错误语义沿用 [2.0.0 冻结接口](../../spec/execution-v2/interfaces.md)。

## 部署和启用

管理侧先用本仓库 recorder 的构建入口生成二进制。选择业务清理目录之外的绝对根，一次性初始化，例如：

```bash
/opt/sledra/bin/sledra execution init-root \
    --root /shared/observability/example \
    --system synthetic --deployment local --partition-timezone=+08:00
```

该命令只建立不可覆盖的 sledra.json；相同设置幂等，不同设置、混合旧布局或非空无标记目录被拒绝。部署路径只是说明，本轮验证均用临时目录。P5 试用归档尚未纳入新版 Bash 入口，使用前须由部署者提供本次构建的二进制和源码库。

业务 owner 的最小调用如下；实际业务仍在原位置运行，结果由 owner 自行保存：

```bash
export SLEDRA_MODE=execution-v2
export SLEDRA_BIN=/opt/sledra/bin/sledra
export SLEDRA_ROOT=/shared/observability/example
source /opt/sledra/share/sledra/integrations/bash/sledra-execution.sh
sledra_execution_begin --task configuration

# 应用在这里解析并使用自己的实际参数。
sledra_execution_context --task configuration --cycle-kind none \
    --string resolution 25km --int members 1 --bool optional false

# 这里演示由 owner 已保存的真实结果，不运行或重试任何业务命令。
business_rc=0
sledra_execution_end --exit-code "$business_rc"
```

有业务时次时用 `--cycle-kind single --cycle TIME`；处理多时次时显式用 `multiple`。`none`/`multiple` 不传 cycle；未记录 context 就是尚未确定。参数支持重复的 --string/--int/--float/--bool/--null，空字符串和 null 不同，数字按契约精确保留。参数键重复或选项非法时拒绝本条；不接受 --run-id、用户指定的 execution_id 或 previous-event。

## owner 与失败处理

source 只加载函数定义，begin 才冻结设置并建立一次 Execution；重复 source/begin 不重置。默认模式关闭时无文件、CLI 调用或诊断；与 `SLEDRA_ENABLED=1` 同时启用则禁用新版并诊断。设置在首次 begin 冻结，修改环境不会重启或转根。

私有 `_SLEDRA_V2_` 变量仅存于当前 BASHPID，均去除 export 属性，兼容调用方 set -a。继承的导出状态会丢弃，包含 PID 不变的 exec bash；正常新 Bash begin 生成新 ID。子 Shell、命令替换及 pipeline 副本不拥有写权，静默 no-op。公共函数返回 0、stdout 空，不安装 trap 或改变 shell options。

context 成功后保存完整事件；相同参数/数值/时间点重传不追加，不同 context 永久禁写。end 成功后关闭，同一结果幂等，不同结果诊断并保留首份。end 不补 context，也不补造缺失阶段。owner 必须先保存真实业务退出码、PIPESTATUS 或信号，再调用 hook；库不执行 callback。

prepare 的单条预写校验失败保留 active。根/工具故障、坏尾、短写、close 或结果不确定、异常退出/回执会保守禁写；后续 hook 不再追加或重建文件。一次 begin 的输入失败同样锁定 disabled。诊断每个 owner 最多一条固定 JSON，不含参数或路径；诊断输出本身仍可能失败。

内部 prepare 回执最多 65792 字节，Shell 用有界 read 和固定 TSV 解析，保留并检查最终 LF，拒绝 NUL/多行/额外字段，不执行返回文本。append 只接收一条 JSON+LF，JSON 本体上限 65536 字节。正常追加检查 close 成功；不重试可能写入的事实，不宣称断电持久性或任意文件系统故障下的硬时延界限。

旧 [sledra.sh](sledra.sh) 及旧命令保留原语义。新入口在构造旧服务前分发；旧命令拒绝带 sledra.json 的根，旧 event 仍保留默认 best-effort/--strict 的返回策略。新路径只写 sledra.json 与 Execution JSONL，无登记、session、metadata、Run 创建或数据库。

## 验证入口

[真实 Bash/Go 测试](../../tests/shell/test_execution.py) 编译临时二进制并在临时根运行，使用 P0 校验器断言实际记录，不依赖 P2 reader。根 make test 已纳入它；单独执行：

```bash
uv run --locked --offline --no-python-downloads python -m unittest discover -s tests/shell -p test_execution.py -v
```

运行测试的开发环境需要 Go、Python 及现有 jsonschema 依赖；这不是记录节点依赖。P1-02 已覆盖 owner 生命周期与跨 CLI 禁写，P1-04 仍需完整示例、业务事件/阶段、信号及故障基线的阶段验收。
