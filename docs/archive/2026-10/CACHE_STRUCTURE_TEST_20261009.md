# 缓存请求结构对照实测：2026-10-09

## 结论

**缓存读取已在正式 Agent 请求路径出现，但尚未稳定；本轮没有修复生产代码，也不能宣布真实用户会话已恢复高命中。** 最有区分力的结果是：完全相同的 Agent HTTP 请求正文，两次均输入 9,334 tokens，第一次返回 9,216 缓存读取（98.74%），约 33.4 秒后第二次返回 0。

这对请求没有追加历史、没有变更工具、指令或输出限额，使用同一普通 SDK 调用方式。原始 HTTP JSON 与本地统计一致。因而，该对照中的差异不能由本地历史改写、不同角色布局或丢失缓存用量字段解释。

需继续核对服务端缓存保留、缓存写入、上游路由及用量转发，**尚未证明其中某一项是根因**。SDK raw-response 请求头、非流式、完整工具目录或输入不足也没有被本轮证据确认为原因。

## 运行范围与方法

- 时间：香港时间 16:01:35–16:11:45（UTC 08:01:35–08:11:45）。
- 配置：`relay`，`https://verysadai.com/v1`，`gpt-6-astra`，Responses，非流式；不切换协议或服务商。
- 每组最多两次请求，输出上限 1024 tokens，超时 45 秒，SDK 和 Agent 请求重试均为 0；某组失败立即停止该组。
- 共 21 次请求尝试：20 次 HTTP 200，1 次读取超时、未取得 HTTP 响应或用量。成功请求报告合计 321,730 输入、23,424 缓存读取 tokens；失败请求是否产生服务商费用未知。跨不同实验组的合计不能当作正式会话性能基准。
- 使用 600 行无私密信息的合成资料；单长消息与多角色布局保留相同资料。工具目录对照为 1 个虚拟工具和完整 54 工具，后者会增加总输入 tokens。
- 正式 Agent 补充验证在独立合成工作区执行 `Agent.ask`，复用生产指令、工具目录、权限状态和普通 `responses.create`。输出限额固定，自动压缩关闭，避免摘要请求混入对照。
- `execute_tool` 与 `Toolbox.call` 均由测试代码强制阻断。工具执行次数 0；没有 SSH、部署、VASP 或 Slurm 操作。
- 只保存请求正文哈希、长度、有限安全请求头、请求 ID 和用量，不保存认证头、真实私密对话或密钥。

最初的 raw 单工具请求读取超时，随后单独运行普通调用及其他组。额外低频对照每轮间隔 30 秒，并复现此前成功 probe 的原始输入。所有组共享合成语料，首次请求可能复用已有缓存，不能视为冷缓存测试。

## 实际结果

以下百分比均来自实际输入及缓存读取，0 为接口明确返回零。

| 组别 | 调用方式 / 历史 | 两轮输入 tokens | 两轮缓存读取 tokens | 两轮命中率 |
|---|---|---:|---:|---:|
| 单工具、长消息 | raw wrapper / replay | 首轮无用量 | 未报告 | 未报告；46.25 秒读取超时，停止 |
| 单工具、长消息 | 普通 create / replay | 14,617 / 14,617 | 0 / 0 | 0 / 0 |
| 54 工具、长消息 | raw wrapper / replay | 19,939 / 19,939 | 0 / 0 | 0 / 0 |
| 54 工具、多角色 | raw wrapper / replay | 19,963 / 19,963 | 0 / 0 | 0 / 0 |
| 54 工具、多角色 | raw wrapper / append | 19,963 / 20,003 | 0 / 0 | 0 / 0 |
| 54 工具、长消息 | 普通 create / replay | 19,939 / 19,939 | 0 / 0 | 0 / 0 |
| 正式 Agent 小会话 | 普通 create / append | 9,334 / 9,388 | 5,248 / 0 | 56.22% / 0 |
| 正式 Agent 大合成会话 | 普通 create / append | 23,748 / 23,802 | 8,960 / 0 | 37.73% / 0 |
| 原成功 probe，30 秒间隔 | raw wrapper / replay | 14,593 / 14,593 | 0 / 0 | 0 / 0 |
| 正式 Agent 首次请求，30 秒间隔 | 普通 create / 精确 replay | 9,334 / 9,334 | 9,216 / 0 | **98.74% / 0** |
| 正式 Agent 小会话复测，30 秒间隔 | 普通 create / append | 9,334 / 9,388 | 0 / 0 | 0 / 0 |

