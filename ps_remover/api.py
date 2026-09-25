"""High-level jobs: open a photo in Photoshop, remove an area, save the result."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Tuple

from .photoshop import run_jsx
from .script import build_script
from .shapes import Shape, has_area, selection_ops

METHODS = ("content-aware", "transparent")
SAVE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".psd")
TRANSPARENCY_EXTENSIONS = (".png", ".tif", ".tiff", ".psd")
DEFAULT_SUFFIX = "_removed"


class JobError(ValueError):
    """The job was set up incorrectly; nothing was sent to Photoshop."""


@dataclass
class RemoveOptions:
    method: str = "content-aware"  # "content-aware": fill in from the surroundings, "transparent": cut out
    expand: int = 4                # grow the selection by this many pixels before removing
    feather: float = 0.0           # soften the selection edge (pixels)
    subject: bool = False          # keep only what Photoshop's Select Subject finds inside the area
    keep_open: bool = True         # open the result in Photoshop afterwards
    jpeg_quality: int = 12         # 0-12, Photoshop's JPEG quality scale

    def validate(self) -> None:
        if self.method not in METHODS:
            raise JobError(f"제거 방식은 {', '.join(METHODS)} 중 하나여야 합니다: {self.method!r}")
        if not 0 <= self.expand <= 100:
            raise JobError("선택 영역 확장은 0~100 픽셀이어야 합니다.")
        if not 0 <= self.feather <= 250:
            raise JobError("가장자리 부드럽게(페더)는 0~250 픽셀이어야 합니다.")
        if not 0 <= self.jpeg_quality <= 12:
            raise JobError("JPEG 품질은 0~12여야 합니다.")


def default_output_path(photo, method: str = "content-aware", suffix: str = DEFAULT_SUFFIX,
                        directory=None, unique: bool = True) -> Path:
    """``photo_removed.jpg`` next to the photo (or in ``directory``).

    Keeps the photo's format when Photoshop can save it; otherwise uses JPG, or
    PNG when the removed area should become transparent. With ``unique`` a
    number is appended instead of reusing an existing file name.
    """
    photo = Path(photo)
    ext = photo.suffix.lower()
    if method == "transparent":
        if ext not in TRANSPARENCY_EXTENSIONS:
            ext = ".png"
    elif ext not in SAVE_EXTENSIONS:
        ext = ".jpg"
    folder = Path(directory) if directory else photo.parent
    candidate = folder / f"{photo.stem}{suffix}{ext}"
    number = 2
    while unique and candidate.exists():
        candidate = folder / f"{photo.stem}{suffix}_{number}{ext}"
        number += 1
    return candidate


def build_remove_config(photo, output, shapes: Sequence[Shape], options: Optional[RemoveOptions] = None,
                        image_size: Optional[Tuple[int, int]] = None, report: str = "return") -> dict:
    """The job description the Photoshop script expects for the "remove" action."""
    options = options or RemoveOptions()
    options.validate()
    if not has_area(shapes) and not options.subject:
        raise JobError("지울 영역이 없습니다. 영역을 선택하거나 '피사체 선택'을 켜세요.")
    output = Path(output)
    if output.suffix.lower() not in SAVE_EXTENSIONS:
        raise JobError(f"저장 형식은 {', '.join(SAVE_EXTENSIONS)} 중 하나여야 합니다: {output.name}")
    return {
        "action": "remove",
        "input": _abspath(photo),
        "output": _abspath(output),
        "ops": selection_ops(shapes),
        "subject": bool(options.subject),
        "expectedSize": [int(image_size[0]), int(image_size[1])] if image_size else None,
        "method": options.method,
        "expand": int(options.expand),
        "feather": float(options.feather),
        "keepOpen": bool(options.keep_open),
        "jpegQuality": int(options.jpeg_quality),
        "report": report,
    }


def remove_area(photo, shapes: Sequence[Shape], output=None, options: Optional[RemoveOptions] = None,
                image_size: Optional[Tuple[int, int]] = None, overwrite: bool = False,
                photoshop: Optional[str] = None, timeout: Optional[float] = None) -> dict:
    """Open ``photo`` in Photoshop, select ``shapes``, remove them and save to ``output``.

    ``image_size`` is the (width, height) the shapes were drawn on; if Photoshop
    opens the photo at another size with the same aspect ratio, the shapes are
    scaled to match. Returns the script's report (``output``, ``warnings``, ...).
    """
    options = options or RemoveOptions()
    photo = Path(photo)
    if not photo.is_file():
        raise JobError(f"사진 파일을 찾을 수 없습니다: {photo}")
    output = Path(output) if output else default_output_path(photo, options.method)
    if output.exists() and not overwrite:
        if _same_file(output, photo):
            raise JobError("결과를 원본 사진에 덮어쓰려면 overwrite 옵션을 켜세요.")
        raise JobError(f"같은 이름의 파일이 이미 있습니다: {output}")
    config = build_remove_config(photo, output, shapes, options, image_size)
    return run_jsx(build_script(config), photoshop=photoshop, timeout=timeout)


def open_photo(photo, photoshop: Optional[str] = None, timeout: Optional[float] = None) -> dict:
    """Start Photoshop (if needed) and open ``photo`` so the user can select in Photoshop."""
    photo = Path(photo)
    if not photo.is_file():
        raise JobError(f"사진 파일을 찾을 수 없습니다: {photo}")
    config = {"action": "open", "input": _abspath(photo), "report": "return"}
    return run_jsx(build_script(config), photoshop=photoshop, timeout=timeout)


def build_remove_current_config(options: Optional[RemoveOptions] = None, output=None,
                                report: str = "return") -> dict:
    """Job description for removing the active document's current selection."""
    options = options or RemoveOptions()
    options.validate()
    if output is not None and Path(output).suffix.lower() not in SAVE_EXTENSIONS:
        raise JobError(f"저장 형식은 {', '.join(SAVE_EXTENSIONS)} 중 하나여야 합니다: {Path(output).name}")
    return {
        "action": "remove_current",
        "output": _abspath(output) if output is not None else None,
        "method": options.method,
        "expand": int(options.expand),
        "feather": float(options.feather),
        "jpegQuality": int(options.jpeg_quality),
        "report": report,
    }


def remove_current_selection(options: Optional[RemoveOptions] = None, output=None,
                             photoshop: Optional[str] = None, timeout: Optional[float] = None) -> dict:
    """Remove what is selected in Photoshop's active document right now.

    The removal is a single history step, so it can be undone in Photoshop.
    With ``output`` a copy of the document is saved there as well.
    """
    config = build_remove_current_config(options, output)
    return run_jsx(build_script(config), photoshop=photoshop, timeout=timeout)


def export_script(path, config: dict) -> Path:
    """Write a stand-alone .jsx for Photoshop's File > Scripts > Browse... menu.

    The script reports back with a dialog instead of a return value.
    """
    path = Path(path)
    if path.suffix.lower() != ".jsx":
        path = path.with_name(path.name + ".jsx")
    path.write_text(build_script(dict(config, report="alert")), encoding="ascii", newline="\n")
    return path


def _abspath(path) -> str:
    return os.path.abspath(os.fspath(path))


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False
