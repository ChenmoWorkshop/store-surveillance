# 技术文档

面向接手开发的工程师。读完这份文档应当能独立改动采集通道、调整统计口径、扩展平台，并知道每处改动的连带影响。

- 代码规模：后端约 2.4k 行（含注释），前端 1.1k 行，测试 485 行
- 技术栈：Python 3.11 / FastAPI / SQLite / Playwright(Chromium) / 原生 JS
- 部署形态：单容器，Uvicorn 单进程，进程内定时调度

---

## 1. 运行形态

一个进程承担四件事：HTTP 服务、静态页面托管、整点调度、异动通知投递。没有消息队列、没有 Redis、没有独立 worker，所以**实例数必须保持 1**，否则同一整点会被多实例重复采样，`snapshots` 的 `UNIQUE(target_id, slot)` 只能挡住重复写入，挡不住重复请求。

```
┌──────────────────────────── 单容器 ────────────────────────────┐
│  Uvicorn (1 worker)                                            │
│  ├── FastAPI 路由  ── /api/v1/*   Basic Auth（云模式）           │
│  ├── StaticFiles   ── /static/*   web/ 目录                     │
│  └── scheduler 协程（每 8 秒）                                  │
│        └── worker.cycle()                                       │
│               ├── 生成整点 job → ThreadPoolExecutor(2) 采集      │
│               ├── queue_deliveries()  汇总异动                   │
│               └── send_pending()      推送到钉钉/企微             │
│                                                                 │
│  SQLite（MONITOR_DB，默认 data/monitor.db）                      │
└────────────────────────────────────────────────────────────────┘
```

启动入口 `monitor/app.py:lifespan`：校验云模式账号密码 → `storage.init()` 建表/迁移 → 起 `scheduler` 协程。`MONITOR_CLOUD=1` 时用 `journal_mode=DELETE`（网络文件系统上 WAL 不可靠），本地用 WAL。

---

## 2. 模块职责

| 文件 | 职责 | 改动风险 |
|---|---|---|
| `monitor/app.py` | 路由、请求模型校验、Basic 鉴权中间件、同源校验、后台调度启动 | 改路由要同步 `web/app.js` |
| `monitor/worker.py` | 整点 job 生成、重试退避、通知分组与投递 | 采样窗口与重试策略在这里 |
| `monitor/storage.py` | 建表与列迁移、增量计算、写入快照、备份/恢复、CSV 导出 | **改表结构必须同时改 `dump_database` / `restore_database` 与 `TABLES`** |
| `monitor/models.py` | Pydantic 入参模型（读数、店铺清单、规则、链接文本） | 校验放宽会让脏数据入库 |
| `monitor/platforms.py` | 分享链接解析与去重、平台分发、自定义 endpoint 数据源 | 新增平台从这里分叉 |
| `monitor/xianyu_product.py` | 闲鱼商品：mtop 详情接口 + 浏览器兜底 | 依赖闲鱼接口与风控策略 |
| `monitor/xianyu_shop.py` | 闲鱼店铺：卖家主页两个接口 | 同上，但云端可用 |
| `monitor/xhs_product.py` | 小红书商品：JSON 接口 + 浏览器渲染兜底 | 依赖页面结构 |
| `monitor/xhs_public.py` | 小红书店铺公开页解析（`window.__SETUP_SERVER_STATE__`） | 页面改版即失效 |
| `monitor/xy_browser.py` | Playwright 启动参数、UA、`navigator.webdriver` 隐藏、出口代理 | 所有浏览器通道共用 |
| `monitor/probe.py` | 诊断：回放页面、打印 cookie 名与两条 appKey 的返回 | 只读，安全 |
| `web/app.js` | 全部前端逻辑：列表、详情、图表、CSV 下载、规则表单 | 无构建，改完记得升 `?v=` |

---

## 3. 数据模型

SQLite，8 张表。建表语句在 `storage.init()`，列迁移用 `PRAGMA table_info` + `ALTER TABLE`，所以老库可以直接升级。

### 3.1 targets — 监控目标

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | |
| `platform` | TEXT | `xianyu` / `xiaohongshu` |
| `kind` | TEXT | `product` / `shop` |
| `external_id` | TEXT | 商品 ID 或店铺/卖家 ID，全部按字符串存 |
| `url` | TEXT | 规范化后的完整链接 |
| `title` | TEXT | 用户可改的显示名 |
| `enabled` | INTEGER | 0 暂停，1 启用 |
| `active_since` | TEXT | UTC ISO 时间，早于此时间不排任务（防止新建目标回补历史） |
| `deleted_at` | TEXT | 软删除，非空即视为已删 |
| `cover_url` / `official_title` / `shop_name` / `category` | TEXT | 采集回来或手填的展示字段 |
| `reviewed_sampling_slot` | TEXT | 人工确认过的采样槽，用于「复核采样」提示 |

