# 工作流模块设计：阶段定义、参数规范、指南与 DOS / Wannier / 声子

状态：步骤 A（阶段机制）、软件清单（§11.6）、步骤 B（参数规范流程与指南，§4.8、§5.1）、步骤 C（三维 DOS，§7.6）以及统一核对窗口与目录冲突递增（§4.9）均已集成至 `main`（工具组懒加载同步落地于 `chat/tool_groups.py`）。本文是上述模块演进的共同设计依据。

阅读顺序：§1 架构 → §2 阶段定义与版本 → §3 步骤 A 改动清单 → §4 参数规范（B）→ §5 指南加载（B）→ §6 预算 → §7 DOS（C）→ §8 Wannier → §9 声子 → §10 兼容与迁移 → §11 软件清单与环境配置。

---

## 1. 架构

```
本地对话 Agent（22 常驻核心工具 + 7 组 39 个按需展开工具）
  │  工具结果 = 数据；授权只来自操作者菜单
  ▼
Toolbox / RemoteCompute ──SSH stdin──▶ 远程 worker（每次调用现发的最新代码）
                                          │  task_release(meta)
                                          ▼
                              releases/<hash>/  任务绑定的引擎版本
                              stage_definitions · stage_gate · full_workflow · …
```

三层职责保持不变，阶段机制只是把"有哪些阶段、顺序如何、各阶段产出什么"从散落各处的硬编码收拢为数据：

| 层 | 读阶段信息的方式 | 原因 |
|---|---|---|
| 引擎（`workflow/stage_definitions.py`，进入 release） | 完整定义：依赖、检查项、执行方式、产物、完成条件 | 任务按准备时的引擎版本执行；定义随 release 冻结 |
| 远程 worker（`remote/worker_common.task_phases`） | 只读 `config.json` 里的阶段**名称与顺序**；需要产物清单时才从任务绑定的引擎加载定义 | worker 每次现发最新代码，却要服务绑定旧 release 的任务；旧任务没有记录，按旧四阶段处理，从不加载新模块 |
| 本地（`remote_compute.delivery_summary`、工具枚举、监控） | 本地 `stage_definitions` 注册表 | 交付检查与输入校验在本地完成 |

核心规则：**程序验收由代码执行**。定义里的文字（检查说明、完成条件）告诉审阅者和操作者代码检查了什么，从不替代检查本身；`record_stage_review` 仍按检查项名称校验，判定由定义里的 `accepts` 决定，而不是由模型阅读说明后自行决定。

## 2. 阶段定义与版本

### 2.1 定义结构（`stage_definitions._definition`）

每个 `(阶段名, 版本)` 一份声明式定义：

| 字段 | 含义 | 由谁执行 |
|---|---|---|
| `name`, `version` | 阶段名（`[a-z][a-z0-9_]{0,31}`）与定义版本 | — |
| `rules_version` | 审阅规则版本（检查说明、可接受判定） | — |
| `depends_on` | 必须在其之前启用并验收的阶段 | `validate()` 检查记录顺序 |
| `inputs` | 输入生成规则 `{rule, enforced_by, note}` | `enforced_by` 指向的代码 |
| `review.required_checks` | 检查项**名称**列表（与旧版逐字相同） | `stage_gate.record` 按名称校验 |
| `review.check_details` | 每项 `{description, accepts}`；`accepts` 是放行判定集合 | `stage_gate.record` 用 `accepts` 判定 |
| `execution` | `{mode, runner}`；`builtin` 为 `run_full` 内置，其他名字查 `full_workflow.STAGE_RUNNERS` | `run_full` / `run_stage` |
| `summaries`, `root_evidence` | 进入审阅证据的根目录摘要/文件（存在即哈希） | `stage_gate.verified_upstream` |
| `phase_outputs` | 运行开始后写在任务根目录的文件（释放/续算用来判断"是否已开始"并归档） | worker `phase_outputs` |
| `result_files`, `delivery` | 下载白名单增量；完整交付必须包含的文件 | worker `result_files`、本地 `delivery_summary` |
| `kept_inputs` | 阶段目录在启动前额外接收的输入（bands：`CHGCAR`），续算时保留 | worker `kept_stage_inputs` |
| `completion` | 完成条件 `{rule, enforced_by}` | `enforced_by` 指向的代码 |
| `parameter_spec` | 参数规范引用 `{'id', 'version'}`（`workflow/parameter_specs.py`）；`None` 表示参数来自扫描方案 | `prepare_full` 按规范重新校验（§4.8） |
| `guide` | 本地指南 id（`src/vasp_agent/guides/<id>.md`），审阅结果附其指针 | 只解释，不参与验收（§5.1） |
| `required_capabilities` | 运行该阶段的环境必须具备的能力标签，如 `('vasp>=6', 'vasp:wannier90')`；现有四阶段为空 | 准备时按 `stage_definitions.unmet` 与环境配置的能力匹配（§11.6），`prepare_full` 也据此拒绝 |

现有四个阶段的定义与旧代码中的常量逐项对应，测试 `test_legacy_tables_are_the_definitions` 断言二者一致：`RESULT_FILES`、`PHASE_OUTPUTS`、交付必需文件、证据摘要集合。

### 2.2 任务记录

新任务在 `prepare_full` 中写入：

```json
"stages": [{"name": "convergence", "version": 1}, {"name": "relaxation", "version": 1},
           {"name": "scf", "version": 1}, {"name": "bands", "version": 1}]
```

- 写在参数预览签名之后、`config.json` 写盘并进入 manifest 哈希之前。预览签名只覆盖默认值，所以已有参数方案的签名不受影响。
- 记录进入 `config.json` → manifest 哈希 → `input_review.context` → 每个阶段审阅快照。改动记录会改变快照，也会使 `core.verify` 失败。
- `validate()`：定义必须存在于本引擎；名称唯一；`depends_on` 必须在前；前三个必须依次为 convergence、relaxation、scf（`BASE_STAGES`，`run_full` 的执行顺序以它们为前缀）。
- **没有该字段的任务就是旧任务**：`entries()` 返回旧四阶段、版本 1；不写回、不迁移。

### 2.3 版本规则

| 变化 | 处理 |
|---|---|
| 检查说明措辞调整、可接受判定不变 | 新 `rules_version`，同时新增定义 `version`；旧版本定义保留在 `DEFINITIONS[name]` 中 |
| 检查项增删、判定集合、产物、完成条件变化 | 新定义 `version`；旧任务继续按记录的版本执行 |
| 记录格式变化 | `RECORD_SCHEMA` 加一，`validate` 同时接受旧格式 |
| 引擎首次支持阶段记录 | 标记 `STAGES_VERSION = 1`；worker 遇到有记录但绑定引擎缺模块的任务拒绝，不按旧流程猜测 |

