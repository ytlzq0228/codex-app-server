# Claude Provider 接入设计（评估 + 方案 + 已验证原型）

> 下文保留初始评估与实施方案。网关第一阶段已实现，当前能力、验证证据和待验收项见 [实施记录](claude-implementation.md)。

日期：2026-10-02。读者：负责继续开发本系统的 Agent。本文给出可直接实施的改动清单、协议规范和验收标准；
Worker 原型已在测试环境跑通，网关侧改动尚未实现。

## 0. 结论摘要

| 决策 | 结论（已与产品负责人确认） |
|---|---|
| Worker 凭证 | **Claude 订阅账号**（Pro/Max/Team），用官方 Claude Code CLI 在 Worker 内 OAuth 登录；与 Codex/Gemini 的"订阅账号贡献"模型一致。预留 API Key 形态的扩展点但不实现。 |
| 公共接口 | 新增 **Anthropic 原生 `POST /v1/messages`**（首要目标，含 SSE、tools、images、system、usage），同时 `/v1/responses`、`/v1/chat/completions` 传入 claude 模型名时命中 claude 转发。 |
| CLI 无法控制的参数 | `max_tokens`、`temperature`、`top_p`、`top_k`、`stop_sequences`、`thinking.budget_tokens`：**接受并忽略**，响应头 `x-gateway-generation-policy: worker-defaults`（与 Gemini 原生中间件一致）。只拒绝结构上无法实现的能力（`tool_choice: any/tool`、assistant prefill、server tools、batches、files）。 |
| 隔离原则 | 新增 `claude` provider 只增加分支，不修改 Codex/Gemini 的现有路径。所有 provider 判定处已经是 `provider == "gemini"`/`"codex"` 显式分支，加 `"claude"` 分支即可。 |

已验证（测试环境 `<deploy-user>@<test-host>`，容器 `claude-worker-proto`，镜像 `codex-claude-worker:proto-20261002`，
与 worker-manager 相同的加固参数）：

- 文本流式输出、`--resume` 会话续接、客户端工具（MCP relay → `/tool-result` 回填）、原生 base64 图片输入、`--json-schema` 结构化输出、
  five_hour/seven_day 额度窗口读取、客户端断连取消、3 路并发、无浏览器环境下的 pty 登录流程（给出 OAuth URL + 等待授权码）。
- 对抗性提示（要求用 Bash 读凭证文件）：没有任何工具可用，未执行任何操作；模型只是在正文里"幻觉"了 `<invoke>` 文本（见 §7 缓解）。
- 验证脚本：[scripts/validate_claude_worker.py](../scripts/validate_claude_worker.py)。

未验证：真实订阅额度耗尽、通过网关 UI 完成的 OAuth 登录（原型只验证到授权 URL 与授权码提交路径）、网关侧改动（未实现）。

## 1. 现状：Codex / Gemini 是如何接入的

网关对 provider 的抽象已经存在，Claude 是第三个实例：

| 层 | 现有实现 | 对 Claude 的意义 |
|---|---|---|
| 模型→provider | [config.py](../src/codex_gateway/config.py) `provider_map()` 解析 `CODEX_GATEWAY_MODEL_PROVIDERS=model:provider`，**已经允许 `claude`**；`providers.provider_for()` 默认 codex | 只需配置 `claude-opus-5-5:claude,...` |
| 能力校验 | [providers.py](../src/codex_gateway/providers.py) `CAPABILITIES` 字典 + `validate_capabilities()`；`provider not in CAPABILITIES` → `provider_unavailable`（这就是当前 "Claude is reserved" 的来源） | 加 `"claude": Capabilities(...)` 和 claude 专属参数规则 |
| 后端分发 | [gemini_backend.py](../src/codex_gateway/gemini_backend.py) `ProviderBackend.adapter(target)`：`codex`→`AppServerBackend`，`gemini`→`GeminiAdapter`，其它→`Provider is not implemented` | 加 `claude`→`ClaudeAdapter`（新模块 `claude_backend.py`） |
| Worker 选择 | [main.py](../src/codex_gateway/main.py) `choose_target(provider=...)` 按 `Worker.provider` 过滤，绑定/固定 Worker 校验 provider 一致 | 完全通用，无需改 |
| 会话续接 | [execution.py](../src/codex_gateway/execution.py) `prepare()`：显式会话标识 + 历史前缀哈希 → `thread/resume`；非 codex 且无法 resume 时 409 `conversation_resume_unavailable` | thread_id = Claude Code session UUID；`--resume` 语义与 Gemini `--conversation` 相同 |
| 客户端工具 | [client_tools.py](../src/codex_gateway/client_tools.py) 声明校验/别名；[tool_sessions.py](../src/codex_gateway/tool_sessions.py) 挂起运行 + 300s TTL + 单结果；Gemini 通过 Worker 内 MCP relay 实现 | Claude Worker 复用同一 MCP relay 协议（已验证） |
| Worker 生命周期 | [manager.py](../src/codex_gateway/manager.py) `WorkerSpec.provider: Literal["codex","gemini"]`，按 provider 选镜像、home 卷挂载点、endpoint scheme | 加 `claude` 分支 |
| 账号/额度/贡献 | [contributions.py](../src/codex_gateway/contributions.py) `probe_gemini`、`/gemini-login/{action}`；[quota.py](../src/codex_gateway/quota.py) `contribution_filters` 按 provider+auth_mode 计入额度；[monitoring.py](../src/codex_gateway/monitoring.py) `read_usage` 按 provider 读额度 | 加 claude 分支（见 §5） |
| 原生协议 | [gemini_native.py](../src/codex_gateway/gemini_native.py) `GeminiNativeMiddleware`：ASGI 中间件把 `/v1beta/models/{m}:generateContent` 翻译成 `/v1/chat/completions`，响应再翻译回去，从而共享鉴权、配额、审计、绑定 | `/v1/messages` 采用同样的边界翻译法，翻译目标选 `/v1/responses`（§6） |

