# AI 对话层 · v0.7

## 缓存用量与稳定请求前缀（2026-10-09）

系统指令和模型可见工具目录保持固定；当前权限与禁用工具作为状态消息追加，未变化时不重复追加。执行权限仍由程序检查。每轮显示接口返回的缓存读取比例；`/usage` 增加缓存读取、写入和前缀诊断，并区分正常对话与摘要请求。缺失数据为未知，不当作零命中。详见[提示词缓存与诊断](PROMPT_CACHE.md)。

## 模型接口的网络模式（2026-10-09）

默认 `auto` 沿用环境或系统代理；在 Windows 上，即使 HTTP_PROXY/HTTPS_PROXY 为空，HTTP 客户端也可能使用系统代理。若中转站允许直连，可显式选择 `direct`，只对这个 Agent 的模型 HTTP 客户端禁用应用代理发现，保留证书、主机名和 CA 环境配置，不改变系统代理或 SSH。程序不在两种线路间自动回退；系统透明 VPN/TUN 仍可能影响实际出口。

```bash
python ai_agent.py --profile relay --network-mode direct --check-config
python ai_agent.py --profile relay --network-mode direct
```

也可在所选 `models.toml` profile 中设置 `network_mode = "direct"`，或设置 `VASP_AGENT_NETWORK_MODE=direct`。优先级为命令行 → 已有环境变量/`.env` → profile → `auto`；`.env` 不覆盖已有环境。`--check-config` 是离线检查，只显示当前模式，不能证明连接成功。切换 `/profile` 时清除上一接口的命令行网络覆盖，读取新 profile/环境设置；`/model` 保留当前模式。连接失败和缓存命中是两项不同检查；绕过不稳定代理不等于缓存问题已经修复。

## 模型断连自动恢复（2026-09-28）

CLI默认最多自动重试5次，仅重发本次尚未取得完整响应的模型请求；SDK自身重试仍关闭。连接异常、超时及HTTP 408/429/500/502/503/504使用5、10、20、40、60秒退避，若有有效Retry-After则取更长等待。每次模型请求的累计退避等待最多300秒，服务端要求超过剩余预算时停止自动重试，不提前重发。请求本身耗时另计，故总耗时可能超过5分钟。重发请求可能产生额外模型费用。

界面显示“正在重新连接 1/5”、等待进度及恢复状态；等待或模型请求期间按Ctrl+C取消本次请求并保留会话，返回输入提示。次数用完仍可 `/retry` 手动继续。401/403、明确的配额耗尽、TLS证书验证错误及程序错误不自动重试。未知连接错误只说明请求失败，不声称已探测整机网络状态。

关闭自动恢复用 `bash start-agent.sh --model-retries 0`；当前会话可用 `/settings model_retries 0` 关闭或 `/settings model_retries 5` 开启。启动参数及运行时设置允许0–5；切换会话/接口沿用本次启动设置。嵌入式Python的Agent默认0次，调用者可显式传入model_retries。独立连接测试和摘要压缩不包含在本轮自动重试中。

现阶段仍为非流式模型请求重试，不是从断流位置续传。只重试模型接口，绝不包裹SSH、工具调用或Slurm提交；已完成工具输出作为历史继续传入。取消和重试均不增加计算授权。下文历史章节中“程序不自动重试”描述由本节更新；关闭重试时仍保持原行为。

源码按功能归入 `src/vasp_agent/`，文档位于 `docs/`；本页命令均从项目根目录执行。原 `python ai_agent.py` / `bash start-agent.sh` 继续可用，安装后的入口通过 `python -m pip install -e ".[ai]"` 更新。完整目录见 [项目说明](../development/PROJECT_LAYOUT.md)。

## 本地草稿编辑与 SSH 密钥更新（2026-09-28）

可要求AI在 `workbench/` 内新建或精确修改文本和脚本草稿，返回差异、文件哈希、修改ID和静态验证结果；不执行脚本。`/changes [ID [偏移]]` 查看持久化差异，`/undo ID` 在文件未再次改变时恢复。正式任务、结果、授权、凭据和POTCAR正文不在通用编辑范围。

密钥变更无需重新导入服务器或私钥：`/server-key 服务器名 "新密钥路径"` 仅更新已有配置的 `identity_file` 并备份，其余配置保留；`/server-key --undo ID` 恢复。上述命令无需模型接口。`/connect [服务器名]` 单独测试只读连接，无参数显示菜单。更新密钥不代表连接成功，更不代表准备或提交。

自然语言示例：“在 workbench/analysis.py 新建绘图脚本，只检查语法不执行”“用我给出的新密钥路径更新 eshell111-vasp544，保持计算配置不变，先不连接”。范围、接口、恢复和测试说明见 [WORKSPACE_EDITING.md](WORKSPACE_EDITING.md)。

## 按 token 预算管理上下文

默认使用估算 token 预算，统计系统指令、当前权限、工具定义和压缩后的对话。没有向接口查询真实容量，也不根据模型名称猜测分词器或窗口大小；采用UTF-8字节量估算，并根据当前模型请求返回的 input_tokens 保守校准。界面明确标注“约/估算”，上次API实际输入量单独显示，不冒充本次准确值。

每个 `models.toml` profile 可设置：

```toml
# 手动预算，不代表服务商已确认模型支持这个容量
context_window = 32768
output_reserve = 4000
context_safety = 2048
compact_trigger = 0.80
compact_target = 0.55
compaction_mode = "summary"
recent_context_tokens = 12000
```

可用输入预算 = 总预算 − 输出预留 − 安全余量。上例为26720；达到约21376输入tokens时尝试压缩，目标约14696。目标不是强制截断保证：近期完整轮次仍保留；压缩后估算仍超过输入预算，则停止主请求并提示处理，避免明知超预算仍调用模型。摘要请求本身也检查预算。输出预留同时设为接口请求的输出上限。

- `/context`：离线查看估算输入、配置容量、可用输入预算、占用比例、压缩阈值、目标和上次API输入用量，不调用模型。
- `/compact`：手动总结较早完整轮次，绕过自动触发间隔，会调用模型；无适合的旧轮次或总结失败时明确不压缩。
- 每次模型请求前显示输入占用，例如“上下文约8,017/26,720 tokens · 30.0% 输入预算”。百分比的分母是可用输入预算，不是模型总窗口。

配置优先级：命令行 → 已有环境变量 → `.env` → profile → 保守默认值。支持 `--context-window`、`--output-reserve`、`--context-safety`、`--compact-trigger`、`--compact-target`、`--compaction-mode`、`--recent-context-tokens`；对应环境变量前缀为 `VASP_AGENT_`，例如 `VASP_AGENT_CONTEXT_WINDOW`。切换profile加载相应预算，切换同profile下模型沿用该profile的显式预算；应由用户核实实际容量。

原始对话文件（`.agent/sessions/ai-session-*.json`）不删减，摘要缓存校验历史哈希后复用；大型工具输出可通过证据文件分页回读。默认按12000估算tokens预算保留尽量多的完整近期轮次，还受压缩目标与指令/工具开销限制；至少保留最新一轮，不拆开完整用户轮次和工具配对。压缩后保留此前的轮次间隔与增长量退避。摘要不是实时状态或权限依据。

重启Agent即可生效，不需要部署集群。显式指定 `--context-chars 60000` 进入旧字符模式；`--context-chars 0` 关闭自动压缩并停用token预算管理，手动 `/compact` 仍可使用。`/usage` 继续显示本次启动累计API用量，与 `/context` 的当前上下文占用不同。默认为结构化文字摘要；原生Responses压缩需显式选择 `--compaction-mode native`。详见[结构化摘要与测试](CONTEXT_SUMMARY.md)。

## 更换中转站后继续旧对话

会话按接口地址、协议和模型隔离。更改 base_url 后，旧文件仍保留，但不会出现在新接口的 `/sessions` 中。

重启后输入 `/import-session`，选择旧会话编号，核对目标接口和模型，再输入“导入”。程序创建当前接口下的新副本并切换，保留原文件；也可选择其他接口的归档会话。导入仅进行本地读写，不请求模型或 SSH。随后发送消息或使用 `/retry` 时，历史文字与工具结果将发送给新接口，因此菜单会明确要求确认。

为兼容不同接口，旧工具调用和结果转为标注的历史文字，不会重新执行；移除旧接口的加密推理及消息标识。导入不迁移提交授权或资源确认，切换会关闭提交权限。可用 `/history` 查看，再要求只读核查任务实时状态。文件名中的来源标识是哈希，不能据此还原旧接口网址。若没有可导入项，请检查启动时使用的工作区是否与以前相同。

## v0.7：默认值与 AI 建议的交互选择

需要先查看默认参数时，模型使用 `preview_scan_defaults` → `propose_scan_plan` → `prepare_scan_plan`。`preview_scan_defaults` 从实际结构、赝势和服务器配置计算默认值，不保存草案、不选择参数、不创建正式任务目录；远程结构解析仅使用随后清理的临时文件。`propose_scan_plan` 才保存正式建议并打开必要的选择菜单。启用参数草案流程时，原直接准备工具列入禁用状态，程序阻断其执行；固定工具目录不随此状态改变。

有差异时直接在终端询问：

```text
请选择：
1. 保留工具默认值
2. 采用 AI 建议
3. 自定义参数
4. 暂不决定
```

在上述参数选项菜单中，输入 4/q 才表示暂不决定，方案保持 pending，不能生成输入。空行（包括多行粘贴可能留下的换行）会提示并继续等待，不会自动取消或选择默认值。无交互终端时只保存待确认方案。稍后输入 `/plan` 选择方案并继续；`/plan 方案ID` 也可直接打开。原 `/plan 任务名` 继续查看已有本地任务。选择后可说“继续，按已选择方案准备任务”。

菜单显示工具默认值、AI 建议、理由以及未约化 k 网格点总数（仅作为规模参考，不等于实际耗时）。用户已明确指定完整序列时使用 user_explicit，直接执行并记录原指令；不能把 AI 自己的建议归为用户指定。程序会核对这条声明：所声称的每个数字都必须作为数字出现在用户本轮消息中（按数字边界匹配，520 不会命中 5200），否则返回 `ORIGIN_NOT_OPERATOR_STATED`，要求改回 ai_suggestion 并由菜单让用户选择。没有建议或建议与默认一致时无需重复选择。

例如：

> 在 eshell111 部署最新版，为硅提出新任务 si-plan-01 的非磁性三维 PBE 扫描方案，起始网格 12×12×12，NBANDS=16。比较工具默认值与 12³、14³、16³ 的建议，说明理由和代价，让我选择后再准备，先不提交。

自定义项输入完整 JSON：

```json
{"encuts":[520,598,676],"meshes":[[12,12,12],[14,14,14],[16,16,16]]}
```

ENCUT 和网格目前各三组，严格递增/逐轴加密。明确选择的序列不再套用倍率。若 ENCUT 低于现有赝势/优化策略下限，程序拒绝而不是静默提高。该下限是工具策略，仍不能取代收敛验证。

