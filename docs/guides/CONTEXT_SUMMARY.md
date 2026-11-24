# 结构化摘要与测试

本次实测与故障修复记录见[2026-10-08测试报告](../archive/2026-10/CONTEXT_SUMMARY_TEST_20261008.md)。

2026-10-08 起，默认使用普通模型请求生成结构化文字摘要。Responses 和 Chat Completions 都支持；不依赖 `/responses/compact`。服务商、模型和协议仍使用当前 profile，失败不切换接口或服务商。

## 配置与使用

在 `models.toml` 对应 profile 添加：

```toml
compaction_mode = "summary"
recent_context_tokens = 12000
```

代码默认值相同；重启 Agent 生效，不需要安装新依赖或部署集群。CLI 参数优先于已有环境变量、`.env` 和 profile：

```bash
python ai_agent.py --profile relay --compaction-mode summary
# 仅在已确认服务商支持该接口时显式选择原生模式
python ai_agent.py --profile relay --compaction-mode native
```

环境变量为 `VASP_AGENT_COMPACTION_MODE`、`VASP_AGENT_RECENT_CONTEXT_TOKENS`。原生模式仅支持 Responses。切换模式从完整本地历史重建相应缓存，不尝试解析加密状态，也不删除会话。已有文字摘要缓存可继续使用，下一次压缩升级为结构化格式。

- `/context`：离线查看 `compaction_mode`（`text_summary` 或 `responses_native`）、占用、触发值、目标和实际近期保留预算。
- `/compact`：手动压缩；可总结的新增旧内容少于1024估算tokens时直接跳过，不请求模型（旧字符模式为3000字符）。失败保持原上下文，可查看失败原因。

## 触发、保留和验证

原有 `context_window`、`output_reserve`、`context_safety`、`compact_trigger` 和 `compact_target` 继续生效。自动触发前计算历史、指令与工具定义的估算占用；可用输入预算等于总窗口减输出及安全预留。默认触发比例为 0.8，目标为 0.55。估算采用 UTF-8 字节数与历史 API 用量校正，不是精确 tokenizer 计数，也不证明服务商容量。

近期完整轮次按预算保留，不固定为四轮。有效预算取 `recent_context_tokens` 与“压缩目标扣除当前指令/工具开销及4000 token摘要预留”两者较小值，最低为0；始终至少保留最新一轮。超大单轮可能超过此预算，程序不会切开它的工具调用与结果。压缩后仍超总输入预算时，主请求停止并提示处理，不强行丢消息。

摘要固定七个字段：目标 `goal`、约束 `constraints`、用户选择 `decisions`、带来源与适用条件的证据 `evidence`、进度 `progress`、待办 `pending`、风险 `risks`。它是历史资料；当前物理确认、参数方案、提交权限、真实作业状态仍以程序记录和工具查询为准。格式校验无法保证模型没有遗漏或错误概括，重要计算仍须重新核验权威记录。

代码会验证非空 JSON、七个字符串数组字段、长度、响应完成状态、没有工具调用，以及压缩幅度。自动摘要至少减少10%当前估算占用；手动压缩须实际变小。验证通过才原子替换缓存。原始 `.ai-session-*.json` 完整保留；摘要及源历史都有哈希校验。大型工具输出保留在 `.context-evidence`，可通过证据读取工具回查。

成功后的小幅追加不会立即再压缩：沿用至少3个新轮次或显著增长的间隔策略；即使超过压缩触发阈值，也不每轮付费总结。失败同样退避，`/compact` 可手动重试。网络重试次数继续由 `--model-retries` 控制。

超长历史分批生成累积摘要；每批最多32000估算输入tokens，且不得超过配置的输入预算。测量包含摘要指令、前批摘要、JSON包装与本批片段。分批结果验证后保存，后续重试可以复用匹配批次。旧字符模式仍以18000字符分批、保留2至4轮。

## 压缩测试

```bash
# 离线预览，不调用接口
python examples/probe_summary.py --profile relay --dry-run

# 真实摘要测试；只发送合成 VASP 历史
python examples/probe_summary.py --profile relay --timeout 90 --retries 1

# 更小输入的连接对照，保留同样关键事实；延长等待并关闭重试
python examples/probe_summary.py --profile relay --diagnostic-lines 0 --timeout 180 --retries 0
```

测试在独立 `.cache/summary-probe/<时间>/` 中运行，保存 `report.md`、`report.json`、`summary.json`（成功时）和合成原始记录。测试显式使用 summary，不改变当前 models.toml 或其他对话；最多6次实际请求，每次超时90秒，单次故障最多重试1次。会产生正常模型 API 用量。

检查点包括：固定 ENCUT=676 eV、已选12³/14³/16³、NBANDS=16、收敛证据来源和适用条件、prepared与未提交的区别、FixZ限制、只读待办、近期工具配对、原记录与权限不变、短追问不重复压缩。字段检查是针对固定场景的辅助判定，还应人工阅读 `summary.json`；不能据此证明所有长对话都无损。