## 2. 目标架构

```
Claude Code CLI / Anthropic SDK ──POST /v1/messages──┐
OpenAI SDK ───────────────────────POST /v1/responses ─┤
OpenAI SDK ───────────────POST /v1/chat/completions ──┤
                                                      ▼
            ClaudeNativeMiddleware（仅 /v1/messages：Messages ⇄ Responses 翻译）
                                                      ▼
            现有 Responses 管线：鉴权 → authorize_model → validate_capabilities(claude)
            → prepare_execution（会话标识/历史哈希）→ choose_target(provider="claude")
                                                      ▼
            ProviderBackend.adapter → ClaudeAdapter（claude_backend.py）
                                                      ▼  HTTP NDJSON（私有，Bearer CODEX_WORKER_TOKEN）
            Claude Worker 容器（worker/claude/service.py）
              └─ 每轮一个进程：claude -p --output-format stream-json --tools "" --mcp-config <relay> ...
                   └─ 客户端工具 → MCP relay（/client-mcp/{token}）→ NDJSON client_tool 事件 → 网关挂起 → /tool-result
```

关键选择：**推理只发生在官方 Claude Code CLI 内**，网关/Worker 不直接持有或转发订阅 OAuth 令牌去调用 `api.anthropic.com`。
这与 Codex（app-server）/Gemini（agy）保持同一形态，也是订阅凭证使用方式上最保守的做法。

## 3. Claude Code CLI 能力实测（2.1.287，headless）

Worker 设计完全基于以下实测行为（本机 + 测试环境均复现）：

| 需求 | CLI 机制 | 实测结论 |
|---|---|---|
| 流式输出 | `-p --output-format stream-json --include-partial-messages --verbose` | 逐行 JSON：`system/init`（含 `session_id`）、`rate_limit_event`、`stream_event`（**内嵌原生 Messages API 事件**：`message_start`/`content_block_start`/`content_block_delta`/`message_delta`/`message_stop`）、`assistant`、`user`（工具结果回显）、`result`（`usage`、`stop_reason`、`structured_output`、`num_turns`、`is_error`、`api_error_status`） |
| 多模态输入 | `--input-format stream-json`，stdin 一行 `{"type":"user","message":{"role":"user","content":[blocks]}}` | 直接接受 Anthropic `image`（base64）块；不需要 Gemini 那种 `gateway_read_image` 变通 |
| 会话 | `--session-id <uuid>` 预分配 / `--resume <uuid>` | 续接不依赖 cwd（跨目录 resume 成功）；会话文件在 `~/.claude/projects/<cwd-slug>/<uuid>.jsonl` |
| 客户端工具 | `--tools ""`（移除全部内置工具）+ `--mcp-config <json>` + `--strict-mcp-config` + `--allowedTools mcp__client` + `--permission-mode dontAsk --permission-prompts none` | 模型看到 `mcp__client__<name>`；`tools/call` 的 `_meta["claudecode/toolUseId"]` 给出原生 `toolu_…` id；一次只有一个未完成调用（relay 串行锁） |
| 系统提示 | `--system-prompt-file <path>`（替换 Claude Code 自身系统提示） | 生效；客户内容不进入 argv |
| 结构化输出 | `--json-schema <schema>` | `result.structured_output` 为校验后的对象；模型内部走 `StructuredOutput` tool_use（Worker 过滤该块） |
| 推理深度 | `--effort low/medium/high/xhigh/max` | 映射 `output_config.effort` / `reasoning.effort` |
| 额度 | 每轮首个 API 调用前产生 `rate_limit_event.rate_limit_info.unifiedWindows.{five_hour,seven_day}.{utilization,resetsAt}` | 可直接映射到 Codex 风格的 5 小时/周窗口（已映射，见 §4.3） |
| 登录 | `claude auth login --claudeai`（pty）：打印 `https://claude.com/cai/oauth/authorize?...`，提示 `Paste code here if prompted >`，成功打印 `Login successful.`；`claude auth status` 输出 JSON（`loggedIn,email,subscriptionType,orgName,authMethod`）；`claude auth logout` | 与 Gemini 的 pty 登录形态一致。**注意 pty 必须足够宽**（≥ 200 列），否则 URL/令牌被换行截断（本次实测踩坑） |
| 测试期凭证 | `claude setup-token` → `sk-ant-oat01-…`（108 字符，1 年），通过 `CLAUDE_CODE_OAUTH_TOKEN` 环境变量注入 | 该模式下 `auth status` **不返回 email/套餐**，因此不会计入贡献额度；仅用于测试 |
| 不可控参数 | 无 `max_tokens`/`temperature`/`stop_sequences`/thinking budget 的 CLI 开关 | 接受并忽略（决策见 §0） |
| 作为客户端 | `ANTHROPIC_BASE_URL=http://gateway` + `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` | 请求 `POST /v1/messages?beta=true`；头：`anthropic-version: 2023-06-01`、`anthropic-beta: claude-code-20250219,interleaved-thinking-2025-05-14,…`、`x-api-key`（或 `Authorization: Bearer`）、`x-app: cli`；体：`model, messages, system[](含 cache_control), tools[]（20 个内置工具）, metadata.user_id, max_tokens=128000, thinking={adaptive, display:omitted}, context_management, output_config.effort, stream=true` |

