# 链接监控台 Sites 版

原生 HTML、CSS 和 JavaScript 前端由 Cloudflare Worker 提供。Worker 同时处理管理员登录、商品与店铺查询和采样 API。D1 数据库保存监控池与历史记录。小红书支持站点采样，闲鱼支持查看历史与手工补录。

## 构建与测试

在本目录使用 Node.js 24 执行以下命令。

```sh
node build.mjs
node --check dist/server/index.js
```

如有监控台导出的备份，可在本机执行 `node check.mjs /path/to/backup.json`。测试使用内存数据库验证管理员登录、历史恢复和主要查询。请将真实备份留在仓库之外。

## 部署

在 Sites 中注册或选择站点，将站点 ID 写入 `.openai/hosting.json` 的 `project_id`，然后重新构建并通过 Sites 发布。仓库里提供可复用的配置，部署时由 Sites 应用 `drizzle/` 中的 D1 迁移。

`LINKWATCH_OWNER_EMAIL` 用于识别站点所有者。`LINKWATCH_MANAGED_ADMIN` 是可选的托管管理员凭据配置，使用时应通过 Sites 的运行时机密设置。

监控站的定时任务关联到 Sites 发布的 MCP 服务，由 `sample_xhs_batch` 分批处理小红书目标。GitHub 上的源代码更新和 Sites 发布是独立操作。
