# VASP Agent 最新进展与缓存排查交接

日期：2026-10-09，Asia/Hong_Kong（UTC+8）。第 1–9 节历史缓存证据截至当天 15:28:08；16:11:45 前完成的后续受控测试见第 10 节。
项目目录：`C:/Users/researcher/works/codex/codex_official/vasp-band-agent`。
环境：Windows、PowerShell / Git Bash、Python 3.12。本文用于让另一个对话接续现有工作。

## 1. 接手后首先要知道的结论

**真实 Agent 会话的缓存问题还没有解决。** 前缀稳定化已经生效，但 `test_agent` 会话连续 7 次响应报告缓存读取为 0。另一方面，相同中转站和模型上的合成测试，在约 14.6k 输入下能返回 99.12% 命中。下一步应查清这两类请求的差异，不能把合成测试成功宣称为正式会话已修复。

用户希望自然语言操作、当前回合弹出必要的选择菜单，不希望记更多斜杠命令或反复确认同一事项。最近代码功能已做独立 Git 提交；缓存与压缩工作还有另一对话留下的大量未提交改动，需要保留。

本文只是交接，不启动计算，不部署远程引擎，不改变权限、模型配置或用户会话。

## 2. 已完成并提交的近期功能

| 提交 | 功能 | 实际行为与边界 |
|---|---|---|
| `7607fb6` | 当前回合物理模型确认菜单 | 新工具 `request_physics_confirmation(server, job_name, proposed_goal)` 可让操作者确认建议目标、自填目标后确认或取消；确认后回传模型继续阶段审阅。已有有效确认不重复询问。无交互、取消、EOF/Ctrl+C 不写确认。不会授予提交权限。 |
| `158d418` | 按任务选择赝势变体 | `preview_scan_defaults` 和 `propose_scan_plan` 支持 `potential_variants`，如 `[{"element":"Cu","variant":"Cu_pv"}]`。选择保存进方案，仅影响新任务，不改库映射及旧任务。预览签名绑定选择与 POTCAR 哈希。记录来源、标题、ZVAL、ENMAX、哈希；显式变体检查 NBANDS 最低占据容量。 |
| `2271076` | 收敛判据解释修复 | `compare` 保留原判定和差值，新增 `effective_thresholds`、`checks`、`failed_checks`、`failure_reasons`、`comparison_policy`。修正 AI 将优化容差误当扫描差值阈值的问题，没有放宽标准。 |
| `7ef78f3` | 按倒空间间距建议网格 | 结构检查及默认参数预览给出三级网格建议，不把固定 8³ 当通用材料建议；用户明确网格优先，建议仍需收敛验证。 |
| `63bb228` | 显式外部 POSCAR 导入 | 用户明确提供的外部结构路径可导入授权工作区；不能因此扩大任意文件访问或提交权限。 |

物理模型菜单仅需重启本地 Agent，复用现有远程 `stage_context` / `confirm_physics` 接口。历史 `confirmed_by=operator_slash_command` 标记为兼容仍保留，新工具通过操作者菜单调用同一保存接口。

赝势变体准备与新版远程收敛说明依赖相应新版远程引擎；本对话没有进行部署。旧 release 不支持显式变体时会报错，不会静默退回默认 Cu。

相关指南：

- `docs/guides/PHYSICS_CONFIRMATION_MENU.md`
- `docs/guides/POTENTIAL_VARIANTS.md`
- `docs/guides/CONVERGENCE_DIAGNOSTICS.md`
- `docs/guides/KMESH_SUGGESTIONS.md`
- `docs/guides/STRUCTURE_IMPORT.md`

## 3. 计算问题背景：不要误判已解决

用户的 `La2CuO4_nm_pbe_cell_retry1`（历史 JobID `71123636`）使用非磁性、三维、电中性 PBE 对照模型；这是用户研究目标，不能擅自改成磁性、SOC、U 或杂化泛函。

配置为 energy_tol=0.001 eV/atom、force_tol=0.01 eV/Å、stress_tol=1 kbar。扫描的实际有效门槛为：能量差 1 meV/atom、力差 0.005 eV/Å、cell 模式应力差 0.5 kbar；ions 模式不检查应力差。每个扫描要求连续两组相邻比较均通过。结构优化残余力/应力检查使用完整容差，是不同概念。

用户提供的核验结果中：

- ENCUT 676→778、778→900 eV，应力差分别约 1.591009、1.291345 kbar，均失败。
- 网格 12³→14³ 应力差 0.263653 kbar，通过；14³→16³ 应力差 0.580631 kbar，失败。后一组力差 0.00493359 eV/Å 仍通过，不能列为失败项。
- 900 eV 已高，不应只根据通用失败提示就自动加到 1200 eV；原始应力张量、占据/电子迭代、FFT 网格、实际赝势等仍需取证，尚未确认应力差根因。
- 本对话尝试读取远程结果时，SSH 曾因本地私钥文件权限过宽而拒绝使用密钥。没有修改权限；这是当时检查的结果，不代表现在服务器仍不可连接。

用户随后提到新任务 `La2CuO4_Cupv_nm_pbe_cell`，意图 Cu→Cu_pv、La/O 保持旧任务一致。Cu_pv 不等于 _h 硬赝势；换赝势后不能沿用旧赝势的收敛证据。保持 La/O 一致应对照旧任务变体和哈希，不能只假定库默认映射未变。新任务当前准备/确认/提交状态未在本轮远程核验，不要依据聊天直接宣称已提交或已完成。

## 4. 缓存相关代码当前状态

接口实测配置：profile `relay`，`https://verysadai.com/v1`，模型 `gpt-6-astra`，协议 `responses`。密钥保存在现有配置体系中，不要输出密钥或复制 `.env` 正文。`resolve_config` 会受到环境变量覆盖，下一次实测应核对实际解析结果。

