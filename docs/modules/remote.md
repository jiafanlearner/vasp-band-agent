# 模块接口卡：`vasp_agent.remote`

> 职责：SSH 连接管理与复用、stdin 自包含远端 RPC worker（10 个标准库单元 + 瘦身入口）、引擎白名单打包、集群探测与环境配置。

## 核心文件与边界（全部 <= 490 行）

| 模块 | 职责 | 关键导出 |
|---|---|---|
| `remote_worker.py` | 远端 RPC 分发、`WORKER_UNITS`/`SOURCE_FILES`、提交预检、下载与 package-only 兼容门面 | `WORKER_UNITS`, `SOURCE_FILES`, `dispatch`, `ROOT_ACTIONS`, `NEW_TASK_ACTIONS`, `TASK_ACTIONS`, `MEMO_ACTIONS` |
| `worker_common.py` | 错误类型、脱敏、唯一 `.tmp` 原子写、哈希缓存与阶段顺序（仅标准库） | `SubmissionError`, `diagnostic_text`, `write_json`, `sha`, `digest_memo`, `task_phases` |
| `worker_slurm.py` | Slurm 容量预检、记账、数组状态与只读监控查询（仅标准库） | `capacity_preflight`, `submission_accounting`, `array_scheduler_status`, `monitor_status` |
| `worker_receipts.py` | 占位提交回执、锁与提交恢复日志（仅标准库） | `submit_with_receipt`, `recover_submission`, `recovery_barrier` |
| `worker_inventory.py` | 集群 VASP 与工具只读探测（仅标准库，不递归 `find`） | `software_inventory`, `Probe` |
| `worker_env.py` | `ENGINE_MODULES`、release 绑定、哈希缓存共享、环境与配置指纹（仅标准库） | `ENGINE_MODULES`, `RESULT_FILES`, `engine_module`, `task_release`, `inspect_environment`, `task_environment`, `release_version` |
| `worker_tasks.py` | 任务列表、归档/恢复、删除预检与前置作业检查（仅标准库） | `list_workflow_jobs`, `task_target`, `deletion_preview`, `manage_task`, `check_previous_job` |
| `worker_lifecycle.py` | 取消作业、释放回执、中断阶段续算与复用校验（仅标准库） | `cancel_job`, `release_cancelled_submission`, `check_resume`, `prepare_phase_resume` |
| `worker_resources.py` | 调度参数校验、资源上下文、并发布局规划、脚本改写与数组校验（仅标准库） | `validate_scheduler`, `resource_context`, `plan_fast_resources`, `configure_resources`, `check_resources` |
| `worker_diagnostics.py` | 受限诊断文件读取、提交诊断、任务状态汇总与 SCF 占据审计（仅标准库） | `submission_diagnostic`, `scf_occupation_audit`, `_status` |
| `worker_prepare.py` | 远端任务准备/预览、收敛重试与能带路径派生（仅标准库） | `_prepare_task`, `_prepare_from_convergence`, `_prepare_bands_path` |
| `bundle.py` | 引擎白名单打包与 `worker_program()` 进程内内存缓存 | `engine_sources`, `local_version`, `worker_program`, `clear_worker_cache`, `stdin_program` |
| `ssh_pool.py` / `remote_compute.py` / `remote_servers.py` / `server_setup.py` / `environment_profiles.py` / `ssh_bench.py` | Paramiko 连接池（并发 stdin 写入防窗口死锁）、RPC 客户端、SSH 凭据管理与集群初始化 | `Pool`, `RemoteCompute`, `Servers`, `delivery_summary`, `probe` |

## 关键契约

- **stdin 纯文本拼接**：`bundle.worker_program()` 按 `(*WORKER_UNITS, 'remote_worker')` 顺序剔除 `# <bundle:package-only>` 后拼接并缓存；保持纯文本传输，不做 `zlib` 压缩；所有单元仅依赖标准库与前置单元。
- **不自动重发**：`ssh_pool._exchange()` 发出 `exec_command` 后断连返回 `255` 交由只读恢复核对。
