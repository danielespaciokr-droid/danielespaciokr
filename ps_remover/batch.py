"""Remove the same area from every photo in a folder, once or whenever new photos arrive."""

from __future__ import annotations

import dataclasses
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Collection, Dict, List, Optional, Set, Tuple, Union

from . import api
from .photoshop import PhotoshopError, PhotoshopNotFound, ScriptFailed, UnsupportedPlatform
from .shapes import Area, AreaSet, area_has_shapes

PHOTO_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".psd", ".psb", ".bmp", ".gif", ".webp", ".heic", ".heif",
    ".dng", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2", ".raf",
)
DEFAULT_OUTPUT_FOLDER = "지운 사진"
DEFAULT_WATCH_INTERVAL = 1.0  # seconds between looks at the folder in watch mode
SETTLE_POLL_SECONDS = 0.5  # how soon to look again at a photo that may still be copying
SETTLE_SECONDS = 0.5  # a photo must also be this long untouched
BUSY_GIVE_UP_SECONDS = 60.0  # a single run gives up on a photo that stays busy this long

Snapshot = Tuple[int, float]  # (size, modification time)


@dataclass
class BatchJob:
    """Remove ``area`` from the photos in ``input_dir``; results go to ``output_dir``."""

    input_dir: Path
    area: Union[Area, AreaSet]
    output_dir: Optional[Path] = None  # default: input_dir / DEFAULT_OUTPUT_FOLDER
    options: api.RemoveOptions = field(default_factory=api.RemoveOptions)
    suffix: str = api.DEFAULT_SUFFIX
    skip_done: bool = True  # leave a photo alone when its result already exists
    photoshop: Optional[str] = None
    timeout: Optional[float] = None

    def __post_init__(self) -> None:
        self.input_dir = Path(self.input_dir)
        self.output_dir = Path(self.output_dir) if self.output_dir else self.input_dir / DEFAULT_OUTPUT_FOLDER
        # One photo after another: leaving each open in Photoshop would pile up documents.
        self.options = dataclasses.replace(self.options, keep_open=False)
        self.options.validate()
        if not self.input_dir.is_dir():
            raise api.JobError(f"사진 폴더를 찾을 수 없습니다: {self.input_dir}")
        if not area_has_shapes(self.area) and not self.options.subject:
            raise api.JobError("지울 영역이 없습니다. 공통 영역을 고르거나 '피사체 선택'을 켜세요.")
        if not self.suffix and _same_dir(self.input_dir, self.output_dir):
            raise api.JobError("결과를 사진과 같은 폴더에 저장하려면 결과 파일 이름에 붙일 말이 필요합니다.")
        self._result_name = re.compile(re.escape(self.suffix) + r"(_\d+)?$") if self.suffix else None

    def output_for(self, photo: Path) -> Path:
        return api.default_output_path(photo, self.options.method, self.suffix, self.output_dir, unique=False)

    def candidates(self, skip: Collection[Path] = ()) -> List[Tuple[Path, Snapshot]]:
        """Photos still to process, each with a (size, modification time) snapshot.

        Two directory listings per call, so looking every second stays cheap in
        a folder that keeps growing.
        """
        done = self._done_names() if self.skip_done else set()
        found = []
        with os.scandir(self.input_dir) as entries:
            for entry in entries:
                path = Path(entry.path)
                if path in skip or not self._is_photo(entry) or self.output_for(path).name.casefold() in done:
                    continue
                try:
                    stat = entry.stat()
                except OSError:
                    continue  # removed meanwhile
                found.append((path, (stat.st_size, stat.st_mtime)))
        found.sort(key=lambda item: item[0].name.casefold())
        return found

    def _done_names(self) -> Set[str]:
        try:
            return {name.casefold() for name in os.listdir(self.output_dir)}
        except OSError:
            return set()

    def _is_photo(self, entry: os.DirEntry) -> bool:
        name, ext = os.path.splitext(entry.name)
        if entry.name.startswith(".") or ext.lower() not in PHOTO_EXTENSIONS or not entry.is_file():
            return False
        # A result saved next to the photos ("photo_removed.jpg", "photo_removed_2.jpg").
        return not (self._result_name and self._result_name.search(name))


