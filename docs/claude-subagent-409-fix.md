# Claude Code 多代理 409 修复

状态：已实现并通过测试环境部署验收（2026-10-02）。仅部署测试服务器 `<deploy-user>@<test-host>`。

## 已核实的根因

本机 Claude Code 2.1.287 的会话 `7288649f-49b2-4f3a-b1bc-422ef8bcd719` 在四个子代理之间共享 session ID，但分别发送稳定的 `x-claude-code-agent-id` 请求头。该值与本地 subagents JSONL 的 agentId 一致。

服务端审计记录确认，四个 agent 的 83 次请求全部 409，且主线程、辅助请求和子代理均归入 `conv_d75f529a…`。主线程运行期间返回 busy/waiting_tool，之后返回 resume_unavailable。当前网关容器于 15:34 UTC 重建，故障时段证据来自数据库审计及本机日志。

原方案仅用 instructions + 首条输入生成分支指纹，现由明确的 agent ID 取代。相同 prompt 的独立 agent 不再碰撞，压缩、instructions 和 tools 变化也不改变分支身份。

## 实现

- Claude 身份包含原有 API key、endpoint、来源及 installation 范围，再加入 session 和显式 agent ID；主线程使用独立的类型标记，不与名为 main 的 agent 碰撞。
- session 同时支持 `x-claude-code-session-id`、JSON `metadata.user_id.session_id`、旧式 `user_…_session_<uuid>`，以及 SDK 的自定义 user_id。JSON UUID 规范化；多个来源不一致仍报告 identifier_conflict。
- `conversation_evidence.client_thread_ids` 保留 session；新增 `client_agent_ids`。用量与执行调用同一身份函数。
- 没有 agent ID、没有工具且要求结构化输出的请求划入 `claude_auxiliary` 审计组，不占用执行会话 lease/checkpoint。此规则是针对已观察到的辅助请求的兼容策略，也适用于同形状 SDK 请求。
- 对 Claude，非追加历史、配置变化、绑定失效或上次执行失败时，可在输入可归一化、忽略尾部 system/developer 提醒后以 user 消息结尾且不是待提交工具结果的情况下，以请求中的完整历史新建 CLI 会话。不会自动补回客户端省略的历史；客户端应发送完整历史或压缩后的自包含历史。
- 工具结果继续依赖 key 范围内认证的 call_id 和 pending thread，agent 不一致仍拒绝。工具结果后附 system/developer reminder 也不能被当作新 user 轮次重建。
- Gemini 保留原有重建限制。正在等待工具的会话仍使用原有等待、取消及 supersede 规则。

## 边界

旧客户端如果不发送 agent ID，服务端无法仅凭相同内容准确识别独立子代理；本次不将内容指纹伪装成可靠身份。已有 pending tool 的续接需保留一致的显式 session/agent 身份；完全省略身份时仍可由已认证 call_id 恢复。

工具结果提交期间不允许改变 tools，保留现有 ToolSessions 校验；ToolSearch 动态更新挂起 CLI 的工具定义不属于本次修复。普通 user 新轮次变更 tools 可重建。

升级会改变 Claude 的 logical ID，历史审计记录保留原值。正在等待的旧工具会话可能需要重新开始 user 轮次；部署后使用新 session 验收。

## 测试与部署

测试覆盖新旧 JSON/session 格式、key/endpoint 隔离、重复身份冲突、相同 prompt 的多个 agent、lease 并发、waiting_tool 隔离、认证工具续接及跨 agent 拒绝、重复辅助请求、Claude 普通多轮 resume、历史/配置/绑定/worker 变化后的重建，以及 Gemini 与工具结果的拒绝行为。

先运行相关 pytest，再按 operations.md 构建完整镜像并备份测试部署。部署仅更新测试 gateway/manager，不更新 Worker 镜像或正式环境。验收检查 healthz、镜像版本、四个子代理分组及正常完成、工具结果续接和普通多轮 resume。回滚使用测试服务器保留的部署备份。

### 本次验收结果

