# 线路诊断、连续追加测试与可选网络模式

日期：2026-10-09，香港时间。接续[固定缓存 key 对照](CACHE_KEY_TEST_20261009.md)，目标是先检查连接，再在连续同进程内比较无 key 与固定 key。此次完成了线路诊断和一个可选客户端配置；模型实验两次都因连接或读取失败停止，**没有完整缓存配对，不能宣布命中率改善或问题已修复**。

## 本轮结果

| 检查 | 实际结果 | 能说明什么 |
|---|---|---|
| 同代理 Windows curl / Python，各 4 次无认证 GET | 8/8 HTTP 401 | 当前短时 TLS 可通；不证明模型 POST 正常 |
| 正式 SDK 底层客户端，2 次无认证 GET | 2/2 HTTP 401，TLS 1.3 | 原 CA / 系统代理配置下可收到响应 |
| SDK shared / fresh 客户端，8 次无认证 GET | 7 次 HTTP 401；1 次新握手 SSL EOF | 小请求也能复现握手失败，不能只归因于模型或 key |
| 相同严格 SSLContext，代理 / 无应用代理，各 4 次新连接 GET | 代理 3/4 HTTP 401，1 次 SSL EOF；无应用代理 4/4 HTTP 401 | 代理路径再次失败；无应用代理仅本组未复现 |
| 原线路连续 append，预设最多 8 个模型请求 | 尝试 2 次，1 个有效响应，0 完整配对 | 第二次 RemoteProtocolError 后停止，不恢复 |
| 可选 direct 正式调用路径，预设最多 4 个模型请求 | 尝试 1 次，读取超时，0 完整配对 | direct 的模型请求尚未验证成功，立即停止 |

全部 26 次 GET 没有认证信息、没有模型推理；不能把预期 HTTP 401 当作模型 API 或缓存通过。本轮收费模型接口共 **3 次尝试、1 次有效响应、2 次失败**；失败用量及账单未知，不记为缓存零。没有补轮次或扩大调用预算，没有联系中转站管理员。

## 代理与握手证据

端口 127.0.0.1:7897 的监听进程确认为 Clash Verge 的 verge-mihomo。SDK 会使用 Windows 系统代理发现，代理环境变量为空并不代表直连。

SDK 复用诊断对每次 GET 完整读取正文，再暂停 2 秒；trace 只保存事件名、时间和异常类型，不保存认证头、请求内容或 trace 原始信息。失败请求已经完成本地代理 CONNECT，随后在 `proxy.start_tls.failed` 发生 ConnectError → SSLEOFError，约 5.015 秒。另一对请求的本机连接/TLS 对象 hash 一致、没有新握手事件，正常收到 401，说明实际复用至少在这一对成功。

原定 shared/fresh 交错顺序导致部分 shared 请求间隔超过默认 5 秒保活期，所以四次 shared 中仅一对确认为实际复用，不能把四次全称作复用成功。没有根据这次结果改变保活默认值。

读取本机代理日志时仅提取目标域名的时间和安全错误标志，节点/组标签只存 hash，没有保存原始日志、其他访问内容或代理凭据。前一轮三次失败与日志时间高度吻合：

| 旧 SDK 尝试 | 失败预计结束时间，香港时间 | Clash 目标域名连接错误时间 | 差值 |
|---|---|---|---:|
| R0 / 2，序号 8 | 19:45:22.884 | 19:45:22.870 | 约 14 毫秒 |
| R1 / 3，序号 10 | 19:48:50.084 | 19:48:50.071 | 约 13 毫秒 |
| R0 / 3，序号 12 | 19:55:05.213 | 19:55:05.200 | 约 13 毫秒 |

对应日志均含 `connect error`、`dial tcp`、`i/o timeout`。这支持这些 SSL EOF 与代理出站连接超时相关，不能单独确定节点、出口、中间设备或目标边缘的具体责任；也不能用它解释已经成功的 HTTP 200 为何返回缓存零。

代理/无应用代理对照使用同一个由实际 HTTP 库生成的严格 SSLContext，保留原 CA 环境；两组请求、HTTP/1.1 和 headers 一致，只改变应用代理选择，交替顺序各四次、无重试。TLS 与主机名校验均开启。不同线路仍可能有不同 DNS、出口及系统 TUN 路由，四次无应用代理成功不能证明长期稳定或 Clash 程序有 bug。

## 连续追加模型实验

使用原 relay / https://verysadai.com/v1 / gpt-6-astra / Responses，只发送合成数据：40,000 ASCII 字符笔记、固定 54 个工具定义、固定指令；权限状态仅初始化一次，后续只追加预定义 assistant 短文本和 user 消息。工具执行被独立阻止。每轮两组唯一 JSON 差异为是否添加同一固定 prompt_cache_key；key 只记录 hash。非流式、输出上限 1,024、SDK / model 重试均 0、阶段超时 90 秒、间隔 2 秒、自动压缩关闭。

