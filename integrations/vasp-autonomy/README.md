# `vasp-autonomy` — 外部代理有界自治接入套件（Antigravity / Codex）

本目录提供将 `vasp-band-agent` 接入外部强代理（**Antigravity 优先，兼顾 Codex**）的完整插件/集成套件，覆盖主控 SOP、独立只读审查子代理、L5 `PreToolUse` 安全护栏与配置模板。

## 目录结构

```text
integrations/vasp-autonomy/
├── README.md                    # 本文：安装、Antigravity/Codex 接入与验收指南
├── SKILL.md                     # Maker 主控端到端有界自治 SOP
├── agents/
│   └── vasp-reviewer.md         # 独立只读 Checker 审查子代理指令与隔离边界
└── hooks/
    ├── autonomy_guard.py        # 纯标准库 L5 PreToolUse 护栏（拦直改保护路径与裸 SSH/Slurm）
    ├── _shell_lex.py            # 纯标准库 Shell 词法/heredoc/命令展开辅助模块
    └── hooks.json               # PreToolUse Hook 配置模板
```

---

## 1. 安装依赖与冒烟核对

在项目根目录的虚拟环境中安装 MCP 与 SSH 可选依赖（`vasp-ctl` 零额外依赖；`vasp-mcp` 需要 `mcp>=1.6,<2`）：

```bash
python -m pip install -e ".[mcp,ssh]"
```

确认两个入口与角色工具白名单（Maker 19 工具 / Checker 11 工具，零外网调用）：

```bash
vasp-mcp --workspace . --role maker --list-tools-json
vasp-mcp --workspace . --role checker --list-tools-json
vasp-ctl --workspace . tools --json
```

---

## 2. Antigravity 接入（推荐主路径）

### 2.1 注册双角色 MCP 服务器（`mcp_config.json`）

在 `~/.gemini/config/mcp_config.json`（或独立插件的 `mcp_config.json`）中为 **Maker** 与 **Checker** 分别配置不同 `--actor-id` 的独立进程（`--ssh-transport pooled` 在长驻进程内复用 SSH 连接池）：

```json
{
  "mcpServers": {
    "vasp-maker": {
      "command": "vasp-mcp",
      "args": [
        "--workspace", "/ABS/PATH/TO/vasp-band-agent",
        "--role", "maker",
        "--actor-id", "agy-maker-main",
        "--host", "antigravity",
        "--model", "pro",
        "--ssh-transport", "pooled"
      ]
    },
    "vasp-checker": {
      "command": "vasp-mcp",
      "args": [
        "--workspace", "/ABS/PATH/TO/vasp-band-agent",
        "--role", "checker",
        "--actor-id", "agy-checker-reviewer",
        "--host", "antigravity",
        "--model", "pro",
        "--ssh-transport", "pooled"
      ]
    }
  }
}
```

> 也可通过环境变量 `VASP_AGENT_HOST` / `VASP_AGENT_MODEL` 注入宿主与模型标签；`--host`/`--model` 命令行参数优先级高于环境变量。**Maker 与 Checker 必须使用不同 `--actor-id`**，否则 `maker_checker` 合同会以 `REVIEW_SELF_APPROVAL_FORBIDDEN` 硬拒收。

### 2.2 挂载 Skill、只读子代理与 L5 Hook

1. **主控 Skill**：将 [SKILL.md](SKILL.md) 放入 `.agents/skills/vasp-autonomy/SKILL.md`（或注册到 `skills.json`），主会话仅挂载 `vasp-maker` MCP。
2. **独立只读审查子代理**：使用 [agents/vasp-reviewer.md](agents/vasp-reviewer.md) 配置子代理，**关闭其写文件与 Shell 工具权限**，仅挂载 `vasp-checker` MCP。
3. **L5 `PreToolUse` 护栏**：
   - 本仓库的 [`.agents/hooks.json`](../../.agents/hooks.json) **已直接挂载** `integrations/vasp-autonomy/hooks/autonomy_guard.py`，无需重复配置；
   - 若独立打包为 Antigravity 原生插件，需将 [hooks/hooks.json](hooks/hooks.json) 放在插件根目录（`<plugin_dir>/hooks.json`），脚本放在 `<plugin_dir>/hooks/autonomy_guard.py` 与 `<plugin_dir>/hooks/_shell_lex.py`（`autonomy_guard.py` 同时兼容 Antigravity `toolCall` -> `{"decision": "deny"}` 与 Claude Code/Codex `tool_name` -> `{"decision": "block"}` / exit 2 双协议）。
