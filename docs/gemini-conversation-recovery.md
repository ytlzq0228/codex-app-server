# Gemini 已完成会话的恢复

2026-10-03 会话 `conv_719adafcfcf0926791bdd79f6f7832fd3864bf1704b641c14437a3c31bf204cb` 的既有排查记录显示：5 次成功请求后，客户端更新了工具定义，并调整了 assistant 文本与历史工具调用的位置。配置哈希及历史前缀不再匹配，原来的 Gemini 限制导致相同请求连续收到 `conversation_resume_unavailable`。

本次允许处于 `ready` 且没有执行租约的 Gemini 会话，在无法续接时创建新的底层会话。请求须有至少两条 user 消息、既有 assistant 对话，实际对话末尾须为 user（允许后附 system/developer 提醒）。历史工具调用必须具有唯一 ID，并带有同类型的唯一结果；缺失结果、孤立结果和重复结果均拒绝恢复。

新的底层会话使用客户端提交的全部历史和当前工具定义，不设置旧的 `previous_response_id`，也不把历史工具结果提交给旧的挂起执行。沿用现有 Gemini 后端的文本上下文转换。此检查确认历史结构自洽，不声称能从哈希证明客户端提交了未经修改的完整历史；历史不匹配时始终创建新会话。

执行中的租约、等待工具的恢复验证、跨 provider 限制和显式续接路径保留。`invalid` 会话不在此次 Gemini 恢复范围内。审计继续记录 `new_thread` 及具体原因（例如 `configuration_changed`、`history_not_append_only`）。

测试包含配置更新、历史重排、正常续接、绑定失效、Worker 不可用及非法工具历史。单元与集成测试使用隔离数据库和模拟后端；尚未进行真实 Gemini 模型验收或环境部署。