普通请求发生TLS/502故障仍可能失败；改用文字摘要解决的是对原生压缩接口的依赖，不是网络线路问题。失败报告会保留具体分类，不把连接故障认作模型容量限制。原生容量诊断仍使用独立的 [probe_compaction.py](../../examples/probe_compaction.py)，两者测试目的不同。

## 本机HTTP解压兼容性

本机实测发现 `httpx2` 调用 Brotli 解码器时使用 `output_buffer_limit` 参数，而已安装的 `brotli 1.0.9` 不接受关键字参数；离线压缩/解压样例可复现 `TypeError: process() takes no keyword arguments`。SDK会把此异常包装成 APIConnectionError，因此它可能掩盖服务端实际响应。

交互客户端与两个测试入口统一通过 `models/api_client.py` 请求 `Accept-Encoding: gzip, deflate`，采用标准库兼容解码器，不改动系统依赖、不更改模型/线路、不关闭TLS证书校验。SDK回归测试验证真实gzip响应可读取。这个修复不意味着之前所有TLS中断或502都由Brotli导致，后续以实际返回结果为准。

## 跨模型兼容的压缩策略（2026-10-12）

文字摘要模式不依赖任何供应商专有接口，按三层保证"总能压缩"：

1. **请求层（按序尝试）**
   - 先发普通摘要请求；若 profile 设了 `summary_reasoning_effort`，按该档位发送。
   - 若响应被 `max_output_tokens` 截断且**只有推理、没有正文**（思考模型把额度全用于推理），改用 `reasoning.effort=none`（Chat 协议为 `reasoning_effort`）重试一次。
   - 若接口拒绝推理参数（HTTP 400/422），去掉该参数重发，并记为"不支持"。
   - 若截断但已有正文，把本批历史减半再试（最小 2000 字符），后续批次沿用较小尺寸。
   - 学到的结果按接口身份写入工作区 `.model-capabilities.json`（已加入 .gitignore），下次启动直接使用；配置项永远优先。
2. **解析层（宽松规整）**：去掉 Markdown 围栏和前后说明文字；中文/同义字段名（目标、约束、决策、证据、进展、待办、风险等）映射到标准字段；字符串值转为列表；缺失字段补空；未知字段并入 evidence。至少识别出 4 个标准字段才接受；出现 `tool_call`、`action`、`command` 等指令类字段直接拒绝。
3. **兜底层（机械压缩）**：模型摘要全部失败时，按规则提取用户要求、AI 回答首句、每个工具调用的结果（成功/失败、错误码、job_id、状态），生成同一格式的摘要并标注"机械压缩，细节以原始记录为准"。终端显示"上下文压缩完成（机械压缩）"，错误日志记 `fallback: mechanical`。摘要探针（`summary_probe`）测的是模型本身能力，因此关闭兜底。

配置示例（`models.toml` 中对应 profile）：

```toml
summary_reasoning_effort = "none"   # none / minimal / low / medium / high；也可用 VASP_AGENT_SUMMARY_REASONING_EFFORT
```

实测：DeepSeek（思考开启）对 28k 输入的摘要把 8000 输出 token 全部用于推理、正文为 0；同一批关闭推理后一次完成（推理 0、输出 3314 token、摘要 7.6k 字符且格式有效）。真实会话离线机械压缩：约 18.2 万 → 9.3k 估算 tokens。

## 续写式摘要（Claude Code 风格，2026-10-12 起为默认首选）

文字摘要模式现在先用"续写式"摘要，再依次回退到分批 JSON 摘要与机械压缩：

- **请求方式**：把当前上下文原样作为消息历史发送，系统指令和工具定义与普通对话完全相同，末尾追加一条交接请求；`tool_choice=none` 禁止调用工具。前缀与平时一致，因此提示缓存可命中几乎全部输入。接口不接受 `tool_choice` 时自动去掉工具定义重发并记住（缓存命中会下降，但仍可用）。
- **输出格式**：先 `<analysis>`（≤500 字要点），再 `<summary>`（Markdown，约 6000 字以内）分 9 节：主要请求与意图、物理模型与约束、任务/文件与参数、真实结果与证据、错误与修复、用户的全部消息、待办、当前工作、下一步（须引用用户原话，否则写"等待用户指示"）。程序只取 `<summary>`；缺结束标签、缺开始标签都能容忍。不要求 JSON，对模型的格式遵从要求低。
- **保留最近轮次**：最近几轮仍原文保留（比 Claude Code 更保守），只摘要更早的部分。
- **回退规则**：内容类失败（上下文超过单次请求容量、无摘要、被截断、含工具调用）→ 分批 JSON 摘要；网络/超时失败 → 直接机械压缩（换摘要方式也会同样失败）。
- **`/compact 重点要求`**：附加说明会写进交接请求，例如 `/compact 重点保留 MoS2 的网格选择`。

实测（DeepSeek，真实会话，关闭推理）：输入 123,413 tokens，其中 **122,880 命中缓存（99.6%）**；`tool_choice=none` 被接受，56 个工具定义在场但未调用；输出 6,090 tokens，9 节齐全，job_id、体积变化与复核差值准确。旧的分批 JSON 方式首次请求仅命中 128 tokens。首次尝试曾因"详细分析+详细摘要"超过 8000 输出上限被截断，已在提示中限定篇幅。
