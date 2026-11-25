# 分阶段改进计划：大响应下载修复、参数与资源统一核对窗口及沙箱增强

> **文档状态**：执行中（基线 commit `8502e63`，远端引擎基线 `release=c9c11663accd1fce`）  
> **创建日期**：2026-11-25  
> **归档约定**：全部 5 个阶段验收并提交后，将本文档安全归档至温/冷归档区（`docs/archive/` 受 `.ignore` 覆盖，保留 Git 历史且不污染日常检索）：
> ```powershell
> git mv docs/development/UNIFIED_CONFIRMATION_AND_STABILITY_PLAN.md docs/archive/2026-11/UNIFIED_CONFIRMATION_AND_STABILITY_PLAN.md
> ```

---

## 1. 背景与前置事实

在 `eshell111-vasp544`（VASP 5.4.4）上完成 3D DOS 端到端真机实测（任务 `si-scan-01_dosf77326`）后，前置阻塞项已全部修复并合入 `main`：

- `9b25d8c`：解除 `record_stage_review(phase="dos")` 5 项检查 `maxItems` 死锁、放开 `STAGE_PARAMETERS.value` 可空类型、补齐本机 DOS 状态与日志白名单；
- `d5535df`：完善冷区 `.ignore` / Hook 守卫；
- `98c37e0`：审阅放行后直弹提交菜单、拆分菜单等待耗时、收口 Paramiko 线程刷屏、精简 `list_scan_plans` 上下文体积；
- `8502e63`：修复 VASP 5.4.4 `vasprun.xml` 不回显 `KPAR`/`NCORE` 导致的 `read_result()` 误报，并部署至集群 `release=c9c11663accd1fce`。

**真机验收现状**：`si-scan-01_dosf77326` 已在集群上完整运行并通过全部程序验收（`workflow-state=complete`、`status=passed`、积分电子数 `8.0 / 8.0` 差值 `0.0`、`fermi_shift=-0.309 eV` 已记录）。本计划阶段 1 修完下载截断后仅需直接拉取已有结果验收，**无需再次重跑集群 VASP 计算**。

---

## 2. 全局工程红线（阶段 1–5 全程强制生效）

1. **科学引擎零改动**：阶段 1–5 **严禁修改 `ENGINE_MODULES` 内任一文件**，全程保持 `bundle.local_version() == 'c9c11663accd1fce'`，无需再次执行 `remote_deploy`。
2. **安全与授权底线不松动**：
   - 物理目标、计算参数、提交核数/资源、提交授权与本机脚本批准 **100% 来自操作者本地交互菜单**，模型不得代选或绕过；
   - **严格单阶段隔离**：统一核对窗口的“审阅通过后直接提交”选项**仅授权当前这 1 个待跑阶段**（新任务仅 `convergence`，派生任务仅 `dos`/`bands`/`convergence`），绝不跨阶段连授；
   - **审阅与远端预检零跳过**：即使操作者在统一核对窗口勾选了预授权提交，落盘后仍必须执行 `get_stage_review` → `record_stage_review(allowed=true)` 及远端 `submit_checked` 完整防篡改与容量预检；任一审阅项为 `blocked` 或预检未过时硬阻断、绝不 `sbatch`。
3. **提示词预算与行数守门**：
   - `PROMPT_TOKEN_BUDGET = 14800`（`tests/test_prompt_budget.py` 全绿），新功能参数优先落在既有懒加载组，不膨胀常驻系统提示；
   - 新增或重构后的 Python 源码文件保持 **`<= 500` 行**（`tools/check.py` 零警告），拆分既有模块必须保留原入口 re-export 门面。
4. **受保护目录与分支**：
   - 严禁改动 `.agent/` 运行时目录与 `runs/`、`downloads/`、`reports/`、`workbench/`、`servers/`、`potentials/` 等操作者数据目录；
   - 保持 `prompts/session_handoff_prompts.md` 本地未跟踪；不删除未合并历史特性分支（如 `claude/dos-step-c`）。
5. **阶段化验收**：每完成一个阶段，独立运行 `python tools/check.py` 与 `bundle.local_version()` 核对全绿后单独提交一个 commit，并勾选本文第 3 节进度表，再推进下一阶段。

