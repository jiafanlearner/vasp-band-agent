# 设计文档：Layer 4 控制面降本与事件驱动唤醒（路线 1）

> **状态**：已实施（Layer 4 控制面与监控辅助全部落地，远端 `release=5b124e988c010ec8` 保持冻结）  
> **日期**：2026-11-28  
> **适用范围**：纯 Layer 4 控制面（`control/`）与 Layer 3 监控辅助（`monitoring/job_monitor.py`、集成套件 `integrations/vasp-autonomy/`）；**零 `ENGINE_MODULES` / `WORKER_UNITS` 改动，远端引擎 `release=5b124e988c010ec8` 保持冻结，无需 `remote_deploy`，`PROMPT_TOKEN_BUDGET = 14800` 零变化**。

---

## 1. 意图与背景

P3 路线 A（A3 插件隔离 + A2 真实集群 5 阶段端到端验收）暴露出两项显著的外部代理运行成本瓶颈，以及一处滞后的阶段标识常量：

1. **Checker 审阅溢出与高频翻页**：`vasp_get_stage_review` 先由 `chat/model_views.py` 精简，但只要精简后略超 `control/service.py` 的 `MAX_RESPONSE_BYTES = 12 KB`，`_compact_and_cap()` 就会丢弃精简视图、回退转存 45–56 KB 的未精简原文 `full_result`；叠加 `Toolbox.read_context_evidence()` 固定 `3500` 字符/页，导致独立只读子代理 `vasp-reviewer` 单阶段被迫分页 13–17 次。
2. **作业等待期轮询空转**：`vasp-ctl watch` 目前只有 `once` / `daemon` / `ack`，主会话依赖 `/schedule` 每 5–15 分钟唤醒 LLM 调用 `vasp_watch_jobs(poll=true)`；A2 实测 68 轮中近半为监控空转，在长耗时体系下会持续膨胀上下文并消耗配额。
3. **阶段常量滞后**：`control/service_contracts.py` 中的 `AUTONOMY_PHASE` 仍停留在 `'P2_maker_checker_autonomy'`。

---

## 2. 目标、非目标与反目标

### 目标
- **子项 A（三层漏斗视图与溢出兜底）**：常规 5 阶段（`convergence` / `relaxation` / `scf` / `bands` / `dos`）的 `vasp_get_stage_review` 压缩至约 **4–9 KB 直接内联（0 次分页）**；极端大载荷超过 **24 KB** 时，默认转存精简/投影后的视图供 `vasp_read_evidence` 按 **8000 字符/页**读取，同时保留原始完整载荷指针 `raw_sha256` 供按需深查。
- **子项 B（事件驱动零空转等待 `vasp-ctl watch wait`）**：新增纯 Python 阻塞等待子命令，作业运行期间完全静默（**0 LLM token**），采用**终态优先 + 连续错误阈值唤醒**，在外部已有 `watch daemon` 持锁时自动只读旁听并在锁释放后无缝接管，退出时借 Antigravity Reactive Wakeup 唤醒主会话。
- **子项 C（阶段常量与集成文档同步）**：将 `AUTONOMY_PHASE` 升至 `'P3_bounded_autonomy'`，并同步更新 `integrations/vasp-autonomy/` 与本地排除目录 `.agents/plugins/` 中的 Skill / Agent 说明。

### 非目标
- 不触碰 `ENGINE_MODULES` / `WORKER_UNITS` / 远端 `snapshot` 计算逻辑；
- 不改变交互式 CLI 侧 `Toolbox.read_context_evidence()` 默认 `3500` 字符分页和 `chat/model_views.py` 的行为契约（保证 `tests/test_model_views.py`、`tests/test_context_budget.py` 零破坏）；
- 不修改 `control/tool_specs.py` 的工具 JSON Schema（`PROMPT_TOKEN_BUDGET` 零增长）；
- 本轮不实现基于 Antigravity 本地 `ConversationService/CreateConversation` 的跨会话触发器（保留为远期方案 B）。

### 反目标（出现即判失败）
- 产生 `model_views` 的 `same_as` 悬空指针，或在投影后体积 `< MIN_CHARS` 时丢失 `full_sha256` 原文索引；
- 因阶段裁剪导致 Checker 缺失 `stage_definitions.py` 要求的任一核验依据（例如 `dos` 缺失 SCF 电子收敛/力/应力，或丢失环境漂移审计依据）；
- `watch wait` 绕过 `JobMonitor` 错误指数退避高频轰炸故障登录节点，或污染磁盘 `job-monitor.json` 中已持久化的 `interval_seconds`；
- 在连续遇到同类错误（`event_outbox` 去重不追加新行）时漏触发连续错误阈值唤醒；
- 允许 `--role checker` 通过 `vasp-ctl watch wait` 发起远端 SSH 轮询；
- 任何新建或修改的 Python 源码超过 500 行软守门阈值，或破坏原模块 re-export 兼容门面。

