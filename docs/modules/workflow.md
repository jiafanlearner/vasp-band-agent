# 模块接口卡：`vasp_agent.workflow`

> 职责：确定性 VASP 科学工作流引擎（准备、收敛扫描、优化与复核、SCF、能带路径与绘图、三维 DOS、Tier ③ 求解器覆盖、阶段门禁与参数规范）。全部 24 个模块随远端 `ENGINE_MODULES` 打包部署（当前 release `5b124e988c010ec8`）。

## 核心文件与边界（全部 <= 500 行）

| 模块 | 职责 | 关键导出 |
|---|---|---|
| `full_workflow.py` | 完整流程执行与门面：单阶段校验写盘、并发执行、两轮优化/复核/SCF/能带主流程、完整分析与 re-export | `prepare_full`, `run_full`, `run_stage`, `analyze_full`, `stage`, `STAGE_RUNNERS`, `RESOURCE_LAYOUT_VERSION` |
| `workflow_prepare.py` | 完整流程准备、预览签名生成、阶段选择与参数规范绑定 | `prepare_full`, `stage_selection`, `recorded_stage_parameters` |
| `workflow_policy.py` | 并行标签（`KPAR`/`NCORE`）、扫描阈值/档位、力/应力验收与收敛测试汇总 | `base_incar`, `execution_tags`, `execution_preview`, `incar_matches`, `relaxation_grids`, `metrics`, `compare`, `check_relaxation`, `accept_convergence` |
| `band_path.py` | 自定义/自动能带路径语法解析、晶胞映射、KPOINTS 生成、审阅上下文与 re-export 门面 | `parse`, `validate`, `render`, `record_for`, `kpoints`, `same`, `review_context`, `prepare_bands_path`, `candidates` |
| `band_path_fork.py` | 已通过 SCF 任务换路径派生（证据校验复制与可重现性检查） | `prepare_bands_path`, `_source_files`, `_check_reproducible` |
| `band_path_candidates.py` | 候选路径推荐（Setyawan–Curtarolo、Hinuma、层状面内、交叉/带边） | `candidates`, `candidates_for_task`, `candidates_for_download` |
| `convergence_array.py` | 远端 convergence Slurm 数组任务执行、收集与原子终态汇总 | `run_array_test`, `collect_array`, `finalize_array`, `execution_command` |
| `convergence_array_checks.py` | 数组父作业绑定、回执校验、恢复日志与调度终态证据核验 | `_context`, `_validate_receipt`, `_prepare_journal`, `_scheduler_check` |
| `vasp_band_agent.py` | 核心原语（原子 `save`、线程局部 `digest_memo`、`event`）、旧版 `bands-only` 流程与可注入 `chat_handler` 的 CLI | `ToolError`, `save`, `digest`, `digest_memo`, `verify`, `execute`, `read_result`, `validate_band_data`, `export_bands`, `main` |
| `band_export.py` | 全路径本征值/占据校验、出版级能带图与 `bands.csv`/`report.json` 导出 | `validate_band_data`, `export_bands` |
| `convergence_reuse.py` | 单项收敛核验、选择性重扫（`prepare_retry`）与生产参数派生 | `inspect`, `prepare_retry`, `prepare_production`, `production_cutoff`, `production_mesh` |
| `stage_gate.py` | 物理模型确认（`CONFIRMATION_VERSION=2`，含 `operator_task_contract` 通道）、上游证据哈希快照、`incar_solver_diff` 注入、阶段审阅记录与检查点推进（零反向依赖） | `CONFIRMATION_VERSION`, `confirm_physics`, `physics`, `context`, `record`, `require_review`, `complete`, `evidence` |
| `solver_overrides.py` | Tier ③ 纯数值求解器 INCAR 覆盖确定性裁判（9 标签强类型/值域/阶段约束、静态注释、fork 上游锁定合并，纯标准库） | `SOLVER_OVERRIDES_VERSION`, `ALLOWED_TAGS`, `SOLVER_NOTES`, `validate`, `apply_to_incar`, `diff_for_phase`, `merge_fork_overrides`, `apply_fork_overrides` |
| `stage_definitions.py` | 版本化声明式阶段定义（依赖、审阅项、产物、能力需求） | `DEFINITIONS`, `LEGACY_STAGES`, `record`, `order`, `definition`, `following`, `required_capabilities` |
| `band_analysis.py` | 对已导出 `bands.csv` 做确定性只读物理分析 | `parse_bands`, `analyze_bands`, `fermi_crossings` |
| `parameter_specs.py` | 可选阶段（`dos` v2 可执行，v1 保留已取代）参数规范、推导建议与校验 | `for_stage`, `describe`, `validate`, `check_record`, `optional_stages` |
| `scf_reuse.py` | 已通过 SCF 证据复用的公共校验与派生配置工具（供换路径与 DOS 派生共用） | `verified_source`, `source_files`, `check_reproducible`, `copy_files`, `fork_config` |
| `dos_stage.py` | 三维 DOS 阶段计划、预检、执行（`STAGE_RUNNERS['dos']`）、运行后验收、导出与 DOS 派生 | `DOS_VERSION`, `plan`, `review_context`, `run`, `check_outputs`, `fork_preview`, `prepare_dos_fork` |
| `dos_plot.py` | DOS 图、能带+DOS 统一零点合图与已校验下载的本地重绘 | `parse_dos`, `plot_dos`, `plot_combined`, `replot_folder`, `DosDataError` |
| `input_review.py` | 带注释输入方案生成（`input-plan.json`）与旧版审阅门禁 | `prepare_plan`, `context`, `record`, `require_review`, `preparation_summary` |
| `band_plot.py` | 出版级能带绘图与基于 `bands.csv` 的本地重绘 | `plot_bands`, `save_figure`, `replot_folder` |
| `dimensionality.py` | 自动判定 3D 体相 / 2D slab（c 轴真空 ≥ 7 Å）与面内路径 | `classify`, `require_supported`, `is_2d`, `band_path_2d` |
| `scan_config.py` | 三级 ENCUT / k 网格序列校验与文件逐项核对 | `mesh_ladder`, `validate_scans`, `verify_scan_inputs` |
| `potential_library.py` | 本地 PBE 赝势库选择与字节级拼接 | `select_potentials`, `assemble_potentials` |

## 关键契约与注意事项

- **严禁依赖控制面或传输层**：本包为 Layer 1 纯科学引擎，随 `ENGINE_MODULES` 部署至无 AI 依赖的计算节点，零向上导入（由 `tests/test_stage3_hardening.py` AST 守门）。
- **并发与兼容**：`vasp_band_agent._digests` 使用 `threading.local()` 隔离并发线程；`tests/data/legacy_stage_walk.json` 锁死旧四阶段快照逐字节兼容。