---

## 3. 总览与进度追踪

| 阶段 | 优先级 | 主题 | 主要改动范围 | `ENGINE_MODULES` | 进度 |
|---|---|---|---|---|---|
| 阶段 1 | P0 | 修复 `ssh_pool` 大响应截断与下载诊断 | `remote/ssh_pool.py`、`remote/remote_compute.py`、`tests/test_ssh_pool.py` | 零改动 | `[x]` 已完成（`11e867a` / `e606211`） |
| 阶段 2 | P1-a | 统一核对底座 + 派生/重扫一次确认 + DOS/Band 目录冲突递增 | `permissions/`、`chat/{dos_fork_menu,band_path_menu,toolbox_workflow,toolbox_submission}.py`、`ui/`、可选 `remote/worker_prepare.py` | 零改动 | `[x]` 已完成（`75c8796` / `b18a6f9`） |
| 阶段 3 | P1-b | 新任务 `/plan` 三合一启动核对窗口 | `planning/{parameter_plans,plan_menu,plan_startup}.py`、`chat/{cli,toolbox_core,prompts,model_views}.py`、`tests/` | 零改动 | `[x]` 已完成（`ee04539`） |
| 阶段 4 | P2 | 文档与接口卡同步 | `docs/design/WORKFLOW_MODULES.md`、`docs/modules/{chat,remote}.md`、`docs/guides/{DOS,BAND_PATHS}.md` | 零改动 | `[ ]` 待开始 |
| 阶段 5 | P3 | Step 3：Docker 沙箱轻量 CodeAct 增强 | `editing/analysis_runner.py`、`chat/{toolbox_workspace,tool_schemas}.py`、`tests/` | 零改动 | `[ ]` 待开始 |

---

## 4. 各阶段详细设计与验收标准

### 阶段 1（P0 · 止血闭环）：修复 `ssh_pool.py` 大响应截断与下载诊断

#### 1.1 问题现象与实测根因
- 在 `eshell111-vasp544` 上对已完成的 `si-scan-01_dosf77326` 调用 `remote_fetch`，连续失败并报 `REMOTE_RESPONSE_INVALID: 远程响应无效：`（`stderr` 为空）。
- 实测抓包证据：`via: pooled, rc: 0, stdout_len: 360448 (11 × 32768), stderr_len: 0`，JSON 在第 `294718` 字符处被截断。
- 精确根因：[ssh_pool.py](../../src/vasp_agent/remote/ssh_pool.py) 的 `_exchange()` 仅检查 `if channel.exit_status_ready():` 就排空当前 `recv_ready()` 并立即 `break` 关通道。真实 OpenSSH `sshd` 在子进程退出后会**先发送 `exit-status`**，同时继续把管道中剩余的 `SSH_MSG_CHANNEL_DATA` 分包发完，**最后才发送 `SSH_MSG_CHANNEL_EOF`**，因此几百 KB 以上的 `fetch` base64 响应会被提前截断。

#### 1.2 改动方案
1. **`src/vasp_agent/remote/ssh_pool.py`**：
   - 将 `_exchange()` 中的退出条件修正为必须同时满足 **`channel.exit_status_ready() and (channel.eof_received or channel.closed)`**；
   - 满足退出条件后再循环排空所有剩余 `recv_ready()` / `recv_stderr_ready()` 字节并读取 `channel.recv_exit_status()`；
   - 保持现有 `deadline` / `remaining()` 超时机制，防止异常远端进程持有句柄不关导致挂死；
   - 更新误导性注释，准确说明 OpenSSH `exit-status` 可能早于 `CHANNEL_EOF` 到达。
2. **`src/vasp_agent/remote/remote_compute.py`**：
   - 解析远端 JSON 失败抛出 `REMOTE_RESPONSE_INVALID` 时，若 `result.stderr` 为空，附带 `returncode`、`len(result.stdout)` 与截断尾部摘要，便于排查。
3. **`tests/test_ssh_pool.py`**：
   - 新增离线回归测试：模拟 `exit_status_ready()` 先变为 `True`，随后数个轮询周期继续到达分片 stdout/stderr 数据包，最后才置位 `eof_received = True`，断言 `_exchange()` 完整收齐全部字节且无截断；
   - 验证在 `eof_received` 迟迟不来时仍受 `deadline` 超时保护。