## 4. Claude Worker（原型已实现：`worker/claude/`）

文件：[Dockerfile](../worker/claude/Dockerfile)、[service.py](../worker/claude/service.py)、[client_bridge.py](../worker/claude/client_bridge.py)。

### 4.1 镜像与运行时

- `debian:bookworm-slim` + 官方独立二进制 `https://downloads.claude.ai/claude-code-releases/2.1.287/linux-x64/claude`，SHA256 固定在 Dockerfile（与 Antigravity 镜像做法一致，manifest 校验值 `3920489a…1718f0`）。
- 非 root `claude`(10001)，只读根文件系统，`cap_drop ALL`，`/tmp` tmpfs。**整个 `/home/claude` 是 Worker 卷**（凭证 `~/.claude/.credentials.json`、状态 `~/.claude.json`、会话 transcript 都在其中）。
- 环境：`DISABLE_AUTOUPDATER=1 DISABLE_TELEMETRY=1 DISABLE_ERROR_REPORTING=1 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`。
- 每轮固定参数（`BASE_ARGS`）：`--tools "" --strict-mcp-config --permission-mode dontAsk --permission-prompts none --disable-slash-commands --setting-sources user --settings '{"permissions":{"defaultMode":"dontAsk","deny":[Bash,Read,Edit,...]}}'`。双重围栏：`--tools ""` 移除内置工具，deny 列表兜底。
- 并发：`MAX_TURNS=8` 个 CLI 进程；额外最多 32 个请求按 FIFO 排队，最长等待 30 秒，断连或取消时释放队列位置。登录/探测期间及同一会话重复执行立即返回执行冲突（409）。

### 4.2 私有 HTTP 协议（Bearer `CODEX_WORKER_TOKEN`）

| 端点 | 请求 | 响应 |
|---|---|---|
| `POST /capabilities` | — | `{"provider":"claude","client_tools":1,"image_input":1,"tool_result_types":["text","image"],"native_stream":1,"structured_output":1,"effort":1,"system_prompt":1,"max_turns":8}` |
| `POST /account` | — | `{"account":{"type":"claude-subscription","email","planType","project"(=orgName),"authMethod"},"available":true}` 或 `{"account":null,"kind":"logged_out"}` |
| `POST /probe` | `{"model"}` | account + 一次真实最小轮（"Reply with OK only."）；失败 `{"available":false,"kind":"limit|logged_out|request|connection"}`；成功附 `rate_limits` |
| `POST /rate-limits` | `{"model"}` | **Codex 形状**：`{"rateLimits":{"primary":{"windowDurationMins":300,"usedPercent","resetsAt"},"secondary":{"windowDurationMins":10080,...}},"available":true,"status":"allowed","checked_at"}`；缓存最近一次 `rate_limit_event`，超过 1 小时才用真实轮刷新 |
| `POST /turn` | 见下 | NDJSON 流 |
| `POST /tool-result` | `{"run_id","worker_call_id","content":[{"type":"text","text"}|{"type":"image","data","mimeType"}],"is_error"}` | `{"ok":true}` |
| `POST /client-mcp/{token}` | MCP Streamable HTTP（JSON-RPC） | 仅本轮 relay 使用 |
| `POST /login/start` `/login/status` `/login/input` `/login/verify` `/login/logout` | 与 Gemini 相同的表单字段 | `{"session_id","stage":"waiting|authorize|done","title","message","login_url","logged_in","account","error","expires_in"}`；`input` 只接受 `action=code|cancel` |

`POST /turn` 请求体：

```json
{"model": "claude-sonnet-5-5",
 "session_id": "<uuid, 新会话由网关预分配>",
 "conversation": "<uuid|null, 非空则 --resume>",
 "system": "<可选, 替换系统提示>",
 "content": [{"type":"text","text":"..."}, {"type":"image","source":{"type":"base64","media_type":"image/png","data":"..."}}],
 "tools": [{"name":"get_weather","description":"...","inputSchema":{...}}],
 "effort": "high|null", "json_schema": {...}|null,
 "workspace": "/workspace/<key_slug>"}
```

约束：content 只允许 `text`/`image`(base64, png/jpeg/gif/webp, ≤10 MiB/张, ≤20 张, ≤20 MiB)；tools 名称 `^[A-Za-z0-9_-]{1,64}$`，≤64 个；`json_schema` 与 `tools` 互斥。

`POST /turn` NDJSON 事件（每行一个对象）：

