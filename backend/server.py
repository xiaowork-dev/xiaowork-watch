#!/usr/bin/env python3
"""A real, persistent control service using Python 3.8+ and SQLite only."""
import argparse
import base64
import concurrent.futures
import contextlib
import datetime
import hashlib
import hmac
import http.cookies
import http.server
import importlib.util
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import shlex
import socketserver
import sqlite3
import stat
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit
import uuid

if __package__:
    from .network import TargetError, check_http, host_name, public_ip, resolve_target, url_target
else:
    # Resolve only the adjacent release-owned module, including Python -I and
    # embedded Windows interpreters that omit the script directory from sys.path.
    _spec = importlib.util.spec_from_file_location("xiaowork_watch_network", Path(__file__).resolve().with_name("network.py"))
    _network = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_network)
    TargetError, check_http, host_name = _network.TargetError, _network.check_http, _network.host_name
    public_ip, resolve_target, url_target = _network.public_ip, _network.resolve_target, _network.url_target

VERSION = "0.3.0"
COOKIE = "xiaowork_watch_session"
SESSION_SECONDS = 8 * 3600
HEARTBEAT_SECONDS = 30
OFFLINE_SECONDS = 90
MAX_HISTORY = 1000
MAX_TOTAL_HISTORY = 100000
MAX_OBJECTS = 1000


class APIError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def stamp(value):
    if value is None:
        return None
    return datetime.datetime.fromtimestamp(value, datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def digest(value):
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def password_hash(password):
    salt = secrets.token_bytes(16)
    hashed = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=32)
    return "scrypt$16384$8$1$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(hashed).decode()


def password_matches(password, encoded):
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$")
        if algorithm != "scrypt" or (int(n), int(r), int(p)) != (16384, 8, 1):
            return False
        hashed = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt), n=16384, r=8, p=1, dklen=32)
        return hmac.compare_digest(hashed, base64.b64decode(expected))
    except (ValueError, TypeError):
        return False


