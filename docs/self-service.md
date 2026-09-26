# 用户服务与计费

## 页面

- `/account`：个人 Key、刷新 Key、修改或设置密码。
- `/admin/users`：创建用户、角色和邮箱维护、禁用、重置密码、查看 Key 状态。
- `/admin/api-keys`：查看归属用户、从已有用户中选择新的归属、刷新密钥。
- `/admin/google`：配置 Google OAuth Client ID、Client Secret、回调地址、允许的邮箱域及启用状态。配置保存在 `google_auth_config` 表，立即生效。Secret 不回显，空白提交保留原值。
- `/usage`：按请求 ID、模型、状态、UTC 日期查询详单。普通用户只能查看请求发生时属于自己的记录，管理员可查看全部。
- `/admin/finance`：按月、用户和模型汇总 API 用量金额，维护模型价格和月度订阅成本，显示金额差额。此版本仅核算金额，不执行支付、扣款或预算限制。
- `/debug`：浏览器携带输入的 Key 调用本站 Responses、Chat Completions 或模型列表，显示 HTTP 状态、耗时和实时响应。

## 身份和会话

`users.username` 为主键。密码使用 PBKDF2 哈希；Google 身份按 `google_sub` 唯一关联，不根据可修改邮箱自动合并已有本地账户。Google 新用户默认 `user`，邮箱作为初始用户名，冲突时生成独立用户名。

`superadmin` 可管理全部用户；`admin` 只能管理普通用户，但可管理 Key、价格、Google 配置及 Worker；`user` 只能访问个人服务。禁止通过用户管理降级或禁用自己。管理员创建或重置密码生成随机初始密码，用户必须先改密。

cookie token 仅在客户端持有，数据库 `user_sessions` 存储其 SHA-256 摘要、CSRF token、用户版本及到期时间。重启不丢会话；到期时间为 12 小时。注销撤销当前会话，改密或管理员更新用户使旧会话失效。

新建 Key 和转移归属均锁定目标用户记录，避免并发绕过每用户一个未删除 Key 的限制。停用的 Key 仍占用名额。刷新仅替换密钥哈希，保留 ID、前缀、历史记录和调度信息。禁用用户会同时阻止其 Key 认证。

## Google OAuth

在 Google Cloud 创建 Web application OAuth client，登记完整回调地址（路径 `/auth/google/callback`），然后在 `/admin/google` 填写 Client ID、Secret 和回调地址并启用。生产环境使用 HTTPS 地址；本地开发可使用 localhost。允许邮箱域留空意味着允许所有经过 Google 验证的邮箱。

授权码通过服务器直接向 Google 换取 token，再请求 userinfo 并验证 email_verified。state 绑定浏览器 cookie、限时且只能使用一次，并使用 PKCE。参考：[Google Web Server OAuth 文档](https://developers.google.com/identity/protocols/oauth2/web-server)。当前自动化测试使用模拟 Google 响应；真实 Google 登录需要实际 OAuth 配置。

## 用量与价格

输入和输出价格单位均为 USD / 1M tokens。按请求模型名称精确匹配，模型别名需单独维护。每笔记录保存价格与费用快照；改价不回溯重算历史。未配置价格的请求明确标记为未定价，不按零价伪装为已计费。

费用 = (输入 tokens × 输入单价 + 输出 tokens × 输出单价) / 1,000,000。

月度节约金额 = 已定价 API 用量金额 − 当月全部 Worker 订阅成本。成本需手动录入，未录入时不显示节约金额。现有上游接口仅提供总输入/输出 tokens，因此不另算缓存或推理 token 价格。

详单保存请求 JSON（包含提示词），不保存 Authorization 请求头和 cookie。有效身份的参数校验、调度失败及中断请求也会记录；认证失败的请求不写入个人详单。历史请求没有原始参数时显示“历史请求未记录参数”。

## 升级

默认 `auto_create_schema=true` 时自动创建新表及添加旧表字段。当前项目的旧管理员迁入用户表，配置中的管理员成为 superadmin；旧 Key 归属该管理员，既有多个 Key 保留，新建与转移适用单 Key 限制。旧管理员表保留用于兼容迁移，新认证只读取用户表。已有签名式会话需重新登录一次，此后会话落库并支持重启保留。

部署前备份数据库和应用目录，仅重建 gateway 服务，Worker 转发器、容器和工作区不需更新。旧代码可在新增字段仍存在时回滚；回滚后需重新登录，旧管理员表不会同步本次升级后的改密。

## 测试

使用独立 PostgreSQL 测试库，避免写入生产数据：

```bash
createdb codex_gateway_feature_test
CODEX_GATEWAY_DATABASE_URL=postgresql+asyncpg:///codex_gateway_feature_test pytest -q
```

mock 模式测试通过 fixture 模拟 Worker manager 的工作区接口，实际转发调度实现保持原样。
