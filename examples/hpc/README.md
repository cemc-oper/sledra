# 站点测试模板

site.example.json只是输入模板，复制到新的用户文件后补scheduler、partition、account、Python、安装/test-home、共享work父根和report父根。当前仅支持Slurm，其他scheduler需适配。路径必须已存在且两计算节点同位置可见。测试需Python>=3.12、Bash>=5.2及示例工具；显式bin不需Go。

```bash
python3 examples/hpc/site.py --config /absolute/path/to/site.json
```

准备生成唯一report、共享测试子目录和job.sh，打印提交/收集命令，不自动提交。站点窗口内审查job后再执行打印的sbatch。每节点一个task运行双角色，有界barrier及timeout；完成后即使失败也收集，失联非零退出并保留树/已有结果。报告旁保存job ID、allocation/node、日志和archive摘要。

单节点可用probe、M1、Shell及可选benchmark，加--mode hpc --bin/--work-root/--report-dir/--keep-work，每个report须新建；按站点策略在计算节点执行。手动双角色流程见源码docs/source/guides/hpc.md，安装版位于share/sledra/docs/testing-hpc.md。

模板、登录节点运行或本地模拟不证明共享存储、调度、MPI、signal、断电持久性或A6预算达标。