| 行 | 含义 |
|---|---|
| `{"heartbeat":true}` | 空闲 15s 心跳（等待客户端工具结果时持续） |
| `{"event":"init","thread_id"}` | CLI 会话已建立；`thread_id` 即 session UUID |
| `{"event":"rate_limit","rate_limit_info":{...}}` | 原始 `rate_limit_event`，网关可按需记录 |
| `{"thread_id","stream":{<原生 Messages 流事件>},"delta":"<仅 text_delta 时>"}` | 原样转发 `message_start`/`content_block_*`/`message_delta`/`message_stop`；`tool_use` 块（relay 调用与 `StructuredOutput`）已被过滤，不出现在 `stream` 中 |
| `{"event":"client_tool","run_id","worker_call_id","tool","arguments","tool_use_id","thread_id"}` | 模型请求客户端工具；网关回 `/tool-result` 后 CLI 继续 |
| `{"thread_id","done":true,"stop_reason","num_turns","usage":{原始},"model_usage","input_tokens","output_tokens","cache_read_tokens","cache_write_tokens"}` | 终态；`input_tokens` = uncached + cache_read + cache_write（与网关/OpenAI 口径一致，Anthropic 原始口径保留在 `usage`） |
| `{"error":"...","kind":"limit|logged_out|request|session|connection"}` | 失败；`kind` 直接对应 `WorkerFailure.kind` |

终态行在进程回收、relay 关闭、临时目录清理之后才发出（与 Antigravity 相同的"可立即续接"承诺）。客户端断连 → SIGINT 进程组，5s 后 SIGKILL，清理过程 cancellation-shielded（已验证无残留进程）。

### 4.3 额度与贡献

- `rate_limit_event.unifiedWindows.five_hour/seven_day.utilization`（0–1）×100 → `usedPercent`；`resetsAt` 原样。网关侧用现有 `rate_limits.summarize_windows()`（Codex 路径）即可渲染，`_pool_usage` 的"周加权剩余额度"路由也直接复用。
- 贡献额度：真实 OAuth 登录时 `auth status` 返回 `email` 与 `subscriptionType`（pro/max/team/enterprise），`auth_mode = "claude-subscription"`；套餐键 `claude:<subscriptionType>`（`subscriptions.plan_key` 已支持 claude 前缀与 "Claude · x" 展示）。

### 4.4 原型的已知限制 / 待办

1. `~/.claude/projects/<cwd-slug>/` 会随 workspace 目录累积会话文件；需要按 `ResponseBinding` 失效或 TTL 清理（建议：Worker 增加 `POST /sessions/prune`，网关在绑定失效/登出时调用；登出时整目录清空）。
2. `/login/logout` 在环境变量注入令牌时无法真正登出（CLI 不会撤销 env 令牌）；生产只用卷内凭证。
3. 同会话并发 409 的逻辑与 Gemini 相同，但本次实测中第一轮完成过快，未能稳定复现 409，需补单测。
4. Worker 不验证 `model` 是否在订阅可用集合内；错误由 CLI 返回（`result.is_error` + `api_error_status`）并分类为 `request`。

## 5. 网关改动清单（按文件）

> 原则：每处都是"新增 claude 分支"，Codex/Gemini 现有分支不改语义。

1. **[providers.py](../src/codex_gateway/providers.py)**
   - `CAPABILITIES["claude"] = Capabilities(images=True, tools=True, structured_output=True, reasoning=True)`。
   - `validate_capabilities` / `validate_chat_capabilities`：当前把所有非 codex 都按 Gemini 规则处理，需拆成 `_validate_gemini(request)` 与 `_validate_claude(request)`。Claude 规则：`reasoning.effort` ∈ {low,medium,high,xhigh,max} 允许；`temperature/top_p/max_output_tokens/truncation/service_tier/prompt_cache_retention/max_tool_calls/include` **接受并忽略**（在响应头声明 `x-gateway-generation-policy: worker-defaults`）；`parallel_tool_calls` 强制 False（relay 串行）；工具结果允许 text+image（Worker `tool_result_types` 已含 image）；结构化输出与工具互斥；`tool_choice` 仅 auto/none（已由 client_tools 保证）。
2. **新模块 `claude_backend.py`**（镜像 `GeminiAdapter`，约 200 行）：
   - `ClaudeAdapter.stream/complete/_turn_events(request, target, tool_run)`，复用 `ToolSessions`、`public_call`、`call_matches_grammar`、`validate_capabilities`、worker 身份校验（`worker.provider != "claude"` → `account_changed`）。
   - 构造 `/turn` 载荷：
     - 新会话：`system = request.instructions`（或原生 system 拼接）；`content` = `request.worker_input()` 的 Anthropic 块形式（text 块 + image 块；现有 `worker_input()` 产出的 `{"type":"image","image_url":...}` 需转成 base64 `source`，data URL 直接解码，HTTP(S) URL 复用 `gemini_images.prepare_images` 的受限下载）。
     - 续接（`request.previous_response_id` 非空）：`conversation = previous_response_id`，`content` 只含 `_execution_input_items` 增量。
     - 工具：`tools = [{"name": spec["alias"] 或原名, "description": dynamic_specs 描述, "inputSchema": schema}]`。建议 **用原名**（Claude 对 `mcp__client__Bash` 这类名字理解更好），命名空间编码为 `ns__name`；`public_call` 需按名字反查 spec（当前按 alias 匹配，加一个 name→spec 映射即可）。
     - `effort = (request.reasoning or {}).get("effort")`；`json_schema = request.output_schema()`。
   - 事件映射：`delta` → `BackendStreamEvent(delta=...)`；`client_tool` → 与 Gemini 相同的 grammar/schema 校验、`await_result`/`receive_result`、`/tool-result` 回填（图片结果转 `{"type":"image","data","mimeType"}`）；`done` → 终态 usage；`error.kind` → `WorkerFailure(kind)`。
   - **为原生路径保留 `stream` 原始事件**：给 `BackendStreamEvent` 增加可选字段 `raw: dict | None`（schemas.py），ClaudeAdapter 把 `stream` 事件放进去；Responses/Chat 路径忽略该字段，`/v1/messages` 翻译层优先使用它（可无损转发 thinking 块、`message_delta.usage` 等）。第一阶段可以不做，用文本增量重建 Messages 事件即可（§6.3）。
   - `probe_claude(worker, db, settings, *, inference=True, login_session=None)`：与 `probe_gemini` 相同，调 `/login/verify`、`/probe`、`/account`；失败文案改为 Claude。
   - `run_claude_login(worker, action, form)`：转发 `/login/*`。
