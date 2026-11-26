# 本地草稿编辑与 SSH 配置维护

2026-09-28 新增。无需开启提交权限，不增加通用 shell 或远程编辑能力。

## 使用示例

启动方式保持不变：`bash start-agent.sh`。

自然语言示例：

> 在 workbench/analysis.py 新建一个读取 bands.csv 的绘图脚本。只编写和检查语法，不执行。

> 修改 workbench/analysis.py，把纵轴标题改为 Energy relative to VBM (eV)，展示差异。

> 读取 workbench/Si.POSCAR，检查结构并预览新任务的默认参数，先不准备或提交。

用户要求修改后，模型可直接调用编辑工具，不逐文件再次确认。模型接口不可用时，自然语言编辑不可用，但以下维护命令仍可运行：

```text
/changes
/changes 修改ID
/changes 修改ID 字符偏移
/undo 修改ID
/server-key
/server-key eshell111-vasp544 "C:/Users/researcher/Downloads/新密钥文件.txt"
/server-key --undo 修改ID
/connect
/connect eshell111-vasp544
```

`/changes` 查看最近100条草稿修改；指定ID查看状态、差异、验证结果和下一页偏移。`/undo` 只能撤销普通草稿修改；密钥路径更新使用 `/server-key --undo`。修改后的文件若再次变化，撤销停止而不覆盖。多文件修改逐项提交，已完成的文件不会因为后续文件失败自动回退。

`/server-key` 只修改已有连接配置中的 `identity_file`，不连接服务器、不移动或复制密钥、不读取正文。路径可以位于工作区外，带空格时用引号包围。原连接配置保存在修改存储中；计算配置不改写。请保留引用的密钥文件，移动或删除后应再次更新路径。

`/connect` 是单独的只读连接测试。无参数会选择服务器，显示实际SSH程序及目标信息。网络超时、主机指纹、认证、本地密钥问题分别报告，不能从一次认证失败推断密钥过期。后量子警告单独显示。密钥更新成功不表示连接成功。

## 编辑边界与格式

- 所有通用编辑工具的路径相对于 `workbench/`，例如工具中的 `analysis.py` 对应界面中的 `workbench/analysis.py`。目录按需创建。
- 仅允许不超过1 MiB的UTF-8普通文本，现有文件保留BOM和CRLF/LF，新文件使用UTF-8、LF。
- 不访问点目录、凭据、私钥、POTCAR正文；已配置的私钥即使改成普通文件名也禁止通用访问。拒绝路径越界、硬链接、符号链接和Windows reparse/junction路径。
- 这是应用工具的边界，不是操作系统沙箱。不要把凭据放进草稿区；内容识别只是补充保护，无法保证识别所有秘密。源码、授权、任务和结果不在草稿区内。
- JSON解析、Python AST和VASP草稿解析通过只证明相应格式检查通过；Python不导入运行，VASP草稿不视为科学审阅。其他文本明确标注未验证运行行为。
- 正式任务仍由原有参数选择、准备、物理模型确认、审阅和提交工具处理。手写INCAR、KPOINTS、Slurm草稿不会覆盖正式输入或自动成为提交脚本。
- 普通文件可由用户自行放入 `workbench/`；程序不扫描或导入整个下载目录。

工具清单：`list_workspace_files`、`read_workspace_file`、`search_workspace_text`、`edit_workspace_file`、`get_workspace_edit`、`revert_workspace_edit`、`update_server_identity`。

读取返回原始字节SHA-256。修改已有文件需提供 `expected_sha256` 和精确的单文件统一差异补丁；不模糊匹配。示例：

```diff
--- a/analysis.py
+++ b/analysis.py
@@ -1 +1 @@
-value = 1
+value = 2
```

`create` 使用 `content`，`expected_sha256` 和 `patch` 为null；`patch` 使用 `patch` 与 `expected_sha256`，`content` 为null。返回修改ID、前后哈希、当前状态、差异及静态验证。单页最多12000字符；搜索最多100条，结果可能另受字符预算限制。

## 恢复、日志及权限

