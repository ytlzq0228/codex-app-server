我自己有好几个自动化的AI辅助工作流程的服务，现在是调用OpenAI的原生Key来进行。我希望写个服务，可以把我的订阅codex账号，使用app-server对其他几个树莓派上运行的工作流程提供符合OpenAI标准的API服务。


+ 自建 FastAPI 网关
+ Codex app-server
+ 每任务独立 docker
+ Bearer Token

# 整体设计：
1、本系统运行在ubuntu 24.04.5 LTS服务器上
2、docker里面跑codex cli，隔离运行环境。
3、拒绝codex cli任何本地操作权限。
4、API网关层具备一定的管理能力。具备一个管理页面可以监控和管理系统现在的服务状态。
5、网关层通过codex原生的app-server模块来向codex发送请求和接收回复。
6、整体通过Python+FastAPI+jinja+postgres实现。

# 用户端：
1、用户端通过HTTP/HTTPS API连接网关层。通信方式符合OpenAI的API规范。

# 管理端：
## docker管理
1、管理端具备管理codex docker的能力。可以创建docker镜像，可以增加、删除docker。
2、系统内可能还有其他docker，本系统记录自己生成的docker，并不显示或者显示但是不可操作其他docker

## 用户Key管理
1、管理员可以生成、停用用户Key
2、每个用户Key可以配置静态关联到某个docker，也可以每次请求进来的时候随机挑选空闲的docker。
3、每个Key进到codex docker的时候创建一个新的WebSocket。
4、每个Key内的多个会话按照codex官方推荐使用同一连接可以创建多个 threadId的方案。
5、最终我们是要实现客户端通过API Key访问网关的时候，如同访问OpenAI原生API一样的隔离粒度。

## codex账号管理
1、管理员可以看到每个docker内的codex是否已经登录，登录状态是否正常
2、未登录/登出的codex，通过管理员端可以重新登录。登录方式为管理员访问管理端，通过WS请求codex的"method": "account/login/start"，获取docker内的codex登录请求，在管理员的电脑上访问OpenAI登录订阅账号。

## 计费模块
需要管理各个Key的用量情况。记录除了输入输出内容详情以外的所有信息。

# 部署
本系统封装成系统systemd服务
测试环境参考ssh <deploy-user>@<test-host>，SSH免密，sudo免密。先不用直接上去部署服务，上去之后可以查看系统环境。



# Codex App Server Gateway PRD

## 目标与范围

为局域网内自动化工作流提供尽量兼容 OpenAI API 的 HTTP/S 接口。FastAPI 网关用 Bearer Token 鉴权，将请求调度到隔离容器中的 `codex app-server`，并管理容器、访问 Key、Codex 登录状态与用量。

MVP 包含 `/healthz`、`/v1/models`、`/v1/responses` 与 `/v1/chat/completions`（均支持 JSON 与 SSE），Key 启停及静态/池化调度，app-server 初始化、thread/turn 生命周期，管理页，以及不含输入输出正文的用量记录。

> 风险：app-server WebSocket 目前仍是实验性能力，官方未承诺生产支持。协议必须封装在独立适配层，并锁定 Codex CLI 版本。使用 ChatGPT/Codex 订阅提供此类服务前，还需确认账户条款和允许的使用方式。

## 环境与架构

- 测试和首期部署基线：Ubuntu 22.04.5 LTS。
- Python 3.12、FastAPI、Jinja2、SQLAlchemy、PostgreSQL。
- systemd 负责 Compose 项目启停；网关、数据库和 worker 均以容器运行。
- 外部只访问网关；app-server 只监听私有 Docker 网络并使用 capability token。

```text
Raspberry Pi -> HTTPS/Bearer -> FastAPI -> PostgreSQL
                                  |
                         private Docker network
                                  |
                         isolated codex workers
```

## 安全

