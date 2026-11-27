# AGENTS.md — `vasp-band-agent` 开发与协作约定

> **一句话目标**：自动化 VASP 能带计算系统——AI 负责对话规划、证据审阅与结果解释，确定性 Python 引擎负责输入生成、哈希校验、集群提交与科学数值验收。

## 1. 工作区目录地图（热 / 温 / 冷分层）

| 目录 / 文件 | 层级 | 职责与操作规则 |
|---|---|---|
| `src/vasp_agent/` | 热区 | 核心源码（11 个子包，见 `docs/ARCHITECTURE.md`） |
| `tests/` | 热区 | 纯离线自动化回归测试（`conftest.py` 强制 `subprocess` SSH 桩，零真实外网/VASP） |
| `tools/` | 热区 | 本地工程校验脚本（如 `tools/check.py`） |
| `.github/workflows/` | 热区 | GitHub Actions 云端全量离线 CI（`.github/workflows/ci.yml`） |
| `docs/ARCHITECTURE.md`、`docs/modules/`、`docs/design/` | 热区 | 架构总览、核心模块接口卡与工作流模块设计（**改代码前先读接口卡**） |
| `.agents/` | **开发代理配置** | 本地开发期上下文护栏 Hook（`.agents/hooks.json`、`.agents/hooks/`），**非应用运行时** |
| `.agent/` | **应用运行时（受保护）** | 产品面向用户的运行数据（会话、审计、证据、状态、监控、缓存），强绑定 `common/agent_paths.py`，**严禁改名或迁移** |
| `docs/archive/` | 温区（`.ignore`） | 已归档历史实验与交接记录，保留 Git 历史，默认不参与搜索 |
| `runs/`、`downloads/`、`reports/`、`workbench/`、`servers/`、`potentials/` | 冷区 / 操作者数据（受保护） | 历史计算任务、凭据、赝势库与结果，**严禁搬动或改写** |
| `.claude/` | 冷区（`.ignore` + `.gitignore`） | 本地辅助工具目录，不纳入代码仓 |

> **关键区分**：`.agent/` 是本软件的运行时数据目录；`.agents/` 是开发协作代理的本地配置与 Hook 目录，二者不可混淆或合并。

## 2. 常用命令（Windows PowerShell 优先）

```powershell
# 安装可编辑包及开发/测试依赖（含 pytest-xdist）
python -m pip install -e ".[ai,ssh,test]"

# 日常本地极速校验（ruff + 源码行数守门 + release 哈希，约 0.15s，默认跳过全量 pytest）
python tools/check.py --skip-pytest --expect-release <当前release哈希>

# 日常最小定向测试（仅跑本次直接改动/新增的 1–3 个对应测试文件，目标 3–10s；超过 3 个文件时加 -n auto --dist loadfile）
python -m pytest tests/test_<目标模块>.py -q --tb=short
python -m pytest -n auto --dist loadfile tests/test_a.py tests/test_b.py tests/test_c.py tests/test_d.py -q --tb=short

# 仅操作者明确要求时才允许本地全量运行（优先并行，默认禁止本地跑全量）
python -m pytest -n auto --dist loadfile -q --tb=short

# 输出完整 JSON 校验明细（按需排障）
python tools/check.py --output .agent/cache/check-report.json

# 校验远端科学引擎 Release 哈希（重构期间用于确认零变更）
python -c "import sys; sys.path.insert(0,'src'); from vasp_agent.remote.bundle import local_version; print(local_version())"
```

> **Windows PowerShell 5.1 提示**：勿使用 `&&`/`||`（改用 `;` 或 `if ($?)`）和 Bash `<<EOF` heredoc（改用 `@' ... '@ | python -`）。

### 分层测试与验收规程（本地极速烟测 + GitHub Actions 云端全量）

1. **本地极速烟测（日常每次改动必跑，目标 3–10 秒）**：
   - 本地运行 `python tools/check.py --skip-pytest --expect-release <当前release哈希>`（校验 `ruff`、`loc` 与 `release`）+ **仅运行本次直接新增或修改的 1–3 个对应 `pytest` 文件**（如 `python -m pytest tests/test_<目标模块>.py -q --tb=short`）；
   - **不再每次默认附带重型/跨模块全局守门文件**（如 `tests/test_project_layout.py`、`tests/test_agent_paths.py`、`tests/test_prompt_budget.py`、`tests/test_context_budget.py`），未触碰对应领域时本地跳过、交由云端 CI 兜底。
2. **重型与领域守门按触发条件按需加跑**：
   - 触碰 `chat/prompts.py`、工具 schema 或上下文压缩 → 加跑 `tests/test_prompt_budget.py`、`tests/test_context_budget.py`；
   - 触碰 `common/agent_paths.py` 或新增运行时路径/文件常量 → 加跑 `tests/test_agent_paths.py`；
   - 触碰 `remote/bundle.py`、`ENGINE_MODULES` 或包导入布局 → 加跑 `tests/test_project_layout.py`；
   - 触碰 Windows 文件锁、路径解析、并发或 CRLF → **必须在本机 Windows 定向跑对应并发/路径用例**（云端 CI 仅跑 Linux，本机是唯一 Windows 真机防线）。