3. **[gemini_backend.py](../src/codex_gateway/gemini_backend.py) `ProviderBackend`**：`self.claude = ClaudeAdapter(settings)`，共享 `tool_sessions`，`adapter()` 增加 `"claude"` 分支。`worker_rpc` 可直接复用（超时参数相同）。
4. **[main.py](../src/codex_gateway/main.py)**
   - `quarantine_worker`（L181）、`recover_worker`（L207）、`worker_recovery_loop`（L255 的 offline+auth_mode 条件）：`== "gemini"` 改为 `in {"gemini","claude"}` 并按 provider 调用 `probe_gemini`/`probe_claude`。
   - `complete_with_failover`（L557）、`response_stream`（L694）、`chat_completion_stream`（L777）的 Gemini 专属错误文案：改成按 `target.provider` 取文案表 `{"gemini": "Gemini ...", "claude": "Claude ..."}`，错误码不变（`provider_quota_exhausted` 等）。
   - `choose_execution_target` / allow_retry 中 `provider_for(...) != "codex"` 的判断保持（非 codex 不做自动重建），无需改。
   - 新增 `app.add_middleware(ClaudeNativeMiddleware)`（§6）；注意中间件顺序：两个原生中间件互不重叠路径。
   - `/v1/models`：`owned_by` 已按 provider 输出，无需改。
5. **[execution.py](../src/codex_gateway/execution.py)** L509：文案 "Gemini conversation cannot be safely resumed" 改为通用（provider 名插值）；逻辑不变。
6. **[manager.py](../src/codex_gateway/manager.py)**：`provider: Literal["codex","gemini","claude"]`；镜像 `CLAUDE_WORKER_IMAGE`（默认 `codex-claude-worker:2.1.287`）；卷 `f"{name}-claude-home": {"bind": "/home/claude"}`；endpoint `http://`。
7. **[contributions.py](../src/codex_gateway/contributions.py) / [admin.py](../src/codex_gateway/admin.py)**：provider 集合加 `claude`；endpoint scheme 规则 `ws` 仅 codex；`probe` 分支；登录路由把 `/{worker_id}/gemini-login/{action}` 泛化为 `/{worker_id}/provider-login/{action}`（保留旧路径），`worker.provider in {"gemini","claude"}`，按 provider 转发到各自 `/login/*`；`relogin_worker_record`/admin L501 对 claude 返回"请在我的 Worker 页面使用 Claude 登录窗口"。
8. **[quota.py](../src/codex_gateway/quota.py) `contribution_filters`**：加 `and_(Worker.provider == "claude", Worker.auth_mode == "claude-subscription")`；免费判定沿用 `plan != "free"`。
9. **[monitoring.py](../src/codex_gateway/monitoring.py)**：`providers` 元组加 `claude`；`read_usage` 对 claude 调 `/rate-limits` 后走 `summarize_windows`（Codex 路径），不是 `provider_usage`。
10. **[self_service.py](../src/codex_gateway/self_service.py) L174**：`edit_provider_grants` 增加 `claude` 复选框。
11. **模板/静态**：`contributions.html` Worker 类型下拉加 `Claude / Claude Code`；`gemini-login.html/.js` 泛化为 provider 登录对话框（标题/按钮文案按 provider，`data-provider-login`），Claude 流程只有 `authorize` 一个阶段（无菜单）。
12. **[config.py](../src/codex_gateway/config.py)**：无需改；新增 `claude_native_model_aliases`（可选，与 `gemini_native_model_aliases` 对称，用于把 `claude-opus-5-5-20260401`、`[1m]` 后缀等别名映射到公开模型名）。
13. **[schemas.py](../src/codex_gateway/schemas.py)**：`BackendStreamEvent.raw: dict | None = None`（可选，见 2）。
14. **[docs/](.)**：更新 `api-compatibility.md`（claude 列）、`client-tools.md`（Claude relay 段）、`images.md`（Claude 原生图片）。

## 6. 原生 `POST /v1/messages`（`claude_native.py`，`ClaudeNativeMiddleware`）

与 `GeminiNativeMiddleware` 同构：在 ASGI 边界把 Anthropic Messages 请求翻译成 `/v1/responses` 请求，再把 Responses SSE/JSON 翻译回 Messages 格式。这样鉴权、`authorize_model`、`validate_capabilities`、执行续接、工具挂起、计费、审计全部复用，**不新增第二条执行管线**。