`check_details` 与 `rules_version` 放在快照哈希**之外**、附在 `get_stage_review` 结果中：它们由定义版本唯一决定，而定义版本已经被快照绑定（新任务通过 `config.stages`，旧任务通过"无记录 = 版本 1"）。这样旧任务的快照与以前逐字节相同，已记录的审阅不失效。

### 2.4 兄弟阶段（bands 与未来的 DOS）

bands 和 DOS 都只依赖已验收的 SCF（`depends_on=('scf',)`），彼此无计算依赖。首版**顺序执行**，不做调度系统：

- 执行顺序 = 记录顺序；检查点 `completed` / `next` 按记录推进，最后一个启用阶段之后才是 `complete`。
- 证据：某阶段审阅绑定记录中**所有在它之前**的阶段（不只是依赖闭包），所以 DOS 排在 bands 后时，bands 的输出在 DOS 审阅后也不能改变。这比依赖闭包更保守；以后若要并行，用新的定义集版本改为依赖闭包。
- `run_full` 对 SCF 之后、非 bands 的目标阶段不再走 bands 分支（不会重做或改写 bands 报告），而是交给 `STAGE_RUNNERS` 中的执行实现；没有实现时拒绝启动，从不静默跳过。
- 能带分析（`analyze_full`）只在 bands 是最后一个启用阶段时把工作流标为 `complete` 并发 `full_workflow_completed`；否则写报告、发 `bands_analyzed`，由检查点进入下一阶段审阅。

## 3. 步骤 A 改动清单（"bands 永远是最后阶段"的全部位置）

| 位置 | 旧假设 | 现在 |
|---|---|---|
| `stage_gate.PHASES/CHECKS` | 固定四阶段与检查表 | 保留为旧四阶段的别名（取自定义）；所有判断改用 `task_stages(root)` |
| `stage_gate.current` | 无检查点时为 `convergence` | 任务首个启用阶段（旧任务仍是 convergence） |
| `stage_gate.check_stage / evidence / require_review / submission_record` | `PHASES.index` | 任务顺序；新增 `evidence_through(root, last)` 供派生任务 |
| `stage_gate.complete` | `bands` 之后为 `complete` | `stages.following()`：最后启用阶段之后才 `complete` |
| `stage_gate.record` | 硬编码 convergence/validation_plan 可 pending | 定义的 `accepts` |
| `stage_gate.context` | `CHECKS[phase]` | 定义的 `required_checks`，并附 `check_details`、`rules_version` |
| workflow-state `calculated` / `analyze_full` | 能带分析即完成 | 仅 bands 为最后阶段时标记 complete |
| `full_workflow.run_full` | bands 后结束 | SCF 后的其他阶段经 `run_stage` / `STAGE_RUNNERS`；未审阅任务只允许旧四阶段 |
| `remote_compute.delivery_summary` | 必需 `bands.*`、四阶段完成 | `stages.delivery_files(config)`；有记录的任务下载附带 `config.json` |
| `remote_compute.fetch` 白名单 | `RESULT_FILES` | `RESULT_FILES` ∪ 已知定义的结果文件 ∪ `config.json` |
| worker `RESULT_FILES` / `PHASE_OUTPUTS` | 固定 | 旧任务原样；有记录的任务取绑定引擎的定义 |
| worker `check_previous_job` | 固定前一阶段 | 任务顺序中的前一阶段 |
| worker 阶段校验（资源、提交、取消、续算、诊断、状态） | 四个名字 | `task_phases(folder)` |
| `worker_slurm.monitor_status` | `completed == 四阶段` 才算完成 | `completed == 任务启用阶段` |
| 续算 `prepare_phase_resume` | bands 保留 `CHGCAR` | 定义的 `kept_inputs` |
| 派生 `prepare_bands_path` | 复用 `index < bands` 的阶段 | 复用"直到 SCF"的阶段；有记录的来源，新任务记录裁剪为基础三阶段 + bands |
| 派生 `prepare_production` | 固定后续目录/回执 | 任务顺序中 convergence 之后的阶段 |
| 本地工具枚举、`/cancel`、监控身份校验、CLI `--phase` | 四个名字 | `stage_definitions.known_names()`（当前仍为这四个，提示词不变） |
| 能带分析入口 `analyze_band_results` | 无完成假设（只读下载的 `bands.csv`） | 不变 |

有意保留的阶段专属代码（不是"最后阶段"假设）：`core.execute` 对 `scf` 记录 CHGCAR 哈希；bands 审阅的 k 路径上下文与 SCF 占据审计；`remote_servers` 的只读元数据白名单（`scf`/`bands`/`dos`/`relax-*`）；提交恢复只支持初始 convergence；旧版 `bands-only` 工作流。

## 4. 参数规范流程（步骤 B）

目标：`optional_stages=["dos"]` 只负责**选择**阶段，具体参数走统一流程：

> 读取阶段参数规范 → 展示建议及依据 → 操作者选择 → 校验并保存 → 生成实际输入。

1. **规范**：定义的 `parameter_spec = {'id': 'dos', 'version': 1}` 指向引擎内一份规范：每个参数的类型、单位、合法范围、默认值的**推导规则**（不是常数）及依据。工具描述保持一句话；完整规范只在工具结果中返回。
2. **建议**：引擎按规范从已验收证据推导建议值并给出依据（例：DOS 网格由 SCF 网格按倒空间间距加密；NBANDS 由 SCF 占据审计和目标能窗推导）。建议不是收敛证明，明确写出。
3. **选择**：复用现有参数方案的菜单路径（`propose_scan_plan` / `parameter_plans.py` 的"默认 vs AI 建议 → 操作者选择"）。若现有入口无法表达，新增一个通用工具（暂名 `propose_stage_parameters`），供 DOS、Wannier、声子共用；不为每种计算新增一套提交/查询工具。
4. **校验**：执行端严格校验——未知键拒绝；只接受规范中的类型和范围；不接受文件路径、Shell 命令或自由文本配置；与预览签名绑定（同 `parameter_choice.preview_signature`）。
5. **保存**：写入 `config.json`（按阶段分键，如 `stage_parameters.dos`）并进入 manifest 哈希和审阅快照；生成的 INCAR/KPOINTS 进入 `input-plan.json`，`full_workflow.stage` 照旧逐项比对。
6. **新任务的阶段选择**：`prepare_full(optional_stages=…)` → `stages.record(BASE + ('bands',) + optional)`；worker 在部署引擎不支持所请求阶段时拒绝，不静默去掉。扫描重试（`prepare_retry`）沿用来源任务的阶段选择，需在步骤 B 中显式传递（步骤 A 中新任务一律为旧四阶段）。
7. **派生**：从 `band_path.prepare_bands_path` 抽出公共的"SCF 证据复用"（结构、赝势、配置、CHGCAR 哈希、可重现性检查），供"已通过 SCF 的任务派生只做 DOS 的新目录"使用；新任务独立审阅和授权，不修改来源任务。

