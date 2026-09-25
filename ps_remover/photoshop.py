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
import os
import platform
import shutil
import subprocess
import tempfile
import time
from typing import Callable, Optional

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
    while True:
        try:
            return func()
        except Exception as exc:  # noqa: BLE001 - COM errors have no common base class
            code = hresult_of(exc)
            if code not in _BUSY:
                raise
            if time.monotonic() >= deadline:
                raise PhotoshopError(_START_FAILED_MESSAGE if code == _SERVER_EXEC_FAILURE else _BUSY_MESSAGE) from None
        time.sleep(_BUSY_RETRY_INTERVAL)


def _com_message(title: str, code: Optional[int], text: str) -> str:
    code_text = f" (0x{code:08X})" if code is not None else ""
    return f"{title}{code_text}: {text}" if text else f"{title}{code_text}."


_NOT_INSTALLED_WINDOWS = (
    "Photoshop을 찾을 수 없습니다. Adobe Photoshop이 설치되어 있는지 확인하세요. "
    "설치되어 있다면 Photoshop을 한 번 직접 실행한 뒤 다시 시도하세요."
)
_BUSY_MESSAGE = "Photoshop이 응답하지 않습니다. Photoshop에 열린 대화상자가 있으면 닫고 다시 시도하세요."
_START_FAILED_MESSAGE = (
    "Photoshop에 연결하지 못했습니다 (0x80080005). Photoshop이 관리자 권한으로 실행 중이면 "
    "Photoshop을 일반 권한으로 다시 실행하거나, 이 도구도 관리자 권한으로 실행하세요."
)
_TIMEOUT_MESSAGE = "Photoshop 작업이 제한 시간 안에 끝나지 않았습니다."


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
