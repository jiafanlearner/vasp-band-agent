# 原生压缩分档测试

在项目根目录运行，使用当前 `models.toml` 和 `.env` 配置，不读取或修改真实会话、不执行 VASP 或 SSH 工具。

```bash
# 离线检查测试档位，不发送请求
python examples/probe_compaction.py --profile relay --dry-run

# 从8k到258k，每档两次，第二轮倒序，避免把时间波动误判为容量
python examples/probe_compaction.py --profile relay

# 对观察到的问题区间增加重复次数
python examples/probe_compaction.py --profile relay --sizes 32k,64k,96k,128k --repeats 3

# 去掉合成工具调用，只测试用户/助理文本
python examples/probe_compaction.py --profile relay --content messages --sizes 8k,32k,64k --repeats 2
```

测试可能产生API费用。默认请求上限24次、累积估算输入上限300万；`--max-requests`、`--max-input-estimate` 可降低预算。默认每次请求间隔20秒，失败后至少等待60秒，尊重 `Retry-After`；服务端要求等待超过180秒时停止。SDK自动重试关闭，每次失败都进入结果。429、502或连接失败后用8k小请求作对照；小请求也失败就停止，不继续增加上下文制造限流。

测试期间先停止同一Key下的 `/compact` 自动重试及其他模型请求，减少并发限流造成的干扰。仍然可能受到中转站共享上游额度的影响，脚本无法保证完全隔离服务端负载。

默认档位为8k、32k、64k、128k、192k、224k、240k、258k，**k=1000，档位是项目UTF-8字节/3的估算，不是模型实际token上限**。原生压缩发送的内容是指令和旧历史，并不包含正式对话请求里的完整工具定义。成功响应的 `usage.input_tokens` 单独记录为API实际输入；失败响应通常没有usage，不能凭估算精确推断硬上限。测试用随机种子生成多轮数值日志和成对工具记录，避免用高度重复文本代替真实长上下文。

结果默认保存在 `.cache/compaction-probe/<时间>/`，包含 `results.json`、`attempts.csv`、`report.md`。可用 `--output-dir` 指定新的输出目录。Ctrl+C保存部分结果，不覆盖之前的实验，不保存加密压缩正文或密钥。完整压缩响应必须通过项目现有格式校验才记为通过。

加密压缩状态的字符串长度不是模型token数；脚本仅记录返回JSON字节数和API报告的输出tokens，不据此推算加密状态的实际上下文占用。

只有明确的 `context_length_exceeded` 等错误代码才归为上下文限制。HTTP 413单独记为请求体限制；429记为限流，502记为网关/上游失败。一个档位重复测试全通过仍不保证以后永远成功；如果未出现明确上下文限制，报告会写“未证实容量边界”。测试不会自动修改 `context_window` 或压缩阈值。

依据：[OpenAI Compaction](https://developers.openai.com/api/docs/guides/compaction)、[Rate limits](https://developers.openai.com/api/docs/guides/rate-limits)。官方要求压缩输入本身仍处于模型上下文窗口内；中转站路由、限流与实际容量需要实测。