def integer(value, label, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise APIError(400, label + "必须为 " + str(minimum) + "–" + str(maximum) + " 的整数。")
    return value


def text(value, label, maximum=100, required=True):
    if not isinstance(value, str) or any(ord(char) < 32 for char in value):
        raise APIError(400, label + "必须为有效文本。")
    try:
        value.encode("utf-8")
    except UnicodeError as error:
        raise APIError(400, label + "必须为有效文本。") from error
    value = value.strip()
    if len(value) > maximum or (required and not value):
        raise APIError(400, label + "长度无效。")
    return value


def boolean(value):
    if type(value) is not bool:
        raise APIError(400, "enabled 必须为布尔值。")
    return value


def fields(body, allowed, required=()):
    if not isinstance(body, dict) or set(body) - set(allowed) or any(key not in body for key in required):
        raise APIError(400, "请求字段缺失或不受支持。")


SCHEMA = """
CREATE TABLE IF NOT EXISTS admin (username TEXT PRIMARY KEY, password_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS monitors (
 id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, url TEXT NOT NULL, method TEXT NOT NULL,
 interval_seconds INTEGER NOT NULL, timeout_ms INTEGER NOT NULL, enabled INTEGER NOT NULL,
 created REAL NOT NULL, updated REAL NOT NULL, revision INTEGER NOT NULL DEFAULT 1, next_due REAL NOT NULL);
CREATE TABLE IF NOT EXISTS monitor_checks (
 id INTEGER PRIMARY KEY AUTOINCREMENT, monitor_id INTEGER NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
 success INTEGER NOT NULL, http_code INTEGER, response_ms REAL NOT NULL, error_type TEXT, error_message TEXT, checked REAL NOT NULL);
CREATE INDEX IF NOT EXISTS monitor_history ON monitor_checks(monitor_id,id DESC);
CREATE TABLE IF NOT EXISTS hosts (
 id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, address TEXT NOT NULL, region TEXT NOT NULL,
 enabled INTEGER NOT NULL, credential_hash TEXT UNIQUE, last_seen REAL, revision INTEGER NOT NULL DEFAULT 1,
 next_due REAL NOT NULL);
CREATE TABLE IF NOT EXISTS nodes (
 id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, region TEXT NOT NULL, enabled INTEGER NOT NULL,
 credential_hash TEXT UNIQUE, last_seen REAL);
CREATE TABLE IF NOT EXISTS associations (
 host_id INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
 node_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE, PRIMARY KEY(host_id,node_id));
CREATE TABLE IF NOT EXISTS enrollments (
 token_hash TEXT PRIMARY KEY, role TEXT NOT NULL, object_id INTEGER NOT NULL, expires REAL NOT NULL,
 UNIQUE(role,object_id));
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, host_id INTEGER NOT NULL, node_id INTEGER NOT NULL, target TEXT NOT NULL,
 host_revision INTEGER NOT NULL, node_name TEXT NOT NULL, region TEXT NOT NULL, created REAL NOT NULL,
 expires REAL NOT NULL, state TEXT NOT NULL, credential_hash TEXT, result_json TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS active_pair ON jobs(host_id,node_id) WHERE state IN ('queued','leased');
CREATE INDEX IF NOT EXISTS node_jobs ON jobs(node_id,state,created);
CREATE TABLE IF NOT EXISTS fleet_results (
 id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT UNIQUE NOT NULL, host_id INTEGER NOT NULL, node_id INTEGER NOT NULL,
 node_name TEXT NOT NULL, region TEXT NOT NULL, sent INTEGER NOT NULL, received INTEGER NOT NULL,
 avg_rtt REAL, status TEXT NOT NULL, error TEXT, checked REAL NOT NULL);
CREATE INDEX IF NOT EXISTS fleet_history ON fleet_results(host_id,id DESC);
CREATE INDEX IF NOT EXISTS pair_history ON fleet_results(host_id,node_id,id DESC);
"""


class Application:
    def __init__(self, data_dir, allow_private=False, start_scheduler=True, clock=time.time, agent_dir=None):
        self.data_dir = Path(data_dir).absolute()
        if self.data_dir.is_symlink():
            raise ValueError("Data directory cannot be a symlink")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.data_dir.is_dir():
            raise ValueError("Data directory is not a directory")
        os.chmod(str(self.data_dir), 0o700)
        database = self.data_dir / "watch.sqlite3"
        for candidate in (database, database.with_name(database.name + "-wal"), database.with_name(database.name + "-shm")):
            if candidate.is_symlink() or (candidate.exists() and not stat.S_ISREG(candidate.stat().st_mode)):
                raise ValueError("Unsafe database file")
        self.db = sqlite3.connect(str(database), check_same_thread=False, timeout=10)
        os.chmod(str(database), 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        for sidecar in (self.data_dir / "watch.sqlite3-wal", self.data_dir / "watch.sqlite3-shm"):
            if sidecar.exists():
                os.chmod(str(sidecar), 0o600)
        self.lock = threading.RLock()
        self.clock, self.allow_private = clock, allow_private
        self.agent_dir = Path(agent_dir) if agent_dir else None
        self.stop = threading.Event()
        self.network_slots = threading.BoundedSemaphore(4)
        self.running_lock = threading.Lock()
        self.running_monitors = set()
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="watch-http")
        self.fleet_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="watch-fleet")
        self.fleet_slots = threading.BoundedSemaphore(50)
        self.fleet_running = set()
        self.fleet_lock = threading.Lock()
        self.login_lock = threading.Lock()
        self.password_slots = threading.BoundedSemaphore(2)
        self.login_attempts = {}
        self.dummy_password = password_hash(secrets.token_urlsafe(24))
        self.scheduler = None
        if start_scheduler:
            self.scheduler = threading.Thread(target=self._schedule_loop, name="watch-scheduler", daemon=True)
            self.scheduler.start()

    @contextlib.contextmanager
    def transaction(self):
        with self.lock:
            with self.db:
                yield self.db

    def close(self):
        self.stop.set()
        if self.scheduler:
            self.scheduler.join(timeout=5)
        self.pool.shutdown(wait=True)
        self.fleet_pool.shutdown(wait=True)
        with self.lock:
            self.db.close()

    def init_admin(self, reset=False):
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM admin").fetchone() and not reset:
                return None
            password = secrets.token_urlsafe(18)
            db.execute("INSERT OR REPLACE INTO admin VALUES (?,?)", ("admin", password_hash(password)))
            if reset:
                db.execute("DELETE FROM sessions")
            return password

    def login(self, body, client):
        fields(body, {"username", "password"}, {"username", "password"})
        username = text(body["username"], "用户名")
        password = body["password"]
        if not isinstance(password, str) or not 1 <= len(password) <= 256:
            raise APIError(400, "密码长度无效。")
        now = self.clock()
        with self.login_lock:
            self.login_attempts = {key: [value for value in attempts if value > now - 60]
                                   for key, attempts in self.login_attempts.items() if attempts and attempts[-1] > now - 60}
            if len(self.login_attempts) >= 256 and client not in self.login_attempts:
                raise APIError(429, "登录请求过于频繁，请稍后重试。")
            attempts = self.login_attempts.setdefault(client, [])
            if len(attempts) >= 5:
                raise APIError(429, "登录请求过于频繁，请稍后重试。")
            attempts.append(now)
        with self.lock:
            row = self.db.execute("SELECT password_hash FROM admin WHERE username=?", (username,)).fetchone()
        if not self.password_slots.acquire(blocking=False):
            raise APIError(429, "登录请求过于频繁，请稍后重试。")
        try:
            valid = password_matches(password, row[0] if row else self.dummy_password)
        finally:
            self.password_slots.release()
        if not row or not valid:
            raise APIError(401, "用户名或密码错误。")
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.transaction() as db:
            current = db.execute("SELECT password_hash FROM admin WHERE username=?", (username,)).fetchone()
            if current is None or current[0] != row[0]:
                raise APIError(401, "管理员密码已改变，请重新登录。")
            db.execute("DELETE FROM sessions WHERE expires<=?", (now,))
            db.execute("INSERT INTO sessions VALUES (?,?,?)", (digest(token), csrf, now + SESSION_SECONDS))
            db.execute("DELETE FROM sessions WHERE token_hash IN (SELECT token_hash FROM sessions ORDER BY expires DESC LIMIT -1 OFFSET 100)")
        return {"authenticated": True, "username": "admin", "csrfToken": csrf}, token

    def session(self, token):
        if not token or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", token):
            return None
        with self.lock:
            row = self.db.execute("SELECT csrf FROM sessions WHERE token_hash=? AND expires>?", (digest(token), self.clock())).fetchone()
        return {"authenticated": True, "username": "admin", "csrfToken": row[0]} if row else None

    def logout(self, token):
        with self.transaction() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (digest(token),))

    def _target(self, value, url=False):
        try:
            if url:
                normalized, unused_scheme, hostname, port, unused_path, unused_authority = url_target(value)
                resolve_target(hostname, port, self.allow_private)
                return value
            normalized = host_name(value)
            resolve_target(normalized, 80, self.allow_private)
            return normalized
        except TargetError as error:
            raise APIError(400, str(error)) from error

    def _monitor_input(self, body):
        required = {"name", "url", "method", "intervalSeconds", "timeoutMs", "enabled"}
        fields(body, required, required)
        name = text(body["name"], "名称")
        url = text(body["url"], "URL", 1024)
        if body["method"] not in ("GET", "HEAD"):
            raise APIError(400, "检测方法仅支持 GET/HEAD。")
        return (name, self._target(url, url=True), body["method"],
                integer(body["intervalSeconds"], "间隔秒数", 30, 86400),
                integer(body["timeoutMs"], "超时毫秒数", 1000, 30000), boolean(body["enabled"]))

    def _found(self, db, table, object_id):
        row = db.execute("SELECT * FROM " + table + " WHERE id=?", (object_id,)).fetchone()
        if row is None:
            raise APIError(404, "该目标不存在。")
        return row

    def _check_model(self, row):
        return {"id": row["id"], "monitorId": row["monitor_id"], "success": bool(row["success"]),
                "httpCode": row["http_code"], "responseTimeMs": row["response_ms"],
                "errorType": row["error_type"], "errorMessage": row["error_message"], "checkedAt": stamp(row["checked"])}

    def _monitor_model(self, row):
        last = self.db.execute("SELECT * FROM monitor_checks WHERE monitor_id=? ORDER BY id DESC LIMIT 1", (row["id"],)).fetchone()
        return {"id": row["id"], "name": row["name"], "url": row["url"], "method": row["method"],
                "intervalSeconds": row["interval_seconds"], "timeoutMs": row["timeout_ms"], "enabled": bool(row["enabled"]),
                "initial": row["name"][0].upper(), "color": "purple", "createdAt": stamp(row["created"]), "updatedAt": stamp(row["updated"]),
                "lastStatus": ("UP" if last["success"] else "DOWN") if last else "UNKNOWN",
                "lastHttpCode": last["http_code"] if last else None,
                "lastResponseTimeMs": last["response_ms"] if last else None, "lastCheckedAt": stamp(last["checked"]) if last else None}

    def monitors(self, object_id=None, private=False):
        with self.lock:
            if object_id is not None:
                models = [self._monitor_model(self._found(self.db, "monitors", object_id))]
            else:
                models = [self._monitor_model(row) for row in self.db.execute("SELECT * FROM monitors ORDER BY id")]
            if not private:
                for model in models:
                    parsed = urlsplit(model["url"])
                    model["url"] = parsed._replace(query="", fragment="").geturl()
            return models[0] if object_id is not None else models

    def save_monitor(self, body, object_id=None):
        values, now = self._monitor_input(body), self.clock()
        with self.transaction() as db:
            if object_id is None:
                if db.execute("SELECT count(*) FROM monitors").fetchone()[0] >= MAX_OBJECTS:
                    raise APIError(409, "监控数量达到上限。")
                cursor = db.execute("INSERT INTO monitors(name,url,method,interval_seconds,timeout_ms,enabled,created,updated,next_due) VALUES (?,?,?,?,?,?,?,?,?)",
                                    values + (now, now, now))
                object_id = cursor.lastrowid
            else:
                self._found(db, "monitors", object_id)
                db.execute("UPDATE monitors SET name=?,url=?,method=?,interval_seconds=?,timeout_ms=?,enabled=?,updated=?,revision=revision+1,next_due=? WHERE id=?",
                           values + (now, now, object_id))
            return self._monitor_model(self._found(db, "monitors", object_id))

    def enable(self, table, object_id, body):
        fields(body, {"enabled"}, {"enabled"})
        value = boolean(body["enabled"])
        with self.transaction() as db:
            self._found(db, table, object_id)
            if table == "monitors":
                db.execute("UPDATE monitors SET enabled=?,revision=revision+1,updated=?,next_due=? WHERE id=?", (value, self.clock(), self.clock(), object_id))
                return self._monitor_model(self._found(db, table, object_id))
            db.execute("UPDATE " + table + " SET enabled=? WHERE id=?", (value, object_id))
            if not value:
                column = "host_id" if table == "hosts" else "node_id"
                db.execute("UPDATE jobs SET state='cancelled' WHERE " + column + "=? AND state IN ('queued','leased')", (object_id,))
            return self._fleet_model(self._found(db, table, object_id), table)

    def remove(self, table, object_id):
        with self.transaction() as db:
            self._found(db, table, object_id)
            if table != "monitors":
                role, column = ("vps", "host_id") if table == "hosts" else ("probe", "node_id")
                db.execute("DELETE FROM enrollments WHERE role=? AND object_id=?", (role, object_id))
                db.execute("DELETE FROM jobs WHERE " + column + "=?", (object_id,))
                if table == "hosts":
                    db.execute("DELETE FROM fleet_results WHERE host_id=?", (object_id,))
            db.execute("DELETE FROM " + table + " WHERE id=?", (object_id,))
        return {"deleted": True}

    def _reserve_monitor(self, object_id):
        with self.running_lock:
            if object_id in self.running_monitors:
                raise APIError(409, "此监控正在检测，请等待结果。")
            if not self.network_slots.acquire(blocking=False):
                raise APIError(429, "检测并发已达到上限，请稍后重试。")
            self.running_monitors.add(object_id)

    def _release_monitor(self, object_id):
        with self.running_lock:
            self.running_monitors.discard(object_id)
            self.network_slots.release()

    def _prune(self, db, table, column, object_id):
        db.execute("DELETE FROM " + table + " WHERE " + column + "=? AND id NOT IN (SELECT id FROM " + table + " WHERE " + column + "=? ORDER BY id DESC LIMIT ?)", (object_id, object_id, MAX_HISTORY))
        db.execute("DELETE FROM " + table + " WHERE id < (SELECT id FROM " + table + " ORDER BY id DESC LIMIT 1 OFFSET ?)", (MAX_TOTAL_HISTORY - 1,))

    def check_monitor(self, object_id, reserved=False):
        if not reserved:
            self._reserve_monitor(object_id)
        try:
            with self.transaction() as db:
                snapshot = dict(self._found(db, "monitors", object_id))
                if not snapshot["enabled"]:
                    raise APIError(409, "请先启用监控。")
                db.execute("UPDATE monitors SET next_due=? WHERE id=?", (self.clock() + snapshot["interval_seconds"], object_id))
            result = check_http(snapshot["url"], snapshot["method"], snapshot["timeout_ms"], self.allow_private)
            now = self.clock()
            with self.transaction() as db:
                current = self._found(db, "monitors", object_id)
                if current["revision"] != snapshot["revision"] or not current["enabled"]:
                    raise APIError(409, "检测期间配置已改变，旧结果未写入。")
                cursor = db.execute("INSERT INTO monitor_checks(monitor_id,success,http_code,response_ms,error_type,error_message,checked) VALUES (?,?,?,?,?,?,?)",
                                    (object_id, result["success"], result["httpCode"], result["responseTimeMs"], result["errorType"], result["errorMessage"], now))
                db.execute("UPDATE monitors SET updated=? WHERE id=?", (now, object_id))
                model = self._check_model(db.execute("SELECT * FROM monitor_checks WHERE id=?", (cursor.lastrowid,)).fetchone())
                self._prune(db, "monitor_checks", "monitor_id", object_id)
                return model
        finally:
            self._release_monitor(object_id)

    def history(self, kind, object_id, page, size):
        with self.lock:
            table, column, parent = ("monitor_checks", "monitor_id", "monitors") if kind == "monitors" else ("fleet_results", "host_id", "hosts")
            self._found(self.db, parent, object_id)
            total = self.db.execute("SELECT count(*) FROM " + table + " WHERE " + column + "=?", (object_id,)).fetchone()[0]
            rows = self.db.execute("SELECT * FROM " + table + " WHERE " + column + "=? ORDER BY id DESC LIMIT ? OFFSET ?", (object_id, size, (page - 1) * size)).fetchall()
            model = self._check_model if kind == "monitors" else self._result_model
            return {"records": [model(row) for row in rows], "total": total, "page": page, "size": size}

    def _fleet_model(self, row, table):
        registered = row["credential_hash"] is not None
        result = {"id": row["id"], "name": row["name"], "region": row["region"], "enabled": bool(row["enabled"]),
                  "agentState": ("ONLINE" if row["last_seen"] is not None and self.clock() - row["last_seen"] <= OFFLINE_SECONDS else "OFFLINE") if registered else "PENDING",
                  "lastSeenAt": stamp(row["last_seen"])}
        if table == "hosts":
            result.update({"address": row["address"], "nodeIds": [item[0] for item in self.db.execute("SELECT node_id FROM associations WHERE host_id=? ORDER BY node_id", (row["id"],))]})
        return result

    def fleet_items(self, table, object_id=None):
        with self.lock:
            if object_id is not None:
                return self._fleet_model(self._found(self.db, table, object_id), table)
            return [self._fleet_model(row, table) for row in self.db.execute("SELECT * FROM " + table + " ORDER BY id")]

    def _result_model(self, row):
        return {"id": row["id"], "hostId": row["host_id"], "nodeId": row["node_id"], "nodeName": row["node_name"], "region": row["region"],
                "sent": row["sent"], "received": row["received"], "avgRttMs": row["avg_rtt"], "status": row["status"], "error": row["error"], "checkedAt": stamp(row["checked"])}

    def fleet(self):
        with self.lock:
            rows = self.db.execute("SELECT r.* FROM fleet_results r WHERE r.id=(SELECT max(t.id) FROM fleet_results t WHERE t.host_id=r.host_id AND t.node_id=r.node_id) ORDER BY r.id DESC").fetchall()
            return {"hosts": self.fleet_items("hosts"), "nodes": self.fleet_items("nodes"), "results": [self._result_model(row) for row in rows]}

    def save_fleet(self, table, body, object_id=None):
        allowed = {"name", "region", "enabled"} | ({"address", "nodeIds"} if table == "hosts" else set())
        fields(body, allowed, allowed)
        name, region, enabled = text(body["name"], "名称"), text(body["region"], "地区", required=False), boolean(body["enabled"])
        address, node_ids = None, []
        if table == "hosts":
            address = self._target(text(body["address"], "地址", 253))
            if not isinstance(body["nodeIds"], list) or len(body["nodeIds"]) > 100:
                raise APIError(400, "测试节点选择无效。")
            node_ids = sorted(set(integer(value, "测试节点编号", 1, 2147483647) for value in body["nodeIds"]))
        with self.transaction() as db:
            for node_id in node_ids:
                self._found(db, "nodes", node_id)
            if object_id is None:
                if db.execute("SELECT count(*) FROM " + table).fetchone()[0] >= MAX_OBJECTS:
                    raise APIError(409, "目标数量达到上限。")
                if table == "hosts":
                    cursor = db.execute("INSERT INTO hosts(name,address,region,enabled,next_due) VALUES (?,?,?,?,?)", (name, address, region, enabled, self.clock() + 60))
                else:
                    cursor = db.execute("INSERT INTO nodes(name,region,enabled) VALUES (?,?,?)", (name, region, enabled))
                object_id = cursor.lastrowid
            else:
                previous = self._found(db, table, object_id)
                if table == "hosts":
                    db.execute("UPDATE hosts SET name=?,address=?,region=?,enabled=?,revision=revision+1,next_due=? WHERE id=?", (name, address, region, enabled, self.clock() + 60, object_id))
                    db.execute("UPDATE jobs SET state='cancelled' WHERE host_id=? AND state IN ('queued','leased')", (object_id,))
                    if previous["address"] != address:
                        db.execute("DELETE FROM fleet_results WHERE host_id=?", (object_id,))
                else:
                    db.execute("UPDATE nodes SET name=?,region=?,enabled=? WHERE id=?", (name, region, enabled, object_id))
                    if not enabled:
                        db.execute("UPDATE jobs SET state='cancelled' WHERE node_id=? AND state IN ('queued','leased')", (object_id,))
            if table == "hosts":
                db.execute("DELETE FROM associations WHERE host_id=?", (object_id,))
                db.executemany("INSERT INTO associations VALUES (?,?)", ((object_id, value) for value in node_ids))
            return self._fleet_model(self._found(db, table, object_id), table)

    def enrollment(self, role, object_id, base_url):
        table = "hosts" if role == "vps" else "nodes"
        token, now = secrets.token_urlsafe(32), self.clock()
        source = self.asset("agent.py")
        expected_sha = hashlib.sha256(source).hexdigest()
        with self.transaction() as db:
            self._found(db, table, object_id)
            db.execute("DELETE FROM enrollments WHERE role=? AND object_id=?", (role, object_id))
            db.execute("INSERT INTO enrollments VALUES (?,?,?,?)", (digest(token), role, object_id, now + 600))
        filename = "xiaowork-watch-agent-install.sh"
        protocol = "'=https'" if base_url.startswith("https:") else "'=http,https'"
        command = "curl -fsS --proto " + protocol + " " + shlex.quote(base_url + "/agent/install.sh") + " -o " + filename + " && sudo bash " + filename
        command += " --server " + shlex.quote(base_url) + " --enrollment-token " + shlex.quote(token) + " --agent-sha " + expected_sha
        if base_url.startswith("http:"):
            command += " --development"
        return {"demo": False, "command": command, "expiresAt": stamp(now + 600)}

    def enroll(self, body):
        fields(body, {"token"}, {"token"})
        token = body["token"]
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", token):
            raise APIError(400, "注册码格式无效。")
        now, credential = self.clock(), secrets.token_urlsafe(32)
        with self.transaction() as db:
            row = db.execute("SELECT * FROM enrollments WHERE token_hash=?", (digest(token),)).fetchone()
            if not row or row["expires"] <= now:
                raise APIError(401, "注册码无效、已使用或已过期。")
            table, column = ("hosts", "host_id") if row["role"] == "vps" else ("nodes", "node_id")
            self._found(db, table, row["object_id"])
            db.execute("UPDATE " + table + " SET credential_hash=?,last_seen=? WHERE id=?", (digest(credential), now, row["object_id"]))
            db.execute("DELETE FROM enrollments WHERE role=? AND object_id=?", (row["role"], row["object_id"]))
            db.execute("UPDATE jobs SET state='cancelled' WHERE " + column + "=? AND state IN ('queued','leased')", (row["object_id"],))
            return {"credential": credential, "role": row["role"], "id": row["object_id"], "heartbeatSeconds": HEARTBEAT_SECONDS}

    def agent_identity(self, credential):
        if not isinstance(credential, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", credential):
            raise APIError(401, "探针凭据无效。")
        hashed = digest(credential)
        with self.lock:
            for role, table in (("vps", "hosts"), ("probe", "nodes")):
                row = self.db.execute("SELECT * FROM " + table + " WHERE credential_hash=?", (hashed,)).fetchone()
                if row:
                    return role, row["id"], hashed
        raise APIError(401, "探针凭据已撤销或无效。")

    def _expire_jobs(self, db):
        now = self.clock()
        db.execute("UPDATE jobs SET state='expired' WHERE state IN ('queued','leased') AND expires<=?", (now,))
        db.execute("DELETE FROM jobs WHERE created<? AND state NOT IN ('queued','leased')", (now - 86400,))

    def queue_host(self, object_id, automatic=False):
        with self.transaction() as db:
            self._expire_jobs(db)
            host = dict(self._found(db, "hosts", object_id))
            if not host["enabled"] or not host["credential_hash"]:
                if automatic:
                    return {"count": 0, "queued": True}
                raise APIError(409, "VPS 必须启用并完成探针安装。")
            eligible = db.execute("SELECT n.* FROM nodes n JOIN associations a ON n.id=a.node_id WHERE a.host_id=? AND n.enabled=1 AND n.credential_hash IS NOT NULL AND n.last_seen>?", (object_id, self.clock() - OFFLINE_SECONDS)).fetchall()
            if not eligible:
                if automatic:
                    return {"count": 0, "queued": True}
                raise APIError(409, "没有关联的在线测试节点。")
            if all(db.execute("SELECT 1 FROM jobs WHERE host_id=? AND node_id=? AND state IN ('queued','leased')",
                              (object_id, node["id"])).fetchone() for node in eligible):
                return {"count": 0, "queued": True}
        try:
            target = resolve_target(host["address"], 80, self.allow_private)[0][1][0]
        except TargetError as error:
            raise APIError(400, str(error)) from error
        with self.transaction() as db:
            current = self._found(db, "hosts", object_id)
            if not current["enabled"] or current["revision"] != host["revision"] or not current["credential_hash"]:
                raise APIError(409, "VPS 配置已改变，请重试。")
            self._expire_jobs(db)
            available = 20000 - db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','leased')").fetchone()[0]
            if available <= 0:
                raise APIError(429, "任务队列已达到上限。")
            nodes = db.execute("SELECT n.* FROM nodes n JOIN associations a ON n.id=a.node_id WHERE a.host_id=? AND n.enabled=1 AND n.credential_hash IS NOT NULL AND n.last_seen>?", (object_id, self.clock() - OFFLINE_SECONDS)).fetchall()
            count = 0
            for node in nodes:
                if count >= available:
                    break
                if db.execute("SELECT 1 FROM jobs WHERE host_id=? AND node_id=? AND state IN ('queued','leased')", (object_id, node["id"])).fetchone():
                    continue
                db.execute("INSERT INTO jobs(id,host_id,node_id,target,host_revision,node_name,region,created,expires,state) VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (uuid.uuid4().hex, object_id, node["id"], target, current["revision"], node["name"], node["region"], self.clock(), self.clock() + 180, "queued"))
                count += 1
            return {"count": count, "queued": True}

    def _job_allowed(self, db, job):
        row = db.execute("SELECT h.enabled,h.revision,n.enabled AS node_enabled,n.credential_hash FROM hosts h JOIN associations a ON h.id=a.host_id JOIN nodes n ON n.id=a.node_id WHERE h.id=? AND n.id=?", (job["host_id"], job["node_id"])).fetchone()
        return row is not None and row["enabled"] and row["node_enabled"] and row["revision"] == job["host_revision"] and row["credential_hash"] is not None

    def heartbeat(self, credential, body):
        fields(body, set())
        role, object_id, hashed = self.agent_identity(credential)
        with self.transaction() as db:
            table = "hosts" if role == "vps" else "nodes"
            updated = db.execute("UPDATE " + table + " SET last_seen=? WHERE id=? AND credential_hash=?", (self.clock(), object_id, hashed))
            if not updated.rowcount:
                raise APIError(401, "探针凭据已撤销。")
            self._expire_jobs(db)
            tasks = []
            if role == "probe" and self._found(db, table, object_id)["enabled"]:
                rows = db.execute("SELECT * FROM jobs WHERE node_id=? AND state IN ('leased','queued') ORDER BY CASE state WHEN 'leased' THEN 0 ELSE 1 END,created LIMIT 100", (object_id,)).fetchall()
                for job in rows:
                    if not self._job_allowed(db, job) or (job["credential_hash"] and job["credential_hash"] != hashed):
                        db.execute("UPDATE jobs SET state='cancelled' WHERE id=?", (job["id"],))
                        continue
                    if job["state"] == "queued":
                        db.execute("UPDATE jobs SET state='leased',expires=?,credential_hash=? WHERE id=?", (self.clock() + 60, hashed, job["id"]))
                    tasks.append({"id": job["id"], "hostId": job["host_id"], "target": job["target"], "sent": 5, "timeoutSeconds": 8})
                    if len(tasks) == 10:
                        break
            return {"role": role, "heartbeatSeconds": HEARTBEAT_SECONDS, "tasks": tasks}

    def agent_result(self, credential, body):
        fields(body, {"jobId", "sent", "received", "avgRttMs", "status", "error"}, {"jobId", "sent", "received", "avgRttMs", "status"})
        if not isinstance(body["jobId"], str) or not re.fullmatch(r"[0-9a-f]{32}", body["jobId"]):
            raise APIError(400, "任务编号无效。")
        sent = integer(body["sent"], "发包数量", 0, 5)
        received = integer(body["received"], "收包数量", 0, sent)
        status_value, average = body["status"], body["avgRttMs"]
        if status_value == "OK":
            if not received or type(average) not in (int, float) or not 0 <= average <= 60000 or not math.isfinite(average):
                raise APIError(400, "成功测量需有回复和有效平均延迟。")
            average = float(average)
        elif status_value in ("TIMEOUT", "ERROR"):
            if received != 0 or average is not None or (status_value == "TIMEOUT" and sent == 0):
                raise APIError(400, "失败测量的实际包数或空延迟无效。")
        else:
            raise APIError(400, "测量状态无效。")
        if body.get("error") is not None and not isinstance(body["error"], str):
            raise APIError(400, "错误说明必须为文本。")
        error = text(body.get("error") or "", "错误说明", 1000, required=False) or None
        normalized = json.dumps({"sent": sent, "received": received, "avgRttMs": average, "status": status_value, "error": error}, sort_keys=True, separators=(",", ":"))
        role, object_id, hashed = self.agent_identity(credential)
        if role != "probe":
            raise APIError(403, "VPS 探针不能提交线路测量。")
        with self.transaction() as db:
            if not db.execute("SELECT 1 FROM nodes WHERE id=? AND credential_hash=?", (object_id, hashed)).fetchone():
                raise APIError(401, "探针凭据已撤销。")
            job = db.execute("SELECT * FROM jobs WHERE id=?", (body["jobId"],)).fetchone()
            if job is None:
                raise APIError(404, "任务不存在。")
            if job["node_id"] != object_id or job["credential_hash"] != hashed:
                raise APIError(403, "此任务不属于当前探针。")
            if job["state"] == "completed":
                if not hmac.compare_digest(job["result_json"], normalized):
                    raise APIError(409, "同一任务不能覆盖已有测量。")
                return {"accepted": True, "duplicate": True, "jobId": job["id"]}
            if job["state"] != "leased" or job["expires"] <= self.clock() or not self._job_allowed(db, job):
                raise APIError(409, "任务已过期或关联配置已改变。")
            db.execute("INSERT INTO fleet_results(job_id,host_id,node_id,node_name,region,sent,received,avg_rtt,status,error,checked) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (job["id"], job["host_id"], job["node_id"], job["node_name"], job["region"], sent, received, average, status_value, error, self.clock()))
            db.execute("UPDATE jobs SET state='completed',result_json=? WHERE id=?", (normalized, job["id"]))
            self._prune(db, "fleet_results", "host_id", job["host_id"])
            return {"accepted": True, "duplicate": False, "jobId": job["id"]}

    def asset(self, filename):
        parent = Path(__file__).resolve().parent.parent
        candidates = [self.agent_dir] if self.agent_dir else [parent / "agent", parent / ".agent"]
        for directory in candidates:
            target = directory / filename
            if target.is_symlink() or directory.is_symlink():
                continue
            if target.is_file() and stat.S_ISREG(target.stat().st_mode) and target.stat().st_size <= 1024 * 1024:
                return target.read_bytes()
        raise APIError(503, "探针发布文件尚不可用。")

    def schedule_once(self):
        now = self.clock()
        with self.lock:
            due = [row[0] for row in self.db.execute("SELECT id FROM monitors WHERE enabled=1 AND next_due<=? ORDER BY next_due LIMIT 20", (now,))]
        for object_id in due:
            try:
                self._reserve_monitor(object_id)
            except APIError:
                continue
            try:
                self.pool.submit(self._background_check, object_id)
            except RuntimeError:
                self._release_monitor(object_id)
        with self.transaction() as db:
            self._expire_jobs(db)
            hosts = [row[0] for row in db.execute("SELECT id FROM hosts WHERE enabled=1 AND credential_hash IS NOT NULL AND next_due<=? ORDER BY next_due LIMIT 100", (now,))]
        for object_id in hosts:
            if self.stop.is_set():
                break
            with self.fleet_lock:
                if object_id in self.fleet_running:
                    continue
                if not self.fleet_slots.acquire(blocking=False):
                    break
                self.fleet_running.add(object_id)
            try:
                self.fleet_pool.submit(self._background_fleet, object_id)
            except RuntimeError:
                with self.fleet_lock:
                    self.fleet_running.discard(object_id)
                    self.fleet_slots.release()

    def _background_fleet(self, object_id):
        try:
            if self.stop.is_set():
                return
            with self.transaction() as db:
                self._found(db, "hosts", object_id)
                db.execute("UPDATE hosts SET next_due=? WHERE id=?", (self.clock() + 60, object_id))
            self.queue_host(object_id, automatic=True)
        except APIError:
            pass  # DNS or object changes are not fabricated measurements.
        finally:
            with self.fleet_lock:
                self.fleet_running.discard(object_id)
                self.fleet_slots.release()

    def _background_check(self, object_id):
        try:
            self.check_monitor(object_id, reserved=True)
        except APIError:
            pass

    def _schedule_loop(self):
        while not self.stop.wait(1):
            try:
                self.schedule_once()
            except Exception:
                print("Background scheduling failed; retrying.", file=sys.stderr, flush=True)


def authority(value):
    if not isinstance(value, str) or not value or len(value) > 300 or any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise APIError(400, "Host 无效。")
    try:
        parsed = urlsplit("//" + value)
        if not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.path or parsed.query or parsed.fragment:
            raise ValueError()
        hostname = host_name(parsed.hostname)
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError()
    except ValueError as error:
        raise APIError(400, "Host 无效。") from error
    return hostname, port


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "xiaowork-watch"
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, unused_format, *unused_args):
        pass  # URLs, passwords and enrollment/session tokens never enter access logs.

    @property
    def app(self):
        return self.server.app

    def _scheme(self):
        try:
            trusted = ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            trusted = False
        return "https" if trusted and self.headers.get("X-Forwarded-Proto") == "https" else "http"

    def _client_ip(self):
        address = ipaddress.ip_address(self.client_address[0])
        forwarded = self.headers.get("X-Real-IP") if address.is_loopback else None
        if forwarded:
            try:
                if "%" in forwarded:
                    raise ValueError()
                address = ipaddress.ip_address(forwarded)
            except ValueError as error:
                raise APIError(400, "代理客户端 IP 无效。") from error
        return str(address)

    def _base(self):
        hostname, port = authority(self.headers.get("Host"))
        host = "[" + hostname + "]" if ":" in hostname else hostname
        if port is not None and port != (443 if self._scheme() == "https" else 80):
            host += ":" + str(port)
        return self._scheme() + "://" + host

    def _origin(self):
        origin = self.headers.get("Origin")
        if not origin or origin != self._base():
            raise APIError(403, "写请求 Origin 必须与当前网站一致。")

    def _secure_transport(self):
        if self._scheme() == "https":
            return True
        hostname, unused_port = authority(self.headers.get("Host"))
        try:
            local_host = hostname == "localhost" or ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            local_host = hostname == "localhost"
        if self.app.allow_private and local_host and ipaddress.ip_address(self.client_address[0]).is_loopback:
            return True
        return False

    def _secure_write(self):
        if not self._secure_transport():
            raise APIError(403, "请先在服务器菜单8配置HTTPS，再登录后台。")

    def _cookie(self):
        try:
            jar = http.cookies.SimpleCookie()
            jar.load(self.headers.get("Cookie", ""))
            return jar[COOKIE].value if COOKIE in jar else None
        except http.cookies.CookieError:
            return None

    def _admin(self):
        if self.headers.get("Authorization"):
            raise APIError(401, "管理操作需要管理员浏览器会话。")
        token = self._cookie()
        session = self.app.session(token)
        if not session:
            raise APIError(401, "管理员会话已失效，请重新登录。")
        self._origin()
        if not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), session["csrfToken"]):
            raise APIError(403, "CSRF 校验失败。")
        return token

    def _credential(self):
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            raise APIError(401, "需要探针凭据。")
        return header[7:]

    def _body(self):
        if hasattr(self, "_json_body"):
            return self._json_body
        if len(self.headers.get_all("Content-Length", [])) > 1:
            raise APIError(400, "安全相关请求头不能重复。")
        if self.headers.get("Transfer-Encoding"):
            raise APIError(400, "不支持分块请求体。")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise APIError(400, "请求长度无效。") from error
        if not 0 <= length <= 65536:
            raise APIError(413, "请求内容过大。")
        if not length:
            self._json_body = {}
            return self._json_body
        if self.headers.get_content_type() != "application/json":
            raise APIError(415, "请求需要 application/json。")
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate key")
                result[key] = value
            return result
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"), object_pairs_hook=unique,
                               parse_constant=lambda unused: (_ for _ in ()).throw(ValueError("Nonfinite number")))
        except (ValueError, UnicodeError, RecursionError) as error:
            raise APIError(400, "JSON 内容无效。") from error
        if not isinstance(value, dict):
            raise APIError(400, "JSON 请求需为对象。")
        self._json_body = value
        return value

    def _respond(self, status, data=None, message="", headers=None, raw=None, content_type=None):
        payload = raw if raw is not None else json.dumps({"code": 0 if status < 400 else status, "message": message, "data": data}, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type or "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)
        self.close_connection = True

    def _pagination(self, query):
        try:
            parsed = parse_qs(query, max_num_fields=10)
            page = int(parsed.get("page", ["1"])[0])
            size = int(parsed.get("size", ["8"])[0])
        except ValueError as error:
            raise APIError(400, "分页参数无效。") from error
        return integer(page, "页码", 1, 1000000), integer(size, "分页大小", 1, 100)

    def _route(self):
        for header in ("Host", "Origin", "X-Forwarded-Proto", "X-Real-IP", "Authorization", "Cookie", "Content-Length", "X-CSRF-Token"):
            if len(self.headers.get_all(header, [])) > 1:
                raise APIError(400, "安全相关请求头不能重复。")
        try:
            parsed = urlsplit(self.path)
        except ValueError as error:
            raise APIError(400, "请求路径无效。") from error
        path, method = parsed.path.rstrip("/") or "/", self.command
        if method == "HEAD":
            method = "GET"
        if method not in ("GET", "OPTIONS") and path.startswith("/api/"):
            self._secure_write()
        if path == "/api/health" and method == "GET":
            return self._respond(200, {"status": "ok", "release": os.environ.get("WATCH_RELEASE", "development"), "version": VERSION})
        if path.startswith("/agent/") and method == "GET":
            filename = path[7:]
            if filename not in ("install.sh", "agent.py", "agent.py.sha256"):
                raise APIError(404, "文件不存在。")
            data = self.app.asset("agent.py" if filename.endswith("sha256") else filename)
            expected = hashlib.sha256(data).hexdigest()
            try:
                query = parse_qs(parsed.query, max_num_fields=5)
            except ValueError as error:
                raise APIError(400, "查询参数过多。") from error
            if "sha" in query and query["sha"] != [expected]:
                raise APIError(409, "探针版本已改变，请重新生成安装命令。")
            if filename.endswith("sha256"):
                data = (expected + "  agent.py\n").encode("ascii")
            return self._respond(200, raw=data, content_type="text/plain; charset=utf-8")
        if path == "/api/auth/session" and method == "GET":
            session = self.app.session(self._cookie()) if self._secure_transport() else None
            return self._respond(200, session or {"authenticated": False, "username": None, "csrfToken": None})
        if path == "/api/auth/login" and method == "POST":
            self._origin()
            if self.headers.get("Authorization"):
                raise APIError(401, "探针凭据不能登录管理员。")
            session, token = self.app.login(self._body(), self._client_ip())
            cookie = COOKIE + "=" + token + "; Path=/; HttpOnly; SameSite=Strict; Max-Age=" + str(SESSION_SECONDS)
            if self._scheme() == "https":
                cookie += "; Secure"
            return self._respond(200, session, headers={"Set-Cookie": cookie})
        if path == "/api/auth/logout" and method == "POST":
            self.app.logout(self._admin())
            cookie = COOKIE + "=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0" + ("; Secure" if self._scheme() == "https" else "")
            return self._respond(200, {"authenticated": False, "username": None, "csrfToken": None}, headers={"Set-Cookie": cookie})
        if path == "/api/agent/enroll" and method == "POST":
            return self._respond(200, self.app.enroll(self._body()))
        if path == "/api/agent/heartbeat" and method == "POST":
            return self._respond(200, self.app.heartbeat(self._credential(), self._body()))
        if path == "/api/agent/results" and method == "POST":
            return self._respond(200, self.app.agent_result(self._credential(), self._body()))
        if path == "/api/fleet" and method == "GET":
            return self._respond(200, self.app.fleet())
        match = re.fullmatch(r"/api/(monitors|vps|probes)(?:/([1-9][0-9]{0,9}))?(?:/(enabled|check|checks|enrollment))?", path)
        if not match:
            raise APIError(404, "接口不存在。")
        kind, object_value, action = match.groups()
        object_id = int(object_value) if object_value else None
        table = {"monitors": "monitors", "vps": "hosts", "probes": "nodes"}[kind]
        if action and object_id is None:
            raise APIError(404, "接口不存在。")
        if method == "GET":
            if action == "checks" and kind in ("monitors", "vps"):
                page, size = self._pagination(parsed.query)
                return self._respond(200, self.app.history(kind, object_id, page, size))
            if action is None:
                private = self._secure_transport() and bool(self.app.session(self._cookie())) and not self.headers.get("Authorization")
                data = self.app.monitors(object_id, private=private) if kind == "monitors" else self.app.fleet_items(table, object_id)
                return self._respond(200, data)
            raise APIError(405, "不支持此请求方法。")
        self._admin()
        if action == "enabled" and method == "PATCH":
            return self._respond(200, self.app.enable(table, object_id, self._body()))
        if action == "check" and method == "POST" and kind == "monitors":
            return self._respond(200, self.app.check_monitor(object_id))
        if action == "checks" and method == "POST" and kind == "vps":
            return self._respond(202, self.app.queue_host(object_id))
        if action == "enrollment" and method == "POST" and kind in ("vps", "probes"):
            base = self._base()
            hostname, unused_port = authority(self.headers.get("Host"))
            try:
                local = hostname == "localhost" or ipaddress.ip_address(hostname).is_loopback
            except ValueError:
                local = hostname == "localhost"
            if not base.startswith("https:") and not (self.app.allow_private and local):
                raise APIError(400, "探针安装需要 HTTPS；仅明确开发模式允许 localhost HTTP。")
            return self._respond(200, self.app.enrollment("vps" if kind == "vps" else "probe", object_id, base))
        if action is None and method == "POST" and object_id is None:
            data = self.app.save_monitor(self._body()) if kind == "monitors" else self.app.save_fleet(table, self._body())
            return self._respond(201, data)
        if action is None and method == "PUT" and object_id is not None:
            data = self.app.save_monitor(self._body(), object_id) if kind == "monitors" else self.app.save_fleet(table, self._body(), object_id)
            return self._respond(200, data)
        if action is None and method == "DELETE" and object_id is not None:
            return self._respond(200, self.app.remove(table, object_id))
        raise APIError(405, "不支持此请求方法。")

    def _handle(self):
        try:
            # Consume bounded JSON before an early authorization error; closing a
            # socket with unread request bytes can discard the response as a RST.
            if self.command in ("POST", "PUT", "PATCH", "DELETE"):
                self._body()
            self._route()
        except APIError as error:
            self._respond(error.status, message=str(error))
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception:
            print("Backend request failed.", file=sys.stderr, flush=True)
            self._respond(500, message="主控暂时无法完成请求。")

    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _handle


class ControlServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, address, app):
        self.app = app
        self.request_slots = threading.BoundedSemaphore(32)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.request_slots.acquire(blocking=False):
            try:
                payload = b'{"code":503,"message":"Server busy","data":null}'
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nContent-Type: application/json\r\nContent-Length: " + str(len(payload)).encode() + b"\r\n\r\n" + payload)
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.request_slots.release()


def main(arguments=None):
    parser = argparse.ArgumentParser(description="xiaowork Watch persistent control service")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8091)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--init-admin", action="store_true")
    actions.add_argument("--reset-admin", action="store_true")
    parser.add_argument("--allow-private-targets", action="store_true", help="Explicit local development/testing only")
    args = parser.parse_args(arguments)
    if not 1 <= args.port <= 65535:
        parser.error("port must be 1–65535")
    os.umask(0o077)
    app = Application(args.data_dir, allow_private=args.allow_private_targets, start_scheduler=not (args.init_admin or args.reset_admin))
    if args.init_admin or args.reset_admin:
        try:
            password = app.init_admin(reset=args.reset_admin)
            if password is None:
                print("Administrator already exists; password was not changed.")
            else:
                print("Username: admin\nPassword: " + password + "\nKeep this password securely; it will not be printed again.")
        finally:
            app.close()
        return 0
    server = ControlServer((args.host, args.port), app)
    print("xiaowork Watch control listening on " + args.host + ":" + str(args.port), flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
