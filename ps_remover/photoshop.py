"""Start Photoshop and run a script in it.

Windows: Photoshop's COM server ("Photoshop.Application"), through pywin32
         when it is installed and PowerShell otherwise.
macOS:   AppleScript ``do javascript`` through ``osascript``.

COM and AppleScript both start Photoshop when it is not running yet.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import ntpath
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

# COM error codes (HRESULT) we treat specially.
_NOT_REGISTERED = {0x80040154, 0x800401F3}  # REGDB_E_CLASSNOTREG, CO_E_CLASSSTRING
_SERVER_EXEC_FAILURE = 0x80080005  # Photoshop did not start in time, or runs with other privileges
_BUSY = {0x80010001, 0x8001010A, _SERVER_EXEC_FAILURE}  # call rejected, retry later, exec failure

BUSY_RETRY_SECONDS = 90.0
_BUSY_RETRY_INTERVAL = 3.0


class PhotoshopError(RuntimeError):
    """Photoshop could not do the job. The message is meant for the user."""


class PhotoshopNotFound(PhotoshopError):
    pass


class UnsupportedPlatform(PhotoshopError):
    pass


class ScriptFailed(PhotoshopError):
    """The script ran but reported an error. ``result`` holds its full report."""

    def __init__(self, message: str, result: dict):
        super().__init__(message)
        self.result = result


def run_jsx(script: str, photoshop: Optional[str] = None, timeout: Optional[float] = None) -> dict:
    """Run ``script`` (see :mod:`ps_remover.script`) in Photoshop and return its report.

    ``photoshop`` picks the application on macOS (e.g. "Adobe Photoshop 2025");
    on Windows the registered COM server is always used.
    """
    system = platform.system()
    if system == "Windows":
        runner: Callable[[str, Optional[str], Optional[float]], str] = _run_windows
    elif system == "Darwin":
        runner = _run_macos
    else:
        raise UnsupportedPlatform(
            "Photoshop 자동 실행은 Windows와 macOS에서만 됩니다. "
            "--export-jsx 로 스크립트 파일을 만든 뒤 Photoshop에서 [파일 > 스크립트 > 찾아보기]로 실행할 수 있습니다."
        )
    tmp = tempfile.mkdtemp(prefix="ps-remover-")
    try:
        jsx_path = os.path.join(tmp, "job.jsx")
        with open(jsx_path, "w", encoding="ascii", newline="\n") as f:
            f.write(script)
        raw = runner(jsx_path, photoshop, timeout)
    finally:
        # A file still locked on Windows must not turn a finished job into an error.
        shutil.rmtree(tmp, ignore_errors=True)
    return parse_result(raw)


def parse_result(raw: str) -> dict:
    text = (raw or "").strip()
    if not text:
        raise PhotoshopError("Photoshop이 결과를 돌려주지 않았습니다. Photoshop 창에 열린 대화상자가 있는지 확인하세요.")
    try:
        data = json.loads(text)
    except ValueError:
        raise PhotoshopError(f"Photoshop의 응답을 해석할 수 없습니다: {text[:500]}") from None
    if not isinstance(data, dict) or "ok" not in data:
        raise PhotoshopError(f"Photoshop의 응답을 해석할 수 없습니다: {text[:500]}")
    if not data["ok"]:
        raise ScriptFailed(data.get("error") or "Photoshop에서 알 수 없는 오류가 났습니다.", data)
    return data


# ------------------------------------------------------------------ Windows


def _run_windows(jsx_path: str, photoshop: Optional[str], timeout: Optional[float]) -> str:
    if importlib.util.find_spec("win32com") is None:  # pywin32 not installed
        return _run_windows_powershell(jsx_path, timeout)
    return _run_windows_pywin32(jsx_path)


def _run_windows_pywin32(jsx_path: str) -> str:
    import pythoncom
    import pywintypes
    import win32com.client.dynamic

    # The GUI runs jobs on a worker thread, which needs its own COM apartment.
    # It is left initialized: uninitializing while COM objects may still be
    # referenced (e.g. from a traceback) can crash the process.
    pythoncom.CoInitialize()
    try:
        app = _retry_busy(lambda: win32com.client.dynamic.Dispatch("Photoshop.Application"), _com_hresult)
        return str(_retry_busy(lambda: app.DoJavaScriptFile(jsx_path), _com_hresult))
    except pywintypes.com_error as exc:
        code = _com_hresult(exc)
        if code in _NOT_REGISTERED:
            raise PhotoshopNotFound(_NOT_INSTALLED_WINDOWS) from None
        raise PhotoshopError(_com_message("Photoshop 스크립트를 실행하지 못했습니다", code, _com_error_text(exc))) from None


def _com_hresult(exc: BaseException) -> Optional[int]:
    code = getattr(exc, "hresult", None)
    if code is None and getattr(exc, "args", None):
        code = exc.args[0]
    return code & 0xFFFFFFFF if isinstance(code, int) else None


def _com_error_text(exc: BaseException) -> str:
    args = getattr(exc, "args", ())
    # pywintypes.com_error: (hresult, text, excepinfo, argerror); excepinfo[2] is the server's message.
    if len(args) >= 3 and args[2] and len(args[2]) >= 3 and args[2][2]:
        return str(args[2][2])
    return str(args[1]) if len(args) >= 2 else str(exc)


# PowerShell writes "OK" or "ERR <stage> <hresult>" on the first line of a
# status file and the script result or error message after it. A file avoids
# console code page trouble with localized error messages.
_POWERSHELL_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$jsxPath = '{jsx}'
$statusPath = '{status}'
function Finish([string]$status, [string]$text) {{
    [System.IO.File]::WriteAllText($statusPath, $status + "`n" + $text, (New-Object System.Text.UTF8Encoding($false)))
    exit 0
}}
function Fail([string]$stage, $err) {{
    $e = $err.Exception
    while ($e.InnerException) {{ $e = $e.InnerException }}
    Finish ('ERR ' + $stage + ' ' + ('0x{{0:X8}}' -f $e.HResult)) $e.Message
}}
try {{ $app = New-Object -ComObject 'Photoshop.Application' }} catch {{ Fail 'connect' $_ }}
try {{
    # InvokeMember leaves DoJavaScriptFile's optional arguments to Photoshop's defaults.
    $result = $app.GetType().InvokeMember('DoJavaScriptFile', [System.Reflection.BindingFlags]::InvokeMethod, $null, $app, @($jsxPath))
}} catch {{ Fail 'script' $_ }}
Finish 'OK' ([string]$result)
"""


