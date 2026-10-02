# Claude Code 多代理 409 `conversation_resume_unavailable` 整改方案

状态：待实现（2026-10-02 诊断完成，未改代码）
范围：`src/codex_gateway/conversations.py`、`src/codex_gateway/execution.py`、相关测试与文档

## 1. 现象

Claude Code 客户端（`ANTHROPIC_BASE_URL` 指向网关）在一个会话里并行启动 4 个子代理（Agent 工具）后，所有子代理都在 10 次重试后失败：

```
API Error: 409 Provider conversation cannot be safely resumed; start a new conversation
```

主线程本身能正常推进。单代理会话里，客户端的辅助请求（1 条消息、0 个工具、带 `text` 结构化输出 schema，疑似标题生成）同样会被 409，只是失败不显眼。

## 2. 证据

### 2.1 客户端

会话 `7288649f-49b2-4f3a-b1bc-422ef8bcd719`（工作目录 `<ticket-system-repo>`）：

- `~/.claude/projects/-home-ytlzq0228-Projects-<ticket-system-repo>/7288649f…/subagents/agent-*.jsonl`：4 个子代理的 `sessionId` 全部等于父会话 ID。
- 4 个子代理没有一条成功的 assistant 消息，15:22 UTC 前后全部以上述 409 结束。

### 2.2 网关（测试服务器 `<deploy-user>@<test-host>`，库 `codex_gateway`）

- `codex-app-server-gateway-1` 日志：大量 `POST /v1/messages?beta=true HTTP/1.1" 409 Conflict`。
- `metadata.user_id` 实际格式是 **JSON 字符串**：`{"device_id":"…","account_uuid":"","session_id":"7288649f-…"}`。设计文档 §6.4 里写的 `user_<hash>_account_<id>_session_<uuid>` 是旧格式。
- 15:18–15:20 所有 Claude 请求的 `logical_conversation_id` 都是 `conv_d75f529a…`，`execution_sessions` 表里只有这一行。
- 按 `md5(instructions)` / `md5(input[0])` 区分，可以看出是三条不同的对话：

| 对话 | 特征 | 结果 |
|---|---|---|
| 主线程 | instructions `dff6…`，32 个工具，`input[0]` 固定为 `3cfd…`，input 长度 2→4→7→…→18 递增 | 200 |
| 辅助请求 | instructions `dadd…`，0 个工具，1 条 input，带 `text` | 第一次 `conversation_resume_unavailable`，之后一直 `conversation_busy` |
| 4 个子代理 | instructions `d9bb…`，17 个工具，`input[0]` 各不相同 | 主线程跑的时候是 `conversation_busy` / `conversation_waiting_tool`；主线程结束后是 `conversation_resume_unavailable` |

复查用的 SQL：

```sql
select to_char(created_at,'HH24:MI:SS') t, status_code, error_code,
       json_array_length(request_params->'input') n,
       md5((request_params->'input'->0)::text) first_item,
       md5(coalesce(request_params->>'instructions','')) instr,
       json_array_length(request_params->'tools') ntools
from usage_records
where provider='claude' and created_at between '2026-10-02 15:18:40+00' and '2026-10-02 15:19:50+00'
order by created_at;
```

在宿主机上执行：`docker exec -i codex-app-server-db-1 psql -U codex -d codex_gateway`。

## 3. 根因

