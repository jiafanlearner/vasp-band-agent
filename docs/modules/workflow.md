# 模块接口卡：`vasp_agent.workflow`

> 职责：确定性 VASP 科学工作流引擎（准备、收敛扫描、优化与复核、SCF、能带路径与绘图、阶段门禁与参数规范）。全部打包进入远端 `ENGINE_MODULES`。

## 核心文件与边界

| 模块 | 行数 | 职责 | 关键导出 |
|---|---:|---|---|
| `full_workflow.py` | 946 *(债务)* | 完整流程准备与执行：收敛扫描、两轮优化、复核、SCF、能带、并行标签 | `prepare_full`, `run_full`, `analyze_full`, `accept_convergence`, `execution_tags`, `STAGE_RUNNERS` |
| `band_path.py` | 772 *(债务)* | 自定义/自动能带路径语法解析、晶胞映射、审阅校验与 SCF 派生 | `parse`, `validate`, `record_for`, `review_context`, `prepare_bands_path` |
| `convergence_array.py` | 523 *(债务)* | 远端 convergence Slurm 数组任务执行与结果汇总验收 | `run_array_test`, `collect_array`, `finalize_array` |
| `vasp_band_agent.py` | 503 *(债务)* | 核心原语（原子 `save`、`digest`、`event`）、旧版 `bands-only` 流程与 CLI | `ToolError`, `save`, `digest`, `digest_memo`, `verify`, `execute`, `read_result`, `export_bands`, `main` |
| `convergence_reuse.py` | 431 | 单项收敛核验、选择性重扫（`prepare_scan_retry`）与生产参数派生 | `inspect`, `prepare_retry`, `prepare_production`, `production_cutoff`, `production_mesh` |
| `stage_gate.py` | 355 | 物理模型确认、上游证据哈希快照、阶段审阅记录与检查点推进 | `confirm_physics`, `physics`, `context`, `record`, `require_review`, `complete`, `evidence` |
| `stage_definitions.py` | 350 | 版本化声明式阶段定义（依赖、审阅项、产物、能力需求） | `DEFINITIONS`, `LEGACY_STAGES`, `record`, `order`, `definition`, `following`, `required_capabilities` |
| `band_analysis.py` | 316 | 对已导出 `bands.csv` 做确定性只读物理分析 | `parse_bands`, `analyze_bands`, `fermi_crossings` |
| `parameter_specs.py` | 299 | 可选阶段（如 `dos` v1）参数规范、推导建议与校验 | `for_stage`, `describe`, `validate`, `check_record`, `optional_stages` |
| `input_review.py` | 286 | 带注释输入方案生成（`input-plan.json`）与旧版审阅门禁 | `prepare_plan`, `context`, `record`, `require_review`, `preparation_summary` |
| `band_plot.py` | 275 | 出版级能带绘图与基于 `bands.csv` 的本地重绘 | `plot_bands`, `save_figure`, `replot_folder` |
| `dimensionality.py` | 105 | 自动判定 3D 体相 / 2D slab（c 轴真空 ≥ 7 Å）与面内路径 | `classify`, `require_supported`, `is_2d`, `band_path_2d` |
| `scan_config.py` | 82 | 三级 ENCUT / k 网格序列校验与文件逐项核对 | `mesh_ladder`, `validate_scans`, `verify_scan_inputs` |
| `potential_library.py` | 57 | 本地 PBE 赝势库选择与字节级拼接 | `select_potentials`, `assemble_potentials` |

## 关键契约与注意事项

- **严禁依赖控制面**：所有模块随 `ENGINE_MODULES` 部署至无 AI 依赖的计算节点，不得引入 `openai`/`rich`/`prompt_toolkit` 或 `chat`/`models` 模块。
- **冻结窗口**：阶段 1 / 阶段 2 不改动本目录内任何文件，确保 `bundle.local_version()` 不变；`tests/data/legacy_stage_walk.json` 锁死旧四阶段快照逐字节兼容。