3. **本地并行与禁跑全量红线**：
   - **默认禁止本地跑全量 `pytest` 或不带 `--skip-pytest` 的 `python tools/check.py`**；仅在操作者明确要求时才允许本地全量运行，且优先使用 `python -m pytest -n auto --dist loadfile -q --tb=short`；
   - 本地一次定向测试超过 3 个文件时，优先加 `-n auto --dist loadfile` 并行（1–3 个轻量文件直接单进程跑，避免 `xdist` 启动开销）。
4. **全量回归验收（统一交给 GitHub Actions CI 云端执行，不占本地 CPU）**：
   - 全量离线回归、未触碰到的全局守门、Linux `os.symlink` 与 `mcp>=1.6` 实测，统一在提交并推送至 GitHub（`main` 或特性分支/PR）后，由 `.github/workflows/ci.yml` 在云端 `ubuntu-latest`（Python 3.12）上运行 `python tools/check.py --skip-pytest` 及 `python -m pytest -n auto --dist loadfile -q --tb=short` 兜底；
   - 坚持“本地极速自查 + 审阅通过后再 commit/push 过云端 CI”，不把云端 CI 当作低级笔误试错机以免污染 `main` 历史；
   - 向操作者汇报校验状态时，必须**严格区分并如实列出**“本地实际跑过的定向测试文件名”与“待/已通过 GitHub Actions 云端全量测试”，**严禁把定向通过写成全量通过**。

## 3. 量化工程红线

1. **单文件行数双阈值守门（`tools/check.py`）**：
   - **软警告阈值 `WARN_LINES = 500`**：Python 源码文件超过 500 行且 `<= 800` 行时，在摘要与 JSON 报告中输出 `warnings` 提醒（建议择机拆分），但不判失败；
   - **硬阻断阈值 `MAX_LINES = 800`**：超过 800 行直接 `FAIL`（阶段 3 后历史豁免清零 `GRANDFATHERED_LINE_CAPS = {}`）。
2. **固定提示词预算**：`PROMPT_TOKEN_BUDGET = 14800`（由 `tests/test_prompt_budget.py` 强制守门）。新阶段规则必须通过工具结果与按需指南（`src/vasp_agent/guides/`）注入，不写入固定系统提示。
3. **远端引擎冻结契约（`ENGINE_MODULES`）**：
   - 修改 `remote_worker.ENGINE_MODULES` 列出的任一科学引擎文件都会改变 `bundle.local_version()` 并要求集群重新 `remote_deploy`。
   - **纯工程重构/非功能升级窗口禁止改动 `ENGINE_MODULES`**，保持 `local_version()` 逐字节不变。
   - 远端 `WORKER_UNITS`（含 `remote_worker.py`）通过纯文本 stdin 拼接执行，**只能依赖 Python 标准库及前置单元**，严禁引入第三方依赖。
4. **科学验收与授权底线**：
   - 物理确认、参数选择、资源确认、提交授权与本机脚本批准必须来自操作者本地菜单，模型不得代选或绕过。
   - 禁止修改历史任务目录的 `manifest.json`、审阅快照与旧引擎回归基准（`tests/data/legacy_stage_walk.json`）。
5. **导入兼容**：拆分大模块（如 `chat/ai_agent.py`、`remote/remote_worker.py`）时，原入口模块必须保留完整 re-export 门面，确保外部导入与测试 `monkeypatch` 零破坏。
6. **跨平台确定性与测试隔离（Windows 开发 / Linux CI）**：
   - **行尾统一 LF**：新增文本/JSON 落盘（尤其参与哈希、回执或基准比对的文件）须显式指定 `encoding='utf-8', newline='\n'`，文档与源码统一 LF（冻结 `ENGINE_MODULES` 窗口除外）。
   - **路径统一 POSIX**：持久化进 JSON、回执、快照或测试断言的相对路径统一使用 `.as_posix()`，严禁将宿主机绝对路径写入哈希或黄金基准。
   - **宿主与依赖隔离**：读取系统级路径（如 `~/.ssh`、`/etc/ssh`、`%PROGRAMDATA%`）须封装可替换辅助函数并在测试 fixture 中隔离到 `tmp_path`；所有测试依赖（含可选包）必须显式声明在 `pyproject.toml` 的 `test` 组，避免硬编码第三方库内部子依赖（如 `httpx`/`httpx2`）。
   - **基准与平台特性测试**：新增黄金基准须保证双平台字节确定性或对比解析后的语义结构；`symlink`/重解析点/文件锁等平台特有行为须采用“真实系统调用（无权限时 skip）+ mock 边界用例”双覆盖。

## 4. 模块速查

- 整体架构与依赖方向：[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- 核心子包接口卡：[chat](docs/modules/chat.md) · [remote](docs/modules/remote.md) · [workflow](docs/modules/workflow.md) · [monitoring](docs/modules/monitoring.md)
- 阶段扩展与参数规范设计：[docs/design/WORKFLOW_MODULES.md](docs/design/WORKFLOW_MODULES.md)
