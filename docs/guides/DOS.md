# 三维态密度（DOS）

DOS 是第一个建在阶段机制上的新工作流（设计见 `docs/design/WORKFLOW_MODULES.md` §7）。首版只支持三维体相；二维 slab 明确拒绝（后续扩展）。

## 两种入口

| 入口 | 何时用 | 怎么做 |
|---|---|---|
| 新任务的可选阶段 | 还没算过的新体系 | 让 AI 用 `propose_scan_plan`（`optional_stages=["dos"]`）提出方案；在 `/plan` 三合一启动核对窗口中一次确认区块 A（物理模型与研究目标）、区块 B（扫描参数、dos 规范建议/AI 建议/逐项修改与环境配置）及区块 C（首阶段 `convergence` 核数与单次预授权）：选 `1` 准备并在审阅通过后直接提交 `convergence`，选 `2` 仅准备，`3`/`4`/`5` 分别修改三区块，`q` 取消。阶段顺序 `convergence → relaxation → scf → bands → dos`；预授权仅限 `convergence` 1 次，后续 `relaxation` / `scf` / `bands` / `dos` 仍逐段单独审阅、经 `request_submission` 单独授权提交。 |
| 从已通过的 SCF 派生 | 已有任务（含旧任务）的 SCF 已验收，只想补 DOS | 对 AI 说“给 <任务名> 做 DOS”，AI 调用 `request_dos_fork`，程序打开统一核对菜单；或运行前被阻断、想改参数时也用它。 |

派生统一核对菜单分为两区：**【计算参数区】**显示来源 SCF 的 E_F、NBANDS、NELECT、网格和 CHGCAR 哈希（均已校验），每个参数的含义、当前取值和规范建议及依据，规范建议生成的 ISMEAR/EMIN/EMAX/NEDOS/NBANDS/KPOINTS，以及目标目录名（若被程序阻断则列出原因和替代方案）；**【提交资源区】**显示 `dos` 阶段的提交模式（`allocation`）、核数、节点、分区、时限与 `KPAR`/`NCORE` 预览（优先加载服务器已保存的默认资源）。菜单收口选项：

- `1`：确认参数与核数，并在审阅通过后直接提交本阶段（仅预授权本次 `dos` 1 次，绝不跨阶段连授）；
- `2`：确认参数与核数，仅准备新目录（暂不提交，审阅后可再经 `request_submission` 授权）；
- `3`：修改计算参数（`字段=值`，空格分隔，`null` 表示空，如 `nbands=120 window_max_eV=4`）；
- `4`：修改核数与资源（支持 `每组=... 节点=... 分区=... 时限=...`，`dos` 阶段并发固定为 `1`；末尾加 `保存` 或输入 `s` 可存为服务器默认，`e` 逐项修改）；
- `q`：取消（不建目录、不授权）。

> 本机未配置 Slurm 提交脚本时仅展示 `1`/`2`/`3`/`q`，只准备目录不授予提交权；非交互模式返回 `operator_confirmation_required`，模型不可代选。引擎拒绝的取值不会建目录，菜单会再问。

基础新目录名为 `<来源>_dos<6 位哈希>`（总长受 `<= 64` 字符约束）。若相同参数组合对应的目录已存在，**绝不覆盖旧目录**：菜单会明确提示目录冲突并自动递增生成下一个可用名 `<base>_2`、`<base>_3`…，经操作者确认后创建（若创建瞬间发生并发同名冲突，程序也会二次询问操作者输入 `1` 确认递增）。新目录复用来源的结构、赝势、配置、环境配置与可执行文件指纹、`scf/CHGCAR`（`charge_sha256` 校验），只运行 `dos`，建目录后立即落盘确认核数（避免审阅返工）；**来源不改动**，不继承来源的审阅和提交授权，并启用 `KPAR`/`NCORE` 执行标签。即使选 `1` 预授权，落盘后仍必须完整通过 `get_stage_review` → `record_stage_review(allowed=true)` 及远端预检才调用 `submit_stage`，判 `blocked` 或预检未过时硬阻断。

## 参数（规范 dos v2）

