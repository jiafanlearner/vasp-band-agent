# eshell111 集群配置

本页环境表和“尚未提交”等叙述记录早期核验状态，不是当前状态。完整 Si 历史记录见 [实跑报告](../../reports/runs/RUN_SI_20260925.md)。原 `eshell111` 中的 FixZ 标记与三维完整晶胞优化冲突；不要将下面的历史 Slurm 示例直接当作当前推荐环境。

2026-09-28 源码已归入 `src/vasp_agent/`，自动部署入口会生成固定白名单的科学引擎包；旧任务绑定的 release 不变。本次目录整理未部署或提交。

v0.6 新任务采用阶段放行，先在本机 `/physics` 确认，再由 AI 审阅并逐阶段提交；旧任务不迁移。详见 AI_USAGE.md。

已通过现有 SSH 连接部署计算源码及独立 Python 环境。远程根目录为 `/work/home/researcher/vasp-agent-remote`，Python 为该目录下的 `venv/bin/python`。任务 `si-remote-check-20260922` 已使用服务器 PBE 赝势准备完成（`ready: true`），状态查询和准备记录回传通过；尚未提交作业。优先使用 [AI_USAGE.md](AI_USAGE.md) 中的本机 AI 远程工具。

| 项目 | 已核验值 |
|---|---|
| SSH 别名 | `eshell111` |
| 调度器 | Slurm |
| 当前可见分区 | `xahcnormal` |
| VASP 环境脚本 | `/work/home/researcher/apprepo/vasp/6.6.0-fixz_ext_oneapi2024.0.1/scripts/env.sh` |
| 硅赝势 | `/work/home/researcher/potpaw_PBE.64/Si/POTCAR` |
| Python 3.12.4 | `/work/home/researcher/apprepo/phonopy/2.26.6-none/app/phonopy-2.26.6/bin/python3` |

未复制或读取私钥正文。登录凭据沿用本机 SSH 配置。

以下为手动部署的替代步骤，现有远程环境无需重复执行。示例目标为新的 `/work/home/researcher/vasp-band-agent-v1`。若目录已存在，请先检查，避免覆盖。

本机 PowerShell，从项目目录执行：

```powershell
ssh eshell111 'mkdir /work/home/researcher/vasp-band-agent-v1'
scp vasp_band_agent.py ai_agent.py pyproject.toml README.md requirements-tested.txt eshell111:/work/home/researcher/vasp-band-agent-v1/
scp -r src/vasp_agent docs examples eshell111:/work/home/researcher/vasp-band-agent-v1/
```

登录集群后创建独立 Python 环境：

```bash
cd /work/home/researcher/vasp-band-agent-v1
mkdir -p src
mv vasp_agent src/vasp_agent
/work/home/researcher/apprepo/phonopy/2.26.6-none/app/phonopy-2.26.6/bin/python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-tested.txt
.venv/bin/python -m pip install -e .
```

安装需要可访问软件源；若集群禁止联网，需要另行配置站点镜像或离线依赖包。Python 路径虽然来自 phonopy 安装，此处只调用解释器，不修改该软件环境。

先准备硅演示任务，再提交（8 核、4 小时是待评估的初始资源配置，不保证具体排队或运行时间；完整模式正常路径包含 13 个子计算）：

```bash
.venv/bin/python vasp_band_agent.py prepare examples/Si.POSCAR runs/si-real --potcar /work/home/researcher/potpaw_PBE.64/Si/POTCAR --nbands 16
export AGENT_DIR=/work/home/researcher/vasp-band-agent-v1
export WORK_DIR=$AGENT_DIR/runs/si-real
sbatch examples/eshell111.slurm
```

查询：`squeue -u researcher`。作业结束后，在本机下载报告：

```powershell
scp 'eshell111:/work/home/researcher/vasp-band-agent-v1/runs/si-real/bands.*' .
scp eshell111:/work/home/researcher/vasp-band-agent-v1/runs/si-real/report.json .
```

真实材料应替换结构与 POTCAR，并先验证结构、磁性假设和参数收敛。当前未确认用户目标材料；硅仅为可运行示例，不代表最终研究体系。

## 2026-09-24 摘要与恢复更新的部署边界

本次开发仅做本机代码与离线模拟测试，未重新验证本页历史集群状态，也未部署或提交真实任务。

客户端更新后需重启 `python ai_agent.py --profile relay`。远程准备摘要和哈希/错误校验依赖科学工作流模块。它们已由包安装及远程引擎白名单覆盖；后续经用户要求部署时，应完整部署引擎包，不单独替换一个模块。源码新位置见 [项目目录说明](../development/PROJECT_LAYOUT.md)。

`remote_worker.py` 每次通过 SSH stdin 发送；它仍从任务绑定的 release 加载核心模块。新部署用于之后的新任务，不会自动升级已有任务。旧 release 缺少 `preparation_summary` 时明确标注摘要不可用，不假装已经核验，也不修改 release 绑定。详见 AI_USAGE.md 的“准备摘要与只读错误恢复”。
