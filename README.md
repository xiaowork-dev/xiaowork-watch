# xiaowork Watch

面向个人开发者的 网站与 VPS 监控平台。本次交付为 v0.2.6 **可交互的前端原型**，采用 Vue 3 + Vite + Axios；实际后端仍按开发文档规划使用 Java 21 + Spring Boot + MySQL 8 + MyBatis-Plus。

## 原型范围

网站监控列表、创建/编辑表单、启用/停用、删除确认、演示检测、详情与分页历史。新增 VPS 管理、自定义测试节点、节点到 VPS 的延迟与丢包视图，以及 Ubuntu/Debian 安装引导。未实现后端检测器、后台调度、数据库、身份验证或真实 Linux 探针。

首次访问时，网站监控、VPS、测试节点和检测历史均为空，由自己添加。更新保留当前浏览器中已有的配置、历史和旧样例。原型不会请求目标地址或发送 Ping；添加后产生的检测结果仍为演示情境，本地修改仅保存在当前浏览器，不代表线上服务状态。停用是独立的 enabled 配置，不改变最近一次 UP/DOWN/UNKNOWN 检测结果。

## VPS 与测试节点

真实版本的设计为：网站是主控，VPS 探针上报心跳，测试节点向选定的 VPS 测量 ICMP 延迟与丢包。当前界面演示分别显示探针在线状态和线路测量结果，不把节点离线当成 VPS 故障。

安装区目前只有全部注释的命令模板；复制模板不会安装服务或注册探针。独立的“演示注册上线”按钮只更新本地示例数据。真实安装需要部署主控后端与 Linux 探针。详细实施约定见 [VPS 与节点设计](docs/design/vps-v0.2.md)。

## 本地启动

使用 Node.js 20.19+（20.x）或 22.12+。本机 Node.js 20.20.2 已验证构建通过：

```powershell
npm install
npm run dev
```

访问终端输出的本地地址。生产构建使用 `npm run build`。

## 服务器部署

保留自己拉取 Git 源码、构建并使用 Nginx 托管的方式。也可在 Ubuntu/Debian 的 SSH 终端用一条命令打开部署菜单：

```bash
curl -fsSL https://raw.githubusercontent.com/xiaowork-dev/xiaowork-watch/main/install.sh | sudo bash
```

首次显示“部署 / 彻底卸载 / 退出”；选择部署成功后，自动进入管理菜单。以后运行 `sudo xiaowork-watch` 打开菜单，可配置域名 HTTP 反代、HTTPS、更新、回退、开关自动更新、查看日志及卸载。默认访问 `http://服务器IP:8088`。main 推送后自动构建发布，服务器约每 15 分钟检查更新。回退不会降级管理菜单。此安装器仅部署当前前端原型。

菜单 **7. 彻底卸载 xiaowork Watch** 只需输入一次 `UNINSTALL` 确认，移除网站、反代、管理命令和专用任务，并清理本项目的历史包、安装配置及证书。保留 Nginx、Certbot 和其他站点。旧版本或网站已停止时，可用最新安装链接直接卸载：

```bash
curl -fsSL https://raw.githubusercontent.com/xiaowork-dev/xiaowork-watch/main/install.sh | sudo bash -s -- --uninstall --purge
```

此命令仍会提示确认；自选目录须补 `--path`。需要保留历史包和证书时使用 `sudo xiaowork-watch uninstall --confirm`，详见部署说明。

需要 HTTPS 时，先把域名 A 记录指向 VPS 并开放 80/443 端口，再选择菜单 **8. 配置 HTTPS**，输入域名、邮箱并确认。程序申请 Let's Encrypt 证书，配置 HTTP 跳转 HTTPS，并独立自动续期。当前入口使用 IPv4，请移除该域名的 AAAA 记录。切换协议或端口会使用新的浏览器本地存储，因此 HTTPS 首次打开也是空列表；原 HTTP 来源的数据保留。

旧 v0.2.1 服务器可先运行 `sudo xiaowork-watch update`，再运行 `sudo xiaowork-watch`。无交互终端时显式加 `--non-interactive` 部署。自选端口、域名、手动源码部署和管理命令见 [服务器部署说明](docs/deployment/server.md)。

## 文件结构

```text
src/App.vue              页面与交互
src/style.css            浅色界面、响应式布局
src/components/          表单和状态徽标
src/api/monitors.js       Axios API 适配器
src/api/demo.js           网站本地示例适配器
src/api/fleet.js          VPS/测试节点数据适配器
docs/design/             设计范围与交接约定
docs/requirements/       原始开发文档
```

## 后续接入

`VITE_DATA_MODE=api` 选择真实 API 适配器；原型默认 demo。此开关只选择数据层，**不会创建后端**。开发代理将 `/api` 转发至 `127.0.0.1:8080`；生产需配置同域反向代理或设置 `VITE_API_BASE_URL`。`VITE_` 变量会进入公开前端包，不应存放秘密。

分页与 JSON 命名暂按 `docs/design/ui-v0.1.md` 和 `docs/design/vps-v0.2.md` 中的草案约定。真实后端尚未运行或验收，当前不应将原型用作生产监控系统。
