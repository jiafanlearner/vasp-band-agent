# 新集群接入：从 SSH 登录到第一次准备任务

本页面向第一次把 Agent 接到自己 Slurm 集群的用户。整个过程在 Agent 对话里完成，不需要手写 `servers/<名称>.compute.json`，也不需要自己在集群上配 Python 环境。

本机仍需：OpenSSH，能用密钥免密码登录集群（首次主机指纹确认、加密私钥解锁在本机终端完成）；本机 Python 和项目依赖（见 README）。集群上需要：Slurm、可用的 VASP（环境脚本或 `module`）、PBE POTCAR 库。

## 1. 导入 SSH 连接

在对话里让 Agent 导入登录脚本（只含一条 `ssh` 命令的 `.bat` / `.sh`），例如：

> 请导入 C:\path\to\login_mycluster.bat，保存为 mycluster，并检查连接。

结果保存为 `servers/mycluster.json`，只记录主机、端口、用户和密钥路径，不复制密钥。细节见 [servers/README.md](../../servers/README.md)。

## 2. `/setup-server mycluster`

向导先做一次只读探测（约 1–3 分钟），然后逐项给出候选，每一步可以输入编号选择、`e` 手动输入、回车采用默认值、`q` 取消：

| 步骤 | 探测来源 | 说明 |
|---|---|---|
| 1 远程根目录 | `$HOME` | 默认 `$HOME/vasp-agent`；任务、release、虚拟环境都在这里 |
| 2 Python | `which python3`、conda 环境、`~/miniconda3` 等、`module avail python/anaconda` | 显示每个解释器能否导入 pymatgen/numpy/matplotlib 及版本；`v` 见第 3 节 |
| 3 VASP 环境 | 环境脚本（能解析出 `vasp_std` 的才列出）、`module avail` 中的 vasp、登录 PATH | 选模块或 PATH 时在根目录生成 `vasp-env.sh` |
| 4 POTCAR 库 | 含 `Si/POTCAR` 的元素子目录 | 只检查文件是否存在，不读取 POTCAR 内容；请确认选的是 PBE 库 |
| 5 分区 | `sinfo`、`scontrol show partition` | 显示每节点核数、默认时限、最长时限 |
| 6 核数 | 所选分区每节点核数 | 单节点作业，1–256 |
| 7 运行时限 | 分区默认时限 / 最长时限 | HH:MM:SS |

探测不会启动 VASP：内容含 `mpirun`、`srun`、`sbatch` 或直接调用 `vasp_std` 的脚本不会被 source。

最后会列出“当前 / 新值”对照表，以及要在集群上写入的环境脚本（如有）。输入“写入”后才会写 `servers/mycluster.compute.json`；已有配置先备份为 `mycluster.compute.json.bak-时间戳`，其中的其他字段（如 `parallel_tests`）保留。校验规则与运行时相同：四个路径必须是绝对路径，分区名只含字母/数字/`_`/`-`，时限为 HH:MM:SS。

## 3. 集群 Python 依赖（可选）

如果没有现成的 Python 能导入这三个包，在第 2 步输入 `v`：

1. 从探测到的 Python ≥ 3.10 中选一个基础解释器，只用它创建 `<根目录>/venv`，不向它安装任何东西。
2. 选择安装方式：
   - **集群联网**：在集群上用 pip 从 PyPI 安装；
   - **集群无外网**：本机按集群 Python 版本、CPU 架构和 glibc 版本运行 `pip download`（只下载 wheel），打包上传到 `<根目录>/wheels/cpXY-架构/`，再用 `--no-index` 离线安装。本机需要能访问 PyPI。
3. 版本固定为 `requirements-tested.txt` 中的 pymatgen、numpy、matplotlib，其余依赖由 pip 解析。完成后列出本次安装或变更的每个包及版本（安装前后 `pip freeze` 对比），并验证能导入。

全程不使用 sudo，也不改动系统或基础 Python。安装成功后 Python 一项自动填为 `<根目录>/venv/bin/python`。

## 4. 部署引擎，并保持最新

写入配置后，向导会比较集群上已部署的引擎版本和本机源码版本，未部署或版本不同时询问是否立即部署（上传引擎源码到 `<根目录>/releases/<版本>` 并检查 Python 依赖）。

以后每次在集群上准备或预览任务前，Agent 都会做同样的比较。本机更新代码后，如果集群还是旧版本，会显示两个版本号，并给出选项：部署本机版本后继续、沿用已部署版本继续（本次会话对这对版本不再询问），或取消。已准备的任务始终绑定各自准备时的 release，重新部署不影响它们。

`/servers` 会列出每台服务器的本机版本、已部署版本和状态（最新 / 需重新部署 / 未部署 / 无计算配置）。

## 5. 检查与下一步

- `/connect mycluster`：只读连接测试。
- 对话中让 Agent 检查集群环境（`check_remote_environment`），确认 VASP 可解析且没有晶胞约束标记。
- 之后按 [AI_USAGE.md](AI_USAGE.md) 准备、确认物理模型、审阅并提交任务。

需要修改配置时再运行 `/setup-server mycluster`：当前值会作为默认值，回车即保留。