### 6.1 路由与鉴权

- 匹配 `POST /v1/messages`（忽略 `?beta=true` 等查询串）与 `POST /v1/messages/count_tokens`。其它路径直通。
- 凭证：`x-api-key: <gateway key>` 或 `Authorization: Bearer <gateway key>` → 统一改写为 `Authorization: Bearer`（Gemini 中间件对 `x-goog-api-key` 的做法）。删除 `x-api-key` 头后再转发。
- `anthropic-version` 必须存在（否则 400 `invalid_request_error`）；`anthropic-beta` 接受并忽略（记录到审计 observation 便于统计客户端）。
- 响应头：`request-id`（复用 `x-request-id`）、`anthropic-version` 回显、`x-gateway-generation-policy: worker-defaults`、`x-gateway-model`。

### 6.2 请求翻译 Messages → Responses

| Messages 字段 | Responses 映射 | 备注 |
|---|---|---|
| `model` | `model`（经 `claude_native_model_aliases`） | 不在 `public_models()` → 404 `not_found_error`（Anthropic 语义） |
| `system`（str 或 `[{type:text,text,cache_control}]`） | `instructions`（文本拼接） | `cache_control` 忽略 |
| `messages[].role=user`，content 为 str 或 `text`/`image`/`tool_result` 块 | `{"role":"user","content":[input_text/input_image]}`；`tool_result` → `function_call_output{call_id: tool_use_id, output: text 或 [input_text/input_image]}` | `image.source.base64` → `data:` URL；`image.source.url` → `image_url`；`document`/`file` 源 → 400 |
| `messages[].role=assistant`，`text` / `tool_use` / `thinking` 块 | `text` → assistant message；`tool_use` → `function_call{call_id: id, name, arguments: json.dumps(input)}`；`thinking`/`redacted_thinking` **丢弃**（签名无法跨会话复用，网关也不存储） | 最后一条若为 assistant → 400（prefill 不支持） |
| `messages[].role=system`（mid-conversation） | `{"role":"developer","content":...}` | |
| `tools[]`（`name,description,input_schema,strict`） | `tools[]{type:function,name,description,parameters:input_schema}` | `type` 为 `bash_*/text_editor_*/web_search_*/computer_*/mcp_toolset/memory_*` 等服务端工具 → 400 `This gateway does not provide server tools`；`defer_loading`/`allowed_callers`/`eager_input_streaming` 忽略；`cache_control` 忽略 |
| `tool_choice` | `auto`→`auto`，`none`→`none`，`any`/`tool` → 400 | `disable_parallel_tool_use` 忽略（本来就串行） |
| `output_config.effort` / `thinking` | `reasoning: {"effort": ...}` | `thinking.budget_tokens`、`display` 忽略 |
| `output_config.format`（json_schema） | `text: {"format": {"type":"json_schema","schema":...}}` | 与 tools 互斥（沿用 Gemini 规则） |
| `max_tokens`、`temperature`、`top_p`、`top_k`、`stop_sequences`、`service_tier`、`context_management`、`betas`、`speed`、`fallbacks`、`inference_geo` | 不转发 | 响应头声明；`metadata.user_id` → `safety_identifier`，并作为显式会话标识来源（§6.4） |
| `stream` | `stream` | 非流式也走同一管线 |
| `mcp_servers`、`container`、`attachments`、`files` | 400 | |

### 6.3 响应翻译 Responses → Messages

非流式：`{"id":"msg_<resp_id>","type":"message","role":"assistant","model","content":[{"type":"text","text"}, {"type":"tool_use","id":call_id,"name","input":json.loads(arguments)}...],"stop_reason":"tool_use"|"end_turn","stop_sequence":null,"usage":{"input_tokens": input - cache_read - cache_write, "cache_read_input_tokens","cache_creation_input_tokens","output_tokens"}}`。
注意 Responses 的 `usage.input_tokens` 含缓存，Anthropic 的不含，需减回去（Worker `done` 行里保留了原始 `usage`，若实现 `BackendStreamEvent.raw` 可直接用）。

流式（SSE，`event:` + `data:` 两行，与 Anthropic 一致）：

```
message_start{message:{id,type:message,role:assistant,model,content:[],stop_reason:null,usage:{input_tokens:0,output_tokens:0}}}
content_block_start{index:0,content_block:{type:text,text:""}}          ← 收到首个 output_text.delta 时
content_block_delta{index:0,delta:{type:text_delta,text}}               ← 每个 delta
content_block_stop{index:0}
content_block_start{index:n,content_block:{type:tool_use,id:call_id,name,input:{}}}   ← 每个 function_call item
content_block_delta{index:n,delta:{type:input_json_delta,partial_json:arguments}}
content_block_stop{index:n}
message_delta{delta:{stop_reason:"tool_use"|"end_turn",stop_sequence:null},usage:{...}}
message_stop
```

Responses 管线在工具调用时先完成文本块、再按 item 顺序输出 function_call（`response_stream` 已如此），翻译层只需状态机跟踪 `content_block` index。错误：流中 `type:error` → `event: error` + `{"type":"error","error":{"type":<映射>,"message"}}`；HTTP 错误 → `{"type":"error","error":{"type":"invalid_request_error|authentication_error|permission_error|not_found_error|rate_limit_error|api_error|overloaded_error","message"}}`，状态码映射：400/401/403/404/429(`provider_quota_exhausted`)/503(`worker_capacity_exceeded`→529 `overloaded_error`)/502→500。

