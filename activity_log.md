## 2026-10-03 17:00:00 +0800 — 完善会话详情、新建会话与容量错误看门狗

- activity_id: ACT-20261003-170000
- modification_version: V0.2.0
- type: code_change
- change_level: L1
- approval: user-approved
- approval_basis: 用户明确要求详情分页、命令折叠、底部对话、新建会话和看门狗。
- skills_used: research-change-control, openai-docs
- branch: 未使用 Git 仓库
- base_commit: 不适用
- worktree_dirty: true
- scope: `server.py`, `web/`, `tests/`, `README.md`

**文件**

- `server.py` — 增加 transcript 游标分页、新线程 `thread/start`、看门狗接口和最短 60 秒容量错误重试。
- `web/session.*` — 默认定位最新记录、顶部加载历史、命令折叠、底部直接对话。
- `web/new.*` — 新建会话页面。
- `web/index.html`, `web/app.js`, `web/styles.css` — 新建入口和看门狗开关。
- `tests/` — 增加分页、容量错误分类和新线程协议测试。

**原因**

减少大型 `thread_history_1.sqlite` 的首屏读取，保持详情页接近桌面版的阅读顺序，并让临时模型容量错误能够受控恢复。

**验证**

- `python3 -m unittest discover -s tests -v`：9 tests passed。
- `python3 -m py_compile server.py`：passed。
- `node --check web/app.js web/session.js web/new.js`：passed。
- 临时端口端到端检查：`/api/health`、`/api/watchdog`、`/new.html` 返回 200；看门狗开关可切换。

## 2026-10-03 19:20:00 +0800 — 收紧看门狗范围并补充 Windows 兼容性

- activity_id: ACT-20261003-192000
- modification_version: V0.2.1
- type: code_change
- change_level: L1
- approval: user-approved
- approval_basis: 用户发现旧会话被看门狗误触发，明确要求继续修复。
- skills_used: research-change-control, openai-docs
- branch: 未使用 Git 仓库
- base_commit: 不适用
- worktree_dirty: true
- scope: `server.py`, `tests/`, `README.md`

**文件**

- `server.py` — 看门狗只接受监控启动后、最近 15 分钟内的容量失败；Windows app-server stdout 改用队列线程读取，支持 `codex.exe` 和 Windows SQLite URI。
- `tests/test_monitor.py` — 增加历史失败不得触发的回归测试。
- `README.md` — 增加 Windows 与跨设备运行说明。

**原因**

上一版只按最新错误类型筛选，会把旧历史会话当成新故障。已停止本次误启动的监控子进程，当前运行服务中的看门狗保持关闭；新代码需重启服务后生效。

**验证**

- `python3 -m unittest discover -s tests -v`：10 tests passed。
- `python3 -m py_compile server.py`：passed。
- `node --check web/session.js web/app.js web/new.js`：passed。

## 2026-10-03 23:20:59 +0800 — 为 Goal 看门狗接入原生恢复协议

- activity_id: ACT-20261003-232059
- modification_version: V0.2.2
- type: code_change
- change_level: L1
- approval: user-approved
- approval_basis: 用户要求确认容量错误时暂停 Goal 的特殊处理并继续实现。
- skills_used: research-change-control, openai-docs
- branch: main
- base_commit: e4c3d90
- worktree_dirty: true
- scope: `server.py`, `tests/test_app_server.py`, `README.md`

**文件**

- `server.py` — 看门狗对暂停/进行中 Goal 使用 `thread/goal/get` 与 `thread/goal/set`，并在发送前重新读取会话状态；app-server 客户端支持 daemon proxy 与 Windows 队列式 stdout 读取。
- `tests/test_app_server.py` — 增加持久化 Goal 恢复 JSON-RPC 回归测试。
- `README.md` — 说明 Goal 恢复、预算检查和同一失败 turn 单次尝试规则。

**原因**

此前的 `/goal resume` 是普通文本 turn，不能保证恢复持久化 Goal。现在改为原生生命周期调用，省略 objective 和 token budget 参数以保留原目标与用量统计；完成通知即结束本次恢复，后续新失败 turn 再由看门狗处理。

**验证**

- `python3 -m unittest discover -s tests -v`：11 tests passed。
- `python3 -m py_compile server.py`：passed。
- `git diff --check`：passed。
