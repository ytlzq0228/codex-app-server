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
- 用户和管理员从 `/auth/login` 登录；登录会话保存在数据库，应用重启后仍有效。会话 Cookie 使用 HttpOnly，管理写操作校验会话内 CSRF Token。
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
- 未登录访问 `/admin` 自动跳转 `/auth/login`；登录、退出及会话过期行为正确，管理操作结果通过页面内弹窗反馈。

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
- 业务请求出现疑似 Worker 故障时，先执行与 Worker 管理页面“探测”相同的账号检查和最小真实推理测试；以推理测试结果作为是否将 Worker 标记为异常的最终依据，探测通过则保持 Worker 可用。
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
WS_PING_TIMEOUT_SECONDS	300	Worker WebSocket keepalive 超时；覆盖长时间客户端工具执行

请求历史界面，按照previous_response_id等线索聚合同一个会话的多个请求

# 增加用户自助服务
1、增加用户表，以 username 作为主键，存储用户凭据、角色、邮箱等信息；登录会话 Token 单独落库存储，便于后续扩展字段。
2、每个Key需要关联到一个用户，默认为创建这个key的用户，可以是admin创建的，存储为admin，也可以是用户自助创建的，存储为用户username。总之谁创建的这个key，这个key就是谁的。admin可以更高为其他用户。更改的时候可以选择为系统内已经存在的用户。
3、用户可创建多个 Key，能同时启用的数量由用户总 Quota 决定。可以刷新 Key 的 value，Key ID 和索引等信息保持不变。
4、用户支持用户名密码登录，支持自助改密码。管理员创建用户的时候生成随机密码，下一次登录必改密码
5、支持Google SSO。参考工单系统项目用户端Google SSO登录。Google SSO登录后如果是新用户，在用户表内新增记录为新用户。默认权限为user
6、用户在登录的时候允许选择使用google sso还是使用用户名密码登录。
7、用户名 `username` 使用邮箱 `@` 前的部分，邮箱完整值单独保存；Google 登录的新用户也按该规则创建。Google OAuth 凭据保存在数据库中，管理员可在管理页修改。

# 用户管理
1、所有用户的身份信息统一进入用户表。表内授权是管理员还是普通用户。系统暂时授权角色为superadmin，admin，user。
2、用户登录会话的token支持落库存储，APP重启的时候不丢会话。
3、增加一个用户管理页面。页面内可以维护现在的用户信息；管理员可设置用户的额外授予 Quota。

# Key管理升级
1、Key管理页面支持增加显示每个Key绑定的用户
2、用户界面支持显示每个用户是否已经生成Key

# 参考更新
之前有一个AI-Code-Proxy项目，该项目是一个AI Key中转平台。本项目中有一些功能值得参考并迁移进本项目。项目在本地/Users/liziqi/Projects/ai-code-proxy
1、用量详情、详单部分请参考AI-Code-Proxy项目并在本项目内实现相似的详单功能。能记录每笔记录的请求参数，并在前端展示详情
2、财务报表功能。本系统上游使用worker内的订阅账号。但是下游依然按照API用量方式进行计费，以体现成本节约效果。维护模型计费价格（USD/1M tokens）表
3、查询与调试。参考AI-Code-Proxy项目实现类似的debug功能
不要参考的：
1、核心转发逻辑，本系统跟AI-Code-Proxy项目上游类型完全不一样。本系统核心转发器逻辑已经经过验证，非必要不要改。如果发现问题，请先与我确认再改
2、项目管理和秘钥映射不需要参考。本项目下游不再存在项目，全部toC直接提供个人Key
3、预算限额不要参考，本期暂时不做预算限额
4、群发消息功能不需要实现。
5、旧系统你只需要参考其功能和逻辑。并且只是参考，不强制一定要照搬旧系统的代码。旧系统上的历史数据你完全不需要关心

前端优化：

