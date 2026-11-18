# 固定缓存键重放：准备完成，连接预检失败

日期：2026-10-10，香港时间。接续[无 key 精确重放](CACHE_ACCOUNTING_REPLAY_20261010.md)。**固定 key 的真实缓存效果尚未测试**：本轮无认证连接预检第一条就发生 TLS EOF，按失败即停规则未发送模型请求。离线模拟已通过，不能把模拟中的缓存零当作真实结果。

## 本轮实际状态

| 项目 | 实际执行 | 结果 |
|---|---:|---|
| 无认证 SDK GET 连接预检 | 1 次 | 约 5 秒后 ConnectError → ConnectError → SSLEOFError，无 HTTP 响应 |
| 固定 key 的模型 POST | **0 次** | 未开始；输入量、缓存量和缓存率均未测量 |
| 本地 SDK MockTransport | 6 次模拟 HTTP | 四次固定正文主流程及缺 usage 停止场景通过 |

预检香港时间 10:15:23.221–10:15:29.632，保持 relay / https://verysadai.com/v1 / gpt-6-astra / Responses / `network_mode=auto` 和现有系统代理。预检使用假密钥的正式客户端底层传输、不携带 Authorization、不加载真实 API 密钥，TLS/主机名校验开启，最多两次 GET、无重试/重定向。第一条失败后没有执行第二条，也没有通过其他线路回退或补跑模型。

本次记录不能证明具体代理节点、远端边缘或中间设备责任；没有额外采集本次代理 trace/日志。约 5 秒发生的 SSLEOFError 也不能直接证明客户端连接超时配置只有 5 秒。**未发送带 key 的 POST，因此不能解释为 key 被拒绝、key 缓存无效或命中率为零。**

## 已准备的测试

新隔离脚本 `.cache/cache-key-replay-20261010/run.py` 沿用先前合成 fixture：40,000 ASCII 字符笔记、54 个工具、固定指令、一次权限初始化，工具执行被阻止。只重放历史，不追加回复；缓存键沿用原合成会话 key，每次实际出站前核验：

- 与旧 A1 相同的完整 bytes SHA-256 和 **86,438 bytes** 长度。
- 相同 key 的 SHA-256、history/instructions/tools hash。
- 移除 key 后，与旧 A0 的内容 hash 相同。

旧 A1 完整正文 hash：

```text
48c2cb0ac66884409ae33824e94af76b8c3a69849ae083519d80d359823107f8
```

计划为同进程、同一客户端、同一个 key 的**最多四次实际 HTTP POST**；重复、重定向、第五次请求、正文或 key 变化在发送前阻断。保持非流式、`store=false`、既有 include/parallel_tool_calls 选项，输出上限 1,024，阶段超时 90 秒、间隔 2 秒、自动压缩关闭，SDK/model 重试均 0，无恢复/线路回退。缺 usage、输出不符、传输或其他控制失败则停止；输入计量不同预先规定为测量结果，不因此停止。

离线验证使用假密钥和 SDK MockTransport，不解析真实环境密钥：四次 body/key 精确同旧 A1、输入计量故意变化时仍完成四次；缺 usage 场景在第二次停止，未知输入/缓存保留 `null`。额外、重复、重定向、正文或 key 改动均发送前阻断。共 **6 次模拟请求**。离线 `completed=true` 仅是诊断脚本行为通过，不是 API 缓存验收。

## 继续条件与结论边界

先取得新的短时连接成功证据，再在新批次内运行四次固定 key 模型重放。以下为已准备命令，**本轮没有执行 live**；当前仍应先处理连接问题。

```powershell
python -B -X utf8 .cache/cache-key-replay-20261010/run.py --profile relay --expected-base-url https://verysadai.com/v1 --expected-model gpt-6-astra --expected-network-mode auto
```

它仍依赖本机被 Git 忽略的 `.cache/` helpers，不能当成已公开的项目 CLI 功能。代码本身有四请求上限，执行者还须遵守预检失败就不启动模型的流程。

未来若四次高命中，只证明该批可重复命中；若固定 key 下仍归零，则它在该批未保证稳定。与前批无 key 的 24.8827% 只能作描述性比较，不能把不同时间、已有暖缓存和未知上游状态下的两批结果当作 key 的因果效果。尚未默认启用 key，未改变模型、协议、流式、推理强度或保留设置。

本轮没有模型请求、真实会话上传、SSH/VASP/Slurm、计算提交、真实 `.env`/密钥/代理设置变更或管理员联系。生产代码未改、旧证据未改，没有运行全量项目回归。

## 本机证据

- `.cache/tls-diagnostics/accounting-preflight-20261010T021523220724Z.json`：本轮第一条无认证 GET 失败。
- `.cache/cache-key-replay-20261010/20261010T021526197865Z/matrix.json` 及 `offline-missing-usage/`：六次 SDK Mock 模拟的记录。
- `.cache/cache-key-replay-20261010/preflight-blocked-20261010T021523220724Z/status.json`：明确记录模型未开始、实际模型请求 0、缓存指标 `null`；同目录 `prepared-run.py` 为准备好的脚本快照。
- `.cache/cache-key-replay-20261010/run.py`、`record_preflight_stop.py`：隔离验证及只读证据整理脚本；没有保存真实密钥、认证头、模型正文或真实会话。

此前两批 10 月 10 日模型记录仍合计六次，本轮没有增加；未将模拟数据混入真实缓存统计。
