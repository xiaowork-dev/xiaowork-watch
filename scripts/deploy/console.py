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
import shutil
import http.client
import socket
import ssl
import time
from urllib.parse import urlsplit

OWNER = "# Managed by xiaowork Watch's frontend installer."
ROOT_PREFIX = "# xiaowork-watch-root: "
MARKER_VALUE = "xiaowork-watch-managed-v1"
TLS_NAME = "xiaowork-watch"
TLS_SERVICE = "xiaowork-watch-certbot-renew.service"
TLS_TIMER = "xiaowork-watch-certbot-renew.timer"
ACME_SERVER = "https://acme-v02.api.letsencrypt.org/directory"
HTTPS_PORT = 443
CERTBOT_PYTHON = "/usr/bin/python3"


class ConsoleError(ValueError):
    pass


@dataclass(frozen=True)
class Paths:
    nginx_site: Path = Path("/etc/nginx/conf.d/xiaowork-watch.conf")
    nginx_proxy: Path = Path("/etc/nginx/conf.d/xiaowork-watch-proxy.conf")
    wrapper: Path = Path("/usr/local/bin/xiaowork-watch")
    service: Path = Path("/etc/systemd/system/xiaowork-watch-update.service")
    timer: Path = Path("/etc/systemd/system/xiaowork-watch-update.timer")
    tls_service: Path = Path("/etc/systemd/system/" + TLS_SERVICE)
    tls_timer: Path = Path("/etc/systemd/system/" + TLS_TIMER)


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


def _run(arguments, timeout=30, display_arguments=None):
    display = display_arguments or arguments
    try:
        if timeout > 30:
            print("正在执行：" + " ".join(display[:3]) + "，请稍候……", flush=True)
        environment = os.environ.copy()
        environment["DEBIAN_FRONTEND"] = "noninteractive"
        result = subprocess.run(arguments, check=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                universal_newlines=True, timeout=timeout, env=environment)
        return result.stdout + result.stderr
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or str(error)).strip()
        raise ConsoleError("命令失败：" + " ".join(display) + "\n" + detail) from error
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ConsoleError("无法完成命令：" + " ".join(display) + "\n" + str(error)) from error


