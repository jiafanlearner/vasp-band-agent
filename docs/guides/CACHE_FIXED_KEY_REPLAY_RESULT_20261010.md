# 固定缓存键真实重放：首条请求被网关并发限制

日期：2026-10-10，香港时间。接续[固定 key 重放准备与连接阻断](CACHE_FIXED_KEY_REPLAY_20261010.md)。用户指出同一 VPN 下 Codex 缓存正常，要求继续测试。沿用现有线路，新的连接预检通过后启动真实固定 key 重放；**第一条模型请求返回 HTTP 429，错误码 `gateway_concurrency_limit`，本批立即停止，固定 key 的缓存效果仍未取得有效测量。**

## 实际执行结果

| 项目 | 实际执行 | 结果 |
|---|---:|---|
| 无认证 SDK GET `/models` 预检 | 2 次 | HTTP 401、TLS 1.3、响应完整；仅证明这两次连接可达 |
| 固定 key 模型 POST `/responses` | 1 次 | HTTP 429，`rate_limit_error / gateway_concurrency_limit` |
| 其余三条重放 | 0 次 | 按失败即停规则未发送，没有重试、恢复或回退 |
| 有效模型响应 | 0 个 | 没有 usage，输入量、缓存量、写入量与缓存率均为 `null` |

预检香港时间 **10:26:34.741–10:26:38.112**，两条耗时 0.875、0.219 秒。预检无 Authorization、未加载真实密钥、最多两次 GET、无重试/重定向、TLS 校验开启；401 是预期的无认证响应，不能用来证明模型调用或缓存正常。

真实模型批次 **10:27:00.913–10:27:32.964**，请求记录起于 **10:27:01.602**，调用耗时 **32.047 秒**。代理 CONNECT 和目标 TLS 握手完成，约 0.593 秒后目标 POST 正文发送完成，约 31.359 秒时收到完整 429 响应；不是没有 HTTP 响应的 TLS EOF 或客户端超时。SDK 错误链为 `RateLimitError → HTTPStatusError`。没有可用请求 ID 或 usage。

## 保持的控制条件

接口仍为 `relay / https://verysadai.com/v1 / gpt-6-astra / responses / network_mode=auto`，正式 `create_client` 路径和现有系统代理未改。一个进程、一个客户端、合成数据、工具执行被阻止，计划最多四次实际 POST；SDK/model 重试均为 0，无恢复、重定向补发或线路回退。

本次出站前守卫确认：

- 真实正文 **86,438 bytes**，SHA-256 为 `48c2cb0ac66884409ae33824e94af76b8c3a69849ae083519d80d359823107f8`，与旧 A1 完全相同。
- key 的 SHA-256 为 `ef6374f7a3652c0623719ef83ff996aa6c901c5ec8d84c0f365ee7e2c8ab6b95`，与原合成 key 相同；没有记录 key 本文。
- 去 key 后 SHA-256 为 `dc78ccadd43eef7d4fee3ca0e3c61fb819a7b6aa80188218ec0ffc5081098d9b`，与旧 A0 相同。
- history/instructions/54 个 tools 的 hash 不变；40,000 ASCII 合成笔记、固定权限、无 append、自动压缩关闭。
- 非流式、`store=false`、输出上限 1,024、阶段超时 90 秒、间隔 2 秒及其他既有选项未改。
- 脚本及直接 Evidence 依赖的 SHA 与此前离线验证相同，因此没有重复执行 Mock 验证。

实际请求守卫为 logical 1 / HTTP allowed 1 / blocked 0，事件日志仅一行，未发后续三条。matrix 中 `completed=false`、`input_tokens_stable=null`；`model_constant` 与 `tier_constant` 在没有有效响应时只是空集合判断，**不能写成已核验返回模型或服务等级一致**。

## 能判断什么

本批接口明确报告网关并发限制，不能把这次失败当作 VPN/TLS 中断。测试进程自身只发了一条请求，没有证据证明 Agent 在本批并发过高；响应不能区分其他请求占用、网关共享通道或具体上游限制，也不能据此宣称账户额度耗尽。

没有成功模型响应，因此不能把缓存率记为 0、不能与[无 key 四次重放的 24.8827%](CACHE_ACCOUNTING_REPLAY_20261010.md)比较，也不能证明 key 被上游采用或导致了拒绝。相同 VPN 下 Codex 缓存正常是有价值的对照，但无法单独证明两者使用相同上游副本或缓存路由。

本批结束，不恢复拼接或补足三条。后续模型接口可用时，再按新批次完整执行原有固定 key 重放；取得有效结果后，才适合继续对照 Codex 的请求选项，例如单独比较流式，避免同时改变 key、协议、推理设置和线路。没有因此默认启用 key，也没有联系管理员。

## 本机证据与变更

- `.cache/tls-diagnostics/accounting-preflight-20261010T022634741186Z.json`：两条预检完整 HTTP 响应。
- `.cache/cache-key-replay-20261010/20261010T022659556606Z/matrix.json`：完整安全字段、429 错误码、usage 缺失、控制及停止记录。
- 同目录 `http-events.jsonl`、`executed-run.py`、`evidence-dependency-run.py`：一条实际请求和当时执行代码快照。
- `.cache/cache-key-replay-20261010/run.py`：隔离脚本，依赖本机 Git 忽略的 helpers，不是发布的 CLI 功能。

本轮仅更新结果文档、导航和交接记录，生产代码及默认配置未改，旧 matrix/历史报告未改。没有真实会话上传、工具执行、SSH/VASP/Slurm、计算提交、密钥或代理设置修改、管理员联系，也没有重跑全量项目测试。
