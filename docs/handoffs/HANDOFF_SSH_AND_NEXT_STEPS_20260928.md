# VASP Agent 交接：SSH 登录排障与后续工作

更新日期：2026-09-28。本文是工作记录与接手建议，不构成新的提交授权。后续以用户最新指令、实际代码和工具结果为准。

## 1. 当前最优先事项

用户在 VASP Agent 中检查服务器，两个配置 `eshell111`、`eshell111-vasp544` 均报：

```text
Permission denied (publickey,gssapi-keyex,gssapi-with-mic,password)
```

最近用户确认：**没有通过 `./start-agent.sh` 启动 Agent**。尚未说明实际启动命令，也尚未回答手动 SSH 登录是否需要账户密码或私钥口令。

上一条回复已请用户在 Git Bash 中运行：

```bash
cd /c/Users/js/works/codex/codex_official/vasp-band-agent
./start-agent.sh
```

预期启动输出指向 `C:\Program Files\Git\usr\bin\ssh.exe`。之后给 Agent 输入：

```text
只测试 eshell111-vasp544 的 SSH 连接，先不部署、不提交。
```

**目前还没有收到该操作结果，不能宣称修复完成。** 没用启动脚本只是排查线索，不能直接认定为根因，因为独立 Git SSH 测试也曾认证失败。

## 2. 本轮已实际验证的事实