- 独立测试数据库中 137 项相关 pytest 通过。
- 测试 gateway/manager 镜像：`codex-gateway:claude-subagent-409-20261002`，image ID `sha256:360e0650b0f9f2b50552557bfe6d62e0e5841dad0c271fa343782561482753f0`。
- 发布文件 168 个、每个容器安装包文件 96 个通过 SHA-256 比对；healthz 返回 200。
- 真实原生 Messages API 验收 session：`925ff1e6-d160-46f9-bf3e-3ae8db0920b3`。使用模拟客户端发送 session/agent 请求头，真实模型为 `claude-opus-5-5`，工具结果为固定测试数据；未执行模型生成的代码。
- 主线程等待工具时，4 个同 prompt 子代理并发完成。主线程和各子代理共 5 次 tool_continuation 成功；额外文本会话第二轮确认 resume。共 12 次请求全部 200、6 个逻辑会话（主线程 + 4 子代理 + 文本验证会话）。临时验收 Key 已禁用。
- 测试服务器备份：`/home/<deploy-user>/codex-app-server/deploy-backups/release-20261002T160341Z`；发布一致性记录：`/home/<deploy-user>/codex-app-server/claude-subagent-deployment-verification.json`。
- 正式环境未部署；本次未驱动本机 Claude CLI 再次执行完整的项目审查任务。

## 第二轮修复：辅助请求与压缩续接（2026-10-02）

真实 CLI 会话 `787c1119-f96a-4b8f-965d-623fc966f13c` 的 7 个 agent 已正确隔离，但采样中 56 次 busy 有 54 次来自状态文案，另外 2 次来自主线程任务完成通知重叠。2 次 waiting_tool 也来自状态文案。压缩摘要请求覆盖正常检查点后，压缩历史末尾 developer 提醒触发 resume_unavailable。

- `claude_helpers.py` 仅识别 Claude Code 2.1.287 客户端特征及末尾完整模板的空白归一化 SHA-256，分别记录 status_summary/context_compaction 和规则版本。不根据历史关键词匹配；这些头不是认证凭据。74 次已捕获状态文案均匹配该模板。未来 CLI 模板变化需要新样本和回归测试，未知模板仍走普通处理。
- 已识别辅助请求保留 session/agent 审计归属，但采用独立辅助分类和新 Worker 会话，不获取正常执行锁、不更新正常检查点、不取消 pending tool。执行副本禁用工具，原始请求保持在审计中；有显式 continuation 或认证 pending tool 时不走此旁路。
- Claude 在 ready/invalid 等允许重建的路径上判断实际对话末尾，保留并传递 system/developer 提醒。工具输出仍使用认证 call_id；waiting_tool 的历史验证、取消和 supersede 条件未放宽，Gemini 限制不变。
- 普通 Claude 请求遇到 busy 后最多等待 5 秒，每个会话每进程最多 8 个等待者，全进程最多 128 个；跨节点仍由数据库 lease 保证互斥。等待释放数据库事务和请求连接，再读检查点并重新验证 pending tool。超时保持 409/Retry-After，取消/断连移除等待者。显式 previous_response_id 继续立即处理原有冲突，不纳入等待。
- 设置：`CODEX_GATEWAY_CLAUDE_EXECUTION_WAIT_SECONDS`（默认 5，0 禁用，最大 30）；`CODEX_GATEWAY_CLAUDE_EXECUTION_MAX_WAITERS`（默认 8，0 禁用，最大 64）。执行审计记录成功等待的 wait_ms。
- 169 项相关测试通过（150 项会话/Claude/工具测试，19 项 API/cluster 测试），包含真实模板、误分类、运行/等待工具/ready 检查点不变、尾部提醒、等待成功/超时/取消/断连/限额和状态重检。仅有已有 Starlette 测试客户端弃用警告。
- 发布名：`claude-helper-409-20261002-r2`。部署目标仅测试环境；部署验收记录单独保存在测试服务器应用目录。

## 2026-10-03：Worker 排队和 2.1.288 兼容

- Worker 执行上限从 4 调整为 8；每个 Worker 最多 32 个 FIFO 等待请求，30 秒超时。排队期间不启动 CLI；断连、取消和超时释放等待位置。重复的执行中或排队会话立即拒绝。维护操作视排队为忙，会话清理保留排队会话。
- 队列满使用 `worker_capacity_exceeded`，排队超时使用 `worker_queue_timeout`（内部 503、Claude 原生 529）；重复执行或维护冲突使用 `worker_execution_conflict`（409）。连接超时使用 `worker_connection_timeout`，读写传输超时使用 `worker_transport_timeout`（504），不自动重放可能已接受的请求。
- 正式日志中的 2.1.288 状态摘要完整指纹与旧版相同，启用该版本的 `status_summary`。2.1.288 压缩模板尚未验证，不启用其辅助旁路。未知版本和未知模板仍按普通请求处理。
- 以上为代码变更记录；运行中的 Worker 需要更新镜像才会生效。
