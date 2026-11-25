# 链接监控台（Store Surveillance）

[![tests](https://github.com/ChenmoWorkshop/store-surveillance/actions/workflows/tests.yml/badge.svg)](https://github.com/ChenmoWorkshop/store-surveillance/actions/workflows/tests.yml)
[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

当前线上版本使用 ChatGPT Sites、Cloudflare Workers 和 D1 SQL，源码和部署说明见 [Sites 版](sites/README.md)。

以下介绍 Python 版。它监控闲鱼、小红书的商品与店铺，按北京时间整点自动采样，记录想要数、展示销量和在售数的变化，并支持异动提醒。

- 后端：Python 3.11 + FastAPI + SQLite，常驻进程自带调度器，浏览器关掉也继续采
- 前端：原生 JS 单页，无构建步骤，由后端直接托管
- 部署：Docker 单镜像，支持腾讯云 CloudBase 云托管

## 当前采集能力

| 平台 | 类型 | 指标 | 通道 | 云端可用 |
|---|---|---|---|---|
| 小红书 | 商品 | 展示销量、价格 | 公开详情 JSON → 浏览器渲染兜底 | ✅ |
| 小红书 | 店铺 | 在售数、商品清单 | 公开店铺页 + 商城商品分页 | 本地实测通过，云端分页待验证 |
| 闲鱼 | 店铺 | 在售数、商品清单 | 电脑版卖家主页 `user.page.head` + `xyh.item.list` 分页 | 本地实测通过，云端分页待验证 |
| 闲鱼 | 商品 | 想要数、价格、状态 | H5 `awesome.detail`（需登录态与干净出口 IP） | ⚠️ 机房 IP 被风控 |

「⚠️」的具体含义：腾讯云机房的出口 IP 会被闲鱼 mtop 网关判为机器人并返回 `RGV587_ERROR`，即使换上真实浏览器指纹也一样。代码已支持用 `MONITOR_XIANYU_PROXY` 指定出口代理；不配置时，闲鱼商品任务会以「被风控拦截」失败，并把原因写进任务记录，不影响其他平台。

整店采集：导入店铺分享链接，点「查看店铺商品」→「采集商品」，完成后可下载商品链接 CSV。遇到缺页会显示已采数量和缺口，也可以导出当前已采到的商品。

## 本地运行

需要 Python 3.11 或以上。

```powershell
python -m venv .venv
& ./.venv/Scripts/python.exe -m pip install -r requirements.txt
& ./.venv/Scripts/python.exe -m uvicorn monitor.app:app --host 127.0.0.1 --port 8765
```

打开 http://127.0.0.1:8765 ，接口文档在 `/docs`。也可以直接跑 `start.ps1`。数据库默认落在 `data/monitor.db`。

跑测试：

```powershell
& ./.venv/Scripts/python.exe -m unittest discover -s tests -q
```

## 部署

复制 `cloudbaserc.example.json` 为 `cloudbaserc.json` 并填入自己的环境 ID，然后按 [`docs/OPERATIONS.md`](docs/OPERATIONS.md) 的步骤构建发布。

**先读这一条**：当前线上服务没有挂持久化卷，**每次发布都会换成新实例、数据库被清空**。发布前必须导出一份备份，发布后核对数量，必要时用 `tools/restore_cloud.py` 恢复。完整流程见运维手册。

## 目录结构

```
monitor/            后端
  app.py            FastAPI 应用、路由、Basic 鉴权、后台调度线程
  worker.py         整点任务生成、重试、异动通知投递
  storage.py        SQLite 表结构、增量计算、备份恢复、CSV 导出
  models.py         Pydantic 输入校验（读数、店铺清单、规则）
  platforms.py      链接解析、平台分发、自定义数据源
  xianyu_product.py 闲鱼商品采集（H5 mtop 接口 + 浏览器兜底）
  xianyu_shop.py    闲鱼店铺采集（电脑版卖家主页）
  xhs_product.py    小红书商品采集
  xhs_public.py     小红书店铺公开页解析
  xy_browser.py     Playwright 统一启动配置（UA、指纹、代理）
  probe.py          采集通道诊断（/api/v1/admin/probe）
web/                前端单页（index.html / app.js / style.css）
tests/              单元测试
tools/              云端备份与恢复脚本
docs/               技术文档、运维手册、交接清单、历史对话记录
```

## 文档

| 文档 | 内容 |
|---|---|
| [docs/TECHNICAL.md](docs/TECHNICAL.md) | 架构、数据模型、API 契约、采集链路、统计口径 |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | 本地运行、云端发布、备份恢复、故障排查 |
| [docs/HANDOVER.md](docs/HANDOVER.md) | 交接现状、未完成事项、下一步优先级 |
| [docs/history/](docs/history/README.md) | 两轮开发的原始对话记录与需求文档 |

## 已知限制

1. **发布清库**：未挂 CFS 持久卷，数据库在容器文件系统内，重建实例即丢。这是当前最大的运维隐患。
2. **闲鱼商品云端被风控**：机房 IP 无法调用详情接口，需要干净出口代理。
3. **店铺清单有采集上限**：每次最多保留 2,000 件。平台请求失败、重复页、超时或在售数对不上时，清单会标为未采齐。

## 许可

MIT，见 [LICENSE](LICENSE)。