另一对话已在工作区加入以下优化和诊断；这些尚未提交到当前 HEAD：

- `Agent.context_instructions()` 固定系统指令，不再把每次变化的权限放进开头。
- `Toolbox.model_schemas()` 给模型固定工具目录；`schemas()` 和执行入口仍检查实际可用性与提交权限，展示工具不等于授权。
- `runtime_state()` / `sync_runtime_state()` 将权限状态作为 developer 消息追加，未变不重复追加；历史消息不能恢复授权。
- `models/prompt_cache.py` 记录输入、缓存读取/写入、指令/工具/设置指纹及历史共同前缀；`null` 表示未报告，0 表示明确报告零。
- Chat Completions 适配器现已保留缓存用量详情，不再丢失该字段。
- `/usage` 展示统计；会话旁 `.cache-metrics.jsonl` 保存逐次诊断。
- `examples/probe_cache.py` → `models/cache_probe.py` → `cache_probe_transport.py` 复用生产协议适配器，用合成输入进行 replay/append、流式等对照，不执行任何计算工具。
- `synthetic_only=true` 是合成输入，不是离线模拟。只有 `--dry-run` 不发模型请求。

相关文件：

```text
src/vasp_agent/chat/ai_agent.py
src/vasp_agent/chat/extra_commands.py
src/vasp_agent/models/model_adapters.py
src/vasp_agent/models/prompt_cache.py
src/vasp_agent/models/cache_probe.py
src/vasp_agent/models/cache_probe_transport.py
src/vasp_agent/models/api_client.py
examples/probe_cache.py
docs/guides/PROMPT_CACHE.md
```

现有 `--diagnostics`、`--cache-mode`、`--breakpoint` 参数是否被这个中转站实际支持，没有完成验证；不要因代码/SDK接受参数就宣称上游支持。官方文档抓取曾遇到 TLS EOF 和证书吊销服务不可达，不要关闭 TLS 验证来掩盖问题。

## 5. 已取得的缓存实测证据

### 5.1 短输入 replay：真实 HTTP 请求，缓存为零

用户原报告：`.cache/cache-probe/20261009T063701336077Z/report.json`，120 条合成参考，三次相同请求，每次输入 3,073 tokens，缓存读取和写入均为 0。

本对话另在香港时间 14:46 复测了两次，并通过 HTTP hook 记录实际地址、状态及请求 ID：

- 地址均为 `POST https://verysadai.com/v1/responses`，HTTP 200。
- 每次输入 3,073，缓存读取 0。
- 请求 ID：`c2ec5e3f-1468-4263-9b71-b9a0d8e3a2d0`、`a0e1627c-2cea-4d68-9b3f-d941e818381d`。
- 文件：`.cache/cache-probe/verify-20261009T064608Z/report.json`、`http-events.json`。

用户曾说下午测试没有账单，但截图的查询截止时间停在 11:09，无法包含下午请求。已经提示扩大时间范围并重新搜索。不能据此声称中转站伪造响应；也没有实际查看其后台来确认下午账单。

### 5.2 较大合成输入：两种模式均出现高命中

香港时间约 14:53，同一接口/模型/协议，600 条合成参考，replay，每组两次，输出上限 1024、超时45秒、SDK重试0。两组只切换流式设置。

| 模式 | 轮次 | 输入 tokens | 缓存读取 | 命中率 | 耗时 |
|---|---:|---:|---:|---:|---:|
| 非流式 | 1 | 14,593 | 14,464 | 99.12% | 4.83s |
| 非流式 | 2 | 14,593 | 14,464 | 99.12% | 3.31s |
| 流式 | 1 | 14,593 | 0 | 0% | 8.95s |
| 流式 | 2 | 14,593 | 14,464 | 99.12% | 6.77s |

证据目录：`.cache/cache-probe/compare-20261009T065327Z/`，包含 `RESULTS.md` 和两个模式各自的 `report.json`、`http-events.json`。

能证明：在这些合成请求下接口能返回高缓存读取，非流式也可以命中。不能证明：真实 Agent 会话已经修复、确切的最小缓存长度、保留期限或稳定上游路由。非流式首轮即命中，可能复用了先前请求，不能把它当作冷缓存写入测试。

### 5.3 真实新会话 test_agent：7 次仍全部零命中

证据：`.ai-session-test_agent-b9dc68ab8038.cache-metrics.jsonl`。
会话：`.ai-session-test_agent-b9dc68ab8038.json`。不要不加筛选地输出完整会话。

| 香港时间 | 输入 tokens | 缓存读取 |
|---|---:|---:|
| 15:00:47 | 9,313 | 0 |
| 15:17:29 | 9,429 | 0 |
| 15:19:55 | 10,720 | 0 |
| 15:21:40 | 10,823 | 0 |
| 15:22:32 | 10,868 | 0 |
| 15:27:49 | 11,252 | 0 |
| 15:28:08 | 11,515 | 0 |

合计 73,920 输入 tokens，接口报告缓存读取率 0%。7 条都成功，无用量不一致；指令、工具和设置各只有一个指纹。第2次起都是 `append_only`、`changed_components=[]`，返回模型和 service tier 标识未变化；这些标识相同不证明真实上游账号/缓存节点相同。

最后三次用户都问了“agent如何节省token，有什么方法”，已对照会话中 user 消息索引9/11/13确认。三次合计 33,635 输入 tokens，缓存均0。最后两次只隔约19秒，不能仅用间隔过长解释。

重复同一句不是整份请求重放，因为旧回答也追加进历史；已有共同前缀仍稳定。不要再让用户反复问同一句来替代受控实验。

## 6. 当前诊断边界与接手后的优先步骤