def _read_regular(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
        raise ConsoleError("拒绝操作非普通文件或符号链接：" + str(path))
    if path.stat().st_size > 1024 * 1024:
        raise ConsoleError("配置文件过大：" + str(path))
    with path.open("r", encoding="utf-8", newline="") as source:
        return source.read()


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
    path = Path(path)
    parent = path.parent
    root_stat = parent.lstat()
    if not stat.S_ISDIR(root_stat.st_mode) or parent.resolve() != parent:
        raise ConsoleError("安装目录不能是符号链接或含有链接路径。")
    if path.is_symlink():
        raise ConsoleError("锁文件不能是符号链接。")
    descriptor = os.open(str(path), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("正在等待正在运行的部署或证书操作完成……", flush=True)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            try:
                current_root, current_lock, opened_lock = parent.lstat(), path.lstat(), os.fstat(handle.fileno())
                valid = (stat.S_ISDIR(current_root.st_mode) and parent.resolve() == parent
                         and (root_stat.st_dev, root_stat.st_ino) == (current_root.st_dev, current_root.st_ino)
                         and stat.S_ISREG(current_lock.st_mode)
                         and (current_lock.st_dev, current_lock.st_ino) == (opened_lock.st_dev, opened_lock.st_ino)
                         and _read_regular(parent / ".xiaowork-watch-managed").strip() == MARKER_VALUE)
            except (OSError, ConsoleError):
                valid = False
            if not valid:
                raise ConsoleError("安装目录或部署锁已改变，请重新执行管理命令。")
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
        prior_proxy = _read_regular(paths.nginx_proxy) if paths.nginx_proxy.exists() or paths.nginx_proxy.is_symlink() else ""
        if config.get("httpsEnabled") or re.search(r"\bssl_certificate\b|\blisten\s+443\s+ssl\b", prior_proxy):
            raise ConsoleError("此站点已配置 HTTPS；请使用菜单 8 管理 HTTPS，HTTP 入口不会降级它。")
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


def _tls_domain(value):
    value = _domain(value).lower()
    if "." not in value or re.fullmatch(r"[0-9.]+", value):
        raise ConsoleError("HTTPS 需要公开 DNS 域名，不能使用本地名称或 IP 地址。")
    return value


def _email(value):
    if not isinstance(value, str) or len(value) > 254 or value.count("@") != 1:
        raise ConsoleError("请输入有效的联系邮箱。")
    local, domain = value.split("@")
    if (not 1 <= len(local) <= 64 or not re.fullmatch(r"[A-Za-z0-9._%+-]+", local)
            or local.startswith((".", "-")) or local.endswith(".") or ".." in local):
        raise ConsoleError("联系邮箱含有无效字符。")
    _tls_domain(domain)
    return value


def _nginx_path(path):
    value = str(path)
    if "\r" in value or "\n" in value:
        raise ConsoleError("配置路径含有无效字符。")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _public_directory(path, root):
    parts = path.relative_to(root).parts
    for length in range(len(parts) + 1):
        directory = root.joinpath(*parts[:length])
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise ConsoleError("目录不安全，已保留：" + str(directory))
        directory.mkdir(exist_ok=True)
        os.chmod(str(directory), 0o755)


def _tls_directories(manager, create=False):
    shared = manager.root / "shared"
    names = {"certs": "letsencrypt", "work": "certbot-work", "logs": "certbot-logs", "acme": "acme"}
    folders = {key: shared / name for key, name in names.items()}
    if shared.is_symlink() or (shared.exists() and not shared.is_dir()):
        raise ConsoleError("共享目录不安全。")
    for key in ("certs", "work", "logs"):
        folder = folders[key]
        marker = folder / ".xiaowork-watch-owned"
        if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
            raise ConsoleError("证书目录不安全：" + str(folder))
        if marker.exists() or marker.is_symlink():
            if _read_regular(marker) != _header(manager):
                raise ConsoleError("证书目录属于其他安装，已保留：" + str(folder))
        elif folder.exists() and any(folder.iterdir()):
            raise ConsoleError("证书目录已有未标记的数据，已保留：" + str(folder))
    for directory in (folders["acme"], folders["acme"] / ".well-known", folders["acme"] / ".well-known/acme-challenge"):
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise ConsoleError("ACME 验证目录不安全。")
    if create:
        _public_directory(folders["acme"] / ".well-known/acme-challenge", manager.root)
        for key in ("certs", "work", "logs"):
            folder = folders[key]
            folder.mkdir(exist_ok=True)
            os.chmod(str(folder), 0o700)
            marker = folder / ".xiaowork-watch-owned"
            if not marker.exists():
                _write(marker, _header(manager), 0o600)
    return folders


def _upstream(config):
    endpoint = urlsplit(config.get("healthUrl", ""))
    try:
        port = endpoint.port or 80
    except ValueError as error:
        raise ConsoleError("本地网站端口无效。") from error
    if (endpoint.scheme != "http" or endpoint.hostname not in ("127.0.0.1", "localhost", "::1")
            or endpoint.username or endpoint.password or port in (80, HTTPS_PORT) or not 1 <= port <= 65535):
        raise ConsoleError("HTTPS 反代需要独立的本地 HTTP 网站端口，例如 8088。")
    host = _domain(config["healthHost"]) if config.get("healthHost") else "127.0.0.1"
    return port, host


def _proxy_location(port, host):
    return ("    location / {\n        proxy_pass http://127.0.0.1:" + str(port) + ";\n"
            "        proxy_set_header Host " + host + ";\n"
            "        proxy_set_header X-Real-IP $remote_addr;\n"
            "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
            "        proxy_set_header X-Forwarded-Proto $scheme;\n    }\n")


def _tls_proxy(manager, domain, config, folders, tls_domain=None, http_domains=None):
    port, host = _upstream(config)
    domains = http_domains or domain
    conf = (_header(manager) + "server {\n    listen 80;\n    server_name " + domains + ";\n"
            "    location ^~ /.well-known/acme-challenge/ {\n        root " + _nginx_path(folders["acme"]) + ";\n"
            "        default_type text/plain;\n        try_files $uri =404;\n    }\n")
    if tls_domain:
        conf += "    location / { return 301 https://" + tls_domain + "$request_uri; }\n}\n"
        live = folders["certs"] / "live" / TLS_NAME
        conf += ("server {\n    listen 443 ssl;\n    server_name " + tls_domain + ";\n"
                 "    ssl_certificate " + _nginx_path(live / "fullchain.pem") + ";\n"
                 "    ssl_certificate_key " + _nginx_path(live / "privkey.pem") + ";\n"
                 "    ssl_protocols TLSv1.2 TLSv1.3;\n" + _proxy_location(port, host) + "}\n")
    else:
        conf += _proxy_location(port, host) + "}\n"
    return conf


def _nginx_reload():
    check = _run(["nginx", "-t"])
    if "conflicting server name" in check.lower():
        raise ConsoleError("Nginx 报告域名冲突；已保留原站点配置。")
    _run(["systemctl", "reload", "nginx"])


def _certificate_target(folders, name):
    certs = folders["certs"]
    for directory in (certs / "archive", certs / "archive" / TLS_NAME, certs / "live", certs / "live" / TLS_NAME):
        if directory.is_symlink() or not directory.is_dir():
            raise ConsoleError("证书目录无效或包含目录链接。")
    live = certs / "live" / TLS_NAME / name
    try:
        target = live.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ConsoleError("证书文件不存在：" + name) from error
    prefix = name[:-4]
    if (target.parent != certs / "archive" / TLS_NAME
            or not re.fullmatch(re.escape(prefix) + r"[0-9]+\.pem", target.name)
            or not target.is_file() or not stat.S_ISREG(target.stat().st_mode)
            or not 0 < target.stat().st_size <= 4 * 1024 * 1024):
        raise ConsoleError("证书文件必须位于本安装专属的 archive lineage。")
    return live


def _certificate_snapshot(folders):
    snapshot = {"links": {}, "renewal": None}
    live = folders["certs"] / "live" / TLS_NAME
    if live.exists() or live.is_symlink():
        for name in ("cert.pem", "chain.pem", "fullchain.pem", "privkey.pem"):
            path = live / name
            if path.exists() or path.is_symlink():
                _certificate_target(folders, name)
                snapshot["links"][name] = path.resolve(strict=True)
    renewal = folders["certs"] / "renewal" / (TLS_NAME + ".conf")
    if renewal.parent.is_symlink():
        raise ConsoleError("证书续期配置目录不能是符号链接。")
    if renewal.exists() or renewal.is_symlink():
        snapshot["renewal"] = (_read_regular(renewal), stat.S_IMODE(renewal.stat().st_mode))
    return snapshot


def _restore_certificate_snapshot(folders, snapshot):
    # Preserve new archives/accounts; restore only the prior owned lineage links.
    live = folders["certs"] / "live" / TLS_NAME
    for name, target in snapshot["links"].items():
        temporary = live / (".restore-" + secrets.token_hex(8))
        try:
            temporary.symlink_to(os.path.relpath(str(target), str(live)))
            os.replace(str(temporary), str(live / name))
        finally:
            if temporary.is_symlink():
                temporary.unlink()
    if snapshot["renewal"] is not None:
        renewal = folders["certs"] / "renewal" / (TLS_NAME + ".conf")
        _write(renewal, snapshot["renewal"][0], snapshot["renewal"][1])


def _validate_certificates(folders, domain):
    cert = _certificate_target(folders, "fullchain.pem")
    key = _certificate_target(folders, "privkey.pem")
    matched = _run(["openssl", "x509", "-in", str(cert), "-checkhost", domain, "-noout"])
    if ("Hostname " + domain + " does match certificate") not in matched.splitlines():
        raise ConsoleError("签发的证书不匹配所选域名。")
    _run(["openssl", "x509", "-in", str(cert), "-checkend", "0", "-noout"])
    return cert, key


class _LoopbackHTTPS(http.client.HTTPSConnection):
    def connect(self):
        connection = socket.create_connection(("127.0.0.1", self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(connection, server_hostname=self.host)
        except BaseException:
            connection.close()
            raise


def _https_health(manager, domain):
    sha = manager._pointed_sha("current")
    if not sha:
        raise ConsoleError("当前没有已安装的网站版本。")
    last_error = None
    for attempt in range(3):
        connection = _LoopbackHTTPS(domain, HTTPS_PORT, timeout=10, context=ssl.create_default_context())
        try:
            connection.request("GET", "/release.json?_tls=" + sha,
                               headers={"Host": domain, "Cache-Control": "no-cache"})
            response = connection.getresponse()
            raw = response.read(1024 * 1024 + 1)
            value = json.loads(raw) if len(raw) <= 1024 * 1024 else None
            if (response.status == 200 and isinstance(value, dict) and type(value.get("schema")) is int
                    and value["schema"] == 1 and value.get("commit") == sha and value.get("kind") == "frontend-prototype"):
                return
            last_error = "HTTPS 未返回当前网站版本"
        except Exception as error:
            last_error = str(error)
        finally:
            connection.close()
        if attempt < 2:
            time.sleep(0.5)
    raise ConsoleError("HTTPS 本地证书与版本检查失败：" + str(last_error))


def _certbot_arguments(folders):
    return ["--config", "/dev/null", "--server", ACME_SERVER, "--no-directory-hooks",
            "--pre-hook", "", "--post-hook", "", "--deploy-hook", "",
            "--config-dir", str(folders["certs"]), "--work-dir", str(folders["work"]),
            "--logs-dir", str(folders["logs"])]


def _isolated_certbot_main(arguments):
    """Disable implicit cli.ini sources only in this disposable Certbot process."""
    os.environ.pop("CERTBOT_SNAPPED", None)  # This runner uses the system apt module.
    try:
        from certbot._internal import constants
    except ImportError as error:
        raise ConsoleError("系统 Python 的 Certbot 模块不可用，请通过 apt 安装 certbot。") from error
    defaults = getattr(constants, "CLI_DEFAULTS", None)
    if not isinstance(defaults, dict) or not isinstance(defaults.get("config_files"), (list, tuple)):
        raise ConsoleError("此 Certbot 版本无法隔离默认配置，未开始证书操作。请使用系统 apt 版本。")
    # --config /dev/null alone still loads the default config list. Clear the
    # actual parser defaults before importing the main entry point.
    constants.CLI_DEFAULTS = dict(defaults, config_files=[])
    from certbot._internal import main
    return main.main(arguments)


def _run_certbot(arguments, timeout=600):
    command = [CERTBOT_PYTHON, "-I", str(Path(__file__).resolve()), "--isolated-certbot"] + list(arguments)
    return _run(command, timeout=timeout, display_arguments=["certbot"] + list(arguments))


def _certbot_available():
    try:
        _run_certbot(["--version"], timeout=30)
        return True
    except ConsoleError:
        return False


def _unit_state(name, query):
    property_name = "UnitFileState" if query == "is-enabled" else "ActiveState"
    output = _run(["systemctl", "show", name, "--property=LoadState", "--property=" + property_name])
    properties = {}
    for line in output.splitlines():
        match = re.fullmatch(r"(LoadState|" + property_name + r")=(.*)", line)
        if match:
            if match[1] in properties:
                raise ConsoleError("systemd 返回了重复状态属性：" + name)
            properties[match[1]] = match[2]
    load = properties.get("LoadState")
    if load == "not-found":
        return False
    state = properties.get(property_name)
    allowed = ({"enabled", "enabled-runtime", "disabled", "static", "alias", "indirect", "generated",
                "transient", "linked", "linked-runtime", "masked", "masked-runtime"}
               if query == "is-enabled" else
               {"active", "reloading", "activating", "inactive", "failed", "deactivating", "maintenance"})
    if load not in {"loaded", "masked"} or state not in allowed:
        raise ConsoleError("无法确认 systemd 服务状态：" + name)
    return state in ({"enabled", "enabled-runtime"} if query == "is-enabled" else {"active", "reloading"})


def _tls_units(manager):
    service = (_header(manager) + "[Unit]\nDescription=Renew xiaowork Watch HTTPS certificate\n"
               "Wants=network-online.target\nAfter=network-online.target nginx.service\n\n"
               "[Service]\nType=oneshot\nExecStart=/usr/local/bin/xiaowork-watch renew-https\nTimeoutStartSec=900\n")
    timer = (_header(manager) + "[Unit]\nDescription=Check xiaowork Watch HTTPS certificate daily\n\n"
             "[Timer]\nOnCalendar=daily\nRandomizedDelaySec=1h\nPersistent=true\n\n"
             "[Install]\nWantedBy=timers.target\n")
    return service, timer


def configure_https(manager, domain, email, paths=DEFAULT_PATHS, locked=None):
    domain, email = _tls_domain(domain), _email(email)
    with _lock(manager, locked):
        config = manager._config()
        _upstream(config)
        if not _owned(paths.wrapper, manager, "wrapper", paths):
            raise ConsoleError("未找到此安装的管理命令，无法配置证书续期服务。")
        if not _owned(paths.nginx_site, manager, "site", paths):
            raise ConsoleError("未找到属于此安装的 Nginx 网站配置。")
        for path, kind in ((paths.nginx_proxy, "proxy"), (paths.tls_service, "tls-service"), (paths.tls_timer, "tls-timer")):
            _require_owned(path, manager, kind, paths)
        tracked = (paths.nginx_proxy, paths.tls_service, paths.tls_timer, manager.root / "config.json")
        snapshots = {path: (_read_regular(path), stat.S_IMODE(path.stat().st_mode))
                     for path in tracked if path.exists()}
        old_proxy = snapshots.get(paths.nginx_proxy, ("",))[0]
        was_tls = bool(config.get("httpsEnabled"))
        if re.search(r"\bssl_certificate\b", old_proxy) and not was_tls:
            raise ConsoleError("已有 HTTPS 配置未登记为本工具管理，已保留；请先检查证书归属。")
        folders = _tls_directories(manager, create=True)
        certificate_state = _certificate_snapshot(folders)
        prior_domain = _tls_domain(config.get("tlsDomain") or config.get("proxyDomain")) if was_tls else None
        if prior_domain:
            # Expired old certificates may still be renewed by this operation.
            _certificate_target(folders, "fullchain.pem")
            _certificate_target(folders, "privkey.pem")
        enabled = _unit_state(TLS_TIMER, "is-enabled") if paths.tls_timer in snapshots else False
        active = _unit_state(TLS_TIMER, "is-active") if paths.tls_timer in snapshots else False
        timer_touched = False
        try:
            if not _certbot_available() or not shutil.which("openssl"):
                _run(["apt-get", "update"], timeout=600)
                _run(["apt-get", "install", "-y", "certbot", "openssl"], timeout=600)
                if not _certbot_available() or not shutil.which("openssl"):
                    raise ConsoleError("系统 apt 的 Certbot 或 OpenSSL 安装后仍不可用。")
            print("正在准备 IPv4 HTTP-01 验证；域名 A 记录需指向服务器且不应保留无效 AAAA，已有 HTTPS 会继续提供服务。", flush=True)
            domains = " ".join(dict.fromkeys((prior_domain, domain))) if prior_domain else domain
            _write(paths.nginx_proxy, _tls_proxy(manager, domain, config, folders, tls_domain=prior_domain, http_domains=domains))
            _nginx_reload()
            print("正在向 Let's Encrypt 申请或检查证书，可能需要几分钟。", flush=True)
            arguments = ["certonly"] + _certbot_arguments(folders) + [
                "--cert-name", TLS_NAME, "--keep-until-expiring", "--renew-with-new-domains",
                "--non-interactive", "--agree-tos", "--email", email, "--preferred-challenges", "http",
                "--webroot", "-w", str(folders["acme"]), "-d", domain]
            _run_certbot(arguments, timeout=600)
            _validate_certificates(folders, domain)
            _write(paths.nginx_proxy, _tls_proxy(manager, domain, config, folders, tls_domain=domain))
            _nginx_reload()
            _https_health(manager, domain)
            service, timer = _tls_units(manager)
            _write(paths.tls_service, service)
            _write(paths.tls_timer, timer)
            _run(["systemctl", "daemon-reload"])
            config.update({"proxyDomain": domain, "httpsEnabled": True, "tlsDomain": domain,
                           "tlsEmail": email, "tlsCertName": TLS_NAME})
            _write_config(manager, config)
            timer_touched = True
            _run(["systemctl", "enable", "--now", TLS_TIMER])
        except BaseException:
            if timer_touched:
                try:
                    _run(["systemctl", "disable", "--now", TLS_TIMER])
                except ConsoleError:
                    pass
            _restore_certificate_snapshot(folders, certificate_state)
            for path in tracked:
                if path in snapshots:
                    _write(path, snapshots[path][0], snapshots[path][1])
                elif path.exists():
                    path.unlink()
            for arguments in (["systemctl", "daemon-reload"], ["nginx", "-t"], ["systemctl", "reload", "nginx"]):
                try:
                    _run(arguments)
                except ConsoleError:
                    pass
            if paths.tls_timer in snapshots:
                try:
                    _run(["systemctl", "enable" if enabled else "disable", TLS_TIMER])
                    _run(["systemctl", "start" if active else "stop", TLS_TIMER])
                except ConsoleError:
                    pass
            raise
    return {"domain": domain, "url": "https://" + domain + "/", "https": True}


def renew_https(manager, paths=DEFAULT_PATHS, locked=None):
    with _lock(manager, locked):
        config = manager._config()
        if not config.get("httpsEnabled"):
            raise ConsoleError("此安装尚未配置 HTTPS。")
        domain = _tls_domain(config.get("tlsDomain") or config.get("proxyDomain"))
        for path, kind in ((paths.nginx_proxy, "proxy"), (paths.tls_service, "tls-service"), (paths.tls_timer, "tls-timer")):
            if not _owned(path, manager, kind, paths):
                raise ConsoleError("HTTPS 续期配置不属于此安装：" + str(path))
        folders = _tls_directories(manager)
        _run_certbot(["renew"] + _certbot_arguments(folders) + [
            "--cert-name", TLS_NAME, "--quiet", "--non-interactive", "--no-random-sleep-on-renew"], timeout=600)
        _validate_certificates(folders, domain)
        _nginx_reload()
        _https_health(manager, domain)
    return {"status": "checked", "domain": domain}


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


def _purge_parent_security(root):
    if os.name != "posix":
        return  # Windows is used only for offline fixtures; deployment requires Linux.
    trusted = {0, os.geteuid()}
    for directory in (root,) + tuple(root.parents):
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in trusted:
            raise ConsoleError("安装目录或父路径不属于可信管理员，完整清理已停止：" + str(directory))
        if info.st_mode & 0o022 and not (directory != root and info.st_mode & stat.S_ISVTX and info.st_uid == 0):
            raise ConsoleError("安装目录或父路径允许其他用户写入，完整清理已停止：" + str(directory))


def _purge_root(manager):
    """Prove the root boundary before opening its lock, without scanning a live update."""
    root = Path(manager.root)
    protected = {Path(value) for value in ("/", "/opt", "/etc", "/usr", "/usr/local", "/var", "/var/lib",
                                          "/var/www", "/home", "/root", "/tmp", "/srv", "/bin", "/sbin")}
    if (not root.is_absolute() or len(root.parts) < 3 or root in protected or root == Path.home()
            or root.is_symlink() or not root.is_dir() or root.resolve() != root
            or root.parent.resolve() != root.parent):
        raise ConsoleError("拒绝完整清理危险或不明确的安装路径：" + str(root))
    _purge_parent_security(root)
    marker = root / ".xiaowork-watch-managed"
    if _read_regular(marker).strip() != MARKER_VALUE:
        raise ConsoleError("安装标记无效，未清理数据：" + str(root))
    return root


def _purge_plan(manager):
    """Read-only proof of a bounded deployment tree while holding its lock."""
    root = _purge_root(manager)
    allowed = {".xiaowork-watch-managed", ".installation-complete", ".deploy.lock", "config.json", "installed.json",
               "releases", "shared", "current", "previous", "control"}
    for child in root.iterdir():
        if child.name not in allowed:
            raise ConsoleError("发现未登记的安装目录内容，完整清理已停止，请先检查该路径：" + str(child))
        if child.name not in {"releases", "shared", "current", "previous", "control"}:
            if child.is_symlink() or not stat.S_ISREG(child.lstat().st_mode):
                raise ConsoleError("安装根目录中的记录不是自有普通文件，已保留：" + str(child))
    for name in ("releases", "shared"):
        directory = root / name
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise ConsoleError("部署数据目录不安全，已保留：" + str(directory))
    shared = root / "shared"
    if shared.exists():
        for child in shared.iterdir():
            if child.name not in {"assets", "acme", "letsencrypt", "certbot-work", "certbot-logs"}:
                raise ConsoleError("共享目录含有未登记内容，已保留：" + str(child))
            if child.is_symlink() or not child.is_dir():
                raise ConsoleError("共享数据目录不安全，已保留：" + str(child))
        _tls_directories(manager)
    releases = root / "releases"
    if releases.exists():
        for release in releases.iterdir():
            if release.is_symlink() or not release.is_dir() or not re.fullmatch(r"[0-9a-f]{40}", release.name):
                raise ConsoleError("历史发布目录无法确认归属，已保留：" + str(release))
            try:
                metadata = json.loads(_read_regular(release / "release.json"))
            except (ValueError, OSError) as error:
                raise ConsoleError("历史发布信息无效，已保留：" + str(release)) from error
            if (not isinstance(metadata, dict) or type(metadata.get("schema")) is not int or metadata["schema"] != 1
                    or metadata.get("commit") != release.name or metadata.get("kind") != "frontend-prototype"):
                raise ConsoleError("历史发布信息不属于此安装，已保留：" + str(release))
    # st_dev alone misses bind mounts. Linux mountinfo includes those mountpoints.
    if sys.platform.startswith("linux"):
        try:
            mount_lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
        except OSError as error:
            raise ConsoleError("无法确认安装目录的挂载边界，完整清理已停止。") from error
        for line in mount_lines:
            parts = line.split()
            if len(parts) < 5:
                raise ConsoleError("无法确认挂载信息，完整清理已停止。")
            mount = Path(re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), parts[4]))
            if mount == root or root in mount.parents:
                raise ConsoleError("安装数据含有挂载目录，已保留：" + str(mount))
    identity = root.stat()
    pending = [root]
    while pending:
        directory = pending.pop()
        for child in directory.iterdir():
            info = child.lstat()
            if stat.S_ISLNK(info.st_mode):
                try:
                    child.resolve().relative_to(root)
                except (ValueError, OSError, RuntimeError) as error:
                    raise ConsoleError("安装目录含有指向外部或无效的链接，已保留：" + str(child)) from error
                continue  # Never walk a linked directory.
            if stat.S_ISDIR(info.st_mode):
                if info.st_dev != identity.st_dev or os.path.ismount(str(child)):
                    raise ConsoleError("安装数据含有挂载目录，已保留：" + str(child))
                pending.append(child)
            elif not stat.S_ISREG(info.st_mode):
                raise ConsoleError("安装目录含有特殊文件，已保留：" + str(child))
    return identity.st_dev, identity.st_ino


def _purge_move(manager, identity):
    root = Path(manager.root)
    current = root.lstat()
    if (root.parent.resolve() != root.parent or not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino) != identity):
        raise ConsoleError("安装目录在清理前已改变，请重新执行卸载。")
    tombstone = root.parent / (".xiaowork-watch-purge-" + secrets.token_hex(16))
    if (tombstone.parent != root.parent or not tombstone.is_absolute()
            or tombstone.exists() or tombstone.is_symlink()):
        raise ConsoleError("无法分配安全的数据清理路径，尚未删除数据。")
    # Move the inode containing the held lock. Old waiters must reject its stale path.
    os.rename(str(root), str(tombstone))
    return tombstone


def _purge_remove(tombstone, identity):
    try:
        current = tombstone.lstat()
        if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != identity:
            raise ConsoleError("清理目录已改变")
        shutil.rmtree(str(tombstone))
    except Exception as error:
        raise ConsoleError("网站入口和服务已卸载，但数据清理未完成。残留数据路径：" + str(tombstone)
                           + "；请检查后清理，未删除 Nginx、Certbot 或其他站点。") from error


def uninstall(manager, confirm=False, paths=DEFAULT_PATHS, locked=None, purge=False):
    if confirm is not True:
        return {"status": "cancelled", "dataRetained": True}
    if type(purge) is not bool:
        raise ConsoleError("完整清理选项必须为布尔值。")
    if purge:
        _purge_root(manager)  # Reject dangerous roots; inspect transient contents after waiting.
    with _lock(manager, locked):
        purge_identity = _purge_plan(manager) if purge else None
        global_paths = [(paths.nginx_site, "site"), (paths.nginx_proxy, "proxy"),
                        (paths.wrapper, "wrapper"), (paths.service, "service"), (paths.timer, "timer"),
                        (paths.tls_service, "tls-service"), (paths.tls_timer, "tls-timer")]
        for path, kind in global_paths:
            _require_owned(path, manager, kind, paths)
        internal = _internal_uninstall_targets(manager)
        snapshots = {path: (_read_regular(path), stat.S_IMODE(path.stat().st_mode))
                     for path, _ in global_paths if path.exists()}
        config = manager._config()
        config_path = manager.root / "config.json"
        old_config = (_read_regular(config_path), stat.S_IMODE(config_path.stat().st_mode))
        timer_states = {}
        for path, name in ((paths.timer, "xiaowork-watch-update.timer"), (paths.tls_timer, TLS_TIMER)):
            if path in snapshots:
                timer_states[name] = (_unit_state(name, "is-enabled"), _unit_state(name, "is-active"))
        nginx_active = _unit_state("nginx.service", "is-active")
        removed = []
        try:
            config["autoUpdate"] = False
            if "httpsEnabled" in config:
                config["httpsEnabled"] = False
            _write_config(manager, config)
            if paths.timer in snapshots:
                _run(["systemctl", "disable", "--now", "xiaowork-watch-update.timer"])
            if paths.service in snapshots:
                _run(["systemctl", "stop", "xiaowork-watch-update.service"])
            if paths.tls_timer in snapshots:
                _run(["systemctl", "disable", "--now", TLS_TIMER])
            if paths.tls_service in snapshots:
                _run(["systemctl", "stop", TLS_SERVICE])
            for path in (paths.nginx_site, paths.nginx_proxy):
                if path in snapshots:
                    path.unlink()
                    removed.append(path)
            if removed:
                _run(["nginx", "-t"])
                if nginx_active:
                    _run(["systemctl", "reload", "nginx"])
            for path in (paths.service, paths.timer, paths.tls_service, paths.tls_timer, paths.wrapper):
                if path in snapshots:
                    path.unlink()
                    removed.append(path)
            if any(path in snapshots for path in (paths.service, paths.timer, paths.tls_service, paths.tls_timer)):
                _run(["systemctl", "daemon-reload"])
            tombstone = _purge_move(manager, purge_identity) if purge else None
        except BaseException:
            for path in removed:
                _write(path, snapshots[path][0], snapshots[path][1])
            _write(config_path, old_config[0], old_config[1])
            commands = [["systemctl", "daemon-reload"], ["nginx", "-t"]]
            if nginx_active:
                commands.append(["systemctl", "reload", "nginx"])
            for command in commands:
                try:
                    _run(command)
                except ConsoleError:
                    pass
            for name, (enabled, active) in timer_states.items():
                for command in (["systemctl", "enable" if enabled else "disable", name],
                                ["systemctl", "start" if active else "stop", name]):
                    try:
                        _run(command)
                    except ConsoleError:
                        pass
            raise
        if purge:
            _purge_remove(tombstone, purge_identity)
        else:
            internal.sort(key=lambda path: 0 if path.name in ("current", "previous", "control") else 1)
            for path in internal:
                path.unlink()
    return {"status": "uninstalled", "dataRetained": not purge, "root": str(manager.root)}


def _open_terminal():
    try:
        return open("/dev/tty", "r+", encoding="utf-8", errors="replace", buffering=1)
    except io.UnsupportedOperation:
        # Some Python builds reject BufferedRandom on a non-seekable tty.
        raw = open("/dev/tty", "r+b", buffering=0)
        return io.TextIOWrapper(raw, encoding="utf-8", errors="replace", write_through=True)


def _say(terminal, text):
    terminal.write(text + "\n")
    terminal.flush()


def _ask(terminal, text):
    while True:
        terminal.write(text)
        terminal.flush()
        line = terminal.readline()
        if not line:
            return None
        # Reject the entire damaged line; dropping undecodable bytes could
        # turn malformed input into a valid YES or UNINSTALL confirmation.
        if "\ufffd" in line:
            _say(terminal, "输入含有无效的 UTF-8 字符，本行未采用。请将 SSH 客户端编码设为 UTF-8 后重新输入。")
            continue
        return line.strip()


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
            + "\n反代域名：" + str(config.get("proxyDomain") or "未配置")
            + "\nHTTPS：" + ("已配置（独立自动续期）" if config.get("httpsEnabled") else "未配置"))