1. **会话 ID 只由 session_id 决定**：[conversations.py:46-51](../src/codex_gateway/conversations.py#L46-L51)。Claude Code 的主线程、子代理和辅助请求共用同一个 `session_id`，所以被合并成同一个 `logical_id`。另外，这里的正则 `session_<uuid>` 匹配不上新的 JSON 格式，现在是靠"把整个 `user_id` 当会话 ID"这条兜底逻辑碰巧生效的。
2. **同一会话的 lease 是独占的**：[execution.py:142-143](../src/codex_gateway/execution.py#L142-L143)。合并之后，5 条对话只能串行执行，抢不到 lease 的直接 409 `conversation_busy`。`waiting_tool` 状态也会挡住其他对话（`conversation_waiting_tool`）。
3. **非 codex provider 不允许新开 thread**：[execution.py:226-227](../src/codex_gateway/execution.py#L226-L227)。子代理的历史不是主线程 checkpoint 的追加，所以 `reason=history_not_append_only`，`action=new_thread`，最终 409 `conversation_resume_unavailable`。辅助请求重复发送时（`input` 与 checkpoint 相同、delta 为空）也会走到这里。

## 4. 修改方案

### 4.1 会话 ID 加入"对话根"指纹（主修复）

位置：`explicit_identity()`，[conversations.py](../src/codex_gateway/conversations.py)。

只对 `provider_for(model) == "claude"`、并且会话 ID 来自 `metadata.user_id` 的请求生效：

```
root = digest({"instructions": params.get("instructions"),
               "first": normal_item(第一条非 additional_tools 的 input 项)})
thread key = f"{session_id}:{root}"
```

要求：

- **不要把 `tools` 放进指纹**。Claude Code 会在会话中途通过 ToolSearch 等方式增加工具，tools 变化应继续由 `configuration_changed` 处理。
- 第一条输入项要用 `execution.normal_item()` 归一化后再 digest，忽略 `cache_control`、ID 之类的传输字段。注意循环导入：可以把 `normal_item` 下沉到不依赖 execution 的模块，或在函数内延迟 import。
- `instructions` 或 `input` 缺失时，退回只用 session_id（保持现有行为）。
- `evidence` 里加一个字段（例如 `conversation_root`，存 digest 前 16 位）便于排查；`client_thread_ids` 仍保存原始 session_id，不要放拼接后的 key，以免影响后台展示。
- `correlate()` 也调用 `explicit_identity()`，所以修改后用量记录也会按主线程和各子代理分组。这是预期效果，需要在 PR 说明里写明。

效果：

- 主线程：instructions 和第一条输入在整个会话内不变（实测 `3cfd…` 不变），可以继续 resume。
- 每个子代理：instructions 或第一条输入不同，各自独立的 `logical_id`，不再互相加锁。
- 辅助请求：独立的 `logical_id`。
- 上下文压缩后第一条输入会变：得到新的 `logical_id`，`state=new`，正常新开 thread，不再 409。

已知限制：两个子代理的 prompt 完全相同（instructions 和第一条输入都一样）时仍会合并成一个会话并串行执行（`conversation_busy` 后客户端重试），但不会再出现永久性的 `resume_unavailable`。这个限制可以接受，写进文档即可。

### 4.2 放宽 non-codex 的 new_thread 409

位置：[execution.py:226](../src/codex_gateway/execution.py#L226)。

当前逻辑：`provider != "codex" and row.state != "new" and action == "new_thread"`，命中就 409。

改为：只有在**新开 thread 会丢失语义**时才拒绝，也就是请求末尾是工具结果（`function_call_output` / `custom_tool_call_output`），需要接回原 CLI 会话里挂起的 tool call 时。其他情况，例如完整历史且最后一项是 user 消息、或者 `history_not_append_only`、`configuration_changed`、`binding_invalidated`、`worker_unavailable`、`worker_account_changed`、`pinned_worker_changed`、`previous_execution_incomplete`，都允许走 `new_thread`，把全量历史平铺进新会话，与未跟踪请求的处理方式一致（见 [claude-integration-design.md §6.4](claude-integration-design.md)）。

实现前先确认：

- 查看 `git show 871147d -- src/codex_gateway/execution.py`，弄清这条限制最初是为 Gemini 加的哪种场景。如果 Gemini 确实需要保持原状，就只对 `claude` 放宽，并在代码注释里写明原因。
- 确认 `claude_backend.py` 在 `previous_response_id` 为空时会生成新的 `session_id`（[claude_backend.py:46](../src/codex_gateway/claude_backend.py#L46)），并把平铺后的历史作为输入。

### 4.3 兼容新旧两种 `user_id` 格式

位置：[conversations.py:46-51](../src/codex_gateway/conversations.py#L46-L51)。

解析顺序：

1. 先尝试 `json.loads(user_id)`；如果结果是 dict，且 `session_id` 是合法 UUID，就取它（转小写）。
2. 否则用现有正则匹配 `session_<uuid>`。
3. 都不匹配时，继续把整个字符串作为会话 ID（兼容自行传 `metadata.user_id` 的 Anthropic SDK 用户）。

同时更新 [claude-integration-design.md](claude-integration-design.md) §6.4 和第 313 行示例，写明新的 JSON 格式。

## 5. 测试

在 `tests/test_claude_native.py` / `tests/test_conversations.py` 补充（可参照现有 `test_session_identity_is_key_scoped`）：

1. **JSON 格式的 user_id**：`{"device_id":"d","account_uuid":"","session_id":"<uuid>"}` 解析得到 `client_thread_ids == [uuid]`；旧格式测试保持通过。
2. **同一 session、不同对话根**：instructions 不同，或第一条输入不同，得到的 `logical_id` 不同；同一对话追加轮次，`logical_id` 不变。
3. **tools 变化不改变 `logical_id`**：只是 `configuration_changed`，并且按 4.2 允许新开 thread，不返回 409。
4. **并发**：同一 session_id 下，主线程拿着 lease（或处于 `waiting_tool`）时，子代理请求不应返回 `conversation_busy` 或 `conversation_waiting_tool`。
5. **重复的辅助请求**：相同的单条输入连续发两次，第二次不返回 409。
6. **工具结果不能被平铺续接**：末尾是 tool output、但原会话无法 resume 时，仍返回 409（保留安全性）。
7. 现有 `tests/test_conversations.py`、`tests/test_claude_native.py`、`tests/test_claude_worker.py` 全部通过。

## 6. 测试服务器验收

1. 按现有镜像的命名惯例构建（部署步骤见 [operations.md](operations.md)），tag 形如 `codex-gateway:claude-subagent-409-<date>`，替换 `codex-app-server-gateway-1`。
2. 本机 Claude Code 指向网关，在 `<ticket-system-repo>` 里让主会话并行启动 ≥4 个子代理，各自读几个文件并回复。
3. 通过标准：
   - 所有子代理正常完成。
   - `usage_records` 中该 session 的请求分布在多个 `logical_conversation_id` 上（主线程 1 个，每个子代理各 1 个，辅助请求各自独立）。
   - 不再出现 `conversation_resume_unavailable`；`conversation_busy` 只允许出现在 prompt 完全相同的子代理之间。
   - 主线程多轮对话仍然走 resume（`execution_decision.action == "resume"`），说明没有退化成每轮新开 thread。

## 7. 临时应急（可选，未验证）

把网关环境变量设为 `EXECUTION_RESUME_ENABLED=false`（[config.py:39](../src/codex_gateway/config.py#L39)），`prepare()` 会直接跳过会话跟踪，每轮都新开会话并平铺历史。代价是用不上 CLI 的 resume（更慢、token 更多），而且工具结果的续接是否受影响**没有验证过**。只建议在修复上线前短时间使用。
