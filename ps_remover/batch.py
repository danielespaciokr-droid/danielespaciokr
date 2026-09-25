"""Remove the same area from every photo in a folder, once or whenever new photos arrive."""

from __future__ import annotations

import dataclasses
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Collection, List, Optional, Set, Tuple

from . import api
from .photoshop import PhotoshopError, PhotoshopNotFound, ScriptFailed, UnsupportedPlatform
from .shapes import Area, has_area

PHOTO_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".psd", ".psb", ".bmp", ".gif", ".webp", ".heic", ".heif",
    ".dng", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2", ".raf",
)
DEFAULT_OUTPUT_FOLDER = "지운 사진"
SETTLE_SECONDS = 3.0  # a file changed more recently than this may still be copying
SETTLE_POLL_SECONDS = 1.0


@dataclass
class BatchJob:
    """Remove ``area`` from the photos in ``input_dir``; results go to ``output_dir``."""

    input_dir: Path
    area: Area
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
        if not has_area(self.area.shapes) and not self.options.subject:
            raise api.JobError("지울 영역이 없습니다. 공통 영역을 고르거나 '피사체 선택'을 켜세요.")
        if not self.suffix and _same_dir(self.input_dir, self.output_dir):
            raise api.JobError("결과를 사진과 같은 폴더에 저장하려면 결과 파일 이름에 붙일 말이 필요합니다.")
        self._result_name = re.compile(re.escape(self.suffix) + r"(_\d+)?$") if self.suffix else None

    def output_for(self, photo: Path) -> Path:
        return api.default_output_path(photo, self.options.method, self.suffix, self.output_dir, unique=False)

    def scan(self, skip: Collection[Path] = (), now: Optional[float] = None) -> Tuple[List[Path], int]:
        """``(photos ready to process, number of photos still being copied)``."""
        now = time.time() if now is None else now
        ready: List[Path] = []
        settling = 0
        for path in sorted(self.input_dir.iterdir(), key=lambda p: p.name.casefold()):
            if path in skip or not self._is_photo(path):
                continue
            try:
                age = now - path.stat().st_mtime
            except OSError:
                continue
            if 0 <= age < SETTLE_SECONDS:
                settling += 1
            elif not (self.skip_done and self.output_for(path).exists()):
                ready.append(path)
        return ready, settling

    def _is_photo(self, path: Path) -> bool:
        if path.name.startswith(".") or path.suffix.lower() not in PHOTO_EXTENSIONS or not path.is_file():
            return False
        # A result saved next to the photos ("photo_removed.jpg", "photo_removed_2.jpg").
        return not (self._result_name and self._result_name.search(path.stem))


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

    def stop(self) -> None:
        """Finish the photo in progress, then end the run."""
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def run(self, watch: bool = False, interval: float = 10.0) -> None:
        error = None
        waiting = False
        while not self._stop.is_set():
            ready, settling = self.job.scan(skip=self.handled)
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


def _same_dir(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.abspath(a) == os.path.abspath(b)
