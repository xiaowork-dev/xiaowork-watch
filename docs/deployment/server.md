# 服务器部署与 GitHub 自动更新

当前 v0.2.3 提供两条部署路径：自己拉取源码构建，或通过中文终端菜单部署 GitHub 的发布包。两种方式部署的都是**前端交互原型**；尚无真实主控后端、数据库或 Linux VPS 探针。首次访问的网站、VPS、测试节点及检测历史为空，由自己添加。已有浏览器中的配置、历史和旧样例不会自动清除。

## 方式一：一键安装并自动更新

支持使用 systemd 的 Ubuntu/Debian。以 root 或有 sudo 权限的账号在自己的服务器执行：

```bash
curl -fsSL https://raw.githubusercontent.com/xiaowork-dev/xiaowork-watch/main/install.sh | sudo bash
```

在 SSH 终端执行后，未安装时首先显示小菜单：**1. 部署 / 2. 卸载 / 0. 退出**。选择部署后，脚本安装 Nginx、Python 3、curl 和 CA 证书，下载最新完整的 GitHub 前端发布包，校验 SHA256，并启动自动更新定时器。成功后自动进入管理菜单。服务器不需要 Node.js，不需要把 SSH 私钥提供给 Codex 或配置 GitHub Secrets。

安装后运行 `sudo xiaowork-watch` 或再次执行安装链接，即可打开管理菜单：查看状态、域名反代、更新、回退、自动更新开关、日志、卸载，以及 **8. 配置 HTTPS**。菜单输入从 `/dev/tty` 读取，支持 `curl | sudo bash`；无交互终端时不会默认开始部署。

默认目录 `/opt/xiaowork-watch`，默认监听 **8088**，浏览器访问 `http://服务器IP:8088`。如云安全组或防火墙限制入站，需要允许所选端口。脚本新增专用 Nginx 站点，不删除其他站点。

自选端口、域名和目录：

```bash
curl -fsSL https://raw.githubusercontent.com/xiaowork-dev/xiaowork-watch/main/install.sh | sudo bash -s -- --port 8088 --domain watch.example.com --path /opt/xiaowork-watch
```

`watch.example.com` 是占位域名，替换为自己的域名并配置 DNS。上面的参数选择原站点端口和域名；管理菜单中的“域名反代”另建 80 端口 HTTP 入口，转发到当前本地站点端口。反代配置先检查、重新加载，失败恢复原配置；不会修改其他站点。同域名已有站点时先自行处理冲突。若网站本身已经监听 80，无需再建立 80 到 80 的反代。

首次部署提供 HTTP。需要 HTTPS 时，在管理菜单选 8，按后面的步骤申请证书。已启用 HTTPS 的站点不会因为误选 HTTP 反代而静默降级。

安装目录必须是空目录，或已有本安装器的标记。已完整安装后，重复执行脚本会打开管理菜单，单纯打开菜单不会更新网站或恢复自动更新。更改原站点端口请编辑专用 Nginx 配置及 `config.json` 中的本地健康检查地址，再检查、重新加载。失败安装会撤下本次新建的配置，保留已下载文件供重试；同名配置已有其他内容时会停止，不覆盖其他项目。

无人值守时显式跳过菜单：

```bash
curl -fsSL https://raw.githubusercontent.com/xiaowork-dev/xiaowork-watch/main/install.sh | sudo bash -s -- --non-interactive
```

旧 v0.2.1 已安装的服务器可直接运行 `sudo xiaowork-watch update`，成功后运行 `sudo xiaowork-watch` 打开新菜单；也可再次执行安装链接获取菜单。旧版兼容入口只加载新版管理工具，不自动更改当前网站版本或回退暂停状态。

更新与管理：

```bash
sudo xiaowork-watch
sudo xiaowork-watch status
sudo xiaowork-watch update
sudo xiaowork-watch rollback
sudo xiaowork-watch proxy watch.example.com
sudo xiaowork-watch auto-update off
sudo xiaowork-watch auto-update on
```

- 自动更新约每 15 分钟查询一次 GitHub，并附带随机延迟。
- `update` 立即检查并更新，重新允许自动更新。
- `rollback` 返回前一版，并把 `autoUpdate` 设置为 false，避免下一次检查马上装回刚退掉的版本。确定新版可以使用后执行 `update` 恢复。
- 如需完全关闭定时检查：`sudo systemctl disable --now xiaowork-watch-update.timer`。恢复：`sudo systemctl enable --now xiaowork-watch-update.timer`。
- 日志：`sudo journalctl -u xiaowork-watch-update.service -n 50 --no-pager`。

### 配置 HTTPS

先把目标域名的 **A 记录指向 VPS 的 IPv4 地址**，并开放入站 **80 和 443**。当前反代入口仅监听 IPv4，请移除该域名的 AAAA 记录，避免验证请求走向未托管的 IPv6 地址。证书申请使用 Let's Encrypt HTTP-01 验证，需要公网能访问域名的 80 端口。已有旧菜单时先输入 0 退出，在 Shell 中执行：

```bash
sudo xiaowork-watch update
sudo xiaowork-watch
```

选 **8. 配置 HTTPS**。输入自己的域名和邮箱；当前已配置的反代域名会作为默认值。确认 DNS、端口及 Let's Encrypt 服务条款后，输入 `YES` 开始申请。成功后 HTTP 自动跳转 HTTPS，Nginx 反代通过 443 提供加密访问，本地原站端口和更新健康检查继续使用 HTTP。