class BatchRunner:
    """Runs a :class:`BatchJob` on the calling thread and reports through ``on_event``.

    Each event is a dict with a "type":
      start     photo, index, count     about to process a photo of this round
      done      photo, result
      failed    photo, error, result    that photo failed (it is not retried); the run goes on
      error     error                   Photoshop trouble; watch mode tries again later
      waiting                           watch mode: no new photos right now
      finished  done, failed, error     the run is over
    """

    def __init__(self, job: BatchJob, on_event: Callable[[dict], None], remove: Optional[Callable] = None):
        self.job = job
        self.on_event = on_event
        self._remove = remove or api.remove_area
        self._stop = threading.Event()
        self.handled: Set[Path] = set()
        self.failed: Set[Path] = set()
        self.done = 0
        self._snapshots: Dict[Path, Snapshot] = {}
        self._busy_since: Dict[Path, float] = {}

    def stop(self) -> None:
        """Finish the photo in progress, then end the run."""
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def run(self, watch: bool = False, interval: float = DEFAULT_WATCH_INTERVAL) -> None:
        error = None
        waiting = False
        while not self._stop.is_set():
            ready, settling = self._check(self.job.candidates(skip=self.handled), time.time(), watch)
            if ready:
                waiting = False
                status, error = self._process(ready, watch)
                if status == "fatal":
                    break
                if status == "retry":
                    self._stop.wait(interval)
                continue  # look again at once: more photos may have arrived meanwhile
            if settling:
                self._stop.wait(SETTLE_POLL_SECONDS)
                continue
            if not watch:
                break
            if not waiting:
                self._emit("waiting")
                waiting = True
            self._stop.wait(interval)
        self._emit("finished", done=self.done, failed=len(self.failed), error=error)

    def _check(self, candidates: List[Tuple[Path, Snapshot]], now: float, watch: bool) -> Tuple[List[Path], int]:
        """``(photos ready to process, number that may still be copying)``.

        A photo is ready once it looks the same as at the previous look and, on
        Windows, no program still has it open for writing.
        """
        previous, self._snapshots = self._snapshots, {}
        ready: List[Path] = []
        settling = 0
        for path, snapshot in candidates:
            self._snapshots[path] = snapshot
            size, mtime = snapshot
            steady = size > 0 and previous.get(path) == snapshot and not 0 <= now - mtime < SETTLE_SECONDS
            if steady and not file_in_use(path):
                self._busy_since.pop(path, None)
                ready.append(path)
                continue
            since = self._busy_since.setdefault(path, now)
            if not watch and now - since > BUSY_GIVE_UP_SECONDS:
                self._busy_since.pop(path)
                self._fail(path, "파일이 계속 바뀌거나 다른 프로그램이 쓰고 있어서 처리하지 못했습니다.", None)
            else:
                settling += 1
        return ready, settling

    def _process(self, photos: List[Path], watch: bool) -> Tuple[str, Optional[str]]:
        self.job.output_dir.mkdir(parents=True, exist_ok=True)
        for index, photo in enumerate(photos):
            if self._stop.is_set():
                break
            self._emit("start", photo=photo, index=index, count=len(photos))
            try:
                result = self._remove(photo, self.job.area, self.job.output_for(photo), self.job.options,
                                      overwrite=not self.job.skip_done, photoshop=self.job.photoshop,
                                      timeout=self.job.timeout)
            except ScriptFailed as exc:
                self._fail(photo, str(exc), exc.result)
            except (PhotoshopNotFound, UnsupportedPlatform) as exc:
                self._emit("error", error=str(exc))
                return "fatal", str(exc)
            except PhotoshopError as exc:
                # Busy, timed out, ...: nothing wrong with the photo itself.
                self._emit("error", error=str(exc))
                return ("retry", str(exc)) if watch else ("fatal", str(exc))
            except Exception as exc:  # noqa: BLE001 - one bad photo must not end a long run
                self._fail(photo, str(exc) or exc.__class__.__name__, None)
            else:
                self.handled.add(photo)
                self.done += 1
                self._emit("done", photo=photo, result=result)
        return "ok", None

    def _fail(self, photo: Path, error: str, result: Optional[dict]) -> None:
        self.handled.add(photo)
        self.failed.add(photo)
        self._emit("failed", photo=photo, error=error, result=result)

    def _emit(self, kind: str, **values) -> None:
        self.on_event(dict(values, type=kind))


def file_in_use(path: Path) -> bool:
    """True while another program still has ``path`` open for writing (checked on Windows only).

    Copying on Windows can create a file at its final size before the data is
    in, so an unchanged size does not prove that the copy has finished.
    """
    if sys.platform != "win32":
        return False
    create_file, close_handle, invalid = _win32_file_api()
    # Sharing with readers only is refused while anyone holds the file open for writing.
    handle = create_file(str(path), 0x80000000, 0x1, None, 3, 0, None)  # GENERIC_READ, FILE_SHARE_READ, OPEN_EXISTING
    if handle is None or handle == invalid:
        return True
    close_handle(handle)
    return False


_WIN32_FILE_API = None


def _win32_file_api():
    global _WIN32_FILE_API
    if _WIN32_FILE_API is None:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        create_file.restype = wintypes.HANDLE
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        _WIN32_FILE_API = (create_file, close_handle, wintypes.HANDLE(-1).value)
    return _WIN32_FILE_API


def _same_dir(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.abspath(a) == os.path.abspath(b)