1. 先重新读取最新会话缓存记录，确认是否在本文7条之后有新数据，再作结论。
2. 查看当前工作区代码和既有测试；不要重新实现另一对话已做的固定指令/固定工具目录/统计功能。
3. 建立真实 Agent 请求与合成 probe 的脱敏对照。尤其分清：约9k–11.5k真实输入中工具/schema占比、合成输入约14.6k中的文本占比，以及消息结构差异。相同总 tokens 不代表同样可缓存前缀。
4. 使用无私密信息的合成会话，把输入规模控制在可比范围，依次只改变一个因素：单个虚拟工具/完整工具目录、少量长消息/真实会话角色结构、精确replay/逐轮append。不要同时改协议、流式、推理强度和缓存参数后强行归因。
5. 如仍无法解释，再单独核对请求缓存标识、保留设置、会话请求头和中转站路由。不能凭印象添加未核实的参数，也不能把 Codex 的账户或会话头直接复制到 Agent。
6. 每组只做少量有界请求（例如2–3次），超时和输出限额明确，出错停止；保存请求指纹、必要的 HTTP 请求 ID、原始返回用量的安全子集。不要保存认证头、真实私密对话正文或密钥。
7. 以同一时区、正确后台日期范围对账；有需要时整理请求 ID 给用户或管理员查询，未经用户授权不要主动对外发消息。
8. 找到证据支持的原因后再修改生产实现；回归权限检查、工具调用、断线恢复和缓存统计。验收必须包含真实 Agent 场景，不能只凭 probe 的高命中率结束。

还没有证实的假设：输入门槛、工具目录处理、请求缓存标识、缓存保留时间、上游轮换、推理设置或服务商统计差异。不要将其中任一项写成根因。

## 7. 测试与部署记录

| 时间节点 | 已实际运行结果 | 使用限制 |
|---|---|---|
| 收敛解释修复后 | 针对性66通过；当时全量695通过、1跳过 | 历史回归结果 |
| 赝势变体功能后 | 针对性48通过；当时全量703通过、1跳过 | 合成赝势与模拟远程接口，无真实POTCAR泄露 |
| 模型确认菜单后 | 针对性26通过；当时全量711通过、1跳过 | 这是本对话最近一次全量回归；之后别的对话仍有未提交修改 |
| 缓存对照时 | `test_prompt_cache.py`、`test_cache_probe.py`、`test_cache_probe_transport.py` 合计21通过 | 不是当前所有改动的全量回归，不应说最新全量711仍已验证 |

上述全量运行存在 spglib 弃用警告。模型缓存实测只调用模型接口，不运行 VASP、Slurm 或 SSH 计算工具。当前交接文档没有另做模型实测和回归。

## 8. Git 与工作区保护

交接开始时分支为 main，HEAD 为 `7607fb6`。历史功能提交已列在前面。随后若本交接文档单独提交，HEAD 会增加一个仅文档提交；以 `git log` 为准。

不要 reset、stash 全部改动或直接 `git add .`。`ai_agent.py` 混有其他对话的缓存/压缩工作。之前功能提交采用精确选择自己的 diff hunks，未将其他工作一起提交。`.cache/` 和会话证据仅在本机，通常被忽略，不应随代码上传；本交接表格保留了关键统计。

交接开始时工作区状态如下（路径相对项目根目录）：

```text
 M .env.example
 M .gitignore
 M docs/README.md
 M docs/guides/AI_USAGE.md
 M models.toml
 M src/vasp_agent/chat/ai_agent.py
 M src/vasp_agent/chat/extra_commands.py
 M src/vasp_agent/models/context_compaction.py
 M src/vasp_agent/models/model_adapters.py
 M src/vasp_agent/models/model_config.py
 M src/vasp_agent/models/request_errors.py
 M tests/test_ai_agent.py
 M tests/test_context_budget.py
 M tests/test_context_compaction.py
 M tests/test_model_reconnect.py
 M tests/test_model_request_errors.py
 M tests/test_native_compaction.py
 M tests/test_project_layout.py
 M tests/test_terminal_ui.py
 M tests/test_timeout_recovery.py
?? docs/guides/COMPACTION_PROBE.md
?? docs/guides/CONTEXT_SUMMARY.md
?? docs/guides/CONTEXT_SUMMARY_TEST_20261008.md
?? docs/guides/MODEL_CONNECTION_DIAGNOSIS_20261008.md
?? docs/guides/PROMPT_CACHE.md
?? docs/guides/TLS_COMPARISON_20261008.md
?? examples/probe_cache.py
?? examples/probe_compaction.py
?? examples/probe_summary.py
?? src/vasp_agent/models/api_client.py
?? src/vasp_agent/models/cache_probe.py
?? src/vasp_agent/models/cache_probe_transport.py
?? src/vasp_agent/models/compaction_probe.py
?? src/vasp_agent/models/prompt_cache.py
?? src/vasp_agent/models/summary_probe.py
?? structures/
?? tests/test_api_client.py
?? tests/test_cache_probe.py
?? tests/test_cache_probe_transport.py
?? tests/test_compaction_probe.py
?? tests/test_prompt_cache.py
?? tests/test_summary_management.py
?? tests/test_summary_probe.py
```

## 9. 可直接发给下一对话的接续说明

> 请阅读 docs/handoffs/HANDOFF_PROGRESS_AND_CACHE_20261009.md，继续排查 VASP Agent 真实会话的缓存零命中。当前固定系统提示/工具目录已生效，test_agent 的7次请求仍零缓存；约14.6k合成probe却可99.12%命中。先检查更新日志和现有未提交代码，再做可比规模、单变量的请求结构对照，找到原因后修复。保留其他对话的改动，不提交计算，不把合成测试结果当正式会话验收。

## 10. 后续受控测试：16:01–16:11

详细证据：[缓存请求结构对照报告](../guides/CACHE_STRUCTURE_TEST_20261009.md)。21 次有界请求尝试中 20 次 HTTP 200，1 次读取超时；仅使用合成数据，工具执行次数为 0，未访问集群、未提交计算、未修改生产代码。