---

## 3. 工程约束

- **引擎冻结**：`bundle.local_version() == '5b124e988c010ec8'` 逐字节不变。
- **单文件行数**：所有源码保持 `<= 500` 行（`service.py` 现 493 行，须先将溢出处理抽离至 `control/service_overflow.py`）。
- **跨平台确定性**：源码与新增落盘统一 UTF-8 + LF（`newline='\n'`）；`watch wait` 须在 Windows 本机通过文件锁并发与旁听/接管回归测试。
- **安全与权限边界**：`autonomy_guard.py` 仅拦截 `vasp-ctl contract approve|revoke|resume`，继续放行 `vasp-ctl watch wait` 并补齐单测；严禁在测试或运行期触碰真实 `.agent/`、历史任务目录或 `~/.gemini/config/`。

---

## 4. 方案对比与选型

### 子项 A：阶段审阅精简与溢出治理
| 方案 | 做法 | 权衡 | 结论 |
|---|---|---|---|
| A1：仅提上限与页宽 | 将上限调大到 24–32 KB、分页放大到 8000 | 简单，但每阶段仍充斥全部历史阶段 INCAR 与早期摘要，12–22 KB 噪音干扰 Checker 注意力，大超胞仍易溢出 | 放弃 |
| A2：先 `model_views` 后阶段裁剪 | 先跑第 1 层 `model_views`，再在第 2 层按阶段裁剪 | `model_views` 已将 `incars` 改写为 `{base, differences}` 且植入 `same_as` 路径引用；后裁剪会迫使反解结构并制造 **`same_as` 悬空死链** | 放弃 |
| **A3：反向三层漏斗（选定）** | **① 阶段聚焦投影 (`stage_review_view.py`) → ② 复用 `model_views` 去噪 → ③ 24 KB 限幅与精简视图溢出兜底 (`service_overflow.py`) + 8000 字符页宽** | 在原始 dict 上先投影，彻底消除 `same_as` 悬空；典型 5 阶段压至 4–9 KB 零翻页；零改动 `model_views.py` 核心逻辑，完全适配后续 P4a 分阶段 INCAR | **采用** |

### 子项 B：事件驱动阻塞等待
| 方案 | 做法 | 权衡 | 结论 |
|---|---|---|---|
| B1：任意事件即时唤醒 | `outbox` 出现 `terminal` / `error` / `recovered` 任一事件即退出 | 与 outbox 1:1 对齐，但集群冷 SSH 偶发单次超时就会打断 0-token 等待、频繁唤醒 LLM | 放弃 |
| B2：跨会话创建触发器 | 基于 `ConversationService/CreateConversation` 终态开新会话 | 依赖宿主内部 API 且上下文交接复杂，适合超长任务远期增强 | 留作后续 |
| **B3：终态优先 + 连续错误阈值唤醒（选定）** | `vasp-ctl watch wait` 进程内静默等待，默认仅在 `terminal` 或活跃作业连续错误达到阈值（默认 3）时唤醒退出；另提供 `--wake-on-error` 开关 | 零额外依赖、跨平台，弱网下保住 0-token 等待且防永久故障死等；与 Antigravity Reactive Wakeup 天然闭环 | **采用** |

---

## 5. 子项 A 详细设计：三层漏斗控制面视图与溢出治理

### 5.1 模块布局与兼容接口
1. **`src/vasp_agent/control/stage_review_view.py`（新建，预计 ~200–240 行）**
   - 导出 `project_stage_review(full_result: dict[str, Any], store: Callable[[Any], dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]` 与 `build_stage_review_view(full_result: dict[str, Any], store: Callable[[Any], dict[str, Any]]) -> dict[str, Any]`。
2. **`src/vasp_agent/control/service_overflow.py`（新建，预计 ~150–180 行）**
   - 承接从 `service.py` 抽离的常量与辅助：`MAX_RESPONSE_BYTES = 24 * 1024`、`EVIDENCE_PAGE_CHARS = 8000`、`SUMMARY_KEYS`、`compact_solver_diff`、`extract_summary`、`trim_log_tail`、`compact_and_cap`。
3. **`src/vasp_agent/control/service.py`（由 493 行降至 ~390 行）**
   - 从 `service_overflow` re-export `MAX_RESPONSE_BYTES`（保持 `vasp_agent.control.MAX_RESPONSE_BYTES` 与 `vasp_agent.control.service.MAX_RESPONSE_BYTES` 导入兼容）；
   - `ControlService._compact_and_cap()` 委托 `service_overflow.compact_and_cap(...)`；
   - `ControlService.vasp_read_evidence(kind='evidence')` 调用 `self.box.read_context_evidence(sha256, offset, page_chars=EVIDENCE_PAGE_CHARS)`。
