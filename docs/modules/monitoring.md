# 模块接口卡：`vasp_agent.monitoring`

> 职责：持久化 Slurm 作业监控列表与后台只读轮询线程；无模型调用权限、无提交权限。

## 核心文件与边界

| 模块 | 职责 | 关键导出 |
|---|---|---|
| `job_monitor.py` | 监控注册表事务读写、后台动态等待轮询、暂态锁退避重试与状态栏摘要 | `MonitorPersistenceError`, `MonitorLockError`, `FileLock`（re-export）, `JobMonitor`, `interval`, `suggested_interval`, `status_line` |
| `event_outbox.py` | 持久监控事件队列（`.agent/state/monitor-events.jsonl`）、终态/异常/恢复事件幂等追加与 `ack` 游标（零向上导入 `chat/`/`control/`） | `EventOutboxError`, `append_event`, `list_events`, `ack_events` |
| `polling.py` | 将单条监控任务分发到本机 `squeue/sacct` 或远端 `RemoteCompute.call('monitor_status')` | `poll_job` |

## 关键接口与状态契约

- **共享锁原语**：`FileLock` 已抽离至 `vasp_agent.common.file_lock`，`job_monitor.py` 保留子类兼容导出（抛出 `MonitorPersistenceError` / `MonitorLockError`），彻底消除 `common <-> monitoring` 循环依赖。
- **动态等待与容错**：后台线程按 `min(next_due_at) - clock()` 动态休眠（上限 `MAX_IDLE_WAIT = 30s`，新登记/配置通过 `_wake.set()` 立即唤醒）；遇 `MonitorLockError` 按指数退避重试，仅在注册表损坏等持久化错误时停止并上报。
- **核心方法（`JobMonitor`）**：`register()`、`configure()`、`deactivate()`、`remove()`、`start(poller, notify)`、`stop()`、`check_once()`。
