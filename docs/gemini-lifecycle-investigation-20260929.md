# Gemini 生命周期实测与修复（2026-09-29）

范围：本机 Gemini CLI 0.61.0 与测试服务器 `<test-host>:8000`。未修改 Codex 推理、工具或会话逻辑，未部署生产环境。

## 实测发现和修复

1. **网络失败后 resume 找不到会话。** 请求已记录用户输入，但失败分支 `agentHistory.rollback(historyLengthBefore)` 再通过 `$set.messages` 从持久化记录移除该输入。新会话因此只剩系统上下文，SessionSelector 不再认为它可恢复。修复为回滚到本次输入已经提交的边界，保留用户输入和已完成工具结果。取消分支使用同一边界。没有自动重跑工具。
2. **空响应报告错误却退出 0。** 非交互分支输出 `status:error` 后正常返回，主入口强制 `process.exit(SUCCESS)`。现设置失败退出码并由主入口保留。模拟空响应验收退出码为 1。
3. **SSE 错误详情丢失。** SDK 只在网络块整体为裸 JSON 时识别 `error`，没有检查 SSE `data:` 解帧后的错误对象；转换后丢失错误字段，表现为 `Model stream ended without a finish reason`。现对解帧后的错误抛出 ApiError，保留 HTTP 类错误码和后端信息。模拟 502 验收保留 `LIFECYCLE_BACKEND_FAILURE`，不再出现无 finish reason；退出非零。
4. **agy 返回空 SUCCESS。** 本轮长输出恢复产生四条 200、零输出文本、零输出 token 的记录。直接调用测试 Worker 内 agy 也复现：只有 user_input DONE，然后 SUCCESS / response 空 / duration_seconds 0；对应会话数据库却存在答案。单次 JSON 模式同样复现，因此不是仅网关的 SSE 转换问题。当前只修复 Worker 的误判成功：没有文本且没有客户端工具调用时明确返回后端错误。**agy 空输出根因及恢复答案的上游修复尚未完成。** 不自动重放含工具副作用的请求。

## 已完成的验证

- 新会话、指定 UUID resume、resume latest：正确保留随机标记。
- 请求开始后 SIGINT、SIGKILL、SIGHUP，再 resume：正确保留输入。
- 文件读取后连续三次 resume：保留随机标记，没有再次调用工具。
- shell 工具完成瞬间强退，再 resume：恢复工具结果；磁盘执行计数保持 1。
- SSE 连接切断：修复前退出 1，随后 resume 42；修复后恢复原 UUID 并准确返回先前标记。
- 模拟空响应：修复后错误退出 1；会话恢复成功。
- 模拟 SSE 502：保留真实错误详情，非零退出。
- 真实交互终端：启动、确认新输入已提交、Esc 取消、继续、/quit 通过。
- 保留默认父子进程启动器的真实终端 resume 与 /quit：通过，恢复取消前标记。
- 约 14 万字符的多样化合成日志：首轮成功得到末尾标记，随后两次 resume 均成功且无工具调用。首轮模型除一次 cat 外额外调用四个只读工具（并未遵守“只用一次工具”约束），因此不能把首轮计作严格工具遵循通过；它与 resume 重复执行不是同一问题。
- Worker 单元回归 38 项通过，包含空 SUCCESS 不得输出 done 的新增用例。部署后真实 40,000 字符合成输入验收返回明确 error、没有 done，确认不再误报空成功。

首次交互自动化使用 LF，没有真正提交后续输入；该次取消/继续测试不计通过。后续改为分别发送文本与 CR，并验证会话记录，完成有效实测。长重复字符工具输出的初次执行成功，但恢复失败，不能计为通过。

## 交付及回滚

本机补丁：`scripts/gemini-cli-lifecycle.patch`。适用于本机已带既有修复的这组 0.61.0 bundle 文件，升级后不能盲目套用。原文件备份：`~/.gemini/repair-backups/lifecycle-20260929`。已启动的旧 Gemini 进程需要退出重开才能加载补丁。

测试镜像：`codex-antigravity-worker:gemini-lifecycle-20260929`，基于原 `gemini-images-20260929-160106`，仅替换 `/opt/service.py`。三个在用 Gemini Worker 均完成能力健康检查，旧容器保留 `-before-lifecycle` 后缀。测试 worker-manager 默认 Gemini 镜像同步更新；网关和 Codex Worker 未重建。

测试部署备份：`/home/<deploy-user>/deploy-stage/gemini-lifecycle-20260929`，包含原容器 inspect（含敏感配置，权限 600）、状态文件和 Compose 备份。回滚需先停止新版并断开网络，再恢复旧容器名称、网络别名并启动，避免两个同名网络别名同时生效。恢复默认镜像时使用 `compose-before.json`，只重建 worker-manager。

本轮 CLI 日志、断流注入记录和测试结果在 `/tmp/gemini-lifecycle-20260929`。这不是压力容量测试；不声称穷尽所有网络、并发和工具竞态。

初始检查发现用户正在运行的旧 Gemini 进程仍指向 `https://gateway.example.com`；本轮所有真实推理测试显式设置测试地址，未改用户全局连接配置。
