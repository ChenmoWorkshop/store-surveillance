# Windows 常驻部署

这套脚本把监控台跑在一台常开的 Windows 机器上，对外用 Cloudflare 快速隧道（免费）
提供一个 HTTPS 地址。数据落在本地 SQLite 文件，重启不丢。

适合：家里有台不关机的电脑 / 小主机，想零成本长期采集。

> **不满足 7×24 的场景**：关机、休眠、合盖睡眠都会中断采集，中断期间的快照无法补采。
> 真正无人值守请部署到云服务器（见仓库根目录的 `docs/OPERATIONS.md`）。

## 一、准备

```bat
cd <仓库根目录>
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m playwright install chromium
```

下载 `cloudflared.exe` 放到 `<仓库根目录>\bin\`，或安装到 PATH。

## 二、设置访问密码

密码只从环境变量读取，**不要写进任何文件**。至少 8 位：

```bat
setx MONITOR_ACCESS_PASSWORD "换成你自己的强密码"
setx MONITOR_ACCESS_USERNAME "admin"
```

`setx` 写入用户环境变量，新开的终端才生效。当前会话可临时：

```bat
set MONITOR_ACCESS_PASSWORD=换成你自己的强密码
```

## 三、启动

| 方式 | 用法 | 适用 |
| --- | --- | --- |
| 一键启动 | 双击 `deploy\windows\start_all.bat` | 手动拉起，带窗口 |
| 静默启动 | `wscript deploy\windows\launch.vbs` | 无窗口 |
| 独立进程 | `python deploy\windows\spawn_daemon.py` | 从终端启动并脱离会话 |

开机自启：给 `launch.vbs` 建快捷方式，放进 `shell:startup`（Win+R 输入）。

两个 bat 都做了幂等检查：端口已监听、cloudflared 已在跑时会直接退出，重复双击不会起第二份采集。

## 四、地址与数据

- 公网地址写在 `<仓库根目录>\public-url.txt`，隧道每次重连都会变
- 数据库：`<仓库根目录>\data\monitor.db`
- 每次启动自动备份到 `data\backups\`，保留最近 14 份
- 日志：`logs\server.log`、`logs\tunnel.log`

要固定地址，需在 Cloudflare Zero Trust 建命名隧道并绑定自有域名
（`cloudflared tunnel create` / `route dns` / `run`），把 `tunnel_watch.py` 里的
`--url` 换成 `run <隧道名>` 即可。

## 五、环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `MONITOR_ACCESS_PASSWORD` | 无，必填 | 访问密码，≥ 8 位 |
| `MONITOR_ACCESS_USERNAME` | `monitor` | 访问用户名 |
| `PORT` | `8812` | 监听端口 |
| `MONITOR_DB` | `<仓库>/data/monitor.db` | 数据库路径 |
| `MONITOR_CLOUD` | `1` | 为 1 时启用访问控制中间件 |
| `GATEWAY_COOKIE` | 未设置 | 为 1 时启用 cookie 登录页，**仅用于会吞掉 `Authorization` 头的网关（如 WorkBuddy Sites）且必须走 HTTPS** |

## 六、安全须知

- 隧道地址是公开的，密码是第一道也是唯一一道防线，不要用弱密码
- 快速隧道与本地服务都是明文 HTTP，密码在隧道内被 TLS 保护，但本机到 cloudflared 之间是明文
- `sites_auth.py` 的 cookie 带 `secure=True`，在纯 HTTP 下浏览器不会保存，会导致登录后仍 401；只在 HTTPS 网关上开启
- 不要把 `data/`、`logs/`、`public-url.txt` 提交到仓库（`.gitignore` 已覆盖）