程序按需安装 Certbot 和 OpenSSL，以 webroot 方式申请证书，只操作本项目专用的反代配置。证书、账户和日志位于安装目录下的 `shared/letsencrypt`、`shared/certbot-work`、`shared/certbot-logs`；验证文件位于 `shared/acme`。账号与私钥不放入 GitHub 或网页公开目录。签发或配置检查失败时恢复原站点配置；重新申请不强制每日签发新证书。

如果服务器已有带配置内容的全局 Certbot `cli.ini`，工具会停止证书操作并显示文件路径，保留原配置，避免继承其他站点的签发选项或钩子。不要直接删除其他站点使用的配置；这种情况下可继续沿用自己已有的证书管理方式。

本项目的 `xiaowork-watch-certbot-renew.timer` 每天检查续期，带随机延迟；只有接近到期时 Certbot 才更新证书。续期成功后检查并重载 Nginx。暂停网页自动更新或回退网页版本不会停掉证书续期。查看日志与立即检查：

```bash
sudo journalctl -u xiaowork-watch-certbot-renew.service -n 50 --no-pager
sudo xiaowork-watch renew-https
```

浏览器本地数据按域名、协议和端口分别保存。从 HTTP 切换到 HTTPS 后属于新的访问来源，列表会首次以空数据开始；HTTP 来源中的旧数据仍在原浏览器中。当前原型没有服务器数据库，不会自动跨来源迁移配置。

DNS 未生效、错误 AAAA 记录、80/443 未开放或已有域名站点冲突时，应先修正再尝试。证书签发与公网验证在自己 VPS 上执行；CI 仅使用本地临时证书检查 Nginx、反代和挑战路径。[Certbot 官方说明](https://eff-certbot.readthedocs.io/en/stable/using.html)介绍 webroot 验证与自动续期。

卸载在菜单中选择后，需要再次输入 `UNINSTALL` 确认。卸载先校验各入口归属，停用网站自动更新和专用证书续期任务，再移除本项目的站点、反代、命令和 systemd 配置，以及当前/前一版/管理入口指针和安装完成标记。`releases/`、`shared/`（包含证书和账户）、`config.json` 与目录标记保留，方便重新部署；Nginx/Certbot 软件、其他站点、全局证书任务和手动源码部署目录保留。没有确认不会执行卸载。明确需要无交互卸载时可运行 `sudo xiaowork-watch uninstall --confirm`。

程序存储结构：

```text
/opt/xiaowork-watch/
  .xiaowork-watch-managed    安装目录标记
  .installation-complete    完整安装标记
  config.json               自动更新开关与本地健康检查地址
  installed.json            当前版本记录
  current -> releases/SHA   当前网页版本
  previous -> releases/SHA  前一版
  control -> releases/SHA/.deploy  稳定管理入口，网页回退不降级菜单
  releases/                 完整发布包解压目录
  shared/assets/            跨版本保留的静态资源
  shared/acme/              HTTP-01 验证目录
  shared/letsencrypt/       本项目证书和账户（私有）
  shared/certbot-work/      本项目证书工作目录（私有）
  shared/certbot-logs/      本项目证书日志（私有）
```

每次更新先下载到独立目录，核对发布标签、包校验和、文件路径和 `release.json` 中的提交 SHA。完整验证后原子切换 `current`；本地 HTTP 健康检查失败会恢复原版本。旧版本与旧 hashed assets 保留，不自动清理。此流程只管理前端文件，未来数据库与探针升级须另行接入，不在当前脚本中执行。

## 方式二：自己拉取源码构建

保留普通 Git 部署流程。在服务器安装受项目支持的 Node.js 与 npm，首次部署：

```bash
git clone https://github.com/xiaowork-dev/xiaowork-watch.git /opt/xiaowork-watch-source
cd /opt/xiaowork-watch-source
npm ci
npm run build
```

将 Nginx 的网站根目录指向 `/opt/xiaowork-watch-source/dist`，配置端口或域名后运行 `sudo nginx -t`，通过后重新加载 Nginx。`npm run dev` 和 `npm run preview` 不用作正式托管服务。

后续更新，每步成功后再执行下一步：

```bash
cd /opt/xiaowork-watch-source
git pull --ff-only origin main
npm ci
npm run build
```

这条路径由你手动更新。源码构建目录和一键安装器目录各自独立；不要对同一个部署目录混用两种流程。配置自己的 Nginx 时，可参考一键安装脚本中的 no-cache HTML 和隐藏文件规则。源码部署的 `/assets` 直接来自 `dist/assets`，不要复制安装器专用的 `shared/assets` alias。

## GitHub 的自动发布流程

`.github/workflows/release.yml` 在 main 每次推送后自动运行：安装锁定依赖 → 检查安装/更新测试 → 构建 Vue 前端 → 打包和生成 SHA256 → 创建草稿 Release → 上传完整资产 → 公开发布为 latest。

发布标签为 `web-完整提交SHA`，资产为 `xiaowork-watch-web.tar.gz` 和同名 `.sha256`。只发布当前 main 的成功构建，旧提交重跑不能取代最新版本；已公开的同标签包不能被覆盖。前端发布包不包含 `.env`、源码、node_modules 或 OpenAI 发布配置。

自己部署的服务器主动读取公开的 GitHub Release，无需 SSH 连接或访问凭据。GitHub 构建失败、网络失败、受 API 限流、校验失败或版本格式不认识时，服务器保留旧版。main 推送完成并不等于所有服务器已更新；通常需等待构建和下一次定时检查。首次发布尚未成功时，安装脚本会报没有完整发布包，需要等待 Actions 完成。

如果以后同仓库开始发布真实主控或探针包，应为其定义单独更新通道，再调整此安装器；当前只接受 `frontend-prototype` 包，不把后端或未知资产当作前端安装。
