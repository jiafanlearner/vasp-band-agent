# 模块接口卡：`vasp_agent.chat`

> 职责：自然语言对话主循环、模型工具目录与执行、斜杠命令、会话/设置/软件环境/取消/物理确认菜单编排。

## 核心文件与边界（全部 <= 400 行，符合 <= 500 行软守门）

| 模块 | 职责 | 关键导出 |
|---|---|---|
| `ai_agent.py` | 薄兼容门面：组合 `Toolbox`/`Agent` 并完整 re-export | `Toolbox`, `Agent`, `INSTRUCTIONS`, `TOOLS`, `MODEL_TOOLS`, `scrub`, `main` |
| `prompts.py` | 固定系统提示词与预算常量 | `INSTRUCTIONS`, `PROMPT_TOKEN_BUDGET` |
| `tool_schemas.py` | 完整工具 schema、精简模型视图与权限门控集合 | `TOOLS`, `MODEL_TOOLS`, `TOOL_GROUPS`, `advertised`, `null_resource_mode`, `SUBMIT_TOOLS` |
| `tool_groups.py` | 常驻核心工具与按需懒加载工具组注册、历史激活派生与压缩引用 | `CORE_TOOLS`, `GROUPS`, `TOOL_TO_GROUP`, `ids`, `group_of`, `active`, `enable`, `references` |
| `toolbox_core.py` | `Toolbox` 初始化（含 `unified_plan_confirm`/`pending_plan_preauth`）、权限登记与 `revoke_submission` 撤销清理、监控/审计/证据库与脱敏 | `ToolboxCoreMixin`, `scrub`, `scrub_payload`, `read_tail`, `item_field`, `output_items` |
| `toolbox_workspace.py` | 服务器管理、`workbench/` 草稿编辑、沙箱/本机分析、能带读取与阶段指南 | `ToolboxWorkspaceMixin` |
| `toolbox_workflow.py` | 扫描方案、阶段/输入审阅、结构/赝势检查、远程准备/状态/下载与选择性重扫统一核对 | `ToolboxWorkflowMixin` |
| `toolbox_submission.py` | 资源规划/应用、统一提交菜单、取消/恢复/诊断与本地/远程提交 | `ToolboxSubmissionMixin`, `_SubmissionSettled` |
| `agent_loop.py` | `Agent` 初始化、对话与工具调用循环、截断扩容重试 | `AgentLoopMixin` |
| `agent_compaction.py` | 上下文预算、摘要/原生压缩、端点能力缓存与缓存指标记录 | `AgentCompactionMixin` |
| `cli.py` | 交互式 CLI 入口（启用 `unified_plan_confirm=True`）、监控终态路由与会话锁 | `main`, `followup_message`, `route_followups`, `answer`, `session_lock` |
| `fork_naming.py` | 派生目录名冲突检测与安全递增（`<base>_2` / `<base>_3`，`<= 64` 字符，绝不覆盖旧目录） | `is_dir_exists_error`, `with_suffix`, `existing_jobs`, `resolve_name`, `conflict_notice`, `confirm_reactive_increment` |
| `dos_fork_menu.py` / `band_path_menu.py` | 三维 DOS 与换能带路径派生统一核对菜单（复用 `permissions.unified_confirm` 与 `fork_naming` 实现参数+核数+单阶段预授权及冲突递增） | `job_name`, `menu_text`, `request` 等 |
| `chat_commands.py` / `extra_commands.py` / `setup_commands.py` / `software_commands.py` / `cancel_commands.py` / `session_commands.py` / `stage_commands.py` / `monitor_commands.py` / `task_commands.py` / `agent_settings.py` / `model_views.py` / `release_check.py` | 各类斜杠命令、菜单、偏好设置与工具输出紧凑视图 | `Commands`, `model_output`, `ensure_current` 等 |

## 关键契约

- **提示词预算**：`PROMPT_TOKEN_BUDGET = 14800`（`tests/test_prompt_budget.py` 强制守门）。
- **门面兼容**：外部调用与测试 `monkeypatch` 继续通过 `vasp_agent.chat.ai_agent` 入口生效。
- **统一核对与跨层边界**：交互会话由 `cli.py` 开启 `unified_plan_confirm=True`；新任务 `/plan` 三合一启动核对由 Layer 3 `planning/plan_startup.py` 编排，与派生/重扫入口共用 Layer 3 `permissions/unified_confirm.py` 及 `ui/menu_text.py`；`revoke_submission()` 同步清空 `pending_plan_preauth` 与已确认资源缓存；上述三个 Layer 3 模块当前均零向上导入 `vasp_agent.chat`，其中 `planning/plan_startup.py` 已由 `tests/test_unified_confirm_stage3.py` AST 守门。
