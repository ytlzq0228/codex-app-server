# Gemini CLI 问题调查（2026-09-28）

正式服务器：<app-1>（本轮只读调查）。测试服务器：<test-host>:8000。
本机 Gemini CLI 0.61.0，隔离 HOME 和工作区：/tmp/gemini-local-diagnostics。用户原有配置未修改。

## 已确认原因

- 14:44:36 UTC：SessionSummaryService 调用 gemini-3.5-flash-lite，返回 model_not_found。对应审计 req_9484b23fb49f4b02b99e86a5ec09f233 和本机 generateContent-api 日志。
- 14:45:09–14:46:18：5 次 client_tools_unavailable 全部绑定 contrib-bd4084f0b063472a992ad3f78576a735。上次生产发布遗漏第二个 Gemini Worker；本次实测其 /capabilities 返回 404。
- 14:46:14：chatcmpl-af144896549e4a7c8bda7a2c018ef862 返回 worker_capacity_exceeded。新 Worker 当时没有执行容量；512 个 ToolSessions 不等于 512 个推理槽。
- 14:48:47、14:49:39：LoopDetectionService 请求 gemini-3-flash-preview 的 JSON Schema 输出，被原生转换层拒绝。客户端与网关日志时间一致。
- 14:50:32：req_90834f11ad6f46528a131bd143839096 为 499 request_interrupted；记录不足以确定客户端断开原因。

上述时间段新 Worker 另有 46 条成功请求。SSE HTTP 200 仅表示流建立，需要结合审计与客户端判断最终结果。原生转换拒绝发生在 Chat 审计之前，需同时检查网关日志。

## 修复

1. 原生 application/json、responseSchema、responseJsonSchema 转为现有输出格式。Gemini prompt 加入 schema，完整输出经过 JSON/schema 校验后才交付；无效输出明确报错。允许移除完整 Markdown 围栏。这不是模型原生约束解码，不保证模型总能满足 schema；暂不支持同时声明工具与结构化输出。
2. 测试环境补充 gemini-3.5-flash-lite、gemini-3-flash-preview 到 gemini-3.8-flash-high 的显式别名。原有两个别名保留。辅助调用实际使用 high 模型，响应头 X-Gateway-Model 报告实际模型，产生相应成本和耗时。
3. 本机恢复会话测试发现 CLI 在同一历史消息中重复序列化 functionResponse。同 ID、同名、同内容结果去重，内容冲突仍拒绝。修复前 --resume latest 报 Function response has no matching call in history；修复后无需重新读文件即可返回正确随机标记。
4. scripts/check_gemini_worker_capabilities.py 从数据库枚举所有未删除 Gemini Worker，逐个检查能力。运行于 gateway 容器，无推理、无状态修改，任一失败返回非零。

## 发布范围与回滚

本轮仅更新测试环境，正式环境旧 Worker 仍需升级。后续发布必须枚举两个 Gemini Worker 并保留各自账号与工作区卷。修改 manager 默认镜像不会升级现有容器。

测试镜像：codex-gateway:gemini-diagnostics-fix-r2。
覆盖文件：/home/<deploy-user>/codex-app-server/compose.gemini-test.json。
原覆盖文件备份：/home/<deploy-user>/deploy-stage/gemini-diagnostics-fix/compose-before.json。
回滚时恢复该文件，只重建 gateway。

## 验证

114 项相关回归测试通过，包括 OpenAI 工具逻辑、Gemini 工具回传、schema 正反例和重复结果冲突。
本机最终测试结果：/tmp/gemini-local-diagnostics/results.json。
原始循环检测回放：/tmp/gemini-local-diagnostics/original-loop-replay.json。
首轮恢复失败和第二轮网关尚未就绪的记录独立保留，不计入最终通过。

### 最终验证结果（15:03–15:05 UTC）

- 本机 CLI 连续读取：3 次模型请求，2 次工具调用成功。
- 恢复会话：1 次模型请求，无工具调用，正确返回此前随机标记。
- 读取、写入、再次读取：4 次模型请求，3 次工具调用成功，磁盘内容精确一致。
- 上述 8 次 CLI 请求 API 错误数为 0，进程退出码均为 0。
- JSON Schema、摘要别名及原始循环检测历史回放通过；辅助服务使用 API 回放验证，并非强制触发 CLI 后台服务。
- 对应网关日志均为 200，检查期间未见 ERROR/WARNING；客户端确认流完成。健康检查与唯一 Gemini Worker 能力检查通过。
- 正式环境未应用本轮修改；容量问题及 499 原因不视为已修复。

## 补充：OAuth、499 和执行连续性

### 已确认的 499 归属

正式环境 req_90834f11ad6f46528a131bd143839096 的请求含 94 条转换后消息、46 个工具结果，最后一条为工具结果。
其最后一个 call_id 在相同 API Key 下唯一对应 chatcmpl-7e5a7ea43b624d0181008dee9a2ad11d，
Thread 为 fc8a9064-8672-4ef9-a2a0-ed991f65c80c。原记录没有 logical_conversation_id 或 thread_id，
因而历史页无法聚合。没有对正式环境历史记录进行回填。

本机 CLI 0.61.0 的这些请求没有 thread-id/session-id。成功记录依靠 Worker Thread 聚合；
工具结果通过 API Key + call_id 认证后继续同一悬挂任务。原审计回退分支未保留已认证 Thread。
本轮本地修复在工具关联通过后及流事件到达时记录 Worker/Thread，并在取消屏蔽区写入 499。
这只修复可证明归属的中断，不根据 IP、相似文本或模型名猜测会话。

### 连续性的边界

- 逻辑客户端会话、Worker Thread、一次 HTTP 请求是三个不同概念。
- 当前这段工具链跨多次 HTTP 请求复用同一个后台执行；成功执行的客户端工具不会因 499 被追溯改判失败。
- 没有显式客户端身份时，新的普通用户轮次依靠完整历史重放；不保证复用上轮 Worker Thread。
- 中断发生在等待模型输出时，ToolSessions 会取消后台执行，防止无人接收的任务继续运行。
  工具结果一旦接受即记录为已提交，不能盲目重试，否则可能重复产生副作用。
- 工具边界保留待回传任务；有效期默认 300 秒，网关重启后内存任务丢失。
  512 是全局会话容量，同一 Key 默认最多 8 个悬挂任务，与 Worker 推理槽数不同。
- 499 只表明交付中断；无法据此确定客户端主动退出、网络断开还是其他原因，也不能证明模型没有执行。
- 辅助摘要/循环检测请求并不自动归属主对话，不宜以同一 Key 或相同时间强行混为一个执行会话。

### OAuth 排查与本地修正

授权码原来与回车一次写入 PTY；这有被交互 CLI 当作粘贴而未提交的风险。
本轮拆分文本和 Enter，增加输入串行化、防重复提交以及提交后的等待状态，避免界面仍显示旧授权表单。
该改动已通过模拟终端写入测试，但尚未完成真实 Google OAuth 成功登录验收。

另一个待确认点是完成判断要求同时出现 email(plan) 和 GCP Project。
Google OAuth 与 Google Cloud 登录可能有不同完成画面；在拿到发生问题的 Worker/时间前，
尚不能认定这是本次卡住的原因，因此未降低账号确认条件。
本轮新增修改仅在本地，尚未部署测试或正式环境。