4. **`src/vasp_agent/chat/toolbox_core.py`（小幅向后兼容扩展）**
   - `read_context_evidence(self, sha256, offset, *, page_chars=3500)`：新增可选关键字参数 `page_chars`，默认保持 `3500`，校验 `type(page_chars) is int and 1 <= page_chars <= 20000`。交互式调用方不传参时行为 100% 不变。

### 5.2 第 1 层：阶段聚焦投影（`stage_review_view.py`，在 `model_views` 之前执行）

**触发条件**：仅当工具为 `vasp_get_stage_review`、`full_result` 为 `dict`、`phase` 存在且属于 `stage_definitions.known_names()`，且存在可投影的 `inputs` 或 `evidence` 字典时执行；否则跳过第 1 层，直接进入第 2 层（兼容测试中构造的最小合成载荷）。

**原文存证**：投影开始前先执行 `raw_stored = store(full_result)`（复用内容寻址 `.agent/evidence/<sha256>.json`，若已存在不重写），获得 `raw_stored['sha256']` 与 `raw_stored['chars']`。

**字段保留与折叠策略**（被折叠的原始字段均通过 `store(section_value)` 落盘，在原位或同级旁键写入 `{'omitted_for_phase': phase, 'sha256': stored['sha256'], 'chars': stored['chars']}`）：

| 路径 | `convergence` | `relaxation` | `scf` | `bands` | `dos` | 说明 |
|---|---|---|---|---|---|---|
| 顶层锚点与阶段专有字段（`phase`/`snapshot`/`review_lease`/`required_checks`/`check_details`/`rules_version`/`guides`/`physics`/`execution_tags`/`incar_solver_diff`/`stage_parameters`/`band_path`/`dos_plan`/`scf_occupation_audit`/`checkpoint`） | 原样保留 | 原样保留 | 原样保留 | 原样保留 | 原样保留 | `checkpoint.evidence_hashes` 交由第 2 层去重/压缩 |
| `inputs.potential_metadata`、`inputs.environment`、`inputs.config`、`inputs.manifest`、`inputs.production_sampling`、`inputs.convergence_reuse`、`inputs.parameter_choice` | 原样保留 | 原样保留 | 原样保留 | 原样保留 | 原样保留 | `inputs.environment` 仅 ~500B，全阶段原样保留以防环境漂移盲区；`manifest.hashes` 由第 2 层压为 `{count, combined_sha256}` |
| `inputs.initial_inputs` | 原样保留 | **整块折叠** | **整块折叠** | **整块折叠** | **整块折叠** | 实测仅含 `convergence/*` 文本文件 |
| `inputs.initial_poscar`、`inputs.submit_script`、`inputs.scan_verification`、`inputs.plan.parameter_notes` | 原样保留 | **整块折叠** | **整块折叠** | **整块折叠** | **整块折叠** | 非首阶段不再重复携带全量 POSCAR/脚本/静态参数说明 |
| `inputs.plan.incars` | 仅 `convergence/*` | 仅 `relax-*` 与 `validation/*` | 仅 `scf` | **`bands` + `scf`** | **`scf`** | 被折掉的条目存入同级旁键 `inputs.plan.incars_omitted`（含 `omitted_keys` + `sha256`），**不混入 `incars` 字典**，确保第 2 层 `_incar_table` 结构合法；`bands`/`dos` 保留 `scf`（~300B）便于交叉核对一致参数 |
| `inputs.plan.kpoints` | 原样保留 | 原样保留 | **整块折叠** | **整块折叠** | **整块折叠** | `plan.kpoints` 仅登记 `relax-*` / `validation/*` |
| `evidence.summaries` | 无上游（空/原样） | 仅 `convergence.json` | 仅 `relaxation.json`、`geometry-summary.json`、`post-relax-validation.json` | 仅 `scf-summary.json` | **仅 `scf-summary.json`** | 被折掉的摘要存入同级旁键 `evidence.summaries_omitted`（含 `omitted_keys` + `sha256`）；`dos` 保留 `scf-summary.json` 补齐 `dos_plan` 缺失的 SCF 电子收敛/力/应力证据 |

**未知或未来新增阶段的向前兼容规则**：
- 若遇到 `stage_definitions.DEFINITIONS` 中未来新增的阶段（如 `wannier`/`phonon`），`evidence.summaries` 自动根据 `depends_on` 反查依赖阶段的 `summaries` 集合；`plan.incars` 保留 `phase_of(k) == phase` 及直接依赖阶段对应键（若无匹配则保守全保留，宁可多留也不误删）。