唯一约束 `(platform, kind, external_id)`：同一商品重复导入会命中已存在记录。

### 3.2 snapshots — 采样快照（核心表）

| 字段 | 说明 |
|---|---|
| `slot` | 整点槽，UTC ISO，如 `2026-11-23T12:00:00+00:00` |
| `observed_at` | 实际采集时刻 |
| `source` | `scheduled`（定时）/ `manual`（手工录入）/ `visible_text`（粘贴文字） |
| `raw_sales` / `exact_sales` | 展示销量原文 / 解析出的精确值 |
| `price_fen` | 价格（分），避免浮点误差 |
| `listing_state` | `active` / `sold` / `delisted` |
| `want_count` | 闲鱼想要数 |
| `hourly_delta` / `daily_delta` | 计算出的增量，可为 NULL |
| `quality` | `exact` / `counter_reset` / 其他降级标记 |
| `daily_baseline_at` | 今日增量所用基准的采集时刻 |
| `price_label` | `券后价` / `到手价` / `活动价` |

唯一约束 `(target_id, slot)`：一个目标一个整点只有一条定时快照。手工读数 `slot` 为 NULL，不受约束。

### 3.3 其余表

| 表 | 用途 | 关键约束 |
|---|---|---|
| `jobs` | 每个目标每个整点一条任务 | `UNIQUE(target_id, slot)`；`state` ∈ queued/success/failed/missed/unavailable |
| `shop_catalogs` | 店铺快照的在售清单 | `snapshot_id` 主键，`items` 为 JSON 数组，`has_more` 标记采集缺口 |
| `import_readings` | 导入链接时的即时读数（不写历史） | `target_id` 主键 |
| `events` | 命中的异动 | `snapshot_id` 唯一，`matched` 为规则名 JSON 数组，`notify` 是否推送 |
| `deliveries` | 通知投递队列 | `UNIQUE(channel, group_key)` 天然幂等 |
| `rules` | 全局规则，单行 | `CHECK(id=1)`，`settings` 为 JSON |

---

## 4. 指标与增量算法

这是最容易改错的部分，`storage.compute_deltas` 有完整注释，改之前先读。

### 4.1 指标选择

```python
def metric_column(target):
    if target["kind"] == "shop":
        return "active_count"        # 店铺统一看在售数
    return "exact_sales" if target["platform"] == "xiaohongshu" else "want_count"
```

- 小红书商品 → 精确展示销量（`已售 1050` → 1050）
- 闲鱼商品 → 想要数（**不是销量**，两者语义不同，不能相加）
- 店铺 → 在售数（来自 `shop_catalogs.active_count`）

### 4.2 小时增量

1. 只比较 `source='scheduled'` 且有值的记录
2. 优先取严格上一整点；**缺采时向前回溯最多 6 小时**，取最近一条有效整点
3. 判断相邻用 **slot 差值**，不是采集时刻差值 —— 采样在 45 分钟窗口内随时发生，补采的采集时刻会偏移，但整点仍相邻
4. 回溯得到的差值只用于展示（前端提示「与 N 小时前相比」），**不参与规则判断**
5. `exact_sales` 若比上次更低，视为计数回退，不给小时增量（除非 `quality='counter_reset'` 已标记）

### 4.3 今日增量

1. 基准 = 当天（北京时间零点）第一条有效整点读数；零点重新起算
2. 当天还没有任何读数时，当前这条即基准，`daily_delta = 0`（不是 NULL）
3. 遇到 `counter_reset` 标记的快照，**基准改为该回退点**，从回退点重新累计 —— 否则回退后的读数永远小于旧基准，当天剩余整点会全部为空
4. `exact_sales` 当前值低于基准但没被标记回退时，同样按「重新起算」处理，`daily_delta = 0`

### 4.4 前端展示约定

- 定时快照无增量（NULL）→ 显示 `0`（用户明确要求：整点没有增量就填 0）
- 时间轴按真实时间比例铺开，整点画网格；标签间距不足时自动稀疏为 1/2/4 小时间隔，跨零点显示日期
- 数据不足 2 个点时不给假曲线

---

## 5. 调度机制

`worker.cycle()` 每 8 秒被调用一次，逻辑：

