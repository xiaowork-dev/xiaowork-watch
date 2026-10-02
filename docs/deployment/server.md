# 服务器部署与更新（v0.4.1）

本版本包含真实网站检测、SQLite 存储、管理员登录和 Linux VPS 的 ICMP / TCP 测速探针。公开页 `/` 只读，后台 `/admin` 登录后可修改。原型中的浏览器数据仍保留，但不会自动导入真实数据库。

## 一键安装和旧版升级

支持 Ubuntu/Debian + systemd。在自己的 SSH 终端执行：

```bash
curl -fsSL https://raw.githubusercontent.com/xiaowork-dev/xiaowork-watch/main/install.sh | sudo bash
```

首次显示“1. 部署 / 2. 彻底卸载 / 0. 退出”，选择部署；成功后进入管理菜单。默认安装 `/opt/xiaowork-watch`，网站端口 8088。自选目录和端口可加 `--path /opt/watch --port 8088`（服务隔离要求使用 /opt 或 /srv 等系统目录，不能放在 /home 或 /root）。无交互部署明确加 `--non-interactive`。

**v0.2.x 首次升级必须重新运行上面的最新安装链接。** 旧更新工具只支持静态原型包，会保留旧版而拒绝新主控包；新链接获取最新已发布管理工具，安装真实后端并保留域名、HTTPS、旧发布包及证书。无需重装 Nginx，不连接用户服务器 SSH。

首次生成 `admin` 账号及随机密码，直接在安装终端输出，账号不写进前端、GitHub 或公开接口。请保存密码，再进入 `https://自己的域名/admin` 登录。忘记密码：

```bash
sudo xiaowork-watch reset-admin
```

重置会撤销现有管理员登录会话。菜单 9 同样支持，确认接受 `yes/y`，不区分大小写。

## 管理菜单

```bash
sudo xw
sudo xiaowork-watch status
sudo xiaowork-watch update
sudo xiaowork-watch auto-update off
sudo xiaowork-watch auto-update on
sudo xiaowork-watch rollback
```

`sudo xw` 是固定的管理菜单入口，支持 `sudo xw status`、`sudo xw update` 等同样的子命令；原命令 `sudo xiaowork-watch` 仍然可用。已有安装先执行 `sudo xiaowork-watch update`，再执行一次 `sudo xiaowork-watch status` 补齐短命令；等待自动更新到 v0.4.1 后也可执行一次原命令来补齐，不需要重新安装。若同名 `xw` 属于其他程序，会保留该程序并提示使用原命令；卸载也不会删除其他程序的短命令。

main 推送通过检查后发布，服务器约每 15 分钟查询一次最新完整发布包。更新校验 SHA256、提交与包类型，停止旧主控后原子切换前后端并重启。短暂升级期间修改请求返回 503，后台调度暂停；健康检查和安装记录完成后才恢复写入。网站或主控健康检查失败会恢复原版、配置及跨数据结构升级前的数据库，避免已确认的写入被回滚。SQLite 位于 `shared/data`，更新保留监控、历史、账号和探针凭据。

回退会暂停自动更新，并同时回退前后端代码。v0.4 使用数据结构版本 2，不能回退到 v0.3 或 v0.2 的不兼容代码；同数据结构版本可正常回退。升级前在私有 `shared/data` 目录保存 `watch.schema1-before-v2.*.sqlite3` 及 `watch.activation-schema*-*.sqlite3` 备份（目录 700、备份 600），正常代码回退不改变数据库。网站、管理员、VPS、凭据和历史继续保留。

服务日志：

```bash
sudo journalctl -u xiaowork-watch-backend.service -n 50 --no-pager
sudo journalctl -u xiaowork-watch-update.service -n 50 --no-pager
```

## 域名和 HTTPS

管理菜单 2 配置 HTTP 域名反代，菜单 8 配置 HTTPS。域名 A 记录指向服务器 IPv4，开放 80/443，移除未由此站点托管的 AAAA 记录。输入域名和邮箱，确认 DNS、端口及 Let's Encrypt 条款后，输入 `yes` 或 `y` 申请。

Certbot 使用本项目的独立证书、账户、工作和日志目录，不读取或修改已有全局/用户配置，不执行全局 hooks。签发失败恢复原反代，成功后 HTTP 跳转 HTTPS，专用定时器自动续期。主控默认 8091 只监听 loopback（网站自选 8091 时主控改用 8092），公开访问统一通过 Nginx。公网管理员登录、修改及探针上报要求 HTTPS，HTTP 仅支持公开查看。

```bash
sudo xiaowork-watch renew-https
sudo journalctl -u xiaowork-watch-certbot-renew.service -n 50 --no-pager
```

## 真实检测与探针

管理员在后台新增网站并设置间隔（30–86400 秒）和超时（1000–30000 毫秒）；主控自动检测，不依赖浏览器保持打开。非 2xx/3xx、DNS、网络超时和 TLS 验证失败会记录失败详情。GET/HEAD 不上传请求体或凭据。目标仅限公网地址，每次 HTTP 跳转重新验证目标，防止请求落入主控内网。