### 5.3 第 2 层：复用 `model_views` 语义去噪与 `full_sha256` 衔接
`build_stage_review_view(full_result, store)` 执行顺序：
1. 对 `full_result` 执行第 1 层投影，得到 `(projected, view_meta)`，其中 `view_meta` 记录：
   ```json
   {
     "projected": true,
     "phase": "<phase>",
     "full_sha256": "<raw_stored.sha256>",
     "full_chars": 56201,
     "omitted": {
       "inputs/initial_inputs": "<sha256>",
       "inputs/plan/incars_omitted": "<sha256>",
       "evidence/summaries_omitted": "<sha256>"
     },
     "note": "控制面已按当前阶段聚焦投影；历史输入与非直接上游摘要已按内容寻址转存，可按需用 vasp_read_evidence 点读，full_sha256 指向未投影完整原文。snapshot 按完整数据计算。"
   }
   ```
2. 将 `projected`（**此时尚未挂载 `stage_review_view`**）传入 `model_views.compact_result(projected, store, 'get_stage_review')`，得到 `compacted`：
   - 哈希表、力/应力/晶格矩阵、残余重复子树按原规则降噪，**不存在指向已折叠路径的 `same_as` 悬空指针**；
   - 若 `compacted` 生成了 `model_view`（即投影后体积仍 `>= MIN_CHARS = 2000` 且发生降噪）：
     - 将 `compacted['model_view']['full']` 更新为 `raw_stored`（指向未投影的完整原始 `full_result`，而非投影版）；
     - 新增 `compacted['model_view']['sections_scope'] = 'projected_view'` 并补充提示：说明 `sections` 中的哈希对应投影视图内被 `model_views` 二次压缩的段落，`full` 指向完整原文。
3. **无论 `model_views` 是否触发（包括投影后体积 `< MIN_CHARS` 直接原样返回的场景）**，最后都将 `view_meta` 写入顶层 `compacted['stage_review_view']`。这样 `full_sha256` 与 `omitted` 索引表 100% 稳定存在，永不因 `< MIN_CHARS` 丢失。

### 5.4 第 3 层：控制面 24 KB 限幅与精简溢出兜底（`service_overflow.py`）
- `MAX_RESPONSE_BYTES = 24 * 1024`（24,576 字节，既让绝大部分投影后阶段审阅 0 翻页直出，又精确兼容现有 `tests/test_control_service.py` 中的大载荷溢出单测（~28.8 KB）与中文日志尾单测（24,946 B））。
- `compact_and_cap(box, name, args, output)` 流程：
  1. 若 `output.get('ok') is not True`，原样返回；
  2. `vasp_read_evidence` 跳过视图精简；`vasp_get_stage_review` 调用 `stage_review_view.build_stage_review_view(full_result, box.store_evidence)`；其余工具保持原 `COMPACT_ALIAS` / `model_views` 逻辑；
  3. 计算 `raw_bytes = len(_encode_json(compacted).encode('utf-8'))`：
     - 若 `raw_bytes <= MAX_RESPONSE_BYTES` 或 `is_evidence_page`：直接返回 `compacted`；
     - 若 `is_log_tail`：调用 `trim_log_tail(box, full_result, raw_bytes)`（沿用二分保尾逻辑，上限自动同步为 24 KB）；
  4. 触发 `payload_overflow`（极端大体系或长诊断）：
     - `compacted_result = compacted.get('result')`；
     - `view_stored = box.store_evidence(compacted_result)`（**默认分页目标改为精简后的视图**）；
     - `raw_stored = box.store_evidence(full_result)`（完整未精简原文，写入 `raw_sha256`、`raw_chars`）；
     - `summary = extract_summary(full_result)`（继续永保 `snapshot`、`phase`、`review_lease`、`incar_solver_diff` 等全部 `SUMMARY_KEYS`）；
     - `preview` 取精简视图 `_encode_json(compacted_result)[:preview_limit]`，二分收缩确保整体严格 `<= MAX_RESPONSE_BYTES`；
     - `read_tool` 指向 `vasp_read_evidence(kind='evidence', sha256=view_stored['sha256'], offset=0)`；
     - `note` 写明 24 KB 上限、`sha256` 为精简视图、`raw_sha256` 为完整原文，每次分页返回最多 8000 字符。

---

## 6. 子项 B 详细设计：`vasp-ctl watch wait` 零空转阻塞等待