### 4.8 实现（步骤 B）

| 部分 | 位置 | 要点 |
|---|---|---|
| 规范 | 引擎模块 `workflow/parameter_specs.py`（进入 `ENGINE_MODULES`，release 名称随之改变；标记 `PARAMETER_SPECS_VERSION = 1`） | `SPECS[id][version]`：`fields`（`integer`/`number`/`choice`，单位、范围、选项、可空）、`suggest(context)` 返回建议值与依据、`check`（字段关联）、`generate`（生成实际输入）、`generate_at`（`prepare` 或 `stage_start`）、`fixed`、`derived`、`dependencies`、`executable`。`describe()` 是工具结果中返回的完整规范 |
| 校验 | `parameter_specs.validate` | 未知字段拒绝；数值不接受布尔、文本、NaN；字符串只能是列出的选项，因此路径、命令和自由文本都进不来；`partial=True` 用于 AI 部分修改，合并建议值后再做完整校验与关联检查 |
| 选择阶段 | `propose_scan_plan` / `preview_scan_defaults` 的 `optional_stages`（枚举 = 有规范的阶段，现为 `dos`） | 只负责选择；`stages.with_optional` 生成记录 `BASE + bands + optional`，没有定义的阶段拒绝 |
| 建议与 AI 修改 | 引擎预览 `stage_specs[阶段]`（签名之后附加，签名不变）；`stage_parameters=[{stage, field, value}]` | 建议来自规范的推导规则并附依据；AI 修改须说明理由，按规范校验后才进方案 |
| 操作者选择 | `parameter_plans.select_stage_parameters`（`/plan` 菜单） | 每个阶段一张表：规范建议 / AI 建议 / 逐项修改（`字段=值`）/ 暂不决定。即使无人修改也必须由操作者选择；非交互时方案 `pending`，摘要 `stage_parameters.<阶段>.status = operator_confirmation_required`，`prepare_scan_plan` 拒绝 |
| 保存 | `parameter-choice.json` 的 `stage_parameters`、`stage_parameter_choices`；`config['stage_parameters'][阶段] = {spec, values}` | 写在预览签名之后、manifest 哈希之前；`prepare_full` 要求与已确认方案逐项相同 |
| 执行端校验 | `full_workflow.recorded_stage_parameters`、`parameter_specs.check_record` | 引擎重新校验：规范存在且版本与阶段定义引用一致、可执行、值合法、恰好覆盖启用的参数化阶段。worker 在引擎缺标记时拒绝，不丢弃阶段 |
| 生成输入 | `input_review.prepare_plan` → `input-plan.json` 的 `stage_parameters` | `generate_at='prepare'` 的规范在准备时生成并记录 `generated`；`stage_start`（DOS：需要 SCF 的 E_F 与网格）记录选定值与规则，阶段启动时生成并与方案比对（步骤 C） |
| 审阅 | `stage_gate.context` | 当前阶段有参数时，`stage_parameters`（选定值与生成内容）进入快照；所有阶段的快照都经 `config.json` 覆盖这些值 |
| 派生 | `prepare_retry` 沿用来源的可选阶段与参数（再校验）；`prepare_bands_path` 裁剪阶段时去掉其参数 | |

DOS 规范 `dos` v1 已写好（字段 `kmesh_factor`、`integration`、`sigma_eV`、`energy_reference`、`window_min_eV`、`window_max_eV`、`sampling_interval_eV`、`empty_band_margin_eV`；固定 `ICHARG=11`、`LORBIT=11`、`LWAVE=.FALSE.`），`generate` 与 `nbands_check` 为纯函数并有测试，供步骤 C 使用。规范标记 `executable: False`，且没有 `dos` 阶段定义：`preview_scan_defaults(optional_stages=["dos"])` 可以查看规范，`propose_scan_plan` 返回 `STAGE_NOT_EXECUTABLE` 且不保存，`prepare_full` 同样拒绝。步骤 C 注册 `DEFINITIONS['dos']`（`parameter_spec={'id': 'dos', 'version': 1}`、`guide='dos'`）并把 `executable` 改为 `True`（实际落地为 `DOS_V2`，见 §7.6）。完整流程用测试专用的合成阶段 `probe` 覆盖（`tests/test_stage_parameters.py`）。

### 4.9 统一核对窗口与安全目录递增（已落地至 `main`）

为消除“参数确认建目录 → 审阅 → 改核数致快照返工”的双重菜单摩擦及同名派生目录冲突，在严格保持授权与审阅底线不变的前提下，统一核对体系分为公共底座、派生/重扫入口与新任务 `/plan` 三合一入口：

| 部分 | 位置 | 要点 |
|---|---|---|
| 公共底座 | `permissions/unified_confirm.py`、`ui/menu_text.py` | 复用 `resource_menu` 与 `submission_dialog` 的校验、已保存服务器默认资源读取与 `KPAR`/`NCORE` 预览；建目录后立即落盘确认核数并刷新签名，避免重审返工；远端写入核数遇通信中断时登记 `operation_id` 且**不授予提交权**，供只读对账 |
| 目录冲突安全递增 | `chat/fork_naming.py` | 派生前（本地 `runs/` 或远端 `list_jobs`）及准备拒收时检测同名目录，**绝不覆盖旧目录**；向操作者提示并按 `<base>_2`、`<base>_3`…（总长 `<= 64` 且匹配 `JOB` 正则）计算新名，经操作者确认后创建 |
| 派生 / 重扫一次确认 | `chat/dos_fork_menu.py`（`dos`）、`chat/band_path_menu.py`（`bands`）、`chat/toolbox_workflow.py` `prepare_scan_retry`（`convergence`，支持 `allocation`/`array`） | 同一菜单流程整合【计算参数区】与【提交资源区】，提供 `1` 确认并在审阅通过后直接提交本阶段、`2` 仅准备、`3/4` 修改参数或资源、`q` 取消；DOS 与能带路径派生接入 `_2`/`_3` 安全递增 |
| 新任务 `/plan` 三合一启动核对 | `planning/plan_startup.py`、`chat/cli.py`、`chat/toolbox_core.py` | 交互会话由 `cli.py` 开启 `unified_plan_confirm=True`，在单次 `/plan` 菜单分区确认：区块 A（物理模型与研究目标）、区块 B（扫描/可选阶段/环境参数）、区块 C（首阶段 `convergence` 资源与单次预授权）。选 `1` 仅在进程内存 `box.pending_plan_preauth` 暂存预授权，`prepare_scan_plan` 后由 `finalize_prepared()` 写入 `physics-model.json`（`confirmed_by='operator_chat_menu'`）、应用确认核数并同步刷新 `preparation_summary`，仅授 `(server, job_name, 'convergence')` 1 次提交权；`revoke_submission()` 同步清空 `pending_plan_preauth`。既有 `request_physics_confirmation` 与 `request_submission` 完整保留 |