#### 1.3 阶段 1 验收标准
- `python tools/check.py` 全绿；`bundle.local_version()` 维持 `c9c11663accd1fce`；
- 对已完成的远端任务 `si-scan-01_dosf77326` 通过默认连接池直接跑一次 `remote_fetch`，完整下载并校验 `dos.csv`、`dos.png`、`dos.pdf`、`dos-report.json` 与 `delivery-summary.json`（不重跑 VASP 计算）。

---

### 阶段 2（P1-a · 公共底座 + 3 个派生/重扫入口）：参数与核数一次确认 & 目录冲突安全递增

#### 2.1 要解决的两个痛点
1. **双重菜单与快照返工**：目前派生/重扫先弹参数选择菜单（按默认 8 核建目录），AI 审阅完再弹 `request_submission` 确认核数并授权提交；若在第二个菜单里改核数，`submit.slurm` 改写会导致快照失效重审。
2. **DOS 与 Band 派生重跑撞目录名**：
   - `dos_fork_menu.py` 的 `job_name()` 按参数取值 6 位哈希生成 `<source>_dos<hash>`；
   - `band_path_menu.py` 的 `job_name()` 按候选序号生成 `<source>_path<choice>`（自定义路径甚至固定 `_pathcustom`）；
   - 当旧目录已存在（例如旧 release 下失败后升级引擎想用相同默认参数重跑，或再次使用自定义能带路径），程序拒绝覆盖导致无法直接按原参数重派。

#### 2.2 公共底座设计（建议新增独立小模块，确保全部文件 `<= 500` 行）
1. **安全递增目录名助手**（如 `src/vasp_agent/chat/fork_naming.py` 或合并入公共核对助手）：
   - 当预设派生名 `<base>` 已存在（本地 `job(name).exists()` 或远端返回“目录已存在”冲突）时，**绝不覆盖旧目录**；
   - 在菜单中明确告知 `<base>` 已存在（注明不会覆盖原目录），并自动计算下一个可用后缀 `<base>_2`、`<base>_3`…（保证总长度 `<= 64` 且匹配 `JOB` 正则），由操作者确认采用新目录名后继续创建。
2. **统一“参数 + 资源核数 + 单阶段提交预授权”核对子模块**（复用 `permissions/resource_menu.py` 与 `submission_dialog.py` 的校验、默认值持久化与 `KPAR/NCORE` 预览逻辑）：
   - 在同一张交互菜单中展示：
     - **【计算参数区】**：各入口专属参数与预检摘要；
     - **【提交资源区】**：提交模式（`allocation` / 仅 `convergence` 支持 `array`）、并发 × 每组 MPI 核数、节点数、分区、时限、`KPAR/NCORE` 预览（默认加载服务器已保存的 `submission_defaults`，无则取服务器/来源任务默认）；
   - 统一收口选项：
     - `1` **确认参数与核数，并在审阅通过后直接提交本阶段**（建目录并按确认资源写好 `submit.slurm`，同时在本地 `Toolbox` 登记仅限该 `(server, new_job_name, target_phase)` 的 `confirmed_resource_selections`、`resource_confirmations` 与 1 次 `allow_submit` 授权）；
     - `2` **确认参数与核数，仅准备（暂不提交）**（同样按确认好的核数落盘，但不授予提交权）；
     - `3` 修改计算参数（各入口沿用原有字段修改交互）；
     - `4` 修改核数与资源（支持直接输入 `并发=... 每组=... 节点=... 分区=... 时限=...`，与现有资源菜单语法完全一致，且可选保存为服务器默认）；
     - `q` 取消（不建目录、不授权）。
   - **落盘一致性**：
     - 创建派生/重扫目录时直接将操作者确认后的资源写入 `submit.slurm` / `remote-job.json`（可通过 stdin `worker_prepare.py` 接受可选 `resources` 参数并复用 `worker_resources` 校验，或创建后立即应用已确认资源再进入审阅），确保后续 `get_stage_review` 看到的快照就是最终资源快照，**零重审返工**；
     - AI 拿到返回结果（附 `submission_preauthorized: true` 及明确 `next` 指引）后，照常执行 `get_stage_review` → `record_stage_review`；仅当 `allowed=true` 时直接调 `submit_stage`，若审阅为 `blocked` 或程序预检未过则停止提交且不消耗额度。