此前香港时间约 14:53 的原 probe 曾命中 99.12%，本轮相同原 probe 的两次请求却均为 0。不能再将原 probe 的一次成功当作稳定的正对照。

## 最强对照与可用于对账的请求 ID

目录：`.cache/cache-structure/quiet-20261009-controlled/agent_small_replay_direct/`。

两次普通 create 请求的实际 HTTP 正文 SHA-256 完全相同：

```text
ab9704bcdfb9d464a85e9741c1b4d1a651d12392be0250329dd99c25bc4dc2fe
```

| 香港请求时间 | HTTP | 输入 | 缓存读取 | 请求 ID |
|---|---:|---:|---:|---|
| 16:10:05.762 | 200 | 9,334 | 9,216 | `d20102a8-6f3c-4fda-bfd1-633aae917dcd` |
| 16:10:39.154 | 200 | 9,334 | 0 | `2fea09d4-ed25-43d2-8758-15321b11ba1e` |

两次均没有 `x-stainless-raw-response`，`x-stainless-read-timeout` 均为 45。实际 Agent 小会话首轮的正文哈希也与上述相同；该较早请求曾读取 5,248 缓存 tokens。这进一步显示相同请求的返回用量会变化，不能据首轮命中推断下一轮必然命中。

## 本地审计与结论边界

1. 正式 Agent small/large 四个请求均已从合成工作区离线重建，正文哈希与现场记录逐一匹配。第二轮仅追加 assistant message 和 user，旧 input、指令、模型、工具和设置不变；没有 reasoning 或 encrypted_content 项。
2. 54 工具的 raw/普通调用两组正文哈希相同，四次均零命中。SDK raw wrapper 额外发送 `x-stainless-raw-response: true`，普通调用不发送。当前没有证据支持该头是缓存根因。
3. SDK/本地缓存数与 HTTP JSON 一致，没有将缺失字段补成零。所有成功响应的缓存写入字段为 0；仅此不能证明上游从未建立缓存。
4. 约 30 秒间隔的精确 replay 也出现高命中后归零，降低本测试频率没有使它稳定。未观测账户其他请求、上游节点或账号，不据此排除服务端负载或路由影响。
5. 模型和 service tier 标识稳定，不能证明服务商使用同一个上游账号、节点或相同缓存策略。
6. 正式用户 `test_agent` 未用于发请求，也未修改；本轮是正式代码路径的合成验证，不能替代真实会话稳定性验收。

官方说明缓存依赖可复用前缀，命中也与服务器缓存位置和路由有关，见[OpenAI Prompt Caching](https://developers.openai.com/api/docs/guides/prompt-caching)。此说明仅支持后续核对方向，不能确定这个中转站的实现或故障原因。

## 证据与复现

本机结果目录（均位于项目根目录）：

```text
.cache/cache-structure/structure-20261009-controlled/matrix.json
.cache/cache-structure/ordinary-20261009-controlled/matrix.json
.cache/cache-structure/remaining-20261009-controlled/matrix.json
.cache/cache-structure/quiet-20261009-controlled/matrix.json
```

各子组保留 `report.json`、`http-events.jsonl` 和缓存请求诊断。隔离 Agent 的会话仅为本轮生成的合成内容。测试脚本 `.cache/cache-structure/run.py` 是本机诊断文件，未并入生产实现或 Git。

离线复现检查：

```powershell
python -X utf8 .cache/cache-structure/run.py --dry-run
```

下面会再次产生两次真实模型请求，不应当成离线检查；输出目录需要更换，避免覆盖已有证据：

```powershell
python -X utf8 .cache/cache-structure/run.py --cases agent_small_replay_direct --pause-seconds 30 --nonce 6ebb8e7d14d93fe5 --output-dir .cache/cache-structure/new-replay
```

离线测试：`test_prompt_cache.py`、`test_cache_probe.py`、`test_cache_probe_transport.py`、`test_api_client.py` 合计 **22 passed**；合成语料一致性及双工具执行阻断也已检查。没有运行全量回归，生产代码未改动。

下一步优先用上面同正文、一命中一归零的请求 ID 向服务商核对缓存节点/账号、缓存保留和写入策略、实际输入前缀，以及用量是否直接透传。尚未对外发送任何消息；本轮停止增加付费样本。服务端行为明确后，再决定是否需要缓存标识、明确断点或其他请求改造，并用真实 Agent 场景验收。