- 本地私钥路径存在：`C:\Users\researcher\.ssh\example_hpc.key`，大小 1679 字节。本轮只检查文件元数据，没有读取私钥正文。
- 当前 Codex PowerShell 中默认 `ssh`、`ssh-add` 解析到 Windows OpenSSH：`C:\Windows\System32\OpenSSH\`。
- 当前进程没有 `SSH_AUTH_SOCK` / `SSH_AGENT_PID`；Windows `ssh-agent` 服务处于停止状态。
- Windows `ssh-add -l` 返回无法连接代理；Git `ssh-add -l` 也返回无法连接代理。该结果仅代表当前检查进程，不能代表用户另开的 Git Bash 窗口。
- 显式使用 Git SSH、`BatchMode=yes`、`StrictHostKeyChecking=yes`、已保存密钥和端口执行只读登录测试，一次返回上述认证失败。
- 显式 Windows SSH 测试，以及后续 Git SSH 调试测试，出现连接超时；因此同时观察到认证失败和连接不稳定。
- 调试连接尚未进入足以区分“服务器拒绝公钥”或“本地不能签名”的环节，不能认定密钥失效、被加密或账户变更。
- 后量子密钥交换警告不是本次登录失败的直接原因。
- 本轮未修改服务器配置、未部署、未提交作业；没有为 SSH 问题做猜测性代码修改。

## 3. SSH 实现与接手排查步骤

`start-agent.sh` 将 `C:/Program Files/Git/usr/bin` 放到 PATH 最前，并通过 Python `shutil.which("ssh")` 与 `os.path.samefile` 验证实际 SSH，再执行 `python ai_agent.py --profile relay "$@"`。

`remote_servers.py` 的 `Servers.ssh_args` 当前使用字符串 `ssh`，由进程 PATH 决定实际程序。参数包含：

```text
BatchMode=yes
StrictHostKeyChecking=yes
ConnectTimeout=30
ConnectionAttempts=1
ServerAliveInterval=15
ServerAliveCountMax=2
```

`run_ssh` 只对明确尚未建立 TCP 连接的错误重试，最多三次，等待 2/4 秒；认证失败不重试。不应把重试扩大到可能已执行远程写操作或提交的断线。

建议下一步：

1. 获取用户按启动脚本重试的结果，确认实际 SSH 路径；必要时确认用户原启动命令。
2. 确认用户手动登录是否要求密码或私钥口令，只问方式，不索要秘密。
3. 如果用户手动登录成功，在同一 Git Bash 环境比较非交互登录、代理状态与 Agent。Codex 的环境可能与用户终端不同。
4. 网络可达后，使用有限的 SSH 调试输出区分公钥提供、服务器接受、公钥签名和最终认证结果，不输出私钥正文。
5. 仅在证据明确后修复：可能是启动程序/环境不一致、代理不可见、密钥未解锁、服务端未授权该公钥或网络间歇故障。不要预设其中任何一种。
6. 如果改进程序，优先考虑可查看实际 SSH 程序与认证诊断；保持严格主机校验、非交互工具行为与提交幂等保护。增加针对性测试，不反复提交真实任务测试 SSH。

手动测试命令（用户自行在终端执行；可能要求交互认证）：

```bash
ssh -p 22 -i 'C:/Users/researcher/.ssh/example_hpc.key' researcher@hpc.example.org
```

## 4. 项目位置与敏感信息

- 项目：`C:\Users\researcher\works\codex\codex_official\vasp-band-agent`
- 当前宿主：Windows，Codex 命令环境是 PowerShell，用户常用 Git Bash。
- 此前检查该目录不是 Git 仓库；接手时自行确认，不覆盖已有未提交文件。
- `.env` 含真实 API 密钥，不要整体读取或打印。不要复制私钥进交接材料。
- 两个服务器配置同主机、账户、端口和密钥，但**计算环境不同**。
- 主机：`hpc.example.org`；端口：22；账户：`researcher`。
- 原导入脚本：`C:\Users\researcher\works\服务器登录脚本\超算登录脚本\login_eshell111.bat`。无需执行或全文展示它来验证当前保存配置。
- SSH 配置在 `servers/<name>.json`，计算配置在 `servers/<name>.compute.json`。

## 5. 科学计算环境及不能丢失的限制

原 `eshell111` 环境包含 FixZ 标记，与三维完整晶胞优化冲突；真实运行曾仅改变 z 方向，不能通过放宽阈值解决。新版环境预检会阻断此类 cell 优化。

候选环境 `eshell111-vasp544` 使用：

```text
/work/home/researcher/vasp-agent-remote/environments/vasp544-public.sh
```

该包装脚本加载公共 VASP 5.4.4 模块。真实任务已验证三方向晶胞响应，但这不等于证明二进制绝无私有补丁或来源完全可信。

共同工作根目录：`/work/home/researcher/vasp-agent-remote`；PBE 库：`/work/home/researcher/potpaw_PBE.64`。

用户曾授权 Si 流程：三维、非磁性、电中性 PBE，NBANDS=16，无 SOC、DFT+U、杂化；最多 16 核、1 节点、4 小时。**历史授权不代表当前程序会话拥有提交权限**，完成实验后已关闭提交开关。此次交接优先排查登录，不需要重跑计算。

## 6. 已完成任务：避免重复计算

### 完整 Si 任务

`eshell111-vasp544 / si-public-20260926` 已完成完整工作流：

| 阶段 | 作业 ID | 记录结果 |
|---|---|---|
| convergence | 70004053 | COMPLETED，0:0 |
| relaxation 与优化后复核 | 70004419 | COMPLETED，0:0 |
| scf | 70109758 | COMPLETED，0:0 |
| bands | 70109937 | COMPLETED，0:0 |

- 记录晶格常数 5.46874444 Å；有限采样路径上 PBE 间接带隙 0.6105 eV，不能当作实验带隙或全布里渊区极值保证。
- 本地目录：`downloads/eshell111-vasp544/si-public-20260926-1cd66915/`。
- 原图纵轴参考标记曾有问题，原件保留；另生成 `bands-vbm.png/pdf` 与 `plot_verified.py`，明确以 VBM 为零，计算数据未更改。
- 详情：`reports/runs/RUN_SI_20260925.md`。

### 可靠性回归任务

`eshell111-vasp544 / si-recovery-check-20260927`：作业 `70125881` 已完成，仅运行 convergence，六个子步骤均正常结束，ENCUT 与 mesh 独立解析均通过。下一阶段 relaxation 待审阅，**没有自动继续**。

- release：`5ad0f0e03ad3611c`。
- 16 MPI、1 节点、10 分钟。
- 本地：`downloads/eshell111-vasp544/si-recovery-check-20260927-0005fe68/`。
- 下载资料标记 partial，原因是只测收敛阶段，不是回归失败。
- 证据：`reports/regression/REGRESSION_CLUSTER_20260927.json`、`reports/regression/REGRESSION_FIXZ_CHECK_20260927.json`。

以上为已保存的核验记录，本轮 SSH 失败期间未重新访问集群确认状态。

## 7. 最近已做的代码改进

详细阅读 `docs/handoffs/HANDOFF_NEW_FEATURES.md`、`docs/handoffs/HANDOFF_AGENT_TOOL_RECOVERY.md` 和 `docs/development/RUN_RELIABILITY_UPDATE.md`，不要把历史待办自动当成当前仍未完成。

主要已有功能：

- 专用输入摘要、赝势元数据读取与工具错误恢复指引；查看摘要不应错误要求 `/physics`，正式放行仍检查条件。
- 参数建议与真实默认值比较，由菜单选择；固定已收敛参数、仅扫描另一参数的重试及来源证据校验。
- 提交前资源表单、多节点资源、授权上限、逐阶段审阅与提交。
- squeue 查询旧作业失败后用 sacct 核验；未知状态不能当作完成。
- 资源操作回执恢复、提交响应丢失后的只读核查、避免重复 sbatch、同一审阅幂等。
- 环境及二进制指纹、FixZ 预检、优化失败诊断、能带验收与下载哈希回执。

### 上下文管理

`context_budget.py`、`context_compaction.py`、`model_config.py`：按估计 token 输入预算触发压缩，计入指令、工具和历史；为输出和安全余量预留空间。估计不是精确 tokenizer 结果，会使用返回的用量校准。

- 代码默认：context_window=32768、output_reserve=4000、context_safety=2048、compact_trigger=0.8、compact_target=0.55。有效值应查当前配置，不要凭文档假定。
- `/context` 查看预算，`/compact` 手动压缩；原始对话保留，超长工具输出可存证据文件并分页读取。
- 用户提到中转站支持约 258k 窗口；此前建议过 240000/8000/16000/0.8/0.55，但**没有依据该建议代替用户修改配置**，实际值接手时核对。
- 历史完整回归记录 266 项通过，此后还有针对性用例；不是本次重新跑出的测试结果。

### Git Bash 路径识别

最近修复 `/c/Users/.../Si.POSCAR 这是结构...` 被误认成未知斜杠命令的问题：

- `workspace_paths.py` 识别路径开头输入；在 Windows 将 Git Bash `/c/...` 转为 `C:/...`。
- `chat_commands.py` 将路径开头自然语言交给模型，保留真正未知命令处理。
- 本地工具与远程准备共用工作区路径约束，禁止越出工作区。
- 针对性验证曾运行 13 项通过，涉及路径、命令分流和远程准备。

## 8. 给接手对话的第一条建议

先阅读本文和 `start-agent.sh`、`remote_servers.py`，确认用户用脚本启动后的连接结果。当前证据不足以确定认证失败根因；不要直接声称换启动方式一定修好。先恢复只读连接，再决定是否需要改进 SSH 诊断代码。保留已有任务与科学验收，不提交重复计算。