正式 Agent 请求路径能返回缓存：小会话首轮 56.22%、大合成会话首轮 37.73%，但次轮都为 0。最强对照为普通 SDK create 精确 replay，**相同 HTTP 正文哈希、两次同为 9,334 输入 tokens，第一次缓存 9,216（98.74%），约 33.4 秒后第二次为 0**；HTTP JSON 与本地统计一致。

原先成功的 14,593-token probe 本轮两次也都为零；完整工具目录、单/多角色、replay/append、普通/raw 调用没有找到能稳定改善命中的单项。隔离 Agent 的历史未改写，无 reasoning 项，四次 small/large 正文已离线重建并与现场哈希匹配。不能再将原 probe 的高命中视为稳定可复现的对照，也不能宣布正式会话已修复。

下一步优先核对服务端缓存/上游路由/用量转发，尚未确定根因。同正文一命中一归零的请求 ID 为 `d20102a8-6f3c-4fda-bfd1-633aae917dcd`、`2fea09d4-ed25-43d2-8758-15321b11ba1e`；具体时间与安全用量见报告。未对外联系服务商。测试证据位于 `.cache/cache-structure/`，缓存相关离线测试 **22 passed**，不是全量回归。

## 11. GitHub 调查与后续编码/断点实测

用户暂不希望联系中转站管理员，因此先做客户端候选因素对照。调查见 [GitHub 缓存调查](../guides/GITHUB_CACHE_INVESTIGATION_20261009.md)，实测见 [指令位置与编码报告](../guides/CACHE_ENCODING_TEST_20261009.md)。本轮仅合成数据，工具执行次数 0；生产代码和默认模型配置未改。

第一批 3 次 HTTP 200 均为零缓存，第四次读取超时后停止；随后复用同一合成指令/历史/工具及正文，以统一 90 秒超时补测。补测香港时间 17:37–17:41，ABC 各 4 次，D 初次 HTTP 400 后停止。合计 17 次尝试：15 成功、1 超时、1 参数拒绝；缓存相关离线回归本轮 **21 passed**，不是全量回归。

| 方案 | 每次输入 | 四轮缓存 tokens | 加权命中率 | 未缓存总输入 |
|---|---:|---|---:|---:|
| A：顶层 instructions | 9,329 | 0 / 0 / 8,960 / 5,248 | 38.07% | 23,108 |
| B：首条 developer，string | 13,445 | 0 / 0 / 5,248 / 5,248 | 19.52% | 43,284 |
| C：首条 developer，input_text | 13,445 | 0 / 5,248 / 13,312 / 0 | 34.51% | 35,220 |

每组 4 次实际 HTTP 正文哈希一致，原始 usage 与 SDK 计数一致。C 的 99.01%→0 相隔约 74.43 秒，再次表明 typed 编码不能稳定解决问题。相同文字移入 developer 后增加 4,116 输入 tokens，离线没有重复拼接，服务侧计量差异未确定；不能只因 typed 某轮高命中就改变生产默认。

D 只添加 developer input_text 内的 `prompt_cache_breakpoint={"mode":"explicit"}`，没有添加 key 或强制 explicit mode。当前接口返回 `invalid_request_error / invalid_parameter / param=prompt_cache_breakpoint`，安全分类确认提及不支持；未继续断点组或自动删参数重发。断点保持关闭。

证据：`.cache/cache-encoding/20261009T093104592897Z/` 和 `.cache/cache-encoding/20261009T093740663948Z/`。本机隔离脚本 `.cache/cache-encoding/run.py` 依赖此前 `.cache/cache-structure/run.py`；两者均不随 Git 发布。下一步可分别测固定 key、流式或推理强度，或先减少重复输入；仍需单变量对照，不需要未经授权联系服务商。根因尚未确认。

## 12. 开源 Agent 请求构建源码研究

用户进一步要求查看真实开源实现，而不只是问题讨论。报告：[开源 Agent 缓存架构源码研究](../guides/OPEN_SOURCE_AGENT_CACHE_ARCHITECTURE_20261009.md)。固定版本核验了 Codex、OpenCode、Cline、Pi、Agents Python、LangChain 的实际函数和序列化路径。此轮没有模型 API 请求、SSH 或生产代码修改。

Codex、OpenCode、Pi 都有稳定 session-derived key；Agents 在 run/session 范围生成和恢复 key，但自动 key 限官方 OpenAI client。Codex 另有 ChatGPT 会话亲和头、同轮服务返回状态和经前缀验证的 WebSocket 增量续接。OpenCode/Pi 按 provider 能力处理缓存参数；Cline 当前 OpenAI builders 没有自动 session key，Anthropic metadata/标记机制不能照搬为 Responses 参数。LangChain 本地答案缓存命中直接返回旧 Generation，不能当作 API cached_tokens 功能。

当前本地已具备稳定指令/工具目录、追加历史和 raw usage 对照，不需要再次实现。真实差异是正式 adapter 没有 key/retention/options 可选透传，生产观测没有完整覆盖最终 HTTP body 指纹和原始字段存在性。下一候选实验是保留现有请求，只对比无 key 与固定 key，并分开 replay/append；研究本轮尚未执行该实验。

不能把源码存在 key 当作已确定根因。官方新模型 key 并非路由优化必需项；第三方 relay 的接受/转发与缓存行为仍未知。同正文 99%→0 和 breakpoint 400 的现有证据继续有效。具体 pinned commit、源码函数、永久链接、已实现/缺口表和可借鉴范围见报告。

## 13. 固定 prompt_cache_key 的受控测试

用户授权进行下一步，完成无 key 与固定 key 的 replay/append 对照。报告：[稳定缓存 key 实测](../guides/CACHE_KEY_TEST_20261009.md)。接口仍为 relay / https://verysadai.com/v1 / gpt-6-astra / Responses，保留顶层 instructions、54 个固定工具、固定合成权限和既有选项；唯一请求变量为是否添加 prompt_cache_key。仅合成数据，工具调用 0、没有 SSH/部署/提交，生产代码与默认配置未因此改动，也未联系服务商。

