# `vasp-band-agent` 系统架构总览

本文说明项目的三层运行时架构、阶段门禁与证据哈希流、远端引擎冻结契约，以及模块依赖方向约定。改动核心代码前，请先查阅本文及 `docs/modules/` 对应接口卡。

---

## 1. 三层架构与数据流

```text
[控制面 · 本地对话与权限层]  chat/ · models/ · permissions/ · planning/ · editing/ · ui/ · guides/
  ├─ Agent 循环经 ModelAdapter (responses / chat_completions) 与模型交互
  ├─ 59 个模型可见工具经 Toolbox 执行；大结果写入 .agent/evidence/ 并由 model_views 精简
  ├─ 物理确认 / 参数选择 / 资源与提交授权 / 取消恢复 全部在本地交互菜单完成，模型不可代选
  └─ JobMonitor 后台只读轮询 Slurm 状态，终态后触发非授权只读跟进
        │
        │  SSH（Paramiko 长连接池 ssh_pool，不可用时降级 subprocess ssh）
        │  stdin 传输：payload JSON + 拼接后的纯标准库 WORKER_UNITS + remote_worker.py
        ▼
[传输与远端调度层]  remote/（remote_compute · ssh_pool · remote_servers · bundle · worker_*）
  ├─ 远端无需预装 vasp_agent 包；每次 RPC 以独立 `python -X utf8 -` 进程执行
  ├─ 新任务加载 deployment.json 指向的最新 release；既有任务严格加载 remote-job.json 绑定的 release
  └─ 执行 Slurm 容量预检、占位回执写入、sbatch 提交、squeue/sacct 诊断与哈希校验下载
        │
        │  sys.path 注入冻结目录 releases/<16位sha256>/
        ▼
[确定性科学引擎层]  workflow/（vasp_band_agent · full_workflow · stage_gate · stage_definitions · …）
  ├─ 仅依赖 pymatgen / numpy / matplotlib，无模型 API 密钥或对话代码
  ├─ 负责结构/维度检查、POTCAR 组装、收敛扫描、两轮优化与复核、SCF、能带路径与绘图验收
  └─ 所有输入与上游输出均以 SHA-256 绑定至 manifest.json、checkpoint.json 与审阅快照
```

---

## 2. 阶段门禁与证据哈希流

完整任务默认按声明式阶段链顺序推进（兼容旧四阶段）：

```text
prepare_full ─▶ [manifest.json (SHA-256) + config.json + input-plan.json]
                     │
                     ▼
        /physics 物理模型确认 ─▶ physics-model.json（含 initial.POSCAR 哈希与维度检测）
                     │
                     ▼
   ┌────▶ 阶段循环：convergence ─▶ relaxation ─▶ scf ─▶ bands（可扩展可选阶段）
   │         1. get_stage_review：核验上游 complete.json / inputs.json / CHGCAR 哈希，生成 snapshot
   │         2. record_stage_review：按 stage_definitions.review_rules 写入 review-<phase>.json
   │         3. request_submission：操作者菜单确认资源与额度 → capacity_preflight 预检
   │         4. submit_stage：写入占位回执 submissions/<phase>.json → sbatch 提交
   │         5. 作业结束：verify + 数值验收 → complete() 推进 checkpoint.json 与 evidence_hashes
   └───────── 若下一阶段非 complete，进入 awaiting_review 等待下一轮审阅与授权
```

**核心不变量**：
1. **代码验收优先**：`stage_definitions.py` 中的文字说明与 `guides/*.md` 仅用于解释，放行与否完全由 Python 校验代码判定。
2. **输入不可变**：任务准备后禁止覆盖输入；参数修改必须新建任务目录（或经受控派生如 `prepare_scan_retry` / `prepare_production_from_convergence` / `prepare_bands_path`）。
3. **旧任务与旧 Release 零破坏**：`config.json` 无 `stages` 字段即为旧四阶段任务；旧任务绑定原 `releases/<hash>/`，`tests/data/legacy_stage_walk.json` 守护快照逐字节兼容。

---

## 3. 运行时数据与打包边界

### 3.1 本地工作区数据（`common/agent_paths.py`）
- **应用运行时**：统一存放于 `.agent/`（`sessions/`、`audit/`、`evidence/`、`state/`、`cache/`、`migration/`），兼容读取旧根目录散落文件。**禁止更改 `.agent/` 路径契约**。
- **操作者数据**：`servers/`、`runs/`、`downloads/`、`structures/`、`potentials/`、`workbench/`、`reports/`、`.env`、`models.toml` 保持在工作区根目录。

### 3.2 远端引擎白名单打包（`remote/bundle.py`）
- `ENGINE_MODULES` 定义进入集群 `releases/<hash>/` 的全部 15 个模块（14 个 `workflow/*` + `remote/remote_worker`），连同 10 个 `WORKER_UNITS` 与旧平铺入口桥接文件组成 `SOURCE_FILES`（共 43 个文件）。
- 打包结果经 LF 归一化后计算 `local_version()`（16 位十六进制哈希）。不包含 `chat/`、`models/`、`editing/`、`permissions/`、`ui/` 或任何凭据。
- `worker_program()` 将 `WORKER_UNITS` 与 `remote_worker.py` 去除 `# <bundle:package-only>` 块后拼接为纯文本单脚本经 stdin 发送，并在进程内缓存结果。

---

## 4. 子包职责与依赖方向约定

目标依赖应严格遵循**自顶向下无环分层**：

```text
Layer 4 (入口与交互编排):  chat/
Layer 3 (领域协作能力):    permissions/ · planning/ · editing/ · monitoring/ · ui/ · guides/
Layer 2 (协议与远端传输):  models/ · remote/
Layer 1 (纯科学引擎):      workflow/
Layer 0 (基础路径与原语):  common/（含共享 FileLock）
```

### 当前剩余架构债务（计划在阶段 3 消解）
1. **超线文件（已从 8 个降至 6 个）**：`chat/ai_agent.py` 与 `remote/remote_worker.py` 已在阶段 2 拆分为 `<= 490` 行模块并保留门面；剩余 6 个为 `workflow/`（4 个）、`models/context_compaction.py`、`planning/parameter_plans.py`。
2. **剩余反向依赖**：`common <-> monitoring` 已通过 `common/file_lock.py` 彻底消除；仍保留 `workflow/stage_gate.py -> remote/remote_worker.py:check_previous_job` 与 `workflow/vasp_band_agent.py -> chat/ai_agent.py:main`（阶段 3 处理）。

**新增代码约束**：禁止新增由低层指向高层的导入；共享基础原语优先沉淀至 `common/`，跨层回调优先通过参数注入而非在低层反向 `import chat`。
