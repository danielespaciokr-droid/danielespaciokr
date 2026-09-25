"""Command line interface. Without arguments the GUI starts."""

from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from . import __version__, api
from .batch import DEFAULT_OUTPUT_FOLDER, DEFAULT_WATCH_INTERVAL, BatchJob, BatchRunner
from .photoshop import PhotoshopError, ScriptFailed
from .settings import check_preset_name, delete_preset, list_presets, load_preset, presets_dir
from .shapes import (ORIENTATION_LABELS, Area, SelectionError, area_has_shapes, ellipse, load_area_set,
                     parse_box, parse_points, polygon, rect)

DESCRIPTION = "Photoshop을 실행해 사진을 열고, 지정한 영역을 선택해 지우는 도구입니다."

EPILOG = """\
예시:
  ps-remover                                            GUI 실행
  ps-remover remove 사진.jpg --rect 120,80,300,200      사각형 영역을 내용 인식 채우기로 지우기
  ps-remover remove 사진.jpg --ellipse 50,60,40,40 --polygon "10,10 90,15 60,80"
  ps-remover remove 인물.jpg --subject                  Photoshop '피사체 선택'으로 찾은 대상을 지우기
  ps-remover batch 사진폴더 --preset 워터마크           폴더의 새 사진마다 공통 영역 지우기
  ps-remover batch 사진폴더 --preset 워터마크 --watch   새 사진이 들어올 때마다 계속 지우기
  ps-remover batch 사진폴더 --preset 워터마크 --watch --adjust
                                                        지운 뒤 녹화해 둔 보정 동작(Camera Raw 필터 등)까지 적용
  ps-remover watch                                      폴더 자동 처리 창만 열기 (저장된 설정으로 바로 시작)
  ps-remover presets                                    저장된 공통 영역 보기
  ps-remover remove 사진.jpg --rect 120,80,300,200 --method action
                                                        녹화해 둔 동작으로 Photoshop [제거] 버튼 쓰기
  ps-remover check-action                               [제거] 버튼 동작이 녹화되어 있는지 확인
  ps-remover check-action --action 보정                 보정 동작이 녹화되어 있는지 확인
  ps-remover open 사진.jpg --preset 워터마크            Photoshop에서 사진을 열고 공통 영역을 선택해 두기
  ps-remover remove-selection                           Photoshop에서 직접 선택한 영역 지우기
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ps-remover",
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="명령")

    gui = commands.add_parser("gui", help="GUI 실행 (기본값)")
    gui.add_argument("photo", nargs="?", help="처음에 열 사진")

    watch = commands.add_parser(
        "watch",
        help="폴더 자동 처리 창만 열기 (새 사진이 들어오면 바로 지움)",
        description="폴더 자동 처리 창을 엽니다. 창에 저장된 사진 폴더와 공통 영역으로 감시를 시작해, "
                    "사진이 들어오는 대로 공통 영역을 지웁니다.",
    )
    watch.add_argument("--start", action="store_true",
                       help="'창을 열면 바로 시작'이 꺼져 있어도 바로 시작 (Windows 시작 시 자동 실행용)")

    remove = commands.add_parser(
        "remove",
        help="사진을 Photoshop에서 열어 지정한 영역을 지우고 저장",
        description="사진을 Photoshop에서 열고, 지정한 영역을 선택해 지운 뒤 새 파일로 저장합니다. "
                    "좌표는 사진의 픽셀 단위이며 (0,0)은 왼쪽 위입니다.",
    )
    remove.add_argument("photos", nargs="+", metavar="사진", help="처리할 사진 (여러 개 가능)")
    _add_area_options(remove)
    _add_removal_options(remove)
    out = remove.add_argument_group("저장")
    out.add_argument("-o", "--output", help="결과 파일 경로 (사진이 하나일 때만). 기본값: 사진이름_removed.확장자")
    out.add_argument("--output-dir", help="결과를 저장할 폴더 (기본값: 원본과 같은 폴더)")
    out.add_argument("--suffix", default=api.DEFAULT_SUFFIX, help="결과 파일 이름에 붙일 말 (기본값: %(default)s)")
    out.add_argument("--overwrite", action="store_true", help="같은 이름의 파일이 있으면 덮어쓰기")
    keep = out.add_mutually_exclusive_group()
    keep.add_argument("--keep-open", dest="keep_open", action="store_true", default=None,
                      help="끝난 뒤 결과를 Photoshop에 열어 두기 (사진이 하나일 때 기본값)")
    keep.add_argument("--no-keep-open", dest="keep_open", action="store_false",
                      help="결과를 Photoshop에 열어 두지 않기 (여러 장일 때 기본값)")
    out.add_argument("--export-jsx", metavar="파일.jsx",
                     help="Photoshop을 실행하지 않고, [파일 > 스크립트 > 찾아보기]로 실행할 스크립트만 저장")
    _add_common_options(remove)

    batch = commands.add_parser(
        "batch",
        help="폴더 안의 사진마다 공통 영역을 지움 (이미 지운 사진은 건너뜀, 폴더 감시 가능)",
        description="폴더 안의 사진마다 같은 영역을 지웁니다. 결과가 이미 있는 사진은 건너뛰므로, "
                    "새 사진이 생길 때마다 다시 실행하면 새 사진만 처리합니다. "
                    "--watch를 쓰면 폴더를 계속 지켜보다가 새 사진이 들어오는 대로 지웁니다.",
    )
    batch.add_argument("folder", metavar="폴더", help="사진이 들어 있는 폴더")
    _add_area_options(batch)
    _add_removal_options(batch)
    group = batch.add_argument_group("저장과 반복")
    group.add_argument("--output-dir", help=f"결과를 저장할 폴더 (기본값: 폴더/{DEFAULT_OUTPUT_FOLDER})")
    group.add_argument("--suffix", default=api.DEFAULT_SUFFIX, help="결과 파일 이름에 붙일 말 (기본값: %(default)s)")
    group.add_argument("--reprocess", action="store_true", help="이미 지운 사진도 다시 처리해서 덮어쓰기")
    group.add_argument("--watch", action="store_true",
                       help="끝난 뒤에도 폴더를 지켜보다가 새 사진이 들어오면 지우기 (Ctrl+C로 끝내기)")
    group.add_argument("--interval", type=float, default=DEFAULT_WATCH_INTERVAL, metavar="초",
                       help="--watch일 때 폴더를 확인하는 간격 (기본값: %(default)s초)")
    _add_common_options(batch)

    presets = commands.add_parser(
        "presets",
        help="저장된 공통 영역 보기",
        description="GUI에서 저장한 공통 영역 목록을 보여 줍니다. --preset 이름으로 remove, batch, open에 쓸 수 있습니다.",
    )
    presets.add_argument("--delete", metavar="이름", help="이 공통 영역을 지우기")
    presets.add_argument("--json", action="store_true", help="결과를 JSON으로 출력")

    open_cmd = commands.add_parser(
        "open",
        help="Photoshop을 실행해 사진을 열기 (영역을 주면 선택까지)",
        description="Photoshop에서 사진을 엽니다. 영역을 주면 그 영역을 선택해 두므로, "
                    "Photoshop 작업 표시줄의 [제거] 버튼을 바로 누르거나 동작을 녹화할 수 있습니다.",
    )
    open_cmd.add_argument("photo", metavar="사진")
    _add_area_options(open_cmd, subject=False)
    _add_refine_options(open_cmd.add_argument_group("선택 다듬기"))
    _add_common_options(open_cmd)

    current = commands.add_parser(
        "remove-selection",
        help="Photoshop에서 직접 선택한 영역을 지움",
        description="Photoshop에서 활성 문서의 현재 선택 영역을 지웁니다. Photoshop에서 [실행 취소] 한 번으로 되돌릴 수 있습니다.",
    )
    _add_removal_options(current, adjust=False)
    current.add_argument("-o", "--output", help="결과를 이 경로에도 저장 (생략하면 저장하지 않음)")
    current.add_argument("--export-jsx", metavar="파일.jsx", help="Photoshop 스크립트 파일로 저장만 하기")
    _add_common_options(current)

    check = commands.add_parser(
        "check-action",
        help="--method action에 쓸 Photoshop 동작이 녹화되어 있는지 확인",
        description="Photoshop 동작 패널에서 동작을 찾아, 녹화된 단계를 보여 줍니다.",
    )
    _add_action_options(check)
    _add_common_options(check)
    return parser


def _add_area_options(parser: argparse.ArgumentParser, subject: bool = True) -> None:
    area = parser.add_argument_group("지울 영역")
    area.add_argument("--preset", metavar="이름", help="GUI에서 저장한 공통 영역 (ps-remover presets로 목록 보기)")
    area.add_argument("--selection", metavar="파일.json", help="선택 영역 파일")
    area.add_argument("--rect", action="append", default=[], metavar="X,Y,W,H",
                      help="사각형: 왼쪽 위 X,Y와 너비,높이 (여러 번 쓸 수 있음)")
    area.add_argument("--ellipse", action="append", default=[], metavar="X,Y,W,H", help="타원: 감싸는 사각형의 X,Y,너비,높이")
    area.add_argument("--polygon", action="append", default=[], metavar='"X,Y X,Y ..."', help="다각형: 꼭짓점 목록 (3개 이상)")
    if subject:
        area.add_argument("--subject", action="store_true",
                          help="Photoshop '피사체 선택' 사용. 영역을 함께 주면 그 안의 피사체만 지움 (CC 2018 이상)")


def _add_removal_options(parser: argparse.ArgumentParser, adjust: bool = True) -> None:
    group = parser.add_argument_group("제거 방식")
    group.add_argument("--method", choices=api.METHODS, default="content-aware",
                       help="content-aware: 주변 내용으로 자연스럽게 채움 (기본값), "
                            "action: 녹화해 둔 Photoshop 동작 실행 (작업 표시줄의 [제거] 버튼 등), "
                            "transparent: 투명하게 잘라냄")
    _add_action_options(group)
    _add_refine_options(group)
    group.add_argument("--jpeg-quality", type=int, default=12, metavar="0-12",
                       help="JPG로 저장할 때 품질 (기본값: %(default)s)")
    if adjust:
        extra = parser.add_argument_group("보정")
        extra.add_argument("--adjust", action="store_true",
                           help="지운 뒤 녹화해 둔 Photoshop 동작(Camera Raw 필터 등)을 사진 전체에 적용")
        extra.add_argument("--adjust-set", default=api.DEFAULT_ACTION_SET, metavar="세트",
                           help="보정 동작의 세트 이름 (기본값: %(default)s)")
        extra.add_argument("--adjust-action", dest="adjust_name", default=api.DEFAULT_ADJUST_NAME, metavar="동작",
                           help="보정 동작 이름 (기본값: %(default)s)")


def _add_refine_options(group) -> None:
    group.add_argument("--expand", type=int, default=4, metavar="PX",
                       help="선택 영역을 넓힐 픽셀 수 (기본값: %(default)s)")
    group.add_argument("--feather", type=float, default=0.0, metavar="PX",
                       help="선택 영역 가장자리를 부드럽게 할 픽셀 수 (기본값: %(default)s)")


def _add_action_options(group) -> None:
    group.add_argument("--action-set", default=api.DEFAULT_ACTION_SET, metavar="세트",
                       help="Photoshop 동작 세트 이름 (기본값: %(default)s)")
    group.add_argument("--action", dest="action_name", default=api.DEFAULT_ACTION_NAME, metavar="동작",
                       help="Photoshop 동작 이름 (기본값: %(default)s)")


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--photoshop", metavar="앱",
                        help="macOS에서 사용할 Photoshop 앱 이름 또는 경로 (예: \"Adobe Photoshop 2025\")")
    parser.add_argument("--timeout", type=float, metavar="초", help="Photoshop 작업 제한 시간")
    parser.add_argument("--json", action="store_true", help="결과를 JSON으로 출력")


def main(argv: Optional[Sequence[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # Korean text on a non-UTF-8 console
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    if args.command in (None, "gui"):
        from .gui import main as gui_main

        return gui_main(getattr(args, "photo", None))
    if args.command == "watch":
        from .gui import watch_main

        return watch_main(start=args.start)
    commands = {
        "remove": _cmd_remove,
        "batch": _cmd_batch,
        "presets": _cmd_presets,
        "open": _cmd_open,
        "remove-selection": _cmd_remove_selection,
        "check-action": _cmd_check_action,
    }
    try:
        return commands[args.command](args)
    except (SelectionError, api.JobError, PhotoshopError, OSError) as exc:
        _error(str(exc))
        return 1


def _cmd_remove(args) -> int:
    area = _collect_area(args)
    options = _removal_options(args)
    photos = _expand_photos(args.photos)
    if args.output and len(photos) > 1:
        raise api.JobError("--output은 사진이 하나일 때만 쓸 수 있습니다. 여러 장은 --output-dir을 쓰세요.")
    options.keep_open = args.keep_open if args.keep_open is not None else len(photos) == 1
    if args.output_dir:
        Path(args.output_dir).mkdir(parents=True, exist_ok=True)

    if args.export_jsx:
        if len(photos) > 1:
            raise api.JobError("--export-jsx는 사진이 하나일 때만 쓸 수 있습니다.")
        output = _output_for(photos[0], args, options)
        config = api.build_remove_config(photos[0], output, area, options)
        path = api.export_script(args.export_jsx, config)
        _print(args, {"ok": True, "script": str(path), "output": str(output)},
               f"스크립트를 저장했습니다: {path}\nPhotoshop의 [파일 > 스크립트 > 찾아보기...]에서 실행하세요.")
        return 0

    failures = 0
    for index, photo in enumerate(photos):
        try:
            output = _output_for(photo, args, options)
            result = api.remove_area(photo, area, output, options, overwrite=args.overwrite,
                                     photoshop=args.photoshop, timeout=args.timeout)
        except ScriptFailed as exc:
            failures += 1
            _report_failure(args, photo, str(exc), exc.result)
            continue
        except api.JobError as exc:
            failures += 1
            _report_failure(args, photo, str(exc), None)
            continue
        except PhotoshopError as exc:
            # Photoshop itself is unavailable, so the remaining photos would fail the same way.
            _report_failure(args, photo, str(exc), None)
            skipped = len(photos) - index - 1
            if skipped:
                _error(f"나머지 사진 {skipped}장은 처리하지 않았습니다.")
            return 1
        _print(args, dict(result, input=str(photo)), _success_text(photo, result))
    return 1 if failures else 0


def _cmd_batch(args) -> int:
    job = BatchJob(
        Path(args.folder), _collect_area(args), Path(args.output_dir) if args.output_dir else None,
        _removal_options(args), suffix=args.suffix, skip_done=not args.reprocess,
        photoshop=args.photoshop, timeout=args.timeout,
    )
    report = _BatchReport(args)
    runner = BatchRunner(job, report)
    if not args.json:
        print(f"사진 폴더: {job.input_dir}\n결과 폴더: {job.output_dir}", flush=True)
    try:
        runner.run(watch=args.watch, interval=args.interval)
    except KeyboardInterrupt:
        print(f"멈췄습니다. {runner.done}장 완료, {len(runner.failed)}장 실패", flush=True)
        return 1 if runner.failed else 0
    return 1 if runner.failed or report.fatal else 0


class _BatchReport:
    """Prints :class:`BatchRunner` events."""

    def __init__(self, args):
        self.json = args.json
        self.fatal: Optional[str] = None

    def __call__(self, event: dict) -> None:
        kind = event["type"]
        if kind == "finished":
            self.fatal = event["error"]
        if self.json:
            print(json.dumps({k: str(v) if isinstance(v, Path) else v for k, v in event.items()},
                             ensure_ascii=False), flush=True)
            return
        if kind == "start":
            print(f"[{event['index'] + 1}/{event['count']}] {event['photo'].name}", flush=True)
        elif kind == "done":
            print(f"    완료 → {event['result'].get('output')}{_warnings_text(event['result'], '    ')}", flush=True)
        elif kind == "failed":
            _error(f"{event['photo'].name}: {event['error']}")
        elif kind == "error":
            _error(event["error"])
            if event.get("retry"):
                print("    이 문제가 해결되면 저절로 이어서 처리합니다.", flush=True)
        elif kind == "waiting":
            print("새 사진을 기다리는 중입니다... (끝내려면 Ctrl+C)", flush=True)
        elif kind == "finished":
            counts = f"{event['done']}장 완료, {event['failed']}장 실패"
            if event["error"]:
                text = f"멈췄습니다: {counts} (위 오류 때문에)"
            elif event["done"] or event["failed"]:
                text = f"끝났습니다: {counts}"
            else:
                text = "새로 지울 사진이 없습니다."
            print(text, flush=True)


def _cmd_presets(args) -> int:
    if args.delete:
        name = check_preset_name(args.delete)
        load_preset(name)  # explains which presets exist if the name is wrong
        delete_preset(name)
        print(f"공통 영역 '{name}'을(를) 지웠습니다.")
        return 0
    entries = []
    for name in list_presets():
        try:
            entries.append((name, load_preset(name), None))
        except SelectionError as exc:
            entries.append((name, None, str(exc)))
    if args.json:
        print(json.dumps([dict(area.to_dict(), name=name) if area else {"name": name, "error": error}
                          for name, area, error in entries], ensure_ascii=False, indent=2))
        return 0
    if not entries:
        print("저장된 공통 영역이 없습니다. GUI에서 지울 영역을 그린 뒤 [지금 영역 저장...]을 누르세요.")
    for name, area, error in entries:
        if area is None:
            print(f"- {name}: 읽을 수 없음 ({error})")
            continue
        print(f"- {name}")
        for orientation, part in area.areas.items():
            size = f", {part.image_size[0]}x{part.image_size[1]} 사진 기준" if part.image_size else ""
            print(f"    {ORIENTATION_LABELS[orientation]}용: 도형 {len(part.shapes)}개{size}, {part.describe()}")
        if len(area.areas) == 1:
            other = "세로" if "landscape" in area.areas else "가로"
            print(f"    ({other} 사진에도 위 영역을 맞춰서 씁니다)")
    print(f"저장 위치: {presets_dir()}")
    return 0


def _cmd_open(args) -> int:
    area = _collect_area(args, required=False)
    options = api.RemoveOptions(expand=args.expand, feather=args.feather)
    result = api.open_photo(args.photo, area if area_has_shapes(area) else None, options,
                            photoshop=args.photoshop, timeout=args.timeout)
    text = f"Photoshop에서 열었습니다: {args.photo}"
    if result.get("selectionBounds"):
        text += " (영역을 선택해 두었습니다)"
    _print(args, result, text + _warnings_text(result))
    return 0


def _cmd_remove_selection(args) -> int:
    options = _removal_options(args)
    if args.export_jsx:
        config = api.build_remove_current_config(options, args.output)
        path = api.export_script(args.export_jsx, config)
        _print(args, {"ok": True, "script": str(path)}, f"스크립트를 저장했습니다: {path}")
        return 0
    result = api.remove_current_selection(options, args.output, photoshop=args.photoshop, timeout=args.timeout)
    text = "Photoshop에서 선택 영역을 지웠습니다. (Photoshop에서 실행 취소로 되돌릴 수 있습니다)"
    if result.get("output"):
        text += f"\n저장: {result['output']}"
    _print(args, result, text + _warnings_text(result))
    return 0


def _cmd_check_action(args) -> int:
    info = api.find_recorded_action(args.action_set, args.action_name, photoshop=args.photoshop, timeout=args.timeout)
    label = f"'{args.action_set} > {args.action_name}'"
    if info.get("found") and info.get("stepCount") != 0:
        text = f"Photoshop 동작 {label}을(를) 찾았습니다. 녹화된 단계: {', '.join(info.get('steps') or []) or '(알 수 없음)'}"
        _print(args, dict(info, ok=True), text)
        return 0
    if info.get("found"):
        reason = "동작은 있지만 녹화된 단계가 없습니다."
    elif info.get("setFound"):
        reason = f"'{args.action_set}' 세트는 있지만 '{args.action_name}' 동작이 없습니다."
    else:
        reason = f"'{args.action_set}' 세트가 없습니다."
    _print(args, dict(info, ok=False), f"Photoshop 동작 {label}을(를) 쓸 수 없습니다: {reason}\n{api.ACTION_SETUP_HELP}")
    return 1


def _expand_photos(patterns: Sequence[str]) -> List[Path]:
    """Expand wildcards ourselves: the Windows command prompt passes "*.jpg" through as is."""
    photos: List[Path] = []
    for pattern in patterns:
        if any(ch in pattern for ch in "*?[") and not Path(pattern).exists():
            matches = sorted(glob.glob(pattern))
            if not matches:
                raise api.JobError(f"맞는 파일이 없습니다: {pattern}")
            photos.extend(Path(m) for m in matches)
        else:
            photos.append(Path(pattern))
    return photos


def _collect_area(args, required: bool = True):
    drawn = ([rect(*parse_box(text)) for text in args.rect]
             + [ellipse(*parse_box(text)) for text in args.ellipse]
             + [polygon(parse_points(text)) for text in args.polygon])
    if args.preset and args.selection:
        raise api.JobError("--preset과 --selection은 함께 쓸 수 없습니다.")
    if (args.preset or args.selection) and drawn:
        raise api.JobError("--preset이나 --selection에 --rect, --ellipse, --polygon을 더할 수는 없습니다.")
    if args.preset:
        area = load_preset(args.preset)
    elif args.selection:
        area = load_area_set(args.selection)
    else:
        area = Area(drawn)
    if required and not area_has_shapes(area) and not getattr(args, "subject", False):
        raise api.JobError("지울 영역을 --preset, --selection, --rect, --ellipse, --polygon 또는 --subject로 지정하세요.")
    return area


def _removal_options(args) -> api.RemoveOptions:
    options = api.RemoveOptions(
        method=args.method,
        action_set=args.action_set,
        action_name=args.action_name,
        expand=args.expand,
        feather=args.feather,
        subject=getattr(args, "subject", False),
        jpeg_quality=args.jpeg_quality,
        adjust=getattr(args, "adjust", False),
        adjust_set=getattr(args, "adjust_set", api.DEFAULT_ACTION_SET),
        adjust_name=getattr(args, "adjust_name", api.DEFAULT_ADJUST_NAME),
    )
    options.validate()
    return options


def _output_for(photo: Path, args, options: api.RemoveOptions) -> Path:
    if args.output:
        return Path(args.output)
    # With --overwrite reuse "photo_removed.jpg" instead of numbering new files.
    return api.default_output_path(photo, options.method, args.suffix, args.output_dir, unique=not args.overwrite)


def _success_text(photo: Path, result: dict) -> str:
    text = f"완료: {photo} → {result.get('output')}"
    bounds = result.get("selectionBounds")
    if bounds:
        left, top, right, bottom = (round(v) for v in bounds)
        text += f"  (지운 영역: X {left}, Y {top}, {right - left}x{bottom - top})"
    return text + _warnings_text(result)


def _warnings_text(result: Optional[dict], indent: str = "") -> str:
    warnings = (result or {}).get("warnings") or []
    return "".join(f"\n{indent}  참고: {w}" for w in warnings)


def _report_failure(args, photo: Path, message: str, result: Optional[dict]) -> None:
    if args.json:
        print(json.dumps({"ok": False, "input": str(photo), "error": message, "result": result}, ensure_ascii=False))
        return
    note = "\n  작업하던 사진은 Photoshop에 열어 두었습니다." if result and result.get("leftOpen") else ""
    _error(f"{photo}: {message}{note}{_warnings_text(result)}")


def _print(args, data: dict, text: str) -> None:
    print(json.dumps(data, ensure_ascii=False) if args.json else text)


def _error(message: str) -> None:
    print(f"오류: {message}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    sys.exit(main())
