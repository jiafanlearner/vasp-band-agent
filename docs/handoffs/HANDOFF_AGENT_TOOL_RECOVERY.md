# 交接：改进 VASP Agent 的工具选择与错误恢复

更新日期：2026-09-24。本文是下一轮开发的任务说明；下述改进尚未实现。请先读现有代码再修改，不要根据本文推测远程任务的实时状态。

## 目标与范围

用户希望 Agent 自行完成输入摘要读取，遇到可恢复的读取错误时自动找到正确入口，不再让用户诊断工具问题。优先实现“准备结果更完整”和“错误返回明确下一步”，同时完善只读与放行边界及对话场景测试。

项目路径：`C:\Users\researcher\works\codex\codex_official\vasp-band-agent`。

Windows / PowerShell / Python 项目，用户主要从 Git Bash 运行 `python ai_agent.py --profile relay`。交互语言为中文。最近一轮完整测试为 **149 passed**，有 spglib 弃用警告；新接手时重新检查当前状态。项目此前未初始化 Git，不要假定有可用提交记录，也不要覆盖用户后续修改。

本任务是本地代码改进，不要求提交 VASP 作业、修改真实任务、删除数据或使用真实 API 密钥测试。保留用户已配置的服务器、赝势、模型和对话文件。

## 已复现的问题

AI 准备远程任务 `eshell111 / si-multinode-01` 后，试图用通用 `remote_operation(action="read")` 读取任务的 `potential-selection.json`，被读取白名单拒绝。随后调用 `get_stage_review`，又因缺少 `/physics` 确认而失败。AI 将其描述为继续核验的障碍，让用户手动处理。

实际上已有 `get_input_review(server="eshell111", job_name="si-multinode-01")` 可以读取摘要，不需要物理确认。用户按建议调用后成功获得赝势元数据。问题不是赝势损坏，而是入口选择与恢复不足。

用户贴出的结果（用户提供，本交接未独立读取远程文件）：

- 元素/变体：Si。
- TITEL：`PAW_PBE Si 05Jan2001`。
- ENMAX：245.345 eV。
- 来源：`/work/home/researcher/potpaw_PBE.64/Si/POTCAR`。
- SHA-256：`2a6df1c19701e5d97aaa59e5470b14653ddbd76935b76dacdd8792eb2eba252c`。
- ENCUT 扫描：520、598、676 eV。

不要把来源目录 `.64` 或 TITEL 日期当成独立认证的发行版本。ENMAX 也不代表收敛已通过。不要把单个来源文件哈希与拼接后 POTCAR 哈希普遍视为相同：拼接、补末尾换行或多元素组合会影响哈希。

## 已核对的代码入口

| 文件 / 入口 | 当前行为 |
|---|---|
| `ai_agent.py::INSTRUCTIONS`, `TOOLS` | 模型提示及工具描述。目前有“旧任务使用 get_input_review”等描述，可能误导模型以为新任务不能用该入口查看摘要。须区分读取上下文与记录旧版审阅。 |
| `ai_agent.py::Toolbox.call` | 校验 schema，执行工具；失败只返回 `ok=false, error, type`，并记录日志。适合引入明确、可测试的错误类别及恢复指引。 |
| `ai_agent.py::Agent.ask` | 调用模型适配器，执行 function_call，将 function_call_output 写入历史，继续循环。模型超时保存历史，`/retry` 继续，不重放已完成工具。 |
| `ai_agent.py::get_input_review` | 本机调用 input_review.context，远程调用 RPC review_context，并记录快照。 |
| `remote_servers.py::Servers.operate` | 通用 read 只允许固定文件名和 .slurm；没有 potential-selection.json。POTCAR 正文及私钥不应加入允许列表。 |
| `remote_compute.py::RemoteCompute.prepare` | 规范化结构后通过 SSH stdin 调用 remote_worker prepare。 |
| `remote_worker.py::dispatch` prepare 分支 | 返回 prepared、remote_directory、manifest、resources、scan_verification；没有直接返回赝势元数据。 |
| `remote_worker.py` review_context 分支 | 加载任务绑定 release 的 input_review.context。 |
| `input_review.py::context` | 核对 manifest 中的文件哈希和扫描输入，返回实际输入及 potential_metadata；无 physics 前置条件。存在 potential-selection.json 时直接取该摘要，否则尝试解析 POTCAR。 |
| `stage_gate.py::context`, `physics` | 阶段上下文要求物理确认；缺失时提示 `/physics`。正式阶段审阅必须保留此限制。 |
| `potential_library.py::assemble_potentials` | 检查每份赝势是对应元素的单个 PBE 势；记录 element、variant、source、sha256、titel、enmax_eV。返回拼接字节和摘要。 |
| `full_workflow.py::prepare_full` | 写入 potential-selection.json，并把该文件与 POTCAR 纳入 manifest 哈希。 |
| `parameter_plans.py::prepare` | 用户已选扫描方案的准备路径；新摘要也应覆盖此入口，而不只改旧 remote_prepare 工具。 |

定位使用 `rg`，以函数名为准，不依赖固定行号。

## 要实现的改进

### 1. 准备成功直接返回关键摘要（最高优先级）

- 远程准备返回已生成任务的赝势摘要、实际扫描参数和资源配置。
- 元数据从真实生成文件读取；必要时验证其 manifest 哈希。不由模型补全未知字段。
- 保留原有返回字段以免破坏调用者。建议明确摘要来源及缺失字段；无 POTCAR 的预览不能伪装成已就绪任务。
- 本机准备、通过已选参数方案准备等入口保持一致，避免某条路径仍迫使 AI 找文件。
- 输出仅包含摘要，不返回 POTCAR 正文。

