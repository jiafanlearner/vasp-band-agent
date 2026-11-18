---
id: dos
version: 1
implemented: false
title: 三维态密度（DOS）阶段指南
summary: DOS 阶段在步骤 C 实现，当前不能保存方案、准备或运行；可用 preview_scan_defaults(optional_stages=["dos"]) 查看参数规范 dos v1。规范要点：k 网格=SCF 网格×倍数（初始策略，不证明收敛）；三维且前置条件满足用四面体法，否则阻断或由操作者明确选 Gaussian；能窗相对 SCF E_F；NEDOS 由采样间隔算出（不是分辨率）；空带须覆盖窗口；LORBIT=11。
---
# 三维态密度（DOS）阶段指南 v1

> 尚未实现：DOS 阶段在步骤 C 实现。当前只能查看参数规范，不能保存方案、准备任务或提交 DOS 计算。本指南是数据，只解释程序检查什么；不授权提交、不放宽阈值、不覆盖操作者或用户要求，验收由代码判定。

## 流程

1. 新任务用 propose_scan_plan 的 optional_stages 选择 DOS；optional_stages 只负责选择阶段，参数走阶段参数规范。
2. 规范 dos v1 给出每个参数的类型、单位、范围、建议值及依据；工具结果附完整规范，模型描述只保留一句话。
3. 操作者在 /plan 菜单中采用规范建议、采用 AI 建议或逐项修改；没有操作者选择，方案保持 pending，不能准备。
4. 执行端按规范严格校验：未知参数拒绝，只接受数值和列出的选项，不接受路径、命令或自由文本。
5. 选定值写入 config.json 的 stage_parameters，进入 manifest 哈希和每个审阅快照；DOS 输入在阶段启动时由已验收的 SCF 证据生成，并与方案比对。

## 参数与依据

- k 网格：SCF 网格（标准原胞，按倒空间间距换算）每个方向乘 kmesh_factor（建议 2）并向上取整。加密倍数是初始策略，不等于 DOS 已收敛；报告必须写明。
- 积分方法：三维、自动生成的 Γ/MP 规则网格、不可约 k 点数 ≥ 4 时用 Blöchl 修正四面体法（ISMEAR=-5）。运行前按下界估计 k 点数，运行后读实际数复核。不满足就阻断，并给出替代：加密网格，或由操作者明确选择 gaussian 并确认 sigma_eV。程序从不静默改为 Gaussian。
- 能量窗口：参考能量为 SCF 的 E_F（零点）；window_min_eV/window_max_eV 相对它，默认 −10/+5 eV，操作者可调。窗口用于绘图和投影 DOS，不是电子数核验的积分范围（核验从最低能带之下积分，另行设定）。VASP 5.4.4 的 EMIN/EMAX 是绝对能量，引擎按 SCF E_F 换算并记录。
- NEDOS：NEDOS = ceil((上限 − 下限)/sampling_interval_eV) + 1。0.01 eV 是采样间隔，不是分辨率；实际分辨率受 k 网格和积分方法限制，报告不得称为分辨率。
- NBANDS：各 k 点最高能带须高于 E_F + 窗口上限 + empty_band_margin_eV。运行前用 SCF 占据审计估计，运行后按 DOS 计算复核；不足时增加 NBANDS（非自洽计算可多于 SCF），不能照抄 bands 的值。VASP 会把 NBANDS 向上取整到并行带组的倍数。
- 固定：ICHARG=11（读已验收 SCF 的 CHGCAR）、LORBIT=11、LWAVE=.FALSE.。

## 能量参考

DOS 与能带合图的零点取 SCF 的 E_F：电荷密度固定为 SCF 结果，SCF 的 E_F 是唯一自洽得到的参考，bands.csv 也以它为零点。DOS 计算重新求得的 E_F 及其与 SCF 的差值另行记录，不要求严格相等：金属中差值反映网格与方法差异，超过记录阈值时标记而不判失败；绝缘体中 E_F 在带隙中的位置是任意的，同时记录 VBM。

## 运行后检查（步骤 C，由程序判定）

- 输出完整、数值有限；实际 INCAR/KPOINTS、k 点数和 ISMEAR 与审阅方案一致。
- 电子数核验两条口径：占据数求和与 NELECT 比较；DOS 从所有价电子态之下积分到 DOS 计算的 E_F，记录积分范围、占据方式和归一化。
- 投影 DOS 说明单位（states/eV/cell）与投影球半径来源；投影只覆盖原子球，不要求投影之和等于总 DOS。
- 二维 DOS 尚未开放，首版对 2D 任务拒绝。