香港时间 19:42:57–21:24:44，包括较长中断和四次恢复。预定 16 个有效轮次全部完成，共 20 次尝试，达到原定上限：16 个 HTTP 200，3 个 TLS EOF、1 个读取超时；四次失败用量未知，不计为缓存零。恢复保留原 key、fixture、正文 hash，仅补失败/未执行轮次，成功轮次没有重复。全部有效响应的原始 usage 与 SDK 一致。

| 模式 | 无 key 四轮缓存 tokens | 固定 key 四轮缓存 tokens | 无 key 加权命中率 | 固定 key 加权命中率 |
|---|---|---|---:|---:|
| replay，输入每轮 9,335 | 0 / 0 / 5,248 / 8,960 | 5,248 / 9,216 / 9,216 / 9,216 | 38.05% | 88.10% |
| append，输入 9,335→9,779 | 8,960 / 5,248 / 0 / 5,248 | 0 / 0 / 9,344 / 9,216 | 50.89% | 48.55% |

八个完整配对的最终正文移除 key 后 hash 相同、输入 tokens 相同；key 各自恒定，replay 全正文 hash 恒定。replay 第 2、3 对跨恢复进程，间隔分别 207.321、491.125 秒；只用同进程第 1、4 对，无 key 47.99%、固定 key 77.47%。该子集两对均无 key 先、key 后，失去先后平衡，仍有暖缓存顺序偏差。append 四对均各自同进程，但第 2→3 轮跨约 98 分钟中断，不能称连续四轮。共享前缀暖缓存、重连路由、时间变化仍是限制。

固定 key 的 replay 后三轮均 98.73%，是值得复测的候选；append 总体没有改善，因此不能默认启用 key 或宣布真实会话已修复。下一步先稳定代理/TLS 线路，再在连续短时间同进程内单独复测 append，保持 key 先后交替，随后用接近真实长会话的合成规模验证；流式/亲和头/TTL 需另做单变量，不能一起改变。本轮 20 次预算已用完，没有追加调用。

附带只读连接核查确认 SDK 通过 Windows 系统代理使用 `http://127.0.0.1:7897`。默认 curl 与 Python 不保证同线路；显式代理 curl HEAD 返回 404、TLS 校验成功，系统代理 Python HEAD 返回 403、TLS 已建立。不能据此证明代理是 EOF 根因或模型 POST 正常。实际 SDK connect/read 均 90 秒，约 5 秒的 SSL EOF 不是证据说明 connect timeout 只有 5 秒。TLS 校验始终开启，没有修改生产代理/连接池。

证据 `.cache/cache-key/20261009T114257101105Z/` 内有 matrix.json、http-events.jsonl、analysis.json、tls-observations.json；隔离 run.py/analyze.py 在 `.cache/cache-key/`，run.py 依赖此前 encoding/structure 脚本。缓存相关离线回归本轮 **21 passed**，不是全量回归。完整统计、配对间隔与限制见报告。

## 14. 连接诊断、连续追加与可选网络模式

接续用户“进行下一步”。报告：[线路与连续追加实测](../guides/CACHE_NETWORK_AND_APPEND_TEST_20261009.md)。本轮无认证 GET 合计 26 次：同代理 curl/Python8次全401、正式 SDK2次全401、SDK shared/fresh8次7个401与1次SSL EOF、同严格CAcontext代理/无应用代理各4次为3/4与4/4个401。两次握手失败都在代理 CONNECT 后 start_tls；一对实际复用成功。GET通过不表示模型POST或缓存通过。

本机端口7897为verge-mihomo。安全过滤旧Clash目标日志，前轮三次SDK SSL EOF结束时间与 `connect error / dial tcp / i/o timeout` 日志相差约13–14毫秒，支持代理出站故障关联；未确定具体节点/出口/远端责任，更不能用连接失败解释成功HTTP200的缓存零。

原线路单进程40,000 ASCII合成notes、54工具、权限初始化一次、pause2s append实验预设8请求，但第2次 RemoteProtocolError 后停：仅1成功（input21586/cache8960/output8、41.51%）和1失败，0完整pair。另做最多4请求的隔离direct确认，实际通过正式 create_client direct 分支，第1次122.609s ReadTimeout后停，0有效响应。此turn收费模型共3attempts/1success/2fail，失败用量未知，无恢复或补轮次。四次POST传输trace脚本只离线验证，没发真实请求。没有真实会话/工具执行/SSH/VASP/Slurm或管理员联系。

已实现正式可选 `network_mode=auto|direct`（默认auto）：CLI `--network-mode`、env/.env `VASP_AGENT_NETWORK_MODE`、profile字段，CLI>env>profile>auto；public显示但不打印key。direct仅禁模型client应用代理，保留SDK实际HTTP栈的CA环境、CERT_REQUIRED/hostname校验，不修改系统/SSH，无自动fallback。/profile清除上次CLI网络覆盖，/model保留。非法值/不安全context立即失败，构造失败close。两profile仍auto；--check-config direct离线通过；24新网络modecase及相关回归合计138 passed（不是全量）。变更仅network的api_client/model_config/ai_agent参数与profile小hunks、新test_network_mode，以及示例/文档；原有其他对话修改保留，未stage/commit。

已询问使用者是否有另一个可用Clash节点/端口，等待可用替代线路。不建议再反复调用当前失败模型路径，也不能把direct当已验证修复。可选功能已完成，连续缓存验收仍未完成。脚本与证据目录详见报告；下一对话先读取用户新线路信息，有限无认证确认后再明确预算做连续模型对照，不从失败summary的0累计量推断缓存为0。

## 15. 2026-10-10 连续验证：输入计量差异触发停止

