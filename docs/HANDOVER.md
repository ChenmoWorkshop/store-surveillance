# 交接清单

给下一位接手这个项目的工程师。目标：拉下代码、读完这份清单，当天就能独立发布一次、并且知道下一步该做什么。

---

## 1. 项目是怎么来的

| 阶段 | 谁 | 做了什么 |
|---|---|---|
| 需求 | 项目负责人 | 按一份飞书文档定义了口径与流程（原文见 `docs/history/gpt-output/`） |
| 第一版 | GPT（Codex） | 从零实现后端调度、SQLite、前端单页、导入与手工读数；本机跑通 |
| 上线 | GPT | 打成 Docker 镜像，发布到腾讯云 CloudBase 云托管，加 Basic Auth |
| 第二轮 | WorkBuddy | 修统计口径（空增量填 0、整点回溯、计数回退）、图表时间轴、接入闲鱼商品与店铺、加诊断接口、加备份恢复 |

两轮的完整对话流程都在 `docs/history/`，遇到「为什么这么写」可以直接去翻。

---

## 2. 当前状态（2026-11-24）

### 能用的

- 链接导入（闲鱼/小红书，商品/店铺，短链解析）
- 北京时间整点自动采样，45 分钟窗口，失败重试 3 次
- 整点表、图表、今日/小时增量、CSV 导出
- 手工读数、粘贴文字读数、店铺清单对比
- 异动规则 + 钉钉/企微通知
- 全量备份导出与恢复（含增量重算）
- 采集通道诊断 `/api/v1/admin/probe`

### 不能用的 / 有前提的

| 项 | 状态 | 原因 |
|---|---|---|
| 闲鱼商品（云端） | ❌ 失败 | 机房 IP 被 mtop 风控（`RGV587`），需配置出口代理 |
| 闲鱼商品（本地/家宽） | ✅ | 直连即可 |
| 店铺翻页 | ✅ 本地已验证 | 闲鱼 36/36、小红书 24/24，CSV 行数一致。云端待验证 |
| 数据持久化 | ❌ 未配 | 无持久卷，每次发布清库 |
| 多用户/权限 | ❌ 未实现 | 单账号 Basic Auth |
| 自动化测试（前端/采集） | ⚠️ 部分覆盖 | 后端单测覆盖店铺分页和 CSV 导出，前端无自动化测试 |

### 线上实例

- 平台：腾讯云 CloudBase 云托管，服务名 `store-surveillance`
- 部署形态：单实例，端口 8080，健康检查 `/healthz`
- 数据库：容器内 `/var/lib/monitor/monitor.db`（**无持久卷**）
- 访问：HTTP Basic Auth，账号密码由负责人持有

---

## 3. 接手第一天

```powershell
# 1. 装环境
python -m venv .venv
& ./.venv/Scripts/python.exe -m pip install -r requirements.txt
& ./.venv/Scripts/python.exe -m unittest discover -s tests -q

# 2. 跑起来
./start.ps1                                                       # http://127.0.0.1:8765

# 3. 用真实链接验证一条通道（闲鱼店铺最稳）
& ./.venv/Scripts/python.exe -c "from monitor import xianyu_shop as s; print(s.read_shop({'external_id':'<卖家ID>'}))"

# 4. 读三份文档
#    docs/TECHNICAL.md  架构与算法
#    docs/OPERATIONS.md 发布与排障
#    本文件第 4 节      未完成事项
```

然后向负责人要以下信息（**仓库里没有，也不应该有**）：

- [ ] 线上服务的 Basic Auth 账号与密码
- [ ] CloudBase 环境 ID、有部署权限的腾讯云账号（或子账号密钥）
- [ ] 是否已配置钉钉/企微机器人，密钥在哪
- [ ] 是否有可用的出口代理（闲鱼商品采集需要）
- [ ] 备份文件的实际存放位置（仓库只有脚本，`backups/` 被忽略）

---

## 4. 未完成事项

按「投入产出比」排序。每项都标了改哪里，不需要重新摸代码。

### P0 · 挂持久卷，止住发布清库

- 位置：CloudBase 控制台 → 服务设置 → 存储挂载，挂到 `/var/lib/monitor`
- 代码侧无需改动，`MONITOR_DB` 已默认指向该路径，`MONITOR_CLOUD=1` 已强制 `journal_mode=DELETE`
- 验收：连续发布两次，目标数量不变
- 副作用：挂载后第 3 节的「四步发布流程」可简化，但首次仍需恢复一次

### P1 · 闲鱼商品采集恢复

- 位置：`monitor/xianyu_product.py`，出口在 `monitor/xy_browser.py:proxy()`
- 方案 A（推荐）：配置 `MONITOR_XIANYU_PROXY` 指向住宅 IP 代理，代码已支持，零改动
- 方案 B：把闲鱼商品也改成读 `www.goofish.com/item?id=` 的 PC 页面接口（店铺通道在机房是通的，商品页可能同理）——**这条尚未验证**，值得先花半小时用 `/admin/probe` 试
- 验收：`/api/v1/targets/<id>/refresh` 返回成功，`want_count` 有值

