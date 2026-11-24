# 变更记录

按时间倒序。每条分「可感知变化」与「技术细节」两部分，便于快速判断某次改动是否影响自己。

---

## 2026-11-24 · 交接整理

**可感知变化**
- 仓库首次公开，补齐技术文档、运维手册、交接清单
- 两轮开发的对话记录与需求文档归档到 `docs/history/`

**技术细节**
- 新增 `docs/TECHNICAL.md`、`docs/OPERATIONS.md`、`docs/HANDOVER.md`
- `cloudbaserc.json` 改为不入库，提供 `cloudbaserc.example.json`
- 代码无功能性改动

---

## 2026-11-23 · 闲鱼双通道 + 采集诊断

**可感知变化**
- 闲鱼店铺可以自动采集了：在售数、店名、商品清单（标题/价格/链接）
- 闲鱼店铺详情页不再隐藏历史、导出；指标列显示「在售数」
- 新增诊断接口，能在不登录服务器的情况下看清采集被卡在哪一步
- 云端闲鱼商品仍失败，但失败原因从「没有返回可核对指标」变成明确的「被风控拦截（RGV587）」

**技术细节**
- 新增 `monitor/xianyu_shop.py`：读 `mtop.idle.web.user.page.head`（在售数）与 `mtop.idle.web.xyh.item.list`（清单），appKey `34839810`
- 新增 `monitor/xianyu_product.py`：H5 `mtop.taobao.idle.awesome.detail`，mtop 签名 + 两次调用取 `_m_h5_tk`，浏览器兜底
- 新增 `monitor/xy_browser.py`：统一 Playwright 启动（Chromium 131 UA、隐藏 `navigator.webdriver`、支持 `MONITOR_XIANYU_PROXY`）
- 新增 `monitor/probe.py` 与 `GET /api/v1/admin/probe`
- 详情接口先试 PC appKey `34839810` 再回落 H5 `12574478`
- 实测：机房 IP 两条 appKey 全部 `RGV587_ERROR`；同 IP 下店铺两个接口正常
- 前端 `app.js` v31

---

## 2026-11-23 · 统计口径修正

**可感知变化**
- 整点表里没有增量的格子显示 `0`，不再显示 `—`
- 图表底部加了真实的每小时时间轴，标签过密时自动稀疏
- 补采/漏采的小时不再整天留空
- 计数回退（销量被重置）后，当天剩余整点继续算增量，而不是全空

**技术细节**
- `compute_deltas`：小时增量改为回溯最多 6 小时；相邻判断从「采集时刻差」改为「slot 差值」
- 今日增量：遇 `counter_reset` 从回退点重新起算；当天首条即基准时返回 `0` 而非 `NULL`
- 新增 `strict_hourly` 概念：只有严格相邻整点的增量参与规则判断，回溯值仅展示
- `drawChart` 改为真实时间比例横轴 + 整点网格
- 前端新增 `deltaOrZero`，定时快照空增量渲染为 0

---

## 2026-11-22 · 上线与运维能力

**可感知变化**
- 部署到腾讯云 CloudBase 云托管，带 HTTP Basic 登录
- 站点内可下载全量备份；命令行可备份/恢复
- 发布清库后可一键恢复，且恢复会重算历史增量

**技术细节**
- `Dockerfile`：python 3.12-slim + Playwright Chromium，端口 8080，`MONITOR_CLOUD=1`
- `monitor/app.py`：Basic Auth 中间件（`compare_digest`）、同源校验、`/healthz` 豁免
- 新增 `GET /api/v1/admin/export`、`POST /api/v1/admin/restore`（`empty` / `replace` 两种模式，整体事务）
- 新增 `tools/backup_cloud.py`（保留 30 份）、`tools/restore_cloud.py`
- 恢复后调用 `recompute_deltas()` 重算所有定时快照增量

---

## 2026-11-22 · 首版

**可感知变化**
- 导入闲鱼/小红书分享链接，自动识别商品或店铺
- 北京时间整点自动采样，浏览器关掉也继续
- 监控池列表、详情页、整点表、今日/小时增量、CSV 导出
- 手工录读数、粘贴页面文字自动提取价格与想要数
- 异动规则与钉钉/企微通知

**技术细节**
- FastAPI + SQLite，进程内调度（`worker.cycle` 每 8 秒），45 分钟采样窗口
- 八张表：`targets` / `snapshots` / `jobs` / `shop_catalogs` / `import_readings` / `events` / `deliveries` / `rules`
- 链接白名单 + 短链最多跳 3 次，防 SSRF
- 小红书商品：公开 JSON 接口 + 浏览器兜底
- 小红书店铺：解析公开页 `window.__SETUP_SERVER_STATE__`
- 24 条单元测试
