# VASP Band Agent · 自动化能带计算

**Automated VASP band structure calculations with an AI agent.**

从晶体结构出发，自动准备并执行 **收敛测试 → 结构优化 → SCF 自洽 → 能带计算 → 绘图与报告**。支持自然语言操作、本机计算和远程 SSH / Slurm 集群。

[English](README.en.md) · [使用文档](docs/README.md) · [AI 操作指南](docs/guides/AI_USAGE.md) · [La₂CuO₄ / Si 能带示例](examples/results/README.md)

![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue)
![VASP workflow](https://img.shields.io/badge/VASP-band%20structure-1f4e79)
![PBE](https://img.shields.io/badge/DFT-PBE-green)

![VASP 自动计算得到的 La₂CuO₄ 非磁性 PBE 能带，采用 Hinuma 路径](docs/assets/la2cuo4-band-structure.png)

*La₂CuO₄ 已有实跑结果：360 个 k 点、64 条能带，自定义 Hinuma 路径，以 SCF 费米能为零点。此非磁性 PBE 计算呈金属性，未包含反铁磁或 DFT+U；[查看数据与计算说明](examples/results/README.md#la2cuo4)。*

<details>
<summary>查看 Si PBE 能带验证示例</summary>

![VASP 自动计算得到的 Si PBE 能带示例](docs/assets/si-band-structure.png)

Si 示例以 VBM 为能量零点，采样路径间接带隙为 0.6104 eV；[查看数据与说明](examples/results/README.md#silicon)。

</details>

## 它能做什么

| 计算环节 | 自动化内容 |
| --- | --- |
| 结构与输入 | 导入结构，检查晶胞、原子间距及占位，核对 POTCAR 元素顺序与 PBE 类型 |
| 精度收敛 | ENCUT 和 k 网格三级扫描，检查能量、力及应力变化 |
| 结构优化 | 两轮优化、结构传递，以及优化后更高精度复核 |
| SCF 与能带 | 生成计算晶胞和高对称路径，SCF 后用 `ICHARG=11` 做固定电荷能带 |
| 结果交付 | 输出 PNG / PDF 能带图、CSV 数据和 JSON 验收报告 |
| AI 与集群 | 对话式准备、输入审阅、SSH 部署、Slurm 提交、作业监控及结果下载 |

![自动能带计算流程：结构检查、参数收敛、结构优化、SCF、能带与结果输出](docs/assets/workflow.svg)

AI 负责理解请求、调用工具、审阅证据并解释结果；计算执行与数值验收由确定性 Python 程序完成。**自动化流程保留操作者确认与阶段审阅**：新完整任务在 convergence、relaxation、scf、bands 之间暂停，审阅通过并明确授权后再提交下一阶段。

## 快速开始

需要 Python 3.11+。真实计算另需自行安装的 **VASP 和合法可用的 PBE POTCAR**；本仓库不提供这些文件。AI 对话另需模型 API，计算节点不需要 API 密钥。

### 1. 安装

```bash
git clone https://github.com/jiafanlearner/vasp-band-agent.git
cd vasp-band-agent
python -m venv .venv
# Linux / macOS
source .venv/bin/activate
# Windows PowerShell 使用：.venv\Scripts\Activate.ps1
python -m pip install -e ".[ai,ssh]"
```

### 2. 先预览输入（不运行 VASP）

```bash
python vasp_band_agent.py prepare examples/Si.POSCAR runs/si-preview
python vasp_band_agent.py status runs/si-preview
```

不提供 POTCAR 时仅生成结构、流程配置和输入清单，不能提交计算。任务目录须为尚未使用的新目录。

### 3. 用 AI 准备真实任务

将 `.env.example` 复制成 `.env`，在本地填写 API 密钥，并在 `models.toml` 中配置自己的模型。显式选择 profile：

```bash
python ai_agent.py --workspace . --profile official
# 也可选择已配置的 relay 或 deepseek profile
```

示例请求：

> 使用 examples/Si.POSCAR，为我的 Slurm 集群准备一个非磁性 PBE 硅能带任务。先检查 VASP 环境和赝势，比较 ENCUT 与 k 网格收敛方案，解释参数，先不提交。

首次使用集群需导入 SSH 连接，并通过 `/setup-server <服务器名>` 配置计算环境。准备后输入 `/physics` 核对物理模型，要求 AI 审阅当前阶段，再明确要求提交。每个后续阶段都需要审阅与提交授权。详见 [AI 使用说明](docs/guides/AI_USAGE.md) 和 [集群接入](docs/guides/CLUSTER_ONBOARDING.md)。

无需 AI 的输入准备也保留：

```bash
python vasp_band_agent.py prepare examples/Si.POSCAR runs/si-real --potcar /path/to/Si/POTCAR --nbands 16
```

此命令只准备任务，不代表已完成物理确认、审阅或提交。新完整任务请遵循阶段流程；`run` 是取得资源和阶段放行后的计算执行入口。

## 当前支持范围

- 非磁性、电中性、PBE、有序周期结构；可从未优化结构开始。
- 三维体相；二维 slab 要求真空方向沿第三晶格矢量 c，真空方向 k 网格为 1，使用面内路径并保留真空。见[维度处理](src/vasp_agent/workflow/dimensionality.py)。
- 自动或[自定义能带路径](docs/guides/BAND_PATHS.md)，本机执行与远程 SSH / Slurm，收敛测试数组任务。
- 磁性、SOC、DFT+U、HSE、声子及 Wannier 不属于当前内置自动能带工作流。扩展指南不等于已实现的计算阶段。

输出包括 `bands.png`、`bands.pdf`、`bands.csv`、`report.json`、收敛与优化复核记录及最终结构。带隙仅覆盖采样路径，PBE 带隙不等于实验准粒子带隙；优化收敛不代表动力学稳定。

## 开发与文档

```bash
python -m pip install -e ".[ai,ssh,test]"
python -m pytest -q
```

源码在 `src/vasp_agent/`，离线测试在 `tests/`。更多说明见[文档导航](docs/README.md)、[作业监控](docs/guides/JOB_MONITORING.md)、[收敛诊断](docs/guides/CONVERGENCE_DIAGNOSTICS.md)和[代码目录](docs/development/PROJECT_LAYOUT.md)。

首次公开发布验证（2026-11-18，Windows / Python 3.12）：**1764 passed, 2 skipped**。同时验证了可编辑安装与无 POTCAR 输入预览；本次没有新运行 VASP。

`.env`、SSH 配置、私钥、POTCAR、会话和计算工作目录保持本地。历史文档中的个人路径与连接信息已替换为示例值；历史状态只代表其记录时点。

<details>
<summary>旧版详细参考（历史说明；当前支持范围和阶段流程以上文及 AI 指南为准）</summary>

## VASP 完整计算 Agent · v0.7 历史参考

## 日常入口与目录

Git Bash 启动：`bash start-agent.sh`。原来的 `python ai_agent.py` 和 `python vasp_band_agent.py` 保留为兼容入口，具体实现已按功能移入 `src/vasp_agent/`。

```text
vasp-band-agent/
├── ai_agent.py / vasp_band_agent.py  原命令兼容入口
├── start-agent.sh                   Git Bash 启动脚本
├── pyproject.toml                   安装、依赖和测试配置
├── src/vasp_agent/                  按功能组织的 Python 包
│   ├── chat/                       对话循环、会话及任务命令
│   ├── models/                     模型接口、配置、上下文预算
│   ├── editing/                    草稿编辑、修改记录、撤销
│   ├── workflow/                   VASP 生成、收敛与科学验收
│   ├── remote/                     SSH、远程传输与引擎打包
│   ├── planning/                   参数草案和选择
│   ├── permissions/                提交授权和资源确认
│   ├── ui/                         终端显示与输入菜单
│   └── common/                     共用路径处理
├── tests/                          离线自动化测试
├── examples/                       结构、Slurm 与界面示例
├── docs/                           使用指南、开发说明、交接及预览
├── reports/                        实跑记录、回归证据、整理清单
├── servers/                        本地服务器及计算配置
├── runs/ / downloads/              本地任务和已下载结果
└── potentials/                     本地赝势配置及合法自备文件
```

草稿目录 `workbench/` 在使用时创建。Agent 自己的运行文件（对话、审计日志、证据、设置、监控记录、扫描方案、默认资源、分析回执、修改记录、缓存）统一放在工作区的 `.agent/` 下，不再散落在仓库根目录：

```text
.agent/
├── sessions/          ai-session-<名称>-<接口标识>.json 及其上下文缓存等附属文件
│   └── archive/       /sessions archive 归档的对话（移动，不删除）
├── audit/             ai-audit.jsonl，满 5 MB 整体改名为 ai-audit.<序号>.jsonl，不截断
├── evidence/          按 SHA-256 命名的证据原文（read_context_evidence 校验哈希）
├── state/             设置、作业监控记录及锁、模型能力、scan-plans/、submission-defaults/、analysis-runs/、workspace-edits/
├── cache/             探针输出与 pytest 缓存，可随时删除
└── migration/         迁移记录及被搁置的旧副本
```

`.env`、`models.toml`、`servers/`、`runs/`、`downloads/`、`structures/`、`reports/`、`workbench/`、`exports/`、`potentials/` 等操作者数据保持原位。旧版本留在根目录的 `.ai-session-*`、`.ai-audit.jsonl`、`.context-evidence/` 等文件仍可直接读取和续写，迁移前不影响使用；迁移方法见 [AI 使用说明](docs/guides/AI_USAGE.md#工作区运行文件与迁移)。缓存和安装生成目录不是业务源码。

- [文档导航](docs/README.md)：按用途查找说明。
- [本地编辑与密钥更新](docs/guides/WORKSPACE_EDITING.md)。
- [代码目录与维护约定](docs/development/PROJECT_LAYOUT.md)。

若使用之前安装的 `vasp-ai` / `vasp-band-agent` 命令，在项目根目录运行 `python -m pip install -e ".[ai,test]"` 更新安装映射。之后也可以运行 `python -m vasp_agent`。根目录兼容启动脚本无需先重新安装项目；依赖仍须已安装。

输入初始晶体结构，自动执行结构检查、参数收敛测试、两轮结构优化、优化后精度复核、静态自洽与能带计算，输出图和数据。v0.3 新增**大模型对话与工具调用层**：AI 理解请求、调用计算工具、诊断日志并解释结果，底层计算与验收仍由确定性程序执行。

自然语言入口：`python ai_agent.py --workspace .`，需安装 AI 可选依赖并配置模型 API。见 [AI 使用说明](docs/guides/AI_USAGE.md)。v0.6 新任务需要 `/physics` 确认与阶段审阅；计算节点不需要模型 API。以下旧版一次运行到底的示例仅适用于已有旧任务，新任务按 AI 使用说明分阶段执行。

默认 `prepare` 生成完整流程，不再要求输入已优化结构。能带路径及最终 SCF 输入只在优化验收后生成，避免使用旧结构。

## 支持范围

- 三维、非磁性、有序周期晶体；PBE；输入可为未优化结构。
- 优化保留输入晶胞；优化后采用与高对称路径匹配的标准原胞，SCF 与能带使用同一晶胞。
- SCF 均匀 Γ 网格 → `ICHARG=11` 高对称路径 → 使用 SCF 费米能级绘图。
- 能带路径默认自动；也可由操作者指定（准备时 `band_path`，或对 SCF 已通过的任务用 `prepare_bands_path` 只重做能带），见 [docs/guides/BAND_PATHS.md](docs/guides/BAND_PATHS.md)。
- 收敛检查、输入 SHA-256 校验、执行事件日志、已验收阶段跳过。
- 不自动处理 SOC、磁性、DFT+U、HSE、二维材料、声子稳定性或不同晶相的全局搜索。
- 默认 ENCUT=520 eV、8×8×8 网格作为扫描起点。晶胞优化起点至少为 1.3×最大 ENMAX。

## 完整流程和验收

1. 检查晶胞、周期原子间距、占位及 POTCAR 元素顺序。
2. 扫描三个 ENCUT：起点的 1、1.15、1.3 倍；固定最密 k 网格。
3. 扫描三个 k 网格：输入网格各方向的 1、1.25、1.5 倍向上取整；固定最高 ENCUT。
4. 两组扫描均要求连续两级差值达标，采用测试过的最高精度参数。
5. 两轮 `IBRION=2` 优化：第二轮从第一轮最终结构重新启动，重建基组。
6. 优化后分别提高 ENCUT 和 k 网格复核，检查静态力/应力。
7. 根据最终结构生成标准原胞及高对称路径，调整 SCF 网格以保持倒空间采样密度。
8. SCF → 固定电荷能带 → 图、数据、完整验收报告。

正常路径共 **13 个 VASP 子计算**，不保证其总耗时。默认验收：能量差 ≤1 meV/atom；收敛扫描的力变化 ≤0.005 eV/Å；晶胞模式的应力变化 ≤0.5 kbar；最终最大力 ≤0.01 eV/Å、最大应力分量 ≤1 kbar。所有电子步骤及离子优化必须通过检查。若扫描范围内没有收敛，流程停止，需增大起点建立新任务；不会自动扩大费用或放松阈值。

`--relax-mode cell`（默认）优化原子、晶胞形状及体积；`--relax-mode ions` 仅优化原子，适合固定实验晶胞，且不要求应力为零。使用 `--force-tol`、`--stress-tol`、`--energy-tol`、`--nsw` 调整验收要求。

## 安装

在此目录执行（Python 3.11 或更高）：

```bash
python -m venv .venv
# Linux / 集群
source .venv/bin/activate
# Windows PowerShell 改用 .venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
python -m pytest -q
```

VASP 可执行程序和本地合法可用的 PBE POTCAR 需要自行提供，本项目不附带它们。

## 第一步：预览输入

```bash
python vasp_band_agent.py prepare examples/Si.POSCAR runs/si-preview
```

无 POTCAR 时只生成初始结构、流程配置和清单，不能执行。示例为硅金刚石结构，晶格常数 5.43 Å。`manifest.json` 中的 `potential_order` 给出所需 POTCAR 元素顺序。

## 第二步：准备真实任务

```bash
python vasp_band_agent.py prepare examples/Si.POSCAR runs/si-real --potcar /absolute/path/to/Si/POTCAR --mesh 8 8 8 --encut 520 --points 40
```

多元素结构需要按输入结构顺序提供拼接完成的 POTCAR。程序检查元素顺序、PBE 类型和 ENMAX。工作目录必须为空；更改参数请新建目录，不要修改已登记输入。

仅在确实需要跳过前置流程时显式加 `--workflow bands-only`，该模式沿用 v0.1 的行为，要求自行验证几何结构和参数精度。旧 v0.1 工作目录继续按旧流程运行，不自动升级。

## 第三步：运行

在已申请计算资源的环境中执行，例如：

```bash
python vasp_band_agent.py run runs/si-real -- srun vasp_std
```

或者使用集群要求的 `mpirun -np ... /path/to/vasp_std`。程序原样传递参数，不启动 shell，不解析管道或重定向。环境模块应在启动 Agent 前加载。不要在登录节点直接运行 VASP。

`examples/job.slurm` 是通用占位模板，必须按真实分区、账号、核数及模块修改。提交前检查这些配置。登录脚本只解决连接问题，并不能确定 VASP 路径、赝势库或作业资源。

提供 `examples/eshell111.slurm` 和 [集群部署说明](docs/guides/CLUSTER.md)。其中路径来自历史环境核验，使用前应确认当前环境；不能直接用于未经确认的三维晶胞优化。

## 结果与状态

```bash
python vasp_band_agent.py status runs/si-real
python vasp_band_agent.py analyze runs/si-real
```

成功后输出：`bands.png`、`bands.pdf`、`bands.csv`、`report.json`。横轴为高对称路径，纵轴是相对 SCF 费米能级的能量（eV）。CSV 记录每条能带、k 点、倒空间距离和能量。

`report.json` 的带隙仅针对采样路径；路径外可能存在其他极值，PBE 带隙也不是实验准粒子带隙。完整模式报告包含参数扫描、优化力/应力以及优化后精度复核记录。有限扫描不能证明全局收敛，优化收敛也不等于动力学稳定。

中间输出：`convergence.json`、`relaxation.json`、`post-relax-validation.json`、`relaxed-1.POSCAR`、`relaxed-2.POSCAR`、`workflow-state.json`。最终结构取自经过验收的 XML 输出。

执行日志在 `events.jsonl` 和各阶段的 `stdout.log`。只有通过检查的阶段才生成 `complete.json`。重复 `run` 可跳过已验收阶段。未验收的失败输出不会被覆盖，应先诊断，并在新目录建立任务。作业被强制终止后可能残留 `.agent.lock`；确认没有进程使用该目录后再手动删除。

## 当前验证情况

离线测试覆盖扫描失败阻断、力/应力验收、优化结构传递、阶段依赖、输入哈希、授权与防重复提交。使用方式见 [AI_USAGE.md](docs/guides/AI_USAGE.md)。

历史真实 Si 工作流及科学结论边界见 [实跑记录](reports/runs/RUN_SI_20260925.md)，近期收敛回归证据见 `reports/regression/`。这些是保存的历史记录，不代表本次目录整理重新连接、部署或运行了计算。最新本地整理验证结果记录在 [目录说明](docs/development/PROJECT_LAYOUT.md)。

## 依据

- [VASP 官方 DFT 能带流程](https://vasp.at/wiki/Band-structure_calculation_using_DFT)
- [ICHARG](https://vasp.at/wiki/ICHARG)
- [pymatgen 高对称路径](https://pymatgen.org/pymatgen.symmetry.html)

后续扩展点：磁性与 SOC 工作流、分阶段集群资源配置、后台 AI 监控。

## 输入解释与 AI 审阅

新完整流程生成带注释的初始 INCAR、PARAMETERS.md 和 input-plan.json。提交前 AI 读取实际输入并记录审阅，输入快照变化会使审阅失效；运行中按已审阅方案生成后续输入。使用步骤与旧任务兼容说明见 [AI_USAGE.md](docs/guides/AI_USAGE.md)。

新版参数准备先比较工具默认值和 AI 建议，有差异时交互选择；`/plan` 可恢复待确认方案。完整 ENCUT 与 k 网格扫描序列会原样生成并核对，见 AI_USAGE.md 的 v0.7 说明。

</details>
