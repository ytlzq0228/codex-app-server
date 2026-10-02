# 双活部署与恢复

两台应用服务器分别运行 gateway、worker-manager 与 Docker。生产节点为
`app-1 / <app-1>` 和 `app-2 / <app-2>`。
每个订阅 Worker 只有一个运行实例，账号及工作区卷保留在其所属节点。
数据库是独立的 `codex_gateway` 库，使用独立应用角色，经各应用节点本地
HAProxy 5000 端口连接 Patroni 当前 primary。

## 调度与状态

`app_nodes` 保存节点地址、Docker 管理服务健康心跳和实时连接负载。
心跳间隔 5 秒、有效期 30 秒；失去心跳的节点不参与新建与调度。
`workers.node_id` 是唯一归属，创建选择存活节点中 Worker 数量较少者，
再以连接负载和节点 ID 排序。数据库事务 advisory lock 串行化分配，
创建预留先提交，管理服务按容器名幂等创建；丢失回复后由所属节点重试。
删除和工作区准备均使用所属节点的 manager。

普通请求仍使用统一的负载、厂商额度及缓存亲和算法，没有本地节点优先级。
跨节点执行由入口 gateway 转发到 Worker 所属 gateway 的鉴权内部执行接口，
其流式事件返回原入口，原入口只记一份请求与计费记录。
同一 Worker 的 WebSocket 池、容量限制和 thread 锁集中在其所属节点，
不会因为两个入口而重复计算容量或竞争工作区槽位。负载心跳在数据库汇总。

用户登录会话、OAuth 状态、登录限流、Key、额度、历史、响应绑定与执行
checkpoint/租约均保存在 PG。进程内 WebSocket、运行任务与工具 Future
无法跨进程序列化：`pending_tool_routes` 按 Key + call_id 记录归属、Thread
和目标，切换入口后的工具输出仍返回原执行进程。节点或进程退出后，
未完成调用明确失败，不重放已可能执行的 turn 或客户端工具。
普通已完成会话在入口切换后读取数据库绑定并恢复 Worker 本地 Thread。
流式连接在其入口故障时会断开，客户端需重连；不承诺无损迁移正在运行的 SSE。

后台账号巡检与故障恢复由所属节点执行，Worker 行锁防止状态冲突；
聚合监控已有数据库 advisory lock。HA 启动迁移和用户初始化也串行化，
禁止每个节点自动生成或覆盖 seed Worker。`/healthz` 检查本节点有效心跳，
数据库不可用或 Docker 管理服务失联后返回 503。

## 部署

使用 `deploy/compose.ha.yaml`，正式目录 `/opt/codex-app-server-ha`。
`.env` 指定统一 gateway 镜像和本机 NODE_IP；`.env.ha` 包含 gateway 配置，
`.env.manager.ha` 包含 manager 配置。三个文件仅 root 可读。
两节点保留相同 Key pepper、Worker token、Manager token；节点 ID 和地址不同。
设置 `CODEX_GATEWAY_BOOTSTRAP_WORKER=false`。数据库连接池每节点 10 + 5。
Docker 网络 `codex-app-server_default` 是已创建的 external network。
生产 Compose project 为 `codex-ha`，gateway/manager 使用明确容器名，
避免 Compose 把保留的旧项目回退容器当作新服务重建。

`deploy/haproxy-db.cfg` 使用 Patroni `OPTIONS /primary` 检查三台 DB：
<db-01>、<db-02>、<db-03>；不能写死数据库 leader。
将 bind 改成本机私网地址。`deploy/restrict-ha-ports.sh` 限制 manager 与
Worker 发布端口只供本机和指定应用节点访问，并限制 DB Proxy 仅本机使用。
通过 systemd 在 Docker 启动后恢复规则。

Worker 4500 端口映射到本机私网地址的动态端口，数据库保存实际地址；
所有请求都有 Worker token。内部 gateway `/internal/execution` 校验 Manager
Bearer token，以及 Worker 归属、generation、provider、endpoint、enabled。
外部反向代理不应转发 `/internal/` 路径。

