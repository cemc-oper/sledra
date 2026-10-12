# 试用包运行配置

安装与运行数据分开。SLEDRA_ROOT为已有可信真实绝对目录，保存Registry/context/document/finish/spool；默认内嵌1.0.0草案契约。共享/HPC须验证实际卷，本地成功不代表平台资格。

在已知Bash owner中source share/sledra/shell/sledra.sh，设置SLEDRA_ENABLED=1和绝对bin/root/config。钩子不定义trap或执行payload，owner保持控制流，函数返回0；每个shell观测失败最多一条固定诊断，关闭无副作用。

config固定producer_role/name/version、source_id/session_id及可选spool限制。并发producer采用不同session；示例process/stage值是合成输入，不是生产预算。

SPEC_DIR未设/空使用内嵌资源。非空须绝对目录、九份share/sledra/spec/1.0.0 JSON并与安装schema/catalog/capability准确一致，允许格式不同。相对、缺失、损坏/异内容分别为contract_path_invalid、contract_unavailable、contract_incompatible，不静默回退。

bin/sledra --help、registry run-create/attempt-create、context validate、run finish、event的--help发现命令；帮助/version不需环境。version分别输出软件/commit/时间/输入SHA/Go/target/契约，dev标识不是发行。

成功stdout/stderr为空；生命周期或strict-event失败退出1、stderr分类JSON。默认business事件失败返回0、stdout为空、stderr有界诊断dropped:1。不确定注册同token/context路径重查，不重跑业务；无透明exec supervisor、analysis、查询或真实确认。

直接调用显式不可变父context，请求内输入路径归调用方，不同cwd使用绝对路径。父级缺失不搜索或猜替代Run。
