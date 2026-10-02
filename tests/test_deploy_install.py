"""Exercise real installer decisions without apt, systemd, or network access.

The decision-stage fixture runs the original script only up to its first root
guard. No production flags or test hooks are added. Linux pty tests keep Bash's
stdin as a script pipe while answers arrive through its controlling terminal.
"""

import errno
import os
from pathlib import Path
import re
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


GUARD = "[[ ${EUID} -eq 0 ]]"
PROBE = """
printf '\\n__INSTALLER_DECISION__:%s|%s|%s|%s|%s|%s|%s|%s|%s\\n' \\
 "$action" "$non_interactive" "$show_after_install" "$installed" \\
 "$listen_port" "$server_name" "$deploy_root" "$confirm_uninstall" "$purge_data"
"""


def installer_source():
    location = Path(__file__).resolve()
    candidates = (location.with_name("install.sh"), location.parent.parent / "install.sh")
    source = next((candidate for candidate in candidates if candidate.is_file()), None)
    if source is None:
        raise RuntimeError("Cannot locate install.sh beside scratch tests or at repository root.")
    return source.read_text(encoding="utf-8")


def decision_stage(source):
    # A missing or ambiguous boundary fails closed instead of executing setup.
    if source.count(GUARD) != 1:
        raise RuntimeError("Installer must have exactly one recognizable root guard.")
    stage = source.split(GUARD, 1)[0]
    if re.search(r"^apt-get(?:\s|$)", stage, flags=re.MULTILINE):
        raise RuntimeError("Package installation appeared before the decision-stage boundary.")
    return stage + PROBE


def management_function(source):
    start = "run_management() ("
    boundary = '\nif [[ "$action" == menu ]]; then'
    if source.count(start) != 1:
        raise RuntimeError("Installer must have exactly one management function.")
    remainder = source.split(start, 1)[1]
    if boundary not in remainder:
        raise RuntimeError("Cannot safely isolate management before the menu decision.")
    definition = start + remainder.split(boundary, 1)[0]
    if not definition.rstrip().endswith(")"):
        raise RuntimeError("The isolated management function has an invalid boundary.")
    return definition


def bash_executable():
    executable = shutil.which("bash")
    if executable:
        return executable
    if os.name == "nt":
        candidate = Path("C:/Program Files/Git/bin/bash.exe")
        if candidate.is_file():
            return str(candidate)
    return None


BASH = bash_executable()


def bash_path(path):
    value = Path(path).resolve().as_posix()
    if os.name == "nt" and re.match(r"^[A-Za-z]:/", value):
        return "/" + value[0].lower() + value[2:]
    return value


def parse_decision(output):
    match = re.search(r"__INSTALLER_DECISION__:([^\r\n]+)", output)
    if not match:
        raise AssertionError("Installer did not finish its decision stage: " + output)
    values = match.group(1).split("|")
    if len(values) != 9:
        raise AssertionError("Unexpected decision probe output.")
    names = ("action", "non_interactive", "show_after_install", "installed", "port", "domain", "path", "confirm", "purge")
    return dict(zip(names, values))