#### 2.3 本阶段接入的 3 个入口
1. **DOS 派生**（`src/vasp_agent/chat/dos_fork_menu.py`）：目标阶段 `dos`，接统一资源区 + 预授权 + 目录冲突 `_2/_3` 递增；
2. **换能带路径派生**（`src/vasp_agent/chat/band_path_menu.py`）：目标阶段 `bands`，接统一资源区 + 预授权 + 目录冲突 `_2/_3` 递增（彻底解决同编号和多次 `c` 自定义路径撞名问题）；
3. **选择性重扫**（`src/vasp_agent/chat/toolbox_workflow.py` 的 `prepare_scan_retry` 与 `src/vasp_agent/ui/scan_retry_summary.py`）：目标阶段 `convergence`（支持 `allocation` / `array` 模式），接统一资源区 + 预授权。

#### 2.4 阶段 2 验收标准
- 新增离线测试覆盖全部 3 个入口：
  1. 选 `1`：资源正确写入、单阶段预授权仅绑定该 `(server, job_name, phase)`，经 `get_stage_review` → `record_stage_review(allowed=true)` 后可直接 `submit_stage` 成功，且提交后授权立即撤销、不可跨任务/跨阶段复用；
  2. 选 `1` 但审阅判 `blocked`：远端与本地均硬阻断提交；
  3. 选 `2`：按所选核数建目录，但 `allow_submit` 保持 `False`，`submit_stage` 报 `SUBMISSION_NOT_AUTHORIZED`；
  4. 目录名冲突（DOS 同默认参数、Band 同候选号及多次自定义路径）：不覆盖旧目录，操作者确认后生成 `_2` / `_3` 新目录；
  5. 非交互模式（`plan_reader is None`）仍严格返回 `operator_confirmation_required`，绝不代选或预授权。
- `python tools/check.py` 全绿；`bundle.local_version()` 维持 `c9c11663accd1fce`；`PROMPT_TOKEN_BUDGET <= 14800`。

---

### 阶段 3（P1-b · 新任务入口）：`/plan` 升级为三合一启动核对窗口

#### 3.1 目标
让**全新任务**（`propose_scan_plan` / `/plan` → `prepare_scan_plan`）同样做到 **1 次启动核对窗口完成确认**，把当前分散的 3 次确认收口为 1 次：
1. **【物理模型与研究目标】**（原 `request_physics_confirmation`）；
2. **【扫描参数、可选阶段参数与环境配置】**（原 `/plan`）；
3. **【首阶段 `convergence` 核数/数组/资源与提交预授权】**（原 `request_submission`）。

#### 3.2 核心设计
1. **草案与交互菜单扩展**（`src/vasp_agent/planning/parameter_plans.py`，按 `<= 500` 行合理拆分辅助模块）：
   - `propose_scan_plan` 保持向后兼容，可复用已有 `rationale` / 结构维度生成初始物理目标建议（非磁性 PBE 范围声明仍由程序明确展示，不支持磁性/SOC/U/HSE/1D/0D 的阻断规则不变）；
   - 在 `/plan` 统一启动核对窗口中分区展示：
     - 区块 A：物理模型与研究目标（可按项编辑文本，确认后随准备流程写入 `physics-model.json`，`confirmed_by='operator_chat_menu'`）；
     - 区块 B：ENCUT / k 网格序列、`scan_preset`、赝势变体、可选阶段（如 `dos`）参数、环境配置；
     - 区块 C：首阶段 `convergence` 提交资源（`allocation`/`array`、并发 × 每组 MPI、节点、分区、时限、`KPAR/NCORE` 预览，复用阶段 2 公共资源核对组件）；
   - 统一收口选项：
     - `1` 确认物理目标、参数与核数，准备并在审阅通过后直接提交首阶段 `convergence`（仅授 `convergence` 1 次提交权）；
     - `2` 确认物理目标、参数与核数，仅准备（暂不提交）；
     - 分别修改物理目标 / 计算参数 / 核数与资源 / 暂不决定 / 取消。
