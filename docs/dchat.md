# D-Chat

管理员在 `/admin/dchat` 配置启用开关、Bot ID、API Client ID、API Client Secret。
地址固定使用 US 服务，不提供 base URL 配置项。Secret 留空保留原值，页面和 JSON 接口均不返回 Secret。
使用与现有 Google 登录配置一致的数据库配置方式；生产环境应保护数据库及备份的访问权限。

通用发送入口为 `notifications.send_message(db, username, text)`，返回包含 `success` 和 `error` 的字典。
D-Chat 底层封装提供 `send_text_message` 和 `get_user_info`，使用 HTTP Basic Auth、
`bot_type=bot_user` 及配置的 Bot ID。

用户管理、Worker 管理和用户选择器显示 D-Chat 的 `full_name`，仍以原 username 提交操作。
查询失败或未启用时回退到 username。显示名按凭证缓存 30 分钟，失败缓存 60 秒，
最多缓存 2048 条；查询并发上限为 5，单个页面等待上限为 4 秒。

Worker 进入 error 状态时，在额度重算事务内写入通知记录；独立后台任务每 30 秒检查一次，
向 Worker 归属用户发送处理提醒。同一次故障成功发送后不重复发送；恢复后再次故障会重新通知。
停用、删除或无归属的 Worker 不发送。主动退出登录的 offline 状态不触发通知；
后续探测转为 error 时仍会提醒。失败间隔 5 分钟重试。
通知只包含 Worker 名称、厂商、概括性故障原因和处理页面，不发送上游原始异常。

新增表 `dchat_config` 和 `worker_notifications` 随现有启动建表流程创建。
多实例通过数据库行锁避免并发重复发送。远端成功但本地提交前进程退出时，重试仍可能重复，
因为 D-Chat 参考接口没有提供幂等键；本实现不承诺跨系统的严格恰好一次投递。

验证使用模拟 D-Chat 响应与独立 PostgreSQL 数据库，不向真实用户发送测试消息。