用户要求“进行下一步验证”。报告：[连续缓存验证，2026-10-10](../guides/CACHE_VALIDATION_20261010.md)。沿用 relay / verysadai.com / gpt-6-astra / Responses / auto 和现有系统代理，未改节点或真实配置。无认证 GET 共5次：首条SDK约5秒SSLEOF后停；随后同代理curl两次401、新批SDK两次401。短时恢复不等于线路永久修复，401不证明模型POST或缓存。

一个进程的合成40k ASCII/54工具/初始化权限一次的连续append计划最多8个HTTP、四对A0/A1交替、无重试/恢复/fallback。实际香港时间09:40:36–09:40:46仅发两条首轮HTTP200，cache读写均0，output各8；无key input21084，固定key input21112，相差28。去key后最终body hash相同、history相同、model/tier hash相同，raw usage与SDK一致；第二条input等值控制失败，立即停止，没有第二轮append或补剩余请求。

须区分**2个有效响应、1个候选pair、0个严格可比较pair**。旧matrix的valid只描述响应核验、paired只列候选；不能引用summary的paired_rounds=1宣布配对通过。原matrix未修改，另存analysis.json解释。TLS/目标POST完整收发成功，本地第二次复用迹象成立，不证明上游同副本。28-token差异不能证明key被插入提示词或具体路由/tokenizer根因；接受key不证明用于缓存。本轮没有生产代码改动/默认启用key、真实会话上传/SSH/VASP/Slurm或管理员联系。

证据在 `.cache/cache-validation-20261010/20261010T014035171792Z/`：matrix.json、http-events.jsonl、executed-run.py（当时脚本快照）、analysis.json。请求ID分别e0c8ff8e-f2cf-4779-8027-24f331802b64和b224041a-7e3f-491f-9b80-a0dd0996a087。无认证证据、脚本依赖和时间见报告。本批已停止，不得恢复拼接。下一步优先单独核对同正文重放的input计量稳定性，再设计key对照；不在看到结果后放宽本批规则。此前138回归属10月9日历史结果，本轮没有重跑全量项目测试。

随后仅修正隔离测试脚本的统计/输出与trace：HTTP成功、响应有效、候选pair、全部控制通过pair分开；零可比较pair时paired用量/率为null；缺usage不记0；CONNECT发送不误认POST；收headers后读body失败保留status、不标no_http_response。最终纯SDKMock主流程8/8、4可比較pair，4故障场景共5Mock请求通过（输入量不等两次停止且0pair、200缺usage、CONNECT后TLS失败、200bodyread失败）。证据在 `.cache/cache-validation-20261010/20261010T015006509228Z/`。没有额外真实模型请求、生产代码改动或重写旧matrix。

## 16. 2026-10-10 无 key 精确 replay 的输入计量验证

用户授权下一步，新增隔离脚本 `.cache/cache-accounting-20261010/run.py`，先SDKMock验证旧A0正文SHA/history/instructions/tools精确重建、四次body不变、input变化仍测满四次、缺usage第二次停止保持null、额外/重复/redirect/正文变更阻断，共6Mock HTTP。真实前置GET两次401/TLS1.3，硬限2、无认证/真实key/重试/redirect；证据accounting-preflight-20261010T020322041448Z.json。模型仍relay/verysadai/gpt-6-astra/Responses/auto、旧40kASCII/54tools，client同进程、无key、不append、90s阶段timeout/1024输出/pause2/无retry-resume-fallback，工具阻断。

真实香港时间10:06:04–10:06:25，四次HTTP200完整、raw/SDK一致，所有controls通过、guard恰好4/blocked0。input为 **21084/21112/21084/21084**，cached为 **0/20992/0/0**，命中率 **0/99.43%/0/0**。Σinput84364/Σcached20992/未缓存63372/output32/total84396；weighted24.8827%，input+output=total各条一致。四次body86377 bytes、SHA dc78ccadd43eef7d4fee3ca0e3c61fb819a7b6aa80188218ec0ffc5081098d9b同旧A0；请求开始间隔5.695892/5.809953/4.646833s。只有第一次CONNECT/TLS，后三次同本机stream/TLS对象hash且无新握手，支持本机连接复用，不证明上游同副本。没有完整headers逐字节证据。

本批 **completed=true、input_tokens_stable=false**；计量变化预先作为研究目标，不是stop条件。证明无key同body也可有+28，因此旧+28不能独归key；同body/本地复用仍高缓存→0，不能由本批正文变化/新TLS解释。尚未证明具体上游路由/计量/tokenizer根因、缓存已修复或key无效。新报告：[精确重放计量与缓存实测](../guides/CACHE_ACCOUNTING_REPLAY_20261010.md)。真实证据 `.cache/cache-accounting-20261010/20261010T020602829984Z/` 内matrix/http-events/脚本与Evidence快照/analysis；离线目录020444162558Z。旧批不改，不混入新统计，无真实会话/SSH/VASP/Slurm/生产改动/管理员联系，无全量回归。

本批4模型预算用完。下一候选是单独固定key精确replay四次，保留其他选项、不自动默认启用；新规则须事前区分输入计量观测与响应控制，不能看到结果后修改旧规则或把单批前后差异当因果。最终key效果仍需同时间段两臂交替、足够重复和正式Agent验收。

## 17. 2026-10-10 固定 key replay 准备完成，预检失败未开始模型

用户授权下一步。新隔离 `.cache/cache-key-replay-20261010/run.py` 精确重建旧A1完整bodySHA48c2cb0ac66884409ae33824e94af76b8c3a69849ae083519d80d359823107f8/86438bytes、原合成keySHA ef6374f7a3652c0623719ef83ff996aa6c901c5ec8d84c0f365ee7e2c8ab6b95；去key同旧A0，提示nonce/54tools/40kASCII/选项不变。guard严格4实际HTTP、正文/key变更/重复/redirect5th出站前阻断；input变化是观测，缺usage/marker/transport等控制失败则停止；auto/90s阶段timeout/1024输出/pause2/无retry-resume-fallback，tools阻断。纯SDKMock主四次+缺usage两次共6模拟请求通过，缺失字段null，代码及Evidence快照在离线目录 `.cache/cache-key-replay-20261010/20261010T021526197865Z/`。