入口 HAProxy 应将两台 APP 的 8000 端口加入同一 backend，使用
`GET /healthz` 检查，保留 Authorization / Cookie / Origin / Fetch Metadata，
关闭 SSE 缓冲并保留足够长的连接超时。`deploy/pfsense-codex-ha.php`
通过 pfSense 原生配置接口仅更新现有 Codex 生产 backend，默认预览验证，
传入 `--apply` 才持久化并重新加载，其他服务与测试 backend 保留。
Web 登录使用同一公共域名，
共享数据库会话不能让浏览器自动跨 IP 共享 cookie。

## 数据迁移和回退

1. 先在 <test-host> + <app-2> 使用独立测试库验证；保留原测试容器和数据库。
2. 停止生产 gateway/manager 的写入，保存最终 pg_dump、配置、镜像/容器
   inspect 及全部账号和工作区卷。恢复到集群独立库，使用新角色作为 owner。
3. 停止原 Worker，备份卷；迁往另一节点的 Worker 必须传输两个卷。
   原容器更名保留停止状态，只启动新容器；同步更新 endpoint/node_id，
   不改变 Worker ID、execution_generation 与响应绑定。
4. 核对每张表行数、Worker 分布、所有受管运行容器、镜像一致性与健康。
   正式切换后停止旧本地数据库并关闭其自动重启，避免旧库再次接受写入。
5. 更新 systemd 正式 Compose 路径；不使用 `--remove-orphans`。

数据库切换前失败可启动保留的原 gateway/manager/Worker。
切换后有新写入时，必须保留集群数据库并将应用回退到兼容版本；
不能直接启动连接旧本地数据库的原 gateway。Worker 回迁也必须先停止
新实例并同步最新账号/工作区卷，才能启动保留实例。禁止同时运行两个
相同账号的副本。原迁移备份 `/opt/codex-app-server/deploy-backups/ha-20261002`
已按后续“仅保留最近一次备份”的要求清理。当前每个正式节点的最新备份
位于 `/opt/codex-app-server-ha/deploy-backups/release-<时间>/`。
之后使用 `scripts/deploy_release.py` 发布：先检查数据库访问，再删除历史备份、
创建本次备份，然后只更新 gateway 和 worker-manager。

节点故障时不自动复制 Worker，也不把已有绑定迁到其他账号。保留节点
继续提供未绑定请求与其自身 Worker 的会话，故障节点 Worker 的既有绑定
返回不可用。节点恢复后使用原卷、Worker ID 和数据库记录重新探测恢复。

## 验证记录

2026-10-02：418 项自动测试通过。<test-host> + <app-2> 的独立共享测试库
验证了两个入口鉴权、跨节点 Codex 执行、previous_response_id 续接、
切换入口后的客户端工具结果、反向 Claude SSE，以及停止节点 2 后
其 pinned Worker 返回 503、节点 1 Worker 继续服务。

正式部署已完成，APP <app-1> / <app-2> 分别运行 6 / 5 个 Worker，
11 个 Worker 均为 ready。两节点使用相同 gateway 镜像，运行包与源码清单
核对通过；原库 15 张表的原有主键保留，短期状态表单独审计。

两个站点的四台 pfSense（<pfsense-a1>/<pfsense-a2>、<pfsense-b1>/<pfsense-b2>）均持久化了两个
APP 的 backend 配置：leastconn、2 秒健康检查、fall 3 / rise 2，并禁止
公共入口访问 `/internal/`。修正了 SJC 生产域名原来指向测试机的配置。
两个站点 VIP 均验证了任意一个 APP 入口被禁用后仍返回健康响应，
测试结束后恢复所有服务器为 enabled；配置备份为各 pfSense 上的
`/conf/config.xml.before-codex-ha-20261002`。

生产验证还覆盖了数据库登录会话跨节点、跨节点 Codex/Gemini 调用、
跨入口响应续接和反向 Claude SSE。实测新 Worker 分配到数量较少的
app-2，跨节点工作区准备和删除均成功，验证临时容器已移除。验证专用账号、Key 和会话已停用或清理，
保留请求审计。<test-host> 原测试部署已恢复；独立 HA 测试库和停止状态的
测试应用容器保留用于复查。旧生产本地 PG 容器已停止且取消自动重启。
正式记录见 `docs/dual-active-verification.json` 与各节点的
`/opt/codex-app-server-ha/release-verification.json`。