1、用户管理-全部用户，列表改为使用一个表格显示用户信息，一行内同时显示用户的各个属性
2、用户管理-新建用户，使用弹窗样式进行交互。系统内其他地方也建议通过弹窗来替代“常驻”和“跳转”以提高交互效率
3、请求详情，使用弹窗。在请求历史和用量详单内点击某个请求 ID都使用弹窗显示这个请求的详情
4、优化所有的下拉菜单样式，使用跟其他元素风格相同的样式
5、左侧导航分为“用户自助服务”和“系统管理”两个有底色区分的分组：所有用户可见自助服务分组，仅 admin 和 superadmin 可见系统管理分组。用户页面统一位于 `/user/`，管理页面统一位于 `/admin/`，登录位于 `/auth/login`；无权访问页面时显示 403 提示页。
6、`/user/workers` 和 `/admin/workers` 使用浏览器可用宽度，Worker 表格行紧凑、减少换行，不在 Worker 名称下显示 Docker 容器名等 `<small>` 辅助行。
7、Worker 账号额度显示账号实际存在的 5 小时窗口和周窗口的**已用百分比**；没有对应限额窗口时，整条进度条及重置时间均不显示。进度从左向右填充；低于 60% 为绿色，60% 至不足 90% 为黄色，90% 及以上为红色。每条进度条内用小号白字、黑色描边显示“窗口：已用 xx%”，旁边仅显示对应重置时间，不显示刷新按钮、更新时间等附加文案。
8、编辑、配置等非常驻交互动作优先使用弹窗，不在列表中长期展示表单控件。列表默认显示易读的文本信息，并在文本旁提供笔形编辑按钮；点击后在弹窗中完成修改。Worker 管理页的归属账号遵循此规则。
9、Worker 管理页按信息关系组织字段：“归属”仅表示 Worker 归属于系统内的哪个用户；Codex 登录邮箱与订阅套餐组合为“登录账号 / 套餐”字段，不与系统用户归属混排。套餐名称使用胶囊样式，胶囊颜色由管理员在 `/admin/finance` 的“订阅套餐 · 月费配置”表格中按套餐自定义。
10、Worker 管理页支持管理员修改 Worker 展示名称。名称以文本显示，旁边提供笔形编辑按钮，点击后在弹窗内修改；修改展示名称不改变 Docker 容器名称、网络地址、登录状态或现有会话。

财务与价格，使用一个表格来显示和编辑所有模型的信息。行内可编辑输入/输出价格
财务与价格主要是价格配置，维护读（输入）、写（输出）、缓存读、缓存写四种 USD/1M tokens 单价。请求记录保存四种 Token 用量与价格快照；财务报表显示总量、按用户/Key 和按 Worker 的四类用量，并按照最新价格重算模拟下游 API 计价。旧请求没有缓存明细，不能倒推真实缓存命中量。本期不做预算限额。


# 用户 Key 额度 Quota

1、Quota 是可同时启用的 Key 名额，不是已创建 Key 的总数。新用户的管理员授予额度默认为 0；总额度 = 管理员授予额度 + 有效 Worker 贡献额度。创建或启用 Key 占用一个名额，停用或删除 Key 释放名额；已停用的 Key 保留记录。

2、管理员可在用户管理页编辑“管理员授予额度”，通过加减按钮或直接输入 0–10000 的最终数字，确认后直接设为该数字，而不是在原值上累加。Worker 贡献额度单独计算，不受这个输入值影响。

3、用户贡献的 Worker 登录非 free 套餐的 ChatGPT 账号，且账号已确认、Worker 启用并处于可用状态时，按登录邮箱去重计入额度：同一用户的同一账号只增加 1 个名额，不同用户各自计算。退出登录、连接异常或删除 Worker 后，应重新计算贡献额度。特别注意：账号用量超限额不扣减 Quota。

4、当总额度小于当前启用的 Key 数量时，立即按最近使用时间从远到近停用超额 Key，优先停用从未使用过的 Key；管理员调减授予额度后也执行此规则。增加额度后，已停用的 Key 需由用户手动启用。

5、用户可以自行停用或启用 Key；创建和重新启用时，都必须保证启用的 Key 总数不超过当前总额度。

# 用户贡献worker
1、worker将保存一个属性，owner。owner为docker创建者，当前的所有docker归属于admin

2、用户可以创建worker，也可以删除自己创建的docker。但是user之间绝对要隔离，用户不可以碰触其他用户的docker。

3、admin可以调整docker的归属

4、需要支持从worker里面读取当前登录账号的能力。

5、用户日常可以管理维护自己的worker，可以登录/重新登录。

6、自助创建 Worker 时，名称格式为 `{username}-worker-{xx}`，其中 `username` 是当前登录用户名，数字后缀从 `01` 自增；用户只能修改后缀，后缀仅接受 01–99 的数字。管理员在自助服务页也遵守此规则。

7、普通用户当前名下存在未登录的worker的情况下，不允许新增worker。

