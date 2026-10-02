"""Run only the installer's short-command block against a temporary bin directory."""

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


BEGIN = 'if [[ ! -e "$shortcut" && ! -L "$shortcut" ]]; then'
END = '[[ ! -e "$service" && ! -L "$service" ]]'


def shortcut_block():
    source = (Path(__file__).resolve().parent.parent / "install.sh").read_text(encoding="utf-8")
    if source.count(BEGIN) != 1:
        raise RuntimeError("Cannot unambiguously locate the short-command installation block")
    remainder = source.split(BEGIN, 1)[1]
    if END not in remainder:
        raise RuntimeError("Cannot isolate the short-command block before service creation")
    block = BEGIN + remainder.split(END, 1)[0]
    if not block.rstrip().endswith("fi"):
        raise RuntimeError("The extracted short-command block has an invalid boundary")
    # Relocate its two fixed system paths only; execute no package/service setup.
    template = "/usr/local/bin/.xiaowork-watch-shortcut.XXXXXXXX"
    command = 'exec /usr/local/bin/xiaowork-watch "\\$@"'
    if block.count(template) != 1 or block.count(command) != 1:
        raise RuntimeError("The short-command paths changed; review this isolated fixture")
    return block.replace(template, '"$fixture_bin/.xiaowork-watch-shortcut.XXXXXXXX"').replace(
        command, 'exec "$fixture_bin/xiaowork-watch" "\\$@"')


def bash_executable():
    if os.name == "nt":
        candidate = Path("C:/Program Files/Git/bin/bash.exe")
        return str(candidate) if candidate.is_file() else None
    return shutil.which("bash")


def bash_path(path):
    # Keep the lexical destination: resolving xw would hide a dangling/foreign link.
    value = Path(path).absolute().as_posix()
    if os.name == "nt" and re.match(r"^[A-Za-z]:/", value):
        return "/" + value[0].lower() + value[2:]
    return value


BASH = bash_executable()


@unittest.skipUnless(BASH, "A Bash executable is required")
class InstallerShortcut(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="watch-shortcut-tests-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        # A space in this relocated path also verifies quoting in the wrapper.
        self.bin = self.root / "fixture bin"
        self.bin.mkdir()
        self.shortcut = self.bin / "xw"
        self.wrapper = self.bin / "xiaowork-watch"
        self.wrapper.write_text(
            '#!/bin/sh\nprintf "__ARG__:<%s>\\n" "$@"\n', encoding="utf-8")
        os.chmod(self.wrapper, 0o755)
        self.deploy_root = self.root / "installation"

    def shell(self, source, *arguments):
        source = "export PATH=/usr/bin:/bin:$PATH\n" + source
        result = subprocess.run(
            [BASH, "--noprofile", "--norc", "-s", "--", *arguments],
            input=source.encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=10)
        output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
        self.assertEqual(result.returncode, 0, output)
        return output

    def install_shortcut(self, intercept=""):
        harness = (
            'set -Eeuo pipefail\nshortcut=$1\ndeploy_root=$2\nfixture_bin=$3\n'
            "shortcut_written=false\nshortcut_temp=''\n" + intercept + shortcut_block()
            + '\nprintf "__SHORTCUT_STATE__:%s|%s\\n" "$shortcut_written" "$shortcut_temp"\n')
        output = self.shell(harness, bash_path(self.shortcut), bash_path(self.deploy_root), bash_path(self.bin))
        self.assertEqual(list(self.bin.glob(".xiaowork-watch-shortcut.*")), [])
        return output

    def assert_not_created(self, output):
        self.assertIn("__SHORTCUT_STATE__:false|", output)
        self.assertIn("短命令 xw 已被其他程序占用", output)

    def create_symlink(self, destination):
        try:
            self.shortcut.symlink_to(destination)
        except (NotImplementedError, OSError) as error:
            self.skipTest("This platform does not support fixture symlinks: " + str(error))

    def test_creates_owned_executable_and_removes_staging_file(self):
        output = self.install_shortcut()
        self.assertIn("__SHORTCUT_STATE__:true|", output)
        expected = ('#!/bin/sh\n# Managed by xiaowork Watch\'s frontend installer.\n'
                    '# xiaowork-watch-root: ' + bash_path(self.deploy_root) + '\n'
                    'exec "' + bash_path(self.wrapper) + '" "$@"\n')
        self.assertEqual(self.shortcut.read_text(encoding="utf-8"), expected)
        self.assertFalse(self.shortcut.is_symlink())
        mode = self.shell('stat -c %a -- "$1"\n', bash_path(self.shortcut)).strip()
        self.assertEqual(mode, "755")

    def test_passes_all_arguments_to_the_long_command_without_expansion(self):
        self.install_shortcut()
        arguments = ["menu", "with spaces", "--domain", "watch.example.com", "literal $HOME;$(echo nope)"]
        output = self.shell('shortcut=$1\nshift\n"$shortcut" "$@"\n', bash_path(self.shortcut), *arguments)
        self.assertEqual(output.splitlines(), ["__ARG__:<" + item + ">" for item in arguments])

    def test_existing_foreign_file_is_preserved(self):
        self.shortcut.write_bytes(b"foreign command\n")
        before = self.shortcut.stat()
        self.assert_not_created(self.install_shortcut())
        self.assertEqual(self.shortcut.read_bytes(), b"foreign command\n")
        self.assertEqual(self.shortcut.stat().st_ino, before.st_ino)

    def test_existing_foreign_symlink_is_preserved(self):
        destination = self.bin / "foreign"
        destination.write_bytes(b"foreign destination\n")
        self.create_symlink(destination)
        self.assert_not_created(self.install_shortcut())
        self.assertTrue(self.shortcut.is_symlink())
        self.assertEqual(os.readlink(self.shortcut), str(destination))
        self.assertEqual(destination.read_bytes(), b"foreign destination\n")

    def test_existing_dangling_symlink_is_preserved(self):
        destination = self.bin / "does-not-exist"
        self.create_symlink(destination)
        self.assert_not_created(self.install_shortcut())
        self.assertTrue(self.shortcut.is_symlink())
        self.assertEqual(os.readlink(self.shortcut), str(destination))
        self.assertFalse(destination.exists())

    def test_existing_directory_is_preserved(self):
        self.shortcut.mkdir()
        original = self.shortcut / "foreign.txt"
        original.write_bytes(b"foreign directory\n")
        self.assert_not_created(self.install_shortcut())
        self.assertEqual(list(self.shortcut.iterdir()), [original])
        self.assertEqual(original.read_bytes(), b"foreign directory\n")

    def test_file_created_between_precheck_and_link_is_not_overwritten(self):
        intercept = ('ln() { local destination="${@: -1}"; printf "racing foreign command\\n" > "$destination";'
                     ' command ln "$@"; }\n')
        self.assert_not_created(self.install_shortcut(intercept))
        self.assertEqual(self.shortcut.read_bytes(), b"racing foreign command\n")

    def test_directory_created_between_precheck_and_link_cannot_receive_a_staged_command(self):
        intercept = 'ln() { local destination="${@: -1}"; mkdir -- "$destination"; command ln "$@"; }\n'
        self.assert_not_created(self.install_shortcut(intercept))
        self.assertTrue(self.shortcut.is_dir())
        self.assertEqual(list(self.shortcut.iterdir()), [])

    def test_failed_link_removes_staging_file_without_claiming_creation(self):
        self.assert_not_created(self.install_shortcut("ln() { return 1; }\n"))
        self.assertFalse(self.shortcut.exists())


if __name__ == "__main__":
    unittest.main()
