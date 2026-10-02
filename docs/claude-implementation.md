# Claude 接入实施与验证

实施日期：2026-10-02。对应 [集成设计](claude-integration-design.md)。

## 已实现

- Claude Provider：Responses / Chat 模型路由，原生 `POST /v1/messages` JSON/SSE，
  有鉴权的 `/v1/messages/count_tokens` 估算接口。
- 原生 system、文本/图片、客户端 tools 和文本/图片 tool_result、effort、JSON Schema、
  缓存读写 usage、错误映射、模型别名和生成策略响应头。
- 复用现有鉴权、厂商授权、调度、审计、计费、绑定、工具会话及账号 generation 隔离。
  Claude Code 的 metadata 会话 UUID 支持完整历史校验后的 `--resume`。
- Claude Worker 镜像选择、独立 home 卷、创建/登录/探测/账号检查/额度窗口、
  付费订阅贡献额度、恢复及厂商授权 UI。旧 Gemini 登录路由保留。
- 新增通用 `provider-login` 路由；Claude 授权链接仅接受 HTTPS 的 claude.com。
  Worker 登出在互斥锁内完成凭证检查、transcript 清理和额度缓存清理。
- Worker 同会话并发返回 409；网关流关闭会立即关闭 Worker HTTP 流。
- 每小时清理超过 24 小时、无有效绑定的孤立 Claude transcript；
  有效 ResponseBinding/ExecutionSession、最近文件和运行中会话保留。
  普通有效绑定不因 TTL 过期。
- Claude Code 实测发现工具结果之后还会追加 system reminder：
  网关保留原始历史，将这些提醒作为文本一起返回 relay，不吞掉新的 user 消息。
- Worker 结构化输出只交付 CLI 的最终 structured_output，过滤中间文字。

## 验证记录

本地使用独立 PostgreSQL 实例与测试数据库，不使用默认数据库或生产数据。
最终全量回归：411 项通过（含原有 340 项及新增 71 项），耗时 87.12 秒；
仅有现有 Starlette/httpx 弃用提示，未修改原有测试断言。
新增测试覆盖原生翻译、分片 SSE、鉴权、错误、缓存计数、工具往返/图片结果、
跨 Key 隔离、重复提交、断流关闭、结构化输出、账号/额度/登出、同会话并发、
会话清理及容器隔离参数。

真实推理使用设计文档中的测试 Worker `claude-worker-proto`、
`claude-sonnet-5-5`，通过 SSH 转发连接本地隔离网关。
测试网关使用独立数据库，工作目录预先创建；本轮真实测试未经过远端
worker-manager 创建流程，也没有替换服务器上运行中的网关或 Worker 镜像。

已通过：

- Responses JSON/SSE：客户端 function/custom grammar 工具、previous_response_id、
  工具结果消费、重复结果拒绝。
- Chat JSON/SSE：客户端函数工具往返。
- 原生 Messages JSON/SSE：客户端工具往返、JSON Schema、base64 图片。
- 原生 metadata 完整历史续接；数据库审计确认执行决策为 resume。
- Anthropic Python SDK 1.11.0：messages.create 与 messages.stream/get_final_message。
- 本机 Claude Code CLI 作为客户端：Read 工具在客户端读取测试文件，
  经网关 → Worker 内 CLI → 客户端工具回填后得到文件中的标记。
- 额度/账号管理、登录完成验证和贡献额度通过 mock Worker 集成测试；
  Worker 并发拒绝及清理通过直接端点测试。

仍需真实账号验收：浏览器 OAuth 授权码从 UI 完整提交后的登录流程、
真实订阅额度耗尽后的恢复。限额错误到 429、冷却状态及额度保留已用自动测试覆盖。
本轮未重建部署 Worker 镜像；Worker 清理/结构化过滤增强已通过本地测试。

## 产品流程与 UI 补齐（2026-10-02）

- 管理员和用户均可创建 Claude Worker；管理列表明确展示厂商，账号支持登录、
  重新登录、独立退出、探测和额度查询。退出会重新计算贡献额度并使原会话失效。
- OAuth 弹窗展示授权会话剩余时间，支持授权失败/过期后重启登录，以及登录成功但
  推理验证失败后的重新探测；授权码提交后隐藏输入，避免重复提交。
- 我的账户展示当前厂商权限；管理员额外授权与 Worker 自动授权复用既有权限规则。
- 调试页增加 Messages、Token 估算、按账号权限筛选的模型、请求示例、流式开关、
  停止请求和生成策略响应头展示；编辑过的 JSON 不被切换接口自动覆盖。
- 我的账户与调试页增加 Claude Code、Anthropic SDK、OpenAI 兼容客户端接入说明，
  明确根地址与 `/v1` 的区别及 CLI 参数限制。
- 修正缓存读/写价格编辑后的保存状态判断，贡献页面明确额度耗尽保留付费贡献名额。

验证：全量 413 项通过，随后新增跨模块产品验收 1 项通过；JavaScript 语法与
`git diff --check` 通过。新增 `test_claude_product.py` 验证管理员创建、普通 Key
厂商授权、三个推理接口 JSON/SSE、Token 估算、真实调度选择与用量审计、授权撤销；
Worker 管理与推理使用模拟服务，不代表真实 Docker/OAuth 验收。
`scripts/validate_claude_ui.py` 使用本地浏览器与模拟 HTTP 验证原生请求头、请求示例、
流式显示、无效 JSON、登录与探测重试、授权过期恢复及移动端弹窗；不访问真实账号。
该脚本需要 Playwright、Jinja2 和 Chrome。

上述修改尚未部署；真实 OAuth、真实订阅耗尽恢复和新 Worker 镜像验收状态保持不变。

## 部署步骤

1. 构建 `docker build -t codex-claude-worker:2.1.287 worker/claude`，
   更新 gateway 与 worker-manager 代码。Compose 已传递
   `CLAUDE_WORKER_IMAGE`（默认该标签）。
2. 在现有 `CODEX_GATEWAY_ALLOWED_MODELS` 中追加 Claude 模型；
   在 `CODEX_GATEWAY_MODEL_PROVIDERS` 中追加对应 `模型:claude` 项，例如：
   `claude-sonnet-5-5:claude,claude-opus-5-5:claude,claude-haiku-4-5:claude`。
3. 可选设置 `CODEX_GATEWAY_CLAUDE_NATIVE_MODEL_ALIASES`，格式与 Gemini 别名相同。
4. 创建 Claude Worker，在登录窗口授权付费订阅，探测通过后配置模型价格与套餐权重。
   生产凭证使用 Worker home 卷中的 OAuth 登录，测试环境令牌不复制到生产配置。
5. 客户端设置 `ANTHROPIC_BASE_URL=<网关地址>` 和
   `ANTHROPIC_API_KEY=<已有网关 Key>`。
   可用 `GATEWAY_URL=... GATEWAY_API_KEY=... python scripts/validate_claude_tools.py`
   复跑 Responses/Chat/原生工具及 Schema 冒烟验证。

没有新增数据库表或列。回滚时停用 Claude Worker、回滚网关与 manager，
保留 `*-claude-home` 卷。

## 第一阶段能力边界

`max_tokens`、采样/停止参数、thinking budget 和缓存控制等接受但不控制 CLI，
通过 `x-gateway-generation-policy: worker-defaults` 声明。
不输出 thinking 块；不支持 assistant prefill、强制工具选择、
服务端工具、MCP connector、文件/PDF、批处理或精确 token 计数。
完整矩阵见 [API 兼容性](api-compatibility.md#claude-provider-and-anthropic-messages-2026-10-02)。