- API Key 只显示一次，数据库保存带 pepper 的 HMAC-SHA256 哈希。
- 管理端使用 `/login` 表单登录和 12 小时有效的 HMAC 签名 HttpOnly Cookie，不使用浏览器 HTTP Basic Auth；所有管理写操作校验会话内 CSRF Token。
- worker 不挂载宿主目录或 Docker socket，使用非 root、只读根文件系统、cap-drop、no-new-privileges 和资源限制。
- Codex 禁止审批；使用只读/受管权限配置。出站网络只允许模型服务所需目标。
- Docker 管理走独立受限组件，且同时校验数据库登记和项目标签。
- 不记录 messages、prompt、completion、工具参数或模型正文。

## API 与会话

- 实现 Responses API 与 Chat Completions API 的文本兼容层，并支持 `stream=true`；请求对象对官方可选字段和后续扩展保持兼容，后端无法兑现的能力返回带准确 `param` 的 OpenAI 风格错误。
- `/v1/responses` 默认每次请求新建 thread；用 `previous_response_id` 映射和复用会话。会话采用滑动 24 小时 TTL，每次成功续接都会刷新整条 thread 的到期时间；过期或无法恢复的会话返回 `previous_response_not_found`。
- `/v1/chat/completions` 保持与官方接口一致的无状态语义：调用方每次重发完整 `messages`，网关不为 Chat Completions 保存 thread 绑定。
- 每个 Key 在一个 worker 上维护一个连接；连接内可管理多个 thread。
- worker 是 Codex 账号隔离边界；测试时允许多个 worker 登录同一个订阅账号。
- 每个 WebSocket/会话使用独立临时工作目录；可按任务需要读写该目录，但不能读取其他连接的目录。

## 调度与计量

- `pinned`：Key 固定 worker，worker 不健康时快速失败。
- `pooled`：从健康、已登录、未排空且有容量的 worker 中选择负载最低者。
- 记录 Key、worker、模型、thread/turn ID、状态、时延和 token 数，不记录正文。
- “计费”首期仅代表用量核算；订阅限额不能直接等同 API token 价格。

## 验收标准

- 无效/停用 Key 返回 OpenAI 风格 401。
- 非流式和流式文本请求可用，事件顺序正确。
- 断连可重连，但不会自动重复提交状态不明的 turn。
- 网关不能访问 worker 文件；worker 无宿主权限。
- 只能操作系统登记且带项目标签的容器。
- 未登录访问 `/admin` 自动跳转 `/login`；登录、退出及会话过期行为正确，管理操作结果通过页面内弹窗反馈。

## 已确认设计决定
1. 首期主要兼容 /v1/chat/completions，还是 /v1/responses？---直接上/v1/responses
2. 是否第一版就必须支持 stream=true？---需要支持SSE
3. 多个 worker 共用一个 Codex 订阅账号，还是每个 worker 使用独立账号？---worker的作用就是隔离不同的codex账号，但是也允许在多个worker内登录相同的codex订阅账号用于测试。
4. Codex 是否允许读写容器内临时目录，还是完全禁用 shell/文件能力？---如有需要，可以写，但是不同的WS之间要隔离。
5. 测试阶段按现有 Ubuntu 22.04.5 推进是否可以？---可以，按照22.04.5推进
   如果有需要，你可以直接登录测试环境测试代码运行，允许直接上去部署，随便搞，不用担心搞崩

后续范围扩展：在保留 `/v1/responses` 为主接口的同时，增加 `/v1/chat/completions` 文本兼容层及 SSE。

## OpenAI API 兼容性迭代计划

审计基线：2026-09-25。范围限定为本项目明确提供的 `/v1/responses`、`/v1/chat/completions`、`/v1/models`、Bearer 鉴权、错误格式及 SSE 协议。Images、Audio、Files、Embeddings、Batch、Realtime、Fine-tuning 等独立 OpenAI 产品暂不纳入网关目标。

