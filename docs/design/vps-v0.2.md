# VPS 与测试节点：v0.2 交互原型及后端约定

## 本轮需求

2026-10-02 用户补充：主控端是网站，VPS 安装探针作为被控端；允许自己添加测试节点；检测方向为测试节点 → VPS；Linux 优先 Ubuntu/Debian；其他功能保持简单。本轮沿用先交付前端交互原型的范围，真实后端和探针另行实施。此需求覆盖 v0.1 文档中暂不做 Probe 的限制。

## 页面与流程

保留网站监控，侧栏增加 VPS 监控和测试节点。

- VPS：新增、编辑、选择测试节点、启停测量、安装引导、删除。详情按节点显示延迟、丢包、测量状态与时间，并提供分页历史。
- 测试节点：新增、编辑地区、启停测量、安装引导、删除。节点保存后需要在 VPS 编辑页勾选，才加入该 VPS 的测量。
- 安装：保存目标 → 对应服务器安装 → 首次注册/心跳 → 在线。VPS 与测试节点复用一个探针程序，通过角色区分任务。

CPU、内存、磁盘、告警、账单和复杂权限暂不加入本版。

## 状态与测量口径

VPS 探针只负责向主控注册及上报心跳；延迟任务由测试节点执行。探针心跳在线、监控 enabled、网络测量结果为三个独立状态。示例洛杉矶 VPS 展示“探针离线，但东京节点仍能收到 Ping 回复”，用于说明它们不等价。

第一种测量固定为 ICMP Ping，每次 5 包，展示收到回复包的平均往返时延 `avgRttMs`，以及 `(sent-received)/sent*100` 丢包率。没有成功回复时延迟为 null，显示“—”；发出 5 包且没有回复才显示 100% 丢包。Ping 无回复可能是防火墙禁止 ICMP，不能直接认定 VPS 关机。

节点离线、待安装或停用时不生成新的失败记录；历史数值保留并标注仅供参考。最近结果超过 180 秒显示数据已过期。浏览器每 15 秒更新过期标签，不触发网络检测。真实版心跳间隔建议 30 秒，主控超过 90 秒未收到心跳标记 OFFLINE；计划测量间隔 60 秒。

## 最小对象

| 对象 | 字段 |
| --- | --- |
| VPS | id, name, address, region, enabled, nodeIds, agentState, lastSeenAt |
| 测试节点 | id, name, region, enabled, agentState, lastSeenAt |
| 测量 | id, hostId, nodeId, nodeName, region, sent, received, avgRttMs, status, checkedAt |
| 安装信息 | demo, command, expiresAt |

`address` 接受 IPv4、IPv6 或主机名，不接受协议、端口、路径。主控应再次校验并限制允许的目标范围；探针执行 Ping 时通过参数数组调用命令，不拼接 shell 字符串。

删除 VPS 同时清理测量历史。更换 VPS 地址后清除旧测量，表单中提前提示。删除节点解除 VPS 关联，保留测量及节点名称快照，历史标记“节点已移除”。真实版删除应同时撤销对应探针凭据。

## 原型边界

独立使用 `xiaowork-watch-fleet-v1` localStorage；不会覆盖网站监控的 `xiaowork-watch-prototype-v1`。默认地址均使用文档用途 IP。在线状态、延迟、丢包都是示例，不发送 ICMP，不安装服务，没有后台调度。

安装模板每行均以 `#` 注释，无法执行；复制模板不会改变探针状态。只有单独点击“演示注册上线”才更改本地示例心跳。命令中使用 `$CONTROL_URL` / `$ONE_TIME_TOKEN` 占位，没有真实凭据。不要将该模板作为实际部署命令。

## 后端接口草案

沿用 Java 21 + Spring Boot + MySQL 8，统一响应 `{code,message,data}`。API 模式不回退到演示数据。

| 操作 | 接口 |
| --- | --- |
| 列表和节点最新结果 | GET `/api/fleet` → `{hosts,nodes,results}`，results 只需每一对节点/VPS的最新记录 |
| VPS CRUD | GET/POST `/api/vps`，PUT/DELETE `/api/vps/{id}` |
| 节点 CRUD | GET/POST `/api/probes`，PUT/DELETE `/api/probes/{id}` |
| 启停测量 | PATCH `/api/vps/{id}/enabled`、`/api/probes/{id}/enabled`，body `{enabled}` |
| 生成一次性安装信息 | POST `/api/vps/{id}/enrollment`、`/api/probes/{id}/enrollment` |
| 排队执行测量 | POST `/api/vps/{id}/checks`，异步返回，不等待 Ping 完成 |
| 历史分页 | GET `/api/vps/{id}/checks?page=1&size=8` → `{records,total,page,size}` |

真正的主控部署在用户服务器上；当前静态预览不提供这些 API。主控管理接口需要单管理员登录保护。

## 真正安装与探针的下一阶段

1. 发布 Linux amd64/arm64 的单一探针程序和安装脚本，优先支持 Ubuntu/Debian + systemd。脚本下载固定版本、验证校验和，创建受限服务用户和 systemd 单元；卸载时停止服务并删除本探针自己的文件。
2. 主控在后台生成短期（建议 10 分钟）、一次性、绑定角色与目标 ID 的注册码。管理员页面仅临时显示命令，不写入浏览器持久存储、前端构建或日志。重复生成时撤销旧注册码。
3. 安装脚本向 HTTPS 主控登记，注册码只用一次，随后交换为单独的探针凭据；权限绑定到一台 VPS 或一个节点。凭据放在受限文件，支持撤销；离线重连不重复注册。
4. 探针只主动连接主控，不需要主控 SSH 登录，也不需要额外打开入站管理端口。目标 VPS 必须允许来自测试节点的 ICMP；主控也可作为用户明确添加的一个测试节点。
5. 节点拉取有明确 target / timeout / jobId 的任务，执行有上限的 Ping，提交 sent / received / avgRttMs。主控验证角色、目标关联和 jobId 幂等性，保存结果并处理超时；执行错误与发出请求后的无回复分开记录。

以上为实施约定，当前仓库尚未包含可发布的 agent/install.sh、探针二进制或后端。

## 验收

检查网站监控仍可使用；VPS 详情展示上海、东京和离线法兰克福节点；无回复显示空延迟及 100% 丢包；离线节点显示旧结果；新增节点与 VPS 均等待安装；复制模板不注册；演示注册后可选择节点并演示检测；节点删除后保留历史；刷新后本地配置保留；移动端三入口均可点击、宽表只在自身滚动；演示流程没有向示例地址发出请求。
