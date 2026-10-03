# Codex Home Monitor

一个面向无头服务器的只读 Codex 会话监控面板。它直接读取一个或多个 `CODEX_HOME` 的 SQLite 状态库，在浏览器中展示会话更新时间、Home、Goal 状态和最近 turn 状态。

## 运行

```bash
python3 server.py --port 8787
```

默认会尝试读取 `$HOME/.codex`，以及 `$HOME/oyx_ws/.codex*` 下看起来像 Codex Home 的目录。生产或多用户环境建议显式指定：

```bash
CODEX_HOMES=/home2/wyy/.codex:/home2/wyy/oyx_ws/.codex_oyx:/home2/wyy/oyx_ws/.codex_oyx_NewAPI \
  python3 server.py --port 8787
```

服务器默认只监听 `127.0.0.1`。从本地机器访问无头服务器：

```bash
ssh -L 8787:127.0.0.1:8787 user@server
```

然后打开 <http://127.0.0.1:8787>。

## Windows 与跨设备访问

监控服务使用 Python 标准库、SQLite 只读连接和浏览器原生 API，Windows 10/11 安装 Python 3.9+ 后可以运行。Windows 上建议显式指定 Home 路径，例如 PowerShell：

```powershell
$env:CODEX_HOMES = "$HOME\.codex;$HOME\oyx_ws\.codex_work"
python .\server.py --host 127.0.0.1 --port 8787
```

直接对话和新建会话还需要对应 Home 中存在 Windows 版 Codex app-server（`codex.exe`）。如果只做会话查看，SQLite 数据库即可，不需要 app-server。

跨设备查看时，服务端需要监听局域网地址：

```bash
python3 server.py --host 0.0.0.0 --port 8787
```

然后从同一局域网设备访问 `http://服务器局域网IP:8787`。当前版本没有登录认证，建议只在可信局域网使用，或继续通过 SSH 隧道访问；不要把端口直接暴露到公网。跨设备查看的 Home 数据仍然是运行服务的那台机器上的数据。

## 环境变量

| 变量 | 用途 |
| --- | --- |
| `CODEX_HOMES` | 用 `:`（Windows 用 `;`）分隔的 Home 路径列表 |
| `CODEX_HOME_ROOTS` | 自动发现 Home 时扫描的根目录列表 |
| `CODEX_MONITOR_REFRESH_SECONDS` | 前端默认刷新间隔，默认 5 秒 |
| `CODEX_MONITOR_WATCHDOG=1` | 服务启动时默认开启容量错误看门狗；默认关闭 |
| `CODEX_MONITOR_WATCHDOG_INTERVAL_SECONDS` | 看门狗间隔，代码会强制不低于 60 秒 |
| `CODEX_MONITOR_WATCHDOG_MAX_AGE_SECONDS` | 允许自动恢复的最近失败时间窗口，默认 900 秒；同时只接受监控进程启动后产生的失败 |

监控部分只以 SQLite read-only 模式读取 `state_5.sqlite`、`goals_1.sqlite` 和 `thread_history_1.sqlite`，不会读取 `auth.json`。直接对话、新建会话和启用看门狗会通过对应 Home 的 app-server 发送 turn，因此会产生 Codex 会话写入。

## 当前 API

- `GET /api/health`
- `GET /api/homes`
- `GET /api/sessions?home_id=&goal_status=&q=&limit=`
- `GET /api/session?home_id=&thread_id=&content_limit=&before_created_at_ms=`：返回独立详情页使用的解析后 transcript；默认读取最新 200 个 item，使用时间游标读取更早记录；默认隐藏 reasoning/contextCompaction，命令输出和 diff 会做长度限制及明显凭证脱敏
- `POST /api/chat`：请求体为 `{"home_id":"...","thread_id":"...","message":"..."}`，返回 SSE
- `POST /api/chat/new`：请求体为 `{"home_id":"...","message":"...","model":"","cwd":""}`，创建新线程并流式发送第一条消息
- `GET/POST /api/watchdog`：读取或切换看门狗。看门狗只重试明确的临时模型容量错误，最短间隔 60 秒

点击首页会话会进入 `/session.html` 独立详情页，正文来自 `thread_items.item_json`，会分别显示用户消息、Codex 回复、命令执行、文件变更、网页搜索和计划。

详情页现在提供 direct chat MVP：`POST /api/chat` 会以 SSE 返回状态和回复 delta。后端通过对应 Home 的 app-server 恢复线程并调用 `turn/start`，不会直接写 SQLite。命令或文件审批请求在审批 UI 完成前会自动拒绝，并通过 SSE 告知页面。新建对话使用 `thread/start`，完成后自动进入详情页。

详情页默认只读取最新 200 个 item 并定位到末尾；在 transcript 顶部继续滚动会通过 `before_created_at_ms` 分页读取更早内容。命令执行默认折叠，直接对话区放在 transcript 下方。

看门狗默认关闭，可在首页点击“看门狗：关闭”启用，也可以设置 `CODEX_MONITOR_WATCHDOG=1` 默认启用。它只处理监控进程启动后、最近 15 分钟内产生的 `serverOverloaded`、`Selected model is at capacity` 等临时模型容量错误，等待至少 60 秒后发送一次“继续”；如果会话有进行中或暂停的 Goal，则发送 `/goal resume`。账户用量、配额、上下文长度和速率限制错误不会自动重试，进程启动前遗留的历史容量错误也不会自动触发。

## app-server 可行性检查

当前服务器实际已经存在 managed app-server：

- `.codex`、`.codex_oyx`、`.codex_oyx_frj`：0.160.0
- `.codex_oyx_NewAPI`：0.159.3
- `.codex_oyx_API`：没有 managed app-server 二进制

每个已运行的 Home 都有独立的 `app-server-control.sock`。独立 stdio app-server 的 JSON-RPC 流程已验证：`initialize` → `initialized` → `thread/resume` 可以正常工作。当前实现为每次发送启动一个独立 app-server 进程，完成后退出；如果目标线程已经被其他 writer 占用，页面会收到明确错误，需要先在原 Codex 客户端中结束当前 turn。

## 验证

```bash
python3 -m unittest discover -s tests -v
```