**本轮真实模型0次，固定key实际效果未测试。** 正式client底层无authGET预检第一条HK10:15:23起，约5s ConnectError→ConnectError→SSLEOFError，无HTTP，第一失败即停；没有第二GET或live补跑。保持relay/verysadai/gpt-6-astra/Responses/auto和系统代理，证书校验开启，预检没有真实key；当前只证明传输未通过，不能归因key或具体上游故障。证据 `.cache/tls-diagnostics/accounting-preflight-20261010T021523220724Z.json`；`.cache/cache-key-replay-20261010/preflight-blocked-20261010T021523220724Z/status.json` 显式not_started/model0/cache指标null，与prepared-run.py保留准备状态。此前10月10日模型两批仍共6次，Mock零缓存不能纳入实测。

报告：[固定key重放准备与连接阻断](../guides/CACHE_FIXED_KEY_REPLAY_20261010.md)。连接恢复后先新的有界无认证确认再新批四次live；本批未默认启用key，未改生产/真实env/代理，未运行SSH/VASP/Slurm、上传真实会话或联系管理员，未全量回归。跨时段有key/无key的结果只能描述比较，最终还需同时间两臂重复及Agent真实场景。不要用本轮离线completed=true宣称正式缓存已修复。

## 18. 2026-10-10 固定 key 真实重放：首条 429 后停止

用户指出同 VPN 下 Codex 缓存正常并授权下一步。保持原 relay/verysadai/gpt-6-astra/Responses/auto 及正式 create_client，新的无认证预检两次 HTTP401/TLS1.3/完整响应通过，HK10:26:34.741–10:26:38.112，证据 `.cache/tls-diagnostics/accounting-preflight-20261010T022634741186Z.json`；不能把预检当模型或缓存通过。

旧固定key脚本与直接Evidence依赖SHA仍同offline，未重做Mock。一个进程计划最多4POST，实际仅第一条，HK批次10:27:00.913–10:27:32.964，POST起10:27:01.602，32.047s后 RateLimitError→HTTPStatusError，完整HTTP429。安全错误为 **rate_limit_error / gateway_concurrency_limit**；CONNECT/TLS1.3成功、目标POST正文完整发送、429完整读取，因此这次不是TLS超时。没有usage，input/cached/write/weighted全部null，0有效响应；未补其余三条，SDK/model retry0/noresume/nofallback，工具执行0。请求守卫logical1/HTTP1/blocked0，事件日志仅1行。

实际bodySHA48c2cb0ac66884409ae33824e94af76b8c3a69849ae083519d80d359823107f8/86438bytes、keySHA ef6374f7a3652c0623719ef83ff996aa6c901c5ec8d84c0f365ee7e2c8ab6b95、去key同旧A0均通过，history/instructions/54tools等不变。matrix的model/tier_constant在无有效response时是空集合判断，不能说已核验返回模型。并发错误不能确定本地并发过高、其他请求/网关通道占用、额度或具体上游责任；不能证明key被上游采用或因key拒绝。固定key效果仍未取得有效测量，不能将null记0或与无key的24.8827%作效果比较。

报告：[固定key真实重放与网关限制](../guides/CACHE_FIXED_KEY_REPLAY_RESULT_20261010.md)。证据 `.cache/cache-key-replay-20261010/20261010T022659556606Z/` 内matrix/http-events/执行脚本及Evidence快照。保留上一预检失败批和所有旧证据，当前批不恢复拼接；后续接口可用时新批完整重放，取得有效结果后才继续单变量比较Codex的请求选项（如流式）。本轮无生产改动/默认key/真实会话/SSH/VASP/Slurm/管理员联系，无全量回归。10月10日截至此批共7模型请求尝试，其中6次HTTP200、1次429；本批实测1而不是计划4。

## 19. 2026-10-10 DeepSeek 官方配置与两种缓存验收

用户提供 DeepSeek 密钥并授权配置与测试。已仅将密钥写入 Git 忽略的本机 `.env`，增加 `DEEPSEEK_API_KEY`，默认 `VASP_AGENT_PROFILE=deepseek`；`models.toml` 默认也改为 deepseek，新增 `https://api.deepseek.com / deepseek-flash / responses / auto / summary` 配置，保守本地上下文预算 240000，原 relay/official 保留。`.env.example` 只增加空变量，公开配置/文档/报告不含密钥。重启用 `python ai_agent.py` 或显式 `--profile deepseek`；已有进程不会自动重载。报告：[DeepSeek 配置与缓存验收](../guides/DEEPSEEK_SETUP_AND_CACHE_20261010.md)。

离线配置及 probe dry-run 通过。香港时间10:54:03–10:54:05，硬限一次认证 GET `/models` 返回200，实际模型列表为 deepseek-flash、deepseek-v4-pro；证据 `.cache/deepseek-validation-20261010/models-20261010T025405602702Z.json`。沿用正式 create_client / ResponsesAdapter / ProbeTransport，用600合成记录、一个无执行工具schema；非流式、effort none、输出1024、阶段超时90s、pause3、SDK/model retry0、无resume/fallback；各批硬限4实际POST，缺usage、输出/HTTP/守卫失败即停，不补轮。没有真实对话、结构、集群信息或凭据上传，没有SSH/VASP/Slurm或管理员联系。