8、如果同一个用户名下的多个 Worker 登录了相同的账号，重复登录的账号不增加 Quota。

9、`/user/workers` 是“贡献 Worker”自助服务页，始终只显示和操作当前用户自己名下的 Worker；管理员进入该页时同样按本人身份隔离。`/admin/workers` 是独立的系统 Worker 管理页，仅 admin 和 superadmin 可进入，可管理系统内全部 Worker 及其归属；管理概览中的 Codex Workers 容器可进入该页。

# 自助服务概览

普通用户登录后进入自助服务概览，页面显示本系统的使用说明，并以进度条和三个圆角矩形容器展示以下步骤：

贡献worker------------------>生成Key----------------->定期维护
显示用户当前是否创建/登录      显示用户当前有没有Key      显示worker状态是否正常提醒用户处理或者显示非常好，一切正常

价格配置内增加订阅账号套餐-月费配置。检索当前worker内登录过的plan名字并配置月订阅价格。同时，允许自有增加新的套餐名字
基于当前已经登录的（包含超限额的，但是不包含退出登录的）的所有worker，配合单价，计算月订阅总费用。

前端优化，如果已经登录的worker，点击"重新登录"按钮后弹窗确认，是否退出当前账号并重新登录。点击是之后，使用ws给docker的codex发送一个退出命令，然后再请求新登录


管理员面板，管理概览，不在显示API key详情和worker详情
增加几个监控面板：
## 当前订阅池总用量
1、增加一个逻辑，订阅套餐 · 月费配置表里面，增加一个字段，套餐权重
2、按照套餐权重，加权平均当前的总用量。这里我理解所有人的用量重置窗口是不一致的，但是没关系，只是为了了解当前的用量风险
3、此用量需要支持回溯，需要支持小时级别的历史数据存储。

## 一个饼图，Woker状态
分母为所有已经创建的Worker，扇区分为
1、完全正常
2、登录，但是当前超限被隔离
3、登录，但是服务异常被隔离
4、未登录
5、其他异常

## Worker历史波动
折线图，显示以上几种状态的历史情况，按10分钟打点存储

# 基于此，评估是否需要抽象一个历史数据记录表。
后续可能还会有更多的历史回溯统计功能。但是不一定需要新增存储，类似财务by天的报表，原始数据可能已经包含了时间


绑定长期保留，按需验证：再次请求时，确认身份、历史和配置兼容，再尝试恢复原 Thread。恢复失败仍要保证本轮对话能正确发送出去。
根据实际事件失效：管理员释放绑定、Worker 删除、Thread 不存在等情况，让绑定失效。
连接独立回收：长期保留绑定不需要一直占着 WS；等待工具结果的 5 分钟和执行锁仍各自保留。
活动与历史分开显示：长期可恢复的绑定不一定是“正在活动”。Key 活动会话只显示最近2小时的对话。数据库内超过2小时的对话保留，但是不显示


请求历史，逻辑会话 ID展开后，按时间倒叙显示请求，最大只显示最近20个。点击more按钮显示更多
请求历史表格显示请求的价格快照；会话行显示该会话内所有请求价格的合计，未定价请求单独标识。
499不算异常，按绿色标识
/admin下的所有页面都允许使用浏览器全部宽度


前端优化：
请求历史页面：
1、Previousid看起来一般不会出现。转发逻辑保留，但是前端表格内不显示Previous Response。展开后详情内显示。
2、会话展开后的请求记录使用16734a着色圈个边框。明显这一个区域是展开的请求记录，区别于其他会话记录行
3、请求详情弹窗内优化展示效果：
 - 字体字号与外面页面的风格保持一致
 - 请求的元数据属性使用渲染成适合阅读的格式化字段。可以使用表格或者胶囊样式
 - Worker字段增加worker名称，账号，所有人，不显示workerID
 - 会话关联依据同样格式化成适合阅读的样式。
 - 会话关联依据，请求观测信息，请求参数的原始JSON默认不展示，折叠，点击展开后查看
 - 请求观测信息格式化展示client_ip，client_address，user-agent，path等重要信息
 - 请求参数格式化展示最后一对input_text和output_text。使用对话气泡的样式展示
 - 请求元数据使用四列布局；输入、缓存读、缓存写、输出的 Token 用量与对应单价按此顺序分成上下两行，同类字段纵向对齐
 - 请求详情弹窗标题栏和关闭按钮固定显示，详情内容在标题栏下方独立滚动



