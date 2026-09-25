"""Per-user storage: named common areas ("presets") and remembered GUI settings."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import List

from .shapes import Area, SelectionError, load_area, save_area

APP_NAME = "ps-remover"
_BAD_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


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


def save_preset(name: str, area: Area) -> Path:
    path = preset_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    save_area(path, area)
    return path


def load_preset(name: str) -> Area:
    path = preset_path(name)
    if not path.is_file():
        saved = ", ".join(list_presets()) or "없음"
        raise SelectionError(f"공통 영역 '{name.strip()}'이(가) 없습니다. 저장된 공통 영역: {saved}")
    return load_area(path)


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
