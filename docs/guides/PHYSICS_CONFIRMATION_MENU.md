# 当前回合的物理模型确认

模型可调用 `request_physics_confirmation(server, job_name, proposed_goal)`，
使用现有交互菜单输入后端，显示目标任务及固定支持范围：三维体相与二维 slab（真空沿 c 轴）、电中性、非磁性 PBE，
不含磁性、SOC、DFT+U、杂化泛函及一维/零维体系。操作者可确认建议目标、修改目标后确认，或取消。
修改研究目标文字不能扩展支持的物理模型。

对于新任务，交互会话的 `/plan` 三合一启动核对窗口（区块 A）也支持在参数与首阶段资源核对时一并确认物理模型与研究目标，
并在 `prepare_scan_plan` 落盘后通过同一 `confirm_physics` 通道写入 `physics-model.json`（`confirmed_by='operator_chat_menu'`），
无需再重复弹出 `request_physics_confirmation`；既有任务、旧任务或独立确认仍使用 `request_physics_confirmation` 与 `/physics`。

确认结果回传模型，同一回合继续阶段审阅；无需再输入 `/physics` 或发送“继续”。
取消、EOF、Ctrl+C 或没有交互输入后端时不确认。已有有效确认时读取返回，避免重复菜单。
审阅和提交授权仍独立；确认模型不会提交计算。

`/physics` 仍可离线使用，与新工具共用保存接口。为兼容旧远程引擎，
独立确认的持久化 `confirmed_by` 保留 `operator_slash_command` 标记（`/plan` 三合一窗口使用 `operator_chat_menu`），均表示操作者本地菜单确认；
工具回执及审计记录实际菜单行为，不允许模型参数代替操作选择。

重启本地 Agent 后可使用。远程复用已有 stage_context/confirm_physics 接口，
无需仅为这个菜单重新部署计算引擎。
