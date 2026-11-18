# Si_pbe_cell / 70292806 只读诊断

2026-09-28，经保存的 eshell111-vasp544 连接查询。未修改远程文件、回执、授权或任务，未提交、取消、部署或运行VASP。首轮SSH查询55秒超时，后续带远端命令超时的只读查询成功。

## 实际证据

- sacct：JobID 70292806，JobName va-Si_pbe_cell，用户researcher，分区xahcnormal。
- WorkDir=/work/home/researcher/vasp-agent-remote/jobs/Si_pbe_cell，与目标一致。
- FAILED，ExitCode=1:0，DerivedExitCode=0:0，Reason=None，Comment和SystemComment为空。
- Submit/Eligible/Start/End均为2026-09-28T18:01:31，Elapsed=00:00:00。
- NNodes=1，NCPUS/ReqCPUS=96，NodeList=None assigned，AllocTRES为空。
- ReqTRES=billing=96,cpu=96,mem=342720M,node=1。
- SubmitLine=sbatch --parsable --export=ALL,VASP_AGENT_PHASE=convergence submit.slurm。
- slurm-70292806.log不存在；scontrol已不能找到该历史JobID。
- 远程submissions/convergence.json仅保存status=submission_uncertain、job_id=null。
- remote-job.json：6组×16进程，1节点，96总进程，4小时；release=559b9d19ff375e36。

`sinfo -p xahcnormal -e -o '%P|%c|%m|%l|%D'`返回：

| 每节点CPU | 每节点内存MB | 节点数 |
|---|---|---|
| 32 | 126536 | 3218 |
| 64 | 255551 | 2285 |
| 64 | 257667 | 25 |

分区配置：State=UP，OverSubscribe=NO，DefMemPerCPU=3570；账户在AllowAccounts内。只记录诊断相关字段，不保存其他账户名单。

## 结论与边界

确定存在资源不可满足：1节点96CPU超过所查询分区最大64CPU/节点；默认请求内存96×3570=342720MB，也超过最大257667MB/节点。该冲突高度吻合未分配节点且零时长失败，无证据显示进入VASP。这是查询时的分区容量；历史拒绝的确切文字仍需平台错误详情或管理员日志，sacct未给具体Reason。

两个独立的客户端/worker缺口：

1. submission_preflight只验证输入/环境/审阅与操作者授权上限，没有查询节点容量。资源合法和授权足够不等于调度器能够分配。
2. remote_worker在sbatch前写入uncertain占位，随后sbatch超时、返回非零、stdout解析失败等路径不持久化原始返回码/stdout/stderr。Toolbox.remote_submit捕获所有异常后，发现占位就返回recovered_after_disconnect=true，并丢掉原异常。故这个字段不能证明当时SSH断线；无法事后确定JobID未写回的精确分支。

## 后续建议（本轮未执行）

- 保留历史任务证据，不删除占位直接重交；后续设计显式核对并关联历史JobID的恢复操作。
- 若保留6×16=96进程，2节点时每节点48进程、171360MB默认内存，容量上可由64核节点承载；仍受配额、策略、可用资源及跨节点环境影响，不保证排队/运行成功。
- 若保持1节点，可选择例如4×16=64进程（剩余2个测试后续执行），或6×8=48进程；均需用户重新选择，改变脚本后重新审阅。
- 提交前加入只读容量校验；持久化sbatch失败诊断，准确区分占位存在与已取得JobID，不用通用断线标签覆盖原错误。


## 本地修复完成

新增分区实际 CPU/默认内存容量预检，在调用 sbatch 前拦截单节点96核等不可能布局；新增脱敏提交诊断持久化及只读历史作业查询。记录恢复不再误称 SSH 断线。新增28个离线回归实例，全量478 passed、1 skipped、15 warnings。未修改真实任务、回执或绑定引擎，未部署、未提交。历史失败的精确调度器报错仍缺失，本次修复不能补回当时未保存的输出。
