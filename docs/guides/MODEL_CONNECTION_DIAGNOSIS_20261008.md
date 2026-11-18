# 模型连接与原生压缩排查（2026-10-08）

## 本次结论

尚未恢复模型接口，不应把故障归因于上下文容量，也不能宣称绕过代理即可解决。

同一个 relay 配置（verysadai.com，模型 gpt-6-astra）的小请求出现两类错误：普通 Responses 请求连接失败，原生 `/responses/compact` 返回 HTTP 502。本轮仅发送合成短文字，不发送真实会话、计算输入或工具结果，不执行 VASP 或 SSH。

## 核验记录

| 模式 | 最小普通请求 | 最小原生压缩请求 |
|---|---|---|
| 系统默认代理 | 约 5 秒连接失败；异常链包含 httpcore2 代理连接、TLS 握手与 SSLEOFError | HTTP 502 |
| 仅诊断进程禁用系统代理 | 约 26 秒 ConnectTimeout | HTTP 502，约 9 秒 |

Python 检测到系统代理 127.0.0.1:7897，虽然当前进程没有显式 HTTP_PROXY/HTTPS_PROXY 环境变量。只检查环境变量不足以排除 Windows 系统代理。

TLS 提前关闭只证明连接在该阶段被中断，不能仅凭客户端堆栈确定是本机代理、代理出口、中转站还是中间链路主动断开。502 也不能单独证明压缩接口不受支持，需要中转站的请求记录与上游错误。

首个诊断尝试曾将 httpx.Timeout 对象传给本机使用 httpx2 的 OpenAI SDK，普通请求出现 TypeError；这是诊断脚本的类型不兼容，已改用数值 timeout 重测。它不应被当作用户原始故障。初次缓存报告若包含该 TypeError，应按此解释。

本轮没有改变模型、协议、上下文窗口、系统代理或 TLS 验证设置，也没有自动切换服务商。

## 处理顺序

1. 暂停容量扫描和反复 `/compact`，先恢复小请求。
2. 在当前代理软件检查 verysadai.com 的实际匹配规则和出口线路，换用可正常连接的线路后再次测试。不能假定直连在本机可用。
3. 在项目根目录进行普通工具接口验证：

   ```bash
   python ai_agent.py --profile relay --test-connection --timeout 30 --model-retries 0
   ```

   该命令会调用真实 API，并测试工具调用及工具结果返回；不运行真实计算。它比最小文本测试多一层工具协议验证。

4. 普通接口正常后，再运行一次小规模压缩测试：

   ```bash
   python examples/probe_compaction.py --profile relay --sizes 1k --content messages --repeats 1 --max-requests 1 --timeout 30
   ```

5. 如果普通接口成功而压缩仍为 502，请中转站检查该模型的 `/v1/responses/compact` 路由、上游权限和失败记录。支持普通聊天或宣称长上下文，不足以证明原生压缩可用。支持情况需实际验证。
6. 小规模压缩稳定成功后，才继续容量分档；不要通过反复增大 timeout 掩盖握手断开或 502。

如果服务商不支持原生压缩，可讨论显式选择“文字摘要压缩”作为另一种模式。当前 Responses 路径不会自动回退；不要把更换协议或服务商作为隐藏恢复行为。若未来实现切换，应从完整本地历史重新生成摘要，不把不透明原生状态当成可读文本，也不丢失权限及提交状态证据。

## 本轮代码修正

`src/vasp_agent/models/request_errors.py` 现在区分 SSLEOFError，并在 httpcore/httpcore2 把底层网络异常隐藏为 `from None` 时恢复有限的类别信息。只展示预定义描述，不展示异常原文、凭据或地址；仍忽略任意应用异常的被隐藏上下文。

覆盖 request_errors、reconnect、native_compaction、compaction_probe 的相关测试共 85 项通过。这是错误诊断改进，不代表远程服务已修好。
