# 收敛比较诊断（2026-10-08）

本次仅改进结果说明，不改变验收判据，不改写已有任务或提交授权。

`compare` 保留 `passed` 和原有三个差值字段，增加 `comparison_policy`、
`effective_thresholds`、`checks`、`failed_checks`、`failure_reasons`。
每项检查包含差值、阈值、单位、是否启用及通过情况；未启用项的 passed 为 null。

扫描及优化后精度复核比较使用 energy_tol、force_tol/2；cell 模式使用
stress_tol/2，ions 模式不启用应力差检查。优化后残余力、应力验收仍使用完整容差。
扫描要求相邻两组比较都通过。默认有效门槛为 1 meV/atom、0.005 eV/Å、0.5 kbar。
因此 0.581 kbar 应力差未通过，不能拿优化残余应力门槛 1 kbar 判为通过。

重启本地 Agent 后提示规则生效。远程结果新增字段需要使用新版核验程序；
不自动修改任务绑定的旧 release。可用 inspect_convergence 从已有真实输出重新核验，
无需为了补充解释重算 VASP。新核验结果不等于已验证历史部署版本。
本次未部署远程程序、连接服务器或提交计算。