2. **准备与授权衔接**（`src/vasp_agent/chat/toolbox_workflow.py`、`src/vasp_agent/chat/stage_commands.py`）：
   - `prepare_scan_plan` 按已选方案建目录后，若方案带有操作者现场确认的物理目标，立即调用既有 `confirm_physics` 通道写入物理确认记录；若带有确认的资源与首阶段预授权，同步应用资源并登记仅限 `(server, job_name, 'convergence')` 的单次提交授权；
   - **保留旧入口零破坏**：`request_physics_confirmation` 与 `request_submission` 工具完整保留，继续服务旧任务、后续阶段（`relaxation` / `scf` / `bands` / `dos`）、以及“仅准备后另行提交”的场景；
   - **不跨阶段连授**：`convergence` 提交消费额度后，`allow_submit` 自动归零；后续进入 `relaxation` / `scf` / `bands` / `dos` 仍必须逐阶段审阅并在 `request_submission` 菜单单独授权。
3. **提示词最小同步**（`src/vasp_agent/chat/prompts.py`）：
   - 用最精简句式说明：当 `prepare_scan_plan` 或派生菜单返回已含操作者确认的物理目标与单阶段预授权时，无需重复弹 `request_physics_confirmation` 或 `request_submission`，直接走 `get_stage_review` → `record_stage_review(allowed=true)` → `submit_stage`；
   - 严守 `tests/test_prompt_budget.py` 全部安全短语和 `14800` 预算。

#### 3.3 阶段 3 验收标准
- 新增端到端离线回归测试：
  1. 新任务在 `/plan` 选 `1`：单次菜单完成物理确认 + 参数确认 + 资源确认 + `convergence` 预授权 → `prepare_scan_plan` → `get_stage_review` → `record_stage_review` → `submit_stage('convergence')` 全通；
  2. `convergence` 提交后尝试提交 `relaxation`：必须报 `SUBMISSION_NOT_AUTHORIZED`（证明绝不跨阶段连授）；
  3. 审阅 `blocked`（如不支持的模型或预检失败）：即使选了 `1` 也必须拒收提交；
  4. 选 `2`（仅准备）：写入物理确认与资源，但 `allow_submit=False`；
  5. 既有 `/plan`、`/physics` 与 `test_stage_parameters.py` / `test_prompt_budget.py` / `test_model_views.py` 全部保持通过。
- `python tools/check.py` 全绿；`bundle.local_version()` 维持 `c9c11663accd1fce`。

---

### 阶段 4（P2 · 文档与接口卡收口）

#### 4.1 改动范围
- [WORKFLOW_MODULES.md](../design/WORKFLOW_MODULES.md)：修正 §11.6 已过时的“分支 `claude/software-inventory-e2f34d`，未合并、未部署”标注，补充阶段 1–3 落地后的统一核对窗口说明；
- [chat.md](../modules/chat.md) 与 [remote.md](../modules/remote.md)：补充新增辅助模块（如资源核对/目录命名助手）与 `ssh_pool` 的 `EOF` 排空契约；
- [DOS.md](../guides/DOS.md) 与 [BAND_PATHS.md](../guides/BAND_PATHS.md)：更新派生菜单（含同参数/同路径递增后缀与一次确认核数）的使用说明。

#### 4.2 阶段 4 验收标准
- 文档与代码现状 100% 一致，链接有效，`python tools/check.py` 全绿。

---

### 阶段 5（P3 / Step 3）：Docker 沙箱轻量 CodeAct 增强

#### 5.1 目标
在完全不削弱容器隔离、不触碰正式任务目录、不改动科学引擎的前提下，增强 `analysis_runner.py` 的本地科学分析能力（解决纯标准库 `python:3.12-slim` 缺乏 `numpy`/`scipy`/`matplotlib` 且无法受控输出图表/数据表的问题）。

#### 5.2 核心设计
1. **可选预装科学镜像支持**（`src/vasp_agent/editing/analysis_runner.py`）：
   - 保持默认基线镜像 `python:3.12-slim`，支持通过白名单/配置检测本地已预装的科学计算镜像（如只接受本地已有镜像 ID 校验，**严禁自动拉取**，仍强制 `--pull=never --network=none --read-only --cap-drop=ALL --security-opt=no-new-privileges --user=65534:65534` 及内存/CPU/PID/fsize/timeout 上限）；
   - `get_analysis_runtime` 返回默认镜像与可选科学镜像的本地可用状态。