草案保存在工作区 `.agent/state/scan-plans/`，可跨重启恢复；同服务器/同任务的待确认或已选择方案不能被模型重新提议默认值所替换。同名但请求参数、建议或来源不同会返回 `SCAN_PLAN_CONFLICT`，不会假报成功取回旧结果。完全相同的请求复用草案，已确认的选择不重复询问。兼容旧对话中先用 `propose_scan_plan` 查询默认值的情况：仅 `selected / no_difference / tool_default` 且尚未准备的草案可补入 AI 建议，保留旧草案内容，并转为待选或立即打开菜单；取消选择后不能准备。其他改变目标参数的情况请由用户指定新的任务名。最终任务保存 `parameter-choice.json`，记录默认、建议、所选序列、来源及原指令；准备时再次核验结构、赝势、程序和默认配置指纹，变化则要求重新拟定方案。

实际生成后解析六组 INCAR/KPOINTS，核对 ENCUT、网格、Γ 中心及位移，写入 `scan-verification.json`；生成、审阅和执行前均检查一致性，任何差异都不能靠 AI 解释放行。回传结果包含选择记录和核对记录。

参数选择仅控制准备，之后仍需 `/physics`、阶段审阅、提交授权和明确提交要求。取消选择不会提交，也不会修改旧任务。

直接 CLI 可指定完整序列（属于操作者明确输入，不经过 AI 建议菜单）：

```bash
python remote_compute.py prepare --server eshell111 --job si-exact-01 --structure examples/Si.POSCAR --encuts 520 598 676 --meshes '[[12,12,12],[14,14,14],[16,16,16]]'
```

## v0.6：物理模型确认、结构化审阅和阶段放行

新任务默认分成四个 Slurm 作业，依次运行 convergence（收敛测试）、relaxation（两轮优化及复核）、scf、bands。每阶段完成后正常退出并释放资源，状态为 `awaiting_review`，等待本地 AI 审阅后再提交下一阶段。仅最后一阶段验收通过才是 complete。旧任务继续使用绑定的旧版程序，不自动迁移。

使用步骤：

1. 重启 Agent，请它部署最新版并在新目录准备任务，例如 `si-stage-01`，先不提交。
2. 输入 `/physics`，按编号选择服务器和任务（最近更新优先）；也可输入 `/physics eshell111` 直接选任务，或 `/physics eshell111 si-stage-01` 直接指定。随后填写研究目标，核对三维、非磁性、电中性、PBE、不含 SOC/U/杂化泛函的假设，输入“确认”。如果不适合材料则取消，当前程序不支持其他模型。该确认是操作者对模型的选择，不是结构自动推导出的科学结论。
3. 对 AI 说：“读取 si-stage-01 的当前阶段证据，完成结构化审阅，解释每项依据与阻断项，先不提交。”
4. 直接说（会打开统一资源确认菜单）：“提交 si-stage-01 已审阅的 convergence 阶段，返回作业编号。”
5. 阶段结束后说：“查询 si-stage-01 的状态，审阅已完成阶段结果，判断能否进入下一阶段。”每次下一阶段提交仍需有效额度和明确提交要求。

`/physics local 任务名` 用于本机任务。确认只能由操作者完成：模型没有能自行写确认的工具，工具请求确认时也只是由程序打开同一个菜单，选项仍由操作者输入。确认记录写明来源，`confirmed_by` 取 `operator_slash_command`（/physics）或 `operator_chat_menu`（对话中弹出的菜单），该值由实际运行菜单的那段本地代码写入，不从模型工具参数取值，因此记录无法声称一个操作者未用过的界面；缺少该字段的旧记录仍按原方式接受，但取值不属于操作者界面时会拒绝。任务开始提交后不能修改物理模型，需要新任务。

审阅工具 `get_stage_review` 返回当前阶段、required_checks、物理模型、实际输入、结果摘要和哈希证据；`record_stage_review` 每项填写 item、evidence、judgment、reason、next_action、blocking。审阅只能放行当前阶段，不能提前审阅未来阶段。只有初始 validation_plan 可 pending 且非阻断，物理模型和其余必需检查须 pass；否则记录为阻断。

| 待执行阶段 | 必需审阅项 |
|---|---|
| convergence | 物理模型、实际输入、验证计划 |
| relaxation | 物理模型、收敛证据 |
| scf | 物理模型、优化几何、优化后收敛复核 |
| bands | 物理模型、SCF 结果、电子数/能带数适配 |

输入、上游结果、结构或确认信息变化都会使对应审阅失效。阶段检查点封存证据哈希，已完成阶段的完成记录缺失时禁止自动重算。恢复时重新核验并读取已验收结果，绝不重新启动这些计算。数值验收失败不能被 AI 审阅覆盖。

`submit_stage` 必须显式给出 phase。每阶段独立登记提交记录，网络超时后禁止盲目重交；重复请求同一阶段返回原记录，不会推进下一阶段。提交后续阶段前还检查上一 Slurm 作业已正常退出。每个阶段消耗一次提交尝试额度（完整流程通常为四次）。已确认提交后自动登记只读定时监控，默认15分钟，可用 `/watch` 查看及控制。必须保持 Agent 运行，退出暂停、重启恢复；不会自动审阅或提交下一阶段。阶段间暂停不占用计算节点；多次排队可能增加等待时间。详见 [提交后的定时检查](JOB_MONITORING.md)。

回传文件包括 physics-model.json、checkpoint.json、review-阶段.json、REVIEW-阶段.md、geometry-summary.json、scf-summary.json。阶段审阅使用当前会话模型，不冒充独立专家或第二模型评审。

physics-model.json 中的 `assumptions_source` 固定为 `operator_assertion_not_structure_derived`：三维、非磁性、电中性、PBE 这些字段是操作者的声明，程序不读取结构去证明它们，菜单也直接说明结构文件无法证明。因此该记录读起来是“操作者当时这么声明”，而不是“程序验证出的材料性质”——确认错了的体系在事后能看出是需要重新确认的假设，而不是一个看起来由程序得出的结论。读取时该字段必须一致，手工删除或改写会被拒绝。

直接 CLI 提交新任务同样要求已确认并已审阅，且必须指定阶段：

```bash
python remote_compute.py submit --server eshell111 --job si-stage-01 --phase convergence
```

下方旧版“审阅一次后提交完整流程”的示例仅适用于旧任务；新版使用本节流程。

## 远程服务器连接

### 本机 AI 启动完整远程计算

新增工具：`remote_deploy` 上传计算程序；`remote_prepare` 上传结构并用服务器赝势准备；`remote_submit` 提交 Slurm；`remote_status` 查询队列/状态/日志；`remote_fetch` 下载报告、最终结构和能带图。

集群计算配置放在 `servers/<名称>.compute.json`，包含独立远程工作根目录、Python、PBE 库、VASP 环境脚本、分区、核数、时限。本项目已配置 eshell111，8 核、4 小时。不同服务器需要按实际路径配置，程序不猜测调度资源。新集群可用 `/setup-server <名称>` 只读探测后按菜单生成该文件，并可选在集群上建虚拟环境，见 [新集群接入](CLUSTER_ONBOARDING.md)。

开启模型提交能力：

```bash
python ai_agent.py --profile relay
```

远程任务直接明确要求提交当前阶段，Agent 会用 `request_submission` 打开统一资源确认菜单。可先用 `/submit` 保存默认资源；本机任务仍使用兼容的 `/submit once` 授权。

对话示例：

> 在 eshell111 部署计算程序，使用 examples/Si.POSCAR 和服务器 PBE 库，按非磁性三维 PBE 模型准备 si-remote-02，NBANDS=16，检查就绪后提交完整计算。

> 查询 eshell111 上 si-remote-02 的状态，解释失败日志。

> 下载 eshell111 上 si-remote-02 的结果。

不用模型也可以执行：

```bash
python remote_compute.py deploy --server eshell111
python remote_compute.py prepare --server eshell111 --job si-remote-02 --structure examples/Si.POSCAR
python remote_compute.py submit --server eshell111 --job si-remote-02
python remote_compute.py status --server eshell111 --job si-remote-02
python remote_compute.py fetch --server eshell111 --job si-remote-02
```

部署只上传六份计算源码，不上传 `.env`、模型密钥、SSH 私钥或本地赝势。源码按内容版本存放；任务使用新目录。远程计算不需要模型 API。准备与提交分开，提交前验证输入哈希。远程提交记录预先落盘，SSH 中断或 sbatch 返回不明确时保留不确定状态，禁止自动重交，需查询远程队列核对。

下载写入本地 `downloads/<服务器>/<任务>-<唯一编号>/`，校验 SHA-256，不覆盖已有结果，不下载 POTCAR。任务未完成时可下载已有中间验收数据；不代表已完成计算。状态查询是按需执行，不是持续后台监控。原 `/jobs` 等仍查询本机，可用自然语言指定服务器调用远程工具。

可在对话中给出 SSH 登录脚本的本地完整路径，并指定保存名称，Agent 会提取连接参数保存到 `servers/<名称>.json`。用 `/servers` 查看，`/connect 名称` 验证；也可直接要求 AI 浏览该服务器目录、读取计算日志或查询远程队列。详细格式与边界见 [服务器配置说明](../../servers/README.md)。私钥留在原位置，原始登录脚本不会被执行或上传给模型。

**SSH 连接复用**：安装 `python -m pip install -e ".[ssh]"`（paramiko）后，每台服务器只握手一次，之后的远程调用（计算调用、服务器信息/作业队列/远程 ls 与 tail、/setup-server 的探测与安装；wheel 上传除外）在同一连接上各开一个通道，类似 Xshell 保持会话；空闲 4 小时或退出 Agent 时关闭（期间每 15 秒发送保活）。主机密钥仍按 known_hosts 严格校验（未知或变化的密钥一律拒绝，不自动添加），认证来源与 `ssh` 相同（服务器记录的密钥、`~/.ssh/config` 的 IdentityFile、默认密钥或 ssh-agent），不读取或保存密码及口令，不转发代理。未安装 paramiko、配置使用 ProxyJump/ProxyCommand/HostKeyAlias/Match/Include、或连接失败时，自动改用原来的每次新建 `ssh` 进程方式，并只提示一次。请求发出后若连接中断，不会重发，按原有“响应丢失”处理（只读核对）。强制使用旧方式：`/settings ssh_transport subprocess` 或启动参数 `--ssh-transport subprocess`。

对比两种方式的耗时（只运行只读的 `deployment_info`，不触碰任务）：`python -m vasp_agent.remote.ssh_bench --server 名称 -n 5`。

## 对话内 `/` 命令

扩展命令已实现：

| 分类 | 命令 |
|---|---|
| 接口 | `/model [ID]`、`/profile [名称]`、`/connection` |
| 会话 | `/rename 名称`、`/export`、`/archive`、`/restore [名称]`、`/search 关键词` |
| 文件与方案 | `/workspace`、`/structure 路径`、`/potentials [结构路径]`、`/plan [任务名]` |
| 计算 | `/jobs`、`/logs 任务名`、`/diagnose 任务名`、`/results 任务名`、`/plot 任务名`、`/cancel 任务名` |
| 运行设置 | `/doctor`、`/usage`、`/settings` |

