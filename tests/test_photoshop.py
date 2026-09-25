import base64
import ctypes
import json
import os
import subprocess
import sys
import types
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

    def test_privilege_mismatch_fails_at_once(self):
        calls = []

        def failing():
            calls.append(1)
            raise photoshop._ComFailure("exec", 0x80080005)

        state = photoshop.WindowsState(tool_elevated=False, running=[(PS_2025, True)])
        with mock.patch.object(photoshop, "windows_state", return_value=state), \
                mock.patch.object(photoshop, "_BUSY_RETRY_INTERVAL", 0):
            with self.assertRaisesRegex(PhotoshopError, "Photoshop이 관리자 권한으로 실행 중"):
                photoshop._retry_busy(failing, lambda e: getattr(e, "hresult", None))
        self.assertEqual(len(calls), 1)  # no minute and a half of waiting first

    def test_lasting_exec_failure_explains_what_was_found(self):
        def failing():
            raise photoshop._ComFailure("exec", 0x80080005)

        state = photoshop.WindowsState(tool_elevated=False, running=[(PS_2025, False)], registered=PS_2025)
        with mock.patch.object(photoshop, "windows_state", return_value=state), \
                mock.patch.object(photoshop.os.path, "isfile", return_value=True), \
                mock.patch.object(photoshop, "BUSY_RETRY_SECONDS", 0), \
                mock.patch.object(photoshop, "_BUSY_RETRY_INTERVAL", 0):
            with self.assertRaises(PhotoshopError) as ctx:
                photoshop._retry_busy(failing, lambda e: getattr(e, "hresult", None))
        message = str(ctx.exception)
        self.assertIn("작업 관리자", message)
        self.assertIn(f"실행 중인 Photoshop: {PS_2025} (관리자 권한: 아니요)", message)

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


PS_2025 = r"C:\Program Files\Adobe\Adobe Photoshop 2025\Photoshop.exe"
PS_2024 = r"C:\Program Files\Adobe\Adobe Photoshop 2024\Photoshop.exe"


