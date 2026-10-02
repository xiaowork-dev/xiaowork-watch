"""Offline UTF-8 terminal regressions; only the Linux case creates a test pty."""
import contextlib
import errno
import importlib.util
import io
import os
from pathlib import Path
import select
import subprocess
import sys
import time
import unittest
from unittest.mock import patch


HERE = Path(__file__).resolve().parent
SOURCE = HERE / "console.py"
if not SOURCE.exists():
    SOURCE = HERE.parent / "scripts" / "deploy" / "console.py"
SPEC = importlib.util.spec_from_file_location("deploy_terminal_console", str(SOURCE))
console = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = console
SPEC.loader.exec_module(console)


@contextlib.contextmanager
def byte_terminal(payload):
    # Actual UTF-8 decoding, with separate byte streams for input and prompts.
    output = io.BytesIO()
    terminal = io.TextIOWrapper(io.BufferedRWPair(io.BytesIO(payload), output),
                                encoding="utf-8", errors="replace", write_through=True)
    try:
        yield terminal, output
    finally:
        terminal.close()


class TerminalEncodingTests(unittest.TestCase):
    def test_read_ahead_of_bad_email_does_not_interrupt_a_valid_ascii_domain(self):
        payload = b"watch.xiaoworks.com\nbad\xe3x\nadmin@example.com\nYES\nUNINSTALL\n"
        with byte_terminal(payload) as (terminal, output):
            self.assertEqual(console._ask(terminal, "域名："), "watch.xiaoworks.com")
            self.assertEqual(output.getvalue().decode("utf-8"), "域名：")
            self.assertEqual(console._ask(terminal, "邮箱："), "admin@example.com")
            self.assertEqual(console._ask(terminal, "HTTPS 确认："), "YES")
            self.assertEqual(console._ask(terminal, "卸载确认："), "UNINSTALL")
            self.assertEqual(output.getvalue().decode("utf-8").count("邮箱："), 2)

    def test_bad_domain_line_retries_and_preserves_next_domain_menu_and_eof(self):
        prompt = "请输入域名："
        with byte_terminal(b"bad\xff.example\nwatch.example.com\n0\n") as (terminal, output):
            self.assertEqual(console._ask(terminal, prompt), "watch.example.com")
            self.assertEqual(console._ask(terminal, "请选择："), "0")
            self.assertIsNone(console._ask(terminal, "下一行："))
            text = output.getvalue().decode("utf-8")
            self.assertEqual(text.count(prompt), 2)
            self.assertTrue(text.split(prompt)[1].strip(), "Rejected input should display a retry explanation")

    def test_invalid_bytes_cannot_be_ignored_into_yes_or_uninstall(self):
        cases = ((b"Y\xffES\nNO\n", "NO"),
                 (b"YES\x80\n0\n", "0"),
                 (b"UN\xfeINSTALL\n\n", ""),
                 (b"UNINSTALL\xff\n", None))
        for payload, expected in cases:
            with self.subTest(payload=payload), byte_terminal(payload) as (terminal, output):
                value = console._ask(terminal, "确认：")
                self.assertEqual(value, expected)
                self.assertNotIn(value, ("YES", "UNINSTALL"))
                self.assertEqual(output.getvalue().decode("utf-8").count("确认："), 2)

    def test_multiple_malformed_lines_retry_until_a_complete_valid_line(self):
        with byte_terminal(b"\xff\n\xe4\xb8\n7\n") as (terminal, output):
            self.assertEqual(console._ask(terminal, "选择："), "7")
            self.assertEqual(output.getvalue().decode("utf-8").count("选择："), 3)

    def test_valid_utf8_remains_intact_and_does_not_reprompt(self):
        with byte_terminal("  中文输入 · 测试\n".encode("utf-8")) as (terminal, output):
            self.assertEqual(console._ask(terminal, "输入："), "中文输入 · 测试")
            self.assertEqual(output.getvalue().decode("utf-8"), "输入：")

    def test_open_terminal_uses_replacement_decoding_in_both_open_branches(self):
        for force_fallback in (False, True):
            with self.subTest(fallback=force_fallback):
                output = io.BytesIO()
                binary = io.BufferedRWPair(io.BytesIO(b"bad\xff\n0\n"), output)

                def open_fixture(path, mode, **options):
                    self.assertEqual(path, "/dev/tty")
                    if mode == "r+":
                        if force_fallback:
                            raise io.UnsupportedOperation("non-seekable fixture tty")
                        return io.TextIOWrapper(binary, encoding=options.get("encoding"),
                                                errors=options.get("errors", "strict"), write_through=True)
                    self.assertEqual(mode, "r+b")
                    self.assertEqual(options.get("buffering"), 0)
                    return binary

                with patch.object(console, "open", side_effect=open_fixture, create=True) as opened:
                    with console._open_terminal() as terminal:
                        self.assertEqual(terminal.encoding, "utf-8")
                        self.assertEqual(terminal.errors, "replace")
                        self.assertEqual(console._ask(terminal, "选择："), "0")
                    self.assertEqual(opened.call_count, 2 if force_fallback else 1)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Real controlling-pty regression requires Linux")
    def test_real_tty_survives_prefilled_bad_bytes_without_consuming_script_stdin(self):
        import fcntl
        import pty
        import termios

        master, slave = pty.openpty()
        process = None
        try:
            attributes = termios.tcgetattr(slave)
            attributes[3] &= ~termios.ECHO
            termios.tcsetattr(slave, termios.TCSANOW, attributes)
            os.write(master, b"bad\xff.example\nwatch.example.com\n0\n")

            def controlling_terminal():
                os.setsid()
                fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

            child = """import importlib.util, sys
spec = importlib.util.spec_from_file_location('real_tty_console', sys.argv[1])
console = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = console
spec.loader.exec_module(console)
with console._open_terminal() as terminal:
    domain = console._ask(terminal, 'DOMAIN> ')
    choice = console._ask(terminal, 'MENU> ')
assert domain == 'watch.example.com', repr(domain)
assert choice == '0', repr(choice)
assert sys.stdin.buffer.read() == b'SCRIPT_STDIN_MUST_REMAIN_UNREAD\\n'
print('__TTY_RESULT__:watch.example.com|0|STDIN_UNTOUCHED', flush=True)
"""
            deadline = time.monotonic() + 4.0
            process = subprocess.Popen(
                [sys.executable, "-c", child, str(SOURCE)], stdin=subprocess.PIPE,
                stdout=slave, stderr=slave, preexec_fn=controlling_terminal,
                pass_fds=(slave,), close_fds=True)
            os.close(slave)
            slave = None
            process.communicate(input=b"SCRIPT_STDIN_MUST_REMAIN_UNREAD\n",
                                timeout=max(0.01, deadline - time.monotonic()))
            data = bytearray()
            while select.select([master], [], [], 0)[0]:
                try:
                    chunk = os.read(master, 65536)
                except OSError as error:
                    if error.errno == errno.EIO:
                        break
                    raise
                if not chunk:
                    break
                data.extend(chunk)
            text = data.decode("utf-8", errors="replace")
            self.assertEqual(process.returncode, 0, text)
            self.assertIn("__TTY_RESULT__:watch.example.com|0|STDIN_UNTOUCHED", text)
            self.assertEqual(text.count("DOMAIN> "), 2, text)
            self.assertNotIn("UnicodeDecodeError", text)
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=1)
            if process is not None and process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            if slave is not None:
                os.close(slave)
            os.close(master)


if __name__ == "__main__":
    unittest.main()