原线路实验香港时间 21:39:15–21:39:45，先无 key 再有 key：

| 请求 | 实际响应 | 用量 / 耗时 |
|---|---|---|
| A0，第 1 轮 | HTTP 200，原始用量与 SDK 一致 | 输入 21,586，缓存 8,960，输出 8；41.51%；8.469 秒 |
| A1，第 1 轮 | APIConnectionError → RemoteProtocolError，无 HTTP 响应 | 用量未知；19.266 秒 |

去掉 key 后两份待发送 JSON hash 一致、历史 hash 一致；但只有一份有效响应，没有完成用量配对。RemoteProtocolError 可能是响应前 EOF 或非法/不完整 HTTP 数据，类型链不能证明 key 被拒绝、请求完整到达上游或未计费。只有第一个响应有 HTTP/1.1、TLS 1.3 和连接对象观测，不能证明失败请求实际复用了哪条连接。

隔离 direct 验证原定最多四次、两轮顺序 A0/A1/A1/A0，实际只尝试第一条 A0。它调用正式 `create_client` 的 opt-in direct 路径，在香港时间约 22:19:13–22:21:16 发生 APITimeoutError → ReadTimeout，无 HTTP 响应、耗时 **122.609 秒**，随后停止。90 秒是分阶段超时，不是整个请求的总截止时间；请求钩子只观察到一次 POST，没有 SDK 自动重试。不能推断其中各阶段分别耗时多少，不能将本次 direct 结果称为模型线路修复。

没有再运行准备好的四次 POST 连接对照脚本，该脚本仅做了 MockTransport 验证。所有本轮模型输入均为合成数据，未上传真实会话，未调用 SSH、VASP 或 Slurm，没有计算作业。

## 已实现的可选网络模式

正式 Agent 新增 `network_mode`，默认 **auto** 保持现有行为；**direct** 只禁用这个模型 HTTP 客户端的应用代理发现，保留证书与主机名校验和既有 SSL_CERT_FILE / SSL_CERT_DIR。系统代理、SSH 和透明 VPN/TUN 未修改，不自动切服务商、不在线路之间自动回退。

支持 `models.toml` 的单个 profile、`VASP_AGENT_NETWORK_MODE` 环境/`.env`、`--network-mode`。优先级为 CLI → 已有环境变量/`.env` → profile → auto。已有环境优先于 `.env`。非法值、不安全 SSL context 或无法识别的 HTTP 栈立即失败；构造失败关闭自建客户端。`/profile` 清除上一接口的 CLI 网络覆盖，`/model` 保留当前模式。

```bash
# 离线查看本次配置，不证明联网成功
python ai_agent.py --profile relay --network-mode direct --check-config

# 显式选择无应用代理的启动方式；此次模型验证尚未通过
python ai_agent.py --profile relay --network-mode direct

# 显式恢复沿用现有环境/系统代理
python ai_agent.py --profile relay --network-mode auto
```

两个现有 profile 都仍是 `network_mode = "auto"`，没有默认启用 direct。`--check-config` 已实际验证显示 direct，密钥只显示 configured。24 个新参数化网络模式测试及相关配置、客户端、重连、超时和缓存测试合计 **138 passed**，不是全量项目回归；包含真实 SDK MockTransport 正文/认证/压缩协商不变、CA 环境保留、无代理 mounts、安全校验、非法值和 profile 切换。没有 Git 提交或改动真实密钥。

## 证据与接续

本机 `.cache/` 证据没有纳入 Git：

- `.cache/tls-diagnostics/comparison-1791552742594020100.json`：curl/Python 同代理八次 GET。
- `.cache/tls-diagnostics/sdk-preflight-1791552869876349700.json`：正式 SDK 两次 GET。
- `.cache/tls-diagnostics/sdk-reuse-20261009T135453748177Z.json`：复用/新握手八次 trace。
- `.cache/tls-diagnostics/route-comparison-20261009T140248754543Z.json`：代理/无应用代理八次。
- `.cache/tls-diagnostics/clash-target-logs-20261009.json`：安全的目标日志标志。
- `.cache/cache-append-continuous/20261009T133914142677Z/matrix.json`：原线路模型尝试两次。
- `.cache/cache-append-continuous/direct-20261009T141912555863Z/matrix.json`：正式 direct 路径尝试一次。
- `.cache/tls-diagnostics/post_connection_probe_20261009.py`：仅离线验证的阶段 trace 诊断。

目前需要一个可用的替代线路或固定节点，才能继续连续缓存对照；已经询问使用者是否有其他 Clash 节点或本地端口。不要用跨中断的结果补成连续配对，不把 GET 成功当模型请求成功，不继续在失败线路上反复消耗模型请求。另一个节点可用后，先有限无认证检验，再进行明确定义的模型对照；不得在没有新证据的情况下把 direct 设为默认或宣称缓存修复。