```
1. 把 slot <= now-45min 且仍 queued 的任务标为 missed
2. 若 now - 当前整点 < 45 分钟：
     为每个 enabled / 未删除 / active_since<=slot 的目标
     INSERT OR IGNORE 一条 job（平台不支持则 state=unavailable）
     若之后数据源恢复可用，把 unavailable 重新置为 queued
3. 取 next_at <= now 的 queued 任务（最多 24 条），线程池 2 并发执行
4. 汇总上一整点的异动并生成投递队列
5. 发送待发通知（每次最多 8 条）
```

**45 分钟窗口**决定了两件事：整点后 45 分钟内必须开始采集；`next_at` 一旦超出窗口，任务直接判失败，不再重试。重试退避为 `25 * 2^attempts` 秒，最多 3 次；「访问受限」类错误固定 180 秒后重试。

**并发度 2** 是保守值。要提到 4 或更高，需要先确认目标平台不会因并发触发风控，并同步检查 Playwright 实例内存占用（每个浏览器上下文约 100–200 MB）。

**平台硬编码**：`cycle()` 里的 `for platform in ("xiaohongshu", "xianyu")` 是写死的，新增平台必须同时改这里，否则永远不会生成任务。

---

## 6. API 契约

全部挂在 `/api/v1` 下，云模式受 Basic Auth 保护（`/healthz` 除外）。写操作额外校验 `Origin` 必须同源。

### 6.1 只读

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 容器健康检查，无鉴权，返回 `{"status":"ok"}` |
| GET | `/api/v1/health` | 运行时状态：`sources`（各平台是否有数据源）、`source_kinds`、`channels`（已配置的通知渠道） |
| GET | `/api/v1/targets` | 目标列表，支持 `q` 搜索、`platform`、`kind`、`enabled`、分页 |
| GET | `/api/v1/targets/{id}/series` | 整点表 + 图表数据 + 最新读数 + 观察时刻 |
| GET | `/api/v1/targets/{id}/products` | 店铺商品清单与上期对比（新增/移出） |
| GET | `/api/v1/targets/{id}/series.csv` | 历史 CSV，带采样来源 |
| GET | `/api/v1/targets/{id}/catalog.csv` | 在售清单 CSV |
| GET | `/api/v1/events` | 异动记录 |
| GET | `/api/v1/rules` | 提醒规则 |
| GET | `/api/v1/ops/runs` | 最近任务运行情况 |
| GET | `/api/v1/admin/export` | 全量备份 JSON（八张表） |
| GET | `/api/v1/admin/probe?id=<itemId>` | 采集通道诊断：页面 URL、标题、cookie 名、两条 appKey 的接口返回 |

### 6.2 写入

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/targets/batch` | 批量导入链接，1–200 条，自动解析平台与类型 |
| PATCH | `/api/v1/targets/{id}` | 改 `enabled` / `title` / `category` |
| DELETE | `/api/v1/targets/{id}` | 软删除（保留历史） |
| POST | `/api/v1/targets/{id}/readings` | 手工录读数（销量/价格/状态/店铺清单） |
| POST | `/api/v1/targets/{id}/readings/visible-text` | 粘贴页面文字，正则提取价格与想要数 |
| POST | `/api/v1/targets/{id}/refresh` | 立即采样一次（不占整点槽） |
| POST | `/api/v1/targets/{id}/review-sampling` | 标记某个采样槽已人工复核 |
| PUT | `/api/v1/rules` | 更新规则（整行覆盖） |
| POST | `/api/v1/admin/restore` | 恢复备份。`mode=empty` 仅空库；`replace` 需 `confirm:"replace"`，整体事务，失败全回滚 |

### 6.3 关键返回结构

`GET /targets/{id}/series`：

```json
{
  "observation": {"want_count": 83, "price_fen": 200, "listing_state": "active"},
  "hours": [{"slot": "...", "state": "success", "want_count": 83,
             "hourly_delta": 2, "daily_delta": 15, "error": null}],
  "chart": [{"at": "...", "value": 83, "source": "scheduled"}],
  "latest": {...}
}
```

`hours` 是整点表的数据源，`chart` 是图表的数据源，两者都按当前目标的指标列取值。

---

## 7. 采集链路

### 7.1 小红书商品（`xhs_product.py`）

1. `read_json_product()` 先用公开详情 JSON 接口
2. 异常或字段缺失时 `read_browser_product()`：Playwright 渲染公开详情页，正则提取 `¥价格` 与 `已售 N`
3. Windows 默认用已安装的 Edge（`MONITOR_BROWSER_CHANNEL` 可改），镜像内用自带 Chromium

### 7.2 小红书店铺（`xhs_public.py`）

先 GET 公开店铺页，从 `window.__SETUP_SERVER_STATE__` 读取在售数和首批卡片。清单不全时，请求商城公开组件接口的 `shop_pc` 场景，从第 0 页开始，每页 10 件，核对所属店铺并按商品 ID 去重。末页仍可能返回 `hasMore=true`，网页在售数、分页接口总数和去重条数一致时即可确认收齐。如果分页中断，保留采集到的清单并标记缺口。

### 7.3 闲鱼店铺（`xianyu_shop.py`）— 云端可用

打开 `https://www.goofish.com/personal?userId=<uid>`，在浏览器上下文里读两个接口：