def _uninstall_dialog(manager, terminal, locked, paths=DEFAULT_PATHS):
    _say(terminal, "\n========== 彻底卸载 xiaowork Watch ==========\n"
         "将停止网站自动更新和证书续期，移除本项目的 HTTP/HTTPS 站点、管理命令及服务入口。\n"
         "将永久清理本项目的历史发布包、安装配置和证书，安装目录：" + str(manager.root) + "\n"
         "Nginx、Certbot 和其他站点保留。")
    if _ask(terminal, "输入 UNINSTALL 确认卸载（输入 0 或直接回车取消）：") != "UNINSTALL":
        _say(terminal, "已取消卸载。")
        return False
    uninstall(manager, confirm=True, paths=paths, locked=locked, purge=True)
    _say(terminal, "彻底卸载完成：网站、反代、管理命令及专用定时任务已移除。\n"
         "本项目历史包、配置和证书已清理，安装目录已移除：" + str(manager.root) + "\n"
         "重新部署可使用安装链接。")
    return True


def run_uninstall_menu(manager, locked, paths=DEFAULT_PATHS):
    try:
        terminal = _open_terminal()
    except (OSError, ValueError) as error:
        print("未找到交互终端，未执行卸载。请在 SSH 终端运行 sudo xiaowork-watch uninstall。\n"
              + str(error), file=sys.stderr)
        return 1
    with terminal:
        try:
            _uninstall_dialog(manager, terminal, locked, paths)
            return 0
        except Exception as error:
            _say(terminal, "卸载失败：" + str(error))
            return 1


