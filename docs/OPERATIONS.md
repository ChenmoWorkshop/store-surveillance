# 运维手册

覆盖本地运行、云端发布、备份恢复、故障排查。**发布前请先读第 3 节的「清库」警告**，这是本项目最容易出事的地方。

---

## 1. 环境变量

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `MONITOR_CLOUD` | 云端必填 `1` | 空 | 开启 Basic Auth、关闭 TrustedHost、SQLite 用 DELETE 日志 |
| `MONITOR_ACCESS_USERNAME` | 云端必填 | `monitor` | 不含冒号的可打印 ASCII |
| `MONITOR_ACCESS_PASSWORD` | 云端必填 | 空 | ≥ 8 位可打印 ASCII，不合规拒绝启动 |
| `MONITOR_DB` | 建议 | `data/monitor.db` | 数据库路径。镜像内默认 `/var/lib/monitor/monitor.db` |
| `MONITOR_XIANYU_PROXY` | 可选 | 空 | 闲鱼出口代理，`http://user:pass@host:port` |
| `MONITOR_BROWSER_CHANNEL` | 可选 | win 下 `msedge`，容器内 `chromium` | Playwright 浏览器通道 |
| `MONITOR_XIANYU_ENDPOINT` | 可选 | 空 | 自定义闲鱼数据源，配置后覆盖内置通道 |
| `MONITOR_XIAOHONGSHU_ENDPOINT` | 可选 | 空 | 同上，小红书 |
| `MONITOR_XIANYU_TOKEN` / `MONITOR_XIAOHONGSHU_TOKEN` | 可选 | 空 | 自定义数据源的 Bearer token |
| `MONITOR_DINGTALK_WEBHOOK` / `MONITOR_DINGTALK_SECRET` | 可选 | 空 | 钉钉群机器人 |
| `MONITOR_WECOM_WEBHOOK` | 可选 | 空 | 企业微信群机器人 |

配置后 `/api/v1/health` 的 `sources` 与 `channels` 会反映实际生效状态，可用来确认变量是否被容器读到。

---

## 2. 本地运行

```powershell
python -m venv .venv
& ./.venv/Scripts/python.exe -m pip install -r requirements.txt
& ./.venv/Scripts/python.exe -m playwright install chromium   # 只有需要浏览器通道时才装
./start.ps1
```

- 访问 http://127.0.0.1:8765 ，接口文档 `/docs`
- 本地模式不需要密码，但只允许 `127.0.0.1` / `localhost`
- 数据库在 `data/monitor.db`，删掉即重置
- 本地跑闲鱼采集走的是家用宽带，通常不会被风控，适合调试通道

跑测试：

```powershell
& ./.venv/Scripts/python.exe -m unittest discover -s tests -q
```

---

## 3. 云端发布

### 3.1 先理解这件事：发布 = 清库

当前服务**没有挂载持久化卷**，`/var/lib/monitor/monitor.db` 在容器文件系统里。每次发布都会滚动到新实例，新实例从空库开始，**旧数据全部丢失**。

因此标准发布流程是四步，一步都不能省：

```
① 导出备份  →  ② 发布  →  ③ 核对目标数量  →  ④ 数量不对就恢复
```

### 3.2 发布前：导出备份

```powershell
$env:MONITOR_BASE_URL = 'https://<你的服务域名>'
$env:MONITOR_ACCESS_USERNAME = '<账号>'
$env:MONITOR_ACCESS_PASSWORD = '<密码>'
& ./.venv/Scripts/python.exe tools/backup_cloud.py
```

备份落在 `backups/cloud-<时间戳>.json`，脚本保留最近 30 份。也可以登录站点，在「运行状态 → 数据备份」直接下载。

### 3.3 发布

**方式 A：CloudBase MCP（推荐）**

把源码复制到一个精简目录（只含 `Dockerfile` / `requirements.txt` / `monitor/` / `web/`），再提交部署。精简可以避免把 `data/monitor.db`、`backups/`、`.venv/` 一起打进去（曾因此传了上百 MB）：

```powershell
rm -rf /f/tmp/surv-build; mkdir -p /f/tmp/surv-build
cp Dockerfile requirements.txt /f/tmp/surv-build/
cp -r monitor web /f/tmp/surv-build/
find /f/tmp/surv-build -name "__pycache__" -type d -prune -exec rm -rf {} +
```

然后调用 MCP 的 `manageCloudRun`：`action=deploy`、`serverName=store-surveillance`、`targetPath=F:\tmp\surv-build`。它会保留已有的环境变量。

**方式 B：tcb CLI**

```powershell
printf '\n' | tcb cloudrun deploy --env-id <envId> --service-name store-surveillance --source . --force --wait
```

`printf '\n'` 不能省：CLI 会问「Enable gray deployment?」，`--force` **不会**跳过这个交互，后台跑就会一直挂住。

### 3.4 等待与判断进度

- 构建约 8–9 分钟（含 Playwright + Chromium 安装）
- `getDeployRecords` 的 `Status` 字段会滞后，看 `getManageTask` 的 `Steps` 才准
- 构建期间站点仍由旧版本服务，不要在这期间写数据

### 3.5 发布后：核对与恢复

