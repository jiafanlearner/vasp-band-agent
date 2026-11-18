# 开源 Agent 的缓存请求构建：源码研究

研究日期：2026-10-09。只读取公共源码，并比对 VASP Agent 当前实现；没有调用付费模型 API、连接集群或改动生产代码。结论来自固定提交的请求构建函数，不把旧 issue、修复计划或界面上的命中率当作当前实现。

**主要发现：这些项目把模型前缀的 KV 缓存交给 API 服务端，客户端负责稳定请求前缀、缓存分组标识、协议兼容和用量观测。Codex、OpenCode、Pi 都有稳定缓存 key；我们正式请求目前缺少这条可选参数通道。不过相同正文高命中后归零仍无法由源码研究确定根因，不能承诺复制 key 就解决中转站问题。**

## 固定源码版本

| 项目 | 检视提交 | 重点 |
|---|---|---|
| openai/codex | `a06545b311fe01e51ce855c7aa5d8da21e9e7aaf` | key、同轮路由状态、增量历史、MCP 工具排序 |
| anomalyco/opencode | `388406238bd5ca15564a762840a2362c3a45bd9c` | session key、工具排序、服务商缓存策略；区分现存 V1/V2 路径 |
| cline/cline | `75111f347f275022abea2b2d784901920d8618be` | 当前 SDK 重构后的 provider metadata、Anthropic 标记、usage |
| earendil-works/pi | `f1b2e77f5b13b2a199b1052cb79c235451afe7d7` | session key、能力开关、保留参数、可选预热的成本判断 |
| openai/openai-agents-python | `125efa029b4bfd84238bd2c4fd69c3406f802663` | run/session key 生成与恢复、官方 endpoint 限定、参数透传 |
| langchain-ai/langchain | `34489d61433cbf5044a630de2e7797e361672900` | 区分本地答案缓存、provider 缓存统计和服务端上下文续接 |

`badlogic/pi-mono` 的公开仓库请求当前重定向到 `earendil-works/pi`；采用 GitHub API 返回的规范仓库和提交，避免把 fork 或第三方说明当成主项目源码。

## Codex：分开管理会话 key、同轮路由和增量上下文

