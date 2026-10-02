# xiaowork Watch

个人网站与 VPS 监控平台，v0.3.0 开始使用真实检测。Vue 3 前端、Python 3 主控、SQLite 数据库；Ubuntu/Debian 一键部署无需 Node.js、Java 或独立数据库。

## 使用方式

- `/`：公开查看网站、VPS、节点状态及检测历史，无需登录。
- `/admin`：管理员登录后新增、编辑、删除、启停监控，立即检测，生成探针安装命令。
- 网站：服务器按设定间隔发送真实 HTTP GET/HEAD 请求，记录状态码、响应时间、成功/失败与错误；浏览器关闭后继续检测。
- VPS：安装探针后每 30 秒向主控上报心跳。自建测试节点对已关联 VPS 发出 5 个 ICMP Ping，保存真实延迟和丢包；节点离线不会生成虚假的失败测量。

监控、账号、历史保存在服务器数据库中，更新与 HTTP/HTTPS 切换不会重置。初始列表为空。旧原型 localStorage 数据不自动导入真实数据库，不再生成演示结果。

## 一键部署 / 从旧版升级

在自己的 Ubuntu/Debian SSH 终端运行：

```bash
curl -fsSL https://raw.githubusercontent.com/xiaowork-dev/xiaowork-watch/main/install.sh | sudo bash
```

未部署时选择 1；原型版重新运行此链接会升级主控并保留已有域名与 HTTPS 配置。首次随机生成管理员密码，仅在安装终端输出；用户名 `admin`。遗忘密码可在服务器执行：

```bash
sudo xiaowork-watch reset-admin
```

也可用管理菜单 **9. 重置管理员密码**，重置后原登录会话失效。没有公开注册或共享默认密码。

安装后 `sudo xiaowork-watch` 打开管理菜单，保留反代、HTTPS、更新、回退、自动更新、日志和彻底卸载。默认网站端口 8088，主控仅在 `127.0.0.1:8091` 监听。main 推送后发布完整前后端包，服务器约每 15 分钟检查更新。详细步骤和手动源码部署见 [服务器部署说明](docs/deployment/server.md)。

## VPS / 测试节点

1. 管理员进入后台添加 VPS 和测试节点。
2. 在每项的“安装探针”中生成命令，在该项对应 Linux 服务器的 SSH 终端粘贴运行。
3. 节点和 VPS 安装成功后上报心跳；编辑 VPS 勾选测试节点，保存后每分钟测量，也可立即提交测量。

公网探针连接主控使用 HTTPS，安装码 10 分钟有效且只用一次。长期凭据保存在对应服务器的受限配置文件；删除目标或重新注册会撤销其旧凭据。探针只主动连接主控，无需在 VPS 上开放管理端口。Ping 无回复可能来自 ICMP 限制，页面把线路结果与探针心跳分开显示。

探针卸载：下载主控提供的 `/agent/install.sh` 后运行 `sudo bash install.sh --uninstall`，仅清理该探针；主控记录可在后台删除。

## 本地开发

Node.js 20.19+ 或 22.12+，Python 3.9+：

```powershell
npm ci
python backend/server.py --data-dir .local-data --init-admin
python backend/server.py --data-dir .local-data --host 127.0.0.1 --port 8091 --allow-private-targets
```

另开终端 `npm run dev`；Vite 将 `/api` 和 `/agent` 代理到 8091。该开发参数仅用于 localhost HTTP 登录和本地真实测试目标，生产服务不启用。前端只调用真实 API；后端未运行时显示连接错误。生产构建使用 `npm run build`。

```powershell
python -m unittest discover -s tests -p 'test_*.py'
node tests/test_frontend.mjs
npm run build
```

生产检测默认仅接受公网 HTTP/HTTPS 与公网 VPS 地址，跳转重新校验、TLS 正常验证；不会把主控内网服务作为公开监控目标。公网登录、管理员修改和探针上报要求 HTTPS（菜单 8 配置），HTTP 入口仅支持公开查看。管理员写接口由后端会话、CSRF 和来源检查保护；公开 GET 响应不包含密码、探针令牌或安装码。

## 代码

`src/` 为公开页面和管理界面，`backend/` 为真实检测、调度、权限和数据库，`agent/` 为 Linux 探针及安装器，`scripts/deploy/` 为校验、更新、HTTPS 和卸载。实用版的接口与部署约定见 [v0.3 设计](docs/design/live-v0.3.md)。
