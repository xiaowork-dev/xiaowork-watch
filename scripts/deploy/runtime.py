"""Managed Python/SQLite runtime. Called only for monitoring-server packages."""
import json
import contextlib
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import subprocess
import tempfile
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
        self.original_release = manager._pointed_sha('current')
        self.target_schema = json.loads((release / 'release.json').read_text(encoding='utf-8')).get('dataSchema', 1)
        self.owner_uid = os.geteuid()
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
        self.stop_attempted = False
        self.marker_active = False
        self.committed = False
        self.backup = None
        self.directory = None
        self.recovery_blocked = False

    def _inactive(self):
        run(["systemctl", "stop", SERVICE_NAME])
        state = run(["systemctl", "show", SERVICE_NAME, "--property=ActiveState", "--value"]).strip()
        if state not in ('inactive', 'failed'):
            raise ValueError("主控服务尚未停止，未修改数据库。")

    def _database_schema(self, path):
        if path.is_symlink() or not path.is_file():
            raise ValueError("数据库或恢复备份路径不安全。")
        try:
            with contextlib.closing(sqlite3.connect(str(path), timeout=10)) as database:
                return database.execute('PRAGMA user_version').fetchone()[0]
        except sqlite3.Error as error:
            raise ValueError("数据库版本检查失败。") from error

    def _marker_read(self):
        path = self.directory / '.activation-in-progress'
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != self.owner_uid
                or (os.name == 'posix' and stat.S_IMODE(info.st_mode) != 0o600) or info.st_size > 8192):
            raise ValueError("遗留升级标记归属或权限无效，已停止。")
        value = json.loads(path.read_text(encoding='utf-8'))
        if (not isinstance(value, dict) or type(value.get('schema')) is not int or value['schema'] != 1 or value.get('root') != str(self.root)
                or type(value.get('databaseSchema')) is not int or value['databaseSchema'] not in (0, 1, 2)
                or type(value.get('targetSchema')) is not int or value['targetSchema'] not in (1, 2)
                or not isinstance(value.get('target'), str) or not re.fullmatch(r'[a-f0-9]{40}', value['target'])
                or (value.get('current') is not None and (not isinstance(value['current'], str) or not re.fullmatch(r'[a-f0-9]{40}', value['current'])))
                or (value.get('backup') is not None and (not isinstance(value['backup'], str) or not re.fullmatch(r'watch\.activation-schema[012]-[a-f0-9]{24}\.sqlite3', value['backup'])))):
            raise ValueError("遗留升级标记内容无效，已保留供恢复。")
        return value

    def _marker_write(self, value):
        descriptor, name = tempfile.mkstemp(prefix='.activation-', dir=str(self.directory))
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
                json.dump(value, output, sort_keys=True)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(name, 0o600)
            os.replace(name, self.directory / '.activation-in-progress')
            self.journal = value
            self.marker_active = True
            self._sync_data_directory()
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _sync_data_directory(self):
        if os.name == 'posix':
            descriptor = os.open(str(self.directory), os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def _snapshot_database(self, schema):
        backup = self.directory / ('watch.activation-schema' + str(schema) + '-' + secrets.token_hex(12) + '.sqlite3')
        descriptor = os.open(str(backup), os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        os.close(descriptor)
        try:
            self._copy_database(self.directory / 'watch.sqlite3', backup)
            if self._database_schema(backup) != schema:
                raise ValueError("升级前数据库快照版本不一致。")
            return backup
        except BaseException:
            backup.unlink()
            raise

    def _copy_database(self, source, destination):
        deadline = time.monotonic() + 10
        def bounded(unused_status, unused_remaining, unused_total):
            if time.monotonic() > deadline:
                raise ValueError("数据库备份超过时限。")
        try:
            with contextlib.closing(sqlite3.connect(str(source), timeout=10)) as original:
                with contextlib.closing(sqlite3.connect(str(destination), timeout=10)) as copied:
                    original.backup(copied, pages=128, progress=bounded, sleep=0.05)
                    if copied.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                        raise ValueError("数据库备份完整性检查失败。")
        except sqlite3.Error as error:
            raise ValueError("数据库备份或恢复失败。") from error
        os.chmod(str(destination), 0o600)
        descriptor = os.open(str(destination), os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _backup_valid(self, backup):
        info = backup.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != self.owner_uid
                or (os.name == 'posix' and stat.S_IMODE(info.st_mode) != 0o600)):
            raise ValueError("恢复备份归属或权限无效。")

    def _restore_database(self, backup):
        self._backup_valid(backup)
        destination = self.directory / 'watch.sqlite3'
        if destination.is_symlink() or not destination.is_file():
            raise ValueError("数据库恢复目标不安全。")
        owner = destination.stat()
        descriptor, name = tempfile.mkstemp(prefix='.restore-database-', dir=str(self.directory))
        os.close(descriptor)
        staged = Path(name)
        try:
            self._copy_database(backup, staged)
            if os.name == 'posix':
                os.chown(str(staged), owner.st_uid, owner.st_gid)
            for suffix in ('-wal', '-shm'):
                sidecar = destination.with_name(destination.name + suffix)
                if sidecar.is_symlink() or (sidecar.exists() and not sidecar.is_file()):
                    raise ValueError("数据库 WAL/SHM 文件不安全。")
                if sidecar.exists():
                    sidecar.unlink()
            os.replace(str(staged), str(destination))
            self._sync_data_directory()
        finally:
            if staged.exists():
                staged.unlink()

    def _current_schema(self):
        if self.original_release is None:
            return None
        metadata = json.loads((self.root / 'releases' / self.original_release / 'release.json').read_text(encoding='utf-8'))
        return metadata.get('dataSchema', 1) if metadata.get('kind') == 'monitoring-server' else None

    def _database_prepare(self):
        marker = self.directory / '.activation-in-progress'
        database = self.directory / 'watch.sqlite3'
        current_schema = self._current_schema()
        self.recovery_blocked = current_schema is not None
        existed = database.exists()
        if not existed and current_schema is not None and self.snapshots[SERVICE] is not None:
            raise ValueError("已有主控数据库缺失，已停止升级。")
        actual = self._database_schema(database) if existed else 0
        self.recovery_blocked = current_schema is not None and current_schema < actual
        if marker.exists() or marker.is_symlink():
            prior = self._marker_read()
            if self.original_release not in (prior['current'], prior['target']):
                raise ValueError("遗留升级标记与当前网站版本不一致，已停止。")
            if actual > self.target_schema:
                raise ValueError("目标版本不能读取现有数据库，已保留升级标记。")
            if current_schema is not None and current_schema < actual:
                if not prior['backup'] or prior['databaseSchema'] > current_schema:
                    raise ValueError("遗留升级缺少匹配的旧数据库备份，未启动旧服务。")
                backup = self.directory / prior['backup']
                self._backup_valid(backup)
                if self._database_schema(backup) != prior['databaseSchema']:
                    raise ValueError("遗留数据库备份版本不匹配。")
                self._restore_database(backup)
                actual = prior['databaseSchema']
                self.recovery_blocked = False
        if actual > self.target_schema:
            raise ValueError("目标版本不能读取现有数据库。")
        # v0.3 used SQLite's default user_version=0. An existing database with
        # that raw version is real legacy data and must be backed up, even when
        # the old release advertises dataSchema=1. Absence is the fresh case.
        self.backup = self._snapshot_database(actual) if existed and actual < self.target_schema else None
        self._marker_write({'schema': 1, 'root': str(self.root), 'current': self.original_release,
                            'target': self.release.name, 'targetSchema': self.target_schema,
                            'databaseSchema': actual, 'backup': self.backup.name if self.backup else None})

    def prepare(self):
        directory = data_directory(self.root)
        self.directory = directory
        if self.snapshots[SERVICE] is not None:
            self.stop_attempted = True
            self._inactive()
        self._database_prepare()
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

    def commit(self):
        # Unlink is the publication boundary. The backend may accept writes as
        # soon as it succeeds; nothing that can raise follows this operation.
        if self._marker_read() != self.journal:
            raise ValueError("升级标记已改变，尚未开放写入。")
        (self.directory / '.activation-in-progress').unlink()
        self.marker_active = False
        self.committed = True

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
        if self.committed or not (self.changed or self.stop_attempted or self.marker_active):
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
        stopped = True
        if self.start_attempted or self.stop_attempted:
            try:
                self._inactive()
            except Exception as error:
                errors.append(str(error))
                stopped = False
        database_restored = stopped and not self.recovery_blocked
        if self.recovery_blocked:
            errors.append("现有数据库与旧版本不兼容，未启动旧服务；升级标记及备份已保留。")
        if self.backup is not None and stopped:
            try:
                self._restore_database(self.backup)
            except Exception as error:
                errors.append("数据库恢复失败；备份保留在 " + str(self.backup) + ": " + str(error))
                database_restored = False
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
        reloaded = recover(["systemctl", "daemon-reload"])
        if database_restored and reloaded and not errors and self.marker_active:
            try:
                if self._marker_read() != self.journal:
                    raise ValueError("升级标记已改变，未清除。")
                (self.directory / '.activation-in-progress').unlink()
                self.marker_active = False
            except Exception as error:
                errors.append(str(error))
        if self.snapshots[SERVICE] is not None:
            recover(["systemctl", "enable" if self.was_enabled else "disable", SERVICE_NAME])
            if self.was_active and (self.start_attempted or self.stop_attempted) and database_restored and not errors:
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