### 6.1 模块布局与 CLI 语法
1. **`src/vasp_agent/control/watch_cli.py`**（由 267 行增至 ~315 行）：在 `register_watch_subparser` 注册 `wait` 子命令，在 `handle_watch` 中校验角色并分发至 `watch_wait.run_wait(...)`。
2. **`src/vasp_agent/control/watch_wait.py`（新建，预计 ~240–290 行）**：实现等待循环、锁协同、事件/错误阈值判定、信号处理与文本/JSON 输出。
3. **`src/vasp_agent/monitoring/job_monitor.py`（由 420 行增至 ~445 行，非 `ENGINE_MODULES`）**：支持进程内可选的覆盖间隔计算，不污染持久化注册表、不破坏错误指数退避。

CLI 语法：
```powershell
vasp-ctl [--workspace .] [--role maker] [--actor-id ...] watch wait \
  [--server SERVER] [--job-name JOB_NAME] [--phase PHASE] \
  [--poll-interval SECONDS] \
  [--max-consecutive-errors N] \
  [--wake-on-error] \
  [--max-seconds SECONDS] \
  [--json]
```

### 6.2 参数与权限契约
- **`--role checker`**：一律拒绝，返回 `TOOL_NOT_ALLOWED_FOR_ROLE`（`exit 1`），与 `vasp_watch_jobs(poll=true)`、`watch daemon` 保持一致的只读隔离。
- **`--server` / `--job-name` / `--phase`**：复用 `service_watch` 同款正则与 `stage_definitions.known_names()` 校验，非法返回 `INVALID_ARGUMENTS`（`exit 2`）。
- **`--poll-interval`**：可选整数，合法范围 **`60 <= value <= 600`**（对齐 `JobMonitor` 60 秒最小安全间隔；默认 `None` 表示完全遵循注册表原间隔）。
- **`--max-consecutive-errors`**：整数，默认 **`3`**，合法范围 `1 <= value <= 50`。
- **`--wake-on-error`**：开关标志（默认 `False`），开启后任一匹配 `kind='error'` 事件即立刻唤醒退出。
- **`--max-seconds`**：有限正浮点数，默认 **`14400.0`**（4 小时上限，防止孤儿进程无限挂起；可按需显式指定更短或更长值）。

### 6.3 不污染注册表、保留指数退避的 `--poll-interval` 机制
为避免改写磁盘 `.agent/state/job-monitor.json` 中的 `interval_seconds`，也避免绕过 `JobMonitor` 的错误指数退避，在 `JobMonitor` 新增两处**纯内存、向后兼容**扩展（默认均为 `None`，零破坏现有调用方）：
- `JobMonitor.__init__` 增加 `poll_interval_override: int | None = None`（仅允许 `60 <= v <= 600`）与 `poll_filter: Callable[[dict[str, Any]], bool] | None = None`（可选，仅对匹配 `watch wait` 过滤条件的活跃作业应用覆盖间隔，不影响工作区里其他不相关作业）；
- `JobMonitor.check_once(self, *, task_filter: Callable[[dict[str, Any]], bool] | None = None)`：允许定向仅对满足 `task_filter` 的匹配作业执行单次实况探测（供 `watch wait` 重入时刷新已达错误阈值的故障作业，不骚扰其他正常作业）。

在 `JobMonitor` 内部定义 `_effective_due(task)`:
- 若未设置 `poll_interval_override` 或 `poll_filter` 不匹配：直接返回 `task['next_due_at']`（与现有行为完全一致）；
- 若匹配：
  - `base = min(task['interval_seconds'], self._poll_interval_override)`（仅允许收紧，不拉长原本更短的间隔）；
  - `err_cnt = int(task.get('error_count') or 0)`；
  - `delay = min(86400, base * 2 ** min(err_cnt - 1, 4))` if `err_cnt > 0` else `base`（**严格对齐 `job_monitor.py#L369` 指数退避公式**）；
  - 若 `task.get('last_checked_at') is not None`：返回 `task['last_checked_at'] + delay`；
  - 若 `last_checked_at is None`（刚提交尚未首次查过）：取注册时的基准时间 `registered_at = task['next_due_at'] - task['interval_seconds']`，返回 `registered_at + delay`。
- `_compute_wait(data)` 与 `_check(force=False)` 的到期判定统一改用 `_effective_due(task)`；写回磁盘的 `task['interval_seconds']` 与 `task['next_due_at']` 仍按原逻辑记录，**磁盘状态零污染**。