def powershell_command(jsx_path: str, status_path: str) -> str:
    return _POWERSHELL_SCRIPT.format(jsx=_ps_quote(jsx_path), status=_ps_quote(status_path))


def _ps_quote(text: str) -> str:
    return text.replace("'", "''")


def _run_windows_powershell(jsx_path: str, timeout: Optional[float]) -> str:
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        raise PhotoshopError("PowerShell을 찾을 수 없습니다. 'pip install pywin32'로 pywin32를 설치한 뒤 다시 시도하세요.")
    status_path = os.path.join(os.path.dirname(jsx_path), "status.txt")
    encoded = base64.b64encode(powershell_command(jsx_path, status_path).encode("utf-16-le")).decode("ascii")
    cmd = [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded]

    def attempt() -> str:
        if os.path.exists(status_path):
            os.remove(status_path)
        try:
            proc = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            raise PhotoshopError(_TIMEOUT_MESSAGE) from None
        if not os.path.exists(status_path):
            detail = (proc.stderr or proc.stdout or b"").decode("utf-8", "replace").strip()
            raise PhotoshopError(f"PowerShell로 Photoshop을 실행하지 못했습니다. {detail[:500]}".strip())
        with open(status_path, encoding="utf-8") as f:
            status, _, text = f.read().partition("\n")
        return _check_powershell_status(status.strip(), text)

    return _retry_busy(attempt, lambda exc: getattr(exc, "hresult", None))


class _ComFailure(PhotoshopError):
    def __init__(self, message: str, hresult: Optional[int]):
        super().__init__(message)
        self.hresult = hresult


def _check_powershell_status(status: str, text: str) -> str:
    if status == "OK":
        return text
    parts = status.split()
    stage = parts[1] if len(parts) > 1 else "?"
    try:
        code: Optional[int] = int(parts[2], 16) & 0xFFFFFFFF
    except (IndexError, ValueError):
        code = None
    if stage == "connect" and code in _NOT_REGISTERED:
        raise PhotoshopNotFound(_NOT_INSTALLED_WINDOWS)
    title = "Photoshop에 연결하지 못했습니다" if stage == "connect" else "Photoshop 스크립트를 실행하지 못했습니다"
    raise _ComFailure(_com_message(title, code, text.strip()), code)