**安全不变量**：非交互模式（`plan_reader is None`）仍返回 `operator_confirmation_required`，模型不可代选；预授权严格绑定单个 `(server, job_name, phase)` 且仅 1 次，提交消费后自动归零，**绝不跨阶段连授**；落盘后仍必须完整执行 `get_stage_review` → `record_stage_review(allowed=true)` 与远端 `submit_checked` 预检，任一审阅项 `blocked` 或预检未过时硬阻断。

## 5. 指南加载规则（步骤 B）

固定提示词只保留通用行为、权限和证据规则；DOS、Wannier 等专属说明放入独立、带版本的指南（`docs/guides/stages/<id>.v<N>.md` 随引擎发布）。

| 规则 | 实现要求 |
|---|---|
| 追加而非修改 | 指南作为工具结果追加到对话；系统提示和工具目录不随阶段变化（测试：多阶段对话中 `context_instructions()` 与 `model_schemas()` 字节不变） |
| 首选随结果附带 | `get_stage_review` / 参数预览结果中附 `guide: {id, version, sha256, summary}`；需要主动查阅时再加一个通用读取工具（`read_stage_guide(id, page)`） |
| 同版本去重 | 会话记录已加载的 `(id, version, sha256)`；重复请求返回"已在第 N 条加载"的引用，不重复注入 |
| 分页 | 每页不超过指南页预算；超长指南先给摘要，再按页读取 |
| 压缩后保留引用 | `context_compaction` 把指南正文替换为 `{id, version, sha256}` 引用桩，模型需要时重新读取；引用不能被摘要改写 |
| 不扩大权限 | 指南是数据：不授权提交、不放宽阈值、不覆盖用户要求；工具结果统一标注 `content_is_untrusted` 语义 |
| 程序验收 | 指南里的任何条件都必须有对应代码检查；指南只解释 |
| 占位指南 | Wannier、声子指南首行写明"尚未实现，不能执行"，读取工具返回 `implemented: false` |

### 5.1 实现（步骤 B）

- 文件：`src/vasp_agent/guides/<id>.md`（随本地程序发布，`pyproject` package-data），头部 `id`、`version`、`implemented`、`title`、`summary`。现有 `dos` v1（说明 DOS 在步骤 C 实现）与 `wannier` v1（首行“尚未实现，不能执行”，含 §8.1 的集群事实）。
- 指针：`get_stage_review`（阶段定义声明 `guide` 时）、`preview_scan_defaults` 与 `propose_scan_plan`（可选阶段的规范声明 `guide` 时）的结果附 `guides: [{id, version, sha256, implemented, title, pages, read, authority, summary}]`，在快照之外。同一版本的摘要在实时上下文中只出现一次，之后为 `summary_loaded: true`。
- 读取：新增一个通用工具 `read_stage_guide(guide, page)`。理由：分页和压缩后重读需要主动入口，`read_context_evidence` 是历史原文读取，不适合承担去重与上限。结果 `guide_page{id, version, sha256, page, pages, tokens}` + `text` + `authority`（数据、不授权、验收由代码判定）。
- 去重与上限：按“实时上下文”（压缩截断之后的历史，`Agent.live_history`）计算已加载内容。同版本同页再次请求返回 `already_loaded` 与原 `call_id`；页上限 `PAGE_TOKENS = 1500`（按 `##` 小节分页，过长再按段落、行、字符切分）；会话注入上限 `INJECTION_TOKENS = 6000`（页正文与指针摘要合计），超出返回 `GUIDE_BUDGET_EXCEEDED` 并提示 `/compact` 后重读。估算用 `ContextBudget().estimate`（未校准，各会话分页一致）。
- 压缩：`ContextCompactor.references = guides.references`，在摘要之后追加程序生成的引用（`dos v1 sha256=…（已读第 1 页）`），原生压缩则作为单独一条消息；摘要无法改写引用。被压缩的正文不再计入上限，可以重新读取。`project()` 截短旧输出时保留 `guides` 指针；`model_views.KEEP` 含 `guides`、`guide_page`、`guide_ref`、`stage_parameters`。
- 稳定性：系统提示与工具目录不随阶段或指南变化（`tests/test_stage_guides.py` 在多步对话中逐次比对 instructions 与 tools）。

## 6. 提示预算

| 项目 | 步骤 A 前 | 步骤 A 后 | 软件清单后 | 步骤 B 后 | 步骤 C + 懒加载后 | 统一核对后（当前） |
|---|---|---|---|---|---|---|
| 固定提示（说明 + 工具目录，`ContextBudget.estimate`） | 14,218 | 14,218 | 14,299 | 14,599 | 8,418（`lazy_tool_groups=True`） | 8,626（`lazy_tool_groups=True`） |
| `PROMPT_TOKEN_BUDGET` | 14,300 | 14,300 | 14,800 | 14,800 | 14,800 | 14,800（懒加载核心上限 9,500） |
| 模型可见工具数（生产对话模式） | 57 | 57 | 58 | 59 | 22 常驻 + 7 组 39 工具按需展开 | 22 常驻 + 7 组 39 工具按需展开 |
| `TOOLS` 总数 | 62 | 62 | 63 | 64 | 66 | 66 |

步骤 B 的 +300：`read_stage_guide`（约 +100）、`optional_stages`（预览与方案各一处）、`stage_parameters`（方案）；INSTRUCTIONS 未增加任何工作流规则。

步骤 C 叠加应用层工具组懒加载后：`dos` 进入各工具的阶段枚举、`request_dos_fork` 挂入 `convergence_fork` 懒加载组、`replot_bands` 描述扩展至 `bands.csv/dos.csv`；INSTRUCTIONS 未增加任何 DOS 规则（规则在 `check_details`、`dos_plan.program_checks` 与指南 `dos` v2 中），初始固定提示实测 8,418 tokens（距 14,800 预算留有 6,382 tokens 余量）。