4. **异步轮询闭环**：提交阶段后，使用 Antigravity `/schedule` 每 5–15 分钟触发主会话调用 `vasp_watch_jobs(poll=true)`；也可在独立终端运行 `vasp-ctl watch daemon`，此时 `vasp_watch_jobs(poll=true)` 自动复用守护进程结果、避免重复 SSH。

---

## 3. Codex 接入（`~/.codex/config.toml`）

在 `~/.codex/config.toml` 中注册双角色 MCP，并保持 Codex 沙箱为 `workspace-write`（Shell 断网，仅 `vasp-mcp` 连集群）：

```toml
[mcp_servers.vasp_maker]
command = "vasp-mcp"
args = [
  "--workspace", "/ABS/PATH/TO/vasp-band-agent",
  "--role", "maker",
  "--actor-id", "codex-maker-main",
  "--host", "codex",
  "--model", "gpt-5",
  "--ssh-transport", "pooled",
]

[mcp_servers.vasp_checker]
command = "vasp-mcp"
args = [
  "--workspace", "/ABS/PATH/TO/vasp-band-agent",
  "--role", "checker",
  "--actor-id", "codex-checker-reviewer",
  "--host", "codex",
  "--model", "gpt-5",
  "--ssh-transport", "pooled",
]
```

> 若宿主不支持 MCP，可通过 `vasp-ctl --workspace . --role maker call <tool_name> --args '{...}' --json` 使用零额外依赖 CLI 回退。

---

## 4. 操作者带外控制命令（仅限本地真实终端）

合同签发、恢复与撤销强制检测真实交互终端（Windows `CONIN$` / POSIX `/dev/tty`），拒绝管道重定向，且永不注册到 MCP：

```bash
# 查看所有合同、账本状态与待签草案
vasp-ctl contract show

# 审核草案并签发不可变合同（默认 maker_checker，可现场指定 --review-mode maker_direct）
vasp-ctl contract approve <draft_id>
vasp-ctl contract approve <draft_id> --ssh-key ~/.ssh/id_ed25519 --ssh-principal operator@local

# 熔断人工核查后恢复或永久撤销合同
vasp-ctl contract resume <contract_id> --reason "verified_resource_fix"
vasp-ctl contract revoke <contract_id> --reason "operator_stopped"

# 后台只读监控守护进程 / 单次轮询 / 事件确认
vasp-ctl watch once --json
vasp-ctl watch daemon
vasp-ctl watch ack <event_id>
```

### 可选 OpenSSH 密码学验签策略（L2 增强）

如需在每次准备/提交/自愈前强制校验合同 OpenSSH 签名，由操作者本机手工创建 `.agent/state/autonomy-policy.json`：

```json
{
  "require_ssh_signature": true,
  "allowed_signers": "/ABS/PATH/TO/allowed_signers",
  "principal": "operator@local"
}
```

> `principal` 为可选项；不填时按签发合同记录的默认 principal 验签。

---

## 5. 五层纵深防御速览

| 层级 | 防线 | 说明 |
|---|---|---|
| L1 | 远端哈希链、Tier ③ 确定性裁判与科学验收 | `manifest.json`、`script_sha256`、`checkpoint.evidence_hashes`、`workflow/solver_overrides.py`（9 标签硬白名单）、`stage_gate`（`CONFIRMATION_VERSION=2` / `operator_task_contract` + `incar_solver_diff` 审阅） |
| L2 | 合同真实 TTY + 可选 SSH 验签 | 拒收非 TTY 管道签发；可选 `ssh-keygen -Y verify` 防止篡改已签合同 |
| L3 | MCP 角色与审阅背书隔离 | Maker 19 工具 / Checker 11 工具；15 分钟租约绑定 `(actor_id, role, host, model, snapshot)` + 本地/远端提交双闸门 |
| L4 | 宿主沙箱与凭据边界 | Checker 禁用写与 Shell；Codex `workspace-write` 断网；高危操作永不入 MCP |
| L5 | `PreToolUse` 护栏 Hook | `autonomy_guard.py` 拦截直写 `.agent/state/contracts/`、`review-attestations/`、`autonomy-policy.json`、`servers/`、`runs/`、`potentials/` 与裸 `ssh`/`scp`/`sftp`/`sbatch`/`scancel` |