### 6.4 状态机、同错去重防护、防重入死循环与锁协同（Owner / Observer 自动切换）
**三个必须显式处理的边界（经 Builder Check 源码核验补齐）**：
1. **监控开关关闭（`/watch off` 或匹配作业 `task['enabled'] == False`）**：`JobMonitor` 与外部 `daemon` 在 `data['enabled'] == False` 或任务自身 `enabled == False` 时不会发起任何 SSH 轮询。若不显式拦截，`watch wait` 会静默坐满 `--max-seconds`（默认 4 小时）。**决策（我方拟定）**：尊重操作者开关——当存在匹配活跃作业，但全局 `monitor_enabled is False` 或所有匹配活跃作业均处于 `enabled == False` 且无待唤醒事件时，立即以 `reason="monitor_disabled"`、`ok: true`、`exit 0` 退出并提示先开启 `/watch on`。
2. **`event_outbox` 同错去重与历史已恢复错误抑制**：
   - `job_monitor.py#L367` 在连续遇到同类错误时 `changed = (old_error != error)` 为 `False`，不会追加新事件行，但会递增注册表 `job-monitor.json` 的 `task['error_count']`；因此必须同时检查 `pending_events` 与 `active_jobs` 中的实时 `error_count`。
   - `event_outbox` 按追加顺序保存未 ack 的 `[error, recovered, ...]`；对同一作业 `(server, job_name, phase, job_id)`，若 `pending_events` 中在某条 `error` 之后已存在更晚的 `recovered` 事件（且其后无更新的 `error`），该旧 `error` 视为已自愈，**不触发错误唤醒**（无论默认模式还是 `--wake-on-error` 模式），避免历史已恢复错误误打断等待。
3. **重入防 0-ms 死循环（`task['error_count']` 跨调用持久化的处理）**：
   - `task['error_count']` 仅在 `JobMonitor` 轮询成功时归零，`vasp_task_status` 或 `vasp_watch_jobs(poll=false, ack_ids=...)` 不会清零它。若 Maker 收到 `error_threshold_reached`、核查发现集群已恢复后直接重跑 `watch wait`，盲目在 0 ms 预检中再次触发 `error_threshold_reached` 会造成无限零等待唤醒死循环。
   - **决策（我方拟定）**：
     - **预检阶段**：优先处理 `terminal` 事件（立即返回 `reason="event"`）、无匹配活跃作业（`reason="no_active_jobs"`）与监控被暂停（`reason="monitor_disabled"`）；
     - **若启动时已有匹配活跃作业达到 `error_count >= max_consecutive_errors`**：
       - 若能获取 `worker_lock`（`owner` 模式）：在进入休眠前，对这些超阈值匹配作业先执行一次定向实况探测 `monitor.check_once(task_filter=...)`——若网络已恢复，探测成功将自动归零 `error_count` 并写入 `recovered`，随后平滑进入 0-token 等待；若故障仍在持续，探测失败后立即以 `reason="error_threshold_reached"` 退出（确证故障未解，绝不空等）；
       - 若外部 `daemon` 持有 `worker_lock`（`observer` 模式）：记录启动基线 `initial_error_counts[key] = job['error_count']`；若该作业存在尚未被 ack 且未被 `recovered` 覆盖的 `error` 事件，则立即以 `reason="error_threshold_reached"` 唤醒（防止外部 daemon 刚达阈值却被忽略）；若旧 `error` 事件已被 Maker `ack`（说明 Maker 已知悉并选择继续观察），则不秒退，仅在该作业 `error_count` **再次递增（`> initial_error_counts[key]`）** 或出现新的未恢复 `error` 事件时唤醒。

**唤醒判定优先级**（每次检查函数 `_evaluate_state(...)`）：
1. **事件唤醒（`reason="event"`）**：`wake_events` 非空，其中
   - 所有 `ev['kind'] == 'terminal'` 均入选；
   - 对每个 `(server, job_name, phase, job_id)` 先过滤掉“已被更晚 `recovered` 覆盖”的旧 `error` 事件；剩余未恢复 `error` 事件中：若 `wake_on_error=True` 全部入选，否则仅 `int(ev.get('error_count') or 0) >= max_consecutive_errors` 入选（`recovered` 本身不单独打断等待）。
2. **连续错误阈值唤醒（`reason="error_threshold_reached"`）**：存在任一匹配活跃作业满足连续错误触发条件（等待过程中 `error_count >= max_consecutive_errors`，或 `observer` 模式下已 ack 旧错误后 `error_count` 较启动基线再次递增）。
3. **无活跃作业早退（`reason="no_active_jobs"`）**：`active_jobs` 为空且未触发上述唤醒（防止对已结束或未注册作业空等至超时）。
4. **监控已暂停早退（`reason="monitor_disabled"`）**：`active_jobs` 非空，但全局 `monitor_enabled is False` 或所有匹配活跃作业均满足 `enabled is False`（需让 `_read_active_jobs` 保留 `enabled` 字段）。

