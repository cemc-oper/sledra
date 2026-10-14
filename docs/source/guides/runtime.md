# 试用包运行配置

安装与运行数据分开。`SLEDRA_ROOT` 为已有可信真实绝对目录，保存 Registry、context、document、finish 和 spool；默认使用内嵌 1.0.0 草案契约。共享/HPC 须验证实际卷，本地成功不代表平台资格。

在已知 Bash owner 中 source `share/sledra/integrations/bash/sledra.sh`，设置 `SLEDRA_ENABLED=1` 和绝对 bin/root/config。钩子不定义 trap 或执行 payload，owner 保持控制流，函数返回 0；每个 Shell 观测失败最多一条固定诊断，关闭时无副作用。

配置固定 `producer_role`、`producer_name`、`producer_version`、`source_id`、`session_id` 及可选 spool 限制。并发 producer 采用不同 session；示例 process/stage 值是合成输入，不是生产预算。

`SLEDRA_SPEC_DIR` 未设或为空时使用内嵌资源。非空时须为绝对目录，包含九份与安装 schema、catalog 和 capability 准确一致的 `share/sledra/spec/1.0.0` JSON；允许不同空白和键顺序。相对、缺失、损坏或异内容分别报 `contract_path_invalid`、`contract_unavailable`、`contract_incompatible`，不会静默回退。

运行 `bin/sledra --help`、`bin/sledra registry run-create --help`、`bin/sledra context validate --help`、`bin/sledra run finish --help`、`bin/sledra event --help` 可发现命令；帮助和版本不需环境。`--version` 分别输出软件、提交、构建时间、输入 SHA、Go、目标与契约版本；`dev` 标识不是公开发行。

成功的记录调用不输出 stdout/stderr；生命周期或 strict event 失败退出 1，并在 stderr 输出分类 JSON。默认 business 模式事件失败返回 0、stdout 为空，stderr 有界诊断包含 `dropped: 1`。不确定注册时以相同 token/context 路径重查，不重跑业务；当前没有透明 exec supervisor、analysis、查询或真实采集确认。

直接调用者提供显式不可变父 context；请求内输入路径归调用方管理，不同 cwd 时使用绝对路径。父级缺失时不得搜索或猜测替代 Run。
