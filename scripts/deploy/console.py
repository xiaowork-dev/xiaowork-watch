#!/usr/bin/env python3
"""Chinese terminal operations for the managed static frontend (Python 3.8+)."""
import contextlib
from dataclasses import dataclass
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import secrets
from urllib.parse import urlsplit

OWNER = "# Managed by xiaowork Watch's frontend installer."
ROOT_PREFIX = "# xiaowork-watch-root: "
MARKER_VALUE = "xiaowork-watch-managed-v1"


class ConsoleError(ValueError):
    pass


@dataclass(frozen=True)
class Paths:
    nginx_site: Path = Path("/etc/nginx/conf.d/xiaowork-watch.conf")
    nginx_proxy: Path = Path("/etc/nginx/conf.d/xiaowork-watch-proxy.conf")
    wrapper: Path = Path("/usr/local/bin/xiaowork-watch")
    service: Path = Path("/etc/systemd/system/xiaowork-watch-update.service")
    timer: Path = Path("/etc/systemd/system/xiaowork-watch-update.timer")


DEFAULT_PATHS = Paths()
OLD_SERVICE = """[Unit]
Description=Check GitHub for xiaowork Watch frontend updates
Wants=network-online.target
After=network-online.target nginx.service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/xiaowork-watch update --automatic
TimeoutStartSec=300"""
OLD_TIMER = """[Unit]
Description=Automatically update xiaowork Watch frontend from GitHub

[Timer]
OnBootSec=5min
OnUnitInactiveSec=15min
RandomizedDelaySec=60
Persistent=true

[Install]
WantedBy=timers.target"""


def _run(arguments):
    try:
        result = subprocess.run(arguments, check=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, universal_newlines=True, timeout=30)
        return result.stdout + result.stderr
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or str(error)).strip()
        raise ConsoleError("命令失败：" + " ".join(arguments) + "\n" + detail) from error
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ConsoleError("无法完成命令：" + " ".join(arguments) + "\n" + str(error)) from error


