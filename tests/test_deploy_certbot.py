"""Certbot isolation regression tests; Linux uses apt modules, never an ACME server."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "console.py"
if not SOURCE.exists():
    SOURCE = HERE.parent / "scripts" / "deploy" / "console.py"
SPEC = importlib.util.spec_from_file_location("deploy_certbot_console", str(SOURCE))
console = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = console
SPEC.loader.exec_module(console)


@contextlib.contextmanager
def fake_certbot(defaults):
    package = types.ModuleType("certbot")
    package.__path__ = []
    internal = types.ModuleType("certbot._internal")
    internal.__path__ = []
    constants = types.ModuleType("certbot._internal.constants")
    constants.CLI_DEFAULTS = defaults
    main = types.ModuleType("certbot._internal.main")
    main.main = Mock(return_value=23)
    internal.constants, internal.main = constants, main
    package._internal = internal
    modules = {module.__name__: module for module in (package, internal, constants, main)}
    with patch.dict(sys.modules, modules):
        yield constants, main.main


class IsolatedRunnerTests(unittest.TestCase):
    def test_actual_config_files_key_is_cleared_without_mutating_other_defaults(self):
        for paths in (["global.ini", "user.ini"], ("global.ini", "user.ini")):
            with self.subTest(paths=paths):
                original = {"config_files": paths, "server": "existing-default", "domains": ["keep.example"]}
                with fake_certbot(original) as (constants, main), patch.dict(os.environ, {"CERTBOT_SNAPPED": "True"}):
                    def execute(arguments):
                        self.assertEqual(arguments, ["certificates"])
                        self.assertEqual(constants.CLI_DEFAULTS["config_files"], [])
                        self.assertNotIn("CERTBOT_SNAPPED", os.environ)
                        return 23
                    main.side_effect = execute
                    self.assertEqual(console._isolated_certbot_main(["certificates"]), 23)
                    self.assertIsNot(constants.CLI_DEFAULTS, original)
                    self.assertIs(constants.CLI_DEFAULTS["domains"], original["domains"])
                    self.assertEqual(constants.CLI_DEFAULTS["server"], "existing-default")
                    self.assertEqual(original["config_files"], paths)
                    main.assert_called_once_with(["certificates"])

    def test_unsupported_schema_fails_before_main(self):
        for defaults in (None, [], {}, {"config-files": []}, {"config_file": []},
                         {"config_files": None}, {"config_files": "global.ini"}, {"config_files": set()}):
            with self.subTest(defaults=defaults), fake_certbot(defaults) as (constants, main):
                with self.assertRaises(console.ConsoleError):
                    console._isolated_certbot_main(["certificates"])
                main.assert_not_called()
                self.assertIs(constants.CLI_DEFAULTS, defaults)

    def test_missing_apt_module_reports_error_without_cli_execution(self):
        import builtins
        original_import = builtins.__import__
        def missing(name, *arguments, **options):
            if name == "certbot._internal":
                raise ImportError("fixture missing apt module")
            return original_import(name, *arguments, **options)
        with patch.object(builtins, "__import__", side_effect=missing), self.assertRaises(console.ConsoleError):
            console._isolated_certbot_main(["--version"])

    def test_certbot_subprocess_uses_fixed_system_python_and_isolated_flag(self):
        arguments = ["renew", "--pre-hook", "", "--cert-name", "literal;$(value)"]
        with patch.dict(os.environ, {"CERTBOT_SNAPPED": "True"}), patch.object(console, "_run", return_value="ok") as run:
            self.assertEqual(console._run_certbot(arguments, timeout=47), "ok")
            self.assertEqual(os.environ["CERTBOT_SNAPPED"], "True")
        run.assert_called_once_with(
            ["/usr/bin/python3", "-I", str(SOURCE.resolve()), "--isolated-certbot"] + arguments,
            timeout=47, display_arguments=["certbot"] + arguments)

    def test_certbot_availability_probes_the_same_isolated_runner(self):
        with patch.object(console, "_run_certbot", return_value="certbot version") as run:
            self.assertTrue(console._certbot_available())
        run.assert_called_once_with(["--version"], timeout=30)
        with patch.object(console, "_run_certbot", side_effect=console.ConsoleError("unavailable")):
            self.assertFalse(console._certbot_available())


def _real_worker(base, mode):
    """Child-only sandbox: map default ini paths into a fixture, audit all I/O."""
    from certbot._internal import cli, constants, main, renewal
    from certbot._internal.plugins import disco
    base = Path(base).resolve()
    global_ini = base / "system" / "cli.ini"
    user_ini = base / "xdg" / "letsencrypt" / "cli.ini"
    # Keep the real XDG lookup and replace only /etc's entry. No real system ini
    # is opened or modified, even in the intentionally unisolated control.
    default_paths = list(constants.CLI_DEFAULTS["config_files"])
    assert len(default_paths) == 2, default_paths
    assert Path(os.path.expanduser(default_paths[1])).resolve() == user_ini
    constants.CLI_DEFAULTS = dict(constants.CLI_DEFAULTS, config_files=[str(global_ini), default_paths[1]])
    folders = {name: base / "managed" / name for name in ("certs", "work", "logs")}
    arguments = ["renew" if mode == "renew" else "certificates"] + console._certbot_arguments(folders)
    arguments += ["--non-interactive", "--no-random-sleep-on-renew"]
    watched = {str(global_ini), str(user_ini)}
    reads, denied = [], []
    phase = ["operation"]

    def inside(path):
        if isinstance(path, int):
            return
        resolved = Path(os.fsdecode(path)).resolve()
        if resolved != Path("/dev/null") and resolved != base and base not in resolved.parents:
            denied.append("outside write: " + str(resolved))
            raise AssertionError(denied[-1])

    def audit(event, values):
        if event == "open":
            path, text_mode, flags = values
            if not isinstance(path, int):
                resolved = str(Path(os.fsdecode(path)).resolve())
                if phase[0] == "operation" and resolved in watched:
                    reads.append(resolved)
                if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                    inside(path)
        elif event in {"socket.connect", "socket.bind", "socket.getaddrinfo", "socket.gethostbyname",
                       "socket.gethostbyaddr", "subprocess.Popen", "os.system"}:
            denied.append(event)
            raise AssertionError("Network and external commands are forbidden: " + event)
        elif event in {"os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.chown"}:
            inside(values[0])
        elif event in {"os.rename", "os.link", "os.symlink"}:
            inside(values[0])
            inside(values[1])

    sys.addaudithook(audit)
    if mode == "control":
        # Prove both mapped defaults really are parser inputs without isolation.
        for path in (global_ini, user_ini):
            constants.CLI_DEFAULTS = dict(constants.CLI_DEFAULTS, config_files=[str(path)])
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                try:
                    cli.prepare_and_parse_args(disco.PluginsRegistry.find_all(), list(arguments))
                except SystemExit as error:
                    assert error.code not in (0, None), error.code
                else:
                    raise AssertionError("Default parser accepted the intentionally invalid cli.ini")
            assert "xiaowork-fixture-invalid-option" in errors.getvalue(), errors.getvalue()
        assert set(reads) == watched, reads
    elif mode == "restore":
        def verify_restore(actual_arguments):
            registry = disco.PluginsRegistry.find_all()
            config = cli.prepare_and_parse_args(registry, list(actual_arguments))
            legacy = {"pre_hook": "/bin/false legacy-pre", "post_hook": "/bin/false legacy-post",
                      "renew_hook": "/bin/false legacy-deploy"}
            for name in legacy:
                assert config.set_by_user(name), name
            renewal.restore_required_config_elements(config, legacy)
            for name in ("pre_hook", "post_hook", "renew_hook", "deploy_hook"):
                assert getattr(config, name) == "", (name, getattr(config, name))
            assert config.directory_hooks is False
            # Positive control: real renewal restoration would revive the old
            # hooks if the explicit empty command-line overrides were removed.
            without_hooks = []
            iterator = iter(actual_arguments)
            for argument in iterator:
                if argument in ("--pre-hook", "--post-hook", "--deploy-hook"):
                    next(iterator)
                else:
                    without_hooks.append(argument)
            config = cli.prepare_and_parse_args(registry, list(without_hooks))
            renewal.restore_required_config_elements(config, legacy)
            for name, command in legacy.items():
                assert getattr(config, name) == command, name
            return 0
        arguments[0] = "renew"
        with patch.object(main, "main", side_effect=verify_restore):
            assert console._isolated_certbot_main(arguments) == 0
        assert not reads, reads
    else:
        # Execute the production script's real --isolated-certbot entry point,
        # retaining the same process so the audit guard covers all Certbot I/O.
        with patch.object(sys, "argv", [str(SOURCE), "--isolated-certbot"] + arguments):
            try:
                runpy.run_path(str(SOURCE), run_name="__main__")
            except SystemExit as error:
                assert error.code in (0, None), error.code
            else:
                raise AssertionError("Certbot script did not finish its CLI entry point")
        assert constants.CLI_DEFAULTS["config_files"] == []
        assert "CERTBOT_SNAPPED" not in os.environ
        assert not reads, reads
    phase[0] = "verification"
    assert not denied, denied
    assert not (base / "hook-executed").exists()
    print("CERTBOT_FIXTURE_RESULT=" + json.dumps({"mode": mode, "reads": reads, "denied": denied}))


@unittest.skipUnless(sys.platform == "linux", "Real system apt Certbot tests require Linux")
class RealCertbotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="xiaowork-certbot-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.global_ini = self.base / "system" / "cli.ini"
        self.user_ini = self.base / "xdg" / "letsencrypt" / "cli.ini"
        hook = self.base / "sentinel-hook"
        hook.write_text("#!/bin/sh\nprintf unsafe > '" + str(self.base / "hook-executed") + "'\n", encoding="utf-8")
        hook.chmod(0o700)
        contents = ("xiaowork-fixture-invalid-option = true\npre-hook = " + str(hook)
                    + "\npost-hook = " + str(hook) + "\ndeploy-hook = " + str(hook) + "\n")
        for path in (self.global_ini, self.user_ini):
            path.parent.mkdir(parents=True)
            path.write_text(contents, encoding="utf-8")
            path.chmod(0o600)
        # Directory hooks are executable but must also be ignored.
        for kind in ("pre", "post", "deploy"):
            directory = self.base / "managed" / "certs" / "renewal-hooks" / kind
            directory.mkdir(parents=True)
            target = directory / "fixture"
            target.write_bytes(hook.read_bytes())
            target.chmod(0o700)
        for folder in (self.base / "home", self.base / "tmp"):
            folder.mkdir()
        self.snapshots = {path: (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
                          for path in (self.global_ini, self.user_ini)}

    def worker(self, mode):
        environment = os.environ.copy()
        environment.update(HOME=str(self.base / "home"), XDG_CONFIG_HOME=str(self.base / "xdg"),
                           TMPDIR=str(self.base / "tmp"), CERTBOT_SNAPPED="True")
        # -I intentionally ignores PYTHONPATH/user packages. Missing apt modules
        # are a test failure on Linux, not a silently skipped integration check.
        result = subprocess.run(["/usr/bin/python3", "-I", "-B", str(Path(__file__).resolve()),
                                 "--certbot-fixture", str(self.base), mode],
                                cwd=str(self.base), env=environment, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = [line for line in result.stdout.splitlines() if line.startswith("CERTBOT_FIXTURE_RESULT=")]
        self.assertEqual(len(lines), 1, result.stdout + result.stderr)
        report = json.loads(lines[0].split("=", 1)[1])
        self.assertEqual(report["denied"], [])
        self.assertFalse((self.base / "hook-executed").exists())
        for path, snapshot in self.snapshots.items():
            self.assertEqual((path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns), snapshot)
        return report

    def test_positive_control_default_parser_rejects_each_existing_invalid_ini(self):
        self.assertEqual(set(self.worker("control")["reads"]), {str(self.global_ini), str(self.user_ini)})

    def test_isolated_actual_certificates_cli_ignores_existing_ini_and_hooks(self):
        self.assertEqual(self.worker("certificates")["reads"], [])

    def test_isolated_actual_empty_renew_cli_ignores_existing_ini_and_hooks(self):
        self.assertEqual(self.worker("renew")["reads"], [])

    def test_real_parser_empty_hooks_survive_legacy_renewal_restore(self):
        self.assertEqual(self.worker("restore")["reads"], [])


if __name__ == "__main__":
    if sys.argv[1:2] == ["--certbot-fixture"]:
        _real_worker(sys.argv[2], sys.argv[3])
    else:
        unittest.main()