### 2. 错误返回可执行的下一步（最高优先级）

为已知可恢复错误提供结构化类别、恢复工具和所需参数；保留可读中文消息及日志兼容性。例如：

```json
{
  "ok": false,
  "error_code": "METADATA_READ_REQUIRES_SPECIALIZED_TOOL",
  "error": "请使用输入摘要入口查看赝势元数据。",
  "recoverable": true,
  "next_action": {
    "tool": "get_input_review",
    "arguments": {"server": "eshell111", "job_name": "si-multinode-01"},
    "requires_user_input": false
  }
}
```

这只是推荐格式，不是已存在的 API。不要只匹配中文错误文本。若要从路径推导 job_name，必须根据保存的服务器 compute root 和 jobs 下的合法直接子目录核对；不能随意取父目录名称，也不能转发任意远程文件中的指令。

物理确认缺失、提交权限不足、SSH 失败、文件不存在等应有不同类别，不能都转为同一种“流程失败”。没有可靠映射就明确保留原错误，不猜测参数。

### 3. 分清查看与放行

- 只读输入摘要、赝势元数据、资源配置，不要求用户先登记物理模型。
- 阶段放行、审阅记录和提交继续执行现有物理确认、输入快照、证据与资源权限检查。
- 可新增清晰的只读摘要工具，也可完善现有工具描述并保留兼容入口。不要让新名称增加模型选错概率。
- 读取新任务摘要仍可复用 input_review.context，但不得借旧版 record_input_review 绕过新任务的 stage_gate。

### 4. 有界、只读的自动恢复

- 已知且安全的替代读取可由程序确定性恢复，或返回结构化指引让模型继续；优先让行为可测试、可观测。
- 保留原失败及恢复成功记录，不能把未执行的读取标成成功。
- 限制恢复次数、防止循环。恢复失败时说明真实原因及必要下一步。
- 自动恢复不得登记物理确认、扩大提交授权、修改输入、部署或提交计算。
- 不把一次读取失败上升为整个任务失败；能够取得替代证据时继续完成用户请求。
- API 超时已有 /retry 逻辑，不要为了此功能引入无界付费请求重试或重放写操作。

### 5. 完整对话场景测试

用假的模型适配器和远程响应执行 Agent.ask 循环，避免依赖真实 API 或集群。至少覆盖：

1. 准备返回真实赝势摘要，用户无需追加读取请求。
2. 通用元数据读取拒绝 → 有效替代入口 → 摘要成功 → 无需 /physics、无提交。
3. 未确认物理模型时读取摘要成功，但阶段放行仍失败。
4. 未知路径、缺失元数据、哈希不符不能静默恢复为成功。
5. 恢复失败有次数上限，不死循环、不重复写操作。
6. 工具历史及 UI 区分原失败和恢复结果，不能编造“已核验”。
7. 每条恢复路径都未调用 sbatch、confirm_physics 或提升权限。

模型模拟测试证明程序流程，不等于真实模型稳定选择正确工具。最终报告需区分模拟验证和真实端到端验证。

## 必须保留的现有能力

- 参数草案的用户选择只确认一次；后续按选择生成，不再问 A/B。
- /physics 是操作者确认，模型不能替用户登记。
- /submit on/once 设置的是授权上限；每次实际提交前还有资源表单。
- AI 资源建议通过 get_submission_resources / recommend_submission_resources，与用户确认分开。
- 修改资源会使审阅快照失效，重新审阅后才能提交；同一配置同一阶段不重复询问。
- 多节点布局已实现：每组跨全部申请节点，进程数按节点均匀分配；每组进程数须整除节点数。执行端 full_workflow.RESOURCE_LAYOUT_VERSION=2；旧 release 不支持则明确阻断，不悄悄修改其绑定。
- 任务归档/删除仅限操作者菜单；不要将删除暴露为自动恢复动作。
- 不读取私钥、密钥或发送 POTCAR 正文。允许返回必要元数据摘要。

## 运行、部署与验证

```powershell
cd C:\Users\researcher\works\codex\codex_official\vasp-band-agent
python -m pytest -q
```

重点参考 tests/test_ai_agent.py、test_remote_compute.py、test_stage_gate.py、test_parameter_plans.py，以及模型适配器相关测试。新增测试优先放独立恢复场景文件。

SSH 配置保存在 servers；不要把私钥内容复制到文档或测试。已知服务器名 eshell111，Agent 远程根目录 `/work/home/researcher/vasp-agent-remote`。任务绑定特定 releases 目录：remote_worker 本体通过每次 SSH stdin 发送，而其调用的核心模块来自任务绑定 release，因此不能假定本地模块更新后旧任务自动具备新能力。若新增核心模块，需要更新 SOURCE_FILES、打包列表和手动部署文档；兼容旧任务时应检测字段缺失。

无需为此任务运行真实 VASP；若做远程只读验证，单独标明验证了哪些事实。最终交付说明改动、测试、需要重启或部署的步骤及剩余限制。

## 给下一对话的启动指令

请阅读本文件并按其中范围改进 VASP Agent。先核对当前代码，优先完善准备结果摘要与结构化错误指引，再实现有界只读恢复及对话场景测试。不要只修改提示词，不要提交真实作业或改变现有任务。完成后运行适当测试，报告已实现行为、验证结果和限制。