统一核对落地后：仅在 `chat/prompts.py` 增补一条精简流转提示（返回已含操作者确认的物理目标与本阶段预授权时，无需重复弹 `request_physics_confirmation` / `request_submission`），并在 `chat/model_views.py` 的 `KEEP` 中加入 `physics_confirmed`、`submission_preauthorized`（不影响固定提示）；生产懒加载模式固定提示实测 **8,626 tokens**（距 14,800 预算余量 6,174 tokens，并满足 `tests/test_prompt_budget.py` 的 9,500 懒加载核心上限）。

方案：

- **固定预算**：14,300 → 14,800（2026-10-15 操作者决定；软件清单落地后实测 14,299，安全压缩已用尽）。这 500 token 只留给步骤 B/C 的少数通用入口，不给各工作流的专属规则。新增通用入口（步骤 B 的参数规范/指南工具、步骤 C 的 `dos` 阶段枚举）必须通过合并重复描述等方式抵消；`model_views.KEEP` 的新增字段只影响工具结果，不影响固定提示。
- **指南独立预算**：单个指南页上限（建议 1,500 tokens）与每会话指南注入总上限（建议 6,000 tokens），用同一 `ContextBudget` 估算；超限时只给引用和摘要。
- **注入上限**另计入每次请求的剩余上下文预算，而不是挤占固定提示预算。
- **缓存**：操作者的中转站实测缓存读取为 0%。设计不依赖、也不声称缓存命中；"前缀稳定"只是让缓存在支持的通道上*可能*生效。成本按每次请求全额输入计算，这正是注入上限必须存在的原因。

## 7. DOS（步骤 C，首版仅三维）

### 7.1 阶段

`dos` 定义：`depends_on=('scf',)`，`required_capabilities=()`（现有 5.4.4 即可），`execution.runner='dos'`；固定 SCF 电荷密度的非自洽计算（`ICHARG=11`、`LORBIT=11`，`LWAVE=.FALSE.`）。两种入口：新任务启用可选 DOS；已通过 SCF 的任务派生只做 DOS 的新目录（§4.7）。二维 DOS 另行扩展（真空轴网格、展宽选择和派生兼容测试补齐后再开放），首版对 2D 任务拒绝。

### 7.2 执行前必须明确的参数

| 项目 | 规则 |
|---|---|
| k 网格 | 按 SCF 倒空间间距生成加密建议（如每方向 ×2 并向上取整）；加密倍数是初始策略，不等于 DOS 已收敛——报告中写明 |
| 积分方法 | 三维且网格满足条件时用四面体法 `ISMEAR=-5`（Blöchl 修正）。前置条件：KPOINTS 为自动生成的规则网格（Γ 中心或 MP，不能是显式列表或线模式）；不可约 k 点数 ≥ 4（VASP 对 NKPT<4 的四面体法报错；运行前用下界估计，运行后读 vasprun 的实际 k 点数复核）。不满足时**阻断**并给出替代方案（加密网格，或由操作者明确选择 Gaussian 并确认 SIGMA），**不静默改 Gaussian** |
| 能窗 | 明确参考能量、范围与用途，可由操作者调整。VASP 5.4.4 的 `EMIN`/`EMAX` 是**绝对能量**，引擎按 SCF 费米能换算并把换算记录写入输入方案 |
| NEDOS | 由目标**采样间隔**计算：`NEDOS = ceil((EMAX−EMIN)/Δ) + 1`。0.01 eV 是采样间隔，不是分辨率；实际分辨率受 k 网格与积分方法限制，报告不得把采样间隔称为分辨率 |
| NBANDS | 检查空带是否覆盖目标窗口上限：运行前用 SCF 占据审计中最高能带的最小值（相对 E_F）估计，运行后要求 DOS 计算中每个 k 点最高能带都高于 `E_F + 窗口上限`（留余量）；不足则验收失败并给出所需 NBANDS。不能照抄 bands 的值；注意 VASP 会把 NBANDS 向上取整到并行带组的倍数（见 execution.json `band_groups`） |

### 7.3 能量参考：零点取 SCF 费米能，同时记录 DOS 计算的费米能

建议：DOS 曲线与合图的零点取 **SCF 的 E_F**，并在报告中记录 DOS 计算自身的 E_F 及二者差值。理由：

1. 电荷密度固定为 SCF 结果，SCF 的 E_F 是唯一自洽得到的参考；`ICHARG=11` 计算重新求出的 E_F 只是在不同网格、不同占据方法（四面体 vs Gaussian）下对非自洽本征值的重新积分。
2. bands 已经以 SCF E_F 为参考（`bands.csv` 的 `energy_minus_scf_ef_eV`），同一参考才能让能带与 DOS 合图对齐，且这个参考有记录。
3. 两者不要求严格相等：金属中差值反映 k 网格/方法差异，本身是有用的诊断；超过记录阈值时**标记**而不判失败。绝缘体中 E_F 在带隙中的位置两种计算都是任意的，所以同时记录 VBM，图中可标出。

### 7.4 运行后程序检查

- 输出完整、数值有限；实际 INCAR/KPOINTS 与审阅方案一致（`core.read_result` 已逐项比对 INCAR）；实际 k 点数、ISMEAR 与方案一致。
- **电子数核验**：有两条互相独立的口径，都记录积分范围、占据方式与归一化：
  - 占据数求和：`Σ_k w_k Σ_n f_nk ×(2/ISPIN)`（vasprun），与 NELECT 比较，不依赖能窗；
  - DOS 积分：从**所有价电子态之下**积分到 DOS 计算的 E_F。积分下限必须低于最低能带（含半芯态——如 Cu_pv 的 3p、La 的 5s/5p，这些都计入 POTCAR ZVAL），否则积分必然少于 NELECT。因此计数用的下限取 SCF 最低本征值减余量，与绘图能窗分开；若单次 VASP 运行的 EMIN 需要同时覆盖两者，用更大的 NEDOS 保持采样间隔。ISPIN=1 时 DOSCAR 总 DOS 已含自旋简并因子 2。
- 费米能变化有记录；不要求不同网格、不同占据方法下严格相等。
- 投影 DOS：说明单位（states/eV/cell，按需换算每化学式）和投影球半径来源（POTCAR RWIGS）；投影只覆盖原子球，**不要求**投影之和等于总 DOS（球外间隙与球重叠）。
- 能带与 DOS 合图使用统一且有记录的能量参考（§7.3）。

输出：总 DOS、元素/轨道投影数据（CSV）、独立图和可选合图；`dos` 定义的 `delivery` 列出这些文件，完成状态与交付按任务启用阶段判断（步骤 A 已提供）。

### 7.5 测试（步骤 C）

DOS 失败与续算、重复提交防护、SCF 证据被修改、空带不足、输出缺失、四面体前置条件不满足时阻断、指南重复加载、旧任务检查点与审阅快照兼容（沿用 `tests/data/legacy_stage_walk.json`）。