客户端
  │
  ├─ /v1/chat/completions
  └─ /v1/responses
          │
     请求解析与能力校验
          │
     模型注册表 → 确定 provider
          │
     公共鉴权、调度、计量、审计
          │
          ├─ CodexAdapter  → Codex worker 池
          ├─ GeminiAdapter → Gemini worker 池
          └─ ClaudeAdapter → Claude worker 池

会话绑定需要带上 provider。 延续请求应绑定原来的厂商、worker 和原生会话，不能根据新传入的模型名随意切换。
能力不能假设完全一致。 图片、工具调用、结构化输出、推理参数、Responses 续接，都需要逐项声明支持情况。不支持的功能应明确报错，避免静默忽略。后续边用边发现问题边改。
但是重要的是，不能影响现有的openai worker的功能

先选Gemini：验证订阅登录、文本流式输出、连续对话、工具往返、取消请求和额度耗尽后的行为。
沿现有 CompletionBackend 扩展适配器：保留已有 Codex 实现，逐步抽出真正公共的部分，避免先大规模重写。
给 worker、模型和会话增加 provider 维度：调度先确定厂商，再在对应池内选 worker。
统一入口上线：优先保持现有客户端的接入方式，必要时再加原生协议入口。

使用测试环境进行开发测试

---------------DONE---------------
用户端额度逻辑优化：
用户贡献度依然按照现有的逻辑进行计算，包括增加和消耗，以及去重账号，这部分逻辑不变。
但是用户名下的所有Key的能力，按照用户创建的worker能力来进行约束。
举个例子，用户创建了两个codex worker，允许创建2个key，但是这两个Key都只有openAI的模型调用权限。
用户创建了一个codex一个gemini的worker。允许创建2个key，这两个Key同时有gpt和gemini的模型调用权限。
账号去重的时候，因为用户登录codex和gemini可能使用完全相同的账户名字，所以去重的时候要考虑增加模型厂商的匹配。不同模型但是账号相同，不算重复。
问题修复：
修正目前gcp-ge-plus-tier · 不提供额度的问题。非free都给提供额度
财务报表-订阅套餐 · 月费配置，套餐名称前面增加模型厂商名字
增加 Worker的时候需要选择worker类型
gemini worker账号已用额度读取失败
gemini worker无法退出登录/重新登录。

---------------DONE---------------
登录 Gemini 订阅账号弹窗优化：
1、优化页面按钮和文字样式，对齐整个系统的风格
2、不再显示原始CLI信息，格式化成适合用户直接阅读和使用的链接或者信息。
3、重新登录与codex一致，点击后弹窗确认是否退出当前账号，重新登陆前先退出。现在退出登录会显示：正在退出原账号…然后：登录操作失败
4、另外，现在codex和gemini的操作结果探测弹窗，都会额外渲染一个代码块。请修正这个异常。系统内其他弹窗也有这个异常渲染空代码块的异常，例如“操作结果”，请一并修复这个前端bug
5、账号已退出 Codex worker is not logged in 隔离于 2026/09/28 17:17:20 下次探测 2026/09/28 18:32:29 这几个字体大小为统一


---------------DONE---------------
厂商权限暂按“名下存在未删除的对应厂商 Worker”实现，所有 Key 共享这些权限；Key 数量仍按健康付费账号去重后的贡献额度计算。这样 Worker 临时异常不会额外改变厂商权限，但原有的额度不足停用 Key 规则仍然生效。若你选择更严格的方案，我再收紧这一条件。
关于这个，我记得之前明确说过，超额不减额度。
PRD: # 用户 Key 额度 Quota
1、Quota 是可同时启用的 Key 名额，不是已创建 Key 的总数。新用户的管理员授予额度默认为 0；总额度 = 管理员授予额度 + 有效 Worker 贡献额度。创建或启用 Key 占用一个名额，停用或删除 Key 释放名额；已停用的 Key 保留记录。
2、管理员可在用户管理页编辑“管理员授予额度”，通过加减按钮或直接输入 0–10000 的最终数字，确认后直接设为该数字，而不是在原值上累加。Worker 贡献额度单独计算，不受这个输入值影响。
3、用户贡献的 Worker 登录非 free 套餐的 ChatGPT 账号，且账号已确认、Worker 启用并处于可用状态时，按登录邮箱去重计入额度：同一用户的同一账号只增加 1 个名额，不同用户各自计算。退出登录、连接异常或删除 Worker 后，应重新计算贡献额度。特别注意：账号用量超限额不扣减 Quota。
4、当总额度小于当前启用的 Key 数量时，立即按最近使用时间从远到近停用超额 Key，优先停用从未使用过的 Key；管理员调减授予额度后也执行此规则。增加额度后，已停用的 Key 需由用户手动启用。
5、用户可以自行停用或启用 Key；创建和重新启用时，都必须保证启用的 Key 总数不超过当前总额度。