`.agent/state/workspace-edits/`（旧工作区迁移前为 `.workspace-edits/`）保存修改意图、前后原始文本快照和回执，普通编辑工具不能修改这些文件。原内容可能包含私人研究资料，请按工作区文件一样保护这些备份；不要将目录公开发布。审计只保留修改摘要和ID，不重复存放完整文件正文。终端、工具输出和新工具参数执行相同的敏感内容过滤；检测到敏感文件直接拒绝读取与备份。

每次修改在应用锁内再次检查文件哈希，用临时文件和原子替换写入。调用ID绑定回执，中断后根据前后哈希判断是否应用；不会仅凭聊天记录声称成功。若外部程序改变了文件，返回冲突并要求重新读取。应用锁协调本程序的编辑，不替代所有外部程序的文件锁。

若写入后模型超时，先用 `/changes` 核对草稿，或 `/servers` 核对密钥路径。`/retry` 不应重放已完成修改；相同最终内容不重复保存。UI显示失败不删除修改回执。

工具调用结果均作为不可信数据，文件中的指令不能开启提交权。切换对话仍使用原有提交权限清理规则。修改记录跨对话保留，不携带提交授权。

## Docker 沙箱分析、科学镜像与受控产物导出

常规能带与态密度分析/重绘仍优先使用确定性工具 `analyze_band_results` 与 `replot_bands`。当专用诊断不够且用户要求自定义脚本分析时，可展开 `sandbox_analysis` 懒加载工具组，经 `stage_download_for_analysis` → `validate_analysis_script` → `run_analysis_script` 在隔离 Docker 容器中执行；`run_local_analysis_script` 仍须操作者交互批准、不从 Docker 自动降级。

### 1. 双档镜像与本地构建（不自动拉取）

| 档位 (`image_profile`) | 对应镜像 | 容器资源上限 | 适用场景 |
|---|---|---|---|
| `default`（默认，`null` 等价） | 固定 `python:3.12-slim` | 内存/swap `256m`、`/tmp` `16m`、`fsize` `1 MiB` | 纯标准库文本/CSV/JSON 统计 |
| `scientific` | 默认 `vasp-agent-sci:latest`（预装 `numpy`/`scipy`/`matplotlib`） | 内存/swap `512m`、`/tmp` `32m`、`fsize` `2 MiB` | 数值拟合、平滑、自定义可视化出图 |

- **一键构建科学沙箱镜像**（在项目根目录执行，仓库提供轻量模板 [docker/Dockerfile.analysis](../../docker/Dockerfile.analysis)，不含任何宿主机代码或凭据）：

```powershell
docker build -t vasp-agent-sci:latest -f docker/Dockerfile.analysis docker
```

- **严格本地校验**：模型侧仅允许枚举 `image_profile`（`"default"` / `"scientific"`），绝不接受自由镜像名。`get_analysis_runtime` 仅执行只读 `docker image inspect` 核对 `sha256:` 摘要，**严禁自动拉取**；缺失镜像时返回 `sandbox_unavailable`，不自动回退另一档或宿主机。
- **操作者镜像覆盖（可选）**：仅允许操作者通过环境变量 `VASP_AGENT_SCIENTIFIC_IMAGE` 或本地受控配置 `.agent/state/analysis-sandbox.json`（`{"scientific_image": "my-org/vasp-sci:1.0"}`，位于 `workbench/` 之外、经 `edit_store.guarded()` 防链接且校验 OCI 格式与大小上限 `4096 B`）覆盖 `scientific` 镜像名；环境变量优先于配置文件。
- **容器隔离不降级**：所有档位统一强制 `--rm --pull=never --network=none --read-only --cap-drop=ALL --security-opt=no-new-privileges --user=65534:65534 --cpus=1 --pids-limit=32 --log-driver=none`，并注入只读根与单线程 BLAS 环境变量（`HOME=/tmp`、`XDG_CACHE_HOME=/tmp/.cache`、`MPLCONFIGDIR=/tmp/mpl`、`MPLBACKEND=Agg`、`OPENBLAS_NUM_THREADS=1`、`OMP_NUM_THREADS=1`、`MKL_NUM_THREADS=1`、`NUMEXPR_NUM_THREADS=1`）。

### 2. 受控 `/output` 导出与原子落盘