### P1 · 云端验证店铺分页

- 本地已验证 `monitor/xianyu_shop.py` 和 `monitor/xhs_public.py` 的分页采集。两平台都核对去重条数与在售数。闲鱼还检查末页标记，小红书也可用分页接口总数确认采齐。单次上限 2,000 件。
- 发布后用星月小铺和文行文化再次核对平台在售数、已采条数和 CSV 行数。

### P2 · 调度去硬编码

- 位置：`monitor/worker.py:cycle()` 里的 `for platform in ("xiaohongshu", "xianyu")`
- 做法：从 `platforms` 导出支持的 `(platform, kind)` 组合，或遍历 `targets` 表里实际出现的组合
- 收益：加平台不用再改调度器

### P2 · 请求限流

- 位置：`monitor/app.py` 的中间件层
- 重点保护 `/refresh`（会触发真实采集）与 `/admin/restore`
- 简单实现：按 IP + 路径的内存令牌桶即可，单实例不存在一致性问题

### P3 · 采集解析单测

- 位置：`monitor/xianyu_product.py:parse_detail`、`xianyu_shop.py:parse`、`xhs_public.py:parse_catalog`
- 这三个都是纯函数，喂一份真实 JSON/HTML 快照就能测，成本很低
- 价值：平台改版时能第一时间知道是解析坏了还是网络坏了

### P3 · 权限分层

- 现状：`/api/v1/admin/*` 与普通接口同一套 Basic Auth，任何持账号者都能覆盖全库
- 做法：拆成两个账号，或在 Basic 之上加一个 `X-Admin-Token`

---

## 5. 必须知道的坑

这些是踩过的，别再踩一次。

1. **发布必清库**。没备份不要发布。恢复脚本必须显式传环境变量，不读配置文件。
2. **`tcb cloudrun deploy` 会问「Enable gray deployment?」**，`--force` 不跳过，后台跑会永久挂起。用 `printf '\n' |` 喂回车，或改用 MCP 部署。
3. **源码包别整个传**。`.venv`、`backups`、`data/monitor.db` 会被打包，几百 MB。用精简目录。
4. **构建要 8–9 分钟**，`getDeployRecords` 的 `Status` 滞后，看 `getManageTask` 的 `Steps`。
5. **本机 DNS 偶发畸形包**，`tcb` CLI 会卡死。`Clear-DnsClientCache` 后重试。
6. **前端改完要升 `app.js?v=N`**，否则用户拿到旧缓存。
7. **改表结构要同步四处**：建表、`ALTER TABLE` 迁移、`dump_database`、`restore_database`。漏最后一处会导致线上恢复失败。
8. **`cycle()` 里平台是硬编码的**，加平台不忘改这里。
9. **增量判断用 slot 差值不用采集时刻差值**，否则补采会算错。
10. **规则只认严格相邻整点**，回溯值仅用于展示。

---

## 6. 代码地图

| 我想改… | 去这里 |
|---|---|
| 采样时间/窗口/重试 | `monitor/worker.py`（`WINDOW`、`cycle`、`failure`） |
| 增量算法 | `monitor/storage.py:compute_deltas` |
| 指标选择（哪个字段算增量） | `monitor/storage.py:metric_column` |
| 表结构 | `monitor/storage.py:init` |
| 备份字段白名单 | `monitor/storage.py:dump_database` / `restore_database` |
| 链接解析 | `monitor/platforms.py:parse_target` / `links` |
| 平台分发 | `monitor/platforms.py:observe` |
| 闲鱼商品采集 | `monitor/xianyu_product.py` |
| 闲鱼店铺采集 | `monitor/xianyu_shop.py` |
| 小红书采集 | `monitor/xhs_product.py` / `xhs_public.py` |
| 浏览器指纹/代理 | `monitor/xy_browser.py` |
| 接口入参校验 | `monitor/models.py` |
| 路由与鉴权 | `monitor/app.py` |
| 页面与交互 | `web/app.js`（1.1k 行，按函数名搜） |
| 图表绘制 | `web/app.js:drawChart` |

---

## 7. 术语

| 词 | 含义 |
|---|---|
| slot（整点槽） | 北京时间某个整点，所有定时快照按它归档 |
| 窗口 | 整点后 45 分钟，超出则该整点任务判失败 |
| 严格相邻 | slot 相差正好 1 小时，只有这种增量参与规则判断 |
| 回溯 | 上一整点缺采时向前找最近有效整点（最多 6 小时），仅用于展示 |
| counter_reset | 平台计数回退（如销量被重置），当天基准改从回退点重算 |
| 想要数 | 闲鱼「N 人想要」，不是销量，不能与小红书销量混算 |
| 在售数 | 店铺当前在架商品总数，店铺类目标的唯一指标 |
| `has_more` | 店铺清单没采全的标记，前端据此显示「尚缺 N 条」 |