`/settings timeout 300` 调整接口超时；`/settings color always` 调整颜色；`/settings max_steps 15` 调整每回合循环上限。修改仅对本次启动生效。

`/model` 和 `/profile` 打开编号选择菜单，切换到所选接口/模型的独立会话，不搬运原会话内容。保留现有配置优先级，环境变量可能覆盖 profile 中的字段，切换后请查看显示的实际地址。`/connection` 会发出两个模型测试请求；`/diagnose` 会发送状态及日志片段给当前模型；`/jobs` 只调用当前机器的 Slurm，不会自动 SSH。

`/archive` 和 `/restore` 先选择“计算任务”或“对话”。`/archive chat` 直接把当前对话连同上下文缓存等附属文件移到 `.agent/sessions/archive/`；`/restore chat` 选择归档对话，原有 `/restore 对话名称` 仍可使用。归档其他对话用 `/sessions archive`（见下文“工作区运行文件与迁移”）。`/export` 输出到 `exports/`，包含对话文本，不包含密钥配置或工具内部记录。`/search` 限当前接口/模型并包含归档对话。

计算任务管理支持已配置的服务器和 `local` 本机，包括旧版任务：

```text
/archive                         # 选择计算任务，再选择服务器和任务
/archive task eshell111 si-plan-01
/restore task                    # 列出归档任务并恢复
/restore task eshell111 si-plan-01
/delete                          # 选择服务器和任务，展示删除预览
/delete eshell111 si-plan-01
```

归档仅添加标记，保留原路径和全部文件，从默认任务列表隐藏；不会取消已有作业。恢复后重新出现在列表中，归档状态下不能通过 Agent 提交。删除会展示绝对路径、工作流状态及已登记 Slurm 作业状态，并要求输入完整任务名。排队、运行、存在运行锁、提交记录不明确、无法核实 Slurm 状态或目录包含链接时拒绝删除。确认后会再次核对状态和文件变化；删除包括输入、日志和结果，不能通过 `/restore` 撤销。删除功能仅开放给操作者的斜杠菜单，不作为 AI 工具。远程操作依赖保存的 SSH 配置。

`/cancel` 仅接受任务目录中登记的作业编号，要求输入完整编号确认；这项显式本地操作无需开启模型的 `--allow-submit` 能力。`/plot` 需要运行机器具备图形环境。`/usage` 累计本次启动的聊天请求和 API 实际返回的 token 数据，不估算价格，不包括连接测试；未返回用量的请求不会被当成已知零用量。

输入 `/` 或 `/help` 显示菜单；`/new` 自动创建新对话，`/new silicon-02` 指定名称；`/sessions` 按编号选择旧对话；`/resume silicon-02` 直接恢复。

其他本地命令：`/history` 查看当前最近文字消息，`/config` 查看脱敏配置，`/status` 列出任务，`/status si-ai-01` 查看任务状态，`/clear` 清屏，`/retry` 继续请求，`/exit` 退出。

新建不会覆盖旧对话；清屏不清除上下文。菜单只显示当前接口、协议和模型下的会话。基础会话命令在本地执行；`/retry`、`/connection`、`/diagnose` 调用模型。

### 实时 `/` 引导

安装或更新 AI 依赖：`python -m pip install -e ".[ai]"`。重新启动后，输入 `/` 即显示下拉命令列表，每行带中文功能说明；继续输入可过滤。↑↓ 选择、Tab 补全、Enter 确认选项，再次 Enter 执行。Esc 收起菜单。输入命令后，底部显示参数用法和功能说明。普通文字不触发补全。

菜单仅读取本地命令定义，不调用模型。非交互输入、缺少依赖或终端不支持时回退普通输入，仍可用 `/help`。部分 Git Bash/mintty 与原生 Windows Python 的控制台接口不兼容，可用 `winpty python ai_agent.py --profile relay`，或在 Windows Terminal/PowerShell 中启动。

终端回复现在采用类似 CLI 助手的排版：正文使用终端默认色，标题、粗体、列表、表格和代码块由 Rich 渲染，不再整段显示青色 Markdown 源码；代码块支持语法高亮。用户输入和工具成功提示使用浅蓝色，工具执行中为青色、失败为红色。浅蓝色对应终端的 bright blue 色槽，实际色调取决于终端主题。默认自动检测颜色支持；`--color always` 强制开启，`--color never` 关闭。自动模式下重定向输出或设置 `NO_COLOR` 时不输出 ANSI 颜色，会话文件仍保存原始文字和结构化工具结果，不保存界面装饰。

工作过程显示模型请求等待、模型随工具调用返回的公开说明，以及每个真实工具调用的名称、目标、开始、成功/失败、耗时和简短结果。仅请求返回后才能展示其公开说明，不显示模型内部推理，也不是逐 token 流式输出。参数菜单等待时间包含在工具耗时内；“工具成功”不等于计算已收敛或作业已提交。

`/trace` 列出本次启动最近 20 条执行记录，`/trace 编号` 查看参数和结果；主输入处 Ctrl+T 可显示最近过程并保留正在输入的文字。过程缓存最多保留 100 条，单条结果详情最多 16000 字符；会话切换后仍是本次启动的记录，重启后清空。凭据字段和终端控制序列在显示副本中隐藏或移除。参数/授权菜单使用自己的按键设置，不启用 Ctrl+T。普通输入回退模式可使用 `/trace` 命令。

`prepare_scan_retry` 的确认菜单只显示来源任务、各参数收敛状态、实际扫描序列、固定值及必要的交叉复核说明，避免展开原子力、应力和哈希明细。界面摘要不改变原工具返回、模型收到的结果或审计日志。完整来源证据可通过 `inspect_convergence` 工具取得，再查看对应的 `/trace` 执行记录。

升级依赖并重启：`python -m pip install -e ".[ai]"`。新增 Rich 为本机显示依赖，不需要部署远程引擎。可先运行 `python -X utf8 examples/terminal_preview.py` 查看离线样例（无 API、SSH 或任务写入），加 `--html docs/previews/terminal-preview.html` 可导出排版预览。样例明确标为演示数据；实际 Agent 的工作过程只显示真实执行事件。

配色可通过本地命令设置，无需调用模型：

```text
/settings colors
/settings colors user bright_blue
/settings colors success cyan
/settings colors running yellow
/settings colors error bright_red
/settings colors reset
```

角色为 user（输入文字和提示）、success（完成）、running（执行中）、error（失败）。颜色支持 black、red、green、yellow、blue、magenta、cyan、white 及对应的 bright_ 前缀，default 表示终端默认色；实际色调由终端主题决定。更改自动保存到当前工作区 `.terminal-colors.json`，新输出立即使用，主输入在下一次提示生效，重启也保留。reset 恢复默认配色。`/settings color never` 和 `--color never` 仍可关闭颜色，配色设置不会绕过颜色开关。旧屏幕文字不会追溯重绘。正文/代码高亮及补全菜单背景不属于这四个角色。

## 自动选择本地 PBE 赝势

把同一套赝势放到 `potentials/PBE/Si/POTCAR`、`potentials/PBE/O/POTCAR` 等元素目录中，AI 会自动发现，按结构元素顺序拼接。也可显式指定 `--potcar-library /已有/PBE目录`。和 `--potcar` 二选一。

可直接说：“检查 examples/Si.POSCAR 需要的赝势，然后准备 si-ai-02 的非磁性 PBE 完整流程，先不提交。”

变体通过 [赝势库说明](../../potentials/PBE/README.md) 中的 `potentials.toml` 指定，AI 不静默更换 `_pv` / `_sv`。库缺失元素时停止，不会生成声称可运行的不完整任务。未配置任何赝势时仍支持预览。

普通 CLI 使用：`python vasp_band_agent.py prepare examples/Si.POSCAR runs/si-library --potcar-library potentials/PBE`。

## 接口超时后继续

默认请求超时为 180 秒，可用 `--timeout 300` 调整。网络超时/连接中断时保留对话，不退出交互界面；输入 `/retry` 使用保存的工具结果继续，不重新执行先前已完成的工具。不会自动重试付费模型请求。

旧进程已经退出时，用相同工作区、profile、模型和 session 重新启动，然后输入 `/retry`。单次模式也支持 `--prompt /retry`。如果中转站自身网关的超时更短，增大本地超时未必解决，需要检查服务商状态。

## 不知道模型名称时：先连接，再选择

仅需配置接口地址、协议和密钥，`model` 可以留空或保留占位值。

```powershell
# 分别获取所选账号可见的模型名称，不发送生成请求
python ai_agent.py --profile official --list-models
python ai_agent.py --profile relay --list-models

# 输入编号或完整名称选择，然后进入对话（q 取消）
python ai_agent.py --profile relay --select-model

# 选择后只测试协议与工具调用能力
python ai_agent.py --profile relay --select-model --test-connection
```

没有填写模型时，普通交互启动也会自动进入选择。选择覆盖本次运行的模型，不改写 `.env` 或 TOML；要保存默认选择，将选中的完整 ID 填入配置。已有错误模型名可直接用 `--select-model` 覆盖，不影响获取列表。非交互脚本需先列出模型，再用 `--model` 指定。

列表来自当前服务商的 `/models`，不内置或猜测名称。列表可包含音频、嵌入等不能驱动本 Agent 的模型，出现于列表不保证支持所选协议、工具调用或当前配额。使用 `--test-connection` 验证。中转站未实现列表接口时会明确报错，可按其文档手动填 ID，不自动更换接口。

API 密钥鉴权与模型名称是两件事：模型拼写错误可能使生成请求失败，但不应被统称为“登录失败”。获取列表失败时会区分鉴权、访问权限、列表接口不支持和限流。

