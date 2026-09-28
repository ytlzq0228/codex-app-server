# Gemini / agy 原生客户端测试与修复（2026-09-28）

测试服务器：`<deploy-user>@<test-host>`。客户端仅访问服务器自身的
`http://127.0.0.1:8000`，使用独立的 `native-client-validation` Key。
Key 只保存在服务器权限为 0600 的文件中，不写入仓库或报告。

## 安装与使用

隔离目录：`/home/<deploy-user>/client-validation`（0700）。

- Gemini CLI：0.61.0，使用独立 Node.js 22.22.0。系统 Node.js 12 保持原状。
- agy：从已部署且经过校验的 Worker 镜像复制的 1.2.12 客户端。
- 启动器：`/home/<deploy-user>/client-validation/bin/gemini`、`.../bin/agy`。
- 两个启动器均配置本机网关和测试 Key，使用独立 HOME。
- Gemini 显式配置 `security.auth.selectedType=gemini-api-key`。
- 自动信任仅限专用 `work` 目录；没有启用全局自动批准工具。

```sh
cd /home/<deploy-user>/client-validation/work
../bin/gemini -m gemini-3.8-flash-high -p 'Reply OK only. Do not use tools.' -o json
../bin/agy --model gemini-3.8-flash-high --print 'Reply OK only. Do not use tools.' --output-format json
```

可重复验收脚本：`scripts/validate_native_clients.py`。脚本生成随机文件内容，
要求客户端读取文件，检查实际回答中的随机标记；不会只根据退出码判断成功。

```sh
python3 /home/<deploy-user>/client-validation/validate_native_clients.py \
  --bin-dir /home/<deploy-user>/client-validation/bin \
  --workspace /home/<deploy-user>/client-validation/work
```

## 已复现的问题及处理

| 问题 | 日志证据 / 原因 | 处理 |
| --- | --- | --- |
| Gemini CLI 无法启动 | Node 12 报 `SyntaxError: Unexpected reserved word`；客户端要求 Node >=20 | 独立 Node 22 启动器 |
| Gemini 网关认证模式失败 | 同时设置 base URL 与 Key 时自动选择 gateway，CLI 0.61.0 的认证校验返回 `Invalid auth method selected` | 显式选用 gemini-api-key |
| 无人值守模式退出 | `not running in a trusted directory` | 仅信任测试目录 |
| 两种客户端均 404 | 服务端收到 `/v1beta/models/...:streamGenerateContent`，原服务只有 OpenAI 接口 | 新增原生协议转换层，复用现有 Chat 鉴权、路由、配额、计费、审计及工具会话 |
| agy 请求的模型名不同 | 主请求使用 gemini-3.8-flash，辅助请求使用 gemini-3.1-flash-lite | 显式配置测试环境别名，响应头返回实际模型 |
| 函数参数为空或缺少必填项 | 客户端多次报 `params must have required property ...`；原桥接只校验 custom grammar | Gemini 函数调用增加 JSON Schema 校验、最多两次纠错、工具名与参数映射；不把无效参数交给客户端 |
| Worker 本地工具与客户端工具混淆 | 完整客户端指令包含本地工具名；Worker 另有受限本地工具，可能反复尝试 | 明确客户端工具到 MCP 工具别名及参数映射，保留 Worker 本地访问限制 |
| agy 工具结果角色不同 | 客户端返回 `functionResponse requires user role` | 兼容 model 角色中的 functionResponse，继续按调用 ID、工具名和 API Key 校验 |
| agy 超时误报成功 | `status: SUCCESS`、退出码 0，但 response 为空，stderr 显示 print timeout | 验收要求实际标记和无超时；不把空结果算成功 |
| 测试超时遗留子进程 | 仅终止 Gemini 主进程后仍有 Node 子进程，可能继续占用 Worker | 验收使用独立进程组，超时终止整个组；已清理本轮遗留进程 |
| 单次上游失败导致整个 Worker 冷却 | 一次 502 后客户端重试成功，但 Worker 仍被标记 error，新请求 503 | 普通连接故障先执行推理探测再决定隔离；忙碌不等于故障；额度和登录失效仍保留原有处理 |
| 服务端错误信息过于笼统 | 工具参数错误与容量等待都被覆盖为 `Gemini could not complete the request` | 保留安全的校验原因和 Gemini 容量提示；原生 SSE 区分 400/429/503 |
| agy 模型列表配置可触发崩溃 | 将 AGY_GATEWAY_MODELS 设置为单个模型后，1.2.12 报 `unknown model key MODEL_PLACEHOLDER_M50` | 启动器不覆盖其内置模型列表，使用服务端显式别名 |

