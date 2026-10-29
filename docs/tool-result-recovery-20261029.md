# Tool result continuation investigation (2026-10-29)

## Production evidence

Read-only audit queries found 267 client_tool_call_unavailable records in the rolling seven-day window at investigation time: Claude 245, Codex 22. Counts are requests, including retries, not distinct incidents. Matching returned call IDs to issuance records under the same API Key found 56 results arriving over 300 seconds after issuance, 202 within 300 seconds, and 9 without a matching issuance in the eight-day lookup window. These timings alone do not establish the cause of every record.

For req_d31ffd8e271a45dc8139995c1fbb23f6:

- 2026-10-27 03:52:23.394401 UTC: chatcmpl-ddb544ddbb6a4af5ba906b511f57dd3e issued task call call_85322cb894944f6e8d704b1a134b925b.
- 03:57:23.372719 UTC: app-2 logged Client tool result expired, elapsed_seconds=300.051, ttl=300 for that exact call ID.
- 03:58:25.574077 UTC: the result reached app-1 and was rejected in 162 ms.
- Expired cluster routes no longer forward to the owning node. The receiving node has no local pending RPC or retirement reason, so it reports unavailable rather than expired.
- The request had no explicit client conversation identity (method=isolated), which made existing checkpoint-based recovery ineligible.

Other confirmed sequences:

- req_9efede9a08564a1783d6651f79904212: the original tool result was accepted, then model_capacity_exceeded terminated the execution. A context-only recovery also encountered model capacity, and another retry of the old result failed with unavailable.
- req_c965612a53884a0c8a69524d805de10b: a context-only replacement completed after the tool was issued; subsequent old-result retries failed with unavailable. Thus not all occurrences are timeout failures.

## Recovery change

Normal pending-RPC routing and verified checkpoint recovery retain priority. After unavailable/expired/cancelled, requests with a complete captured body, user context, and exactly one matching call/result pair can instead run an independent context-only answer.

The independent request clears tool declarations (including additional_tools), sets tool_choice=none, appends a continuation request and recovery notice, and starts without a previous response or execution binding. It does not claim, cancel, or overwrite the original conversation checkpoint. The model is instructed to use existing results, explain uncertainty, and avoid repeating tools.

Requests with explicit previous_response_id, missing/ambiguous history, duplicate results, changed tool contracts, incomplete capture, or disabled execution recovery retain existing validation. This does not guarantee success when the model itself is unavailable.

Both Responses and Chat Completions, streaming and non-streaming, retain their normal response formats. Audit records retain original client parameters and record detached_tool_recovery plus its triggering error code.

## Deployment attribution

The earlier production release was recovery-20261026-test1, deployed on October 27 at 01:54–01:56 UTC, approximately two hours before the original error, not two hours before this investigation.

The local Codex session 01a1779b-2d4c-7760-bebe-6e6dabcc2ab8 contains the user instruction at October 26 11:26:46 UTC: “部署到正式环境吧，注意正式环境重启期间本会话会中断”. Its tool record at October 27 01:54:16 UTC launched the retry deployment through a user systemd task. Both node status records passed. Execution-account details remain in private environment/session records; those records do not independently authenticate the natural person using the account.

This investigation and source change do not deploy or restart production.

## Validation

- 142 tests passed across detached recovery, existing tool recovery, execution and client-tool protocol tests, using a dedicated local PostgreSQL database.
- Two pre-existing cases in test_streaming_continuation_and_pending_tools expect 409 for a new user turn but receive the existing context_only_recovery 200 response. Both failures were reproduced with the new fallback disabled; they were excluded from the passing run, not changed.
- New coverage includes both public endpoints, streaming/non-streaming responses, original audit preservation, no database access for detached execution preparation, normal live-RPC priority, custom/function tools, and refusal of ambiguous or incomplete histories.
- In-memory offline replay of the three production requests above selected detached recovery with tools disabled. No model or client tool was invoked by that replay, and no production state was changed.
- git diff --check passed. Real model generation and production rollout remain unperformed.


## 测试环境部署验收（2026-10-29 UTC）

- 已部署测试版本 `detached-tool-20261029-test1`，gateway / manager 镜像 ID 为 `sha256:e2fb1f5545c3cf1502b49cd27e24427760a19fd52b06859fc877dcc866c60c98`。
- 238 个部署文件及两个服务运行包哈希通过，healthz 和登录页正常，5 个受管 Worker 容器 ID、镜像和启动时间不变。
- 真实 Codex 模型：Responses、Chat Completions 各自流式与非流式共 4 项通过。使用随机缺失 call ID 和合成 lookup 结果，完整历史恢复返回 HTTP 200、正确结果标记，没有再次调用工具；4 条审计均确认 detached_tool_recovery 且 tools_disabled=true。
- 初始验收脚本使用 Codex 不支持的 required 工具选择，被参数校验拒绝；改为 auto。流式断言改为拼接 SSE 文本块，最终四项全部通过。未为验收修改业务逻辑。
- 临时验证 Key 和用户均在 finally 中禁用。验收仅覆盖真实 Codex；Claude 和跨节点线上故障未在此次部署中注入验证。
- 发布包、status.json 和 smoke-r4.log 位于测试主机及本机 `/tmp/codex-detached-tool-20261029-test1`。测试备份目录后缀为 `release-20261029T034518Z`。
- 正式环境未部署。