完整的字段级审计与官方规范链接见 [`docs/api-compatibility.md`](docs/api-compatibility.md)。状态定义：

- **兼容**：请求格式、响应格式和主要语义均已实现。
- **部分兼容**：常见客户端可以工作，但部分参数或行为与官方服务不同。
- **缺失**：属于本项目目标，但尚未实现。
- **不纳入**：与文本 Codex 后端能力或“不保存输入输出正文”的隐私设计冲突。

| 模块 | 当前状态 | 主要差异 | 计划 |
|---|---|---|---|
| Bearer 鉴权 | 兼容 | 使用网关自行签发的 API Key | 已完成 |
| OpenAI 错误格式 | 兼容 | 复杂联合类型的错误路径仍可进一步规范 | P1 |
| `X-Request-Id` | 兼容 | 所有 HTTP 响应均生成请求 ID | 已完成 |
| 官方 Python SDK | 文本接口兼容 | 已覆盖 Models、Responses JSON/SSE、Chat JSON/SSE | 已完成 |
| API 限流 | 缺失 | 没有每 Key RPM/TPM、429 和限流响应头 | P1 |
| 请求大小限制 | 部分兼容 | 已限制 `Content-Length`，尚未覆盖无长度的分块请求 | P1 |
| 幂等处理 | 缺失 | 尚未处理幂等键和重复请求 | P2 |
| Models 列表/查询 | 兼容 | 模型创建时间和所有者是网关生成值 | P2 |
| Responses 文本请求 | 兼容 | 支持字符串和常见文本输入项 | 已完成 |
| Responses 工具调用历史输入 | 部分兼容 | 当前序列化成文本上下文，没有保留完整类型语义 | P1 |
| Responses 图片/文件输入 | 不纳入 | 当前后端仅承诺文本输入 | 暂缓 |
| `instructions` | 部分兼容 | 当前拼接到提示词，而非独立的上游 developer instruction | P1 |
| `previous_response_id` | 兼容 | 使用公开 response ID 到 Codex thread 的私有映射 | 已完成 |
| Responses 存储、查询和删除 | 不纳入 | 不保存输入输出正文，无法提供内容查询 | 保持隐私设计 |
| Responses 后台模式 | 不纳入 | 缺少异步任务执行和正文持久化 | 暂缓 |
| Conversation 对象 | 缺失 | 当前只支持 `previous_response_id` | P2 |
| Responses 成功 SSE 生命周期 | 兼容 | 核心文本事件及顺序已实现 | 已完成 |
| Responses 失败/未完成 SSE | 部分兼容 | 已支持 `response.failed`，尚未产生 `response.incomplete` | P1 |
| 客户端断开取消上游 | 缺失 | HTTP 断开后 Codex turn 可能继续运行 | P0 |
| 流式混淆字段 | 部分兼容 | 接受 `include_obfuscation`，尚未生成 padding | P2 |
| Reasoning effort | 兼容 | 已映射到 app-server `effort` | 已完成 |
| Reasoning summary/items | 缺失 | 尚未转发推理摘要事件和输出项 | P2 |
| Structured Outputs | 兼容 | JSON Schema 已映射到 app-server `outputSchema` | 已完成 |
| `temperature` / `top_p` | 部分兼容 | 接收参数，但 app-server 暂无等价控制项 | P1 |
| `max_output_tokens` | 缺失 | 接收参数但尚未可靠限制输出 | P0 |
| 截断和上下文管理 | 缺失 | `truncation`、context management 尚未生效 | P1 |
| Metadata | 部分兼容 | 可回显和记录，尚未校验官方数量及长度限制 | P1 |
| `store` | 部分兼容 | 网关永不保存正文，响应固定报告 `false` | 保持隐私设计 |
| Service tier | 部分兼容 | 无法确认或选择订阅后端的实际处理层级 | P0 |
| Prompt caching 参数 | 部分兼容 | 接收但尚未传递到上游 | P1 |
| Safety identifier | 部分兼容 | 接收但尚未传递到上游 | P2 |
| Logprobs | 不支持 | 无法生成时明确拒绝，不返回虚假数据 | 已完成 |
| Responses 工具 | 部分兼容 | 自动工具声明可忽略，强制工具选择会明确拒绝 | P2 |
| Usage 明细 | 部分兼容 | 有输入、输出和总量；缓存、推理明细尚不完整 | P1 |
| Chat Completions 文本 | 兼容 | 通过 Responses/app-server 适配 | 已完成 |
| Chat 消息角色 | 部分兼容 | 文本角色可用，完整工具调用消息链尚未保留类型 | P1 |
| Chat SSE | 兼容 | 支持增量 chunk、流式 usage 和 `[DONE]` | 已完成 |
| Chat `n` | 缺失 | 只支持 `n=1` | P2 |
| `max_completion_tokens` | 缺失 | 尚未可靠限制输出 | P0 |
| `stop` | 缺失 | 当前接收但尚未执行停止序列 | P0 |
| Penalties / seed | 部分兼容 | app-server 暂无完整等价参数 | P1 |
| Prediction | 不支持 | 无法实现时明确拒绝 | 已完成 |
| Chat 工具调用 | 部分兼容 | 不执行客户端自定义工具 | P2，除非产品范围调整 |
| Chat 音频输出 | 不纳入 | 当前为文本服务 | 暂缓 |
| Chat 持久化 CRUD | 不纳入 | 与不保存正文的设计冲突 | 保持隐私设计 |
| Finish reason | 部分兼容 | 成功时通常为 `stop`，尚不能准确报告 `length` | P0 |

