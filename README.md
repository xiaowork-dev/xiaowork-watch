# xiaowork Watch

面向个人开发者的 HTTP/HTTPS 服务监控平台。本次交付为 v0.1 **可交互的前端原型**，采用 Vue 3 + Vite + Axios；实际后端仍按开发文档规划使用 Java 21 + Spring Boot + MySQL 8 + MyBatis-Plus。

## 原型范围

监控列表、创建/编辑表单、启用/停用、删除确认、演示检测、监控详情、倒序分页历史记录。未实现后端检测器、后台调度、数据库和身份验证。

所有默认服务使用保留的 `.example` 域名。原型不会请求这些域名。检测结果为固定演示情境，本地修改仅保存在当前浏览器；不代表线上服务状态。停用是独立的 enabled 配置，不改变最近一次 UP/DOWN/UNKNOWN 检测结果。

## 本地启动

使用 Node.js 20.19+（20.x）或 22.12+。本机 Node.js 20.20.2 已验证构建通过：

```powershell
npm install
npm run dev
```

访问终端输出的本地地址。生产构建使用 `npm run build`。

## 文件结构

```text
src/App.vue              页面与交互
src/style.css            浅色界面、响应式布局
src/components/          表单和状态徽标
src/api/monitors.js       Axios API 适配器
src/api/demo.js           仅限原型的本地数据适配器
docs/design/             设计范围与交接约定
docs/requirements/       原始开发文档
```

## 后续接入

`VITE_DATA_MODE=api` 选择真实 API 适配器；原型默认 demo。此开关只选择数据层，**不会创建后端**。开发代理将 `/api` 转发至 `127.0.0.1:8080`；生产需配置同域反向代理或设置 `VITE_API_BASE_URL`。`VITE_` 变量会进入公开前端包，不应存放秘密。

分页与 JSON 命名暂按 `docs/design/ui-v0.1.md` 中的草案约定。真实后端尚未运行或验收，当前不应将原型用作生产监控系统。
