# 提示词缓存与诊断

2026-10-09 更新。重启本地 Agent 即生效，无新增依赖，无需部署集群。

## 改动

固定系统指令和发送给模型的工具目录。操作者的授权目标、额度、资源上限与禁用工具列表，作为程序生成的状态消息追加到对话末尾；未变时不重复追加。重启或上下文压缩后重新按当前程序状态核验，缺少最新状态才追加。历史消息不能恢复授权。

展示完整工具目录不授予权限。执行时仍经过原来的工具可用性检查、参数校验、提交授权、目标限制、资源确认及阶段审阅。参数草案流程启用时，直接准备入口仍被阻断。

这样消除了授权开关引起的系统指令及工具列表变化。保持共同前缀、把动态内容放后面符合 [OpenAI 提示词缓存文档](https://developers.openai.com/api/docs/guides/prompt-caching) 的建议，但本地前缀稳定不保证每个中转站请求都命中。没有更换接口、模型、流式设置或推理强度，也没有强行添加服务商可能不支持的缓存选项。

## 查看

每次成功模型响应后显示缓存读取量、比例、写入量和历史前缀变化。输入 `/usage` 可查看本次启动累计统计：

- `cache.conversation`：正常对话响应；`cache.summary`：文字摘要；`cache.native_compaction`：显式启用的原生压缩；`cache.all`：上述合计。
- `responses_with_cache_read`：同时返回输入量和有效缓存读取量的响应数。
- `cache_read_percent`：这些响应的总缓存读取量 / 总输入量。不是各轮百分比简单平均。
- `reported_cache_write_tokens`：接口报告的缓存写入量；未报告时为 `null`，不算作 0。
- `last_cache_request`：最近一次已记录响应的本地前缀诊断。

`cached_tokens=0` 表示接口明确报告零读取；`null` 表示未知。缓存写入不等于缓存读取，写入量不能计入命中率。接口返回缓存量超过输入量时标记异常，并排除该响应的累计缓存统计。这里只报告接口用量，不推算中转站费用。

每个会话旁的 `.ai-session-*.cache-metrics.jsonl` 保存逐次请求诊断。只写哈希、计数、时间、错误类别及 HTTP 状态；不写提示词正文、密钥或原始错误正文。该文件已加入 Git 忽略规则。统计在本次启动内累计，逐次文件跨重启保留；请求比较的内存基线在重启或切换 Agent 实例后重建。

关注这些字段：

| 字段 | 含义 |
| --- | --- |
| `changed_components` | 固定指令、工具、模型或请求设置哪些发生变化 |
| `history_change=append_only` | 相比上一成功请求，历史原有消息保持一致，仅尾部增长 |
| `history_change=prefix_rewritten` | 历史共同前缀改变，压缩也可能触发；不一定是错误 |
| `common_prefix_items` | 本地共有消息条数，不是缓存 token 数 |
| `returned_model_changed` | 接口返回的模型标识是否改变；标识不变不能证明实际后端未变 |

对话、摘要和原生压缩各自比较成功请求，失败不会覆盖基线。哈希是本地规范消息的诊断，不等同于服务端实际序列化或分词结果。压缩会改变历史前缀，但可以降低总输入量；不要仅为提高命中率关闭必要压缩。

## 可重复的小型实测

在项目根目录运行：

```bash
# 离线显示配置概要，不发送请求
python examples/probe_cache.py --profile relay --dry-run

# 3次完全相同的真实模型请求，合成参考约15k输入tokens；实际大小以响应为准
python examples/probe_cache.py --profile relay

# 更小的约3k输入对照
python examples/probe_cache.py --profile relay --records 120
```

默认只发3轮、每轮输出上限1024、超时90秒、SDK重试关闭；首次错误、意外工具调用或不完整响应后停止。使用所选接口会产生模型费用。参考资料和权限状态均为合成数据，不读取真实对话、不调用任何工具、不连接计算服务器。测试沿用生产协议适配器及缓存统计模块。新版默认 `--mode replay` 重复相同请求；`--mode append` 才逐轮追加实际模型响应和合成状态变化。

报告保存到 `.cache/cache-probe/<时间>/report.json`，每次请求另有 `requests.jsonl`。`completed=true` 只表示响应序列完成，不能解读为命中率改善或缓存能力验收成功。

## 第一版实测限制（逐轮追加）

2026-10-09，当前 relay、`gpt-6-astra`、Responses、非流式：

| 合成输入规模 | 结果 |
| --- | --- |
| 120条参考，3轮 | 实际输入3073 / 3117 / 3161 tokens；缓存读取均0；缓存写入均0 |
| 600条参考，第一次 | 第一轮5.70秒后 APIConnectionError，无用量，停止 |
| 600条参考，独立再试一次 | 第一轮5.76秒后 APIConnectionError，无用量，停止 |

小输入三轮指令、工具和设置哈希一致，后两轮历史均仅追加。报告目录分别为 `20261009T030622163679Z`、`20261009T030856016881Z`、`20261009T030920312600Z`。

这些结果验证了请求结构与统计通路，尚未验证中转站高命中。不能将较大输入的连接失败归因于缓存或容量，也不能据此声称已达到截图中 Codex 的命中率。后续真实会话可用 `/usage` 和逐次诊断区分本地前缀变化与接口返回零缓存；如前缀稳定仍低命中，需要结合服务商的路由和原始用量记录继续定位。

## 第二版：精确重放与单变量对照

以下参数仅改变独立测试，不修改 `.env`、`models.toml`、真实会话或日常 Agent 的行为。先用 `--dry-run` 确认选项。每条命令是一组独立请求，会产生费用；不要把多项开关一起改变后，将差异归因于其中某项。

```bash
# 基线：相同模型、相同历史、相同工具、相同请求参数，连续重复3次
python examples/probe_cache.py --profile relay --records 120 --mode replay

# 只改变流式开关，保留相同输入和推理默认设置
python examples/probe_cache.py --profile relay --records 120 --mode replay --stream

# 只改变推理强度，与第一组比较
python examples/probe_cache.py --profile relay --records 120 --mode replay --reasoning-effort high

# 逐轮追加，模拟Agent延长会话
python examples/probe_cache.py --profile relay --records 120 --mode append

# Responses可选能力探测：第二轮加入上一响应ID，请求缓存诊断
python examples/probe_cache.py --profile relay --records 120 --rounds 2 --diagnostics

# 固定合成参考末尾设置显式断点，保留隐式缓存；仅Responses
python examples/probe_cache.py --profile relay --records 120 --cache-mode implicit --breakpoint
```

可以显式指定 `--protocol responses` 或 `--protocol chat_completions`，不自动回退。两种协议均支持测试流式与推理强度；诊断和断点测试目前仅实现 Responses。`--cache-mode explicit` 必须同时有 `--breakpoint`。接口不支持时记录错误并停止，不切换服务商、不删掉参数重试。输出目录已有 `report.json` 时拒绝覆盖。

`--diagnostics` 会在后续请求添加 `comparison_response_id`，因此即使使用 replay，整份请求参数也不再完全相同；不要把此组当作精确重放基线。该参数只请求比较，不加载服务器历史。返回成功但未提供诊断时，`diagnostics_present=false`，不能据此宣称支持。参考[官方缓存诊断](https://developers.openai.com/api/docs/guides/prompt-caching/diagnostics)；中转站是否透传需独立核验。

新报告增加：

- `request_parameters_hash`：交给 SDK 的最终请求参数哈希，不是抓取 TLS 字节，也不含认证请求头。
- `identical_request_parameters`：该组实际尝试的参数是否全部相同；需同时检查 `completed`，单次失败不能证明重复测试完成。
- `wire_usage`、`cached_tokens_present`、`cache_write_tokens_present`：非流式直接检查收到的 HTTP JSON；流式检查 SDK 汇总响应中实际收到的字段。能区分字段缺失和显式零，不能证明中转站没有改写上游数据。
- `cache_diagnostics`：只保留有限的诊断枚举、数值，其他自由文本以哈希替代；响应ID仅以哈希落盘，比较所需ID保留在内存。
- `error_chain_types`：异常链的类型，不保存私密异常正文，可区分 TLS、连接与 HTTP 错误。
- `cache_observed`：是否有任一响应报告缓存读取大于零；不是稳定高命中的证明。

### 第二版实测（2026-10-09，香港时间14:26–14:28）

同一 relay / gpt-6-astra / Responses，生产配置未改：

| 测试 | 结果 |
| --- | --- |
| replay基线初次尝试 | 首轮APIConnectionError，停止，未取得用量 |
| replay基线独立再试 | 3轮完成，每轮3073输入tokens，读取/写入均0；最终请求参数哈希全部相同；HTTP JSON确实包含两个零字段 |
| 仅开启流式 | 首轮TLS SSLEOFError，停止；不能判断流式对缓存的影响 |
| 可选官方诊断组 | 首轮TLS SSLEOFError，未到达第二轮诊断请求；不能判断接口支持情况 |

报告目录依次为 `20261009T062616933810Z`、`20261009T062747269213Z`、`20261009T062806705060Z`、`20261009T062827005556Z`。没有运行真实推理强度或断点对照，不能声称这些开关有效；已经用真实SDK加本地模拟HTTP/SSE验证两种协议的解析和用量保留。

目前可以排除“这组三次请求是因为本地历史变化才没有复用”及“本地统计将缺失缓存字段补为零”这两种解释。仍需在连接稳定后完成同规模流式、推理强度及诊断对照，并向服务商核实缓存写入/上游路由；尚未与真实Codex请求做逐字段抓取对照。
