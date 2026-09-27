# 上下文关联观测

目标：先比较不同客户端如何标识会话，再决定逻辑会话关联和后端 Thread 自动续用规则。当前不启用推断、自动续用或按新字段调度。

2026-09-27 生产样本：

- Responses 客户端在 `client_metadata` 中携带 thread_id、session_id、安装 ID、turn_id，且 prompt_cache_key 与客户端 thread_id 一致；连续输入为 13、15、17、19 项。后端生成不同 Thread。
- Chat Completions 客户端只有 model/messages/tools 等请求体字段，无明确会话 ID；messages 为 6、8、10、12 项，逐次完整追加历史，分配到不同 Worker。
- 缓存读取命中不能证明属于同一会话。客户端 Thread ID 不能直接作为后端 Thread ID 使用。

## 新增记录

`usage_records.request_params` 继续保存接收到的 JSON 请求体（含未知扩展字段）；独立的 `request_observation` JSON 保存：

- 到达时间（UTC）、网关 X-Request-ID、方法、路径、HTTP 版本、scheme、ASGI client 地址。
- 请求头名称和值、查询参数名称和值，以数组保留同名重复项。不限制为预先猜测的会话字段。
- 收到的请求体字节数、SHA-256、是否读完、是否超过原有请求体记录上限。
- 观测版本及截断说明。每字段最多 8192 字符、每类最多 128 项、头和查询参数合计按原始字段最多 64 KiB；过大值整体省略。

认证、Cookie、Key、Token、密码等已知敏感名称脱敏；JSON 请求头中的同类字段递归脱敏，URL 查询参数同样处理。请求体沿用原有记录方式。本次不改变原有请求体保存范围。未知自定义字段仍可能包含敏感业务信息，观测记录沿用请求详情的用户/管理员访问控制。

观测版本 2 的 `client_ip` 只读取 `request.client.host`；`client_address` 是同一 ASGI 地址及端口，不代表原始 TCP peer。Uvicorn 在应用之前处理代理头：Compose 显式开启 `--proxy-headers`，`--forwarded-allow-ips` 默认仅信任 `192.0.2.8,192.0.2.9,203.0.113.8,203.0.113.9`，可通过 `CODEX_GATEWAY_TRUSTED_PROXY_IPS` 配置。来自这些 pfSense HAProxy 节点的请求，按 XFF 右侧非信任地址还原 IP，并按 X-Forwarded-Proto 还原 scheme；其他来源伪造代理头不会改写应用地址。经代理还原后的端口可能为 0，不能用它识别会话。

HAProxy 须追加真实连接来源到 XFF，并覆盖 X-Forwarded-Proto；不要将信任范围设置为 `*` 或整个 Docker 网段。原始 XFF 仍作为观测请求头保存，但不用于业务 IP 判断。这里不使用 X-Auth 共享密钥认证。Compose 之外启动 Uvicorn 时也须显式传同样的参数。现有观测记录不回填。

成功、已认证的拒绝及中断请求均保留观测信息；未认证请求、进入审计前即被大小限制拒绝的请求沿用原有行为，不写用量记录。旧记录没有这些字段，不能事后补齐请求头。

## 测试采样

每种客户端测试：新会话、多轮追问、另开会话、编辑历史或分叉、重试、并发、上下文压缩、客户端重启。在请求详情中比较 headers、query_params 与 request_params 中的 metadata/client_metadata，并检查哪些 ID 随会话、轮次或上下文窗口变化。仅记录标识，不据此改变转发。

后续自动续用需要：API Key 范围内的客户端标识映射、前序输入和实际输出校验、增量转换、并发/重试/分叉处理，以及 Worker 和工作目录一致性。