def _read_regular(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
        raise ConsoleError("拒绝操作非普通文件或符号链接：" + str(path))
    if path.stat().st_size > 1024 * 1024:
        raise ConsoleError("配置文件过大：" + str(path))
    return path.read_text(encoding="utf-8")


def _write(path, text, mode=0o644):
    path = Path(path)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ConsoleError("拒绝替换非普通文件：" + str(path))
    fd, temporary = tempfile.mkstemp(prefix=".xiaowork-watch-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _write_config(manager, config):
    _write(manager.root / "config.json", json.dumps(config, ensure_ascii=False, indent=2) + "\n", 0o600)


@contextlib.contextmanager
def _default_locked(path):
    if os.name != "posix":
        raise ConsoleError("运维修改需要 Linux 文件锁。")
    import fcntl
    if Path(path).is_symlink():
        raise ConsoleError("锁文件不能是符号链接。")
    with open(str(path), "a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _lock(manager, locked):
    return (locked or _default_locked)(manager.root / ".deploy.lock")


def _root_text(manager):
    value = str(manager.root)
    if "\n" in value or "\r" in value:
        raise ConsoleError("安装路径含有无效字符。")
    return value


def _header(manager):
    return OWNER + "\n" + ROOT_PREFIX + _root_text(manager) + "\n"


def _domain(value):
    if not isinstance(value, str) or len(value) > 253 or not value:
        raise ConsoleError("请输入有效域名，例如 watch.example.com。")
    if not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part)
               for part in value.split(".")):
        raise ConsoleError("域名不能包含协议、端口、路径、空格或特殊字符。")
    return value


def _owned(path, manager, kind, paths=DEFAULT_PATHS):
    """Only an exact root marker or a strict v0.2.1 template proves ownership."""
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return False
    text = _read_regular(path).replace("\r\n", "\n")
    lines = text.splitlines()
    root_lines = [line for line in lines if line.startswith(ROOT_PREFIX)]
    if root_lines:
        return lines.count(OWNER) == 1 and root_lines == [ROOT_PREFIX + _root_text(manager)]
    root = _root_text(manager)
    if kind == "site":
        return bool(OWNER in lines
                and re.search(r"^\s*root\s+" + re.escape(root + "/current") + r";\s*$", text, re.M)
                and re.search(r"^\s*alias\s+" + re.escape(root + "/shared/assets/") + r";\s*$", text, re.M))
    if kind == "wrapper":
        return any(text.strip() == '#!/bin/sh\nexec /usr/bin/python3 "' + root + target + '" --root "' + root + '" "$@"'
                   for target in ("/current/.deploy/manage.py", "/control/manage.py"))
    if kind in ("service", "timer"):
        expected = OLD_SERVICE if kind == "service" else OLD_TIMER
        return text.strip() == expected and _owned(paths.wrapper, manager, "wrapper", paths)
    return False


def _require_owned(path, manager, kind, paths):
    if (Path(path).exists() or Path(path).is_symlink()) and not _owned(path, manager, kind, paths):
        raise ConsoleError("该文件不属于此安装，已保留：" + str(path))


def set_auto_update(manager, enabled, locked=None):
    if type(enabled) is not bool:
        raise ConsoleError("自动更新状态必须为布尔值。")
    with _lock(manager, locked):
        config = manager._config()
        config["autoUpdate"] = enabled
        _write_config(manager, config)
    return {"autoUpdate": enabled}


def _control_target(manager):
    link = manager.root / "control"
    if not link.is_symlink():
        if link.exists():
            raise ConsoleError("管理入口 control 不是自有符号链接，已保留。")
        return None
    target = link.resolve()
    if (target.name != ".deploy" or target.parent.parent != manager.root / "releases"
            or not re.fullmatch(r"[0-9a-f]{40}", target.parent.name) or not target.is_dir()):
        raise ConsoleError("管理入口 control 指向安装目录外或无效版本。")
    for name in ("manage.py", "console.py"):
        if not _read_regular(target / name):
            raise ConsoleError("管理入口缺少 " + name)
    return target


def ensure_control_entry(manager, paths=DEFAULT_PATHS, locked=None, refresh=False):
    """Keep management code independent of the website's current/rollback link."""
    with _lock(manager, locked):
        existing = _control_target(manager)
        _require_owned(paths.wrapper, manager, "wrapper", paths)
        chosen = existing
        if existing is None or refresh:
            sha = manager._pointed_sha("current")
            candidate = manager.root / "releases" / sha / ".deploy" if sha else None
            if (candidate is not None and candidate.is_dir() and not candidate.is_symlink()
                    and not (candidate / "console.py").is_symlink()
                    and (candidate / "console.py").is_file()):
                for name in ("manage.py", "console.py"):
                    if not _read_regular(candidate / name):
                        raise ConsoleError("当前版本缺少管理文件 " + name)
                chosen = candidate
        if chosen is None:
            return False
        previous_wrapper = _read_regular(paths.wrapper) if paths.wrapper.exists() else None
        previous_mode = stat.S_IMODE(paths.wrapper.stat().st_mode) if previous_wrapper is not None else None
        control = manager.root / "control"
        temporary = manager.root / (".control-" + secrets.token_hex(8))
        switched = False
        try:
            if chosen != existing:
                temporary.symlink_to(chosen.relative_to(manager.root), target_is_directory=True)
                os.replace(str(temporary), str(control))
                switched = True
            if previous_wrapper is not None:
                root = _root_text(manager)
                wrapper = ('#!/bin/sh\n' + _header(manager) + 'exec /usr/bin/python3 "' + root
                           + '/control/manage.py" --root "' + root + '" "$@"\n')
                if previous_wrapper != wrapper:
                    _write(paths.wrapper, wrapper, 0o755)
        except BaseException:
            if switched:
                if existing is None:
                    control.unlink()
                else:
                    temporary.symlink_to(existing.relative_to(manager.root), target_is_directory=True)
                    os.replace(str(temporary), str(control))
            if previous_wrapper is not None:
                _write(paths.wrapper, previous_wrapper, previous_mode)
            raise
        finally:
            if temporary.is_symlink():
                temporary.unlink()
        return True


def configure_proxy(manager, domain, paths=DEFAULT_PATHS, locked=None):
    domain = _domain(domain)
    with _lock(manager, locked):
        config = manager._config()
        endpoint = urlsplit(config.get("healthUrl", ""))
        try:
            port = endpoint.port or 80
        except ValueError as error:
            raise ConsoleError("安装配置中的本地端口无效。") from error
        if (endpoint.scheme != "http" or endpoint.hostname not in ("127.0.0.1", "localhost", "::1")
                or endpoint.username or endpoint.password or not 1 <= port <= 65535):
            raise ConsoleError("反代要求 healthUrl 使用本地 HTTP 端口。")
        if port == 80:
            raise ConsoleError("网站已监听 80 端口，无需新增 80 端口反代；请直接配置原站点域名。")
        upstream_host = config.get("healthHost", "")
        upstream_host = _domain(upstream_host) if upstream_host else "127.0.0.1"
        # The site's managed config must still belong to this installation.
        if not _owned(paths.nginx_site, manager, "site", paths):
            raise ConsoleError("未找到属于此安装的 Nginx 网站配置。")
        _require_owned(paths.nginx_proxy, manager, "proxy", paths)
        old = _read_regular(paths.nginx_proxy) if paths.nginx_proxy.exists() else None
        old_mode = stat.S_IMODE(paths.nginx_proxy.stat().st_mode) if old is not None else 0o644
        conf = (_header(manager) + "server {\n    listen 80;\n    server_name " + domain + ";\n"
                "    location / {\n        proxy_pass http://127.0.0.1:" + str(port) + ";\n"
                "        proxy_set_header Host " + upstream_host + ";\n"
                "        proxy_set_header X-Real-IP $remote_addr;\n"
                "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
                "        proxy_set_header X-Forwarded-Proto $scheme;\n    }\n}\n")
        _write(paths.nginx_proxy, conf)
        try:
            check = _run(["nginx", "-t"])
            if "conflicting server name" in check.lower():
                raise ConsoleError("Nginx 报告域名冲突；请先检查已有站点的域名配置。")
            _run(["systemctl", "reload", "nginx"])
            config["proxyDomain"] = domain
            _write_config(manager, config)
        except BaseException:
            if old is None:
                paths.nginx_proxy.unlink()
            else:
                _write(paths.nginx_proxy, old, old_mode)
            try:
                _run(["nginx", "-t"])
                _run(["systemctl", "reload", "nginx"])
            except ConsoleError:
                pass
            raise
    return {"domain": domain, "url": "http://" + domain + "/", "https": False}


def _internal_uninstall_targets(manager):
    # Verify all paths before disabling anything. Root and download data remain.
    for name in ("current", "previous"):
        manager._pointed_sha(name)
    _control_target(manager)
    targets = []
    for name in ("installed.json", ".installation-complete"):
        path = manager.root / name
        if not path.exists() and not path.is_symlink():
            continue
        text = _read_regular(path)
        if name == "installed.json":
            try:
                metadata = json.loads(text)
            except ValueError as error:
                raise ConsoleError("安装记录无效，拒绝卸载。") from error
            if not isinstance(metadata, dict) or metadata.get("kind") != "frontend-prototype":
                raise ConsoleError("安装记录不属于前端原型，已保留。")
        elif text.strip() != MARKER_VALUE:
            raise ConsoleError("安装完成标记不属于此安装，已保留。")
        targets.append(path)
    for name in ("current", "previous", "control"):
        path = manager.root / name
        if path.is_symlink():
            targets.append(path)
    return targets


def uninstall(manager, confirm=False, paths=DEFAULT_PATHS, locked=None):
    if confirm is not True:
        return {"status": "cancelled", "dataRetained": True}
    with _lock(manager, locked):
        global_paths = [(paths.nginx_site, "site"), (paths.nginx_proxy, "proxy"),
                        (paths.wrapper, "wrapper"), (paths.service, "service"), (paths.timer, "timer")]
        for path, kind in global_paths:
            _require_owned(path, manager, kind, paths)
        internal = _internal_uninstall_targets(manager)
        snapshots = {path: (_read_regular(path), stat.S_IMODE(path.stat().st_mode))
                     for path, _ in global_paths if path.exists()}
        config = manager._config()
        config["autoUpdate"] = False
        _write_config(manager, config)
        if paths.timer in snapshots:
            _run(["systemctl", "disable", "--now", "xiaowork-watch-update.timer"])
        if paths.service in snapshots:
            _run(["systemctl", "stop", "xiaowork-watch-update.service"])
        removed = []
        try:
            for path in (paths.nginx_site, paths.nginx_proxy):
                if path in snapshots:
                    path.unlink()
                    removed.append(path)
            if removed:
                _run(["nginx", "-t"])
                _run(["systemctl", "reload", "nginx"])
            for path in (paths.service, paths.timer, paths.wrapper):
                if path in snapshots:
                    path.unlink()
                    removed.append(path)
            if paths.service in snapshots or paths.timer in snapshots:
                _run(["systemctl", "daemon-reload"])
        except BaseException:
            for path in removed:
                _write(path, snapshots[path][0], snapshots[path][1])
            for command in (["systemctl", "daemon-reload"], ["nginx", "-t"], ["systemctl", "reload", "nginx"]):
                try:
                    _run(command)
                except ConsoleError:
                    pass
            raise
        internal.sort(key=lambda path: 0 if path.name in ("current", "previous", "control") else 1)
        for path in internal:
            path.unlink()
    return {"status": "uninstalled", "dataRetained": True, "root": str(manager.root)}


def _open_terminal():
    try:
        return open("/dev/tty", "r+", encoding="utf-8", buffering=1)
    except io.UnsupportedOperation:
        # Some Python builds reject BufferedRandom on a non-seekable tty.
        raw = open("/dev/tty", "r+b", buffering=0)
        return io.TextIOWrapper(raw, encoding="utf-8", write_through=True)


def _say(terminal, text):
    terminal.write(text + "\n")
    terminal.flush()


def _ask(terminal, text):
    terminal.write(text)
    terminal.flush()
    line = terminal.readline()
    return line.strip() if line else None


def _status_text(manager):
    status, config = manager.status(), manager._config()
    def version(sha):
        if not sha:
            return "无"
        label = sha[:12]
        metadata = manager.root / "releases" / sha / "release.json"
        if metadata.is_file() and not metadata.is_symlink():
            value = json.loads(_read_regular(metadata))
            if isinstance(value, dict) and isinstance(value.get("version"), str):
                label = value["version"] + "（" + label + "）"
        return label
    return ("当前版本：" + version(status.get("current"))
            + "\n可回退版本：" + version(status.get("previous"))
            + "\n自动更新：" + ("开启" if status.get("autoUpdate", True) else "暂停")
            + "\n反代域名：" + str(config.get("proxyDomain") or "未配置"))


def run_menu(manager, locked, paths=DEFAULT_PATHS):
    try:
        terminal = _open_terminal()
    except (OSError, ValueError) as error:
        print("未找到交互终端；请在 SSH 终端运行 sudo xiaowork-watch menu。未执行安装或删除。\n" + str(error), file=sys.stderr)
        return 1
    with terminal:
        while True:
            _say(terminal, "\n========== xiaowork Watch ==========\n服务器管理\n\n  1. 查看状态\n  2. 配置域名 HTTP 反代\n  3. 更新网站\n  4. 回退上一版本\n  5. 自动更新开关\n  6. 查看更新日志\n  7. 卸载网站入口（保留下载数据）\n  0. 退出\n")
            choice = _ask(terminal, "请选择：")
            if choice in (None, "0"):
                return 0
            try:
                if choice == "1":
                    _say(terminal, _status_text(manager))
                elif choice == "2":
                    domain = _ask(terminal, "请输入域名（留空取消）：")
                    if domain:
                        result = configure_proxy(manager, domain, paths=paths, locked=locked)
                        _say(terminal, "HTTP 反代已配置：" + result["url"] + "\n请把域名 DNS 指向此服务器并开放 80 端口。本功能不自动申请 HTTPS 证书。")
                elif choice == "3":
                    with _lock(manager, locked):
                        result = manager.install_or_update()
                    ensure_control_entry(manager, paths=paths, locked=locked, refresh=True)
                    _say(terminal, "更新完成。\n" + _status_text(manager))
                elif choice == "4":
                    if _ask(terminal, "回退会暂停自动更新。输入 ROLLBACK 确认：") == "ROLLBACK":
                        with _lock(manager, locked):
                            result = manager.rollback()
                        _say(terminal, "回退完成；自动更新已暂停。\n" + _status_text(manager))
                    else:
                        _say(terminal, "已取消回退。")
                elif choice == "5":
                    value = _ask(terminal, "输入 1 开启、0 关闭自动更新（留空取消）：")
                    if value in ("0", "1"):
                        set_auto_update(manager, value == "1", locked=locked)
                        _say(terminal, "自动更新已" + ("开启。" if value == "1" else "关闭。"))
                    elif value:
                        _say(terminal, "请输入 1 或 0。")
                elif choice == "6":
                    _require_owned(paths.service, manager, "service", paths)
                    _say(terminal, _run(["journalctl", "-u", "xiaowork-watch-update.service", "--no-pager", "-n", "50"]))
                elif choice == "7":
                    first = _ask(terminal, "卸载会停止更新并移除网站/命令入口，保留发布包和共享资源。输入 7 继续：")
                    if first == "7" and _ask(terminal, "再次确认：请输入 UNINSTALL：") == "UNINSTALL":
                        uninstall(manager, confirm=True, paths=paths, locked=locked)
                        _say(terminal, "卸载完成，下载数据与安装配置已保留。可重新执行安装链接。")
                        return 0
                    _say(terminal, "已取消卸载。")
                else:
                    _say(terminal, "请选择菜单中的编号。")
            except Exception as error:
                _say(terminal, "操作失败：" + str(error) + "\n可修正后重试，或输入 0 退出。")