| 接口 | appKey | 取什么 |
|---|---|---|
| `mtop.idle.web.user.page.head` | `34839810` | `module.tabs.item.number` 在售数、`module.base.displayName` 店名、`baseInfo.kcUserId` 校验 |
| `mtop.idle.web.xyh.item.list` | `34839810` | `cardList[].cardData`：`id` / `title` / `priceInfo.price`，`nextPage` 标记还有下一页 |

`PAGE_SIZE = 20`。沿用卖家主页的商品分组和上一页返回的分页参数读取后续页，按商品 ID 去重。确认收齐需要到达末页，且商品数与卖家主页的在售数一致。

### 7.4 闲鱼商品（`xianyu_product.py`）— 机房 IP 被风控

mtop 签名规则：

```
sign = md5(token + "&" + t + "&" + appKey + "&" + body)
token 来自 cookie `_m_h5_tk` 的下划线前半段
首次请求不带 token → 返回 TOKEN_EMPTY 并下发 cookie → 再请求一次即可
```

流程：

1. 直连 `h5api.m.goofish.com/h5/mtop.taobao.idle.awesome.detail/1.0/`
2. 被风控则切浏览器：真 Chromium 打开 `h5.m.goofish.com/item?id=<id>`，监听页面自己发出的详情响应
3. 页面没发请求时，用页面算好的 `isg/tfstk` 指纹在浏览器上下文里补发，**先试 `34839810`（PC），再试 `12574478`（H5）**
4. 都失败则抛出带原因的错误，写进 `jobs.error`

实测结论（2026-11-23）：家宽两条通道都通，机房 IP 两条 appKey 全部 `RGV587_ERROR`，`_m_h5_tk` 都发不下来。**这不是代码问题**，出口 IP 信誉问题。解决方案是配置 `MONITOR_XIANYU_PROXY`。

### 7.5 自定义数据源（外部 endpoint）

设置 `MONITOR_XIANYU_ENDPOINT` / `MONITOR_XIAOHONGSHU_ENDPOINT` 后，该平台所有目标改为请求这个 endpoint（GET，查询参数 `platform/kind/external_id/url`，可选 Bearer token），返回结构与 `Reading` 一致。响应上限 64 KB，返回的 `external_id` 必须与请求一致。

### 7.6 浏览器统一配置（`xy_browser.py`）

- UA 固定为与镜像内 Chromium 同版本（131），避免 UA/特征不一致被识别
- `add_init_script` 去掉 `navigator.webdriver`
- `--disable-blink-features=AutomationControlled`
- 代理从 `MONITOR_XIANYU_PROXY` 读取，格式 `http://user:pass@host:port`

---

## 8. 异动规则与通知

规则存在 `rules.settings`（单行 JSON），字段与默认值：

```json
{"hourly_units": 5, "premium_price_fen": 2900, "premium_units": 2,
 "price_drop_fen": 0, "state_alerts": false}
```

- `hourly_units`：近一小时增加 ≥ N 触发（小红书）
- `premium_price_fen` + `premium_units`：价格 ≥ 29 元且近一小时增加 ≥ 2 触发
- `price_drop_fen`：闲鱼降价幅度阈值
- `state_alerts`：商品状态变更（售出/下架）是否提醒

**规则只认严格相邻整点的增量**（`strict_hourly`），回溯得到的累计值不参与，避免把跨几小时的涨幅误报成「近一小时」。

通知走 `worker.queue_deliveries()`：等该整点所有 job 收口（没有 queued）后，按 1800 字节分片，写 `deliveries` 队列；`send_pending()` 逐条发送，钉钉支持 HMAC-SHA256 加签。渠道地址只允许 `https://oapi.dingtalk.com` 与 `https://qyapi.weixin.qq.com`，防 SSRF。

发送失败标 `unknown` 而不是 `failed` —— 因为 HTTP 超时不代表对方没收到，宁可显示「待核查」也不重复轰炸。

---

## 9. 鉴权与安全

