# DeepSeek 官方配置与缓存测试

日期：2026-10-10，香港时间。用户授权使用 DeepSeek 官方 API 配置并测试。本页记录独立服务的验收，不把它作为原中转站缓存问题的根因结论。

## 配置

默认 profile 已改为 `deepseek`，`models.toml` 新增独立配置：

```toml
[profiles.deepseek]
base_url = "https://api.deepseek.com"
api_key_env = "DEEPSEEK_API_KEY"
model = "deepseek-flash"
protocol = "responses"
compaction_mode = "summary"
network_mode = "auto"
```

本机 `.env` 设置 `DEEPSEEK_API_KEY` 及 `VASP_AGENT_PROFILE=deepseek`，密钥不出现在本页、源码或测试报告中；`.env` 已经由 Git 忽略。原 `relay` 和 `official` 配置仍可通过 profile 切换。上下文预算保留保守的 240,000 本地配置，不等同于容量实测。

重启 Agent 后从项目根目录运行：

```bash
python ai_agent.py
```

或显式选择接口：

```bash
python ai_agent.py --profile deepseek
python ai_agent.py --profile relay
```

已有进程不会自动重新读取 `.env`。现有进程可用 `/profile deepseek` 切换，并按程序提示处理旧上下文；建议缓存验证采用新对话，避免混入其他模型的历史状态。

## 已验证的配置与模型发现

- `python ai_agent.py --check-config` 离线成功，默认 `deepseek / deepseek-flash / responses`，密钥只显示 `[configured]`。
- 合成缓存探针 dry-run 成功：600 条合成记录、replay 四轮、非流式、思考关闭、输出上限 1,024、SDK 重试 0。
- 香港时间 **10:54:03.004–10:54:05.603**，一次认证 GET `https://api.deepseek.com/models` 返回 HTTP 200，模型列表为 `deepseek-flash`、`deepseek-v4-pro`，配置模型确实存在。此请求不生成模型回复，不是缓存验收。

模型发现使用正式 `create_client`、当前 `network_mode=auto`，TLS 校验开启、超时 30 秒、实际 GET 硬限一次、无重试，不保存密钥、授权头或响应正文。证据 `.cache/deepseek-validation-20261010/models-20261010T025405602702Z.json`。

## 缓存测试规则

沿用生产 Responses adapter 和缓存解析路径，使用固定合成内容及不能执行的工具 schema；不上传真实对话、POSCAR、集群数据或凭据。单进程串行最多四次实际 POST，600 条合成记录，replay 不追加回复，预先固定间隔 3 秒，非流式、`reasoning.effort=none`、输出 1,024、阶段超时 90 秒、重试 0。最终出站正文必须四次同 bytes/hash，缺少 usage、SDK/raw 计数不一致、工具调用、输出截断、HTTP 或控制失败则停止，没有补轮次、恢复或换线路。