2. **受控只写产物导出目录**：
   - 仅当调用显式请求导出（或默认挂载只写临时导出目录再经宿主机校验搬运）时，在容器内提供受限临时输出挂载（如 `/output`），运行结束后由宿主机严格校验并原子落盘至 `workbench/outputs/<run_id>/`：
     - 严格限制文件数量上限、单文件大小上限、总导出体积上限与允许后缀白名单（如 `.csv`/`.json`/`.txt`/`.md`/`.png`/`.svg`/`.pdf`）；
     - 严禁符号链接、硬链接、路径穿越、越出 `workbench/outputs/<run_id>/`；
     - 导出清单与逐文件 SHA-256 写入运行回执（`.agent/analysis_runs/<run_id>.json`），并明确标注 `content_is_untrusted: true`（不代表科学验收或正式任务产物）。
3. **工具接口与向后兼容**（`src/vasp_agent/chat/toolbox_workspace.py`、`src/vasp_agent/chat/tool_schemas.py`）：
   - 任何新增可选参数全部落在 **`sandbox_analysis` 懒加载组**内，不影响常驻工具目录预算；
   - **严禁删除或削弱**确定性工具 `analyze_band_results` 与 `replot_bands`（常规能带与 DOS 验收/重绘仍优先走确定性工具）。

#### 5.3 阶段 5 验收标准
- 新增离线单测覆盖：预装镜像探测与无镜像拒绝（不自动拉取）、受控导出成功及回执 SHA-256 核对、超文件数/超体积/非法后缀/符号链接/路径穿越全部拒绝、容器隔离参数断言不变；
- `python tools/check.py` 全绿；`bundle.local_version()` 维持 `c9c11663accd1fce`；`PROMPT_TOKEN_BUDGET <= 14800`；改动源码单文件 `<= 500` 行。

---

## 5. 执行方式与主对话协作约定

建议按 **阶段 1 → 阶段 2 → 阶段 3 → 阶段 4 → 阶段 5** 顺序逐段推进，每段开始前先读本文对应章节及涉及子包的 `docs/modules/*.md` 接口卡，每段完工按下列清单收口：

1. 运行 `python tools/check.py` 全绿（`ruff` + `pytest` + `loc` 0 warnings）；
2. 运行 `bundle.local_version()` 确认仍为 `c9c11663accd1fce`；
3. 核对 `git status --short -uall`，确保 `.agent/` 与历史数据目录零触碰、`prompts/session_handoff_prompts.md` 保持未跟踪；
4. 为本阶段建立清晰 commit，并在本文第 3 节进度表中将对应阶段标记为 `[x] 已完成（<commit>）`。

---

## 6. 完工归档步骤（放回温/冷归档区）

当第 3 节 5 个阶段全部标记为 `[x]` 且最终 `python tools/check.py` 全绿后，执行归档：

```powershell
# 1. 将计划文档移至归档区（受 .ignore 覆盖，不参与日常默认搜索，完整保留 Git 历史）
New-Item -ItemType Directory -Force -Path docs/archive/2026-11 | Out-Null
git mv docs/development/UNIFIED_CONFIRMATION_AND_STABILITY_PLAN.md docs/archive/2026-11/UNIFIED_CONFIRMATION_AND_STABILITY_PLAN.md

# 2. 校验与提交归档
python tools/check.py
git commit -m "docs(archive): archive completed unified confirmation and stability plan"
```

---

## 7. 附注：非代码本机环境建议

本次集群实测发现 `eshell111.hpccube.com:65082` 走了本地代理 Fake-IP 网段（`198.18.0.86`），导致 SSH 握手常需 20–30 s、偶发断连以及 60 s 后台监控 `REMOTE_TIMEOUT`。建议在本地代理规则中为超算域名配置直连并跳过 Fake-IP：

- 路由规则：`DOMAIN-SUFFIX,hpccube.com,DIRECT`
- Fake-IP 跳过列表（`fake-ip-filter`）：`+.hpccube.com`
