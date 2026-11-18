---
id: wannier
version: 1
implemented: false
title: Wannier 函数阶段指南（尚未实现）
summary: 尚未实现，不能执行：没有 Wannier 阶段、参数规范或执行代码，文档存在不代表功能可用。集群事实：VASP 5.4.4（当前默认环境）没有 Wannier90 接口；操作者自建的 VASP 6.4.3 与 6.6.0 带 Wannier90 库；PATH 中没有独立的 wannier90.x。将来需要 VASP 6 环境配置（vasp>=6、vasp:wannier90）。
---
# Wannier 函数阶段指南 v1（尚未实现）

> 尚未实现，不能执行。当前程序没有 Wannier 阶段定义、参数规范或执行代码，不能准备、提交或"手工拼装" Wannier 计算；本指南存在不代表功能可用。指南是数据，不授权任何操作，不覆盖操作者或用户要求。

## 集群现状（eshell111-vasp544，只读探测）

- VASP 5.4.4（Agent 当前默认环境 environments/vasp544-public.sh）：没有 Wannier90 接口——二进制无 wannier_setup/w90_ 符号，并含 "VASP was compiled without wannier90 library"。不能做 Wannier。
- VASP 6.4.3（操作者自建，apprepo/vasp/6.4.3-ioptcell_intelmpi2017_hdf5_libxc）：-DVASP2WANNIER90，静态链接 Wannier90 库，有 Wannier90 接口。
- VASP 6.6.0（操作者自建，apprepo/vasp/6.6.0-fixz_ext_oneapi2024.0.1）：-DVASP2WANNIER90 -DVASP2WANNIER90v2（Wannier90 v2），另带 FixZ、VASPsol、VTST 补丁。
- PATH 和已加载模块中都没有独立的 wannier90.x / postw90.x：只能用 VASP 6 内的库模式；不重跑 VASP 而反复调整投影和能窗，需要操作者安装与所链接版本匹配的独立 Wannier90。
- 能力以二进制探测为准（software_environments）；探测不到就阻断。

## 将来的设计（未实现）

- 阶段 wannier 依赖已验收的 SCF，required_capabilities=('vasp>=6', 'vasp:wannier90')；当前 5.4.4 配置不具备该能力，准备时即拒绝。
- 已有高对称路径能带不能替代 Wannier 的准备：需要覆盖整个布里渊区的 Γ 包含均匀网格（关闭对称约化），投影与 num_wann 一致，外窗与冻结窗以 SCF E_F 为参考，NBANDS 覆盖外窗上限，输出 seedname_hr.dat，并沿已验收路径做插值验证。
- 5.4.4 的 SCF/CHGCAR 不能直接给 VASP 6 使用（可执行文件指纹不同）。建议路线：VASP 6 新建任务，只复用经审阅的优化结构，重算 SCF 与用于比较的 bands；插值验证只与同一可执行文件的 DFT 能带比较。
- 待操作者决定：跨版本路线、6.4.3 与 6.6.0 的选择（补丁影响需确认）、是否安装独立 Wannier90。