`tool_use.id` 直接使用网关的 `call_id`（`call_<hex>`）；客户端会原样回填到 `tool_result.tool_use_id`，`ToolSessions` 据此恢复挂起运行，无需额外映射表。

`count_tokens`：返回估算 `{"input_tokens": ceil(chars/4)}`，响应头 `x-gateway-token-count: estimated`（Claude Code 偶尔调用，用于上下文估算；不能 501，否则部分客户端报错）。

### 6.4 会话续接与 Claude Code 客户端

2026-10-02 更新：当前 `metadata.user_id` 是含 `device_id/account_uuid/session_id` 的 JSON 字符串，兼容下述旧格式。`x-claude-code-session-id` 与 metadata 共同确认 session，`x-claude-code-agent-id` 明确区分子代理。主线程使用独立标记；无 agent 的无工具结构化辅助请求不占用执行会话。详见 [多代理修复](claude-subagent-409-fix.md)。

`/v1/messages` 无状态：每轮全量 `messages`。现有 `execution.prepare()` 需要"显式会话标识 + 历史前缀"才会 `--resume`，否则每轮都新建会话并把全量历史扁平化为首条用户消息（Codex/Gemini 的 Chat 路径亦如此）。

- Claude Code 客户端的 `metadata.user_id` 形如 `user_<hash>_account_<id>_session_<uuid>`，每个会话稳定。建议在 `conversations.explicit_identity()` 增加来源：`params.metadata.user_id` 中的 `session_<uuid>` → `client_thread_ids`（与 `x-codex-turn-metadata.thread_id` 同级）。Anthropic SDK 用户可自行传 `metadata.user_id`。
- 续接条件沿用：模型/instructions/tools/output_schema 哈希一致、历史为已保存前缀的追加、绑定 active、Worker generation 不变。Claude Code 每轮工具集合不变，`system` 稳定（它自身做了 snapshot），满足条件。
- 工具结果续接：客户端在下一请求末尾带 `tool_result`（可能多个，若模型并行调用）。本设计 relay 串行，一次只会有一个未完成 `tool_use`，与 `ToolSessions` "单结果" 约束一致；若客户端一次回填多个 `tool_result`（历史里已完成的），翻译层只把**最后一个未完成 call_id** 作为挂起结果，其余作为历史 `function_call_output`（`tool_outputs()` 已按尾部连续项解析，注意顺序）。
- 历史扁平化时 `thinking` 块丢弃、`tool_use`/`tool_result` 以 `ASSISTANT TOOL CALL`/`TOOL OUTPUT` 文本形式呈现（现有 `_item_text`）。

### 6.5 与原生 API 的差距（必须写进 api-compatibility.md）

| 能力 | 状态 |
|---|---|
| 文本/图片输入、system、tools(function)、tool_result(text/image)、流式、usage、effort、JSON schema 输出、`metadata.user_id` | 支持 |
| `max_tokens`、采样参数、`stop_sequences`、`thinking.budget_tokens`、`cache_control`、`context_management`、`betas` | 接受并忽略，头部声明 |
| `thinking` 输出块 | 第一阶段不输出（文本重建）；实现 `BackendStreamEvent.raw` 后可透传 `thinking`（`display: omitted` 时内容为空，仅签名） |
| `tool_choice: any/tool`、assistant prefill、server tools、MCP connector、files/documents/PDF、batches、`count_tokens` 精确值、`stop_reason: max_tokens/stop_sequence/refusal` 区分 | 不支持 / 400 / 估算 |
| 并行工具调用 | 串行（一次一个 `tool_use`） |

## 7. 安全

- Worker：无内置工具（`--tools ""`）+ settings deny 列表 + `--strict-mcp-config` 只连本轮 relay（随机 token URL）+ `dontAsk/none` 拒绝一切权限提示 + 非 root/只读/无 Docker/无宿主挂载。客户内容（系统提示）走 0600 临时文件，不进 argv。
- 已验证：对抗提示下没有任何执行发生。模型会在正文"幻觉" `<invoke name="Bash">` 文本，建议 ClaudeAdapter 在**未声明客户端工具**时追加系统提示 "No tools are available in this session; answer directly and never write tool invocations." （`--append-system-prompt-file`），并在网关侧对输出中的 `<invoke` 片段计数做监控而非改写。
- 凭证：OAuth 凭证只存在 Worker home 卷；网关 DB/审计不记录授权码（`request_observation.sensitive_name` 已覆盖 `code`/`token`）。`setup-token` 仅测试使用。
- 合规提示：订阅凭证始终由官方 CLI 持有和使用，网关不直接调用 Anthropic API；这是能力边界（无法控制 max_tokens 等）的根因，也是刻意选择。

## 8. 测试与验收

已通过（测试环境，原型 Worker 容器，`claude-sonnet-5-5`）：