| 字段 | 默认 | 说明 |
|---|---|---|
| `kmesh_factor` | 2 | SCF 网格每方向的倍数（Γ 中心）。初始策略，不证明 DOS 已收敛 |
| `integration` | tetrahedron | ISMEAR=-5（Blöchl）。需规则网格且不可约 k 点下界 ≥ 4，否则阻断；`gaussian` 须操作者明确给 `sigma_eV` |
| `sigma_eV` | null | 仅 gaussian |
| `energy_reference` | scf_fermi | 零点 = SCF 的 E_F |
| `window_min_eV` / `window_max_eV` | −10 / +5 | 绘图与投影窗口（相对 SCF E_F） |
| `sampling_interval_eV` | 0.01 | 采样间隔（不是分辨率），决定 NEDOS |
| `empty_band_margin_eV` | 1.0 | 最高能带须高于 E_F + 窗口上限 + 余量 |
| `nbands` | null | null 沿用 SCF 的 NBANDS；不够覆盖窗口时阻断，改大（或缩小窗口上限） |

EMIN 取“窗口下限”与“最低 SCF 本征值 − 2 eV（Gaussian 再加 6σ）”中较低者，使电子数积分覆盖半芯态；NEDOS 按采样间隔覆盖整个范围（上限 20001）。

## 审阅与验收

运行前，`get_stage_review` 的 `dos_plan` 给出将要运行的 INCAR/KPOINTS 和四项程序预检（`scf_source`、`inputs`、`integration`、`window`），对应审阅项 scf、inputs、integration、window；预检未过时不能记录放行。运行时引擎重新生成并逐项比对。

运行后程序检查：输出完整有限；实际参数与方案一致；总 DOS 积分电子数（从 EMIN 到 DOS 计算的 E_F，ISPIN=1 已含自旋因子 2）与 NELECT 相差 ≤ 0.05 e，且 EMIN 低于最低本征值至少 0.5 eV；最高能带在每个 k 点覆盖窗口；记录 E_F(DOS) − E_F(SCF)（超过 0.1 eV 标记，不判失败）；投影 DOS 单位 states/eV/cell，不要求投影之和等于总 DOS。任一硬检查失败，`dos-report.json` 记录 `failed`，阶段不完成。

## 输出

`dos.csv`、`dos.png`/`dos.pdf`、`dos-report.json`、`dos-plan.json`；有 `bands.csv` 时另有 `bands-dos.png`/`.pdf`（两图零点都是 SCF E_F；单独的 `bands.png` 对绝缘体可能以 VBM 为零点）。`report.json` 增加 `dos` 字段。下载后用 `replot_bands`（或 `/plot replot 目录`）本地重绘为 `dos.replot.*` 与 `bands-dos.replot.*`，原图和收据不变。

## La₂CuO₄：给现有任务补 DOS

现有任务的 SCF 是在 VASP 5.4.4 上算的，DOS 不需要 Wannier90，也不需要 VASP 6，所以直接派生即可（同一可执行文件，指纹必须一致）：

1. 确认目标集群已部署包含三维 DOS 支持的当前 release（来源任务仍绑定原 release 不受影响；派生目录绑定当前部署的 release）。
2. 对 AI 说：“给 eshell111-vasp544 上的 La2CuO4_HTT_Cu_pv_780_ions_32c 做 DOS”。AI 调用 `request_dos_fork`，统一核对菜单同时展示来源 SCF 证据、参数建议与提交资源区。
3. 看 `nbands` 一行：若 SCF 的 NBANDS 不足以覆盖 E_F + 5 + 1 eV，建议值会给出（约 1.5 倍），或按 `3` 调整 `nbands`/`window_max_eV`；如需调整核数或时限按 `4`。Cu_pv 的 3p、La 的 5s/5p 是半芯态，EMIN 会自动放到最低能带之下，NEDOS 相应变大（仍按 0.01 eV 采样）。
4. 选 `1` 一次确认参数与核数并预授权 `dos` 单次提交（若仅想准备则选 `2`），得到 `La2CuO4_HTT_Cu_pv_780_ions_32c_dos<哈希>`（若同名目录已存在，确认后自动递增为 `_2`/`_3`，不覆盖原目录）；AI 执行 `get_stage_review` → `record_stage_review(allowed=true)` 后可直接调用 `submit_stage`（若之前选 `2`，则在审阅后再走 `request_submission` → `submit_stage`）。
5. 完成后 `remote_fetch` 下载，`replot_bands` 重绘。派生目录不含 bands，所以不生成合图；需要同一任务内的能带 + DOS 合图时，用新任务的可选 DOS 阶段（bands → dos）。