**执行流程**：
1. **预检查（Immediate Check）**：进入循环前检查 `terminal` / 未恢复立即事件、`no_active_jobs`、`monitor_disabled`；若已满足，直接以 `mode="immediate"` 输出结果并 `exit 0`（防止提交到启动 `watch wait` 间隙已完成导致丢唤醒）。
2. **主等待循环（完全静默，等待期间不打印任何心跳 stdout）**：
   - 安装 `SIGTERM`（若主线程支持）与捕获 `KeyboardInterrupt`，统一触发 `stop_event`；
   - 若启动时存在 `error_count >= max_consecutive_errors` 的匹配作业且当前未由外部持锁，先在 `monitor.start()` 获锁前/后立即做一次定向实况探测（`check_once(task_filter=...)`）以打破重入死循环；
   - 尝试 `monitor.start(poll_fn, _on_notify)` 获取 `worker_lock`：
     - **拿到锁（`mode="owner"`）**：`JobMonitor` 后台线程按 `_effective_due` 动态休眠并轮询；每当 `_on_notify(event)` 回调到达（终态/错误/恢复）立即 `wake_signal.set()`，主线程零延迟复查 `_evaluate_state()`；同时周期性检查 `monitor._thread.is_alive()`（若因持久化损坏退出，以 `reason="monitor_exited"`, `ok=False`, `exit 1` 收尾）。
     - **未拿到锁（`mode="observer"`，`daemon_detected=True`）**：外部 `watch daemon` 或交互会话正持有 `worker_lock`，`watch wait` 绝不发起重复 SSH，改为按 `observer_interval`（默认 `1.0s`，测试可注入更小值）静默轮询本地状态并复查 `_evaluate_state()`；同时每隔 `lock_probe_interval`（如 `2.0s`）非阻塞尝试一次 `monitor.start()`——一旦外部 daemon 中途退出释放 `worker_lock`，立即自动接管并切入 `mode="owner"`，确保不会因 daemon 崩溃而永久僵死。
   - 若到达 `deadline = monotonic() + max_seconds`：停止 `monitor` 并以 `reason="timeout"` 退出。
3. **资源清理与退出契约**：
   - `finally` 中必调用 `monitor.stop()` 释放 `worker_lock` 并恢复信号处理器；
   - **退出码**：
     - `0`：`event` / `error_threshold_reached` / `no_active_jobs` / `monitor_disabled` / `timeout` / `interrupted`（均带 `ok: true` 与明确 `reason`，避免 Antigravity 后台任务唤醒误判命令崩溃）；
     - `1`：`monitor_exited` / `TOOL_NOT_ALLOWED_FOR_ROLE` / `OUTBOX_*` / `MonitorPersistenceError`；
     - `2`：`INVALID_ARGUMENTS` / `INVALID_WORKSPACE`。
   - **`--json` 输出结构**：
     ```json
     {
       "ok": true,
       "result": {
         "autonomy_phase": "P3_bounded_autonomy",
         "reason": "event",
         "mode": "owner",
         "daemon_detected": false,
         "waited_seconds": 142.6,
         "filters": {"server": "eshell111-vasp544", "job_name": "si-job", "phase": "scf"},
         "wake_events": [...],
         "pending_count": 1,
         "pending_events": [...],
         "active_jobs": [...],
         "error_jobs": [...]
       }
     }
     ```
     （`error_jobs` 列出达到 `max_consecutive_errors` 的活跃作业子集，方便 Maker 立刻定位故障作业。）

---

## 7. 子项 C 与集成套件同步

1. **`src/vasp_agent/control/service_contracts.py`**：`AUTONOMY_PHASE = 'P3_bounded_autonomy'`。
2. **集成套件（代码仓 `integrations/vasp-autonomy/`）与本地排除副本（`.agents/plugins/`）同步更新**：
   - `integrations/vasp-autonomy/SKILL.md` & `.agents/plugins/vasp-autonomy/skills/vasp-autonomy/SKILL.md`：
     - 步骤 4a 更新为 24 KB 上限、`stage_review_view` 阶段聚焦投影说明与 `raw_sha256` 深查指引；
     - 步骤 4c 首推 Maker 在 `vasp_submit_stage` 后以后台非守护命令启动 `vasp-ctl watch wait --server <server> --job-name <job> --phase <phase> --json` 并结束回合（0-token 等待，Reactive Wakeup 自动唤醒），保留 `/schedule` 作降级兜底；
   - `integrations/vasp-autonomy/agents/vasp-reviewer.md` & `.agents/plugins/vasp-reviewer/agents/vasp-reviewer.md`：
     - 更新 24 KB 溢出规则说明；明确 `stage_review_view` / `omitted_for_phase` 节点为按阶段折叠的历史证据，仅当怀疑历史漂移或需交叉核查更早阶段时按需调用 `vasp_read_evidence`，平时无需无故翻读全量原文；
   - `integrations/vasp-autonomy/README.md`：补充 `vasp-ctl watch wait` 命令参考；
   - `docs/design/EXTERNAL_AGENT_AUTONOMY.md`：实施完成后在对应章节补注已落地状态。