def _retry_busy(func, hresult_of):
    """Call ``func``, retrying while Photoshop is starting up or has a dialog open."""
    deadline = time.monotonic() + BUSY_RETRY_SECONDS
    checked_privileges = False
    while True:
        try:
            return func()
        except Exception as exc:  # noqa: BLE001 - COM errors have no common base class
            code = hresult_of(exc)
            if code not in _BUSY:
                raise
            if code == _SERVER_EXEC_FAILURE and not checked_privileges:
                # Waiting does not help when only one side runs as administrator.
                checked_privileges = True
                state = windows_state()
                if privilege_mismatch_message(state):
                    raise PhotoshopError(start_failed_message(state)) from None
            if time.monotonic() >= deadline:
                if code == _SERVER_EXEC_FAILURE:
                    raise PhotoshopError(start_failed_message(windows_state())) from None
                raise PhotoshopError(_BUSY_MESSAGE) from None
        time.sleep(_BUSY_RETRY_INTERVAL)


def _com_message(title: str, code: Optional[int], text: str) -> str:
    code_text = f" (0x{code:08X})" if code is not None else ""
    return f"{title}{code_text}: {text}" if text else f"{title}{code_text}."


_NOT_INSTALLED_WINDOWS = (
    "Photoshop을 찾을 수 없습니다. Adobe Photoshop이 설치되어 있는지 확인하세요. "
    "설치되어 있다면 Photoshop을 한 번 직접 실행한 뒤 다시 시도하세요."
)
_BUSY_MESSAGE = "Photoshop이 응답하지 않습니다. Photoshop에 열린 대화상자가 있으면 닫고 다시 시도하세요."
_TIMEOUT_MESSAGE = "Photoshop 작업이 제한 시간 안에 끝나지 않았습니다."


# ------------------------------------------------ Windows: why COM failed
#
# 0x80080005 (CO_E_SERVER_EXEC_FAILURE) means Windows started Photoshop for
# us but never got to talk to it. Photoshop runs only once, so the usual
# reason is a Photoshop already running with other privileges: Windows does
# not connect a program run as administrator to one that is not (or the other
# way round), starts a second Photoshop instead, and that one just hands over
# to the first and quits. Other reasons: Photoshop hangs at start-up behind a
# dialog, or the registration points at another (e.g. removed) version.


@dataclass
class WindowsState:
    """What was found out about this program and Photoshop; None where unknown."""

    tool_elevated: Optional[bool] = None
    running: List[Tuple[str, Optional[bool]]] = field(default_factory=list)  # (Photoshop.exe path, elevated)
    registered: Optional[str] = None  # the Photoshop.exe Windows starts for scripts


def privilege_mismatch_message(state: WindowsState) -> Optional[str]:
    """The fix when this program and every running Photoshop differ in privileges, else None."""
    known = [elevated for _, elevated in state.running if elevated is not None]
    if state.tool_elevated is None or not known or state.tool_elevated in known:
        return None
    if state.tool_elevated:
        return (
            "Photoshop에 연결하지 못했습니다 (0x80080005). 이 프로그램은 관리자 권한으로 실행되었는데 Photoshop은 "
            "일반 권한으로 실행 중이라 서로 연결할 수 없습니다. 이 프로그램을 닫고 run_windows.bat을 "
            "'관리자 권한으로 실행' 말고 그냥 더블클릭해서 다시 여세요."
        )
    return (
        "Photoshop에 연결하지 못했습니다 (0x80080005). Photoshop이 관리자 권한으로 실행 중이라 이 프로그램이 "
        "연결할 수 없습니다. Photoshop을 종료한 뒤 시작 메뉴에서 그냥 다시 실행하세요. Photoshop 아이콘을 오른쪽 "
        "클릭 → [속성] → [호환성]에서 '관리자 권한으로 이 프로그램 실행'이 체크되어 있다면 해제하세요."
    )


