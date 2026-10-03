#!/usr/bin/env python3
"""Read-only web monitor for multiple Codex Homes.

The server intentionally uses only the Python standard library so it can run on
a minimal headless host without installing a web framework.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import re
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, quote, unquote, urlparse


ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
DEFAULT_REFRESH_SECONDS = int(os.environ.get("CODEX_MONITOR_REFRESH_SECONDS", "5"))
WATCHDOG_MIN_INTERVAL = 60
WATCHDOG_DEFAULT_MAX_AGE_SECONDS = 15 * 60


def _split_paths(value: str) -> List[Path]:
    """Split a platform path list and discard empty entries."""
    return [Path(item).expanduser() for item in value.split(os.pathsep) if item.strip()]


def discover_homes(explicit: Optional[Sequence[str]] = None) -> List[Path]:
    """Return configured Homes, de-duplicated and limited to real directories."""
    candidates: List[Path] = []
    if explicit:
        candidates.extend(Path(item).expanduser() for item in explicit)
    elif os.environ.get("CODEX_HOMES"):
        candidates.extend(_split_paths(os.environ["CODEX_HOMES"]))
    else:
        if os.environ.get("CODEX_HOME"):
            candidates.append(Path(os.environ["CODEX_HOME"]).expanduser())
        candidates.append(Path.home() / ".codex")

        roots = _split_paths(os.environ.get("CODEX_HOME_ROOTS", ""))
        if not roots:
            roots = [Path.home() / "oyx_ws"]
        for root in roots:
            if root.is_dir():
                candidates.extend(sorted(root.glob(".codex*")))

    result: List[Path] = []
    seen = set()
    for path in candidates:
        try:
            path = path.resolve()
        except OSError:
            continue
        if path in seen or not path.is_dir():
            continue
        # A Home is useful to the monitor when at least one state database is present.
        if not any((path / name).exists() for name in ("state_5.sqlite", "goals_1.sqlite", "thread_history_1.sqlite")):
            continue
        seen.add(path)
        result.append(path)
    return result


def _home_id(path: Path) -> str:
    """Create a stable URL-safe identifier without exposing credentials."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", str(path)).strip("_")
    return safe or "codex-home"