### 迭代顺序

#### P0：协议与语义正确性

1. 落实 `max_output_tokens`、`max_completion_tokens` 和 `stop`，并正确返回 `finish_reason=length` 或对应 Responses incomplete 状态。
2. 客户端断开连接时取消或中断对应 Codex turn，避免后台继续消耗订阅额度。
3. 清理 `service_tier` 等无法验证的响应回显，不得把请求值伪装为上游实际结果。
4. 保持官方 OpenAI Python SDK 黑盒测试持续通过，并扩充异常及断流场景。

#### P1：主要语义覆盖

1. 增加每 Key RPM/TPM 限流、OpenAI 风格 429 错误及限流响应头。
2. 补齐 usage 中可从 app-server 获得的缓存 Token、推理 Token 等明细。
3. 完善 Responses 输入项、Chat 工具历史消息及错误字段路径。
4. 校验 metadata 官方数量和长度限制。
5. 处理截断、上下文、缓存和采样参数：能够传递则真实传递，否则明确报告限制。
6. 补齐 `response.incomplete` 等流式终态，并覆盖分块请求体大小限制。

#### P2：可选能力

1. Conversation 对象、推理摘要、流式 obfuscation 和幂等处理。
2. 多输出 `n > 1`。
3. 只有在产品范围明确调整后，才实现客户端工具执行或更多 OpenAI 产品接口。

### 兼容性原则

- “字段被接受”不等于“功能兼容”；只有参数真实影响上游执行或返回值时才标记为兼容。
- 无法实现但会改变结果语义的参数，应返回包含准确 `param` 的 OpenAI 风格错误，不能静默伪装成功。
- 为兼容会自动携带能力声明的客户端，可以接受并忽略不强制执行的声明；一旦客户端要求强制执行，则必须明确拒绝。
- 不为了接口表面一致而违反“不保存输入输出正文”的隐私要求。
- 每解决一项差异，都必须增加单元测试或官方 SDK 黑盒测试，并在测试服务器验证后更新本表状态。

### Worker 故障转移与恢复（已完成）