replay HK11:01:07.658–11:01:21.065，4HTTP200有效。input每次14732；cached 0/14592/14592/14592，对应0/99.05/99.05/99.05%。Σinput58928/cache43776/未缓存15152，weighted74.2873%，后三轮99.0497%；output20/total58948。四次正文73397bytes、SHA287325d943549f4966fae37bcb68fbcb7570683d5093e0442d9180b0de417128恒定，SDK/raw一致，guard4/blocked0。证据目录 `.cache/deepseek-validation-20261010/20261010T030106175668Z/`，脚本SHA649b9d441449ad40db90fcbaf37de480bd6dd174729dccb6d7ba0750aebfa957，最新离线025809455575Z。

append HK11:25:49.083–11:26:01.489，4HTTP200有效，input14732/14772/14812/14852，cached各14592，率99.05/98.78/98.51/98.25%。Σinput59168/cache58368/未缓存800，weighted98.6479%，output20/total59188。首条同replay已经命中，明确是暖缓存后连续追加，不是冷启动对照。实际history3/6/9/12，旧实际出站input与验证SDKoutput原样保留后追加；静态instructions/tools/options恒定、模型匹配、SDK/raw一致、guard4/blocked0。完整body及输入数随追加变化是预期，不是控制失败；未报告tier不能视作真实等级核验。证据 `.cache/deepseek-validation-20261010/append-20261010T032547725602Z/`，脚本SHAc6ada9543b4fdc3473df9de7fe0c9cf285ec352af7df2889c6680877dad03ce3，最新离线append-20261010T032256261188Z。

本次共1模型GET+8模型POST全部成功，两份脚本均先Mock主4与缺usage2共6次，模拟数据不混入真实结果；原始缓存写入缺失保持null。两批均仅首条有握手。本批证明官方服务的这两种非思考合成场景持续命中，不能据此归因或宣布旧relay修复，更未验收正式VASP全工具/真实长对话/思考模式/窗口容量。生产adapter未改，正常思考默认未改；没有全量pytest。下一步可在deepseek新对话做有界正式工具场景，必须另定真实调用预算；本批8次预算已用完，不追加付费测试。

## 20. La₂CuO₄ / Cu_pv 的实际 Agent 工具与缓存测试

用户随后要求换真实La₂CuO₄ POSCAR测试，并确认Cu变体为Cu_pv，本次仍为工具/准备与缓存诊断，不提交VASP。使用已有 `structures/9ca4e496dd1d526c-e4d88a20.POSCAR`，实际7原子、顺序Cu/La/O、数量1/2/4，非正交晶胞。结构工具成功返回几何候选7×7×4、9×9×5、11×11×6，尚未收敛。没有继承Si非磁模型、NBANDS16、收敛证据或提交授权。报告：[实际工具与缓存测试](../guides/LA2CUO4_DEEPSEEK_TOOL_CACHE_20261010.md)。

隔离helper `.cache/la2cuo4-deepseek-20261010/run_agent.py` 调用真实Agent.ask/Toolbox.call/Responses，54完整固定工具目录，执行白名单仅inspect_structure/list_servers/指定Cu_pv目录的单次只读操作/满足连接前提后的preview；正式准备、部署、物理确认和提交均禁用。新会话 `la2cuo4-cupv-20261010T064627363905Z-d7084958e99a` 保存在工作区。DeepSeek官方deepseek-flash/auto/none思考/非流式/output4000/阶段90s/无压缩/SDK和model retry0，硬限8但实际3轮用户提问共4POST，无补满请求。input13115/14316/15055/16885；cache0/13184/14848/16640，率0/92.09/98.63/98.55%，Σinput59371/cache44672，weighted75.2421%，后三次96.5756%。四条200/completed/model匹配/rawSDK一致；静态hash固定、旧实际历史prefix保留。真实证据 `.cache/la2cuo4-deepseek-20261010/20261010T064627363905Z/report.json` 及executed-run.py/tools-audit和独立session/cache-metrics。

实际工具仅结构成功、Cu_pv ls失败，**没有赝势预览/正式准备/提交**。服务器用已有eshell111-vasp544（标准VASP环境），PBE目录 `/work/home/researcher/potpaw_PBE.64/Cu_pv`。前置查询握手超时；Agent内查询明确SSH_KEY_FILE_INVALID：WindowsOpenSSH拒绝读取权限过宽、继承CodexSandboxUsers的私钥，然后认证拒绝。根代理检查仅ACL元数据，验证当前用户为owner，备份原SDDL到 `.cache/la2cuo4-deepseek-20261010/ssh-key-acl-before.json`，只对保存配置中那份Downloads私钥关闭继承并限定当前js/SYSTEM/Administrators，未读/复制/换私钥正文，未改服务器配置或目录ACL。修复后另一次只读目录查询仍SSH超时，认证未知，证据potential-directory-after-acl.json；不能宣称连接已恢复或Cu_pv不存在。Cu/La/O实际TITEL/ZVAL/ENMAX/哈希与合并POTCAR均待核验。

模型解释暴露3处不准确：认证失败说成未到服务器；把相邻密度档网格重复误解为轴间重复；NBANDS公式不清楚。不要照抄这些话；应说目录命令未取得结果，倒格使用完整矩阵，非磁基线占据带至少ceil总价电子/2加空带余量，ZVAL未知时不报实测容量。若材料研究目标需要AFM/U，当前生产工具不支持，不能因操作者确认而擅用非磁PBE替代。

Helper异常统计旧实现会把HTTP200但校验失败的usage计入成功并可能None求和崩溃。本批四条全通过，数字有效；保持执行快照/旧报告不改，批后修正为只统计validated_response=true且禁止未校验后重复/redirect。12项离线守卫检查通过，没有额外模型POST或全量回归/生产代码改动。公开文档/13份本地会话和证据密钥扫描通过；未上传POTCAR/私钥/APIkey正文。下一步先恢复SSH连接并只读确认Cu_pv，随后预览实际三元素元数据，再讨论La₂CuO₄物理模型和生产工具支持；不要自动重跑已完成缓存批或未经授权提交计算。