### 7.6 实现（步骤 C，已移植集成至 `main`）

| 部分 | 位置 | 要点 |
|---|---|---|
| 规范 | `parameter_specs.DOS_V2`（`executable: True`）；v1 保留为“已由 v2 取代”，从未可执行，也没有任务记录过它 | 新增字段 `nbands`（null = SCF NBANDS）；`generate` 需要 `band_minima_eV`（SCF 各带在全部 k 点的最小值，相对 E_F）与 `lowest_eigenvalue_eV`；EMIN = SCF E_F + min(窗口下限, 最低本征值 − 2 eV（Gaussian 再加 6σ）)，NEDOS 按采样间隔覆盖全范围（≤ 20001），记录 `grid_spacing_eV`。阻断原因都写明替代方案 |
| 阶段定义 | `stage_definitions.DEFINITIONS['dos'][1]` | `depends_on=('scf',)`、`parameter_spec={'id':'dos','version':2}`、`guide='dos'`、runner `dos`、`kept_inputs=('CHGCAR',)`；检查项 physics、scf、inputs、integration、window（只接受 pass）；`phase_outputs`/`result_files`/`delivery` 见定义。顺序 = 记录顺序：新任务 BASE + bands + dos |
| 引擎 | 新引擎模块 `workflow/dos_stage.py`（`DOS_VERSION = 1`，另有 `full_workflow.DOS_VERSION = 1`）、`workflow/dos_plot.py`、`workflow/scf_reuse.py`（均进入 `ENGINE_MODULES`，release 名称随之改变） | `scf_evidence` 只读已校验文件（vasprun 与 CHGCAR 对完成记录、scf-summary、scf/KPOINTS 网格一致、带数一致）；`plan` → `build` 生成 INCAR（`base_incar` + 规范输出，四面体去掉 SIGMA）与 KPOINTS 及四项 `program_checks`；`run`（`STAGE_RUNNERS['dos']`）重新生成，阻断即拒绝，写 `dos-plan.json`，经 `full_workflow.stage(..., reviewed=(incar, kpoints))` 运行（只允许方案未登记的阶段使用；登记过的阶段不能另行指定） |
| 审阅 | `stage_gate.context` 在 dos 阶段附 `dos_plan`（快照内）；`stage_gate.record` 在 `program_checks` 有未通过或 `blocked` 非空时拒绝放行（可记录 blocked） | 新任务的参数不能原地改；`dos_plan.next` 指向 `request_dos_fork` |
| 运行后检查 | `dos_stage.check_outputs` | outputs、parameters、electron_count（容差 `ELECTRON_COUNT_TOLERANCE = 0.05` e/晶胞；下限须低于 DOS 计算最低本征值 ≥ `MIN_COUNT_CLEARANCE_EV = 0.5`；ISPIN=1 不再乘 2；占据数求和与梯形积分仅作参考）、empty_bands、fermi_shift（> 0.1 eV 标记）、projections（说明单位与归一化，不要求和等于总 DOS）。失败写 `dos-report.json`（status failed）并抛错，阶段不完成 |
| 输出 | `dos_stage.export`、`dos_plot` | `dos.csv`（相对 SCF E_F 的能量、绝对能量、总/积分 DOS、元素×l 投影，末两列 SCF 与 DOS 计算 E_F）、`dos.png/pdf`、有 bands.csv 时 `bands-dos.png/pdf`（统一零点 SCF E_F，`COMBINED_NOTE`）、`dos-report.json`，`report.json` 增加 `dos` 字段（bands 写的字段不变）。`remote_compute.delivery_summary` 对启用 dos 的任务要求 `report.json.dos.status == 'passed'` |
| 续算 | worker `worker_lifecycle.py`（`phase_stage_folders`、`generated_at_start`） | 阶段启动时生成输入的阶段没有 `input-plan.json` 的 INCAR 条目：其目录即阶段名；复用核对按 `inputs.json`，INCAR 由引擎在运行时逐字节比对 |
| 派生 | `scf_reuse.verified_source`（`band_path_fork.prepare_bands_path` 改用它）；`dos_stage.fork_preview`、`prepare_dos_fork`；worker `worker_prepare.py` 动作 `dos_fork_preview`、`prepare_dos_fork`（引擎缺 `DOS_VERSION` 即拒绝）；模型工具 `request_dos_fork`（归属 `convergence_fork` 懒加载组）→ `chat/dos_fork_menu.py` | 共用检查：来源空闲、manifest（结构、POTCAR、config、方案）、物理确认、SCF 已验收、`evidence_through('scf')`（含 CHGCAR 对 `charge_sha256`）、检查点证据、可重现性、传入环境时可执行文件指纹一致。派生记录 = 来源的基础三阶段 + dos，`stage_parameters` 只含 dos，启用执行标签；来源不改动；`dos-fork.json` 收据。菜单已接入统一核对底座与 `fork_naming`（支持一次确认参数+核数+单阶段 `dos` 预授权及同名目录 `_2`/`_3` 安全递增），只由操作者选择，引擎拒绝的取值不建目录并再问 |
| 2D | `dos_stage.refuse_2d`、规范 `check` | 准备、预览、派生对二维 slab 一律拒绝（“后续扩展”） |
| 本地重绘 | `dos_plot.replot_folder`；`replot_bands` 工具与 `/plot replot` | 按下载收据核对 dos.csv/bands.csv/报告，写 `dos.replot.*`、`bands-dos.replot.*` 与记录，不改原图 |

使用说明：`docs/guides/DOS.md`（含 La₂CuO₄ 现有任务的派生步骤；其 SCF 在 VASP 5.4.4 上算，DOS 不需要 VASP 6）。

## 8. Wannier（接口设计，未实现）

已有高对称路径能带不能替代 Wannier 的准备。阶段结构：`wannier` 依赖 `scf`，`required_capabilities=('vasp>=6', 'vasp:wannier90')`；非自洽计算在**完整均匀网格**上写出 Wannier90 输入，并以库模式在 VASP 内运行 Wannier90。

### 8.1 集群现状（eshell111-vasp544，只读探测结果，由交接会话提供）