- 连接失败、账号退出和订阅限额错误会自动隔离 Worker，自动池不再向其分配新请求。
- 池化且无会话绑定的请求，仅在确认 Codex turn 尚未开始时允许换 Worker 重试一次。
- 已产生模型输出、可能已开始执行、固定 Worker Key 或携带 `previous_response_id` 的请求禁止自动重放。
- 会话绑定 Worker 不可用时返回 `session_worker_unavailable`，不会静默丢失上下文。
- 后台按冷却周期探测隔离 Worker；连接正常且账号仍已登录后恢复为 `ready`。
- 管理页面显示隔离类型、故障原因、隔离时间、下次探测时间和最近恢复时间。
- 手动探测和后台恢复均执行一个最小真实 Codex turn；仅登录状态正常但已达到用量限额的 Worker 不会被恢复。
- API 请求在进入耗时的 Codex turn 前释放数据库事务和连接，避免并发请求耗尽连接池；池化调度随机分散到健康 Worker。

### 管理端审计与会话管理（已完成）

- 请求历史按 API Key 与 Codex thread 聚合为会话，使用 `previous_response_id`、ResponseBinding 和内部 thread ID 串联多轮请求；展开会话后按时间查看每次请求的 Worker、模型、状态码、耗时、Token 和错误代码，不保存输入输出正文。
- Key 活动会话按 Codex thread 聚合，展示对应 Worker；支持删除单个 thread 的全部响应绑定或清空某个 Key 的全部绑定。
- 活动会话页只展示未过期且状态正常的 Responses 会话，并显示最近使用时间和 TTL 到期时间；后台定时将超过 24 小时未使用的绑定标记为过期。
- 支持修改 Key 名称以及自动池化/固定 Worker 调度策略；已有会话不会因策略修改而迁移。
- 删除 Key 采用软删除：Key 立即失效并释放活动会话，请求历史和审计关联继续保留。
- 管理员可在控制台验证当前密码后自助修改密码；凭据使用 PBKDF2-SHA256 加盐存储，改密会使其他旧登录会话立即失效。

### App-server WebSocket 并发池

- 优先复用同一 Key、同一 Worker 的空闲 WS；没有空闲连接且尚未达到上限时创建新 WS。
- 同一 Key、同一 Worker 最多保留 10 条 WS；单 Worker 所有 Key 合计最多保留 40 条 WS。
- 达到任一连接上限后排队等待，超过 30 秒返回 HTTP 503 和 `worker_capacity_exceeded`。
- 空闲超过 600 秒的 WS 由后台回收。
- `previous_response_id` 固定原 Worker，但可通过该 Key 在该 Worker 上任一空闲 WS 恢复；同一 Codex thread 使用独立锁保持 turn 串行。
- 每个 WS 使用 `/{key_id}/ws-{slot_id}` 独立工作目录。


增加会话超时清理机制
检查现在同一个key在同一个docker上是不是最大只会创建一个WS。是否有可能创建多个WS并设置单worker的最大WS并发上限

优先复用该 Key 的空闲 WS。
未达到 Key/Worker 上限时创建新 WS。
达到 Key 上限后等待该 Key 的空闲 WS。
达到 Worker 总上限后不再建连接，进入排队。
等待超时返回明确的 worker_capacity_exceeded。
previous_response_id 仍固定原 Worker，但可以选择该 Key 在该 Worker 上的任一空闲 WS恢复 thread。
同一个 Codex thread 必须加独立锁，避免同一会话的两个 turn 并发执行。
每个 WS 使用独立工作目录，例如 /{key_id}/{slot_id}，满足不同 WS 文件隔离要求。

MAX_WS_PER_KEY_WORKER	10	同一 Key 在同一 Worker 最多并行两个请求
MAX_WS_PER_WORKER	40	单个 Worker 所有 Key 合计的持久 WS 上限
WS_IDLE_TTL_SECONDS	600	回收长期空闲连接
WS_ACQUIRE_TIMEOUT_SECONDS	30	等待连接槽位超时后返回 429/503

请求历史界面，按照previous_response_id等线索聚合同一个会话的多个请求
