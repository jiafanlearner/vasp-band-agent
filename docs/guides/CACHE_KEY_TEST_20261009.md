# 稳定缓存 key 的受控实测

日期：2026-10-09，香港时间。接续 [开源源码研究](OPEN_SOURCE_AGENT_CACHE_ARCHITECTURE_20261009.md)，检验 Codex、OpenCode、Pi 使用的稳定会话缓存标识在当前 relay 上是否有可见收益。这里的 prompt_cache_key 是请求分组标识，不是 API 密钥。

## 设计与执行边界

接口沿用 relay / https://verysadai.com/v1 / gpt-6-astra / Responses。普通 SDK create，顶层 instructions 不移动，保留 54 个固定工具定义、固定权限快照和原有请求选项。非流式、输出上限 1,024、超时 90 秒、SDK/model 重试均为 0、自动压缩关闭，请求间暂停 15 秒。仅合成输入，不读取真实会话、不执行工具、不访问集群。

| 组别 | 模式 | 唯一缓存参数变化 |
|---|---|---|
| R0 | 精确 replay，四次完整 input 不变 | 完全省略 prompt_cache_key |
| R1 | 与 R0 相同 input | 同一固定 replay key |
| A0 | 四轮合成 append，保留旧 prefix | 完全省略 prompt_cache_key |
| A1 | 与 A0 每轮相同 input | 同一固定 append key |

两种模式使用不同固定 key，日志只保存其 hash。并未同时加入 headers、TTL、断点、推理强度或其他选项。合成 append 追加预定义 assistant 文本 cache-probe-ok、新 user 和不变权限消息；不注入各响应的动态 ID/reasoning。该控制方式检验合成历史追加，不替代真实工具调用场景验收。

各组四次，预设 16 个有效轮次，全部尝试上限 20（包括超时或连接失败）。顺序按四轮固定为：R0/R1/A1/A0；A0/A1/R1/R0；R1/R0/A0/A1；A1/A0/R0/R1。每种模式两次有 key 先、两次无 key 先，避免永远让一个方案吃到更晚的暖缓存。

SDK MockTransport 离线验证通过：真实序列化 JSON 只差 key，调用者历史未修改；replay 恒定、append 保留完整旧前缀。现场 HTTP hook 另记录每次完整正文 hash、删除 key 后的规范 JSON hash、input hash、HTTP 状态、request ID、原始 usage 安全数值和字段存在性，并核对 SDK 数值。未保存认证头、API 密钥或完整响应正文。

恢复只允许没有 HTTP 响应的 APIConnectionError/APITimeoutError；不恢复参数拒绝、认证失败或 usage 校验失败。恢复前核验 fixture 和 key hash，仅重做失败/尚未完成的轮次，成功轮不重复。

## 执行与中断记录

证据目录：`.cache/cache-key/20261009T114257101105Z/`，包括 matrix.json、http-events.jsonl、analysis.json、tls-observations.json。实际执行跨度为香港时间 **19:42:57–21:24:44**，含中断等待；不能当作连续运行约 102 分钟的测试。

最终完成预定 **16 个有效轮次、8 个完整配对**，共 20 次模型请求尝试，达到原定预算，没有扩大测试量。全部有效响应 HTTP 200，返回状态 completed、指定短文本、没有工具调用；原始 HTTP usage 与 SDK 数值逐项一致。返回模型及 service tier 的各自 hash 在所有有效响应中恒定。实际调用工具次数为 0。

| 尝试编号 | 组别/轮次 | 香港时间 | 失败 | 耗时 |
|---|---|---|---|---:|
| 8 | R0 / 2 | 19:45:17 | APIConnectionError，底层 SSLEOFError | 5.016 秒 |
| 10 | R1 / 3 | 19:48:45 | APIConnectionError，底层 SSLEOFError | 5.015 秒 |
| 12 | R0 / 3 | 19:55:00 | APIConnectionError，底层 SSLEOFError | 5.016 秒 |
| 14 | A0 / 3 | 20:03:11 | APITimeoutError，底层 ReadTimeout | 90.515 秒 |

这四次均没有 HTTP 响应，缓存与输入用量未知，不计为零，也不能断言服务端必然没有执行或计费。原进程和四次恢复共五个进程区段，每次保留同一实验目录、key、指令/工具/历史 hash；成功轮次没有重复。没有更换服务商、协议、TLS 验证或请求参数。恢复保持请求内容，但不能保证新连接落在相同上游缓存节点，也无法保持连续时间控制。

## 实测结果

每种模式均有四个完整配对，下表的命中率为 **总 cached_tokens / 总 input_tokens**；不平均各轮百分比。输入总量只统计有有效响应的轮次。

| 模式与参数 | 输入总 tokens | 四轮缓存 tokens | 缓存总 tokens | 加权命中率 | 未缓存输入 tokens |
|---|---:|---|---:|---:|---:|
| replay，无 key | 37,340 | 0 / 0 / 5,248 / 8,960 | 14,208 | **38.05%** | 23,132 |
| replay，固定 key | 37,340 | 5,248 / 9,216 / 9,216 / 9,216 | 32,896 | **88.10%** | 4,444 |
| append，无 key | 38,228 | 8,960 / 5,248 / 0 / 5,248 | 19,456 | **50.89%** | 18,772 |
| append，固定 key | 38,228 | 0 / 0 / 9,344 / 9,216 | 18,560 | **48.55%** | 19,668 |

