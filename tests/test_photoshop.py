import base64
import json
import os
import subprocess
import unittest
from unittest import mock

from ps_remover import photoshop
from ps_remover.photoshop import PhotoshopError, PhotoshopNotFound, ScriptFailed, UnsupportedPlatform


class ParseResultTests(unittest.TestCase):
    def test_success(self):
        self.assertEqual(photoshop.parse_result('  {"ok": true, "output": "a.jpg"}\n'), {"ok": True, "output": "a.jpg"})

    def test_script_failure_carries_report(self):
        with self.assertRaises(ScriptFailed) as ctx:
            photoshop.parse_result('{"ok": false, "error": "\\uc120\\ud0dd", "leftOpen": true}')
        self.assertEqual(str(ctx.exception), "선택")
        self.assertTrue(ctx.exception.result["leftOpen"])

    def test_garbage(self):
        for raw in ("", "undefined", "[1, 2]", '{"output": 1}'):
            with self.subTest(raw=raw), self.assertRaises(PhotoshopError):
                photoshop.parse_result(raw)


class RunJsxTests(unittest.TestCase):
    def test_linux_is_unsupported(self):
        with mock.patch("platform.system", return_value="Linux"):
            with self.assertRaises(UnsupportedPlatform):
                photoshop.run_jsx("1")

    def test_dispatches_to_platform_runner(self):
        seen = {}

        def fake_runner(path, app, timeout):
            with open(path, encoding="ascii") as f:
                seen["script"] = f.read()
            seen["args"] = (app, timeout)
            return json.dumps({"ok": True})

        with mock.patch("platform.system", return_value="Darwin"), \
                mock.patch.object(photoshop, "_run_macos", side_effect=fake_runner):
            self.assertEqual(photoshop.run_jsx("psrRun(1);", "Adobe Photoshop 2024", 10), {"ok": True})
        self.assertEqual(seen, {"script": "psrRun(1);", "args": ("Adobe Photoshop 2024", 10)})


class MacTests(unittest.TestCase):
    def test_applescript_targets_bundle_id_by_default(self):
        lines = photoshop.applescript_lines()
        self.assertIn('tell application id "com.adobe.Photoshop"', lines)
        self.assertIn("with timeout of 3600 seconds", lines)
        self.assertIn("set psResult to do javascript jsCode", lines)

    def test_applescript_quotes_app_name(self):
        lines = photoshop.applescript_lines('Odd "Name"\\', timeout=12.7)
        self.assertIn('tell application "Odd \\"Name\\"\\\\"', lines)
        self.assertIn("with timeout of 12 seconds", lines)

    def test_app_name_from_path(self):
        self.assertIsNone(photoshop.mac_app_name(None))
        self.assertEqual(photoshop.mac_app_name("Adobe Photoshop 2025"), "Adobe Photoshop 2025")
        self.assertEqual(
            photoshop.mac_app_name("/Applications/Adobe Photoshop 2025/Adobe Photoshop 2025.app/"),
            "Adobe Photoshop 2025",
        )

    def test_error_mapping(self):
        self.assertIn("자동화", str(photoshop.mac_error("execution error: Not authorized to send Apple events (-1743)")))
        self.assertIsInstance(photoshop.mac_error("Can’t get application id \"com.adobe.Photoshop\". (-1728)"),
                              PhotoshopNotFound)
        self.assertIn("제한 시간", str(photoshop.mac_error("AppleEvent timed out. (-1712)")))
        self.assertIn("boom", str(photoshop.mac_error("boom")))

    def test_run_macos_builds_osascript_command(self):
        completed = subprocess.CompletedProcess([], 0, stdout='{"ok": true}\n', stderr="")
        with mock.patch("subprocess.run", return_value=completed) as run:
            self.assertEqual(photoshop._run_macos("/tmp/job.jsx", "/Applications/X/Adobe Photoshop 2024.app", None),
                             '{"ok": true}\n')
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[0], "osascript")
        self.assertEqual(cmd[-1], "/tmp/job.jsx")
        self.assertIn('tell application "Adobe Photoshop 2024"', cmd)

    def test_run_macos_failure(self):
        completed = subprocess.CompletedProcess([], 1, stdout="", stderr="execution error: (-1743)")
        with mock.patch("subprocess.run", return_value=completed):
            with self.assertRaises(PhotoshopError):
                photoshop._run_macos("/tmp/job.jsx", None, None)


class WindowsTests(unittest.TestCase):
    def test_powershell_command_quotes_paths(self):
        command = photoshop.powershell_command("C:\\Users\\O'Neil\\job.jsx", "C:\\t\\status.txt")
        self.assertIn("$jsxPath = 'C:\\Users\\O''Neil\\job.jsx'", command)
        self.assertIn("'0x{0:X8}' -f $e.HResult", command)
        self.assertIn("InvokeMember('DoJavaScriptFile'", command)
        # -EncodedCommand takes base64 of UTF-16LE text.
        encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
        self.assertEqual(base64.b64decode(encoded).decode("utf-16-le"), command)

    def test_status_ok(self):
        self.assertEqual(photoshop._check_powershell_status("OK", '{"ok":true}'), '{"ok":true}')

    def test_status_not_registered(self):
        with self.assertRaises(PhotoshopNotFound):
            photoshop._check_powershell_status("ERR connect 0x80040154", "Class not registered")

    def test_status_script_error(self):
        with self.assertRaisesRegex(PhotoshopError, "0x80004005.*Syntax error"):
            photoshop._check_powershell_status("ERR script 0x80004005", "Error 8: Syntax error. Line: 3")

    def test_busy_errors_are_retried(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise photoshop._ComFailure("busy", 0x80010001)
            return "done"

        with mock.patch.object(photoshop, "_BUSY_RETRY_INTERVAL", 0):
            self.assertEqual(photoshop._retry_busy(flaky, lambda e: getattr(e, "hresult", None)), "done")
        self.assertEqual(len(calls), 3)

    def test_busy_gives_up_after_deadline(self):
        def busy():
            raise photoshop._ComFailure("busy", 0x8001010A)

        with mock.patch.object(photoshop, "BUSY_RETRY_SECONDS", 0), \
                mock.patch.object(photoshop, "_BUSY_RETRY_INTERVAL", 0):
            with self.assertRaisesRegex(PhotoshopError, "응답하지 않습니다"):
                photoshop._retry_busy(busy, lambda e: getattr(e, "hresult", None))

    def test_server_exec_failure_mentions_privileges(self):
        def failing():
            raise photoshop._ComFailure("exec", 0x80080005)

        with mock.patch.object(photoshop, "BUSY_RETRY_SECONDS", 0), \
                mock.patch.object(photoshop, "_BUSY_RETRY_INTERVAL", 0):
            with self.assertRaisesRegex(PhotoshopError, "관리자 권한"):
                photoshop._retry_busy(failing, lambda e: getattr(e, "hresult", None))

    def test_temp_folder_is_removed(self):
        seen = {}

        def fake_runner(path, app, timeout):
            seen["path"] = path
            return '{"ok": true}'

        with mock.patch("platform.system", return_value="Windows"), \
                mock.patch.object(photoshop, "_run_windows", side_effect=fake_runner):
            photoshop.run_jsx("1")
        self.assertFalse(os.path.exists(os.path.dirname(seen["path"])))

    def test_other_errors_are_not_retried(self):
        calls = []

        def broken():
            calls.append(1)
            raise ValueError("nope")

        with self.assertRaises(ValueError):
            photoshop._retry_busy(broken, lambda e: None)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