---

## 8. 留给实施阶段的细节（由 Builder 自行决定）

- `stage_review_view.py` 内部 helper 函数命名与浅拷贝实现细节（须确保绝不原地修改调用方传入的 `full_result` 字典）；
- `watch_wait.py` 的可读中文非 JSON 摘要排版；
- 单测中 `clock`、`poller`、`observer_interval`、`stop_event` 的依赖注入签名（用于亚秒级无真睡眠确定性单测）。

---

## 9. 测试与验收计划

按 `AGENTS.md` 分层验收规程，本轮本地定向必跑以下测试 + `tools/check.py`（默认不跑全量 `pytest`，推送后由 GitHub Actions 云端跑全量回归）：

1. **`tests/test_stage_review_view.py`（新建）**：
   - 覆盖 5 个阶段（`convergence` / `relaxation` / `scf` / `bands` / `dos`）的投影字段集合；
   - 验证反向漏斗顺序无 `same_as` 悬空指针、`inputs.environment` 全阶段保留、`dos` 保留 `scf-summary.json`、`bands`/`dos` 保留 `plan.incars['scf']`；
   - 验证投影后 `< MIN_CHARS` 与 `>= MIN_CHARS` 两种路径下 `stage_review_view.full_sha256` 均指向未投影完整原文，`model_view.full` 一致且 `sections` 可完整回读；
   - 验证未知未来阶段按 `depends_on` 向前兼容、不误删；
   - 实测完整 5 阶段真实样例投影后体积均远小于 24 KB（目标 ~4–9 KB）。
2. **`tests/test_control_service.py`（更新并扩展）**：
   - `AUTONOMY_PHASE == 'P3_bounded_autonomy'`；
   - `MAX_RESPONSE_BYTES == 24 * 1024`，验证溢出时 `sha256` 指向精简视图且 `raw_sha256` 指向完整原文、二者均可回读且已脱敏；
   - 控制面 `vasp_read_evidence(kind='evidence')` 按 8000 字符分页，同时断言交互侧 `Toolbox.read_context_evidence(sha, 0)` 仍为 3500 字符；
   - 现有 `test_read_evidence_log_enforces_12kb_cap_and_preserves_newest_tail`（24,946 B）保持稳定通过。
3. **`tests/test_watch_cli.py`（新建，Windows 本机真机锁测试）**：
   - 立即返回路径（已有 pending `terminal` / 无匹配活跃作业 / `/watch off` 全局或单任务禁用返回 `reason="monitor_disabled"`）；
   - 终态优先忽略单次瞬态错误并在后续 `terminal` 退出，以及同一作业已有更晚 `recovered` 时抑制旧 `error` 误唤醒；
   - **同错去重场景**：连续 3 次同类错误触发注册表 `error_count == 3` 唤醒（`reason="error_threshold_reached"`）；
   - **重入防死循环场景**：`owner` 模式重入时对已达阈值的作业执行一次实况探测（成功则归零并继续等待、仍失败则立刻退出）；`observer` 模式下旧 `error` 未 ack 立即唤醒、已 ack 则等 `error_count` 再次递增；
   - `--wake-on-error` 立即唤醒未恢复的单次错误；
   - `--poll-interval`（合法 60–600 / 越界 `exit 2`）验证首次加速轮询、出错仍按指数退避、且磁盘 `job-monitor.json` 中 `interval_seconds` 未被改写；
   - 外部进程/句柄持有真实 OS `worker_lock` 时进入 `observer` 模式（0 重复 `poller` 调用），锁释放后自动接管切回 `owner` 并完成唤醒；
   - `--max-seconds` 超时返回 `exit 0` + `reason="timeout"`；`--role checker` 拒绝（`exit 1`, `TOOL_NOT_ALLOWED_FOR_ROLE`）。
4. **配套回归**：
   - `tests/test_autonomy_guard.py`（补充 `vasp-ctl watch wait` 放行断言）；
   - `tests/test_model_views.py` 与 `tests/test_context_budget.py`（确认交互式 CLI 预算与全量回读零回归）；
   - `python tools/check.py --skip-pytest --expect-release 5b124e988c010ec8`（确认 `ruff` OK、`loc` 0 warnings、`release` 哈希零变更）。