上游单次执行失败（请求 `chatcmpl-98892abdcf8b4b3d97d2194547e835b0`）在原日志中只有通用 connection 分类，未保留具体模型端原因。本次修复的是未经验证就隔离整个 Worker 的可用性问题，不宣称已确定该次上游失败的根因。

## 接口与限制

新增 `/v1beta/models/{model}:generateContent` 和
`/v1beta/models/{model}:streamGenerateContent`。
支持 `x-goog-api-key` 和 Bearer Key、文本、函数声明、函数结果、多轮顺序工具调用、
SSE 与 usage。工具调用 ID 和 thoughtSignature 保留续传关联。

转换层只处理上述原生路由；OpenAI 路由仍走原流程。
新增 JSON Schema 校验和工具说明仅作用于 Gemini Worker 适配器。

测试环境配置：

```text
CODEX_GATEWAY_GEMINI_NATIVE_MODEL_ALIASES=gemini-3.8-flash:gemini-3.8-flash-high,gemini-3.1-flash-lite:gemini-3.8-flash-high
```

这两个别名实际都使用 **gemini-3.8-flash-high**，不表示部署了 Lite 模型。
辅助调用同样使用该 Worker、消耗时间和 Token。
`X-Gateway-Model` 返回实际模型，计费记录也按实际模型保存。

订阅 Worker 控制采样和思考策略。原生 CLI 默认的 temperature、topP、topK、
maxOutputTokens、thinkingConfig 作为兼容选项接收，但不保证这些设置生效；
`X-Gateway-Generation-Policy: worker-defaults` 明确这一点。
结构化输出、非文本内容、多候选、强制工具调用、安全策略覆盖及未知生成参数明确拒绝。
未实现原生 countTokens / 模型发现等其他 Google API。

工具等待期间不能更换工具集合；例如客户端自动切换 plan mode 改变工具列表时，
仍会返回显式协议错误。验收任务没有切换 plan mode。
Gemini Worker 仍只有一个执行槽；512 个 ToolSessions 不等于 512 路 Gemini 推理。

## 验证与部署

- 相关单元回归：106 passed（原生适配、Gemini 工具、原有客户端工具、provider、grammar、Worker 隔离判定）。
- 两个客户端的真实文本请求通过。
- 原生非流式函数调用及工具结果续传通过，返回文件标记。
- Gemini CLI 完整工具集实际调用 read_file，通过随机标记验收。
- agy 实际调用文件工具并回传结果，通过随机标记验收。
- 未宣称运行当前完整测试套件，也未在本轮执行新的 OpenAI 实际模型推理。

客户端日志位于 `/home/<deploy-user>/client-validation`：
初次启动、原生文本、各轮工具调试日志以及最终 `work/*-validation.log`。
服务端日志可用 `docker logs codex-app-server-gateway-1` 和
`docker logs gemini-integration-worker` 查阅。
新增参数纠错日志只记录工具别名、次数和校验规则，不记录参数值。

最终网关镜像：`codex-gateway:native-clients-20260928-r6`。Worker 与管理器镜像未更换。

部署备份：`/home/<deploy-user>/deploy-backups/native-clients-20260928`。
本次无数据库结构修改。回滚时恢复备份的 Compose overlay 并重建 gateway；
原镜像为 `codex-gateway:ui-capacity-20260928`。
