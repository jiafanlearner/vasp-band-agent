# 服务器连接配置

Agent 将从登录脚本提取出的连接参数保存在本目录 `<名称>.json`，以后通过名称重用。不复制原始脚本，不执行其命令，不保存密码或私钥正文；私钥仍保存在用户原有位置，配置只保存路径。配置 JSON 已加入 Git 忽略。

对话示例：

> 请导入 C:\Users\researcher\works\服务器登录脚本\超算登录脚本\login_eshell111.bat，保存为 eshell111，并检查连接。

> 连接 eshell111，查看 /work/home/researcher/potpaw_PBE.64 里的目录。

> 查看 eshell111 的 Slurm 队列。

也可输入 `/servers` 查看保存项，`/connect eshell111` 检查连接。每次操作启动一个非交互 SSH 进程，重复使用保存的配置与本机 SSH 身份，不维持永久登录 shell。

支持含单条 `ssh [-i 私钥路径] [-p 端口] [-l 用户] [用户@]主机` 的 UTF-8 `.bat` / `.sh`。也支持 `ssh eshell111` 引用本机已配置别名。不会解释复杂脚本、跳板命令或密码自动输入。不要把私钥正文或密码贴入对话。

OpenSSH 必须已安装。首次主机指纹验证或加密私钥解锁，需要在本机终端完成；程序使用已有 known_hosts 和 ssh-agent，不绕过主机校验。

远程能力：环境检查、目录列表、计算输入/日志/报告读取、Slurm 队列查询，以及部署、准备、提交和结果下载。后者需要 `<名称>.compute.json` 计算配置，可用 `/setup-server <名称>` 探测生成，详见 docs/guides/CLUSTER_ONBOARDING.md 与 AI_USAGE.md。远程返回内容会作为工具数据发给当前模型接口。原 `/jobs`、`/cancel` 仍在程序所在机器执行，暂无远程取消工具。
