# 个人开发 Agent 低缓存命中：GitHub 调查

调查日期：2026-10-09。用户希望先自行排查，暂不联系中转站管理员。本轮只检索项目 issue、修复 PR、源码、公开测试记录与供应商文档，没有增加模型 API 请求，也没有修改生产代码。

## 核心发现

确实有开发者遇到低命中，甚至相同请求先高命中、下一次为零。另有项目通过消息编码、明确缓存边界、稳定工具目录等改进了客户端实现。需要区分开发者报告、已合并的代码修复和真实接口效果，不能把“issue 已关闭”理解为所有供应商都稳定高命中。

我们此前的受控测试中，相同 HTTP 正文、9,334 输入 tokens、普通 Responses 调用，缓存从 9,216 降到 0。这个现象最接近 Codex #30425，尚未查到能通用解决它的已确认修复。客户端仍有可自行验证的方向，尤其是指令位置、消息编码和稳定前缀边界。

## 案例与适用范围

| 案例 | 开发者遇到什么 | 实际处理与当前状态 | 对我们的意义 |
|---|---|---|---|
| [Codex #30425](https://github.com/openai/codex/issues/30425) | 固定 key、相同请求指纹、重放请求仍零缓存；相邻流量也出现高命中后归零。 | 2026-06-28 提出，当前 Open；未见公开确认根因或关联修复。 | 最贴近我们的精确 replay 现象；固定 key 不保证命中。 |
| [Effect #6342](https://github.com/Effect-TS/effect/issues/6342) | 官方 Responses、gpt-4.1-mini 下，system 的 string 内容三次缓存均 0；typed input_text 为 0 / 4352 / 0。 | [PR #6549](https://github.com/Effect-TS/effect/pull/6549)于 2026-07-26 合并，将 system/developer 改为 typed input_text。 | 值得做编码对照；typed 也有零命中，且我们当前指令位于顶层 instructions，场景不同。 |
| [Codex #35300](https://github.com/openai/codex/issues/35300) | 固定约 9k 前缀、变化的最终用户消息，Bedrock Mantle / gpt-5.6-sol 下作者报告零命中。 | 作者仅加入前缀末尾断点后报告约 98.6%；issue 仍 Open。部分订阅后端另有人报告拒绝该参数。 | 支持测试正确断点；其变化后缀场景不能解释我们同正文失去缓存，兼容性必须先核验。 |
| [Cherry Studio #15798](https://github.com/CherryHQ/cherry-studio/issues/15798) | MCP 工具和动态系统内容影响前缀，缓存统计不清楚。 | [PR #16603](https://github.com/CherryHQ/cherry-studio/pull/16603)于 2026-07-03 合并：确定性排序、避免时间模板污染缓存边界、Anthropic 缓存和读写统计。 | 稳定目录与动态内容后置可借鉴；我们已做部分同类改进，不能把 Anthropic 参数照搬进 Responses。 |
| [Zoo Code #1277](https://github.com/Zoo-Code-Org/Zoo-Code/issues/1277) | 开发者报告 OpenRouter 多轮 Agent 缓存低，涉及会话路由、新模型能力名单与统计。 | issue Open，[PR #1278](https://github.com/Zoo-Code-Org/Zoo-Code/pull/1278)仍 Draft。 | 路由确实值得关注；OpenRouter 专属参数不代表 verysadai 支持。 |
| [Agents Python #4270](https://github.com/openai/openai-agents-python/issues/4270) | SDK 将缓存字段缺失和明确零归一为同一种零。 | [PR #4279](https://github.com/openai/openai-agents-python/pull/4279)于 2026-08-07 合并，支持保留 raw usage。 | 修复的是可观察性；我们已核对 HTTP 明确零，不是这次剩余问题的答案。 |
| [PraisonAI #2993](https://github.com/MervinPraison/PraisonAI/issues/2993) | system 混入动态信息，历史缺少缓存断点。 | [PR #2996](https://github.com/MervinPraison/PraisonAI/pull/2996)于 2026-07-14 合并，主要增加 Anthropic 历史断点；真实 API 集成测试跳过。 | 借鉴不可变历史及稳定前缀；不当作已证实的 OpenAI 高命中方案。 |
| [OpenClaw #91223](https://github.com/openclaw/openclaw/issues/91223) | 报告者称 active-memory 动态注入使命中从约 99.9% 降至 22%，停用后恢复。 | Closed as not planned；缺少当前版本可靠复现，不能当成维护者确认的根因。 | 检索记忆、时间、队列等动态状态应避免每轮插入旧前缀。 |

Effect 的公开复现两臂还用了不同的 STRING / INPUT_TEXT 文本标签，输入分别为 4,368 和 4,549 tokens，并非严格同文、只改 JSON 编码的实验。它提供开发者报告与维护者修复线索，不能单独证明编码是唯一原因。本机后续 B/C 实验保持指令原文完全相同。

## 有公开测试记录的个人项目

[caching.ai](https://github.com/caching-ai/caching.ai/blob/main/bench/README.md)公布了缓存代理与直连的实验。S6 原始日志中，gpt-5.6-sol 每臂 60 次调用、约 30 秒间隔，直连报告零读取，代理臂 59 次命中，按 tokens 加权约 97.76%。本轮查阅并重算了公开数据，未独立重跑 API。[原始 JSONL](https://github.com/caching-ai/caching.ai/blob/main/bench/results/run-202607-v0100/S6_gpt56_r1.jsonl)

[实现](https://github.com/caching-ai/caching.ai/blob/main/apps/proxy/src/logic/cacheControl.ts)在共享 system/developer 末尾添加断点，并由稳定前缀及 tools 哈希派生 key。该 GPT 单元没有保活 ping。实验同时改变了路由和缓存参数，不能分离各项作用；场景是无工具、流式 Chat Completions、变化用户问题，也与我们的 Responses 精确 replay 不同。节费数据按 usage 与标价计算，不是我们核验的账单。[方法与限制](https://github.com/caching-ai/caching.ai/blob/main/BENCHMARK.md)

可借鉴其断点构建和对照设计，无需迁移到另一家托管代理。公开审计项目 [audit-prompt-caching](https://github.com/sernote/audit-prompt-caching)则主要是审计技能和合成示例，不应当作真实高命中修复实测。

## 对我们可自行进行的下一组实验

先将尚未验证的客户端因素隔离，而不是立即改真实会话默认行为。

| 组别 | 具体变化 | 能检验什么 |
|---|---|---|
| A | 当前顶层 instructions，其余请求保持现状。 | 生产基线。 |
| B | 同一段 instructions 移至 input 首条 developer，content 仍为 string。 | 指令位置；A/B 不是纯编码对照。 |
| C | 与 B 只差 content 改成 typed input_text 数组。 | B/C 才是纯消息编码对照。 |
| D | 与 C 只差稳定块末尾加入 OpenAI prompt_cache_breakpoint，保留隐式模式。 | 兼容性通过后检验明确边界。 |
| E，可选 | 单独比较不传 key 与固定 key，其余参数相同。 | 中转站是否存在可测的 key 行为；不是命中保证。 |

每组使用合成材料、固定模型/协议/工具/推理设置/输出上限，按组交错顺序重复数轮；不执行计算工具。记录实际 HTTP 正文哈希、请求 ID、有效请求数量、输入与缓存量，按总缓存 / 总输入统计，不挑最高一次作为结论。首轮可能暖缓存，应如实记录，不能宣称冷启动。限制总请求数及输出，发生失败停止对应组。

已有断点测试曾得到 HTTP 400，但没有足够错误细节判定是不支持还是形状问题。先做独立兼容检查，保留脱敏的 error code/param；HTTP 200 但字段被忽略也不能证明上游使用了该功能。不要自动删参数、换服务商或复制其他供应商的 cache_control。

OpenAI 官方文档说明：顶层 instructions 本身不能放明确断点；需要把可复用指令放在 developer 的 input_text 中。对 GPT-5.6 及以后模型，key 不是优化缓存的必需项，缓存仍受可用节点与路由影响。因此上述选项应默认关闭，实验有改善后才决定生产配置。[官方缓存说明](https://developers.openai.com/api/docs/guides/prompt-caching)

## 开源项目普遍采用的工程方法

1. 固定指令、工具定义与顺序，变化权限和运行状态只追加；配置真实变化时另记前缀版本。
2. 保留完整旧消息与工具配对；减少不必要的摘要重写。压缩后首次命中下降应单独归因记录，不因此放任输入无限增长。
3. 区分明确零与缺失字段，展示每次未缓存输入，并分开对话、摘要和诊断请求。
4. 区分协议和供应商缓存契约：OpenAI 断点、Anthropic cache_control、OpenRouter 会话亲和不能互换。
5. 同时减少总输入：去掉重复日志、限制工具输出，避免为了提高命中百分比填充无用文字或无收益保活。

OpenRouter 的会话亲和有明确官方说明，可通过 session_id 或 x-session-id 使用，但仅适用于其支持的路由；闲置或上游失效会影响亲和。[OpenRouter 文档](https://openrouter.ai/docs/guides/best-practices/prompt-caching#using-session-id-for-sticky-sessions)

## 当前建议

优先做 A/B/C 编码与指令位置对照，其次兼容性探测与 D 断点对照。我们已经完成前缀稳定化及原始用量核对，不必重新实现；正式会话中可补齐 wire hash 和请求 ID。上述研究提供可自行验证的候选方法，尚未确证能修复我们的同正文 98.74%→0。暂不需要联系管理员，也不应直接强制开启所有缓存参数。