codex异常状态叫做隔离/红色
gemini异常状态叫做offline/灰色
能不能给统一了


前端优化，请求历史展开某个会话后Worker Thread：列表限制最大宽度，多的换行展示，现在给屏幕宽度撑爆了
前端优化，所有金额按照小数点后4位显示，所有token数量超过1M的按照million显示，精确到小数点后4位


⚠ Eligibility Check
  ⎿  Eligibility check failed: Your current account is not eligible for Antigravity. Try signing in with another personal Google account. If you believe
     this is an error, please contact your administrator.


增加管理员可以手动为用户开启provider能力的开关。
前端优化，该企业套餐未通过官方 CLI 返回数值额度，请在企业控制台查看。这个替换成同样的进度条显示，内容为"无限制"，永远计算为0%
管理概览，当前订阅池总用量，图表字体优化为固定字号，现在随窗口大小动态变换
管理概览，用量计算区分Provider计算，Codex和Gemini单独计算可用用量。
前端优化，Worker 历史波动图表字体优化为固定字号，现在随窗口大小动态变换。Worker 状态和历史波动图表放在同一个d容器里面，左右布局。高度使用现在Worker 状态
容器的高度


/根PATH跳转到user/overview
Worker 状态和历史波动图表放在同一个d容器里面，左右布局。worker状态饼图保持固定宽度，剩下的动态给历史波动图


没有权限的GCP账户在登录的时候应该遇到了一个我们没遇到的页面，你试试


/admin/reports
/user/usage
页面中按钮与输入框或者选择框控件水平没有居中，请优化前端，高度对齐，水平居中

/user/account页面，修改密码功能改成一个按钮，然后弹窗输入新老密码。另外，如果没有配置过密码，是纯google开户的用户，不显示修改密码功能


这个项目目前已经完成适配codex appserver和gemini agy worker的能力。包含chat response 图片 工具。请查看现有的系统逻辑，评估并设计增加对claude模型的支持能力：
1、新建新的claude类型的worker
2、API请求的时候传入claude的模型名字，则命中新增的claude转发逻辑。
3、新增的claude的转发逻辑不要影响现有的openAI和gemini转发逻辑
4、测试环境<deploy-user>@<test-host>.你可以直接在测试环境上测试worker和claude cli客户端。在测试环境上使用127.0.0.1进行能力调用。
5、我们希望实现该服务尽可能对其原生claude API的所有能力。所有调用能力和格式尽可能follow原生API。
6、你可以在测试环境上进行各种测试。最终的开发你不需要全部完成，我会让之前写这个系统的agent来使用你的方案继续完成后面的开发动作
7、有问题通过表单的方式问我

双活方案
参考服务器清单查看现有的服务器端环境
目前本系统的环境为 <deploy-user>@<app-1>主/已部署当前版本、<deploy-user>@<app-2>备/新机器
本机高可用方案：
1、使用DB Proxy连接目前的PG数据库集群。迁移现有的本地数据库内的实例到数据库集群
2、<app-1>和<app-2>均支持用户流量接入，两个APP节点双活
3、<app-1>和<app-2>均运行worker管理和docker服务。所有用户的Worker 50 50创建在两个APP节点上，创建worker的时候，选择当前worker负载低的APP节点创建1个新worker，用户无感，系统自动选择。用户的一个账号只在one of two APP nodes上创建，不需要创建冗余docker。单一APP节点挂掉的情况下，系统降级后只少有一半的活跃worker可用。
4、不管用户流量从app-01还是app-02进入。均可以使用本app本地的worker或者跨app节点使用其他的worker。本地worker还是跨app worker调度算法和优先级完全相同。目前库内已经有worker状态表。看两个app节点如何维护这张表，以及如何抽象出共享状态的worker管理。
5、检查系统全部逻辑，保证app运行内存尽可能做到无状态，保证用户在切换app节点的时候的状态连续性。
6、测试环境<deploy-user>@<test-host>。测试期间你可以使用<test-host>+<app-2>进行测试。然后在<app-1>+<app-2>上正式部署