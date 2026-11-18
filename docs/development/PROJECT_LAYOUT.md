# 按功能组织的项目结构

## 源码

项目采用 `src` 布局，包名 `vasp_agent`。根目录的两个 Python 文件仅负责兼容旧启动命令，不再承载业务逻辑。

| 子包 | 责任 | 主要模块 |
|---|---|---|
| `chat` | 对话、会话、斜杠命令分发 | ai_agent、chat_commands、extra_commands、stage_commands、task_commands |
| `models` | 模型 API、配置和上下文 | model_adapters、model_config、model_catalog、context_budget、context_compaction |
| `editing` | 工作草稿及修改回执 | workspace_editor、edit_store、edit_commands |
| `workflow` | 科学计算及验收 | vasp_band_agent、full_workflow、potential_library、input_review、stage_gate、scan_config、convergence_reuse |
| `remote` | 连接、RPC、固定引擎包 | remote_servers、remote_compute、remote_worker、bundle |
| `planning` | 扫描参数草案 | parameter_plans |
| `permissions` | 提交权限和资源表单 | submission_permissions、submission_dialog |
| `ui` | 终端显示和交互输入 | terminal_ui、interactive_prompt |
| `common` | 共用路径工具 | workspace_paths |

模块保留原基名，便于从历史交接文档定位。代码和测试使用完整包导入，例如 `from vasp_agent.remote.remote_servers import Servers`。pytest 的源码路径由 `pyproject.toml` 配置，不依赖旧的顶层模块安装。

## 安装与入口

在项目根目录运行：

```bash
python -m pip install -e ".[ai,test]"
python -m pytest -q
bash start-agent.sh
```

兼容入口：`python ai_agent.py`、`python vasp_band_agent.py`。安装后可使用 `vasp-ai`、`vasp-band-agent`、`python -m vasp_agent`。远程维护 CLI 改为 `python -m vasp_agent.remote.remote_compute --help`；普通 Agent 用户仍通过自然语言工具完成远程操作。

编辑器、CI 或外部脚本如原先导入 `remote_servers` 等顶层模块，需要改为完整包路径。旧命令入口保留不等于保留全部内部模块的顶层导入接口。

## 远程兼容

`remote/bundle.py` 从已安装的包资源生成固定白名单引擎。部署包只含科学工作流、远程 worker、必要的空包初始化文件和旧名称启动桥接文件；不包含模型配置、密钥、对话、草稿或本地编辑工具。

新发布版本同时提供 `vasp_agent/workflow/` 包和 `vasp_band_agent.py` 等旧入口，兼容提交脚本及清单约定。部署 worker 只允许明确列出的文件路径，并创建必要父目录。

通过 SSH stdin 发送的 worker 仍按任务绑定的 release 读取模块，已有平铺目录的旧 release 无需迁移。本地按包导入的 worker 使用本地包模块；生产 RPC 每次使用独立 Python 进程，避免把其他任务的引擎缓存混入当前版本。

本次没有连接服务器、部署新 release 或修改任务的 release 绑定。后续如果准备新任务并需要新版引擎，仍由用户按原流程要求部署。

## 数据与维护

`servers/`、`runs/`、`downloads/`、`potentials/` 和隐藏会话、审计、参数草案、上下文证据维持原路径。它们是运行数据，不是源码；移动需要独立的数据迁移设计，不能随源码整理批量搬走。

文档分为 `docs/guides/`、`docs/development/`、`docs/handoffs/`、`docs/previews/`；历史报告进入 `reports/runs/` 和 `reports/regression/`。文件移动清单见 [organization-20260928.json](../../reports/maintenance/organization-20260928.json)。

编译缓存、pytest 缓存、egg-info 和构建输出由工具生成并被忽略；它们不参与源码分组，也不作为计算证据。未来新功能应放入对应子包，新说明进入对应文档目录，避免重新堆回项目根目录。

本次旧根目录缓存、旧安装元数据与构建产物归入 `.cache/`，pytest 使用 `.cache/pytest`。新的可编辑安装元数据可能出现在 `src/*.egg-info/`，属于正常生成文件。

## 本次验证（2026-09-28）

- 全量离线回归：**334 passed、1 skipped、15 warnings，40.27秒**。跳过项为Windows当前权限下无法创建真实符号链接；警告是既有spglib弃用提示。
- 原两个Python启动入口、`python -m vasp_agent`、远程CLI模块入口、安装后的 `vasp-ai` / `vasp-band-agent` 均通过 `--help`；Git Bash启动脚本语法通过。
- wheel构建、临时目录安装与包资源读取通过；当前Python环境的可编辑安装映射已更新，未更新依赖或调用付费模型接口。
- 新引擎包可在临时目录独立导入并准备不可提交的无POTCAR预览；stdin worker能读取新包预览，也能加载模拟旧release。未SSH连接、运行VASP或调用真实调度器。
- 文档内部链接检查通过；三份历史回归JSON搬迁前后SHA-256一致。没有未改写的顶层内部模块导入。