def start_failed_message(state: WindowsState) -> str:
    """Explains a lasting 0x80080005 with what could be found out, ending with the facts."""
    mismatch = privilege_mismatch_message(state)
    if mismatch:
        return f"{mismatch}\n\n{_describe_state(state)}"
    title = "Photoshop에 연결하지 못했습니다 (0x80080005)."
    running_paths = [path for path, _ in state.running if ntpath.isabs(path)]
    if state.registered and not os.path.isfile(state.registered):
        advice = (f"Windows에 등록된 Photoshop 파일이 없습니다: {state.registered}\n"
                  "Creative Cloud 앱에서 Photoshop을 업데이트하거나 다시 설치하세요.")
    elif state.registered and running_paths and not any(_same_path(p, state.registered) for p in running_paths):
        advice = ("지금 실행 중인 Photoshop과 Windows에 스크립트용으로 등록된 Photoshop이 다릅니다 "
                  "(여러 버전이 설치되어 있으면 생깁니다).\n"
                  "Photoshop을 모두 종료한 뒤 다시 시도하면 등록된 버전이 실행됩니다. 다른 버전을 쓰려면 "
                  "Creative Cloud 앱에서 쓰지 않는 버전을 제거하세요.")
    else:
        advice = ("다음을 차례로 해 보세요.\n"
                  "1) Photoshop에 로그인·업데이트·알림 같은 창이 떠 있으면 닫고 다시 시도\n"
                  "2) 그래도 안 되면 Photoshop을 종료하고, 작업 관리자(Ctrl+Shift+Esc)의 [세부 정보] 탭에 "
                  "Photoshop.exe가 남아 있으면 [작업 끝내기]\n"
                  "3) Photoshop을 시작 메뉴에서 직접 실행하고, 다 켜진 뒤 다시 시도\n"
                  "4) 이 프로그램과 Photoshop 중 하나만 '관리자 권한으로 실행'되어 있으면 연결되지 않습니다. "
                  "둘 다 그냥 더블클릭으로 실행하세요.")
    return f"{title}\n{advice}\n\n{_describe_state(state)}"


def _describe_state(state: WindowsState) -> str:
    def yes_no(value: Optional[bool]) -> str:
        return "알 수 없음" if value is None else ("예" if value else "아니요")

    running = ", ".join(f"{path} (관리자 권한: {yes_no(elevated)})" for path, elevated in state.running) or "없음"
    return ("확인한 내용\n"
            f"- 이 프로그램 관리자 권한: {yes_no(state.tool_elevated)}\n"
            f"- 실행 중인 Photoshop: {running}\n"
            f"- 스크립트용으로 등록된 Photoshop: {state.registered or '찾지 못함'}")


def _same_path(a: str, b: str) -> bool:
    return ntpath.normcase(ntpath.normpath(a)) == ntpath.normcase(ntpath.normpath(b))


def windows_state() -> WindowsState:
    """Looks at this program, the running Photoshop and its registration (Windows only)."""
    state = WindowsState()
    if sys.platform != "win32":
        return state
    for fill in (_fill_privileges_and_processes, _fill_registration):
        try:
            fill(state)
        except Exception:  # noqa: BLE001 - only makes the error message less specific
            pass
    return state


def _fill_privileges_and_processes(state: WindowsState) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                    ctypes.POINTER(wintypes.DWORD)]
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.POINTER(wintypes.DWORD),
                                             wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]

    def elevated(process) -> Optional[bool]:
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(process, 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
            return None
        try:
            value, size = wintypes.DWORD(), wintypes.DWORD()
            if not advapi32.GetTokenInformation(token, 20, ctypes.byref(value), ctypes.sizeof(value),
                                                ctypes.byref(size)):  # TokenElevation
                return None
            return bool(value.value)
        finally:
            kernel32.CloseHandle(token)

    state.tool_elevated = elevated(kernel32.GetCurrentProcess())

    class ProcessEntry(ctypes.Structure):  # PROCESSENTRY32W
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260)]

    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        return
    pids = []
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(ProcessEntry)
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            if entry.szExeFile.lower() == "photoshop.exe":
                pids.append(entry.th32ProcessID)
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    for pid in pids:
        path, is_elevated = "Photoshop.exe", None
        process = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if process:
            try:
                buffer = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(len(buffer))
                if kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
                    path = buffer.value
                is_elevated = elevated(process)
            finally:
                kernel32.CloseHandle(process)
        state.running.append((path, is_elevated))


