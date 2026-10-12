# 平台探针与节点工具

[公共参数与HPC流程](../../docs/source/testing-hpc.md)说明源码/安装调用、依赖及报告边界。test-platform以共享开发bin运行单节点原语和本地双角色；test覆盖并发隔离、owner清理、失联时限、错误架构与缺依赖报告。

probe仍将--parent作为work-root别名。使用新的report-dir，不覆盖[历史WSL2结果](results/wsl2-ext4-2026-10-11.json)。v2报告含文件系统原始延迟、实际ELF/version启动和真实mount。测试树除外部bin外限制200文件/10MiB，Go缓存不在树中。

cross_node提供prepare/worker/collect/本地simulate。Registry/context/spool共用新拥有的测试树，两角色校验相同绝对context，有界原子barrier。结果保存命令、节点/mount、登记时间、flock排斥/释放与spool可见性。角色只领取一次，准备/失败树保留；模拟不是真实跨节点。

合成通过只验证指定mount/节点的操作，不证明共享HPC、断电、scheduler信号、MPI或A6预算。