| VASP | 位置 | Wannier90 |
|---|---|---|
| 5.4.4（Agent 当前环境 `environments/vasp544-public.sh`） | 公共构建 | **无**：没有 `wannier_setup`/`w90_` 符号，二进制含 "VASP was compiled without wannier90 library"。不能做 Wannier |
| 6.4.3（用户自建，`apprepo/vasp/6.4.3-ioptcell_intelmpi2017_hdf5_libxc`，有 `scripts/env.sh`） | `-DVASP2WANNIER90`，静态链接 Wannier90 库（`w90_comms_*` 符号） | 有 |
| 6.6.0（用户自建，`apprepo/vasp/6.6.0-fixz_ext_oneapi2024.0.1`，有 `scripts/env.sh`） | `-DVASP2WANNIER90 -DVASP2WANNIER90v2`，package_summary 写 "Wannier90/v2"；另带 FixZ、VASPsol、VTST 补丁 | 有 |

PATH 和已加载模块中都没有独立的 `wannier90.x` / `postw90.x`。VASP 6 内的库模式（`LWANNIER90_RUN`）可用；若要在不重跑 VASP 的情况下反复调整投影和能窗，需要操作者安装与所链接版本匹配的独立 Wannier90。

一般规则（不随集群变化）：`LWANNIER90=.TRUE.` 需要编译时 `-DVASP2WANNIER90` 并链接 Wannier90 库；VASP 5.4.4 的 `-DVASP2WANNIER90` 对应 Wannier90 1.2 接口，2.x 需要 `-DVASP2WANNIER90v2`。能力以二进制探测为准（§11），探测不到就阻断。

### 8.2 设计含义

- **Wannier 需要 VASP 6 环境配置**（§11）；当前 5.4.4 配置不具备 `vasp:wannier90`，准备时即拒绝，不在提交后才失败。
- **约束补丁标记**：两套 VASP 6 的路径分别含 `ioptcell`、`fixz`，`remote_worker.inspect_environment` 对 `relax_mode=cell` 任务会因这些标记拒绝。Wannier 任务若不做晶胞优化可不受影响，但配置的能力标签必须如实记录补丁（如 `patch:fixz`），由操作者确认其不影响非自洽计算。
- **跨版本证据复用**：现有规则在 `executable_sha256` 不同时拒绝复用，所以 5.4.4 的 SCF/CHGCAR 不能直接喂给 VASP 6 的 Wannier 阶段。

### 8.3 待操作者决定

1. **跨版本路线**。两种选择：
   - (a) 用 VASP 6 新建任务，以已验收的优化结构为起点，重算 SCF（以及用于比较的 bands），再做 Wannier；
   - (b) 显式、经审阅的跨版本几何复用：只复用 `relaxed-2.POSCAR`（哈希与来源可执行文件指纹写入记录），不复用电荷密度。
   **建议 (a)，并在其中采用 (b) 的几何记录方式**：几何是与可执行文件无关的数据，可以经审阅复用；电荷密度和本征值与编译、补丁和数值库相关，SCF 重算成本低，重算后同一可执行文件的证据链完整。新任务先在 VASP 6 下做一次与生产设置相同的静态力/应力复核，确认结构在新版本下仍满足阈值；不满足则阻断，不在新版本下静默重新优化。
2. **用 6.4.3 还是 6.6.0**。6.6.0 带 FixZ/VASPsol/VTST 补丁；补丁是否改变非自洽或库模式行为需操作者判断。建议优先选补丁较少、满足能力的构建，由操作者确认。
3. **是否安装独立 Wannier90**（版本须与所链接库一致），用于不重跑 VASP 的投影/能窗迭代。
4. **插值验证的参考能带**：必须与 Wannier 使用**同一可执行文件**计算的 DFT 能带比较（即路线 (a) 在 VASP 6 下重算的 bands），不与 5.4.4 的 bands 比较，否则差异无法归因。

### 8.4 输入与验收

| 项目 | 要求 |
|---|---|
| k 网格 | Γ 包含的均匀 MP 网格，覆盖整个布里渊区；关闭对称约化（`ISYM=0` 或 `-1`），使 VASP 写出的 k 点与 `.win` 中的 `mp_grid`/`kpoints` 一致；程序比对二者 |
| 投影 | 操作者在参数规范中选择（如 Cu d、O p）；`num_wann` 与投影数一致；程序校验元素与轨道存在 |
| 能窗 | `dis_win_min/max`（外窗）与 `dis_froz_min/max`（冻结窗），以 SCF E_F 为参考换算；NBANDS 覆盖外窗上限（同 §7.2 的检查） |
| 输出 | `write_hr = .true.` → `seedname_hr.dat`；`.wout` 中的扩展度收敛、解纠缠收敛 |
| 插值验证 | 沿已验收的 bands 路径做 Wannier 插值，与同一可执行文件的 DFT 能带比较；冻结窗内最大偏差超过阈值则不通过；报告偏差分布 |

占位指南明确"尚未实现"，读取结果 `implemented: false`。

## 9. 声子（接口设计，未实现）

能力需求示例：`required_capabilities=('tool:phonopy',)`（位移生成与力常数汇总；集群上是否可用由 §11 的探测确认）。结构："位移生成 → 多个力计算 → 汇总验收"，复用数组作业能力。

| 阶段 | 内容与验收 |
|---|---|
| `phonon_displacements` | 在已验收的优化结构上建超胞并生成对称不等价位移。超胞：各方向长度下限（如 ≥ 10 Å）由操作者确认，记录变换矩阵；位移幅度（默认 0.01 Å）在规范范围内；位移数受数组上限约束（现有 `MAX_ARRAY_TESTS=7` 只适用于收敛扫描，声子需独立的数组类型与上限） |
| `phonon_forces` | 每个位移一个数组子任务（`run-array-test` 同类的受控启动），输入哈希与位移清单绑定；`IBRION=-1`、`NSW=0`、与生产相同的 ENCUT/PREC/LREAL，`EDIFF` 收紧（如 1e-8）；每个元素单独验收力数组有限且完整 |
| `phonon_analysis` | 汇总力常数：所有位移齐全且哈希一致；残余力漂移（力之和）低于阈值；声学求和规则检查；Γ 点声学支频率接近 0；虚频**报告并标记**（可能是真实不稳定或超胞/精度不足），不自动判为成功或失败 |

首版不做 DFPT。精度、超胞与位移幅度分别验证，任何一项不满足都阻断而不是放宽。

## 10. 兼容与迁移

