# 文档导航

所有命令示例默认在项目根目录运行，而不是在 `docs/` 中运行。

新访客请先阅读[项目首页](../README.md)或 [English overview](../README.en.md)，查看[真实 Si 能带示例](../examples/results/README.md)。历史排障和交接文档中的个人路径、账户、主机地址已替换为示例值；这些连接示例不能直接用于其他集群，旧任务状态和命令以记录时点为准。

## 日常使用

- [AI 使用说明](guides/AI_USAGE.md)：启动、会话、自然语言工具和计算流程。
- [提交后的定时检查](guides/JOB_MONITORING.md)：默认15分钟、自动建议间隔、`/watch` 控制、重启恢复及阶段完成提示。
- [远程 Slurm 数组收敛提交](guides/SLURM_ARRAY_SUBMISSION.md)：仅 convergence 阶段的数组模式、每项资源与授权峰值、菜单确认和真实输出验收。
- [本地编辑与密钥维护](guides/WORKSPACE_EDITING.md)：草稿编辑、恢复和无需模型的维护命令。
- [集群说明](guides/CLUSTER.md)：历史环境、部署方式及版本边界。
- [服务器配置](../servers/README.md) 与 [赝势配置](../potentials/PBE/README.md)。
- [原生压缩分档测试](guides/COMPACTION_PROBE.md)：用合成上下文比较容量、限流和网关故障。
- [结构化摘要与测试](guides/CONTEXT_SUMMARY.md)：默认压缩策略、配置、完整历史保护和合成 Si 对话实测。
- [提示词缓存与诊断](guides/PROMPT_CACHE.md)：稳定请求前缀、缓存用量、合成连续请求测试与实测限制。
- [DeepSeek 配置与缓存测试](guides/DEEPSEEK_SETUP_AND_CACHE_20261010.md)：官方接口、独立 profile、模型列表及合成缓存实测。
- [La₂CuO₄ / Cu_pv 实际工具缓存测试](guides/LA2CUO4_DEEPSEEK_TOOL_CACHE_20261010.md)：完整工具目录的缓存结果、SSH 权限修复及待完成的赝势核验。

## 开发维护

- [目录结构与安装](development/PROJECT_LAYOUT.md)：功能划分、导入方式及兼容边界。
- [工作流模块设计](design/WORKFLOW_MODULES.md)：版本化阶段定义、参数规范、指南加载、提示预算，以及 DOS / Wannier / 声子的接入方式和旧任务兼容。
- [可靠性改进](development/RUN_RELIABILITY_UPDATE.md)。
- [工具恢复实现](development/TOOL_RECOVERY_IMPLEMENTATION.md)。
- [终端界面改进](development/TERMINAL_UI_UPDATE.md) 和 [HTML 预览](previews/terminal-preview.html)。

## 接手工作与历史证据

- [新增功能交接](handoffs/HANDOFF_NEW_FEATURES.md)。
- [SSH 排障交接，2026-09-28](handoffs/HANDOFF_SSH_AND_NEXT_STEPS_20260928.md)。
- [最初工具恢复需求](handoffs/HANDOFF_AGENT_TOOL_RECOVERY.md)：历史文档，不是当前待办清单。
- [Si 实跑记录](../reports/runs/RUN_SI_20260925.md)。
- [报告目录](../reports/README.md)：回归证据与文件移动清单。
- [缓存验证，2026-10-10](guides/CACHE_VALIDATION_20261010.md)：连接复查、缓存键输入计量差异与停止记录。
- [精确重放计量验证，2026-10-10](guides/CACHE_ACCOUNTING_REPLAY_20261010.md)：无 key、相同正文的输入变化与缓存高命中后归零。
- [固定 key 重放准备，2026-10-10](guides/CACHE_FIXED_KEY_REPLAY_20261010.md)：模拟验收通过，TLS 预检失败，真实模型未开始。
- [固定 key 真实重放，2026-10-10](guides/CACHE_FIXED_KEY_REPLAY_RESULT_20261010.md)：新预检通过，首条模型请求返回网关并发限制，未获得缓存测量。

历史文档中的源码文件名可通过目录说明或移动清单定位；其中的任务状态和旧测试数量属于当时记录，不自动成为当前事实。
