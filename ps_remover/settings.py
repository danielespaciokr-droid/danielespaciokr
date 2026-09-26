"""Per-user storage: named common areas ("presets") and remembered GUI settings."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple, Union

from .shapes import Area, AreaSet, SelectionError, load_area_set, save_area

APP_NAME = "ps-remover"
STARTUP_SCRIPT_NAME = "PS Remover 폴더 자동 처리.cmd"
LOG_KEEP_DAYS = 30
_BAD_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
# Words that tell a common area's orientation apart in its name, e.g. '워터마크 가로형' / '워터마크 세로형'.
_ORIENTATION_WORDS = re.compile(r"(가로|세로)\s*(사진)?\s*[형용]?|landscape|portrait|horizontal|vertical")


def config_dir() -> Path:
    """Where presets and settings are kept; the PS_REMOVER_HOME variable overrides it."""
    override = os.environ.get("PS_REMOVER_HOME")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / APP_NAME


def presets_dir() -> Path:
    return config_dir() / "presets"


def check_preset_name(name: str) -> str:
    name = (name or "").strip()
    if not name or len(name) > 60 or name.startswith(".") or _BAD_NAME.search(name):
        raise SelectionError('공통 영역 이름은 1~60자이고 \\ / : * ? " < > | 를 쓸 수 없습니다.')
    return name


def preset_path(name: str) -> Path:
    return presets_dir() / f"{check_preset_name(name)}.json"


def list_presets() -> List[str]:
    folder = presets_dir()
    if not folder.is_dir():
        return []
    return sorted((path.stem for path in folder.glob("*.json")), key=str.casefold)


def save_preset(name: str, area: Union[Area, AreaSet]) -> Path:
    path = preset_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    save_area(path, area)
    return path


def load_preset(name: str) -> AreaSet:
    path = preset_path(name)
    if not path.is_file():
        saved = ", ".join(list_presets()) or "없음"
        raise SelectionError(f"공통 영역 '{name.strip()}'이(가) 없습니다. 저장된 공통 영역: {saved}")
    return load_area_set(path)


def partner_preset(name: str, orientation: str) -> Optional[str]:
    """The common area to use along with ``name`` on photos of ``orientation``.

    ``name`` itself when it has an area drawn for that orientation; else one
    that has, saved under the same name but for its orientation word:
    '워터마크 세로형' for '워터마크 가로형', '글씨 (세로)' for '글씨 (가로)' or
    for '글씨'. None when there is none.
    """
    try:
        if orientation in load_preset(name).areas:
            return name
    except SelectionError:
        return None
    base = _base_name(name)
    found = []
    for other in list_presets():
        if other.casefold() == name.casefold() or _base_name(other) != base:
            continue
        try:
            if orientation in load_preset(other).areas:
                found.append(other)
        except SelectionError:
            continue
    return found[0] if len(found) == 1 else None


def preset_pair(name: str) -> Tuple[str, str]:
    """The common areas for landscape and for portrait photos, going with ``name`` (see partner_preset)."""
    if not name:
        return ("", "")
    landscape = partner_preset(name, "landscape")
    portrait = partner_preset(name, "portrait")
    if landscape != name and portrait != name:  # unreadable, or drawn for neither: leave it to load_pair
        return (name, name)
    return (landscape or name, portrait or name)


def load_pair(landscape: str, portrait: str) -> AreaSet:
    """The area for landscape photos from common area ``landscape``, for portrait ones from ``portrait``.

    A common area without an area for that orientation gives its other one,
    which is then fitted to those photos.
    """
    first = load_preset(landscape)
    if not portrait or portrait.casefold() == landscape.casefold():
        return first
    second = load_preset(portrait)
    return AreaSet({"landscape": first.for_orientation("landscape"), "portrait": second.for_orientation("portrait")})


def _base_name(name: str) -> str:
    """``name`` without its orientation word, spaces and brackets: the same for both of a pair."""
    return re.sub(r"[\W_]+", "", _ORIENTATION_WORDS.sub("", name.casefold()))


def delete_preset(name: str) -> None:
    path = preset_path(name)
    if path.is_file():
        path.unlink()


def load_settings(section: str) -> dict:
    """Values saved with :func:`save_settings`, or ``{}``."""
    value = _read_settings().get(section)
    return value if isinstance(value, dict) else {}


def save_settings(section: str, values: dict) -> None:
    data = _read_settings()
    data[section] = values
    path = config_dir() / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _read_settings() -> dict:
    try:
        data = json.loads((config_dir() / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------- folder watcher log


def log_dir() -> Path:
    return config_dir() / "logs"


def append_log(text: str, now: Optional[float] = None) -> None:
    """Adds ``text`` to today's log of the folder watcher. Failing to write is ignored."""
    stamp = time.localtime(now)
    path = log_dir() / f"auto-{time.strftime('%Y-%m-%d', stamp)}.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S', stamp)}] {text}\n")
    except OSError:
        pass


def prune_logs(keep_days: int = LOG_KEEP_DAYS, now: Optional[float] = None) -> None:
    """Deletes watcher logs older than ``keep_days`` days, so a computer that is always on does not fill up."""
    cutoff = (time.time() if now is None else now) - keep_days * 86400
    try:
        old = [path for path in log_dir().glob("auto-*.log") if path.stat().st_mtime < cutoff]
    except OSError:
        return
    for path in old:
        try:
            path.unlink()
        except OSError:
            pass


# ------------------------------------------------------- start at sign-in


def startup_script_path() -> Optional[Path]:
    """The script Windows runs at sign-in to start the folder watcher; None elsewhere."""
    appdata = os.environ.get("APPDATA")
    if sys.platform != "win32" or not appdata:
        return None
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / STARTUP_SCRIPT_NAME


def starts_at_login() -> bool:
    path = startup_script_path()
    return bool(path and path.is_file())


def set_start_at_login(enabled: bool) -> None:
    """Add or remove the folder watcher from the programs Windows starts at sign-in."""
    path = startup_script_path()
    if path is None:
        raise OSError("Windows에서만 쓸 수 있는 기능입니다.")
    if not enabled:
        if path.is_file():
            path.unlink()
        return
    project = Path(__file__).resolve().parent.parent
    lines = [
        "@echo off",
        "rem Starts the PS Remover folder watcher when you sign in to Windows.",
        "rem Remove this file (or untick the option in the program) to stop that.",
        "chcp 65001 >nul",
        f'cd /d "{project}"',
        f'start "" "{_windowless_python()}" -m ps_remover watch --start',
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\r\n") as f:
        f.write("\n".join(lines) + "\n")


def _windowless_python() -> str:
    """pythonw.exe next to this Python, so no console window stays open."""
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return str(pythonw if pythonw.is_file() else sys.executable)
