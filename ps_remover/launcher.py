"""What run_windows.bat and run_mac.command start.

Installs the packages the program needs the first time (Pillow, plus pywin32
on Windows), then opens it. Problems are explained in plain Korean, because
the people double-clicking a launcher are usually not programmers.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from typing import List, Optional, Sequence

PYTHON_DOWNLOAD = "https://www.python.org/downloads/"

NO_TKINTER = (
    "이 Python에는 프로그램 화면(Tkinter) 기능이 없습니다.\n"
    f"{PYTHON_DOWNLOAD} 에서 Python을 내려받아 설치한 뒤 다시 실행하세요.\n"
    "(macOS에서 Homebrew로 설치한 Python이라면 터미널에서: brew install python-tk)"
)
INSTALL_FAILED = (
    "필요한 프로그램을 설치하지 못했습니다. 인터넷 연결을 확인한 뒤 다시 실행하세요.\n"
    "계속 안 되면 위에 나온 메시지를 캡처해서 보내 주세요."
)


def missing_packages() -> List[str]:
    """pip names of the required packages that cannot be imported."""
    missing = []
    if importlib.util.find_spec("PIL") is None:
        missing.append("Pillow")
    if sys.platform == "win32" and importlib.util.find_spec("win32com") is None:
        missing.append("pywin32")
    return missing


def install_command(packages: Sequence[str]) -> List[str]:
    command = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check"]
    if sys.prefix == sys.base_prefix:  # not in a virtual environment: install for this user only
        command.append("--user")
    return command + list(packages)


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if importlib.util.find_spec("tkinter") is None:
        return _fail(NO_TKINTER)
    missing = missing_packages()
    if not missing:
        from .cli import main as cli_main

        return cli_main(argv)
    print(f"처음 실행 준비: {', '.join(missing)}을(를) 설치합니다. 1~2분 걸릴 수 있습니다...", flush=True)
    if subprocess.call(install_command(missing)) != 0:
        return _fail(INSTALL_FAILED)
    # A new interpreter sees packages that were just installed into a fresh user site-packages.
    return subprocess.call([sys.executable, "-m", "ps_remover", *argv])


def _fail(message: str) -> int:
    print(f"\n{message}\n", file=sys.stderr, flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