class InstallerFixture(unittest.TestCase):
    def setUp(self):
        self.source = installer_source()
        self.stage = decision_stage(self.source)
        self.temporary = tempfile.TemporaryDirectory(prefix="watch-installer-tests-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        os.chmod(str(self.root), 0o755)
        self.fixture = self.root / "installation"
        self.fixture.mkdir()
        os.chmod(str(self.fixture), 0o755)

    def arguments(self, *extra):
        return ["--path", bash_path(self.fixture)] + list(extra)

    def installed_fixture(self):
        (self.fixture / ".xiaowork-watch-managed").write_text("xiaowork-watch-managed-v1\n", encoding="ascii")
        (self.fixture / ".installation-complete").write_text("xiaowork-watch-managed-v1\n", encoding="ascii")
        tools = self.fixture / "current" / ".deploy"
        tools.mkdir(parents=True)
        # Deliberately an older installation without console.py. Never executed.
        (tools / "manage.py").write_text("# old manager fixture\n", encoding="ascii")

    def run_without_tty(self, source, arguments=(), *, unprivileged=False):
        options = {}
        if os.name == "posix":
            options["start_new_session"] = True
            if unprivileged and os.geteuid() == 0:
                def drop_privileges():
                    os.setgroups([])
                    os.setgid(65534)
                    os.setuid(65534)
                options["preexec_fn"] = drop_privileges
        else:
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
            # Git Bash launched without profiles does not populate its coreutils
            # PATH from a native Windows parent shell.
            source = "export PATH=/usr/bin:/bin:$PATH\n" + source
        return subprocess.run(
            [BASH, "--noprofile", "--norc", "-s", "--"] + list(arguments),
            input=source.encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=10, **options)

    def combined(self, result):
        return (result.stdout + result.stderr).decode("utf-8", errors="replace")


@unittest.skipUnless(BASH, "A Bash executable is required")
class InstallerDecisions(InstallerFixture):
    def test_default_without_terminal_never_silently_deploys(self):
        result = self.run_without_tty(self.source, self.arguments(), unprivileged=True)
        output = self.combined(result)
        self.assertEqual(result.returncode, 2, output)
        self.assertIn("--non-interactive", output)
        self.assertNotIn("__INSTALLER_DECISION__", output)

    def test_configuration_options_do_not_imply_install_consent(self):
        result = self.run_without_tty(self.stage, self.arguments("--port", "9090", "--domain", "watch.example.com"))
        self.assertEqual(result.returncode, 2, self.combined(result))
        self.assertIn("--non-interactive", self.combined(result))

    def test_installed_default_also_requires_terminal(self):
        self.installed_fixture()
        result = self.run_without_tty(self.stage, self.arguments())
        self.assertEqual(result.returncode, 2, self.combined(result))
        self.assertIn("--non-interactive", self.combined(result))

    def test_non_interactive_preserves_port_domain_and_path_arguments(self):
        result = self.run_without_tty(self.stage, self.arguments("--non-interactive", "--port", "9090", "--domain", "watch.example.com"))
        self.assertEqual(result.returncode, 0, self.combined(result))
        decision = parse_decision(self.combined(result))
        self.assertEqual(decision["action"], "deploy")
        self.assertEqual(decision["non_interactive"], "true")
        self.assertEqual(decision["port"], "9090")
        self.assertEqual(decision["domain"], "watch.example.com")
        self.assertEqual(decision["path"], bash_path(self.fixture))
        self.assertEqual(decision["show_after_install"], "false")

    def test_explicit_install_does_not_require_menu_terminal(self):
        result = self.run_without_tty(self.stage, self.arguments("--install"))
        self.assertEqual(result.returncode, 0, self.combined(result))
        self.assertEqual(parse_decision(self.combined(result))["action"], "deploy")

    def test_explicit_uninstall_and_confirmation_are_parsed(self):
        result = self.run_without_tty(self.stage, self.arguments("--uninstall", "--non-interactive", "--confirm"))
        self.assertEqual(result.returncode, 0, self.combined(result))
        decision = parse_decision(self.combined(result))
        self.assertEqual(decision["action"], "uninstall")
        self.assertEqual(decision["confirm"], "true")
        self.assertEqual(decision["purge"], "false")

    def test_purge_is_only_accepted_for_explicit_uninstall(self):
        result = self.run_without_tty(self.stage, self.arguments("--uninstall", "--purge", "--confirm"))
        self.assertEqual(result.returncode, 0, self.combined(result))
        self.assertEqual(parse_decision(self.combined(result))["purge"], "true")
        for flags in (("--purge",), ("--install", "--purge"), ("--non-interactive", "--purge")):
            with self.subTest(flags=flags):
                result = self.run_without_tty(self.stage, self.arguments(*flags))
                self.assertEqual(result.returncode, 2, self.combined(result))
                self.assertNotIn("__INSTALLER_DECISION__", self.combined(result))

    def test_installed_non_interactive_maps_to_update_deploy_action(self):
        self.installed_fixture()
        result = self.run_without_tty(self.stage, self.arguments("--non-interactive"))
        self.assertEqual(result.returncode, 0, self.combined(result))
        decision = parse_decision(self.combined(result))
        self.assertEqual(decision["installed"], "true")
        self.assertEqual(decision["action"], "deploy")

    def test_help_works_without_root_and_without_terminal(self):
        result = self.run_without_tty(self.source, ["--help"], unprivileged=True)
        self.assertEqual(result.returncode, 0, self.combined(result))
        self.assertIn("--non-interactive", self.combined(result))
        self.assertIn("--uninstall", self.combined(result))

    def test_short_help_also_precedes_parameter_validation(self):
        result = self.run_without_tty(self.source, ["--port", "invalid", "-h"], unprivileged=True)
        self.assertEqual(result.returncode, 0, self.combined(result))
        self.assertIn("--install", self.combined(result))

    def test_invalid_arguments_fail_before_any_setup(self):
        cases = [("--unknown",), ("--port",), ("--port", "0"), ("--port", "65536"), ("--domain", "bad/domain"), ("--path", "/tmp/../outside")]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                result = self.run_without_tty(self.stage, self.arguments(*arguments))
                self.assertEqual(result.returncode, 2, self.combined(result))
                self.assertNotIn("__INSTALLER_DECISION__", self.combined(result))

    def test_decision_stage_creates_no_installation_files(self):
        result = self.run_without_tty(self.stage, self.arguments("--non-interactive"))
        self.assertEqual(result.returncode, 0, self.combined(result))
        self.assertEqual(list(self.fixture.iterdir()), [])

    @unittest.skipUnless(sys.platform.startswith("linux"), "Published-tool selection fixture requires Linux")
    def test_uninstall_purge_uses_downloaded_tools_even_with_old_control_and_current(self):
        # Both installed dispatch branches are valid. Uninstall must bypass
        # them so cleanup fixes can work without a successful website update.
        for name, sha in (("control", "a" * 40), ("current", "b" * 40)):
            deployed = self.fixture / "releases" / sha / ".deploy"
            deployed.mkdir(parents=True)
            (deployed / "manage.py").write_text("OLD_INSTALLED_MANAGER\n", encoding="ascii")
            (deployed / "console.py").write_text("# old console\n", encoding="ascii")
            target = Path("releases") / sha
            if name == "control":
                target = target / ".deploy"
            (self.fixture / name).symlink_to(target, target_is_directory=True)

        binaries = self.root / "fake-bin"
        binaries.mkdir()
        downloaded = self.root / "downloaded-management"
        downloaded.mkdir()
        python_stub = binaries / "python3"
        python_stub.write_text("""#!/bin/sh
manager=$1
shift
IFS= read -r kind < "$manager"
printf '__MANAGER_KIND__:%s\\n' "$kind"
printf '__MANAGER_PATH__:%s\\n' "$manager"
printf '__MANAGER_ARGS__:'
for value in "$@"; do printf '%s|' "$value"; done
printf '\\n'
""", encoding="utf-8")
        # Production uses a fixed /tmp template; keep the fixture's temporary
        # directory inside this test rather than creating system-wide files.
        mktemp_stub = binaries / "mktemp"
        mktemp_stub.write_text("""#!/bin/sh
[ "$#" -eq 2 ] && [ "$1" = '-d' ] && [ "$2" = '/tmp/xiaowork-watch-menu.XXXXXXXX' ] || exit 91
printf '%s\\n' "$WATCH_TEST_DOWNLOAD_DIR"
""", encoding="utf-8")
        os.chmod(str(python_stub), 0o755)
        os.chmod(str(mktemp_stub), 0o755)
        harness = """set -Eeuo pipefail
deploy_root=$1
export PATH="$2:$PATH"
export WATCH_TEST_DOWNLOAD_DIR=$3
load_latest_scripts() {
 printf '__DOWNLOADED_TO__:%s\\n' "$1"
 printf 'LATEST_PUBLISHED_MANAGER\\n' > "$1/manage.py"
 printf '# published console fixture\\n' > "$1/console.py"
}
""" + management_function(self.source) + "\nrun_management uninstall --confirm --purge\n"
        result = subprocess.run(
            [BASH, "--noprofile", "--norc", "-s", "--", str(self.fixture), str(binaries), str(downloaded)],
            input=harness.encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True, timeout=5)
        output = self.combined(result)
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("__DOWNLOADED_TO__:" + str(downloaded), output)
        self.assertIn("__MANAGER_KIND__:LATEST_PUBLISHED_MANAGER", output)
        self.assertNotIn("__MANAGER_KIND__:OLD_INSTALLED_MANAGER", output)
        self.assertIn("__MANAGER_PATH__:" + str(downloaded / "manage.py"), output)
        self.assertIn("__MANAGER_ARGS__:--root|" + str(self.fixture) + "|uninstall|--confirm|--purge|", output)
        self.assertEqual(output.count("__MANAGER_PATH__:"), 1)
        for name in ("control", "current"):
            self.assertTrue((self.fixture / name).is_symlink())
        for sha in ("a" * 40, "b" * 40):
            self.assertEqual((self.fixture / "releases" / sha / ".deploy/manage.py").read_text(encoding="ascii"), "OLD_INSTALLED_MANAGER\n")


@unittest.skipUnless(sys.platform.startswith("linux") and BASH, "Linux controlling-pty semantics are required")
class InstallerTerminalDecisions(InstallerFixture):
    def run_with_terminal(self, answer, arguments=()):
        import fcntl
        import pty
        import termios

        master, slave = pty.openpty()

        def controlling_terminal():
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

        process = subprocess.Popen(
            [BASH, "--noprofile", "--norc", "-s", "--"] + list(arguments),
            stdin=subprocess.PIPE, stdout=slave, stderr=slave,
            preexec_fn=controlling_terminal, pass_fds=(slave,), close_fds=True)
        os.close(slave)
        output = bytearray()
        answered = False
        deadline = time.monotonic() + 10
        try:
            # This is the curl | bash shape: stdin contains script source, while
            # /dev/tty is an independent controlling terminal for the menu.
            process.stdin.write(self.stage.encode("utf-8"))
            process.stdin.close()
            while time.monotonic() < deadline:
                readable, _, _ = select.select([master], [], [], 0.1)
                if readable:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError as error:
                        if error.errno == errno.EIO:
                            break
                        raise
                    if not chunk:
                        break
                    output.extend(chunk)
                if not answered and "请选择".encode("utf-8") in output:
                    os.write(master, answer)
                    answered = True
                if process.poll() is not None and not readable:
                    break
            try:
                # A closing pty may report EIO just before the child is reaped.
                code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                raise AssertionError("Menu did not finish: " + output.decode("utf-8", errors="replace"))
            return code, output.decode("utf-8", errors="replace"), answered
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
            os.close(master)

    def test_first_deploy_choice_uses_terminal_without_consuming_script_pipe(self):
        code, output, answered = self.run_with_terminal(b"1\n", self.arguments())
        self.assertTrue(answered, output)
        self.assertEqual(code, 0, output)
        decision = parse_decision(output)
        self.assertEqual(decision["action"], "deploy")
        self.assertEqual(decision["show_after_install"], "true")
        self.assertEqual(list(self.fixture.iterdir()), [])

    def test_first_uninstall_choice_does_not_run_setup(self):
        code, output, answered = self.run_with_terminal(b"2\n", self.arguments())
        self.assertTrue(answered, output)
        self.assertEqual(code, 0, output)
        decision = parse_decision(output)
        self.assertEqual(decision["action"], "uninstall")
        self.assertEqual(decision["show_after_install"], "false")
        self.assertEqual(decision["purge"], "true")
        self.assertEqual(list(self.fixture.iterdir()), [])

    def test_invalid_selection_reprompts_before_valid_choice(self):
        code, output, _ = self.run_with_terminal(b"9\n2\n", self.arguments())
        self.assertEqual(code, 0, output)
        self.assertIn("请输入 0、1 或 2", output)
        self.assertEqual(parse_decision(output)["action"], "uninstall")

    def test_exit_and_empty_selection_cancel_without_setup(self):
        for answer in (b"0\n", b"\n"):
            with self.subTest(answer=answer):
                code, output, answered = self.run_with_terminal(answer, self.arguments())
                self.assertTrue(answered, output)
                self.assertEqual(code, 0, output)
                self.assertNotIn("__INSTALLER_DECISION__", output)
                self.assertEqual(list(self.fixture.iterdir()), [])

    def test_terminal_eof_cancels_without_setup(self):
        code, output, answered = self.run_with_terminal(b"\x04", self.arguments())
        self.assertTrue(answered, output)
        self.assertEqual(code, 0, output)
        self.assertNotIn("__INSTALLER_DECISION__", output)
        self.assertEqual(list(self.fixture.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