```powershell
# 1. 看健康状态
curl -s -u <账号>:<密码> "https://<域名>/api/v1/health"

# 2. 看目标数量，和备份里的数字对比
curl -s -u <账号>:<密码> "https://<域名>/api/v1/targets?limit=10"

# 3. 数量为 0 或明显变少 → 恢复
$env:MONITOR_BASE_URL = 'https://<域名>'
$env:MONITOR_ACCESS_USERNAME = '<账号>'
$env:MONITOR_ACCESS_PASSWORD = '<密码>'
& ./.venv/Scripts/python.exe tools/restore_cloud.py backups/cloud-<时间戳>.json
```

`restore_cloud.py` 默认走 `mode=empty`，只在空库时写入；覆盖线上现有数据必须显式 `--replace --yes`。恢复会校验字段、外键、行数，任一失败整批回滚，并自动重算所有定时快照的增量。

### 3.6 彻底解决清库：挂载 CFS

在 CloudBase 控制台为该服务挂载文件存储到 `/var/lib/monitor`，然后在环境变量里确认 `MONITOR_DB=/var/lib/monitor/monitor.db`（镜像已默认）。挂载后：

- 首次仍需从备份恢复一次
- 之后发布不再清库，第 3.1 节的四步流程可以简化为「发布 → 验证健康」
- 网络文件系统上必须保持 `journal_mode=DELETE`（`MONITOR_CLOUD=1` 已强制）

---

## 4. 备份与恢复

### 备份内容

八张表全量：`targets`、`import_readings`、`jobs`、`snapshots`、`shop_catalogs`、`events`、`deliveries`、`rules`。格式带 `version` 与 `counts`，可直接人读。

### 恢复语义

| 模式 | 行为 | 使用场景 |
|---|---|---|
| 默认（`empty`） | 仅当目标表为空时写入，否则报错 | 新部署、发布后清库 |
| `replace` | 清空目标表后整体写入，需 `confirm:"replace"` | 主动回滚到某个时间点 |

恢复完成后会调用 `recompute_deltas()`，用当前算法重算所有整点的 `hourly_delta` / `daily_delta`。所以**用旧备份恢复也能得到符合新口径的增量**，这是修统计 bug 后补数据的常用手段。

### 建议节奏

- 每次发布前必做
- 大改采集逻辑前做一次
- 备份文件本地保留，不要提交到仓库（`.gitignore` 已排除 `backups/`）

---

## 5. 故障排查

| 现象 | 排查顺序 |
|---|---|
| 整点没采到，任务 `failed` | 看 `jobs.error` → `/api/v1/ops/runs` → 若是闲鱼商品且含 `RGV587`，是 IP 风控，配代理 |
| 任务一直 `unavailable` | 该平台/类型没有数据源。查 `/api/v1/health` 的 `source_kinds` |
| 整点表某几格是空的但图表有值 | 空的是 `hourly_delta` 为 NULL 且该目标当小时无快照；前端会把定时快照的空增量显示为 0，若有整行缺失说明任务没跑 |
| 今日增量整天是 0 | 检查是否有 `quality='counter_reset'` 快照；或当天首条读数就是最高值 |
| 图表时间轴标签重叠 | `drawChart()` 的稀疏逻辑按容器宽度计算，容器过窄时会退化；不是 bug |
| 页面 401 | 云模式 Basic Auth。浏览器会缓存凭证，换账号需关标签页重开 |
| 发布后全是 0 条目标 | 清库了，按 3.5 恢复 |
| `tcb` CLI 卡住不动 | 本机 DNS 偶发对 `tcb.tencentcloudapi.com` 返回畸形包；`Clear-DnsClientCache` 后重试，或改用 MCP |
| 闲鱼店铺能采、闲鱼商品不能 | 正常现象，见 TECHNICAL 7.4，机房 IP 风控 |
| 通知没收到 | `/api/v1/health` 看 `channels` 是否为空；`deliveries` 表看 `state` 是 `queued` / `unknown` / `sent` |

### 诊断接口

```bash
curl -u <账号>:<密码> "https://<域名>/api/v1/admin/probe?id=<商品ID>"
```

返回：最终 URL、页面标题、body 文本片段、cookie 名列表、页面自己发出的 mtop 调用、两条 appKey 的直接调用结果。**这是排查闲鱼风控最快的手段**，不需要登录服务器。

### 日志

- 容器 stdout：CloudBase 控制台「日志」页，或 MCP `getProcessLog`
- 只看异常：`logger.exception("调度执行失败")` 会把整轮 cycle 的堆栈打出来
- 采集失败不会打日志，只写 `jobs.error`，去数据库/接口看

---

## 6. 容量与费用

- 单目标每天 24 条快照，10 个目标一年约 8.8 万行，SQLite 完全够用
- 并发 2，单次采集 3–15 秒（浏览器通道），24 个目标一轮约 2–3 分钟
- 云托管最小/最大实例都设 1，避免多实例重复采样

---

## 7. 上线检查清单

发布前：

- [ ] 已导出备份，文件存在且大小合理
- [ ] 本地测试全绿（`unittest discover`）
- [ ] 目标平台的真实链接本地跑通一次
- [ ] 环境变量未丢失（对比 `/api/v1/health`）
- [ ] 前端改动已升 `app.js?v=N`

发布后：

- [ ] `/healthz` 返回 ok
- [ ] `/api/v1/health` 的 `sources` 符合预期
- [ ] 目标数量与备份一致
- [ ] 手动触发一次 `/refresh` 能成功
- [ ] 下一个整点自动采样正常
- [ ] 页面能打开，图表正常
