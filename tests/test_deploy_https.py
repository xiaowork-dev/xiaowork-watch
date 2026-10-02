"""Offline HTTPS fixtures: no public ACME requests or real system changes."""
import contextlib
from dataclasses import fields
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "console.py"
if not SOURCE.exists():
    SOURCE = HERE.parent / "scripts" / "deploy" / "console.py"
SPEC = importlib.util.spec_from_file_location("deploy_https_console", str(SOURCE))
console = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = console
SPEC.loader.exec_module(console)
SHA = "a" * 40
DOMAIN = "watch.example.com"


class Manager:
    def __init__(self, root):
        self.root = root
    def _config(self):
        return json.loads((self.root / "config.json").read_text(encoding="utf-8"))
    def _pointed_sha(self, name):
        return SHA if name == "current" else None
    def status(self):
        return {"current": SHA, "autoUpdate": self._config()["autoUpdate"]}


class Terminal:
    def __init__(self, inputs):
        self.inputs, self.output = io.StringIO(inputs), io.StringIO()
    def readline(self):
        return self.inputs.readline()
    def write(self, value):
        return self.output.write(value)
    def flush(self):
        pass
    def __enter__(self):
        return self
    def __exit__(self, *_):
        return False


class HttpsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.root, self.system = base / "install", base / "system"
        self.root.mkdir()
        self.system.mkdir()
        self.default_ini = self.system / "global" / "cli.ini"
        self.default_ini.parent.mkdir()
        self.xdg_home = self.system / "xdg"
        defaults = patch.object(console, "CERTBOT_SYSTEM_CONFIG", self.default_ini)
        defaults.start()
        self.addCleanup(defaults.stop)
        environment = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.xdg_home)})
        environment.start()
        self.addCleanup(environment.stop)
        self.manager = Manager(self.root)
        self.paths = console.Paths(**{field.name: self.system / field.name for field in fields(console.Paths)})
        self.calls, self.locks = [], []
        self.locked_now = False
        self.tools = True
        self.issue_version = 1
        self.timer_enabled, self.timer_active = False, False
        self.fail = None
        self.write_config()
        (self.root / "installed.json").write_text(json.dumps({"kind": "frontend-prototype"}), encoding="utf-8")
        (self.root / ".installation-complete").write_text(console.MARKER_VALUE, encoding="utf-8")
        site = "server {\n root " + str(self.root) + "/current;\n alias " + str(self.root) + "/shared/assets/;\n}\n"
        for path, value in ((self.paths.nginx_site, site), (self.paths.nginx_proxy, "server { listen 80; }\n"),
                            (self.paths.wrapper, "#!/bin/sh\n"), (self.paths.service, console.OLD_SERVICE),
                            (self.paths.timer, console.OLD_TIMER)):
            path.write_text(console._header(self.manager) + value, encoding="utf-8")

    def write_config(self, **changes):
        config = {"healthUrl": "http://127.0.0.1:8088/release.json", "healthHost": "", "autoUpdate": False,
                  "proxyDomain": DOMAIN, "unrelated": "preserved"}
        config.update(changes)
        (self.root / "config.json").write_text(json.dumps(config), encoding="utf-8")

    def test_public_listener_upstream_conflicts_stop_before_certificate_request(self):
        for port in (80, 443):
            with self.subTest(port=port):
                self.write_config(healthUrl="http://127.0.0.1:" + str(port) + "/release.json")
                with patch.object(console, "_run") as commands, self.assertRaises(console.ConsoleError):
                    console.configure_https(self.manager, DOMAIN, "ops@example.com", paths=self.paths, locked=self.locked)
                commands.assert_not_called()

    @contextlib.contextmanager
    def locked(self, path):
        self.assertFalse(self.locked_now)
        self.assertEqual(path, self.root / ".deploy.lock")
        self.locks.append(path)
        self.locked_now = True
        try:
            yield
        finally:
            self.locked_now = False

    def certificates(self, version):
        folders = console._tls_directories(self.manager, create=True)
        archive = folders["certs"] / "archive" / console.TLS_NAME
        live = folders["certs"] / "live" / console.TLS_NAME
        renewal = folders["certs"] / "renewal"
        for folder in (archive, live, renewal):
            folder.mkdir(parents=True, exist_ok=True)
        for name in ("cert", "chain", "fullchain", "privkey"):
            target = archive / (name + str(version) + ".pem")
            target.write_text("fixture pem " + name + str(version), encoding="utf-8")
            link = live / (name + ".pem")
            if link.exists() or link.is_symlink():
                link.unlink()
            if os.name == "posix":
                link.symlink_to(Path("../../archive") / console.TLS_NAME / target.name)
            else:
                link.write_text("Windows fixture; structural validator is mocked", encoding="utf-8")
        (renewal / (console.TLS_NAME + ".conf")).write_text("renewal fixture version=" + str(version), encoding="utf-8")
        return folders

    def command(self, arguments, **kwargs):
        self.calls.append((arguments, kwargs))
        if self.fail and self.fail(arguments):
            raise console.ConsoleError("simulated command failure")
        if arguments[:2] == ["certbot", "certonly"]:
            self.certificates(self.issue_version)
        if arguments[:3] == ["apt-get", "install", "-y"]:
            self.tools = True
        if arguments == ["systemctl", "is-enabled", console.TLS_TIMER]:
            return "enabled" if self.timer_enabled else "disabled"
        if arguments == ["systemctl", "is-active", console.TLS_TIMER]:
            return "active" if self.timer_active else "inactive"
        if arguments[:3] == ["systemctl", "enable", "--now"] and arguments[-1] == console.TLS_TIMER:
            self.timer_enabled = self.timer_active = True
        if arguments[:3] == ["systemctl", "disable", "--now"] and arguments[-1] == console.TLS_TIMER:
            self.timer_enabled = self.timer_active = False
        if arguments == ["systemctl", "enable", console.TLS_TIMER]:
            self.timer_enabled = True
        if arguments == ["systemctl", "disable", console.TLS_TIMER]:
            self.timer_enabled = False
        if arguments == ["systemctl", "start", console.TLS_TIMER]:
            self.timer_active = True
        if arguments == ["systemctl", "stop", console.TLS_TIMER]:
            self.timer_active = False
        if "-checkhost" in arguments:
            domain = arguments[arguments.index("-checkhost") + 1]
            return "Hostname " + domain + " does match certificate"
        return ""

    @contextlib.contextmanager
    def runtime(self, health=None):
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(console, "_run", side_effect=self.command))
            stack.enter_context(patch.object(console.shutil, "which", side_effect=lambda name: "/usr/bin/" + name if self.tools else None))
            stack.enter_context(patch.object(console, "_https_health", side_effect=health))
            stack.enter_context(patch("sys.stdout", new=io.StringIO()))
            if os.name != "posix":
                stack.enter_context(patch.object(console, "_validate_certificates", return_value=(Path("fullchain.pem"), Path("privkey.pem"))))
            yield

    def apply(self, domain=DOMAIN, email="ops@example.com"):
        return console.configure_https(self.manager, domain, email, paths=self.paths, locked=self.locked)

    def test_https_success_is_isolated_and_enables_owned_renewal(self):
        with self.runtime():
            result = self.apply()
        self.assertEqual(result["url"], "https://" + DOMAIN + "/")
        proxy = self.paths.nginx_proxy.read_text(encoding="utf-8")
        self.assertIn("listen 443 ssl;", proxy)
        self.assertIn("ssl_protocols TLSv1.2 TLSv1.3;", proxy)
        self.assertIn("location ^~ /.well-known/acme-challenge/", proxy)
        self.assertIn("return 301 https://" + DOMAIN + "$request_uri;", proxy)
        self.assertIn("proxy_pass http://127.0.0.1:8088;", proxy)
        config = self.manager._config()
        self.assertTrue(config["httpsEnabled"])
        self.assertFalse(config["autoUpdate"])
        self.assertEqual(config["healthUrl"], "http://127.0.0.1:8088/release.json")
        self.assertEqual(config["unrelated"], "preserved")
        service = self.paths.tls_service.read_text(encoding="utf-8")
        self.assertIn("ExecStart=/usr/local/bin/xiaowork-watch renew-https", service)
        self.assertIn("TimeoutStartSec=900", service)
        self.assertIn(console.ROOT_PREFIX + str(self.root), service)
        timer = self.paths.tls_timer.read_text(encoding="utf-8")
        self.assertIn("OnCalendar=daily", timer)
        self.assertIn("RandomizedDelaySec=1h", timer)
        self.assertTrue(self.timer_enabled)
        args, options = next(item for item in self.calls if item[0][:2] == ["certbot", "certonly"])
        self.assertEqual(options["timeout"], 600)
        for flag in ("--webroot", "--keep-until-expiring", "--renew-with-new-domains", "--non-interactive", "--agree-tos", "--no-directory-hooks"):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index("--server") + 1], console.ACME_SERVER)
        self.assertEqual(args[args.index("--config") + 1], "/dev/null")
        for flag, folder in (("--config-dir", "letsencrypt"), ("--work-dir", "certbot-work"), ("--logs-dir", "certbot-logs")):
            self.assertEqual(args[args.index(flag) + 1], str(self.root / "shared" / folder))
        self.assertFalse(any("certbot.timer" in arguments for arguments, _ in self.calls))

    def test_dns_and_email_injection_fail_without_commands(self):
        for domain, email in (("a;evil.example.com", "ops@example.com"), (DOMAIN, "ops@example.com\nX=evil"),
                              (DOMAIN, "x@example.com; touch /tmp/x"), ("127.0.0.1", "ops@example.com"),
                              (DOMAIN, ""), (DOMAIN, "-option@example.com")):
            with self.subTest(domain=domain, email=email), patch.object(console, "_run") as commands:
                with self.assertRaises(console.ConsoleError):
                    self.apply(domain, email)
                commands.assert_not_called()

    def test_certbot_failure_restores_http_and_config_and_never_enables_timer(self):
        original_proxy = self.paths.nginx_proxy.read_bytes()
        original_config = (self.root / "config.json").read_bytes()
        self.fail = lambda args: args[:2] == ["certbot", "certonly"]
        with self.runtime(), self.assertRaises(console.ConsoleError):
            self.apply()
        self.assertEqual(self.paths.nginx_proxy.read_bytes(), original_proxy)
        self.assertEqual((self.root / "config.json").read_bytes(), original_config)
        self.assertFalse(self.paths.tls_service.exists())
        self.assertFalse(self.paths.tls_timer.exists())
        self.assertFalse(self.timer_enabled)

    def test_https_health_failure_rolls_back_complete_configuration(self):
        original = self.paths.nginx_proxy.read_bytes()
        with self.runtime(health=console.ConsoleError("bad SNI/current release")), self.assertRaises(console.ConsoleError):
            self.apply()
        self.assertEqual(self.paths.nginx_proxy.read_bytes(), original)
        self.assertNotIn("httpsEnabled", self.manager._config())
        self.assertFalse(self.paths.tls_timer.exists())

    def test_timer_enable_failure_restores_original_config_and_new_files(self):
        old_proxy = self.paths.nginx_proxy.read_bytes()
        old_config = (self.root / "config.json").read_bytes()
        self.fail = lambda args: args == ["systemctl", "enable", "--now", console.TLS_TIMER]
        with self.runtime(), self.assertRaises(console.ConsoleError):
            self.apply()
        self.assertEqual(self.paths.nginx_proxy.read_bytes(), old_proxy)
        self.assertEqual((self.root / "config.json").read_bytes(), old_config)
        self.assertFalse(self.paths.tls_service.exists())
        self.assertFalse(self.paths.tls_timer.exists())
        self.assertFalse(self.timer_enabled)

    def test_foreign_proxy_and_renewal_unit_are_preserved(self):
        for target in (self.paths.nginx_proxy, self.paths.tls_service, self.paths.tls_timer):
            with self.subTest(target=target):
                original = target.read_text(encoding="utf-8") if target.exists() else None
                target.write_text("foreign configuration", encoding="utf-8")
                with patch.object(console, "_run") as commands, self.assertRaises(console.ConsoleError):
                    self.apply()
                commands.assert_not_called()
                self.assertEqual(target.read_text(encoding="utf-8"), "foreign configuration")
                if original is None:
                    target.unlink()
                else:
                    target.write_text(original, encoding="utf-8")

    def test_existing_https_cannot_be_downgraded_by_http_entry(self):
        self.write_config(httpsEnabled=True)
        original = self.paths.nginx_proxy.read_bytes()
        with patch.object(console, "_run") as commands, self.assertRaises(console.ConsoleError):
            console.configure_proxy(self.manager, DOMAIN, paths=self.paths, locked=self.locked)
        commands.assert_not_called()
        self.assertEqual(self.paths.nginx_proxy.read_bytes(), original)

    def test_unrecorded_ssl_config_also_cannot_be_downgraded(self):
        self.paths.nginx_proxy.write_text(console._header(self.manager) + "server { listen 443 ssl; ssl_certificate /manual/file; }", encoding="utf-8")
        with patch.object(console, "_run") as commands, self.assertRaises(console.ConsoleError):
            console.configure_proxy(self.manager, DOMAIN, paths=self.paths, locked=self.locked)
        commands.assert_not_called()

    def test_missing_packages_use_long_timeout_noninteractive_apt(self):
        self.tools = False
        with self.runtime():
            self.apply()
        packages = [item for item in self.calls if item[0][0] == "apt-get"]
        self.assertEqual(len(packages), 2)
        self.assertEqual(packages[1][0], ["apt-get", "install", "-y", "certbot", "openssl"])
        self.assertTrue(all(options["timeout"] == 600 for _, options in packages))

    def test_active_global_config_blocks_issuance_and_renewal_without_changes(self):
        user_ini = self.xdg_home / "letsencrypt" / "cli.ini"
        user_ini.parent.mkdir(parents=True)
        for ini in (self.default_ini, user_ini):
            with self.subTest(path=ini):
                original_proxy = self.paths.nginx_proxy.read_bytes()
                original_config = (self.root / "config.json").read_bytes()
                ini.write_text("# existing setup\ndeploy-hook = private-command\n", encoding="utf-8")
                with self.runtime(), self.assertRaises(console.ConsoleError) as failure:
                    self.apply()
                self.assertNotIn("private-command", str(failure.exception))
                self.assertEqual(self.calls, [])
                self.assertEqual(self.paths.nginx_proxy.read_bytes(), original_proxy)
                self.assertEqual((self.root / "config.json").read_bytes(), original_config)
                self.assertIn("private-command", ini.read_text(encoding="utf-8"))
                ini.unlink()
        with self.runtime():
            self.apply()
        self.calls = []
        original_config = (self.root / "config.json").read_bytes()
        original_proxy = self.paths.nginx_proxy.read_bytes()
        user_ini.write_text("pre-hook = private-renew-hook\n", encoding="utf-8")
        with self.runtime(), self.assertRaises(console.ConsoleError):
            console.renew_https(self.manager, paths=self.paths, locked=self.locked)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.paths.nginx_proxy.read_bytes(), original_proxy)
        self.assertEqual((self.root / "config.json").read_bytes(), original_config)

    def test_default_config_paths_comments_and_unsafe_files(self):
        self.default_ini.write_text("\n # Debian sample\n ; comments\n\t\n", encoding="utf-8")
        with self.runtime():
            self.apply()
        self.default_ini.unlink()
        home = self.system / "home"
        relative = self.system / "letsencrypt" / "cli.ini"
        relative.parent.mkdir()
        relative.write_text("staging = true\n", encoding="utf-8")
        previous_cwd = Path.cwd()
        try:
            os.chdir(self.system)
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": ""}), self.assertRaises(console.ConsoleError):
                console._check_certbot_defaults()
        finally:
            os.chdir(previous_cwd)
        for xdg, target in (("~/.config", home / ".config/letsencrypt/cli.ini"),
                            (str(self.system / "xdg-*"), self.system / "xdg-one/letsencrypt/cli.ini")):
            with self.subTest(xdg=xdg):
                target.parent.mkdir(parents=True)
                target.write_text("force-renewal = true\n", encoding="utf-8")
                original_expanduser = os.path.expanduser
                def expanduser(path):
                    return str(home / path[2:]) if path.startswith("~/") else original_expanduser(path)
                with patch.dict(os.environ, {"XDG_CONFIG_HOME": xdg}), patch.object(
                        console.os.path, "expanduser", side_effect=expanduser), self.assertRaises(console.ConsoleError):
                    console._check_certbot_defaults()
                if xdg == "~/.config":
                    with patch.dict(os.environ), patch.object(console.os.path, "expanduser", side_effect=expanduser):
                        os.environ.pop("XDG_CONFIG_HOME", None)
                        with self.assertRaises(console.ConsoleError):
                            console._check_certbot_defaults()
                target.unlink()
        self.default_ini.mkdir()
        with self.assertRaises(console.ConsoleError):
            console._check_certbot_defaults()
        self.default_ini.rmdir()
        self.default_ini.write_text("# harmless\n", encoding="utf-8")
        with patch.object(console, "_read_regular", side_effect=PermissionError("private")), self.assertRaises(console.ConsoleError):
            console._check_certbot_defaults()
        if os.name == "posix":
            self.default_ini.unlink()
            self.default_ini.symlink_to(relative)
            with self.assertRaises(console.ConsoleError):
                console._check_certbot_defaults()

    def test_apt_created_active_default_config_is_rechecked_before_acme(self):
        self.tools = False
        old_proxy = self.paths.nginx_proxy.read_bytes()
        old_config = (self.root / "config.json").read_bytes()
        original_command = self.command
        def command(arguments, **kwargs):
            result = original_command(arguments, **kwargs)
            if arguments[:3] == ["apt-get", "install", "-y"]:
                self.default_ini.write_text("post-hook = package-hook\n", encoding="utf-8")
            return result
        with self.runtime(), patch.object(console, "_run", side_effect=command), self.assertRaises(console.ConsoleError):
            self.apply()
        self.assertFalse(any(args[0] == "certbot" for args, _ in self.calls))
        self.assertEqual(self.paths.nginx_proxy.read_bytes(), old_proxy)
        self.assertEqual((self.root / "config.json").read_bytes(), old_config)
        self.assertEqual(self.default_ini.read_text(encoding="utf-8"), "post-hook = package-hook\n")

    def test_owned_certificate_namespace_refuses_unmarked_existing_data(self):
        folder = self.root / "shared/letsencrypt"
        folder.mkdir(parents=True)
        (folder / "manual-certificate.pem").write_text("manual", encoding="utf-8")
        with patch.object(console, "_run") as commands, self.assertRaises(console.ConsoleError):
            self.apply()
        commands.assert_not_called()
        self.assertTrue((folder / "manual-certificate.pem").exists())

    def test_renewal_is_independent_from_website_update_pause_and_always_reloads(self):
        with self.runtime():
            self.apply()
        self.calls = []
        with self.runtime():
            result = console.renew_https(self.manager, paths=self.paths, locked=self.locked)
        self.assertEqual(result["status"], "checked")
        self.assertFalse(self.manager._config()["autoUpdate"])
        args = next(args for args, _ in self.calls if args[:2] == ["certbot", "renew"])
        self.assertIn("--cert-name", args)
        self.assertEqual(args[args.index("--cert-name") + 1], console.TLS_NAME)
        self.assertNotIn("--dry-run", args)
        self.assertIn(["nginx", "-t"], [args for args, _ in self.calls])
        self.assertIn(["systemctl", "reload", "nginx"], [args for args, _ in self.calls])

    def test_renewal_nginx_failure_propagates_to_systemd(self):
        with self.runtime():
            self.apply()
        self.fail = lambda args: args == ["systemctl", "reload", "nginx"]
        with self.runtime(), self.assertRaises(console.ConsoleError):
            console.renew_https(self.manager, paths=self.paths, locked=self.locked)

    def test_uninstall_preserves_certificates_and_only_removes_owned_tls_units(self):
        with self.runtime():
            self.apply()
            console.uninstall(self.manager, True, paths=self.paths, locked=self.locked)
        self.assertFalse(self.paths.tls_service.exists())
        self.assertFalse(self.paths.tls_timer.exists())
        self.assertTrue((self.root / "shared/letsencrypt/archive" / console.TLS_NAME / "fullchain1.pem").exists())
        self.assertTrue((self.root / "shared/letsencrypt/.xiaowork-watch-owned").exists())
        self.assertFalse(any("certbot.timer" in args for args, _ in self.calls))

    def test_uninstall_foreign_tls_unit_aborts_full_precheck(self):
        self.paths.tls_timer.write_text("foreign timer", encoding="utf-8")
        with patch.object(console, "_run") as commands, self.assertRaises(console.ConsoleError):
            console.uninstall(self.manager, True, paths=self.paths, locked=self.locked)
        commands.assert_not_called()
        self.assertTrue(self.paths.wrapper.exists())

    def test_failed_uninstall_restores_tls_config_and_active_renewal_timer(self):
        with self.runtime():
            self.apply()
        old_proxy = self.paths.nginx_proxy.read_bytes()
        old_config = (self.root / "config.json").read_bytes()
        self.fail = lambda args: args == ["nginx", "-t"] and not self.paths.nginx_proxy.exists()
        with self.runtime(), self.assertRaises(console.ConsoleError):
            console.uninstall(self.manager, True, paths=self.paths, locked=self.locked)
        self.assertEqual(self.paths.nginx_proxy.read_bytes(), old_proxy)
        self.assertEqual((self.root / "config.json").read_bytes(), old_config)
        self.assertTrue(self.manager._config()["httpsEnabled"])
        self.assertTrue(self.timer_enabled)
        self.assertTrue(self.timer_active)
        self.assertTrue(self.paths.tls_service.exists())
        self.assertTrue(self.paths.tls_timer.exists())
        self.assertTrue((self.root / "installed.json").exists())

    def test_menu_https_uses_default_domain_and_requires_yes_consent(self):
        terminal = Terminal("8\n\nops@example.com\nNO\n8\n\nops@example.com\nYES\n0\n")
        with patch.object(console, "_open_terminal", return_value=terminal), patch.object(console, "configure_https", return_value={"url": "https://" + DOMAIN + "/"}) as configure:
            self.assertEqual(console.run_menu(self.manager, self.locked, paths=self.paths), 0)
        configure.assert_called_once_with(self.manager, DOMAIN, "ops@example.com", paths=self.paths, locked=self.locked)
        self.assertIn("80/443", terminal.output.getvalue())
        self.assertIn("Let's Encrypt", terminal.output.getvalue())

    def test_checkhost_zero_exit_but_mismatch_output_is_rejected(self):
        with patch.object(console, "_certificate_target", return_value=Path("fixture.pem")), patch.object(
                console, "_run", return_value="Hostname " + DOMAIN + " does NOT match certificate") as command:
            with self.assertRaises(console.ConsoleError):
                console._validate_certificates({}, DOMAIN)
        self.assertEqual(command.call_count, 1)

    def test_https_health_checks_current_release_using_domain_sni_and_host(self):
        class Response:
            status = 200
            def read(self, limit):
                return json.dumps({"schema": 1, "commit": SHA, "kind": "frontend-prototype"}).encode()
        with patch.object(console, "_LoopbackHTTPS") as connection:
            connection.return_value.getresponse.return_value = Response()
            console._https_health(self.manager, DOMAIN)
            self.assertEqual(connection.call_args.args[:2], (DOMAIN, 443))
            self.assertEqual(connection.return_value.request.call_args.kwargs["headers"]["Host"], DOMAIN)
            connection.return_value.close.assert_called_once()

    def test_loopback_tls_connection_keeps_domain_sni_without_dns_connection(self):
        context = console.ssl.create_default_context()
        connection = console._LoopbackHTTPS(DOMAIN, 443, timeout=10, context=context)
        with patch.object(console.socket, "create_connection") as socket_call, patch.object(
                console.ssl.SSLContext, "wrap_socket") as wrap:
            connection.connect()
        socket_call.assert_called_once_with(("127.0.0.1", 443), 10)
        self.assertEqual(wrap.call_args.kwargs["server_hostname"], DOMAIN)

    @unittest.skipUnless(os.name == "posix", "Real Certbot symlink layout requires Linux")
    def test_legitimate_live_certificate_symlinks_are_accepted(self):
        folders = self.certificates(1)
        path = console._certificate_target(folders, "fullchain.pem")
        self.assertTrue(path.is_symlink())
        self.assertEqual(path.resolve().parent, folders["certs"] / "archive" / console.TLS_NAME)

    @unittest.skipUnless(os.name == "posix", "Certificate symlink boundaries require Linux")
    def test_certificate_symlink_outside_lineage_is_rejected(self):
        folders = self.certificates(1)
        link = folders["certs"] / "live" / console.TLS_NAME / "fullchain.pem"
        link.unlink()
        foreign = self.system / "fullchain1.pem"
        foreign.write_text("foreign pem", encoding="utf-8")
        link.symlink_to(foreign)
        with self.assertRaises(console.ConsoleError):
            console._certificate_target(folders, "fullchain.pem")

    @unittest.skipUnless(os.name == "posix", "Old lineage rollback requires Linux symlinks")
    def test_domain_change_failure_restores_old_links_renewal_tls_config_and_timer(self):
        with self.runtime():
            self.apply()
        original_proxy = self.paths.nginx_proxy.read_bytes()
        original_config = (self.root / "config.json").read_bytes()
        folders = console._tls_directories(self.manager)
        fullchain = folders["certs"] / "live" / console.TLS_NAME / "fullchain.pem"
        old_target = fullchain.resolve()
        renewal = folders["certs"] / "renewal" / (console.TLS_NAME + ".conf")
        old_renewal = renewal.read_bytes()
        old_links = {name: (folders["certs"] / "live" / console.TLS_NAME / (name + ".pem")).resolve()
                     for name in ("cert", "chain", "fullchain", "privkey")}
        self.issue_version = 2
        with self.runtime(health=console.ConsoleError("new domain TLS health failed")), self.assertRaises(console.ConsoleError):
            self.apply("new.example.com")
        self.assertEqual(self.paths.nginx_proxy.read_bytes(), original_proxy)
        self.assertEqual((self.root / "config.json").read_bytes(), original_config)
        self.assertEqual(fullchain.resolve(), old_target)
        for name, target in old_links.items():
            self.assertEqual((folders["certs"] / "live" / console.TLS_NAME / (name + ".pem")).resolve(), target)
        self.assertEqual(renewal.read_bytes(), old_renewal)
        self.assertTrue(self.timer_enabled)
        self.assertTrue(self.timer_active)
        self.assertTrue((folders["certs"] / "archive" / console.TLS_NAME / "fullchain2.pem").exists())

    @unittest.skipUnless(os.name == "posix", "Directory permissions require Linux")
    def test_umask_077_acme_webroot_and_parents_remain_nginx_readable(self):
        old = os.umask(0o077)
        try:
            folders = console._tls_directories(self.manager, create=True)
            for path in (self.root, self.root / "shared", folders["acme"], folders["acme"] / ".well-known",
                         folders["acme"] / ".well-known/acme-challenge"):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(folders["certs"].stat().st_mode), 0o700)
        finally:
            os.umask(old)


if __name__ == "__main__":
    unittest.main()