| 项 | 实现 |
|---|---|
| 访问控制 | 云模式 HTTP Basic，`secrets.compare_digest` 定长比较；`/healthz` 豁免 |
| 账号来源 | `MONITOR_ACCESS_USERNAME` / `MONITOR_ACCESS_PASSWORD`，启动时校验格式（密码 ≥ 8 位可打印 ASCII），不合规直接拒绝启动 |
| CSRF | 写操作校验 `Origin` 必须等于当前 host，否则 403 |
| 本地模式 | `TrustedHostMiddleware` 只允许 `127.0.0.1` / `localhost` |
| SSRF | 自定义数据源与通知地址都做 scheme + hostname 白名单 |
| 图片 | 只接受 `*.alicdn.com` 的封面，前端 `img` 不直出第三方任意域名 |
| 链接 | 只允许白名单域名，短链最多跳 3 次 |
| 备份 | 64 MB 上限，恢复整体事务 + 外键校验，失败回滚 |

已知缺口（交接时需知悉）：

1. **Basic Auth 是单账号共享**，没有多用户、没有审计日志。密码泄露 = 全量数据泄露。
2. **`/api/v1/admin/*` 与普通接口同一套鉴权**，任何持账号者都能导出/覆盖全库。若要多角色，需要先拆分鉴权。
3. **无请求频率限制**，`/refresh` 可被反复调用触发真实采集，可能加速触发平台风控。

---

## 10. 前端结构

`web/index.html` 五个视图 + 两个独立页，靠 `location.hash` 路由：

| hash | 视图 |
|---|---|
| `#pool`（默认） | 监控池列表 + 导入 |
| `#events` | 异动记录 |
| `#rules` | 提醒规则表单 |
| `#runs` | 运行状态（任务、备份下载） |
| `#shop/<id>` | 店铺商品页 |
| 详情面板 | 点列表项展开，含整点表、图表、读数明细、CSV |

无框架、无打包。改动后必须同步升 `index.html` 里的 `app.js?v=N`，否则浏览器缓存会拿到旧脚本。

图表在 `drawChart()`：按真实时间比例计算 x 坐标，整点画网格线，标签按可用宽度自动稀疏（1/2/4 小时），跨零点显示日期。

---

## 11. 测试

```powershell
& ./.venv/Scripts/python.exe -m unittest discover -s tests -q
```

测试集中在 `tests/test_monitor.py`，覆盖：

- 链接解析（各平台分享链接、短链、非法域名）
- 读数校验（模糊销量不能指定精确值、多个价格要报错）
- 增量计算（跨零点、缺采回溯、计数回退、当天首条）
- 调度（窗口内/外、重试上限、unavailable 恢复）
- 备份恢复（字段校验、外键、replace 确认）

没有端到端测试，前端靠手工冒烟。**改采集通道时建议先在本地跑一次真实链接**，再动测试。

---

## 12. 扩展指引

### 加一个平台

1. `platforms.py`：`LONG_HOSTS` 加域名，`parse_target` 加解析分支
2. 新建 `monitor/<platform>_<kind>.py`，实现 `supported()` 与 `read_*()`，返回 `Reading` 可接受的结构
3. `platforms.target_configured()` 和 `observe()` 各加一行分发
4. **`worker.cycle()` 的平台循环里加上新平台**（容易漏）
5. `storage.metric_column()` 决定用哪个指标列
6. 前端 `web/app.js` 的 `metricName()` 加显示名

### 加一个指标

1. `models.Reading` 加字段并纳入 `check_metrics` 的「至少一个指标」判断
2. `storage` 的 `snapshots` 加列 + `init()` 里加迁移
3. `dump_database` / `restore_database` 的字段白名单加列（漏了会导致恢复丢数据）
4. `compute_deltas` 的 `metric_column` 决定是否参与增量
5. 前端表格与图表列

### 改表结构

必须同时改四处，否则线上恢复会失败：`init()` 建表、`ALTER TABLE` 迁移、`dump_database()` 导出、`restore_database()` 字段校验。

---

## 13. 技术债

按影响排序：

1. **无持久卷** → 每次发布清库（运维层面解决，见 OPERATIONS）
2. **店铺分页依赖平台网页接口** → 访问受限或返回不一致时只能保留已采清单
3. **`cycle()` 平台硬编码** → 扩展性差
4. **单账号 Basic Auth** → 无法区分只读/管理权限
5. **无请求限流** → `/refresh` 可被滥用
6. **前端无构建、无测试** → 1.1k 行单文件，改动靠手工验证
7. **采集模块无单测** → 解析函数是纯函数，其实很容易补测