def _timestamp_ms(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    # Codex currently stores *_ms in milliseconds. Older columns may be seconds.
    return number if number >= 10**11 else number * 1000


def _iso_time(value: Any) -> Optional[str]:
    millis = _timestamp_ms(value)
    if millis is None:
        return None
    try:
        return datetime.fromtimestamp(millis / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def _clip(value: Any, limit: int = 280) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


_SECRET_PATTERNS = [
    (re.compile(r"(?i)(openai[_-]?api[_-]?key|codex[_-]?api[_-]?key|api[_-]?key|authorization|password|token)\s*[:=]\s*([^\s,;]+)"), r"\1=***"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{12,}"), "Bearer ***"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"), "sk-***"),
]


def _redact(value: Any, limit: int = 20000) -> str:
    """Keep transcript display useful without copying obvious credentials."""
    text = _clip(value, limit)
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _error_text(value: Any) -> str:
    """Return a searchable, redacted representation of a persisted turn error."""
    if isinstance(value, (dict, list)):
        try:
            value = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            value = str(value)
    return _redact(value, 12000)


def _is_capacity_error(value: Any) -> bool:
    """Match transient model-capacity failures, excluding quota/context failures.

    A watchdog retry is safe for a temporarily overloaded model. Account usage
    limits and context-length errors need a different remediation and must not
    be retried forever by the monitor.
    """
    text = _error_text(value).lower()
    if not text or any(token in text for token in (
        "usage limit", "usagelimit", "quota", "context_length", "context window",
        "context length", "too many tokens", "maximum tokens", "credit_balance",
        "spend_limit", "rate_limit_exceeded",
    )):
        return False
    return any(token in text for token in (
        "serveroverloaded", "server_overloaded", "server is overloaded",
        "temporarily overloaded", "model is overloaded", "model temporarily",
        "selected model is at capacity", "model is at capacity", "at capacity",
    ))


def _text_blocks(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict):
            block_type = block.get("type")
            if block_type in {"text", "input_text", "output_text"} and block.get("text"):
                parts.append(str(block["text"]))
    return "\n".join(parts)


def _parse_transcript_item(item_type: str, raw: Dict[str, Any], turn_id: Any, created_at: Any) -> Optional[Dict[str, Any]]:
    """Normalize persisted Codex item JSON into safe, display-oriented content."""
    kind = item_type or raw.get("type") or "event"
    base = {
        "id": raw.get("id"),
        "turn_id": turn_id,
        "item_type": kind,
        "created_at": _iso_time(created_at),
        "role": "system",
        "kind": "event",
        "title": kind,
        "text": "",
    }
    if kind in {"reasoning", "contextCompaction"}:
        return None
    if kind == "userMessage":
        base.update({"role": "user", "kind": "message", "title": "用户", "text": _redact(_text_blocks(raw.get("content")))})
    elif kind == "agentMessage":
        phase = raw.get("phase") or "assistant"
        base.update({"role": "assistant", "kind": "message", "title": "Codex", "phase": phase, "text": _redact(raw.get("text"))})
    elif kind == "commandExecution":
        base.update({
            "role": "tool", "kind": "command", "title": "命令执行",
            "command": _redact(raw.get("command"), 10000),
            "status": raw.get("status") or "unknown",
            "cwd": _redact(raw.get("cwd"), 1000),
            "text": _redact(raw.get("aggregatedOutput"), 30000),
        })
    elif kind == "fileChange":
        changes = []
        for change in raw.get("changes") or []:
            if not isinstance(change, dict):
                continue
            details = change.get("kind")
            if isinstance(details, dict):
                details = details.get("type") or details.get("kind")
            changes.append({"path": _redact(change.get("path"), 2000), "kind": details or "change", "diff": _redact(change.get("diff"), 30000)})
        base.update({"role": "tool", "kind": "file", "title": "文件变更", "changes": changes, "text": f"{len(changes)} 个文件发生变更"})
    elif kind == "webSearch":
        base.update({"role": "tool", "kind": "search", "title": "网页搜索", "query": _redact(raw.get("query"), 5000), "text": _redact(raw.get("query"), 5000)})
    elif kind == "plan":
        base.update({"role": "assistant", "kind": "plan", "title": "计划", "text": _redact(raw.get("text"), 30000)})
    elif kind in {"subAgentActivity", "collabAgentToolCall"}:
        base.update({"role": "system", "kind": "agent", "title": "Agent 协作", "text": _redact(raw.get("prompt") or raw.get("kind") or raw.get("tool") or raw.get("status"), 6000)})
    elif kind == "sleep":
        base.update({"role": "system", "kind": "event", "title": "等待", "text": f"等待 {raw.get('durationMs') or 0} ms"})
    else:
        text = raw.get("text") or raw.get("summary") or raw.get("query") or raw.get("status") or ""
        if not text:
            return None
        base.update({"title": kind, "text": _redact(text)})
    return base


class AppServerError(RuntimeError):
    pass


class AppServerTimeout(AppServerError):
    pass


def find_app_server_binary(home: Path) -> Optional[Path]:
    binary_names = ["codex.exe", "codex"] if os.name == "nt" else ["codex", "codex.exe"]
    candidates = [
        home / "packages" / "app-server-daemon" / "current" / "bin" / name
        for name in binary_names
    ] + [
        home / "packages" / "standalone" / "current" / "bin" / name
        for name in binary_names
    ]
    release_root = home / "packages" / "app-server-daemon" / "releases"
    if release_root.is_dir():
        candidates.extend(path for release in sorted(release_root.iterdir(), reverse=True) for path in [release / "bin" / name for name in binary_names])
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return path
    return None


class AppServerClient:
    """Small stdio JSON-RPC client for one isolated Codex Home.

    A new process is used per turn. This keeps the monitor from sharing a
    mutable protocol connection across browser requests and makes cleanup
    deterministic if a browser disconnects.
    """

    def __init__(self, home: Path):
        self.home = home
        self.binary = find_app_server_binary(home)
        self.process: Optional[subprocess.Popen] = None
        self.request_id = 0
        self._pending_events: List[Dict[str, Any]] = []
        self._stdout_queue: "queue.Queue[Optional[bytes]]" = queue.Queue()

    def _start(self) -> None:
        if not self.binary:
            raise AppServerError("该 Home 没有可用的 Codex app-server 二进制")
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(self.home)
        self.process = subprocess.Popen(
            [str(self.binary), "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=environment,
            bufsize=0,
        )
        threading.Thread(target=self._pump_stdout, name="codex-app-server-stdout", daemon=True).start()

    def _pump_stdout(self) -> None:
        """Read app-server lines in a thread so stdio also works on Windows.

        ``select.select`` cannot wait on anonymous pipe handles on Windows;
        queueing complete JSON lines keeps the protocol code platform-neutral.
        """
        process = self.process
        if not process or not process.stdout:
            self._stdout_queue.put(None)
            return
        try:
            for line in process.stdout:
                self._stdout_queue.put(line)
        except (OSError, ValueError):
            pass
        finally:
            self._stdout_queue.put(None)

    def _send(self, message: Dict[str, Any]) -> None:
        if not self.process or not self.process.stdin:
            raise AppServerError("app-server 未启动")
        payload = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            self.process.stdin.write(payload)
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise AppServerError("app-server 连接已断开") from exc

    def _read(self, timeout: float = 30.0) -> Optional[Dict[str, Any]]:
        if not self.process:
            raise AppServerError("app-server 未启动")
        try:
            line = self._stdout_queue.get(timeout=timeout)
        except queue.Empty:
            raise AppServerTimeout("app-server 响应超时")
        if line is None:
            raise AppServerError("app-server 已退出")
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise AppServerError("app-server 返回了无法解析的 JSON") from exc
        return value if isinstance(value, dict) else None

    @staticmethod
    def _is_server_request(message: Dict[str, Any]) -> bool:
        return message.get("id") is not None and bool(message.get("method"))

    def _handle_server_request(self, message: Dict[str, Any]) -> None:
        method = str(message.get("method") or "")
        # Until the UI has explicit approval controls, never authorize tools.
        if "permission" in method.lower():
            self._pending_events.append({"type": "approval_required", "method": method, "message": "该 turn 请求额外权限，GUI 尚未提供权限控件，已拒绝"})
            self._send({"id": message["id"], "error": {"code": -32000, "message": "GUI 尚未提供权限审批"}})
        elif "Approval" in method or "approval" in method:
            self._pending_events.append({"type": "approval_required", "method": method, "message": "GUI 尚未提供审批控件，已拒绝该请求"})
            self._send({"id": message["id"], "result": {"decision": "decline"}})
        else:
            self._send({"id": message["id"], "error": {"code": -32000, "message": "此 app-server 请求暂不支持"}})

    def _request(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 30.0) -> Dict[str, Any]:
        self.request_id += 1
        request_id = self.request_id
        self._send({"id": request_id, "method": method, "params": params or {}})
        while True:
            message = self._read(timeout)
            if not message:
                continue
            if self._is_server_request(message):
                self._handle_server_request(message)
                continue
            if message.get("id") == request_id:
                if message.get("error"):
                    error = message["error"]
                    raise AppServerError(str(error.get("message") or "app-server 请求失败"))
                return message.get("result") or {}

    def _initialize(self) -> None:
        self._request("initialize", {"clientInfo": {"name": "codex_home_monitor", "title": "Codex Home Monitor", "version": "0.1.0"}}, timeout=15)
        self._send({"method": "initialized", "params": {}})

    def _start_turn(self, thread_id: str, text: str) -> Dict[str, Any]:
        return self._request("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": text}]}, timeout=30)

    def _stream_turn(self, turn: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
        yield {"type": "started", "turn": turn.get("turn") if isinstance(turn, dict) else turn}
        deadline = time.monotonic() + max(60, int(os.environ.get("CODEX_MONITOR_CHAT_TIMEOUT_SECONDS", "3600")))
        while True:
            while self._pending_events:
                yield self._pending_events.pop(0)
            if time.monotonic() >= deadline:
                raise AppServerError("app-server turn 超过最大等待时间")
            try:
                message = self._read(30.0)
            except AppServerTimeout:
                yield {"type": "heartbeat"}
                continue
            if not message:
                continue
            if self._is_server_request(message):
                self._handle_server_request(message)
                continue
            method = message.get("method")
            params = message.get("params") or {}
            if method == "turn/completed":
                yield {"type": "completed", "turn": params.get("turn") or params}
                return
            if method == "error":
                yield {"type": "error", "stage": "turn", "message": str(params.get("message") or params)}
                return
            if method in {"item/agentMessage/delta", "item/agentMessage/completed", "item/started", "item/completed", "item/commandExecution/outputDelta", "commandExecution/outputDelta", "turn/diff/updated", "turn/plan/updated"}:
                yield {"type": "notification", "method": method, "params": params}

    def run_turn(self, thread_id: str, text: str) -> Iterable[Dict[str, Any]]:
        self._start()
        try:
            self._initialize()
            self._request("thread/resume", {"threadId": thread_id, "excludeTurns": True}, timeout=45)
            yield from self._stream_turn(self._start_turn(thread_id, text))
        finally:
            self.close()

    def run_new_thread(self, text: str, model: str = "", cwd: str = "") -> Iterable[Dict[str, Any]]:
        self._start()
        try:
            self._initialize()
            params: Dict[str, Any] = {}
            if model.strip():
                params["model"] = model.strip()
            if cwd.strip():
                params["cwd"] = cwd.strip()
            result = self._request("thread/start", params, timeout=45)
            thread = result.get("thread") if isinstance(result, dict) else None
            thread_id = thread.get("id") if isinstance(thread, dict) else None
            if not thread_id:
                raise AppServerError("app-server 未返回新会话 ID")
            yield {"type": "created", "thread": thread, "thread_id": thread_id}
            yield from self._stream_turn(self._start_turn(thread_id, text))
        finally:
            self.close()

    def close(self) -> None:
        process, self.process = self.process, None
        if not process:
            return
        try:
            if process.stdin:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.terminate()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass
        try:
            if process.stdout:
                process.stdout.close()
        except OSError:
            pass


class ReadOnlyDatabase:
    def __init__(self, path: Path):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        # Keep the drive colon legal in Windows SQLite URI filenames.
        db_path = str(self.path.resolve()).replace("\\", "/")
        uri = "file:" + quote(db_path, safe="/:") + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=1.5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=1200")
        return connection

    def table_columns(self, connection: sqlite3.Connection, table: str) -> set[str]:
        try:
            return {row[1] for row in connection.execute("PRAGMA table_info(" + table + ")")}
        except sqlite3.Error:
            return set()


def _select_expr(columns: set[str], name: str, alias: Optional[str] = None, default: str = "NULL") -> str:
    output_name = alias or name
    return (f'"{name}" AS "{output_name}"' if name in columns else f"{default} AS \"{output_name}\"")


@dataclass
class HomeReader:
    path: Path

    @property
    def home_id(self) -> str:
        return _home_id(self.path)

    @property
    def label(self) -> str:
        return self.path.name or str(self.path)

    def _db(self, name: str) -> ReadOnlyDatabase:
        return ReadOnlyDatabase(self.path / name)

    def _thread_rows(self) -> List[Dict[str, Any]]:
        db = self._db("state_5.sqlite")
        try:
            connection = db.connect()
        except (OSError, sqlite3.Error):
            return []
        try:
            columns = db.table_columns(connection, "threads")
            if not columns or "id" not in columns:
                return []
            fields = [
                _select_expr(columns, "id"),
                _select_expr(columns, "title"),
                _select_expr(columns, "name"),
                _select_expr(columns, "first_user_message"),
                _select_expr(columns, "preview"),
                _select_expr(columns, "cwd"),
                _select_expr(columns, "model"),
                _select_expr(columns, "model_provider"),
                _select_expr(columns, "agent_nickname"),
                _select_expr(columns, "agent_role"),
                _select_expr(columns, "git_branch"),
                _select_expr(columns, "cli_version"),
                _select_expr(columns, "archived", default="0"),
                _select_expr(columns, "created_at_ms"),
                _select_expr(columns, "updated_at_ms"),
                _select_expr(columns, "recency_at_ms"),
                _select_expr(columns, "created_at"),
                _select_expr(columns, "updated_at"),
                _select_expr(columns, "rollout_path"),
            ]
            order = "updated_at_ms DESC" if "updated_at_ms" in columns else ("updated_at DESC" if "updated_at" in columns else "id")
            return [dict(row) for row in connection.execute(f"SELECT {', '.join(fields)} FROM threads ORDER BY {order}")]
        except sqlite3.Error:
            return []
        finally:
            connection.close()

    def _goal_rows(self) -> Dict[str, Dict[str, Any]]:
        db = self._db("goals_1.sqlite")
        try:
            connection = db.connect()
        except (OSError, sqlite3.Error):
            return {}
        try:
            columns = db.table_columns(connection, "thread_goals")
            if "thread_id" not in columns:
                return {}
            fields = [
                _select_expr(columns, "thread_id"), _select_expr(columns, "goal_id"),
                _select_expr(columns, "objective"), _select_expr(columns, "status"),
                _select_expr(columns, "token_budget"), _select_expr(columns, "tokens_used"),
                _select_expr(columns, "time_used_seconds"), _select_expr(columns, "created_at_ms"),
                _select_expr(columns, "updated_at_ms"),
            ]
            rows = [dict(row) for row in connection.execute(f"SELECT {', '.join(fields)} FROM thread_goals")]
            return {row["thread_id"]: row for row in rows}
        except sqlite3.Error:
            return {}
        finally:
            connection.close()

    def _latest_turns(self) -> Dict[str, Dict[str, Any]]:
        db = self._db("thread_history_1.sqlite")
        try:
            connection = db.connect()
        except (OSError, sqlite3.Error):
            return {}
        try:
            columns = db.table_columns(connection, "thread_turns")
            if "thread_id" not in columns:
                return {}
            fields = [
                _select_expr(columns, "thread_id"), _select_expr(columns, "turn_id"),
                _select_expr(columns, "status"), _select_expr(columns, "started_at"),
                _select_expr(columns, "completed_at"), _select_expr(columns, "duration_ms"),
                _select_expr(columns, "error_json"),
            ]
            latest: Dict[str, Dict[str, Any]] = {}
            for row in connection.execute(f"SELECT {', '.join(fields)} FROM thread_turns"):
                item = dict(row)
                key = item["thread_id"]
                stamp = _timestamp_ms(item.get("completed_at") or item.get("started_at")) or 0
                old_stamp = _timestamp_ms(latest.get(key, {}).get("completed_at") or latest.get(key, {}).get("started_at")) or 0
                if key not in latest or stamp >= old_stamp:
                    latest[key] = item
            return latest
        except sqlite3.Error:
            return {}
        finally:
            connection.close()

    def sessions(self) -> List[Dict[str, Any]]:
        goals = self._goal_rows()
        turns = self._latest_turns()
        output: List[Dict[str, Any]] = []
        for row in self._thread_rows():
            thread_id = row.get("id")
            goal = goals.get(thread_id) or {}
            turn = turns.get(thread_id) or {}
            updated = row.get("updated_at_ms") or row.get("recency_at_ms") or row.get("updated_at") or row.get("created_at_ms") or row.get("created_at")
            title = row.get("title") or row.get("name") or row.get("first_user_message") or row.get("preview") or "未命名会话"
            goal_status = goal.get("status") or "none"
            turn_status = turn.get("status") or "unknown"
            output.append({
                "key": f"{self.home_id}:{thread_id}",
                "home_id": self.home_id,
                "home_label": self.label,
                "thread_id": thread_id,
                "title": _clip(title, 180),
                "preview": _clip(row.get("preview") or row.get("first_user_message"), 320),
                "cwd": row.get("cwd") or "",
                "model": row.get("model") or row.get("model_provider") or "",
                "agent_nickname": row.get("agent_nickname") or "",
                "agent_role": row.get("agent_role") or "",
                "git_branch": row.get("git_branch") or "",
                "cli_version": row.get("cli_version") or "",
                "archived": bool(row.get("archived") or 0),
                "goal": self._format_goal(goal),
                "goal_status": goal_status,
                "latest_turn": self._format_turn(turn),
                "latest_turn_status": turn_status,
                "activity": self._activity(goal_status, turn_status),
                "created_at_ms": _timestamp_ms(row.get("created_at_ms") or row.get("created_at")),
                "updated_at_ms": _timestamp_ms(updated),
                "updated_at": _iso_time(updated),
            })
        return output

    @staticmethod
    def _activity(goal_status: str, turn_status: str) -> str:
        normalized = (turn_status or "").lower()
        if normalized in {"in_progress", "running", "started", "active"}:
            return "working"
        if normalized in {"failed", "error", "errored", "aborted"}:
            return "error"
        if goal_status in {"active", "running"}:
            return "idle_goal"
        return "idle"

    @staticmethod
    def _format_goal(goal: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not goal:
            return None
        return {
            "goal_id": goal.get("goal_id"),
            "objective": _clip(goal.get("objective"), 1000),
            "status": goal.get("status") or "unknown",
            "token_budget": goal.get("token_budget"),
            "tokens_used": goal.get("tokens_used") or 0,
            "time_used_seconds": goal.get("time_used_seconds") or 0,
            "created_at_ms": _timestamp_ms(goal.get("created_at_ms")),
            "updated_at_ms": _timestamp_ms(goal.get("updated_at_ms")),
            "updated_at": _iso_time(goal.get("updated_at_ms")),
        }

    @staticmethod
    def _format_turn(turn: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not turn:
            return None
        error_text = _error_text(turn.get("error_json")) if turn.get("error_json") else ""
        started_at_ms = _timestamp_ms(turn.get("started_at"))
        completed_at_ms = _timestamp_ms(turn.get("completed_at"))
        return {
            "turn_id": turn.get("turn_id"),
            "status": turn.get("status") or "unknown",
            "started_at": _iso_time(turn.get("started_at")),
            "completed_at": _iso_time(turn.get("completed_at")),
            "started_at_ms": started_at_ms,
            "completed_at_ms": completed_at_ms,
            "last_activity_ms": completed_at_ms or started_at_ms,
            "duration_ms": turn.get("duration_ms"),
            "has_error": bool(turn.get("error_json")),
            "error": error_text,
            "capacity_error": _is_capacity_error(error_text),
        }

    def session_detail(self, thread_id: str, content_limit: int = 200, before_created_at_ms: Optional[int] = None) -> Optional[Dict[str, Any]]:
        session = next((item for item in self.sessions() if item["thread_id"] == thread_id), None)
        if not session:
            return None
        db = self._db("thread_history_1.sqlite")
        events: List[Dict[str, Any]] = []
        content: List[Dict[str, Any]] = []
        content_limit = max(1, min(2000, int(content_limit)))
        has_more = False
        next_before_created_at_ms: Optional[int] = None
        try:
            connection = db.connect()
            columns = db.table_columns(connection, "thread_items")
            if columns and "thread_id" in columns:
                fields = [
                    _select_expr(columns, "turn_id"), _select_expr(columns, "item_type"),
                    _select_expr(columns, "created_at_ms"), _select_expr(columns, "started_at_ms"),
                    _select_expr(columns, "completed_at_ms"), _select_expr(columns, "item_json"),
                ]
                clauses = ["thread_id = ?"]
                query_args: List[Any] = [thread_id]
                if before_created_at_ms is not None and "created_at_ms" in columns:
                    clauses.append("created_at_ms < ?")
                    query_args.append(int(before_created_at_ms))
                query_args.append(content_limit + 1)
                query = f"SELECT {', '.join(fields)} FROM thread_items WHERE {' AND '.join(clauses)} ORDER BY created_at_ms DESC LIMIT ?"
                rows = list(connection.execute(query, query_args))
                has_more = len(rows) > content_limit
                rows = rows[:content_limit]
                for row in reversed(rows):
                    item = dict(row)
                    events.append({
                        "turn_id": item.get("turn_id"),
                        "item_type": item.get("item_type") or "event",
                        "created_at": _iso_time(item.get("created_at_ms") or item.get("started_at_ms") or item.get("completed_at_ms")),
                    })
                    raw_text = item.get("item_json")
                    if raw_text:
                        try:
                            raw_item = json.loads(raw_text)
                        except (TypeError, ValueError):
                            raw_item = {}
                        if isinstance(raw_item, dict):
                            parsed = _parse_transcript_item(item.get("item_type") or raw_item.get("type", "event"), raw_item, item.get("turn_id"), item.get("created_at_ms") or item.get("started_at_ms") or item.get("completed_at_ms"))
                            if parsed:
                                parsed["item_id"] = item.get("item_id")
                                content.append(parsed)
                if has_more and rows:
                    try:
                        next_before_created_at_ms = int(dict(rows[-1]).get("created_at_ms"))
                    except (TypeError, ValueError):
                        next_before_created_at_ms = None
            connection.close()
        except (OSError, sqlite3.Error):
            pass
        session["events"] = events
        session["content"] = content
        session["content_limit"] = content_limit
        session["content_truncated"] = has_more
        session["has_more"] = has_more
        session["next_before_created_at_ms"] = next_before_created_at_ms
        session["rollout_path"] = ""  # Never expose local rollout paths through the API.
        return session


class WatchdogManager:
    """Retry only transient model-capacity failures at a bounded interval."""

    def __init__(self, monitor: "Monitor"):
        self.monitor = monitor
        try:
            configured_interval = int(os.environ.get("CODEX_MONITOR_WATCHDOG_INTERVAL_SECONDS", str(WATCHDOG_MIN_INTERVAL)))
        except ValueError:
            configured_interval = WATCHDOG_MIN_INTERVAL
        self.interval_seconds = max(WATCHDOG_MIN_INTERVAL, configured_interval)
        try:
            configured_max_age = int(os.environ.get("CODEX_MONITOR_WATCHDOG_MAX_AGE_SECONDS", str(WATCHDOG_DEFAULT_MAX_AGE_SECONDS)))
        except ValueError:
            configured_max_age = WATCHDOG_DEFAULT_MAX_AGE_SECONDS
        self.max_age_seconds = max(WATCHDOG_MIN_INTERVAL, configured_max_age)
        self.enabled = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self._states: Dict[str, Dict[str, Any]] = {}
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._recent: List[Dict[str, Any]] = []
        if os.environ.get("CODEX_MONITOR_WATCHDOG", "0").lower() in {"1", "true", "yes", "on"}:
            self.set_enabled(True)

    def set_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        with self._lock:
            self.enabled = enabled
            if enabled and (self._thread is None or not self._thread.is_alive()):
                self._stop.clear()
                self._thread = threading.Thread(target=self._loop, name="codex-monitor-watchdog", daemon=True)
                self._thread.start()
            elif not enabled:
                self._stop.set()

    def status(self) -> Dict[str, Any]:
        with self._lock:
            jobs = [{"key": key, **value} for key, value in self._jobs.items()]
            return {
                "enabled": self.enabled,
                "interval_seconds": self.interval_seconds,
                "max_age_seconds": self.max_age_seconds,
                "active_jobs": jobs,
                "recent": list(self._recent[-20:]),
            }

    def _record(self, value: Dict[str, Any]) -> None:
        with self._lock:
            self._recent.append({"at": datetime.now(timezone.utc).isoformat(), **value})
            del self._recent[:-20]

    def _loop(self) -> None:
        while not self._stop.wait(DEFAULT_REFRESH_SECONDS):
            if self.enabled:
                self.scan_once()

    def scan_once(self) -> None:
        if not self.enabled:
            return
        now = time.time()
        for reader in self.monitor.readers:
            try:
                sessions = reader.sessions()
            except Exception as exc:
                self._record({"type": "scan_error", "message": str(exc)})
                continue
            for session in sessions:
                turn = session.get("latest_turn") or {}
                if str(turn.get("status") or "").lower() not in {"failed", "error", "errored"}:
                    continue
                if not turn.get("capacity_error"):
                    continue
                last_activity_ms = turn.get("last_activity_ms")
                # Do not bootstrap from failures that predate this monitor
                # process. Only failures observed during this run are eligible.
                if not last_activity_ms or last_activity_ms < int(self.monitor.started_at * 1000):
                    continue
                if now - (last_activity_ms / 1000) > self.max_age_seconds:
                    continue
                key = session["key"]
                fingerprint = hashlib.sha256((str(turn.get("turn_id")) + "\n" + str(turn.get("error"))).encode("utf-8")).hexdigest()[:20]
                with self._lock:
                    state = self._states.setdefault(key, {"fingerprint": "", "attempts": 0, "next_at": now})
                    if state.get("fingerprint") != fingerprint:
                        state.update({"fingerprint": fingerprint, "attempts": 0, "next_at": now + self.interval_seconds})
                        self._record({"type": "detected", "key": key, "message": "检测到暂时的模型容量错误，等待后重试"})
                    if key in self._jobs or now < float(state.get("next_at") or 0):
                        continue
                    state["attempts"] = int(state.get("attempts") or 0) + 1
                    state["next_at"] = now + self.interval_seconds
                    self._jobs[key] = {"attempt": state["attempts"], "message": "启动自动继续"}
                worker = threading.Thread(target=self._run_job, args=(reader, session, key), name="codex-monitor-watchdog-job", daemon=True)
                worker.start()

    def _run_job(self, reader: HomeReader, session: Dict[str, Any], key: str) -> None:
        goal = session.get("goal") or {}
        message = "/goal resume" if goal.get("status") in {"active", "paused", "running"} else "继续"
        writer_key: Optional[str] = None
        try:
            writer_key = self.monitor._acquire_writer(session["home_id"], session["thread_id"])
            failed = False
            for event in AppServerClient(reader.path).run_turn(session["thread_id"], message):
                failed = failed or event.get("type") == "error"
            self._record({"type": "completed" if not failed else "error", "key": key, "message": message})
        except Exception as exc:
            self._record({"type": "error", "key": key, "message": str(exc)})
        finally:
            if writer_key:
                self.monitor._release_writer(writer_key)
            with self._lock:
                self._jobs.pop(key, None)


class Monitor:
    def __init__(self, paths: Sequence[Path]):
        self.readers = [HomeReader(path) for path in paths]
        self.started_at = time.time()
        self._writer_lock = threading.Lock()
        self._active_writers: set[str] = set()
        self.watchdog = WatchdogManager(self)

    def _writer_key(self, home_id: str, thread_id: str) -> str:
        return f"{home_id}:{thread_id}"

    def _acquire_writer(self, home_id: str, thread_id: str) -> str:
        key = self._writer_key(home_id, thread_id)
        with self._writer_lock:
            if key in self._active_writers:
                raise AppServerError("该会话已有一个正在运行的写入任务")
            self._active_writers.add(key)
        return key

    def _release_writer(self, key: str) -> None:
        with self._writer_lock:
            self._active_writers.discard(key)

    def homes(self) -> List[Dict[str, Any]]:
        result = []
        for reader in self.readers:
            sessions = reader.sessions()
            result.append({
                "home_id": reader.home_id,
                "label": reader.label,
                "path": str(reader.path),
                "session_count": len(sessions),
                "goal_count": sum(1 for item in sessions if item.get("goal")),
            })
        return result

    def sessions(self, params: Dict[str, List[str]]) -> List[Dict[str, Any]]:
        home_filter = (params.get("home_id") or [""])[0]
        goal_filter = (params.get("goal_status") or [""])[0].lower()
        query = (params.get("q") or [""])[0].strip().lower()
        try:
            limit = max(1, min(5000, int((params.get("limit") or ["500"])[0])))
        except ValueError:
            limit = 500
        result: List[Dict[str, Any]] = []
        for reader in self.readers:
            if home_filter and reader.home_id != home_filter:
                continue
            for item in reader.sessions():
                if goal_filter and item["goal_status"].lower() != goal_filter:
                    continue
                haystack = " ".join([item.get("title", ""), item.get("preview", ""), item.get("cwd", ""), item.get("model", "")]).lower()
                if query and query not in haystack:
                    continue
                result.append(item)
        result.sort(key=lambda item: item.get("updated_at_ms") or 0, reverse=True)
        return result[:limit]

    def detail(self, home_id: str, thread_id: str, content_limit: int = 200, before_created_at_ms: Optional[int] = None) -> Optional[Dict[str, Any]]:
        reader = next((item for item in self.readers if item.home_id == home_id), None)
        return reader.session_detail(thread_id, content_limit, before_created_at_ms) if reader else None

    def chat(self, home_id: str, thread_id: str, text: str) -> Iterable[Dict[str, Any]]:
        reader = next((item for item in self.readers if item.home_id == home_id), None)
        if not reader:
            raise AppServerError("找不到指定的 Codex Home")
        if not text.strip():
            raise AppServerError("消息不能为空")
        if len(text) > 20000:
            raise AppServerError("消息长度不能超过 20000 个字符")
        key = self._acquire_writer(home_id, thread_id)
        def stream() -> Iterable[Dict[str, Any]]:
            try:
                yield from AppServerClient(reader.path).run_turn(thread_id, text.strip())
            finally:
                self._release_writer(key)
        return stream()

    def new_chat(self, home_id: str, text: str, model: str = "", cwd: str = "") -> Iterable[Dict[str, Any]]:
        reader = next((item for item in self.readers if item.home_id == home_id), None)
        if not reader:
            raise AppServerError("找不到指定的 Codex Home")
        if not text.strip():
            raise AppServerError("消息不能为空")
        if len(text) > 20000:
            raise AppServerError("消息长度不能超过 20000 个字符")
        if cwd and (not os.path.isabs(cwd) or not Path(cwd).is_dir()):
            raise AppServerError("工作目录必须是服务器上的绝对目录")
        client = AppServerClient(reader.path)
        def stream() -> Iterable[Dict[str, Any]]:
            try:
                yield from client.run_new_thread(text.strip(), model.strip(), cwd.strip())
            finally:
                client.close()
        return stream()

    def watchdog_status(self) -> Dict[str, Any]:
        return self.watchdog.status()

    def set_watchdog(self, enabled: bool) -> Dict[str, Any]:
        self.watchdog.set_enabled(enabled)
        return self.watchdog.status()


class RequestHandler(BaseHTTPRequestHandler):
    monitor: Monitor

    def log_message(self, format: str, *args: Any) -> None:
        # Keep the console useful without logging query content or paths.
        print(f"{self.address_string()} - {format % args}")

    def _send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path: Path, content_type: str) -> None:
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            self._send_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_sse(self, event: Dict[str, Any]) -> None:
        data = ("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode("utf-8")
        self.wfile.write(data)
        self.wfile.flush()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        try:
            if parsed.path == "/api/health":
                self._send_json({"ok": True, "homes": len(self.monitor.readers), "refresh_seconds": DEFAULT_REFRESH_SECONDS})
            elif parsed.path == "/api/homes":
                self._send_json({"homes": self.monitor.homes()})
            elif parsed.path == "/api/sessions":
                sessions = self.monitor.sessions(params)
                self._send_json({"sessions": sessions, "count": len(sessions), "generated_at": datetime.now(timezone.utc).isoformat()})
            elif parsed.path == "/api/session":
                home_id = (params.get("home_id") or [""])[0]
                thread_id = (params.get("thread_id") or [""])[0]
                try:
                    content_limit = max(1, min(2000, int((params.get("content_limit") or ["200"])[0])))
                except ValueError:
                    content_limit = 200
                try:
                    before = (params.get("before_created_at_ms") or [""])[0]
                    before_created_at_ms = int(before) if before else None
                except ValueError:
                    before_created_at_ms = None
                detail = self.monitor.detail(home_id, thread_id, content_limit, before_created_at_ms)
                if detail is None:
                    self._send_json({"error": "session_not_found"}, HTTPStatus.NOT_FOUND)
                else:
                    self._send_json(detail)
            elif parsed.path == "/api/watchdog":
                self._send_json(self.monitor.watchdog_status())
            elif parsed.path in {"/", "/index.html"}:
                self._send_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
            elif parsed.path == "/app.js":
                self._send_file(WEB_DIR / "app.js", "application/javascript; charset=utf-8")
            elif parsed.path == "/styles.css":
                self._send_file(WEB_DIR / "styles.css", "text/css; charset=utf-8")
            elif parsed.path == "/session.html":
                self._send_file(WEB_DIR / "session.html", "text/html; charset=utf-8")
            elif parsed.path == "/session.js":
                self._send_file(WEB_DIR / "session.js", "application/javascript; charset=utf-8")
            elif parsed.path == "/new.html":
                self._send_file(WEB_DIR / "new.html", "text/html; charset=utf-8")
            elif parsed.path == "/new.js":
                self._send_file(WEB_DIR / "new.js", "application/javascript; charset=utf-8")
            else:
                self._send_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:  # Keep the API alive when one Home changes during a read.
            print(f"request error: {type(exc).__name__}: {exc}")
            self._send_json({"error": "internal_error"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path not in {"/api/chat", "/api/chat/new", "/api/watchdog"}:
            self._send_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
            return
        if parsed.path == "/api/watchdog":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                return self._send_json(self.monitor.set_watchdog(bool(body.get("enabled"))))
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                return self._send_json({"error": "invalid_json", "message": str(exc)}, HTTPStatus.BAD_REQUEST)
        stream_started = False
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 100000:
                self._send_json({"error": "invalid_body"}, HTTPStatus.BAD_REQUEST)
                return
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            home_id = str(body.get("home_id") or "")
            thread_id = str(body.get("thread_id") or "")
            text = str(body.get("message") or "")
            if parsed.path == "/api/chat/new":
                events = self.monitor.new_chat(home_id, text, str(body.get("model") or ""), str(body.get("cwd") or ""))
            else:
                events = self.monitor.chat(home_id, thread_id, text)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            stream_started = True
            self._send_sse({"type": "status", "message": "正在连接 app-server"})
            for event in events:
                self._send_sse(event)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            self._send_json({"error": "invalid_json", "message": str(exc)}, HTTPStatus.BAD_REQUEST)
        except AppServerError as exc:
            # Headers may already be committed when a turn fails, so use SSE then.
            if stream_started:
                try:
                    self._send_sse({"type": "error", "stage": "app_server", "message": str(exc)})
                except (BrokenPipeError, OSError):
                    pass
            else:
                try:
                    self.send_response(HTTPStatus.BAD_GATEWAY)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    payload = json.dumps({"error": "app_server_error", "message": str(exc)}, ensure_ascii=False).encode("utf-8")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except (BrokenPipeError, OSError):
                    pass
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            print(f"chat error: {type(exc).__name__}: {exc}")
            try:
                self._send_json({"error": "internal_error"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            except (BrokenPipeError, OSError):
                pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Codex Home monitor")
    parser.add_argument("--host", default=os.environ.get("CODEX_MONITOR_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("CODEX_MONITOR_PORT", "8787")))
    parser.add_argument("--home", action="append", dest="homes", help="Codex Home path; repeat for multiple Homes")
    args = parser.parse_args()
    paths = discover_homes(args.homes)
    if not paths:
        print("No Codex Homes found. Set CODEX_HOMES or pass --home.")
    monitor = Monitor(paths)
    RequestHandler.monitor = monitor
    server = ThreadingHTTPServer((args.host, args.port), RequestHandler)
    print(f"Codex Home Monitor listening on http://{args.host}:{args.port}")
    print("Homes:")
    for path in paths:
        print(f"  - {path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