依据：[OpenAI Models 列表接口](https://developers.openai.com/api/reference/resources/models/methods/list)。

## 配置密钥、接口和模型

复制 `.env.example` 为 `.env`，在本机填写 `OPENAI_API_KEY` / `RELAY_API_KEY`，并选择 `VASP_AGENT_PROFILE`。在 `models.toml` 的对应 profile 中填写真实 `model` 和 `base_url`。密钥正文只放 `.env` 或环境变量，不能放 TOML。

```powershell
Copy-Item .env.example .env
# 在编辑器中填写密钥，并修改 models.toml 中的占位模型和中转地址
python ai_agent.py --check-config
python ai_agent.py --profile relay --check-config
python ai_agent.py --profile relay --test-connection
python ai_agent.py --profile relay
python ai_agent.py --profile relay --model 实际模型ID
```

`--check-config` 完全离线，仅显示密钥是否存在；无密钥时退出码为 2。`--test-connection` 发出两个小请求，验证真实工具调用与工具结果回传，可能产生少量 API 费用；不会发送项目结构、日志或调用 VASP 工具。未通过就报告失败，不尝试其他接口。

优先级为 **CLI > 进程环境 > .env > models.toml**。`.env` 不覆盖已有进程环境，也不执行变量插值。配置文件默认从 `--workspace`（默认当前目录）读取；可用 `--config` / `--env-file` 指定路径。

| CLI | 环境变量 | TOML |
|---|---|---|
| `--profile` | `VASP_AGENT_PROFILE` | `default_profile` |
| `--model` | `VASP_AGENT_MODEL` | profile 的 `model` |
| `--base-url` | `VASP_AGENT_BASE_URL` | `base_url` |
| `--protocol` | `VASP_AGENT_PROTOCOL` | `protocol` |
| `--api-key-env` | `VASP_AGENT_API_KEY_ENV` | `api_key_env` |

协议明确选择 `responses` 或 `chat_completions`，共用同一套工具和执行校验。不同中转站对参数的支持可能不同，连接测试未通过时需按该接口文档修正配置；不会自动切换服务商或协议。使用中转站时，对话、结构摘要和日志片段会经过该中转站。

会话文件名自动附加接口地址、协议和模型的摘要，不会将原接口的会话自动发送到新接口。旧 v0.3 会话文件保留，但新版本不会自动载入。

本版在 v0.2 的完整计算工具上加入真实大模型工具调用循环：

**自然语言请求 → 模型选择工具 → 程序校验并执行 → 返回结果给模型 → 模型继续决策或回答。**

模型参与任务规划、文件检查、参数建议、日志诊断与结果解释。昂贵的 13 阶段计算仍由确定性工作流执行，不在每个离子步调用模型。本版不会在后台持续唤醒 AI；作业提交后返回，用户再次询问时读取最新状态。

## 本地启动

在项目目录运行：

```powershell
python -m pip install -e ".[ai,test]"
python -m pytest -q
```

在 `.env` 或当前终端环境配置所选接口的密钥，并在 `models.toml` 或环境中选择支持工具调用的模型。程序不硬编码默认模型。密钥不要写入受版本控制的文件或聊天。

```powershell
# 已在本机环境配置好密钥后，设置实际模型 ID：
$env:VASP_AGENT_MODEL = "你的模型ID"
python ai_agent.py --workspace .
```

也支持 `vasp-ai` 或 `python vasp_band_agent.py chat`。输入 `/exit` 退出。

可以直接说：

> 检查 examples/Si.POSCAR。按非磁性三维 PBE 模型，准备名为 si-ai-01 的完整计算流程，解释参数选择，先不提交。

没有 POTCAR 时会创建预览。模型读取的是结构摘要、日志片段和参数，不会收到 POTCAR 正文。

单次请求示例：

```powershell
python ai_agent.py --workspace . --session silicon --prompt "查看 runs 中的任务，告诉我哪些已完成、哪些需要处理。"
```

## 集群运行和提交

以下为直接在集群运行 AI 的旧入口，`submit_job` 在 AI 进程所在机器执行。现在也可保持 AI 在本机运行，使用本页前面的 `remote_*` 工具完成 SSH 部署与提交；该方式无需集群访问模型 API。

手动安装完整项目请按 [CLUSTER.md](CLUSTER.md) 复制入口、`src/vasp_agent/` 和项目元数据；安装使用 `python -m pip install -e ".[ai]"`。模型配置和密钥在运行环境单独设置，不随源码上传。配置好后：

```bash
cd /work/home/researcher/vasp-band-agent-v1
.venv/bin/python ai_agent.py --workspace . \
  --potcar /work/home/researcher/potpaw_PBE.64/Si/POTCAR \
  --submit-script /work/home/researcher/vasp-band-agent-v1/examples/eshell111.slurm \
  --agent-dir /work/home/researcher/vasp-band-agent-v1 \
 
```

此时可以说：

> 用 examples/Si.POSCAR，以非磁性 PBE 模型准备 si-ai-02，NBANDS 取 16，提交完整流程。

或者之后说：

> 查看 si-ai-02 的作业状态。如果失败，读取对应日志，说明证据、可能原因及下一步建议。

`--allow-submit` 表示操作者给本次会话开放提交能力；默认不开启。实际参数通过结构化工具校验，模型不能执行任意 shell、改写脚本或降低既定的力/应力验收标准。集群脚本仍由操作者指定并审核，其中资源配置是固定的。

每次启动默认最多提交一个新任务。已登记任务重复请求只返回原提交记录。若 sbatch 超时或返回不明确，写入 `submission_uncertain`，不会自动重交；请核对队列。已有失败目录不覆盖，模型可在用户要求下用新名称准备修订方案。

## 会话和记录

- `.agent/sessions/ai-session-<名称>.json`：完整对话和工具反馈，重新启动同名会话可继续。
- `.agent/audit/ai-audit.jsonl`（及轮转出的 `ai-audit.<序号>.jsonl`）：工具名称、参数、结果或错误，每条记录自带 `time` 和 `session`。
- `.agent/evidence/`：按 SHA-256 寻址的证据原文，压缩历史与超长审计载荷共用。
- `runs/<任务>/ai-plan.json`：模型给出的参数选择依据。
- `runs/<任务>/submission.json`：Slurm 作业编号及提交状态。

这些对话、结构摘要与日志片段会发送至你配置的模型 API。SDK 使用 `store=False`；这不意味着服务提供商不执行其其他适用的数据处理政策。会话文件保留在本地，应按项目数据管理。日志有基础密钥脱敏，但不构成所有敏感内容都能识别的保证。

单回合默认最多 10 次模型请求，每次最多 4000 输出 token；`--max-steps` 可设置 1–30。长对话不会自动摘要，必要时用新的 `--session` 并让模型重新读取任务状态。

## 工作区运行文件与迁移

Agent 自己写的文件都在工作区 `.agent/` 下（目录说明见仓库 README）。路径只由 `src/vasp_agent/common/agent_paths.py` 决定，其他代码不再写死文件名；文件格式没有变化，只换了位置。

**旧文件仍可用。** 旧版本把这些文件写成根目录下的点文件（`.ai-session-*`、`.ai-audit.jsonl`、`.context-evidence/`、`.agent-settings.json`、`.job-monitor.json` 及锁、`.model-capabilities.json`、`.scan-plans/`、`.submission-defaults/`、`.analysis-runs/`、`.workspace-edits/`、`.cache/`、`.ai-archive/`）。迁移之前，每一项按“`.agent/` 中有就用 `.agent/`，否则旧位置有就用旧位置，都没有才在 `.agent/` 新建”解析：已有的对话、方案、设置在原位置读取和更新，不会出现两份；新建的内容进入 `.agent/`。审计日志只追加，新记录总是写到 `.agent/audit/`，读取时先读旧 `.ai-audit.jsonl` 再读新分段，顺序不变。

**作业监控只有一份记录。** 监控记录和它的两个锁文件作为一组解析：只有旧 `.job-monitor.json` 存在而 `.agent/state/job-monitor.json` 不存在时，整组用旧位置；否则整组用 `.agent/state/`。新记录只会在没有旧记录时创建；两者都存在时以 `.agent/` 为准，迁移会把旧记录搁置到 `.agent/migration/parked/`。每次读写都在锁内重新确认位置，迁移移动记录后正在运行的 Agent 会自动跟随。

**迁移步骤（在终端执行，不在对话中执行）：**

1. 关闭所有 Agent 窗口（包括旧版本启动的窗口；旧版本不持有对话锁，迁移无法检测它们）。
2. 预览，只列出将移动的每个文件，不做任何修改：
   ```bash
   python ai_agent.py --workspace . --migrate-workspace
   ```
3. 确认后执行：
   ```bash
   python ai_agent.py --workspace . --migrate-workspace apply
   ```

迁移只移动（改名）文件，不删除、不改写内容：旧副本若在 `.agent/` 已有对应文件（`.agent/` 的那份才是正在使用的），以及无法识别的临时文件，都移到 `.agent/migration/parked/`；搬空的旧目录会被移除。每次移动前后都写入 `.agent/migration/migration-record.jsonl`（源路径、目标路径、sha256、字节数）。中断后重新执行 `apply` 会先根据记录补齐中断的那一步，再继续剩余文件；重复执行不会再移动任何文件。另一个 Agent 进程正在运行后台监控、正在写监控记录、任何对话处于打开状态或另一个迁移正在进行时，迁移拒绝执行且不移动任何文件；迁移进行中启动 Agent 也会被拒绝。需要撤销时：

```bash
python ai_agent.py --workspace . --migrate-workspace revert-plan
python ai_agent.py --workspace . --migrate-workspace revert
```

撤销按记录逆序移回；迁移后已被继续写入（哈希变化）的文件会保留在 `.agent/` 并列出。`servers/*.json`、`models.toml`、`.env`、`runs/` 等操作者数据从不移动。对话内 `/workspace migrate` 只显示预览和上述命令；`/workspace layout` 显示 `.agent/` 各目录的文件数和根目录尚未迁移的旧文件数。

**对话归档。** `/sessions` 默认只列出未归档对话；`/sessions archived` 列出归档；`/sessions archive 名称…` 把对话及其附属文件（`.context.json`、`.context.parts.json`、`.context.errors.jsonl`、`.cache-metrics.jsonl`）一起移到 `.agent/sessions/archive/`；`/sessions archive older 30` 列出 30 天未修改的对话，输入“归档”确认后移动；`/sessions restore [名称]` 移回。其他接口的对话用 `名称@接口标识` 指定，恢复后仍可按 `/resume 名称@接口标识` 跨接口继续。当前对话请用 `/archive chat`；在另一个 Agent 窗口打开的对话会被跳过。归档名已被占用时自动加后缀，恢复时同名对话已存在则拒绝；从不覆盖或删除。

## 本次验证边界

自动测试使用模拟模型响应验证多步工具选择、输出回传、错误处理、会话保存、提交去重和超时防重交，不消耗 API 额度。本次开发环境没有配置 API 密钥，因此尚未做真实模型端到端联调，也没有提交真实 VASP 作业。

实现依据：[OpenAI 官方 Function calling 文档](https://developers.openai.com/api/docs/guides/function-calling)。

## 对话内提交权限

- `/submit` 或 `/permissions`：同一个默认资源设置界面，按服务器及扫描/串行阶段类型保存；不授权、不提交。
- `/submit once`（兼容入口）：配置服务器和资源上限，允许一次提交尝试，用完自动关闭。
- `/submit on`（兼容入口）：同样配置资源，设置 1–10 次提交尝试额度。
- `/submit off`：立即阻止后续提交，不取消已经运行的作业。
- `/submit status`：查看当前授权范围与剩余额度。

查看和准备默认开启。提交默认关闭；切换、新建、恢复对话或切换模型/接口后关闭，重启也不会恢复历史授权。取消菜单或输入无效值保留原权限。授权只保存在内存，审计日志会记录变更。默认资源设置菜单不会发起模型请求或启动计算；带明确任务阶段的提交菜单在操作者确认后可进入审阅/提交预检。

一次指一次 Slurm 作业提交尝试；新版任务只执行当前阶段，旧任务可能执行完整工作流。远程请求失败或结果不明确也会消耗额度；先查询状态，禁止盲目重交。提交时核验实际脚本的分区、节点数、总核数（任务数 × 每任务 CPU 数）和时长，并清除 SBATCH_* 环境覆盖。超出上限或无法解析的资源指令将被拒绝。改变上限不修改已准备任务的资源配置。

`--allow-submit` 不再开启权限，请使用交互菜单；单次 `--prompt` 模式不支持授权提交。直接执行 `remote_compute.py submit` 属于操作者手工提交入口，不受 AI 对话权限开关控制。

取消作业仍需原有 `/cancel` 确认；不新增覆盖任务或远程删除能力。提交授权不会开放任意远程 shell。

## 并行收敛测试

默认保持串行，避免自动增加资源。准备新远程任务时可以明确要求：

> 在 eshell111 部署最新版，使用 examples/Si.POSCAR 准备新的 si-parallel-01，按非磁性三维 PBE 完整流程，NBANDS=16。独立测试最多同时运行 3 个，每个测试 8 核，先不提交。返回输入参数和总资源配置。

`remote_prepare` 的 `parallel_tests` 范围为 1–6，`cores_per_test` 为每个测试的 MPI 进程数（每进程一个 CPU）；null 沿用服务器配置。服务器配置可选同名字段，未配置时并发数为 1，每个测试核数沿用原 ntasks。总核数 = 并发数 × 每个测试核数，目前单节点、总核数最多 256，能否分配取决于集群节点与配额。

```bash
python -m vasp_agent.remote.remote_compute deploy --server eshell111
python -m vasp_agent.remote.remote_compute prepare --server eshell111 --job si-parallel-01 --structure examples/Si.POSCAR --parallel-tests 3 --cores-per-test 8
```

在默认 `allocation` 模式下，3 × 8 配置会申请一个24核Slurm作业。统一菜单确认后仅授权本次目标阶段的资源及一次提交尝试，不必先填写另一张上限表。兼容的 `/submit once` 路径仍检查先前授权上限。

在 `allocation` 模式下，六组初始扫描及三组优化后复核分别以有上限的并发执行；所有结果验收通过才继续下游。每个测试使用独立目录和日志；状态报告列出各子测试状态。若执行失败，取消尚未开始的测试，等待已经启动的测试结束，然后停止流程；保留已经产生的结果，不自动重交。数值收敛不通过也会阻断后续优化或能带。

两轮优化、SCF 和能带按依赖顺序运行。逐阶段任务现在可在每次提交前分别选择资源；优化、SCF 和能带阶段的表单固定并发数为 1。旧版一次执行完整流程的任务仍可能在串行阶段闲置部分核。程序不动态调整正在运行的 Slurm 作业，也不保证排队加计算的总耗时更短。

### 审计日志的会话关联键、脱敏与超长载荷（2026-10-11）

审计日志的每条记录现在由写入方加上 `time`（UTC ISO 8601）和 `session`。两者都由 `Toolbox.log` 写入，不由调用点提供，因此不可能漏写，也不会被同名载荷字段覆盖——记录无法给自己的日期或会话造假。

`session` 是审计日志、任务目录 `events.jsonl` 和远程请求之间的关联键。它经 `VASP_AGENT_SESSION` 环境变量传递：本机提交时进入 sbatch 环境，远程请求随 payload 送交 worker（名称在那边按 `[A-Za-z0-9][A-Za-z0-9_-]{0,63}` 校验，因为它会成为 `--export` 参数），再由与 `VASP_AGENT_PHASE` 相同的 `--export=ALL,...` 通道带到计算节点，供 `event()` 写入。切换 `/model` 或 `/profile` 会刷新它，因为会话名携带接口身份。

密钥脱敏改为在序列化之前作用于叶字符串：此前对整条序列化记录套用行首锚定的密钥模式，任何载荷里出现一次 `password: x` 形状的子串都会把整行替换成脱敏标记，静默丢掉一条同域审计。现在记录本身保留，只有该值被替换；键名本身标记为密钥的字段整值替换为 `[REDACTED SENSITIVE CONTENT]`，而不是按模式匹配。

单条载荷超过 6000 字符时不再截断，而是按内容寻址转存到 `.agent/evidence/<sha256>.json`（与上下文压缩共用同一目录），记录中留下含 `sha256`、字符数和 600 字符预览的存根。工具结果的 `ok`、`error_code` 等可按字段查询的键仍内联，只有超长的 `result`/`error` 被转存，因此记录仍能区分成功与失败。全文用 `read_context_evidence` 分页取回，哈希校验保证往返可验证。这是原文，不是摘要；但它与压缩历史共用保留期限，将来清理 `.agent/evidence/` 会同时影响两者。

### 远程 convergence 作业数组（2026-10-10）

远程阶段式收敛可明确选择 `submission_mode=array`；旧默认仍为 `allocation`。在资源菜单填写 `模式=array 并发=6 每组=16 节点=1 时限=04:00:00`，核对新预览后选择1确认。数组的节点数为每个元素的节点数；这个例子授权峰值为96 CPU、6节点，每个元素各有4小时Slurm时限。最大并发为6且不能超过当前实际测试数，冻结清单为3/4/6/7项；保持原科学扫描，不扩展成九组合扫描，不自动优化科学或MPI参数。

数组元素独立排队，`squeue -r` 可展开。全部元素回执及真实VASP输出验收通过后才放行下一阶段审阅；失败、超时、缺回执或缺少调度证据均不能放行。最后worker负责汇总，不额外提交collector作业；`remote_status`在数组终态时可补做输出验收。仅适用于远程 `convergence`，后续阶段使用 `allocation` 单组计算。

先 `remote_deploy` 最新版，再准备新任务；旧任务绑定release不迁移，已有排队或运行作业不能转换，本例不修改71251802。功能已做mock调度与合成输出回归，未宣称真实集群数组验证。完整对话、菜单、资源含义及验收流程见[远程Slurm数组收敛提交](SLURM_ARRAY_SUBMISSION.md)。

### 每次提交前确认资源

远程任务直接要求提交当前阶段，Agent 使用 `request_submission` 打开统一菜单，优先展示用户已保存的默认资源：

```text
资源确认：服务器 / 任务 / 当前阶段
提交模式：allocation（未指定模式时的默认值）
并发 6 组 × 每组 16 MPI；节点 2；分区 compute；时限 04:00:00；总计 96 核。
1. 确认以上资源并授权本次阶段提交
2. 保存为默认，并确认本次阶段提交
3. 逐项修改
4. 取消
5. 自定义参数（末行直接填写）
输入编号或参数，例如 并发=6 每组=16 节点=2 时限=04:00:00（回车/q 取消）：
```

自定义输入是严格的“参数=值”格式，不执行代码、不交给模型猜测；可用中文键或 `parallel_tests/cores_per_test/nodes/partition/walltime/submission_mode`，收敛阶段支持 `模式=array` / `模式=allocation`，也可单独输入 `array` / `allocation`。填写后返回预览，仍需选1或2确认。默认参数按工作区、服务器和阶段类型保存在 `.agent/state/submission-defaults/`，包含五个资源字段及可选提交模式，无授权字段。扫描默认并发数在剩余测试更少时会明确展示调整；优化/SCF/能带共用单组默认，扫描默认不会误用到串行阶段。已确认菜单仅授予本次任务和阶段一次提交尝试，另一个任务或阶段必须再次确认。重启/切换对话后只保留默认参数，不恢复授权。

`/submit` 和 `/permissions` 无参数时是同一离线默认资源菜单，选择服务器、阶段类型后确认保存；不会自动准备或提交。当前末行提供文本输入，复用已有菜单输入后端，并非复刻Codex的完整图形界面。本机计算仍使用既有 `/submit once`，本次合并针对远程资源确认。

AI 可先调用 `get_submission_resources` 读取真实配置和授权上限，再调用 `recommend_submission_resources` 提出并发数、每组 MPI 进程数及理由。无已保存默认时，表单展示有效 AI 建议作为候选，用户仍可修改；建议绑定任务阶段和配置签名，过期或超限时不会采用。没有有效 AI 建议时，明确显示“程序默认建议”，收敛阶段最多默认 3 组并发，受授权总核数限制。两类建议都不等于节点容量探测或性能测试结论。授权 128 核不表示必须申请 128 核。当前引擎支持 1–256 个节点、最多 6 组并发，总 MPI 进程数不超过 65536，仍受操作者授权上限限制。每组 MPI 进程数必须能被节点数整除；`allocation` 的每组跨全部申请节点均匀分配，各组通过独占 CPU 的作业步并发；`array` 的每项跨本项节点均分，授权另按并发节点峰值检查。节点的实际容量仍须与集群配置相符。其他阶段不询问测试并发数，只询问适用的资源项。输入非整数会原地提示重填；选择返回修改会保留刚填写的值作为默认值。

选择 1 后，程序更新任务的 `config.json` 和 `submit.slurm`，重新记录配置哈希；不修改科学输入参数。资源发生变化会使原审阅快照失效，工具返回 `awaiting_resource_review`，AI 应读取新上下文并重新审阅，通过后继续提交。同一会话内同一任务阶段的同一配置不重复询问，切换会话、重启或重新授权后需要重新确认。取消、超限或审阅失败均不会消耗提交次数；实际调用提交时才消耗次数。排队、运行中或已有该阶段提交记录的任务不能通过表单修改资源。

本机入口也要求使用受控的 `srun vasp_std` 脚本，并显式设置 `OMP_NUM_THREADS=1`；任意自定义启动脚本不会自动改写为并行运行。非交互模式缺少操作者输入时拒绝提交。

独立命令 `python -m vasp_agent.remote.remote_compute submit --server eshell111 --job 任务名 --phase convergence` 也通过同一个资源及本次授权确认菜单，不再直接发送提交 RPC。CLI 没有模型审阅循环：修改资源后若需重新审阅，会保存配置并停止，提示回到 AI 对话继续；没有有效审阅不会提交。非交互重定向输入不支持 CLI 提交。这是应用入口的交互约束，不限制操作者自行使用 SSH/Slurm 提交命令。

实现使用 Slurm `srun --exclusive --exact` 限定每个计算步骤的资源，说明见 [Slurm srun 官方文档](https://slurm.schedmd.com/srun.html)。并发模式仅支持受控的 `srun vasp_std` 工作流启动入口。

## AI 参数审阅与带注释的 INCAR

终端重点显示已增强：Markdown 粗体默认使用浅蓝色加粗，行内代码使用浅青色加粗并保留终端默认背景，标题及表头使用亮青色，代码块使用蓝紫配色。引用提示也会突出显示，仅改变样式，不自动把文字解释为警告或计算成功。可用 `/settings colors emphasis bright_blue` 更换正文重点颜色，设置会保存；`/settings color never` 仍可关闭颜色。重启 Agent 后用于新输出，历史屏幕不会重绘；已有自定义重点颜色保留。

### 已收敛参数复用与选择性扫描

对 AI 说明来源任务及新任务名，例如：

> 检查 eshell111 上旧任务的 ENCUT 和 k 网格各自收敛状态。如果 ENCUT 的实际证据通过，请在新目录 si-mesh-only-02 中复用该结果，固定最高已测试 ENCUT，只扫描 12³、14³、16³。先显示方案让我选择，准备后不要提交。

工具 `inspect_convergence` 从真实 vasprun.xml、输入和完成记录重新计算能量、力、应力比较，不依赖聊天陈述或任务整体成功状态。返回每个参数的 passed / failed / unavailable。即使另一参数失败，已通过的参数仍可复用。

`prepare_scan_retry` 支持 `both`（全部）、`mesh`（仅网格，复用 ENCUT）、`encut`（仅截断能，复用网格）。每次用新任务目录，继承来源结构、POTCAR、模型、NBANDS 和阈值；菜单确认后创建。被复用参数的原扫描序列不改变，固定采用最高测试值。只生成待执行扫描的目录；真实来源证据复制到 `reused-evidence/` 并纳入 manifest，`convergence-reuse.json` 记录来源、比较结果和适用条件，没有伪造完成标记。再次扩大未通过参数时也可从重试任务继续复用。

复用只证明原固定条件下通过。例如旧 ENCUT 测试固定 12³，新网格增大到 16³，不会声称 ENCUT 已在 16³ 重新通过；这一差异交由阶段审阅评估。优化后的 higher-cutoff / denser-mesh 复核仍保留。若来源实际输出缺失、损坏、电子不收敛、输入不一致或参数比较未通过，禁止复用。新任务仍需 /physics、阶段审阅及提交授权。

准备摘要和提交表单会显示 compute/reuse 清单及本次测试数。现有已提交任务不修改。远程使用前需部署包含 `convergence_reuse.py` 的新版计算引擎；本机重启 Agent。验证使用模拟 VASP 输出，不自动启动集群计算。

新准备的完整流程会生成：

- 六组初始扫描的实际 INCAR、POSCAR、KPOINTS；INCAR 带简短英文注释，保持参数解析不变。
- `input-plan.json`：完整 13 阶段的 INCAR 方案、可由准备入口确定的 KPOINTS（relax-1、relax-2 及三组复核；SCF 和能带随优化后原胞生成，不预登记）、准备入口值、默认来源和待验证项。
- `PARAMETERS.md`：中文选择依据、适用边界及各阶段参数；这是代码说明，不代表 AI 已审阅。
- AI 审阅后生成 `ai-review.json` 和中文 `AI_REVIEW.md`，并在 `reviews/` 保留历史记录。

示例对话：

> 在 eshell111 部署最新版，使用 examples/Si.POSCAR 在新目录 si-review-01 准备非磁性三维 PBE 完整流程，NBANDS=16。读取实际输入并进行 AI 审阅，解释参数来源、理由和待验证项，先不提交。

AI 调用 `get_input_review` 读取实际初始输入、赝势元数据及后续生成规则，再调用 `record_input_review` 记录中文说明。server=null 时审阅本地任务。模型身份由程序记录；无须服务器访问模型 API。这里只复用当前 AI 会话审阅，不冒充独立第二模型或专家审查。

结论分为 `pass`、`needs_validation`、`blocked`。初始方案尚未经过收敛测试，通常使用 needs_validation，表示允许按工作流先做验证；不支持的磁性、SOC、U、HSE 或未确认的关键物理假设应阻断。AI 说明应区分用户指定、AI 建议、代码默认和测试依据；代码自身只标“准备入口参数”，不猜测用户是否明确指定。

新任务没有有效审阅或结论为 blocked 时不可提交。审阅绑定输入与提交脚本快照，改变后必须重新审阅。AI 审阅不提供提交授权，仍需用户明确提交要求及操作者在统一菜单确认。新任务的直接 CLI 提交或运行也检查审阅记录。

后续几何与 KPOINTS 在优化后生成，提交前审阅的是规则。运行时检查生成的 INCAR 与方案一致；方案登记了 KPOINTS 的阶段同时比对采样，其余阶段（SCF、能带）的网格不预登记，改由阶段证据核对：SCF 网格写入 `scf-summary.json` 并随 SCF、能带审阅回读，`bands/KPOINTS` 计入能带审阅校验的哈希。继续执行原有哈希、阶段依赖和收敛验收；需要改变参数时准备新任务并重新审阅。AI 判断不是科学正确性的保证。

旧任务不修改、不自动补注释或强制追溯审阅，需要此功能时使用新目录。部署新版不会改变旧任务绑定的计算版本。远程结果回传包含参数说明、方案和当前 AI 审阅文件。

INCAR 注释采用 `#`，格式依据 [VASP 官方 INCAR 文档](https://vasp.at/wiki/INCAR)。


### 多节点 MPI 布局

以下多组共享整个分配的示例使用 `allocation`。`array` 的节点数按每元素计算，授权按并发节点峰值核验，见[数组资源说明](SLURM_ARRAY_SUBMISSION.md)。

资源表单按项即时校验，错误只要求重填当前字段，先前有效输入保留。填写每组MPI进程数后，先显示总核数（并发组数×每组进程数），再询问申请的物理节点数；节点字段不能填写总核数。节点候选仅表示满足均分规则和节点授权，不代表已验证服务器容量。并发数同时受本次实际独立测试数量约束，输入9而只有6个测试时立即提示重填。分区、运行时限和授权上限也逐项报错；返回修改沿用刚才的有效值，q取消不应用资源。

例如6组×每组16个MPI进程，总计96核。当前布局要求每一组都跨全部申请节点均分，节点数必须整除16：在节点授权允许时，可选1、2、4、8、16，不能选6、48或96。若选2节点，每节点总计48进程，每组在每节点8进程，必须确认每节点可用CPU容量支持这一布局。若选1节点则要求单节点容纳96进程；节点数不是并发组数，程序不会擅自改成每组独占一个节点的另一种布局。

例如 3 组并发 × 每组 16 个 MPI 进程，申请 2 节点：总计 48 核，每节点申请 24 核，每组在每节点使用 8 核。提交脚本明确设置 nodes、ntasks、ntasks-per-node，每个 srun 使用对应的节点数、每组进程数和每节点进程数。优化、SCF、能带同样可跨节点，但不并发执行互相依赖的计算。运行时检查实际节点数和总进程数。

旧任务绑定的旧发布版本不支持此布局，程序会在修改前阻止：先让 AI 部署最新版，再准备新任务；无需修改正在运行的作业。新功能已做模拟 Slurm 参数验证，实际跨节点 MPI 连通性、VASP 环境和每节点容量需在集群测试。

### 准备摘要与只读错误恢复（2026-09-24）

`remote_prepare`、本机 `prepare_calculation` 和已选方案的 `prepare_scan_plan` 现在保留原返回字段，并增加 `preparation_summary`。摘要从生成文件读取，核对 manifest 哈希和实际扫描输入，包含赝势元数据、扫描序列、资源、来源及缺失字段；不返回 POTCAR 正文。`ready=false / preview_only=true` 表示缺少 POTCAR 的不可提交预览。文件一致性、ENMAX、来源目录和 TITEL 均不构成收敛结论或发行版本认证，来源文件哈希也不等于拼接 POTCAR 哈希。

新旧任务均可用 `get_input_review` 查看输入及赝势摘要，无需 `/physics`。`get_stage_review` / `record_stage_review` 的物理确认和证据门禁不变；`record_input_review` 仍仅适用于旧版任务。

当通用 `remote_operation(read)` 拒绝读取赝势摘要时，程序仅在路径严格匹配已保存服务器配置的 `<root>/jobs/<合法任务名>/potential-selection.json` 时给出结构化 `next_action`，并自动调用 `get_input_review`。未知路径、嵌套目录、路径穿越和任意文件内容都不能触发替代动作。同一轮每个目标最多自动恢复一次，总计最多两次；原失败保持 `ok=false`，独立结果放在 `recovery.output`，UI 和审计日志分别记录两次执行。恢复失败保留真实错误，不重放准备或提交。模型调用轮数仍受原有 `max_steps` 限制，API 超时由 `/retry` 继续已保存历史，不自动重试付费请求。

错误结果保留 `error`、`type`，新增 `error_code`、`recoverable`，已知类别附带 `next_step` 或安全的 `next_action`。物理确认、授权、文件缺失、哈希不符和 SSH 失败有不同类别；未知或旧版远程错误保留原文，不按中文消息猜测恢复动作。

### 模型接口 HTTP 错误恢复（2026-09-28）

超时和连接异常现在分别显示 `APITimeoutError` / `APIConnectionError`，并显示本次模型调用的实际等待秒数、当前客户端超时设置和可识别的底层原因。耗时使用单调时钟测量，不包含之前工具执行或上一次重试；超时配置是连接、读取等阶段的限制，不是保证等待时长，也不是整轮对话的总时长。`/settings timeout 600` 修改后，下次请求诊断会显示新的设置。

底层异常链只转成固定类别（如 DNS、TLS、连接重置、读取超时或 HTTP 协议异常），不输出原始错误正文、请求网址或凭据。未取得足够信息时显示“无法进一步定位”，不推断是服务商故障或密钥问题。连接异常可能远早于配置超时；延长超时不能修复已经断开的连接。诊断改进不增加自动重试或流式请求。

对话请求遇到 HTTP 502、503、504 等服务端错误或 429 限流时，保留当前会话和已完成的工具结果，返回输入提示，不退出交互程序，不自动重复付费请求。根据有效的 `Retry-After` 响应头（秒数或 HTTP 日期）或响应体 `retry_after` 显示等待时间；多个有效值取较大者。仅接受 0–604800 秒范围；缺失或异常值提示稍后重试。401/403 单独提示核对令牌及模型权限；其他请求错误提示检查配置，不声称等待即可恢复。

看到“至少等待 60 秒”时，等待后输入 `/retry`。旧版本已退出时，重新运行 `bash start-agent.sh`，用 `/sessions` 选择原对话，再 `/retry`；不必 `/new` 或重复发送原请求。不要将 HTTP 网关故障当作 POSCAR、SSH 或计算失败。接口异常返回的原始正文不直接显示，以免带出凭据或不可信指令。此处理适用于对话请求；独立的 `--test-connection` 失败仍会报告错误并退出。

本地恢复改进不能修复中转站源站故障；反复出现时请服务商核查。它不改变计算任务审阅、授权或提交恢复规则。

本机退出后重新执行 `python ai_agent.py --profile relay` 即可加载客户端改进。远程核心模块需要在准备新任务前按既有流程部署新版；本次改动不自动部署、修改已有任务或切换其 release。旧 release 没有摘要函数时，准备结果明确返回 `preparation_summary.available=false`；其 `get_input_review` 校验能力仍以绑定版本为准，旧错误可能只有 `REMOTE_ERROR`。


## 提交容量检查与异常回执诊断（2026-09-28）

远程提交预检和实际调用 `sbatch` 前，现在都会通过只读 `sinfo`、`scontrol` 查询目标分区，检查均分布局下的每节点 CPU、分区默认内存和满足两者的节点数量。查询失败或格式未知时停止；容量合格不代表节点当前空闲、账户配额足够或调度必然成功。当前受控脚本使用分区默认内存，不支持由此检查放行显式内存参数。

例如在 `allocation` 模式下，分区节点最大为 64 核，6 组并发 × 每组 16 MPI 进程 = 96 核，不能申请 1 节点。2 节点意味着每节点 48 核，每组在每节点运行 8 个 MPI 进程；还需满足内存、分区及账户限制。这个例子不是性能最优建议。`array` 模式可为每项申请1节点/16 MPI，6并发按峰值6节点/96 CPU授权。修改资源仍需操作者确认和重新审阅。

提交异常会在阶段提交记录的 `submission_diagnostic` 中保存时间、命令、返回码、脱敏且限长的 stdout/stderr。超时或不可解析的 JobID 保持 `submission_uncertain`，不能自动重交。非零退出同样保留记录供核查。脱敏只能识别部分常见凭据模式，不应主动把秘密传入调度参数。旧任务缺失的原始输出无法事后补回。

可以直接说：

> 只读诊断 eshell111-vasp544 的 Si_pbe_cell，阶段 convergence，平台作业编号 70292806。核对提交回执和历史记账，不修改记录、不提交。

Agent 使用 `get_submission_diagnostic`；JobID 必须来自真实工具结果或操作者，不能猜测。工具核对历史作业名称和工作目录，查询为空也不能证明没有提交。该工具不修复回执、不清除提交占位，也不授予重新提交权限。

本地程序重启即可加载新工具；远程 RPC 会传送当前 worker，保留任务原有绑定引擎。本次修复不自动部署或修改现有任务。对已经失败且回执不明的任务，保留原目录及证据，核实历史状态后可另行明确要求在新任务目录准备修正方案；不得通过手改原回执绕过防重复提交。


### 无 JobID 时自动查找历史作业（2026-09-28 更新）

`get_submission_diagnostic` 的 `job_id=null` 现在先使用回执中的编号；回执也无编号时，自动按当前 SSH 用户、`va-任务名` 和有界时间范围查询 `sacct`，再精确核对名称、工作目录。无需先到平台手动查编号。

默认时间范围优先采用回执 `submission_diagnostic.started_at` 前后一天；旧回执使用文件修改时间前后一天，并标记 `receipt_mtime_estimate`（只是查找线索，不是提交成功证据）；没有回执则查最近7天。`start_time`、`end_time` 可同时指定 ISO 时间，跨度最多31天；无时区按远程服务器本地时间解释。返回实际查询范围，结果为空时可据此调整窗口，不能认定从未提交。

唯一完整匹配返回 `discovered_job_id`；多个匹配返回 `lookup_status=ambiguous` 及候选列表，不自动挑选；解析不完整返回 `incomplete`，查询失败返回 `query_error`。最多展示100条候选并标记截断。同一目录的多个阶段可能同名，因此 `phase_verified=false`，找到编号不等于确认阶段，也不授予重交权限。所有查询保持回执字节不变。

例如：

> 自动查找 eshell111-vasp544 / Si_pbe_cell 的历史 JobID，convergence 阶段，先核对回执和作业目录。仅诊断，不提交。

> 把历史查询时间范围设为服务器本地时间 2026-09-28T17:40:00 到 2026-09-28T18:30:00，继续只读查找。

重启 Agent 后新工具描述生效；不需要重新部署计算引擎。


### 综合提交故障诊断

同一个 `get_submission_diagnostic` 入口现在还会汇总历史申请的分区、CPU/节点/内存、ReqTRES、分配资源和运行时长；读取当前 `submit.slurm` 的资源指令、文件哈希及 `config.json` 中的并行布局；通过只读 sinfo/scontrol 检查当前分区容量。唯一匹配作业时读取固定 `slurm-<JobID>.log` 尾部（最多12000字节、脱敏），不跟随链接、不执行任务脚本。记账或容量查询失败时保留其他可用证据，并避免重复查询同一分区。

返回 `diagnosis.confirmed_configuration_findings` 和 `resource_layout_candidates`。例如 96 核/1节点对比最大64核节点会指出容量冲突；6组×16 MPI/3节点会指出每组无法均分。候选保持组数和每组进程数，最多列出3种满足当前硬件容量及均分规则的布局，不代表账户配额可用、性能最优或已获提交授权。

历史申请来自 sacct，当前脚本明确标记不是历史快照。历史资源与当前分区的冲突不证明当时失败的唯一根因；历史 ReqMem/ReqTRES 单独展示，默认内存比较使用当前分区配置。多候选时不擅选作业日志，记账容量比较最多处理前三条。历史精确错误仍可能需要管理员，但不应因此忽略已有配置冲突。

可直接说：“综合诊断 Si_pbe_cell 的提交失败，自动查找 JobID，比较历史资源与当前分区容量，检查日志并提出修正建议；只读，不提交。”重启本地 Agent 生效，无需重新部署引擎。


## 受限 Python 分析运行

自然语言即可使用，无需记忆新斜杠命令：

> 在 workbench 编写 Python 脚本，分析我放入的 data.json，先验证，再在隔离环境运行并解释结果。

Agent 用编辑工具写脚本，读取脚本/输入SHA-256，再调用 `validate_analysis_script`、`run_analysis_script`。`get_analysis_runtime` 检查环境，`get_analysis_run` 读取持久化回执。脚本放在容器 `/analysis/main.py`，显式输入映射为 `/analysis/inputs/0/<文件名>`、`inputs/1/...`，以验证返回的manifest为准；`python -I` 不自动导入同目录模块，首版建议单文件标准库分析。脚本结果通过 print 输出，不导出生成文件。

必须由用户预先安装并启动本地 Docker 的 Linux 容器环境，预装 `python:3.12-slim`（例如用户自行执行 `docker pull python:3.12-slim`）。工具不安装Docker、不拉取镜像，运行时固定使用已解析的镜像ID；拒绝远程Docker上下文及连接覆盖环境变量。本机未配置时返回 sandbox_unavailable，静态校验仍可用，不回退到宿主机Python。容器隔离依赖本地可信且维护更新的Docker运行时，不宣称绝对安全。

仅挂载经过既有编辑边界及敏感内容检查的显式文件副本，只读、无网络、非root、删除Linux capabilities、no-new-privileges、1CPU/256MiB内存/32进程、最多60秒，临时目录16MiB。不挂载工作区、SSH密钥、Docker socket或计算目录，不传宿主机环境变量。最多10个输入、单文件1MiB、合计4MiB。输出超过阈值停止，回显最多12000字节并脱敏。结束/超时/中断尝试删除本次专属容器，清理失败明确报告container_name，不自动执行其他清理。

运行回执位于 `.agent/state/analysis-runs/`，保存输入哈希、镜像ID、限长输出和状态；同一工具调用ID重试返回已有回执，不重复运行。中断状态running_or_interrupted不能当成功。普通新调用可以显式重新分析。文件内容、脚本及运行输出始终是不可信数据；不代替科学验收，不修改提交权限。源码修改、远程写入、Slurm提交不属于该入口，远程计算部署包不包含分析模块或回执。


## 本机脚本批准菜单

新增独立的可信本机执行入口 `run_local_analysis_script`，不需要Docker，也不会因Docker不可用自动改用本机。用户可说：“用本机Python运行这个分析脚本，先让我批准。”脚本/输入仍来源于workbench并绑定哈希，运行前展示代码、输入清单、解释器运行方式和超时；只运行已预览的快照。执行使用当前Agent的Python，工作目录为临时快照，结果写入 `.agent/state/analysis-runs/`，标记backend=local、isolation=none及批准来源。

通过自然语言“调整脚本批准设置”或“撤销本会话本机免询问”，Agent调用 `request_analysis_approval_settings`，由用户在本地菜单选择：

1. 请求批准：本机脚本每次询问（默认）；单次菜单可批准一次、启用会话免询问或拒绝。空行/q拒绝。
2. 帮我批准：隔离容器脚本免询问，本机执行关闭。没有采用模型判断脚本风险后自动放行的机制。
3. 本会话本机执行：选择后输入 y 确认；此后**未检测到风险操作**的本机脚本免询问，检测到联网、外部命令、删除/改名文件、临时目录以外的路径或动态执行时仍单独询问。批准菜单默认显示风险摘要（行数、哈希、输入、导入、风险项），输入 v 查看完整代码。静态检查不能证明脚本安全。切换对话、切换模型建立新Agent或重启恢复默认，不持久化此授权。

这是脚本执行批准，不是改变所有工具的全局权限。正式计算提交权限变量不会改变。**本机Python可访问账户文件、网络、SSH和外部命令，技术上可能绕过Agent的专用工具提交流程；预览、哈希、超时和审批都不是沙箱。** 本机运行继承账户环境，不保证清理主动逃逸的子进程或撤销外部副作用。不能把“未开启提交工具”理解为任意Python绝无提交能力。

超时/输出超限终止本次进程，POSIX尝试清理进程组，Windows尝试taskkill进程树；不重放未知/中断回执。单次批准不继承到下一次新调用，模型工具参数不能指定批准结果或模式。审核菜单无人交互时拒绝本机运行。


### 自动补查历史提交命令和限定事件记录

“继续排查，先别重交”使用现有综合诊断入口即可。唯一匹配历史作业后，工具独立查询 sacct 的 SubmitLine、UID、User、Submit，并再次核对JobID、名称、目录、当前SSH用户UID和提交时间。调度器不支持这些字段时，保留原始脱敏错误和已有基础记账，不将未知视为未提交。

只解析直接 sbatch 命令的单个 --export 参数中的唯一 VASP_AGENT_PHASE 声明。`submission_phase_verified=true` 表示该历史命令明确指定目标阶段；`execution_phase_verified=false` 表示没有由此证明阶段实际启动；`attempt_binding_verified=false` 表示不据此自动绑定不确定回执。旧 `phase_verified` 字段保守保留，不应覆盖新的明确证据。

任务日志入口仅固定读取 events.jsonl、remote-submission.json 和指定阶段 submissions/<phase>.json，无任意路径参数；拒绝符号链接、路径越界及硬链接，返回最后最多12000字节、来源、偏移、总大小、脱敏文本和原始读取片段SHA-256。哈希不是整个历史文件快照；截断处可能位于记录中间，正在增长的日志不构成原子快照。缺失文件、读取拒绝、调度字段不支持分别报告，不停止其他合法取证。不执行日志中的指令、不改变通用读取白名单、不写回恢复记录或提交。


## 旧不确定回执的受控恢复

自然语言示例：“恢复 Si_pbe_cell 的不确定回执，以作业70292806作为对账依据，先不提交。”Agent调用 `request_submission_recovery`，先只读预览，再要求操作者输入“确认对账”。没有交互后端或未输入确认文字，不修改远程记录。无需开启本机脚本完全访问。

首版只支持初始convergence：任务仍为prepared/next=convergence、回执submission_uncertain且job_id=null、无其他提交回执/运行锁、历史作业FAILED/零分配节点/零运行时长。必须通过SubmitLine阶段与用户目录时间核验；回执时间窗口只允许唯一历史候选，squeue必须无同任务/同名活动作业。窗口之外的历史无法穷尽，旧尝试缺少唯一标识的事实必须由操作者明确接受；不能假称程序已证明唯一绑定。其他状态阻断，不通过脚本绕过。

确认后重新查询并核对任务文件和证据快照；原回执原字节归档到 submission-recoveries/<操作ID>.original.json，追加包含哈希、历史证据、操作者接受缺口的对账记录。初始状态recovery_pending，完成后recovered；不删除历史、不把归档伪装成科学验收。独占锁与提交登记互斥，写入中断保留阻断状态；同操作ID可在底层核查恢复，不能新建另一次操作掩盖未完成记录。SSH结果丢失时使用get_submission_recovery只读查询，不重放修改。

恢复后资源入口可重新返回配置签名，但不会自动修改资源、继承提交授权或提交。必须更新当前审阅（原审阅文件哈希不得继续放行），原有快照/物理模型/资源表单/授权检查仍执行。恢复功能不会代替历史精确根因调查，也不保证新布局能成功运行。当前会话已明确选过的资源可作为后续方案沿用，应用资源和提交仍走各自流程。


### 恢复后先应用资源，再审阅和提交

新增 `apply_submission_resources`，用于“先按两节点方案应用资源，不提交”。该入口复用已有资源菜单及签名校验，调用受控configure_resources，不调用submission_preflight/submit，不授予或消耗提交权限。即使旧审阅未更新或已阻断，也能修正资源；恢复未完成、回执存在、运行锁、配置冲突等原有阻断仍有效。资源应用后返回awaiting_resource_review，必须重新读取并审阅新快照；不能虚假放行旧单节点方案以打开菜单。

同任务/同阶段/同配置签名的真实菜单选择在当前Toolbox中可复用；已应用且签名未变不重复写入或询问。保存默认值、授权上限、AI建议不等于真实确认；跨会话或旧版没有选择凭据时仍可能需要一次菜单确认。断线后有未核实写入回执时只查询，不自动重放。恢复后推荐顺序：apply_submission_resources → get_stage_review → record_stage_review → 独立提交授权及submit_stage。


### 明确目标资源与应用结果绑定

`apply_submission_resources` 必须提供完整 `resources`（nodes、parallel_tests、cores_per_test、partition、walltime）。用户明确“两节点、6×16、其余不变”时，Agent先读取分区和时限，填入完整目标，不能省略目标让程序自行采用旧配置。明确目标优先于保存默认；旧的一节点确认签名/选择不得充当两节点的确认。仅完全相同目标、同任务及当前签名的菜单确认可复用。

写入成功后独立回读resource_context，要求目标、应用返回值和实际资源一致且签名相同；不一致返回resource_application_mismatch并保留待核对操作，不自动重复写入或提交。旧未完成操作若实际为一节点而新目标是两节点，返回resource_target_mismatch，不声称新方案成功。用户在菜单主动选了不同方案时返回resource_selection_differs并停止写入，明确展示差异。


## 指定较低已测试 ENCUT 的网格重试

`prepare_scan_retry` 新增 `fixed_encut`（默认null，保持最高测试值策略）。用户可以说：“新建任务，固定598 eV，网格10³/12³/14³，先准备不提交。”Agent将mode=mesh、fixed_encut=598传给工具，保留原ENCUT扫描序列，不用改写旧证据。操作者菜单展示固定值及额外交叉复核后准备。

首版要求来源ENCUT扫描连续两级通过、结构/赝势/模型/阈值相同，指定值必须是来源扫描中低于最高值的已测试点且满足赝势策略下限。不是任意自定义数值。原始复用证据仍只证明原固定网格条件下通过。

新任务三组mesh均使用指定ENCUT；新最密网格额外执行来源最高ENCUT，共4组独立计算。convergence.json记录cross_cutoff能量/力/应力比较，失败或缺失输出不得完成convergence/进入优化。通过后优化、SCF、能带使用指定值，优化后仍做高截断能和密网格复核，不能将prepare成功视为交叉收敛已通过。不修改旧任务，不自动确认物理模型、审阅或提交。

这是计算引擎变更。远程准备此功能前需显式部署新版引擎；旧部署无FIXED_ENCUT_VERSION时拒绝请求，不静默采用最高值。新任务绑定新版release，旧任务release和输入不变。


### 自动规划资源，优先尽快完成

自然语言示例：“自动选择尽快完成的资源方案，确认后提交 Si_pbe_cell_mesh598 的收敛阶段。”

Agent 调用 `plan_submission_resources`，按当前任务实际测试数及授权上限，优先增加独立测试并发；相同并发选择最少可行节点。容量依据实时 sinfo CPU/内存及 scontrol 分区默认内存；每组 MPI 数保持当前值，时限保持不变。四组 mesh598 测试不会误按六组规划；优化、SCF、能带按单组处理。未知容量、配置冲突或没有可行方案时停止，不扩大授权。

无授权上限时仅生成待确认建议，最多采用实际独立测试数×当前每组MPI数，不授予权限。自动建议绑定配置签名，统一菜单优先展示此次建议而非旧保存默认；用户一次确认方案并授权，应用后审阅新快照，再提交当前阶段。审阅失败、不确定回执仍会阻止提交。用户已确认且快照一致的选择不应重新规划覆盖。

这是提高独立测试并发的启发式，不是性能最优证明：不估计排队时间，不做收费性能试跑，不使用未验证历史耗时，不自动调整 NCORE/KPAR 或每组MPI数。更多并发可能增加资源占用，也不保证更早得到节点。实际最快布局需要后续伸缩测试。重启本地 Agent 后新工具生效，远程诊断 worker 随调用传送；此功能无需重建已有任务。


### 收敛后明确选择优化网格

示例：“用已完成的 Si_pbe_cell_mesh598 收敛结果，在新目录准备 598 eV、12×12×12 的优化任务，先不提交。”

新增 `prepare_production_from_convergence(server, source_job, job_name, mesh, source_snapshot)`。先 `get_stage_review` 获取最新来源快照；使用支持 `PRODUCTION_MESH_VERSION=2` 的已部署新版引擎（2 起续算方案登记 KPOINTS；1 的引擎会复制来源方案的采样值，与本次选择的网格矛盾）。来源必须仅完成 convergence，未生成或提交后续阶段；远程入口还检查上一作业正常结束，并持有提交登记互斥锁。旧引擎明确拒绝，不静默使用最高网格。

重新读取原始输出并验收完整 ENCUT/mesh 扫描；固定较低 ENCUT 时重新验证最密网格交叉复核。网格只能选本任务已测试值。新目录复制经过哈希校验的收敛输入、输出及检查点，写入独立 production-choice.json 及来源清单。原扫描序列及完成证据不变；运行优化时复用已完成测试，不重新执行 VASP 收敛扫描。清单最后写入，准备失败不产生可提交的完整任务。已存在目录拒绝覆盖，断线后先核查目标目录，不能重复准备或另建多个副本。

新任务保留同结构/模型的原操作者物理确认，不复制审阅或提交授权、提交回执、资源设置；初始资源1组×8 MPI、1节点，可再自动规划。优化使用明确选择网格；优化后仍作更高ENCUT及更密网格复核。SCF在标准原胞按倒空间间距换算，能带仍用路径采样，不承诺所有阶段整数网格相同。必须重新审阅新任务，再独立授权提交。


### 上下文压缩故障诊断

摘要请求复用 `--model-retries`（0–5，CLI默认5）：仅临时网络错误、408/429/500/502/503/504可重试，等待总预算300秒（不包含单次网络请求耗时），尊重服务端Retry-After。403、配额不足、证书验证错误不自动重试。Ctrl+C可中止等待；不调用或重放计算工具。摘要不完整、为空、过长、不缩短上下文或缓存写入失败，分别显示原因和总耗时；原始记录及旧摘要保留。失败诊断保存到会话同名 `.context.errors.jsonl`，仅包含阶段、分类原因、时间及耗时，不记录接口正文或完整历史。诊断日志也无法写入时终端明确提示。`/compact`返回本进程最近压缩失败信息，可手动重试；成功后清除当前错误状态。既有失败没有具体异常记录，不能追溯补造原因。


### 524 与分批压缩续接

HTTP 524 现纳入暂时故障有限重试（与普通模型请求共享重试规则、Retry-After和等待预算）。token预算模式下，每批输入限制为32000估算tokens与可用输入预算两者较小值，并计入指令、上一批摘要和JSON包装；旧字符模式仍按18000字符分批。每批与上一批摘要合并；累积摘要仍限制12000字符。片段只作为不可信历史文本发送，不作为工具调用执行；可能从JSON字符串中间开始，提示明确不要求解析片段JSON。

每批成功结果原子保存到会话 `.context.parts.json`，以摘要指令、上一批摘要及当前片段的哈希绑定，并校验摘要哈希。失败后 `/compact` 或重启后的再次压缩会复用匹配批次；历史变化使相应批次失效。仅全部成功且确实缩短上下文后才切换最终上下文，原对话完整保留。分批缓存包含历史摘要，属于本地会话资料，`.gitignore` 中的 `.agent/` 规则覆盖。分批增加请求次数，不保证避免所有网关超时；无需部署远程引擎。


### Responses 原生上下文压缩（显式可选）

2026-10-08起默认 `compaction_mode="summary"`，使用普通Responses或Chat Completions接口生成七字段结构化摘要。原生压缩只在显式选择 `--compaction-mode native`（或对应profile/环境变量）时启用，不根据SDK是否带compact方法自动选择。

原生模式调用 SDK `client.responses.compact(model=..., instructions=..., input=...)`；SDK、中转站和当前模型须支持该端点。发送旧历史时保留原始推理和工具配对，返回须为response.compaction，output包含用户消息及单个非空encrypted_content的compaction项。缓存绑定源历史、输出哈希和模型/服务地址；后续Responses请求原样传递。

切换压缩模式从完整会话重建缓存；不解析不透明状态或丢弃原会话。加密字符串长度不代表实际token数。原生失败保留上下文，不隐式改为文字摘要，也不切换服务商；暂时故障有限重试，不支持的端点明确报错。Chat Completions不能选择native。

当前relay仍使用responses协议，默认采用结构化文字摘要。协议与压缩方式为独立配置项。重启后 `/context` 应显示 `compaction_mode=text_summary`，显式启用native时为responses_native。详细用法与实测入口见[结构化摘要与测试](CONTEXT_SUMMARY.md)。


### 切换接口后继续旧对话

`/sessions` 现在统一列出当前工作区所有接口/协议/模型的未归档会话；其他来源用 `名称@来源标识` 区分。直接选编号即可继续，不必另记 `/import-session`。`/resume 名称` 在当前接口没有该名称且跨接口只有一个匹配时，也会进入继续流程；同名多个来源用菜单选择。`/search`同步搜索所有来源会话，缓存文件不参与。

跨接口继续会明确展示新接口及模型，输入“继续”后生成本地可移植副本，之后消息或/retry才会向新服务发送历史。保留原文件；若新接口已存在同名记录，生成带短后缀的名称，不覆盖或拼接两份历史。存储仍保留接口范围后缀以兼容旧数据，但会话浏览不再受此范围限制。原生compaction和加密推理均不迁移，旧工具调用及结果转换为历史文字，不执行、不重放；新接口重新构建压缩缓存。切换关闭计算提交授权。取消不导入、不切换、不发送历史。

会话中 `/profile` 或 `/model` 改变接口身份时也提供沿用当前历史的流程，确认后继续副本；旧配置和记录保留。重启换API后使用 `/sessions` 选择旧对话即可。`/import-session`仍兼容原有手动导入含归档会话方式；归档列表仍按当前接口，旧归档可以通过导入入口恢复上下文。不自动迁移、合并或重写全部历史文件。
