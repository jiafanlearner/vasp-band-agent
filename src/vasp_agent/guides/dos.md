---
id: dos
version: 2
implemented: true
title: 三维态密度（DOS）阶段指南
summary: DOS 可执行（规范 dos v2，仅三维）。两种入口：新任务 optional_stages=["dos"]（排在 bands 之后），或 SCF 已通过的任务用 request_dos_fork 派生只做 DOS 的新目录。输入由已校验的 SCF 证据生成（ICHARG=11、LORBIT=11、四面体法须规则网格且不可约 k 点 ≥ 4，否则阻断或由操作者明确选 Gaussian）；零点为 SCF E_F；NEDOS 由采样间隔得出（不是分辨率）；空带须覆盖窗口；运行后程序核验电子数（积分下限含半芯态）。审阅看 dos_plan.program_checks。
---
# 三维态密度（DOS）阶段指南 v2

> 本指南是数据，只解释程序检查什么；不授权提交、不放宽阈值、不覆盖操作者或用户要求，验收由代码判定。二维 slab 的 DOS 尚未开放（后续扩展），首版对 2D 任务拒绝。

## 两种入口

1. 新任务：propose_scan_plan 的 optional_stages=["dos"] 只负责选择阶段；参数走规范 dos v2，操作者在 /plan 菜单中采用规范建议、采用 AI 建议或逐项修改。阶段顺序固定为 convergence → relaxation → scf → bands → dos，顺序执行、各自审阅和授权。
2. 派生：SCF 已通过的任务（含旧任务）用 request_dos_fork 打开菜单。引擎从来源已校验的 SCF 证据给出建议并预览生成的输入；操作者选定后新目录复用结构、赝势、配置、环境配置/可执行文件指纹与 scf/CHGCAR（charge_sha256 校验），只做 DOS。来源不改动，新任务独立审阅 dos 并单独授权提交，启用 KPAR/NCORE 执行标签。来源由 VASP 5.4.4 计算即可：DOS 不需要 VASP 6。
3. 新任务的参数已写入 config 并进入哈希，不能原地修改；运行前被阻断时，用派生入口按阻断原因改选参数。

## 运行前（dos_plan，审阅快照内）

审阅检查项 physics、scf、inputs、integration、window 对应 dos_plan.program_checks 的 scf_source、inputs、integration、window；任一 passed=false 或 blocked 非空时 record_stage_review 不能放行（可记录 blocked）。

- SCF 来源：E_F、NBANDS、NELECT、网格与各能带最小值都取自已校验的 scf/vasprun.xml、scf-summary.json、scf/KPOINTS；CHGCAR 与完成记录哈希一致。
- 输入一致：与 SCF 相同的 ENCUT/PREC/EDIFF/赝势；固定 ICHARG=11、LORBIT=11、LWAVE/LCHARG=.FALSE.。运行时重新生成并与审阅方案逐项比对，不同则拒绝启动。
- k 网格：SCF 网格 × kmesh_factor（建议 2）向上取整，Γ 中心。加密倍数是初始策略，不等于 DOS 已收敛。
- 积分方法：tetrahedron（ISMEAR=-5，Blöchl）要求自动生成的 Γ/MP 规则网格且不可约 k 点下界 ≥ 4；不满足即阻断，替代为增大 kmesh_factor，或由操作者明确选择 gaussian 并确认 sigma_eV。程序从不静默改为 Gaussian。
- 能窗：window_min_eV/window_max_eV 相对 SCF E_F（默认 −10/+5 eV）。VASP 5.4.4 的 EMIN/EMAX 是绝对能量，按 SCF E_F 换算并记录。EMIN 取窗口下限与“最低 SCF 本征值 − 余量”中较低者，使电子数积分覆盖半芯态（如 Cu_pv 3p、La 5s5p）。
- NEDOS = ceil((EMAX − EMIN)/sampling_interval_eV) + 1；0.01 eV 是采样间隔，不是分辨率。超过 20001 即阻断，替代为增大采样间隔。
- NBANDS：nbands=null 沿用 SCF；第 NBANDS 条带在 SCF 全部 k 点上的最小值须 ≥ 窗口上限 + empty_band_margin_eV，否则阻断，替代为增大 nbands 或缩小窗口上限；不能照抄 bands 的值。nbands 大于 SCF 能带数时覆盖情况运行前未知，由运行后检查判定。VASP 会把 NBANDS 向上取整到并行带组的倍数。

## 运行后（程序判定）

- outputs：总 DOS、积分 DOS、各原子 lm 投影齐全、长度等于 NEDOS、数值有限。
- parameters：实际 ICHARG、LORBIT、ISMEAR、NEDOS 与方案相同，NBANDS 不小于方案，能量网格首末与 EMIN/EMAX 一致，四面体法实际 k 点数 ≥ 4。
- electron_count：从 EMIN 积分到 DOS 计算的 E_F（VASP 积分 DOS 线性插值），与 NELECT 比较，容差 0.05 e/晶胞；积分下限须低于 DOS 计算最低本征值至少 0.5 eV（含半芯态），否则判失败。ISPIN=1 时总 DOS 已含自旋简并因子 2，不再乘 2。占据方式写明（四面体或 Gaussian 与 SIGMA）。占据数求和与梯形积分仅作参考。
- empty_bands：DOS 计算中最高能带在每个 k 点都高于 SCF E_F + 窗口上限 + 余量；不足判失败并提示增大 nbands。
- fermi_shift：记录 E_F(DOS) − E_F(SCF)，超过 0.1 eV 标记，不要求相等。
- projections：单位 states/eV/cell，投影到 PAW 球（RWIGS），按元素与 l 求和；不要求投影之和等于总 DOS，窗口内比值仅作说明。

## 输出与交付

dos.csv（energy_minus_scf_ef_eV、绝对能量、总 DOS、积分 DOS、元素/轨道投影，附 SCF 与 DOS 计算的 E_F 列）、dos.png/dos.pdf、dos-report.json、dos-plan.json；bands.csv 存在时另有 bands-dos.png/pdf，两图零点都是 SCF E_F（单独的 bands.png 对绝缘体可能以 VBM 为零点，合图不用）。report.json 增加 dos 字段。完成状态与交付按任务启用阶段判断；下载后 replot_bands 或 /plot replot 可本地重绘 DOS 与合图。