DeepSeek 自动管理缓存，Responses 目前不支持 `prompt_cache_key` / `prompt_cache_retention`、原生 `context_management`、`previous_response_id` 或 encrypted reasoning；它们不能作为本次缓存控制项。参考[官方 Responses 兼容说明](https://api-docs.deepseek.com/guides/responses_api/)及[思考模式参数](https://api-docs.deepseek.com/guides/thinking_mode/)。缓存构建需要时间且属于 best-effort，不以 100% 命中作为功能验收标准，见[官方缓存指南](https://api-docs.deepseek.com/guides/kv_cache/)。

缺失缓存字段为 `null`，显式 `cached_tokens=0` 才是零命中；加权命中率为有效响应的总缓存 tokens / 总输入 tokens。缓存写入未知时不记为零；缓存量属于输入量的一部分，不重复计入总量。不同服务商的模型、tokenizer、缓存实现都不同，不能把不同模型的 token 数或两批缓存率作为同一服务内的因果对照。

## 相同请求重放结果

香港时间 **11:01:07.658–11:01:21.065**，四次实际 POST 全部 HTTP 200，输出完整、未产生工具调用。最终请求正文四次均为 **73,397 bytes**，SHA-256 为 `287325d943549f4966fae37bcb68fbcb7570683d5093e0442d9180b0de417128`；SDK 与原始 HTTP JSON 输入量和缓存量逐条一致。

| 轮次 | 输入 tokens | 缓存 tokens | 命中率 | HTTP 耗时 |
|---|---:|---:|---:|---:|
| 1 | 14,732 | 0 | 0% | 0.922 秒 |
| 2 | 14,732 | 14,592 | 99.05% | 1.141 秒 |
| 3 | 14,732 | 14,592 | 99.05% | 0.531 秒 |
| 4 | 14,732 | 14,592 | 99.05% | 0.968 秒 |

总输入 **58,928**，其中缓存 **43,776**、未缓存 **15,152**；全批加权命中率 **74.2873%**，后三次为 **99.0497%**。输出合计 20，总 tokens 为 58,948，各条 input + output = total。缓存写入字段未报告，保留 `null`。

输入量在本批稳定，guard 实际 HTTP 4 / blocked 0，所有参数一致，只有首条出现连接握手事件。请求开始间隔约 4.038 / 4.152 / 3.548 秒，包括预定的 3 秒等待。公共探针的后续轮次 `elapsed_seconds` 包含等待；上表使用 `HTTP_events.elapsed_seconds`，不能把两者混为网络耗时。

这证明**本批 DeepSeek 官方、非思考模式、单个合成工具 schema 的 replay 能持续命中缓存**。不能据此宣布真实长对话、正式 VASP 工具目录、思考模式或原中转站问题已经通过验收。

证据 `.cache/deepseek-validation-20261010/20261010T030106175668Z/`：`report.json`、`http-events.jsonl`、`requests.jsonl` 和 `executed-run.py`。最新离线验证目录为 `20261010T025809455575Z/`，使用假密钥 Mock 主四次及缺 usage 第二次停止共六次，模拟结果不计入实测。执行脚本 SHA-256 为 `649b9d441449ad40db90fcbaf37de480bd6dd174729dccb6d7ba0750aebfa957`。

## 对话追加验证

香港时间 **11:25:49.083–11:26:01.489**，四轮合成 append 均 HTTP 200、完整响应、无工具调用。沿用同一 fixture 和模型，首条与上批 replay 首条正文 bytes/hash 完全一致，已经命中缓存；本批是暖缓存后的连续追加验证，不能作为冷启动对照。

| 轮次 | 输入 tokens | 缓存 tokens | 命中率 | HTTP 耗时 |
|---|---:|---:|---:|---:|
| 1 | 14,732 | 14,592 | 99.05% | 0.625 秒 |
| 2 | 14,772 | 14,592 | 98.78% | 0.547 秒 |
| 3 | 14,812 | 14,592 | 98.51% | 0.625 秒 |
| 4 | 14,852 | 14,592 | 98.25% | 0.781 秒 |

总输入 **59,168**，缓存 **58,368**，未缓存 **800**；加权命中率 **98.6479%**。输出合计 20，总 tokens 为 59,188，各条 input + output = total。SDK/raw 用量逐条一致，缓存写入未报告，仍为 `null`。

四条实际输入历史条数为 3 / 6 / 9 / 12；每轮保留上一轮实际出站 input 及通过核验的 SDK 输出，再追加权限快照和新用户消息。旧前缀与 SDK 输出保留检查、固定 instructions/tools/其他选项、返回模型匹配、请求守卫实际 4 / blocked 0 均通过。完整正文随正常追加变化，`identical_request_parameters=false` 不代表静态配置变化；`input_tokens_stable=false` 也符合本批对话增长方式。接口未报告 service tier，不能把未报告值恒定当作已核验真实服务等级。

实际出站正文 73,397 / 73,784 / 74,172 / 74,559 bytes，首次正文同 replay，后三轮只追加；只有首次出现握手事件。上表耗时只来自 HTTP events，不含每轮预定的 3 秒等待。每次缓存均为 14,592，说明旧的大段合成前缀被复用；本批不能证明每个新追加 token 都已经被缓存。

证据 `.cache/deepseek-validation-20261010/append-20261010T032547725602Z/`：`report.json`、`http-events.jsonl`、`requests.jsonl` 及执行脚本/依赖快照。执行脚本 SHA-256 为 `c6ada9543b4fdc3473df9de7fe0c9cf285ec352af7df2889c6680877dad03ce3`，replay 依赖 SHA 同上批。最新离线目录为 `append-20261010T032256261188Z/`，假密钥 Mock 主流程四次与缺 usage 第二次停止共六次，未计入真实测试。

本次合计 **一次模型列表 GET、八次实际模型 POST**，八条模型响应均有效，没有重试、补轮或换线路。配置与两种合成缓存场景通过；正式 VASP 工具目录、真实多轮工具返回、思考模式和大窗口容量仍未实测。本次测试关闭思考只用于诊断，没有改变正常 Agent 的思考默认行为，也没有运行服务器计算。