def _fill_registration(state: WindowsState) -> None:
    import winreg

    def value(path: str) -> Optional[str]:
        for view in (getattr(winreg, "KEY_WOW64_64KEY", 0), 0):  # Photoshop is a 64-bit program
            try:
                with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, path, 0, winreg.KEY_READ | view) as key:
                    data, kind = winreg.QueryValueEx(key, "")
            except OSError:
                continue
            if isinstance(data, str) and data.strip():
                return ntpath.expandvars(data) if kind == winreg.REG_EXPAND_SZ else data
        return None

    clsid = value(r"Photoshop.Application\CLSID")
    if not clsid:
        version = value(r"Photoshop.Application\CurVer")
        clsid = value(version + r"\CLSID") if version else None
    if clsid:
        state.registered = exe_from_command(value(rf"CLSID\{clsid}\LocalServer32"))


def exe_from_command(command: Optional[str]) -> Optional[str]:
    """The program in a registered command line such as '"C:\\...\\Photoshop.exe" /automation'."""
    command = (command or "").strip()
    if command.startswith('"'):
        return command[1:].split('"', 1)[0] or None
    match = re.match(r"(.+?\.exe)(?:\s|$)", command, re.IGNORECASE)
    return match.group(1) if match else (command.split()[0] if command else None)


# -------------------------------------------------------------------- macOS

MAC_BUNDLE_ID = "com.adobe.Photoshop"
_MAC_TIMEOUT_SECONDS = 3600


def applescript_lines(app_name: Optional[str] = None, timeout: Optional[float] = None) -> list:
    target = f'application "{_as_quote(app_name)}"' if app_name else f'application id "{MAC_BUNDLE_ID}"'
    seconds = int(timeout) if timeout else _MAC_TIMEOUT_SECONDS
    return [
        "on run argv",
        "set jsCode to read (POSIX file (item 1 of argv))",
        f"tell {target}",
        "activate",
        f"with timeout of {seconds} seconds",
        "set psResult to do javascript jsCode",
        "end timeout",
        "end tell",
        "return psResult",
        "end run",
    ]


def mac_app_name(photoshop: Optional[str]) -> Optional[str]:
    """Accept an application name or a path to the .app bundle."""
    if not photoshop:
        return None
    name = photoshop.rstrip("/")
    if name.endswith(".app") or "/" in name:
        name = os.path.basename(name)
        if name.endswith(".app"):
            name = name[:-4]
    return name or None


def _run_macos(jsx_path: str, photoshop: Optional[str], timeout: Optional[float]) -> str:
    cmd = ["osascript"]
    for line in applescript_lines(mac_app_name(photoshop), timeout):
        cmd += ["-e", line]
    cmd.append(jsx_path)
    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=(timeout + 30) if timeout else None,
        )
    except FileNotFoundError:
        raise PhotoshopError("osascript를 찾을 수 없습니다.") from None
    except subprocess.TimeoutExpired:
        raise PhotoshopError(_TIMEOUT_MESSAGE) from None
    if proc.returncode != 0:
        raise mac_error(proc.stderr, photoshop)
    return proc.stdout


def mac_error(stderr: str, photoshop: Optional[str] = None) -> PhotoshopError:
    text = (stderr or "").strip()
    if "-1743" in text:
        return PhotoshopError(
            "macOS가 Photoshop 제어를 막았습니다. [시스템 설정 > 개인정보 보호 및 보안 > 자동화]에서 "
            "이 프로그램을 실행한 앱(터미널 또는 Python)의 'Adobe Photoshop' 항목을 켜 주세요."
        )
    not_found = ("-1728", "-10814", "-2740", "-2741", "Can't get application", "Can’t get application")
    if any(marker in text for marker in not_found):
        name = f"'{photoshop}'" if photoshop else "Adobe Photoshop"
        return PhotoshopNotFound(f"{name}을(를) 찾을 수 없습니다. 설치 여부와 앱 이름을 확인하세요. 원본 메시지: {text}")
    if "-1712" in text:
        return PhotoshopError(_TIMEOUT_MESSAGE)
    return PhotoshopError(f"Photoshop 스크립트를 실행하지 못했습니다: {text}")


def _as_quote(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')