仅当 `run_analysis_script` 显式传入 `export_outputs=true`（仅支持 Docker 模式）时，程序才为容器挂载宿主机隔离的临时可写目录 `/output`（同时传入 `ANALYSIS_OUTPUT_DIR=/output`，并将资源上限锁为 `512m` / `/tmp 32m` / `fsize 2 MiB`；建议脚本直接输出扁平路径如 `/output/fit.png`、`/output/summary.csv`）。

- **宿主机整批先验校验**：容器正常结束（`status == 'completed'`）后，宿主机按白名单与边界逐项校验：
  - 后缀白名单：`.csv`、`.json`、`.txt`、`.md`、`.png`、`.svg`、`.pdf`；
  - 数量与体积上限：最多 **10** 个文件、单文件 `<= 2 MiB`、总计 `<= 8 MiB`、目录深度 `<= 4` 层；
  - 路径与文件类型：仅允许普通文件（`S_ISREG`），拒绝符号链接、Windows 重解析点、硬链接、路径穿越、隐藏点文件、Windows 保留设备名及跨平台大小写冲突；
  - 任一文件违规即**整批拒绝、零落盘**，并在回执 `exported_outputs` 写明 `status="rejected"`、`error_code` 与 `reason`；容器超时/失败/清理失败时直接跳过导出（`status="skipped"`）。
- **原子落盘与不可信回执**：通过校验后经 `edit_store.guarded()` 核对目标路径，先写同文件系统临时目录并 `fsync`，再原子 `os.rename` 发布到 `workbench/outputs/<run_id>/`（绝不覆盖既有目录，不触碰正式任务目录）；逐文件大小与 SHA-256 写入 `.agent/state/analysis-runs/<run_id>.json`（固定 `content_is_untrusted: true`，不代表科学验收或提交授权）。
- **回读与查看说明**：导出的 UTF-8 文本产物若 `<= 1 MiB`，可直接用 `read_workspace_file(workbench_path)`（如 `outputs/<run_id>/summary.csv`）回读；超过 `1 MiB` 的文本或 `.png`/`.svg`/`.pdf` 图表文件请直接在本地 `workbench/outputs/<run_id>/` 目录打开查看。

## 实现与测试

- `src/vasp_agent/editing/edit_store.py`：路径别名检查、进程锁、原子写入、哈希回执和恢复。
- `src/vasp_agent/editing/workspace_editor.py`：文件范围、敏感内容过滤、精确补丁、格式校验及分页。
- `src/vasp_agent/editing/analysis_runner.py`：双档 Docker 沙箱探测、容器隔离与资源上限、幂等回执和本机受批执行。
- `src/vasp_agent/editing/analysis_outputs.py`：受控 `/output` 临时挂载、宿主机整批安全校验与 `workbench/outputs/<run_id>/` 原子落盘。
- `docker/Dockerfile.analysis`：轻量科学沙箱镜像构建模板（`python:3.12-slim` + `numpy`/`scipy`/`matplotlib`）。
- `src/vasp_agent/remote/remote_servers.py`：密钥引用更新、撤销、配置哈希、连接详情和错误分类。
- `src/vasp_agent/editing/edit_commands.py`：无需模型的本地维护入口；工具与终端显示沿用现有框架。
- 上述模块与模板均不加入 `remote_worker.ENGINE_MODULES` / `SOURCE_FILES`；无须重新部署远程计算引擎。

专项测试：

```bash
python -m pytest -q tests/test_workspace_editor.py tests/test_server_identity.py tests/test_edit_integration.py tests/test_analysis_runner.py tests/test_analysis_outputs.py tests/test_analysis_approval.py
```

完整回归：`python -m pytest -q`。测试使用临时目录、伪密钥文件及模拟SSH/模型，不连接真实服务器、不请求模型接口、不执行VASP或提交Slurm。Windows缺少创建符号链接权限时实际symlink用例会跳过，另有无需此权限的reparse检查及真实硬链接测试。

2026-09-28 本次实际验证：新增专项 **57 passed, 1 skipped**；全量 **327 passed, 1 skipped, 15 warnings**（36.11秒）。跳过项为当前环境不能创建真实符号链接，警告为既有spglib弃用提示。另已检查修改模块的Python语法及本地安装模块清单。以上不是集群实跑或真实密钥认证结果。
