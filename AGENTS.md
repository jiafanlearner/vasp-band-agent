# AGENTS.md — `vasp-band-agent` 开发与协作约定

> **一句话目标**：自动化 VASP 能带计算系统——AI 负责对话规划、证据审阅与结果解释，确定性 Python 引擎负责输入生成、哈希校验、集群提交与科学数值验收。

## 1. 工作区目录地图（热 / 温 / 冷分层）

| 目录 / 文件 | 层级 | 职责与操作规则 |
|---|---|---|
| `src/vasp_agent/` | 热区 | 核心源码（11 个子包，见 `docs/ARCHITECTURE.md`） |
| `tests/` | 热区 | 纯离线自动化回归测试（`conftest.py` 强制 `subprocess` SSH 桩，零真实外网/VASP） |
| `tools/` | 热区 | 本地工程校验脚本（如 `tools/check.py`） |
| `docs/ARCHITECTURE.md`、`docs/modules/`、`docs/design/` | 热区 | 架构总览、核心模块接口卡与工作流模块设计（**改代码前先读接口卡**） |
| `.agents/` | **开发代理配置** | 本地开发期上下文护栏 Hook（`.agents/hooks.json`、`.agents/hooks/`），**非应用运行时** |
| `.agent/` | **应用运行时（受保护）** | 产品面向用户的运行数据（会话、审计、证据、状态、监控、缓存），强绑定 `common/agent_paths.py`，**严禁改名或迁移** |
| `docs/archive/` | 温区（`.ignore`） | 已归档历史实验与交接记录，保留 Git 历史，默认不参与搜索 |
| `runs/`、`downloads/`、`reports/`、`workbench/`、`servers/`、`potentials/` | 冷区 / 操作者数据（受保护） | 历史计算任务、凭据、赝势库与结果，**严禁搬动或改写** |
| `.claude/` | 冷区（`.ignore` + `.gitignore`） | 本地辅助工具目录，不纳入代码仓 |

> **关键区分**：`.agent/` 是本软件的运行时数据目录；`.agents/` 是开发协作代理的本地配置与 Hook 目录，二者不可混淆或合并。

## 2. 常用命令（Windows PowerShell 优先）

```powershell
# 安装可编辑包及开发/测试依赖
python -m pip install -e ".[ai,ssh,test]"

# 统一工程校验（ruff + pytest + 源码行数守门；stdout <= 4 KB）
python tools/check.py

# 输出完整 JSON 校验明细（按需排障）
python tools/check.py --output .agent/cache/check-report.json

# 单独运行指定测试文件
python -m pytest tests/test_project_layout.py -q

# 校验远端科学引擎 Release 哈希（重构期间用于确认零变更）
python -c "import sys; sys.path.insert(0,'src'); from vasp_agent.remote.bundle import local_version; print(local_version())"
```

> **Windows PowerShell 5.1 提示**：勿使用 `&&`/`||`（改用 `;` 或 `if ($?)`）和 Bash `<<EOF` heredoc（改用 `@' ... '@ | python -`）。

## 3. 量化工程红线

1. **单文件行数上限**：新增或重构后的 Python 源码文件 **<= 500 行**（既有 6 个超线文件在 `tools/check.py` 中列为显式债务豁免，严禁新增超线文件或使既有债务继续膨胀）。
2. **固定提示词预算**：`PROMPT_TOKEN_BUDGET = 14800`（由 `tests/test_prompt_budget.py` 强制守门）。新阶段规则必须通过工具结果与按需指南（`src/vasp_agent/guides/`）注入，不写入固定系统提示。
3. **远端引擎冻结契约（`ENGINE_MODULES`）**：
   - 修改 `remote_worker.ENGINE_MODULES` 列出的任一科学引擎文件都会改变 `bundle.local_version()` 并要求集群重新 `remote_deploy`。
   - **纯工程重构/非功能升级窗口禁止改动 `ENGINE_MODULES`**，保持 `local_version()` 逐字节不变。
   - 远端 `WORKER_UNITS`（含 `remote_worker.py`）通过纯文本 stdin 拼接执行，**只能依赖 Python 标准库及前置单元**，严禁引入第三方依赖。
4. **科学验收与授权底线**：
   - 物理确认、参数选择、资源确认、提交授权与本机脚本批准必须来自操作者本地菜单，模型不得代选或绕过。
   - 禁止修改历史任务目录的 `manifest.json`、审阅快照与旧引擎回归基准（`tests/data/legacy_stage_walk.json`）。
5. **导入兼容**：拆分大模块（如 `chat/ai_agent.py`、`remote/remote_worker.py`）时，原入口模块必须保留完整 re-export 门面，确保外部导入与测试 `monkeypatch` 零破坏。

## 4. 模块速查

- 整体架构与依赖方向：[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- 核心子包接口卡：[chat](docs/modules/chat.md) · [remote](docs/modules/remote.md) · [workflow](docs/modules/workflow.md) · [monitoring](docs/modules/monitoring.md)
- 阶段扩展与参数规范设计：[docs/design/WORKFLOW_MODULES.md](docs/design/WORKFLOW_MODULES.md)