- **旧任务**（`config.json` 无 `stages`）：继续按旧四阶段执行；不写入新字段；审阅快照、检查点与 workflow-state 与改动前逐字节相同——`tests/data/legacy_stage_walk.json` 是在未修改的引擎（530835e）上采集的，`test_legacy_task_reviews_and_checkpoints_match_the_pre_change_engine` 每次比对。
- **绑定旧 release 的远程任务**：worker 只从 `config.json` 读名称（没有记录就是旧四阶段），旧任务从不加载 `stage_definitions`；`status`、`fetch`、监控、取消/释放/续算在 release 不含该模块时照旧工作（测试用打包的 stdin 程序在没有任何引擎的情况下 fetch 旧任务）。
- **有记录但引擎缺模块的任务**：拒绝，不按旧流程猜测（只可能由手工改动造成）。
- **release 哈希**：新增引擎模块 `stage_definitions.py`，部署后 release 名称改变；只有部署后新准备的任务带阶段记录。已有任务（包括 La₂CuO₄）保持绑定原 release，不受影响。
- **本地任务**：同一规则；本地运行用当前代码，旧任务无记录即旧行为。
- **派生任务**：生产续算复制来源配置（有记录则保留）；换路径派生对有记录的来源裁剪为基础三阶段 + bands，旧来源保持无记录；扫描重试走 `prepare_full`，得到当前记录。
- **工具枚举**：`PHASES` 取自 `known_names()`，步骤 A 时为四个名字（固定提示不变），步骤 C 已注册 `dos` 共五项（见 §6 预算对比）。

## 11. 软件清单与环境配置（已实现，见 §11.6）

阶段定义的 `required_capabilities` 已在步骤 A 中存在（现有四阶段为空），本节设计它所匹配的对象。实现是独立的后续步骤，步骤 A 不改变任何环境选择行为。

1. **只读探测**。只查已知根目录（如 `apprepo/vasp/*`、Agent 的 `environments/`、操作者登记的路径）和 `module avail` 的输出；**不做大范围 NFS `find`**（实测超时）。每个 VASP 构建记录：版本、`vasp_std/gam/ncl` 路径、可执行文件 sha256、编译选项（`makefile.include` / package_summary）、由符号与字符串推出的能力（如 `vasp:wannier90`、`vasp:wannier90v2`、`patch:fixz`、`patch:vtst`、`hdf5`、`libxc`），以及其他工具（phonopy、wannier90.x、postw90.x、vaspkit）的路径与版本。探测只读，结果是证据，不授予使用权。
2. **操作者确认的环境配置**。每个配置 = 一个环境脚本 + 能力标签（含版本，如 `vasp>=6`）+ 可执行文件 sha256。标签由探测提出、操作者确认，不由模型声明。
3. **准备时选择**。准备任务时，为每个启用阶段挑选能力满足 `required_capabilities` 的配置；只有一个匹配时展示并确认，多个匹配时在操作者菜单中选择，没有匹配时拒绝准备并说明缺什么。
4. **提交脚本模板不变**，只替换 `source <env script>` 和资源行；`validate_scheduler`、`check_resources` 与受控启动行的检查保持原样。
5. **绑定**。准备好的任务绑定其配置与可执行文件哈希（现有 `environment-review.json` 扩展记录配置名与标签）；提交时哈希变化即阻断，并请操作者重新确认（现有行为是要求新建任务；是否允许"确认后继续"由操作者决定，默认保持阻断）。跨配置复用证据遵循 §8.3 的规则。

### 11.6 实现（已合并至 `main` 并随远端引擎部署）

| 部分 | 位置 | 要点 |
|---|---|---|
| 只读探测 | `remote/worker_inventory.py`（worker 单元，仅标准库）；worker 动作 `software_inventory` | 候选来自 `module -t avail` + `module show`、已配置 `vasp_env`、Agent 的 `environments/*.sh`、操作者根目录（默认 `~/apprepo`）的 `<根>`、`<根>/*`、`<根>/vasp/*` 下的 `bin/`、`app/bin/` 布局；每条命令单独超时（20 s，module 30 s），总预算 240 s，每目录最多 80 项，无递归搜索、不建远程根目录。每个构建一次流式读取 `vasp_std`：sha256、`-D` 编译选项、`vasp.X.Y.Z` 版本串、"compiled without wannier90" 标记；另读 `makefile.include`/`package_summary.json`（≤512 KB）。能力：`vasp:<主版本>`、`vasp:wannier90`、`vasp:wannier90v2`、`vasp:hdf5`、`vasp:openmp`、`vasp:libxc`、`vasp:vaspsol`（`-Dsol_compat` 或字样）、`vasp:vtst`、`patch:fixz`、`patch:ioptcell`；无 Wannier 标记优先于选项。构建自带 `scripts/env.sh` 只在不含启动命令且加载后解析到本构建时才算通过。工具：phonopy（`--version`）、wannier90.x/postw90.x、vaspkit、bader |
| 本地记录 | `remote/environment_profiles.py`；`servers/environments/<服务器>.inventory.json`、`.profiles.json` | 不写 `servers/*.json`。清单带时间戳与 `probe_version`，模型看到的是 `content_is_untrusted` 数据 |
| 登记 | `chat/software_commands.py`：`/software [show\|scan\|register]`；模型工具 `software_environments`（read/scan/register） | 登记只在交互菜单逐个构建确认或跳过；非交互返回 `operator_confirmation_required`。无可用脚本时由模块生成到 `<root>/environments/<名称>.sh`，写入前显示内容并要求输入“写入”；登记前用 `environment_profile_check` 复核脚本和可执行文件哈希与探测一致 |
| 选择 | `parameter_plans.environment_choice`、`Toolbox.direct_environment` | 候选 = 已登记配置 + 隐式 `default`（`vasp_env`，无已确认能力；已有同脚本配置时由其代替）。`relax_mode=cell` 排除 `patch:fixz`/`patch:ioptcell`。一个匹配：方案写明；多个：`/plan` 菜单选择，未选前方案保持 pending；没有：拒绝并说明缺什么（如“需要 vasp:wannier90，已登记环境均不满足”）。未登记任何配置时方案与载荷与以前完全相同 |
| 绑定 | worker `task_environment`；引擎 `full_workflow.ENVIRONMENT_PROFILES_VERSION = 1` | 选定的已登记配置随准备请求发送；worker 复核脚本与可执行文件哈希（不同则 `ENVIRONMENT_PROFILE_DRIFT`，请操作者重新登记后新建任务），`submit.slurm` 只把 `source` 行换成该配置脚本；`config['environment_profile']`（名称、脚本、两个哈希、能力）在签名之后、manifest 之前写入，`environment-review.json` 增加 `profile`。派生任务沿用来源配置记录。提交时按任务记录的配置复核；旧任务（无该字段）照旧用 `vasp_env` |
| 版本标记 | `remote_worker.ENVIRONMENT_PROFILES_VERSION`、`full_workflow.ENVIRONMENT_PROFILES_VERSION` | 引擎缺标记时，带配置的准备、派生和提交都明确拒绝，不改用默认环境 |

首次真实探测（只读）：`/software scan eshell111-vasp544 ~/apprepo`；之后 `/software register eshell111-vasp544` 逐项确认。
