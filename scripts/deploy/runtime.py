"""Managed Python/SQLite runtime. Called only for monitoring-server packages."""
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from urllib.parse import urlsplit

OWNER = "# Managed by xiaowork Watch's frontend installer."
ROOT_PREFIX = "# xiaowork-watch-root: "
USER = "xiaowork-watch"
COMMENT = "xiaowork Watch backend"
SERVICE_NAME = "xiaowork-watch-backend.service"
SERVICE = Path("/etc/systemd/system/" + SERVICE_NAME)
SITE = Path("/etc/nginx/conf.d/xiaowork-watch.conf")
PROXY = Path("/etc/nginx/conf.d/xiaowork-watch-proxy.conf")


def run(arguments, capture=True):
    result = subprocess.run(arguments, check=True, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None, text=True, timeout=45)
    return result.stdout + result.stderr if capture else ""


def owned(path, root):
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("主控配置路径无效：" + str(path))
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
        if lines.count(OWNER) != 1 or [x for x in lines if x.startswith(ROOT_PREFIX)] != [ROOT_PREFIX + str(root)]:
            raise ValueError("主控配置属于其他安装，已停止：" + str(path))


def write(path, text):
    # Root-owned directory, atomic replacement; never follow destination links.
    import tempfile
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("拒绝替换非普通配置文件。")
    fd, name = tempfile.mkstemp(prefix=".watch-runtime-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(name, 0o644)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def api_port(config):
    frontend_port = urlsplit(config.get("healthUrl", "")).port or 8088
    value = config.get("backendPort", 8092 if frontend_port == 8091 else 8091)
    if type(value) is not int or not 1024 <= value <= 65535:
        raise ValueError("backendPort 必须为 1024–65535 的整数。")
    if value == frontend_port:
        raise ValueError("主控 backendPort 不能与网站端口相同。")
    return value


def unit(root, port, sha):
    return (OWNER + "\n" + ROOT_PREFIX + str(root) + "\n"
            "[Unit]\nDescription=xiaowork Watch real monitoring backend\n"
            "After=network-online.target\nWants=network-online.target\n\n"
            "[Service]\nType=simple\nUser=" + USER + "\nGroup=" + USER + "\n"
            "Environment=WATCH_RELEASE=" + sha + "\nEnvironment=PYTHONDONTWRITEBYTECODE=1\n"
            "ExecStart=/usr/bin/python3 " + str(root / "current/.backend/server.py")
            + " --host 127.0.0.1 --port " + str(port) + " --data-dir " + str(root / "shared/data") + "\n"
            "Restart=on-failure\nRestartSec=3\nTimeoutStopSec=35\nUMask=0077\n"
            "NoNewPrivileges=true\nPrivateTmp=true\nProtectSystem=strict\nProtectHome=true\n"
            "ReadWritePaths=" + str(root / "shared/data") + "\n"
            "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX\n\n[Install]\nWantedBy=multi-user.target\n")


def site_domains(text, domains):
    domains = [value for value in domains if value]
    for value in domains:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", value):
            raise ValueError("受控域名别名格式无效。")
    def replace(match):
        names = list(dict.fromkeys(match.group(1).split() + domains))
        return 'server_name ' + ' '.join(names) + ';'
    if domains:
        text, count = re.subn(r'server_name\s+([^;]+);', replace, text, count=1)
        if not count:
            raise ValueError("受控网站缺少 server_name 配置。")
    return text


def nginx_site(text, port, domains=()):
    text = site_domains(text, domains)
    if "# xiaowork-watch-api-start" in text:
        text = re.sub(r"\n+# xiaowork-watch-api-start.*?# xiaowork-watch-api-end\n", "\n", text, flags=re.S)
        text = re.sub(r"\n+    # xiaowork-watch-locations-start.*?    # xiaowork-watch-locations-end\n", "\n", text, flags=re.S)
    # Only the inner managed server accepts forwarded TLS from a local outer
    # Nginx proxy. Public requests cannot claim https by supplying a header.
    maps = ("\n# xiaowork-watch-api-start\n"
            'map "$remote_addr:$http_x_forwarded_proto" $xiaowork_watch_scheme {\n'
            '    default $scheme;\n    "127.0.0.1:https" https;\n    "::1:https" https;\n}\n'
            'map "$remote_addr:$http_x_real_ip" $xiaowork_watch_client {\n'
            '    default $remote_addr;\n    ~^127\\.0\\.0\\.1:(.+)$ $1;\n    ~^::1:(.+)$ $1;\n}\n'
            '# xiaowork-watch-api-end\n')
    locations = "\n    # xiaowork-watch-locations-start\n"
    for prefix in ("/api/", "/agent/"):
        locations += ("    location ^~ " + prefix + " {\n"
                      "        proxy_pass http://127.0.0.1:" + str(port) + ";\n"
                      "        proxy_set_header Host $http_host;\n"
                      "        proxy_set_header X-Forwarded-Proto $xiaowork_watch_scheme;\n"
                      "        proxy_set_header X-Real-IP $xiaowork_watch_client;\n"
                      "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
                      "        client_max_body_size 64k;\n        proxy_read_timeout 40s;\n"
                      "        add_header Cache-Control \"no-store\" always;\n    }\n")
    locations += "    # xiaowork-watch-locations-end\n"
    match = re.search(r"(?m)^server\s*\{", text)
    if not match:
        raise ValueError("受控网站配置缺少 server 块。")
    text = text[:match.start()] + maps + text[match.start():]
    index = text.rfind("}")
    return text[:index] + locations + text[index:]


def data_directory(root):
    import pwd
    try:
        account = pwd.getpwnam(USER)
        if account.pw_gecos != COMMENT or account.pw_dir != "/nonexistent" or account.pw_shell != "/usr/sbin/nologin":
            raise ValueError("同名系统用户属于其他用途，已停止。")
    except KeyError:
        run(["useradd", "--system", "--user-group", "--no-create-home", "--home-dir", "/nonexistent",
             "--shell", "/usr/sbin/nologin", "--comment", COMMENT, USER])
        account = pwd.getpwnam(USER)
    directory = root / "shared/data"
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise ValueError("主控数据目录不是普通目录。")
    directory.mkdir(mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    os.chown(directory, account.pw_uid, account.pw_gid)
    return directory


class Activation:
    def __init__(self, manager, release, config):
        if os.name != "posix" or os.geteuid() != 0:
            raise ValueError("真实主控安装需要 Ubuntu/Debian 的 root 权限。")
        self.root, self.release, self.port = manager.root, release, api_port(config)
        self.config = dict(config)
        self.domains = [config.get('proxyDomain'), config.get('tlsDomain')]
        if self.root == Path('/root') or Path('/root') in self.root.parents or self.root == Path('/home') or Path('/home') in self.root.parents:
            raise ValueError("主控服务隔离不支持 /home 或 /root 安装目录，请使用 /opt 或 /srv 下的目录。")
        if not re.fullmatch(r"/[A-Za-z0-9._/-]+", str(self.root)):
            raise ValueError("主控安装路径含有 systemd 不支持的字符。")
        self.snapshots = {}
        for path in (SERVICE, SITE, PROXY):
            owned(path, self.root)
            self.snapshots[path] = path.read_text(encoding="utf-8") if path.exists() else None
        if self.snapshots[SITE] is None:
            raise ValueError("主控升级需要已有受控 Nginx 网站配置。")
        self.was_active = subprocess.run(["systemctl", "is-active", "--quiet", SERVICE_NAME],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        self.was_enabled = subprocess.run(["systemctl", "is-enabled", "--quiet", SERVICE_NAME],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        self.changed = False
        self.start_attempted = False
        self.units_touched = False

    def prepare(self):
        directory = data_directory(self.root)
        # Admin is created once and printed only to the local installing terminal.
        run(["runuser", "-u", USER, "--", "/usr/bin/python3", "-B", str(self.release / ".backend/server.py"),
             "--data-dir", str(directory), "--init-admin"], capture=False)
        self.changed = True
        write(SITE, nginx_site(self.snapshots[SITE], self.port, self.domains))
        if self.snapshots[PROXY] is not None:
            # Preserve the original Host through the public HTTP/HTTPS proxy.
            proxy = re.sub(r"proxy_set_header Host [^;]+;", "proxy_set_header Host $http_host;", self.snapshots[PROXY])
            write(PROXY, proxy)
        write(SERVICE, unit(self.root, self.port, self.release.name))
        if 'conflicting server name' in run(["nginx", "-t"]).lower():
            raise ValueError("Nginx 域名与已有网站冲突，已停止升级。")

    def start(self):
        self.units_touched = True
        run(["systemctl", "daemon-reload"])
        run(["systemctl", "enable", SERVICE_NAME])
        self.start_attempted = True
        run(["systemctl", "restart", SERVICE_NAME])
        self.health(self.release.name)
        run(["systemctl", "reload", "nginx"])
        self.proxy_health(self.release.name)

    def proxy_health(self, sha):
        endpoint = urlsplit(self.config.get("healthUrl", ""))
        if endpoint.scheme != 'http' or endpoint.hostname not in ('127.0.0.1', 'localhost', '::1'):
            raise ValueError("网站 API 健康检查要求本地 HTTP 地址。")
        url = 'http://' + endpoint.netloc + '/api/health'
        host = self.config.get('tlsDomain') or self.config.get('proxyDomain') or self.config.get('healthHost')
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, *arguments, **keywords):
                return None
        opener = build_opener(ProxyHandler({}), NoRedirect())
        for attempt in range(10):
            try:
                request = Request(url, headers={'Host': host} if host else {})
                with opener.open(request, timeout=3) as response:
                    value = json.loads(response.read(16384))
                if value.get('code') == 0 and value.get('data', {}).get('release') == sha:
                    return
            except Exception:
                pass
            if attempt < 9:
                time.sleep(0.25)
        raise ValueError("Nginx API 健康检查失败，更新将恢复原版本。")

    def health(self, sha):
        opener = build_opener(ProxyHandler({}))
        for attempt in range(20):
            try:
                request = Request("http://127.0.0.1:" + str(self.port) + "/api/health")
                with opener.open(request, timeout=2) as response:
                    value = json.loads(response.read(16384))
                if value.get("code") == 0 and value.get("data", {}).get("release") == sha:
                    return
            except Exception:
                pass
            time.sleep(0.25)
        raise ValueError("主控健康检查失败，更新将恢复原版本。")

    def restore(self):
        if not self.changed:
            return
        errors = []
        def recover(arguments):
            try:
                run(arguments)
                return True
            except Exception as error:
                errors.append(str(error))
                return False
        # Before start(), a first upgrade has no loaded backend unit. A failed
        # stop must never prevent restoration of the old Nginx configuration.
        if self.start_attempted:
            recover(["systemctl", "stop", SERVICE_NAME])
        if self.snapshots[SERVICE] is None and self.units_touched:
            recover(["systemctl", "disable", SERVICE_NAME])
        for path, text in self.snapshots.items():
            try:
                if text is None:
                    if path.exists():
                        path.unlink()
                else:
                    write(path, text)
            except Exception as error:
                errors.append(str(error))
        recover(["systemctl", "daemon-reload"])
        if self.snapshots[SERVICE] is not None:
            recover(["systemctl", "enable" if self.was_enabled else "disable", SERVICE_NAME])
            if self.was_active and self.start_attempted:
                recover(["systemctl", "start", SERVICE_NAME])
        if recover(["nginx", "-t"]):
            recover(["systemctl", "reload", "nginx"])
        if errors:
            raise ValueError("已尝试恢复原配置，仍有服务恢复错误：" + "; ".join(errors))


def reset_admin(manager, capture=False):
    root = manager.root
    owned(SERVICE, root)
    if not SERVICE.exists() or not (root / "current/.backend/server.py").is_file():
        raise ValueError("此安装尚未启用真实主控，请先升级。")
    return run(["runuser", "-u", USER, "--", "/usr/bin/python3", "-B", str(root / "current/.backend/server.py"),
                "--data-dir", str(root / "shared/data"), "--reset-admin"], capture=capture)
