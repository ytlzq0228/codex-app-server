# D-Chat

管理员在 `/admin/dchat` 配置启用开关、Bot ID、API Client ID、API Client Secret。
地址固定使用 US 服务，不提供 base URL 配置项。Secret 留空保留原值，页面和 JSON 接口均不返回 Secret。
使用与现有 Google 登录配置一致的数据库配置方式；生产环境应保护数据库及备份的访问权限。

通用发送入口为 `notifications.send_message(db, username, text)`，返回包含 `success` 和 `error` 的字典。
D-Chat 底层封装提供 `send_text_message` 和 `get_user_info`，使用 HTTP Basic Auth、
`bot_type=bot_user` 及配置的 Bot ID。

用户管理、Worker 管理和用户选择器显示 D-Chat 的 `full_name`，仍以原 username 提交操作。
查询失败或未启用时回退到 username。完整用户信息（接口 result 的所有字段）以 JSON 保存到
本地数据库表 dchat_user_cache，按凭证指纹和 username 隔离，重启后仍可使用。
优先读取缓存；缓存缺失或自上次成功刷新起已满 7 天时查询线上，成功后更新完整信息并重新计时。
失败不覆盖缓存、不刷新有效期。查询并发上限为 5，每批线上查询等待上限为 4 秒。

页面数据接口只读取有效本地缓存，缺失时先显示 username，不等待线上查询。
JS 在页面渲染后通过 /user/display-names 分批补全，每批最多 5 个，逐批处理全部用户名；
重复用户名合并请求，用户列表和各编辑弹窗、Worker 归属、API Key 归属、用户选择器和个人概览同步更新。
异步更新只修改文本，不改变 username、选中值或表单提交目标。失败保留现有文本，继续后续批次。
普通用户仅能查询自己的显示名，管理员可查询已存在用户；接口不返回完整用户资料。

/admin/users/data 和 /admin/options/users 每页 30 条，display_names 对应当前页；
Worker 归属选择器初始提供 30 个用户并补齐当前页所有归属，通过搜索或“更多选项”加载其他用户。
这些是列表分页限制，不是显示名总量上限。

Worker 进入 error 状态时，在额度重算事务内写入通知记录；独立后台任务每 30 秒检查一次，
向 Worker 归属用户发送处理提醒。同一次故障成功发送后不重复发送；恢复后再次故障会重新通知。
停用、删除或无归属的 Worker 不发送。主动退出登录的 offline 状态不触发通知；
后续探测转为 error 时仍会提醒。失败间隔 5 分钟重试。
通知只包含 Worker 名称、厂商、概括性故障原因和处理页面，不发送上游原始异常。

新增表 `dchat_config`、`dchat_user_cache` 和 `worker_notifications` 随现有启动建表流程创建。
多实例通过数据库行锁避免并发重复发送。远端成功但本地提交前进程退出时，重试仍可能重复，
因为 D-Chat 参考接口没有提供幂等键；本实现不承诺跨系统的严格恰好一次投递。

验证使用模拟 D-Chat 响应与独立 PostgreSQL 数据库，不向真实用户发送测试消息。