class WindowsDiagnosisTests(unittest.TestCase):
    def test_privilege_mismatch(self):
        state = photoshop.WindowsState
        self.assertIn("이 프로그램은 관리자 권한으로 실행되었는데",
                      photoshop.privilege_mismatch_message(state(True, [(PS_2025, False)])))
        self.assertIn("속성] → [호환성]", photoshop.privilege_mismatch_message(state(False, [(PS_2025, True)])))
        for same in (state(False, [(PS_2025, False)]), state(True, [(PS_2025, True)]),
                     state(None, [(PS_2025, True)]), state(False, [(PS_2025, None)]), state(False, []),
                     state(False, [(PS_2025, True), (PS_2024, False)])):  # one of them could answer
            with self.subTest(state=same):
                self.assertIsNone(photoshop.privilege_mismatch_message(same))

    def test_start_failed_message(self):
        state = photoshop.WindowsState
        with mock.patch.object(photoshop.os.path, "isfile", return_value=False):
            message = photoshop.start_failed_message(state(False, [], PS_2024))
        self.assertIn(f"등록된 Photoshop 파일이 없습니다: {PS_2024}", message)
        with mock.patch.object(photoshop.os.path, "isfile", return_value=True):
            other = photoshop.start_failed_message(state(False, [(PS_2025, False)], PS_2024))
            same = photoshop.start_failed_message(state(False, [(PS_2025.upper(), False)], PS_2025))
            unknown = photoshop.start_failed_message(state(None, [("Photoshop.exe", None)], PS_2024))
            elevated = photoshop.start_failed_message(state(True, [(PS_2025, False)], PS_2025))
        self.assertIn("등록된 Photoshop이 다릅니다", other)
        self.assertIn("작업 관리자", same)  # paths are compared the Windows way
        self.assertIn("작업 관리자", unknown)  # a path that could not be read proves nothing
        self.assertIn("알 수 없음", unknown)
        self.assertIn("그냥 더블클릭", elevated)
        empty = photoshop.start_failed_message(state())
        self.assertIn("실행 중인 Photoshop: 없음", empty)
        self.assertIn("스크립트용으로 등록된 Photoshop: 찾지 못함", empty)

    def test_state_is_windows_only(self):
        with mock.patch.object(photoshop.sys, "platform", "linux"):
            self.assertEqual(photoshop.windows_state(), photoshop.WindowsState())

    def test_exe_from_command(self):
        cases = {
            f'"{PS_2025}" /automation': PS_2025,
            f"{PS_2025} -Embedding": PS_2025,
            PS_2025: PS_2025,
            "": None,
            None: None,
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertEqual(photoshop.exe_from_command(command), expected)

    def test_processes_and_privileges(self):
        api = FakeWin32({7: ("explorer.exe", r"C:\Windows\explorer.exe", False),
                         8: ("Photoshop.exe", PS_2025, True),
                         9: ("PHOTOSHOP.EXE", PS_2024, None)},  # its token cannot be read
                        tool_elevated=False)
        state = photoshop.WindowsState()
        with mock.patch.object(ctypes, "WinDLL", api.load, create=True):
            photoshop._fill_privileges_and_processes(state)
        self.assertFalse(state.tool_elevated)
        self.assertEqual(state.running, [(PS_2025, True), (PS_2024, None)])
        self.assertEqual(api.open_handles, set())  # every handle was closed again

    def test_registration_lookup(self):
        clsid = "{6DECC242-87EF-11CF-86B4-444553540000}"
        direct = {
            r"Photoshop.Application\CLSID": clsid,
            rf"CLSID\{clsid}\LocalServer32": f'"{PS_2025}" /automation',
        }
        through_version = {
            r"Photoshop.Application\CurVer": "Photoshop.Application.190",
            r"Photoshop.Application.190\CLSID": clsid,
            rf"CLSID\{clsid}\LocalServer32": (r"%ProgramFiles%\Adobe\Photoshop.exe", FakeWinreg.REG_EXPAND_SZ),
        }
        for keys, views, expected in [
            (direct, (FakeWinreg.KEY_WOW64_64KEY,), PS_2025),
            (direct, (0,), PS_2025),  # e.g. 32-bit Windows: the 64-bit view is not there
            (through_version, (FakeWinreg.KEY_WOW64_64KEY,), r"C:\Program Files\Adobe\Photoshop.exe"),
            ({}, (FakeWinreg.KEY_WOW64_64KEY, 0), None),
        ]:
            state = photoshop.WindowsState()
            with self.subTest(expected=expected), \
                    mock.patch.dict(sys.modules, {"winreg": FakeWinreg(keys, views)}), \
                    mock.patch.dict(os.environ, {"ProgramFiles": r"C:\Program Files"}):
                photoshop._fill_registration(state)
                self.assertEqual(state.registered, expected)


class FakeWin32:
    """kernel32 and advapi32 functions used to find Photoshop processes and their privileges."""

    SELF = 0xFFFF

    def __init__(self, processes, tool_elevated):
        self.processes = processes  # pid -> (exe name, full path, elevated or None when unreadable)
        self.tool_elevated = tool_elevated
        self.open_handles = set()
        self._next = 100
        self._snapshots = {}
        self._handles = {}

    def load(self, name, use_last_error=False):
        functions = {
            "kernel32": ["GetCurrentProcess", "OpenProcess", "CloseHandle", "QueryFullProcessImageNameW",
                         "CreateToolhelp32Snapshot", "Process32FirstW", "Process32NextW"],
            "advapi32": ["OpenProcessToken", "GetTokenInformation"],
        }[name]
        dll = types.SimpleNamespace()
        for function in functions:
            method = getattr(self, function)
            # Plain functions, so that the code can set argtypes and restype on them.
            setattr(dll, function, (lambda m: lambda *args: m(*args))(method))
        return dll

    def _open(self, what):
        self._next += 1
        self._handles[self._next] = what
        self.open_handles.add(self._next)
        return self._next

    def GetCurrentProcess(self):  # noqa: N802 - Win32 names
        return self.SELF

    def OpenProcess(self, access, inherit, pid):  # noqa: N802
        assert access == 0x1000
        return self._open(("process", pid)) if pid in self.processes else None

    def CloseHandle(self, handle):  # noqa: N802
        handle = getattr(handle, "value", handle)
        self.open_handles.remove(handle)
        return 1

    def QueryFullProcessImageNameW(self, process, flags, buffer, size):  # noqa: N802
        path = self.processes[self._handles[process][1]][1]
        buffer.value = path
        size._obj.value = len(path)
        return 1

    def CreateToolhelp32Snapshot(self, flags, pid):  # noqa: N802
        assert flags == 0x2
        handle = self._open("snapshot")
        self._snapshots[handle] = sorted(self.processes)
        return handle

    def Process32FirstW(self, snapshot, entry):  # noqa: N802
        return self.Process32NextW(snapshot, entry)

    def Process32NextW(self, snapshot, entry):  # noqa: N802
        assert entry._obj.dwSize == ctypes.sizeof(entry._obj)
        remaining = self._snapshots[snapshot]
        if not remaining:
            return 0
        pid = remaining.pop(0)
        entry._obj.th32ProcessID = pid
        entry._obj.szExeFile = self.processes[pid][0]
        return 1

    def OpenProcessToken(self, process, access, token):  # noqa: N802
        assert access == 0x0008
        if process == self.SELF:
            elevated = self.tool_elevated
        else:
            elevated = self.processes[self._handles[process][1]][2]
        if elevated is None:
            return 0
        token._obj.value = self._open(("token", elevated))
        return 1

    def GetTokenInformation(self, token, kind, value, length, returned):  # noqa: N802
        assert kind == 20 and length == ctypes.sizeof(value._obj)  # TokenElevation: one DWORD
        value._obj.value = int(self._handles[token.value][1])
        return 1


class FakeWinreg:
    """Just enough of the winreg module; values are found only in the given registry views."""

    HKEY_CLASSES_ROOT = "HKCR"
    KEY_READ = 0x20019
    KEY_WOW64_64KEY = 0x0100
    REG_SZ = 1
    REG_EXPAND_SZ = 2

    def __init__(self, keys, views):
        self.keys = keys
        self.views = views

    def OpenKey(self, root, path, reserved, access):  # noqa: N802 - winreg's name
        view = access & self.KEY_WOW64_64KEY
        if root != self.HKEY_CLASSES_ROOT or path not in self.keys or view not in self.views:
            raise FileNotFoundError(2, "not found")
        value = self.keys[path]
        return _FakeKey(value if isinstance(value, tuple) else (value, self.REG_SZ))

    @staticmethod
    def QueryValueEx(key, name):  # noqa: N802 - winreg's name
        assert name == ""
        return key.value


class _FakeKey:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


if __name__ == "__main__":
    unittest.main()
