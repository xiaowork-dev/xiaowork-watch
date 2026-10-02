# v0.3 实用版约定

本轮用户要求覆盖“先做前端原型”的阶段范围：网站/VPS 检测必须真实，公开访客只读，管理员登录后修改。为沿用现有 Ubuntu/Debian 一键安装中的 Python 运行环境，本版采用 Python 标准库 + SQLite；原文档的 Java/MySQL 方案不作为当前运行依赖。

## 数据与权限

服务器数据库保存网站、VPS、节点、检测历史、管理员密码 hash、会话及探针凭据。首次初始化没有监控示例。前端不写 localStorage、不选择模拟适配器、不回退演示结果；API 连接失败明确显示错误。

公开 GET 网站/历史/节点状态无登录要求。全部 CRUD、启停、主动检测和安装码生成在后端要求管理员会话和 CSRF；管理员页面仅根据该会话显示操作，不把隐藏按钮作为授权。账号固定 admin，无公开注册；随机初始密码与重置仅由服务器 CLI 输出。Cookie 为 HttpOnly、SameSite=Strict，HTTPS 时 Secure；会话/CSRF 不写浏览器持久存储。密码使用带盐 scrypt，登录有限速。公网登录、管理修改和探针上报只接受 HTTPS；HTTP 只读。localhost 开发 HTTP 需显式 --allow-private-targets，生产单元不传入该参数。公开监控 URL 不显示查询参数和片段，避免公开凭据；管理员 HTTPS 会话可查看完整配置。

## 真实检测

网站定时检测及手动检查共用真实 HTTP GET/HEAD 检测器，限制并发/超时，正常 TLS 校验、DNS 固定到校验的公网 IP、每次跳转重新校验，不访问 loopback/内网/元数据目标。结果记录响应码、耗时、成功/失败类型与检查时间。调度在主控运行，与浏览器是否打开无关。

VPS agent 每 30 秒主动心跳，90 秒过期显示 OFFLINE；首次安装前 PENDING。测试节点领取关联且启用的 VPS 任务，每个任务真实 ICMP 5 包，固定公网 IP、8 秒等待，记录实际 sent/received/avgRttMs。失败执行为 ERROR；已发包无回复为 TIMEOUT。已注册 VPS 心跳离线也可测线路；只要测试节点在线且已启用，不将心跳与 ICMP 混为一谈。离线/停用节点不生成假失败，已有测量保留并显示过期。

## 接口

统一 `{code,message,data}`；成功 code=0。错误配合 400/401/403/404/409/429 等 HTTP 状态。保留 v0.2 网站和 fleet API 字段，增加：

| 接口 | 权限/用途 |
| --- | --- |
| GET `/api/auth/session` | 当前浏览器会话；匿名返回 authenticated=false |
| POST `/api/auth/login` | admin + password，签发 cookie 会话 |
| POST `/api/auth/logout` | 管理员及 CSRF，撤销会话 |
| POST `/api/agent/enroll` | 10 分钟单次安装码换角色与记录绑定 credential |
| POST `/api/agent/heartbeat` | Bearer agent credential；返回角色及授权任务 |
| POST `/api/agent/results` | Bearer probe credential；jobId 归属、字段、期限与幂等校验 |
| GET `/api/health` | 公开健康端点，包含正在运行的 release SHA |
| GET `/agent/install.sh`、`/agent/agent.py`、`/agent/agent.py.sha256` | 公开下载，源码不包含凭据 |

重新生成安装码撤销前一安装码；注册新探针时撤销旧长期凭据。删除记录撤销其凭据和未完成任务。凭据不能访问管理员写接口，也不能提交别的节点任务。

## 运行和更新

实用版包 kind=`monitoring-server`，包含前端、`.backend`、`.agent`、`.deploy`；不存在密码、数据库或 `.env`。旧 v0.2 管理器拒绝未知包，因此首次升级重跑最新安装链接；之后自动更新完整前后端，`shared/data` 始终独立保留。主控仅监听 loopback，由 Nginx 同域代理。专用系统用户、受限读写路径，后台服务与网页一同通过健康检查后完成更新；失败恢复原代码和服务配置。

原型数据不自动转成真实检测历史；前端不删除原 localStorage。公开URL、协议和端口变化后监控配置仍由服务器统一提供。实用版不允许回退到模拟原型。更新包不自动覆盖数据结构；后续结构变更必须另外提供迁移和备份。

## 验证

本地与 CI 用真实临时 HTTP 服务验证返回码、超时和持久化；Linux 真实 Ping 本地 fixture，包含无权限/注入/协议错误检查。权限测试直接调用写接口，确认匿名和 agent 凭据不可修改管理数据，管理员 CSRF 校验有效。部署测试覆盖首次实用版升级、失败恢复、私有数据清理，以及原 HTTPS/Certbot 流程。最终公网签发与 VPS 可达性在用户自己的部署环境中完成。