def run_menu(manager, locked, paths=DEFAULT_PATHS):
    try:
        terminal = _open_terminal()
    except (OSError, ValueError) as error:
        print("未找到交互终端；请在 SSH 终端运行 sudo xiaowork-watch menu。未执行安装或删除。\n" + str(error), file=sys.stderr)
        return 1
    with terminal:
        while True:
            _say(terminal, "\n========== xiaowork Watch ==========\n服务器管理\n\n  1. 查看状态\n  2. 配置域名 HTTP 反代\n  3. 更新网站\n  4. 回退上一版本\n  5. 自动更新开关\n  6. 查看更新日志\n  7. 彻底卸载 xiaowork Watch\n  8. 配置 HTTPS\n  0. 退出\n")
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
                    if _uninstall_dialog(manager, terminal, locked, paths):
                        return 0
                elif choice == "8":
                    default = manager._config().get("proxyDomain", "")
                    entered = _ask(terminal, "HTTPS 域名" + (" [" + default + "]" if default else "") + "（Enter 接受默认）：")
                    if entered is None:
                        continue
                    domain = entered or default
                    if not domain:
                        _say(terminal, "已取消；HTTPS 需要域名。")
                        continue
                    email = _ask(terminal, "联系邮箱（必填，留空取消）：")
                    if not email:
                        _say(terminal, "已取消；联系邮箱不能为空。")
                        continue
                    domain, email = _tls_domain(domain), _email(email)
                    _say(terminal, "请确认域名 A 记录已指向此服务器，当前入口仅支持 IPv4，请移除该域名的 AAAA；80/443 端口已开放，并同意 Let's Encrypt 服务条款：https://letsencrypt.org/repository/")
                    if (_ask(terminal, "输入 yes/y 确认并申请证书（不区分大小写）：") or "").lower() not in {"yes", "y"}:
                        _say(terminal, "已取消 HTTPS 配置。")
                        continue
                    result = configure_https(manager, domain, email, paths=paths, locked=locked)
                    _say(terminal, "HTTPS 已配置：" + result["url"] + "\n专用证书续期任务已开启，暂停网站自动更新不影响续期。")
                else:
                    _say(terminal, "请选择菜单中的编号。")
            except Exception as error:
                _say(terminal, "操作失败：" + str(error) + "\n可修正后重试，或输入 0 退出。")


if __name__ == "__main__":
    if sys.argv[1:2] != ["--isolated-certbot"]:
        sys.exit("请使用 xiaowork-watch 管理命令。")
    try:
        sys.exit(_isolated_certbot_main(sys.argv[2:]))
    except (ConsoleError, ImportError) as error:
        sys.exit(str(error))