在“测速目标”添加名称、域名/IP 和 ICMP / TCP 方式。TCP 支持 `hb-ct-v4.ip.zstaticcdn.com:80`，ICMP 不填端口；目标只需网络可达，不安装程序。添加 VPS 时关联这些目标，再在 VPS 记录生成探针安装命令，粘贴到该 VPS 的 Ubuntu/Debian SSH 终端。公网安装要求 HTTPS 主控，命令固定探针 SHA256，使用 10 分钟单次安装码兑换 VPS 记录绑定凭据。安装创建专用受限用户、服务和配置，探针只主动连主控。

VPS 每 30 秒心跳，超过 90 秒显示离线；每分钟从该 VPS 向目标发起 5 次真实 ICMP Ping 或 TCP 连接。DNS 在 VPS 本机解析，仅向校验后的公网 IP 发起测量；端口关闭、连接拒绝、超时会产生真实失败统计，执行或解析错误单独显示。ICMP 为丢包率，TCP 为连接失败率。VPS 离线时保留旧结果并标明过期，不生成假数据。

**v0.3 升级步骤：** 主控执行 `sudo xiaowork-watch update`（也可等待自动更新），刷新页面；对每台已有 VPS，在安装弹窗选择“升级已安装探针”，复制命令到这台 VPS 执行。原地升级保留配置及凭据，校验归属和源码后才替换，失败恢复原代码。旧探针未升级时继续心跳，后台显示需升级，不会领取新协议任务。旧测试节点记录保留并停用，需要填写真实目标地址后启用；旧方向历史不会混入当前测量。原测试节点服务器上的旧探针可分别卸载。

探针日志和卸载：

```bash
sudo journalctl -u xiaowork-watch-agent.service -n 50 --no-pager
curl -fsS https://自己的主控域名/agent/install.sh -o watch-agent-install.sh
sudo bash watch-agent-install.sh --uninstall
```

## 彻底卸载主控

菜单 7 输入 `UNINSTALL`，清理本项目网站、反代、后端服务、更新和续期任务，以及本项目的监控数据库、升级备份、账号、历史包、配置与证书。Nginx、Certbot 及其他站点保留。已经安装在其他 VPS 上的探针需分别卸载，主控不远程执行卸载命令。

旧安装或网站异常时可直接用最新工具：

```bash
curl -fsSL https://raw.githubusercontent.com/xiaowork-dev/xiaowork-watch/main/install.sh | sudo bash -s -- --uninstall --purge
```

无交互卸载加 `--confirm`；自选目录加 `--path`。仅撤下服务、保留数据可用 `sudo xiaowork-watch uninstall --confirm`（不加 `--purge`）。归属或路径检查失败会停止并显示原因。

## 手动源码部署

保留 Git 源码部署方式，安装 Python 3.9+、Node.js 20.19+ 或 22.12+、Nginx。源码目录与一键安装目录分开使用：

```bash
git clone https://github.com/xiaowork-dev/xiaowork-watch.git /opt/xiaowork-watch-source
cd /opt/xiaowork-watch-source
npm ci
npm run build
sudo useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin --comment 'xiaowork Watch backend' xiaowork-watch
sudo install -d -o xiaowork-watch -g xiaowork-watch -m 700 /var/lib/xiaowork-watch
sudo -u xiaowork-watch python3 backend/server.py --data-dir /var/lib/xiaowork-watch --init-admin
```

将后端作为自己的 systemd 服务托管，使用 `User=xiaowork-watch`、`Group=xiaowork-watch`，执行 `/usr/bin/python3 /opt/xiaowork-watch-source/backend/server.py --host 127.0.0.1 --port 8091 --data-dir /var/lib/xiaowork-watch`，配置 `Restart=on-failure`、`UMask=0077`。后端源码目录须允许服务用户读取。

Nginx root 指向 `/opt/xiaowork-watch-source/dist`，SPA 用 `try_files $uri $uri/ /index.html`；`/api/`、`/agent/` 两个 `location ^~` 代理到 `http://127.0.0.1:8091`，保留 `Host $http_host` 和 `X-Forwarded-Proto $scheme`，设置 `client_max_body_size 64k`、`proxy_read_timeout 40s`，禁止隐藏文件并对 HTML/API 使用 no-cache/no-store。通过已有证书工具配置 HTTPS。

后续 `git pull --ff-only origin main` → `npm ci` → `npm run build` → 重启自己的后端服务并检查健康。手动部署不会使用一键安装器的自动更新/回退/卸载工具，数据目录持续保留。

## 文件与发布

安装目录的 `current`/`previous` 指向受验证代码包，`control` 指向稳定运维工具；`shared/assets` 保留 hashed 前端资源，`shared/data` 是仅后端用户可访问的 SQLite 数据目录，证书位于 `shared/letsencrypt`。发布包中的 `.backend`/`.agent`/`.deploy` 均由 Nginx 隐藏文件规则保护；探针源文件仅通过后端专用端点提供。

GitHub Actions 在 main 推送后检查 Ubuntu/Debian Certbot、真实本地 HTTP 与 Ping、权限、数据持久化、安装/更新/卸载，然后构建前端并发布 `monitoring-server` 包。没有测试或部署用户实际服务器；公网目标的最终可达性取决于部署服务器和节点的网络。
