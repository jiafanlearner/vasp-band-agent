# Python 与 curl 同代理连接对照（2026-10-08）

测试时间：香港时间 2026-10-08 15:23–15:24。

## 方法

- 目标统一为 `GET https://verysadai.com/v1/models`。
- 所有请求显式指定 `http://127.0.0.1:7897`，使用 HTTP/1.1、相同 User-Agent 和 Accept；不读取 `.env`，不携带 API 密钥，不发送真实会话或项目数据。
- 关闭环境代理的隐式选择、curl 配置文件及重定向，保持证书和主机名校验。每次创建新连接，无自动重试。
- Python 使用当前 SDK 对应的 httpx2 与 truststore；curl 分别使用 Windows 和 Git 自带版本。
- 第一批各模式两次，第二轮倒序；出现一次 curl 握手失败后，对 Windows curl 与 Python 默认 TLS 再交错各测四次。
- 两客户端使用相同代理入口；未读取或控制 Clash 内部规则，不能证明代理自动选择的出口节点在全部请求间完全相同。

## 环境

| 客户端 | 版本及 TLS 实现 |
|---|---|
| Python | 3.12.3，OpenSSL 3.6.2，httpx2 2.12.0，truststore 0.10.4 |
| Windows curl | 8.14.1，Schannel |
| Git curl | 8.19.0，Schannel |

## 实际结果

| 模式 | 收到 HTTP 401 | TLS/连接失败 | 说明 |
|---|---:|---:|---|
| Python 默认 TLS | 6/6 | 0 | 成功协商 TLS 1.3 |
| Windows curl 默认 TLS | 5/6 | 1 | 一次 curl 退出码 35，约 5 秒后握手失败 |
| Git curl 默认 TLS | 2/2 | 0 | 全部收到响应 |
| Python 仅 TLS 1.2 | 2/2 | 0 | 成功协商 TLS 1.2 |

HTTP 401 是预期响应：测试故意不携带密钥。它表示本次已完成 HTTPS 连接并收到鉴权层的响应，不代表模型推理或压缩接口已通过。

失败的 Windows curl 已收到本地代理 `CONNECT 200`，随后发生：

```text
curl: (35) schannel: failed to receive handshake, SSL/TLS connection failed
```

该请求的 TLS 完成时间为零，不能把失败时记录的 `ssl_verify_result=0` 解读为已经完成证书校验。

## 结论与边界

1. 本轮没有复现 Python 特有的稳定故障；默认 Python TLS 1.3 连续收到六次响应。
2. 独立的 Schannel/curl 也能复现握手失败，因此现有证据不支持将此前错误单独归因于 Python、SDK 或 OpenSSL。
3. 综合前一次 Python SSLEOFError 与本次 curl 偶发失败，优先排查代理出口到目标服务的间歇性连接问题；具体是代理、出口、中间设备还是服务边缘，需要更多线路或服务端证据。
4. TLS 1.2 的两次成功不足以证明强制降为 TLS 1.2 能解决问题；默认 TLS 1.3 同样通过，本轮没有修改 Agent TLS 设置。
5. 测试没有调用原生压缩接口，因此之前的 HTTP 502 是否恢复仍未知。网络可达与压缩路由可用应分开核验。

下一步可先在用户选定的固定代理出口重复同样对照；稳定后再分别执行普通模型请求和最小压缩请求。不要据此增大上下文窗口，也无需根据本轮证据改动模型或关闭证书校验。

## 复现与原始记录

项目根目录运行（无 API 费用，无密钥）：

```powershell
& 'C:\Users\researcher\anaconda3\python.exe' -X utf8 -u .cache/tls-diagnostics/compare_tls.py --modes curl_windows,python_default --repeats 4
```

- 脚本：`.cache/tls-diagnostics/compare_tls.py`。
- 第一批：`.cache/tls-diagnostics/comparison-1791444187059558500.json`。
- 第二批：`.cache/tls-diagnostics/comparison-1791444235127902600.json`。
- 原始记录与脚本位于本地缓存，不应当作已纳入 Git 的文件。

本轮未修改系统代理、模型配置、会话或计算任务。