`ModelClient::prompt_cache_key()` 优先采用 override；部分内部会话按父线程分组，其余用 session_id。`build_responses_request()` 在真实 Responses 请求中设置该 key，通常不是每次生成随机 key，也不是用整份不断增长的历史作 key。[key 函数](https://github.com/openai/codex/blob/a06545b311fe01e51ce855c7aa5d8da21e9e7aaf/codex-rs/core/src/client.rs#L638-L650)、[请求构建](https://github.com/openai/codex/blob/a06545b311fe01e51ce855c7aa5d8da21e9e7aaf/codex-rs/core/src/client.rs#L948-L1070)

ChatGPT 后端还有 `session-id` 的 cache affinity。`ModelClientSession` 保存服务端返回的 `x-codex-turn-state`，只在同一轮续写/重试中原样回传；跨轮复用会违反其合约。它不是一个可以随意复制给其他中转站的万能 HTTP 头。[会话亲和](https://github.com/openai/codex/blob/a06545b311fe01e51ce855c7aa5d8da21e9e7aaf/codex-rs/core/src/client.rs#L652-L660)、[同轮状态](https://github.com/openai/codex/blob/a06545b311fe01e51ce855c7aa5d8da21e9e7aaf/codex-rs/core/src/client.rs#L282-L308)

WebSocket 增量续接前，`get_incremental_items()` 检查旧 input 加已返回 output 是否为新 input 的前缀，且其他请求属性没有变化；通过才使用 previous_response_id 和新后缀，否则发送全量。减少网络传输与提高 cached_tokens 不是同一个保证。[增量验证](https://github.com/openai/codex/blob/a06545b311fe01e51ce855c7aa5d8da21e9e7aaf/codex-rs/core/src/client.rs#L1454-L1518)

MCP 工具规范化按原始工具身份排序，namespace 内也排序。历史上确有将 HashMap 的随机工具顺序改为确定顺序的修复；不能泛称所有外层工具都全局按字母排序。[当前工具排序](https://github.com/openai/codex/blob/a06545b311fe01e51ce855c7aa5d8da21e9e7aaf/codex-rs/codex-mcp/src/tools.rs#L113-L214)、[真实排序修复](https://github.com/openai/codex/commit/ee2ccb5cb6e2dc73fef72fc008801ca012b8f4f2)

标准 Responses wire struct 有 prompt_cache_key，检视路径没有 retention、Anthropic cache_control 或 OpenAI breakpoint 字段；使用服务端报告的 cached_tokens 统计。因此不能把 Codex 的正常命中归结为“它一定加了断点”。[请求结构](https://github.com/openai/codex/blob/a06545b311fe01e51ce855c7aa5d8da21e9e7aaf/codex-rs/codex-api/src/common.rs#L278-L360)、[usage 解析](https://github.com/openai/codex/blob/a06545b311fe01e51ce855c7aa5d8da21e9e7aaf/codex-rs/codex-api/src/sse/responses.rs#L128-L151)

## OpenCode：稳定 session key 与 provider 缓存策略分层

V1 `ProviderTransform.options()` 对 OpenAI adapter 默认将 sessionID 放入 promptCacheKey，setCacheKey 可配置。`LLMRequestPrep.prepare()` 对工具名称排序。工具集合仍可能因权限变化改变，排序只解决顺序抖动。[session key](https://github.com/anomalyco/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/opencode/src/provider/transform.ts#L1323-L1335)、[工具排序](https://github.com/anomalyco/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/opencode/src/session/llm/request.ts#L148-L185)

现存 V2 `runTurnAttempt()` 也使用 session-derived key；原生 OpenAI Responses `lowerOptions()` 将它序列化为 prompt_cache_key。两条路径都存在，不把 V2 实现自动推定为所有现有 CLI 的运行路径。[V2 key](https://github.com/anomalyco/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/core/src/session/runner/llm.ts#L183-L223)、[wire 参数](https://github.com/anomalyco/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/llm/src/protocols/openai-responses.ts#L456-L475)

`applyCachePolicy()` 只给 Anthropic Messages/Bedrock Converse 加 inline hint；默认边界为最后的工具定义、system 部分与最新 user。检视的 OpenAI 路径跳过这套标记策略。它与 OpenAI 新模型自身的 breakpoint 是不同契约，不能据此声称 OpenAI 不支持其官方断点。[策略代码](https://github.com/anomalyco/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/llm/src/cache-policy.ts#L18-L45)、[provider gate](https://github.com/anomalyco/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/llm/src/cache-policy.ts#L99-L110)

Responses usage 读取原始 cached_tokens；当前 V2 摘要请求不复制主请求的 providerOptions/key。旧 issue 关于“摘要共用 key”的描述不能直接当作此提交的现状。[usage](https://github.com/anomalyco/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/llm/src/protocols/openai-responses.ts#L503-L520)、[摘要请求](https://github.com/anomalyco/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/core/src/session/compaction.ts#L178-L229)

## Cline：按服务商元数据选择标记，不是所有接口统一打开

当前 SDK 路径已移至 `sdk/packages/llms/src/providers`。`buildAiSdkRequestMessages()` 按顺序转换历史，再根据 provider routing metadata 决定是否给最新 user 的末尾 text part 加缓存信息。Anthropic built-in 已带 routing metadata；generic compatible 的能力标签不等于自动套用同一策略。[消息构建](https://github.com/cline/cline/blob/75111f347f275022abea2b2d784901920d8618be/sdk/packages/llms/src/providers/ai-sdk.ts#L385-L423)、[provider 元数据](https://github.com/cline/cline/blob/75111f347f275022abea2b2d784901920d8618be/sdk/packages/llms/src/providers/builtins.ts#L1191-L1202)

`createPromptCacheProviderOptions()` / `applyPromptCacheToLastTextPart()` 在不同 provider option bucket 中发送 ephemeral cache control。这是服务商适配，不是 OpenAI Responses 的统一断点。[标记转换](https://github.com/cline/cline/blob/75111f347f275022abea2b2d784901920d8618be/sdk/packages/llms/src/providers/routing/anthropic-compatible.ts#L94-L176)

检视的当前 OpenAI builders 没有自动注入 session cache key，工具转换保留调用者顺序。Cline 并非上述机制全部具备的“标准答案”。它有构建后 prompt 诊断捕获，并归一化各 provider 的缓存读写 usage；缺失值会归零，这部分不应替换我们已有的 missing≠0 观测。[OpenAI 构建](https://github.com/cline/cline/blob/75111f347f275022abea2b2d784901920d8618be/sdk/packages/llms/src/providers/routing/provider-option-rules.ts#L173-L211)、[请求诊断](https://github.com/cline/cline/blob/75111f347f275022abea2b2d784901920d8618be/sdk/packages/llms/src/providers/ai-sdk.ts#L2287-L2358)、[usage](https://github.com/cline/cline/blob/75111f347f275022abea2b2d784901920d8618be/sdk/packages/llms/src/providers/ai-sdk.ts#L1265-L1315)

## Pi：缓存选项有明确兼容开关，预热也核算成本

普通 OpenAI Responses `buildParams()` 从 sessionId 设置 prompt_cache_key。兼容配置控制长保留/显式模式的支持；根据情况选择旧 retention 或新 prompt_cache_options，并为 ChatGPT sign-in 省去已知不兼容的选项。[请求参数](https://github.com/earendil-works/pi/blob/f1b2e77f5b13b2a199b1052cb79c235451afe7d7/packages/ai/src/api/openai-responses.ts#L302-L338)、[能力与保留映射](https://github.com/earendil-works/pi/blob/f1b2e77f5b13b2a199b1052cb79c235451afe7d7/packages/ai/src/api/openai-responses.ts#L82-L114)

`createClient()` 区分 OpenRouter 的 x-session-id 与其 OpenAI 会话格式；Codex Responses 路径另行构建专属 headers。源码说明作者做了 provider-specific 处理，不证明 verysadai 实现其中任何 header 的亲和性。[普通会话头](https://github.com/earendil-works/pi/blob/f1b2e77f5b13b2a199b1052cb79c235451afe7d7/packages/ai/src/api/openai-responses.ts#L259-L285)、[Codex 会话头](https://github.com/earendil-works/pi/blob/f1b2e77f5b13b2a199b1052cb79c235451afe7d7/packages/ai/src/api/openai-codex-responses.ts#L1663-L1680)

`CacheWarmer.evaluate()` 比较可能避免的 miss 成本与额外预热成本，预期节省达阈值才决定 warm，另有关闭、上下文变更与时间上限检查。因此“为了命中不停发送请求”并非可直接复制的方案。[成本决策](https://github.com/earendil-works/pi/blob/f1b2e77f5b13b2a199b1052cb79c235451afe7d7/packages/coding-agent/src/core/cache-warmer.ts#L378-L399)、[停止检查](https://github.com/earendil-works/pi/blob/f1b2e77f5b13b2a199b1052cb79c235451afe7d7/packages/coding-agent/src/core/cache-warmer.ts#L186-L223)

## Agents SDK 与 LangChain：透传机制和本地缓存要分开

Agents 的 `PromptCacheKeyResolver` 在同一 run 复用 key，并保存到 RunState 供恢复；尊重调用者设置，按 session/conversation/group 生成稳定分组。自动生成限定官方 OpenAI client，不把此默认行为直接推定给 relay。[生成与恢复](https://github.com/openai/openai-agents-python/blob/125efa029b4bfd84238bd2c4fd69c3406f802663/src/agents/run_internal/prompt_cache_key.py#L17-L130)、[官方 endpoint 限定](https://github.com/openai/openai-agents-python/blob/125efa029b4bfd84238bd2c4fd69c3406f802663/src/agents/models/openai_responses.py#L535-L536)

其请求构建分别透传 store、previous_response_id、retention/options 和额外参数。raw usage 可保留字段存在性。这些不是本地实现模型 KV 缓存，也不是换 SDK 就必然提高命中。[请求构建](https://github.com/openai/openai-agents-python/blob/125efa029b4bfd84238bd2c4fd69c3406f802663/src/agents/models/openai_responses.py#L1032-L1073)、[原始 usage](https://github.com/openai/openai-agents-python/blob/125efa029b4bfd84238bd2c4fd69c3406f802663/src/agents/usage.py#L20-L45)

LangChain `set_llm_cache` 对完整 messages 与模型参数查本地 Generation 缓存，命中直接返回旧答案，不发 API。它不会提高 API 返回的 cached_tokens。`use_previous_response_id` 则省去已由服务端续接的旧消息，属于另一机制。[本地命中提前返回](https://github.com/langchain-ai/langchain/blob/34489d61433cbf5044a630de2e7797e361672900/libs/core/langchain_core/language_models/chat_models.py#L1892-L1924)、[续接请求](https://github.com/langchain-ai/langchain/blob/34489d61433cbf5044a630de2e7797e361672900/libs/partners/openai/langchain_openai/chat_models/base.py#L2031-L2039)

## 与我们当前代码的差距

| 做法 | VASP Agent 当前状态 | 是否值得新增 |
|---|---|---|
| 稳定基础指令、工具目录；权限/运行状态后置 | 已实现；不能重复做一遍当作新修复 | 保持，并测试不变性 |
| 追加历史，保留工具调用配对与 reasoning | 已实现；压缩或配置变化另会改变前缀 | 保持 |
| missing≠0、缓存读写、逻辑组件 hash | 已实现；probe 已核对原始 HTTP usage | 保持，复用现有代码 |
| 稳定 key 的生成、恢复及 adapter 透传 | 正式 Responses/Chat 请求均没有此通道 | **最值得单独试验的真实差异** |
| provider 缓存能力与参数适配 | 尚无完整配置；当前 breakpoint 实测 400 | 采用显式配置，未核验能力不默认开启 |
| 最终 HTTP 正文 hash、request ID、原始字段存在性 | 合成隔离测试具备；生产遥测主要是逻辑请求 hash | 将诊断能力复用到生产；只存 hash/安全数值 |
| Codex 同轮状态与 WebSocket 增量续接 | 当前普通 create 不具备 | 不是第一步；须先核验 relay 合约和恢复语义 |
| 预热/保活 | 未实现 | 暂不新增，先确认命中与实际成本收益 |
| 本地旧答案直接重放 | 不是当前缓存问题的解决机制 | 不用于实时任务状态、审阅、授权或提交 |

本地源码位置：`src/vasp_agent/models/model_adapters.py` 的 `ResponsesAdapter.create()` 第 23–29 行和 Chat 构建第 63–71 行；`src/vasp_agent/chat/ai_agent.py` 的 context_instructions/runtime_state/model_schemas/ask 请求流程；`src/vasp_agent/models/prompt_cache.py` 和 `cache_probe_transport.py` 的用量与指纹记录。

## 适合本项目的下一步

1. **先单独验证稳定 key。** 保留当前顶层 instructions、模型、工具、协议、输出限额和消息编码，比较不传 key 与同一合成会话固定 key。分别测精确 replay 和历史追加，按未缓存输入量、总命中率与实际费用评估；不混入 headers、断点、流式或推理设置。该测试本轮尚未执行。
2. 若验证有帮助，再实现配置可选的 CachePolicy：同一真实会话重启后 key 不变，模型/API profile 改变时隔离；不要用每次 request ID、时间戳或不断增长的历史 hash 生成 key。摘要与主对话分开记录，避免污染诊断。
3. 将现有 wire hash、request ID、raw usage 安全子集复用到正式请求观测。先提高可解释性，避免用户只能看账单猜原因。
4. 保留服务商能力开关；不对 relay 自动套用 Anthropic 标记、ChatGPT headers 或未经验证的 TTL。断点拒绝与稳定正文偶发归零已有证据，不能当成纯本地前缀问题。

官方当前说明：GPT-5.6 及以后的 OpenAI 模型自动处理缓存路由，key 可用于分开统计，但不是优化缓存的必需条件；也不保证固定在同一台机器。我们当前模型名为 gpt-6-astra，且经过第三方中转，因此稳定 key 应被视作可验证的兼容策略，不是已证明的遗漏根因。[OpenAI 官方缓存说明](https://developers.openai.com/api/docs/guides/prompt-caching)

此前本机 [编码实测](CACHE_ENCODING_TEST_20261009.md) 中 A/B/C 都不稳定，C 的同正文 99.01%→0，D 因断点参数返回 400。研究结论没有推翻这些测量，也没有宣称生产缓存已修复。