replay 每轮输入都是 9,335 tokens；append 每轮分别为 9,335、9,483、9,631、9,779 tokens。replay 各组所有尝试的完整 HTTP 正文 hash 恒定；两模式各轮配对正文移除 key 后的规范 JSON hash 一致、输入 tokens 相同。两种 key 各自全程保持不变，无 key 组字段确实省略。因此没有发现历史改写或偷偷添加其他参数造成的组间差异。

replay 固定 key 后三轮均为 **98.73%**，达到预先设定的候选标准。但 append 固定 key 前两轮为零，后两轮才达到 **97.02%、94.24%**，总体没有改善；无 key 组也有高命中。不能把 replay 的收益直接推广为真实 Agent 多轮对话已稳定修复。

### 配对间隔与同进程敏感性

| 模式 | 轮次 | 两个请求开始时间间隔 | 同一进程区段 |
|---|---:|---:|---|
| replay | 1 | 18.231 秒 | 是 |
| replay | 2 | 207.321 秒 | 否 |
| replay | 3 | 491.125 秒 | 否 |
| replay | 4 | 20.007 秒 | 是 |
| append | 1 | 18.723 秒 | 是 |
| append | 2 | 21.432 秒 | 是 |
| append | 3 | 18.247 秒 | 是 |
| append | 4 | 23.557 秒 | 是 |

仅保留同进程的 replay 第 1、4 轮，各组输入 18,670 tokens，无 key 命中 **47.99%**，固定 key **77.47%**。方向仍相同，但只剩两个配对，两个区段之间又有长时间间隔，不能视为连续四轮复制成功。**这两个保留配对都是无 key 先、key 后**，失去了原设计的先后平衡，仍有暖缓存顺序偏差；不能把它解释为已排除中断混杂后的因果效果。

append 四对均在各自区段内相邻执行，每个前后区段内也各有一次 key 先、一次 key 后；但第 2→3 轮跨越约 **98 分钟**中断。前半段 key 均零、后半段 key 均高，分组前后表现逆转可能受线路或服务状态变化影响，原因尚未确定。

## 附带的连接核查

为判断失败能否恢复，只发送无认证的 `/v1/models` HEAD 请求，没有调用模型，没有执行集群操作。SDK 为 OpenAI 3.11.0，实际 HTTP 库为 httpx2 2.12.0。离线查看客户端确认其通过 Windows 系统代理发现机制使用 `http://127.0.0.1:7897`；代理环境变量为空并不代表直连。

默认 curl 一次获得 HTTP 404、TLS 校验成功，默认 Python urllib 一次 SSL EOF。两者默认线路可能不同，这一对不能称为同线路对照。随后 curl 显式使用上述代理获得 HTTP 404、TLS 校验成功；Python 在系统代理线路获得 HTTP 403，说明这次 TLS 已建立。HEAD 的 HTTP 状态不证明认证模型 POST 正常，也不证明代理导致了 EOF。

实际 SDK connect/read 超时均为 90 秒；约 5 秒 SSL EOF 不能解释为本地配置了 5 秒 connect timeout。连接池 keepalive_expiry 为 5 秒、测试暂停 15 秒，通常会重新握手，这只能解释为何会再次暴露连接问题，不能确定根因。证书与主机名校验始终开启。未调整代理、TLS、生产连接池或超时配置。

## 解释限制

首次不视为冷启动：其他组及其他流量可能已经暖了共享指令/工具前缀。不同 key 也不能证明完全隔离上游缓存或强制固定机器。主比较采用同模式同轮双方均有效的完整配对，并报告所有失败与未配对记录；按总 cached / 总 input、未缓存输入及每轮表现评估，不挑最好的一次。

四轮仅能说明本线路与时段的观察，不证明长期效果；只有后 3 轮都至少 90% 且配对表现一致，才视为值得进一步复测的候选，不凭一次 HTTP 200 或高命中就改生产默认。此次 replay 是候选，append 没有通过。OpenAI 当前说明 GPT-5.6 以后模型自动处理缓存路由，key 不是优化缓存的必需条件；第三方 relay 的参数接受不代表上游实际使用。[官方说明](https://developers.openai.com/api/docs/guides/prompt-caching)

此次保留正式 Agent 的默认调用方式，**不默认开启 key，也不宣称缓存已修复**。较合适的下一步是先定位并稳定当前代理/TLS 线路，再在连续短时间、同一进程内单独复测固定 key 的 append，保持两组先后交替，不用跨中断结果拼成连续测试。可随后扩大到接近真实长会话的合成规模，继续核对最终请求正文和原始 usage；只在重复结果支持时增加正式 adapter 的可选配置。流式、亲和 headers、TTL 等分别作为后续单变量，不能一起加入再归因于 key。没有联系中转站管理员。

隔离脚本 `.cache/cache-key/run.py` 依赖此前 `.cache/cache-encoding/run.py` 和 `.cache/cache-structure/run.py`；`.cache/cache-key/analyze.py` 离线重算完整配对和进程区段，均为本机诊断脚本。缓存相关离线回归本轮 **21 passed**，SDK MockTransport 对唯一变量与历史不变的检查通过，不是全量项目回归。生产代码及默认模型配置未因本实验修改。