```
capabilities / account / probe(2.7s) / rate-limits(5h 9%, 7d 2%)
text turn 'PROBE-ONE'  usage {input 242, output 10}
resume turn 'PROBE-ONE' (同 session uuid)
tool turn: get_weather(Paris) → "It's sunny and 23°C in Paris right now."  tool_use_id=toolu_01FH…
image turn (16x16 蓝色 PNG) → 'Blue'
schema turn → {'city': 'Paris', 'population': 2100000}
invalid session id → 422; 无 Bearer → 401
cancel: 首个 delta 后断连 → 下一轮正常, 无残留进程
parallel ×3 → 3.9s 全部完成
login/start → stage=authorize + claude.com OAuth URL（容器内无浏览器）; cancel 正常
adversarial → 无执行
```

实施后必须补的验收：

1. 单测：`tests/test_claude_native.py`（仿 `test_gemini_native.py`：翻译函数 + ASGI 直通 + SSE 状态机 + 错误映射）、`tests/test_providers.py` 扩展 claude 能力规则、`tests/test_claude_backend.py`（mock Worker NDJSON：delta/client_tool/done/error 各一）。
2. 现有全量回归不得变化（Codex/Gemini 用例 0 修改）。
3. 测试环境 e2e：
   - OpenAI 路径：`scripts/validate_gemini_tools.py` 复制为 `validate_claude_tools.py`，模型改 claude，覆盖 Responses/Chat JSON+SSE、工具往返、`previous_response_id`。
   - 原生路径：`cd /tmp && ANTHROPIC_BASE_URL=http://127.0.0.1:8000 ANTHROPIC_API_KEY=<网关 Key> claude -p "列出当前目录文件并总结" --model claude-sonnet-5-5`（Claude Code 作为客户端，经网关 → Worker 内 CLI；Bash/Read 作为客户端工具往返）；以及 anthropic Python SDK `client.messages.create/stream` 文本、图片、tools、json_schema 用例。
   - UI：创建 Claude Worker → 登录对话框显示 OAuth URL → 粘贴授权码 → 探测通过 → 贡献 +1、套餐 `Claude · team` 显示、额度窗口显示。
   - 取消/断连、Worker 换账号后 `account_changed`、登出后绑定失效。
4. 未验证且需要专项：真实额度耗尽（`kind=limit` → 429 `provider_quota_exhausted` + Worker 冷却）、同会话并发 409。

## 9. 部署

- 镜像：`docker build -t codex-claude-worker:2.1.287 worker/claude`；worker-manager 环境加 `CLAUDE_WORKER_IMAGE`。
- 网关配置示例：`CODEX_GATEWAY_ALLOWED_MODELS=...,claude-opus-5-5,claude-sonnet-5-5,claude-haiku-4-5`，`CODEX_GATEWAY_MODEL_PROVIDERS=...,claude-opus-5-5:claude,claude-sonnet-5-5:claude,claude-haiku-4-5:claude`，可选 `CODEX_GATEWAY_CLAUDE_NATIVE_MODEL_ALIASES=claude-opus-5-5-20260401:claude-opus-5-5,claude-sonnet-5-5[1m]:claude-sonnet-5-5`。
- 迁移：无新表/列（`provider` 列已存在）；`model_prices` 需为 claude 模型配置价格（UI 已支持 provider 前缀）。
- 回滚：停用 claude Worker 后回滚网关镜像即可；保留 `*-claude-home` 卷。
- 测试环境现状：容器 `claude-worker-proto`（`127.0.0.1:4501`，Bearer 在 `~/claude-worker-build/.worker-token`），测试令牌 `~/.claude-worker-oauth-token`（0600，1 年有效，仅测试；验证完成后应删除并在 claude.com 撤销）。构建上下文 `~/claude-worker-build/`。

## 10. 附录：实测样本

Claude Code 作为客户端发出的请求（经本机抓包，已脱敏）：

```
POST /v1/messages?beta=true
anthropic-version: 2023-06-01
anthropic-beta: claude-code-20250219,interleaved-thinking-2025-05-14,thinking-token-count-2026-05-13,context-management-2025-06-27,prompt-caching-scope-2026-01-05,mid-conversation-system-2026-04-07,per-turn-control-2026-07-01,mid-conversation-tool-changes-2026-07-01,effort-2025-11-24
x-api-key: <key>   (ANTHROPIC_AUTH_TOKEN 时为 Authorization: Bearer)
user-agent: claude-cli/2.1.284 (external, sdk-cli)   x-app: cli
{"model":"claude-opus-5-5","messages":[...],"system":[{"type":"text","text":...},...],
 "tools":[{"name":"Agent",...},... 20 个],"metadata":{"user_id":"user_…_session_<uuid>"},
 "max_tokens":128000,"thinking":{"type":"adaptive","display":"omitted"},
 "context_management":{...},"output_config":{"effort":"medium"},"stream":true}
```

Worker 内 CLI 一轮的 stream-json 行序列（工具往返）：

```
system/init{session_id,tools:["mcp__client__get_weather"]} → system/status → rate_limit_event
→ stream_event: message_start, content_block_start(tool_use mcp__client__get_weather), content_block_delta(input_json_delta)…, content_block_stop, message_delta(stop_reason=tool_use), message_stop
→ [relay tools/call → /tool-result] → user(tool_result 回显)
→ stream_event: message_start, content_block_start(text), content_block_delta(text_delta)…, content_block_stop, message_delta(end_turn), message_stop
→ result{subtype:success, usage(累计), num_turns:2, session_id}
```
