"""Command line interface. Without arguments the GUI starts."""

from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from . import __version__, api
from .photoshop import PhotoshopError, ScriptFailed
from .shapes import SelectionError, Shape, ellipse, load_selection, parse_box, parse_points, polygon, rect

DESCRIPTION = "Photoshop을 실행해 사진을 열고, 지정한 영역을 선택해 지우는 도구입니다."

EPILOG = """\
예시:
  ps-remover                                            GUI 실행
  ps-remover remove 사진.jpg --rect 120,80,300,200      사각형 영역을 내용 인식 채우기로 지우기
  ps-remover remove 사진.jpg --ellipse 50,60,40,40 --polygon "10,10 90,15 60,80"
  ps-remover remove *.jpg --selection 워터마크.json --output-dir 결과
  ps-remover remove 인물.jpg --subject                  Photoshop '피사체 선택'으로 찾은 대상을 지우기
  ps-remover open 사진.jpg                              Photoshop에서 사진만 열기
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

    remove = commands.add_parser(
        "remove",
        help="사진을 Photoshop에서 열어 지정한 영역을 지우고 저장",
        description="사진을 Photoshop에서 열고, 지정한 영역을 선택해 지운 뒤 새 파일로 저장합니다. "
                    "좌표는 사진의 픽셀 단위이며 (0,0)은 왼쪽 위입니다.",
    )
    remove.add_argument("photos", nargs="+", metavar="사진", help="처리할 사진 (여러 개 가능)")
    area = remove.add_argument_group("지울 영역 (여러 번, 섞어서 쓸 수 있음)")
    area.add_argument("--rect", action="append", default=[], metavar="X,Y,W,H", help="사각형: 왼쪽 위 X,Y와 너비,높이")
    area.add_argument("--ellipse", action="append", default=[], metavar="X,Y,W,H", help="타원: 감싸는 사각형의 X,Y,너비,높이")
    area.add_argument("--polygon", action="append", default=[], metavar='"X,Y X,Y ..."', help="다각형: 꼭짓점 목록 (3개 이상)")
    area.add_argument("--selection", metavar="파일.json", help="GUI에서 저장한 선택 영역 파일")
    area.add_argument("--subject", action="store_true",
                      help="Photoshop '피사체 선택' 사용. 영역을 함께 주면 그 안의 피사체만 지움 (CC 2018 이상)")
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

    open_cmd = commands.add_parser("open", help="Photoshop을 실행해 사진을 열기만 함")
    open_cmd.add_argument("photo", metavar="사진")
    _add_common_options(open_cmd)

    current = commands.add_parser(
        "remove-selection",
        help="Photoshop에서 직접 선택한 영역을 지움",
        description="Photoshop에서 활성 문서의 현재 선택 영역을 지웁니다. Photoshop에서 [실행 취소] 한 번으로 되돌릴 수 있습니다.",
    )
    _add_removal_options(current)
    current.add_argument("-o", "--output", help="결과를 이 경로에도 저장 (생략하면 저장하지 않음)")
    current.add_argument("--export-jsx", metavar="파일.jsx", help="Photoshop 스크립트 파일로 저장만 하기")
    _add_common_options(current)
    return parser


def _add_removal_options(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("제거 방식")
    group.add_argument("--method", choices=api.METHODS, default="content-aware",
                       help="content-aware: 주변 내용으로 자연스럽게 채움 (기본값), transparent: 투명하게 잘라냄")
    group.add_argument("--expand", type=int, default=4, metavar="PX",
                       help="지우기 전에 선택 영역을 넓힐 픽셀 수 (기본값: %(default)s)")
    group.add_argument("--feather", type=float, default=0.0, metavar="PX",
                       help="선택 영역 가장자리를 부드럽게 할 픽셀 수 (기본값: %(default)s)")
    group.add_argument("--jpeg-quality", type=int, default=12, metavar="0-12",
                       help="JPG로 저장할 때 품질 (기본값: %(default)s)")


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
    try:
        if args.command == "remove":
            return _cmd_remove(args)
        if args.command == "open":
            return _cmd_open(args)
        return _cmd_remove_selection(args)
    except (SelectionError, api.JobError, PhotoshopError, OSError) as exc:
        _error(str(exc))
        return 1


def _cmd_remove(args) -> int:
    shapes, image_size = _collect_shapes(args)
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
        config = api.build_remove_config(photos[0], output, shapes, options, image_size)
        path = api.export_script(args.export_jsx, config)
        _print(args, {"ok": True, "script": str(path), "output": str(output)},
               f"스크립트를 저장했습니다: {path}\nPhotoshop의 [파일 > 스크립트 > 찾아보기...]에서 실행하세요.")
        return 0

    failures = 0
    for index, photo in enumerate(photos):
        try:
            output = _output_for(photo, args, options)
            result = api.remove_area(photo, shapes, output, options, image_size=image_size,
                                     overwrite=args.overwrite, photoshop=args.photoshop, timeout=args.timeout)
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


def _cmd_open(args) -> int:
    result = api.open_photo(args.photo, photoshop=args.photoshop, timeout=args.timeout)
    _print(args, result, f"Photoshop에서 열었습니다: {args.photo}")
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


def _collect_shapes(args):
    shapes: List[Shape] = []
    image_size = None
    if args.selection:
        shapes, image_size = load_selection(args.selection)
    shapes += [rect(*parse_box(text)) for text in args.rect]
    shapes += [ellipse(*parse_box(text)) for text in args.ellipse]
    shapes += [polygon(parse_points(text)) for text in args.polygon]
    if not shapes and not args.subject:
        raise api.JobError("지울 영역을 --rect, --ellipse, --polygon, --selection 또는 --subject로 지정하세요.")
    return shapes, image_size


def _removal_options(args) -> api.RemoveOptions:
    options = api.RemoveOptions(
        method=args.method,
        expand=args.expand,
        feather=args.feather,
        subject=getattr(args, "subject", False),
        jpeg_quality=args.jpeg_quality,
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


def _warnings_text(result: Optional[dict]) -> str:
    warnings = (result or {}).get("warnings") or []
    return "".join(f"\n  참고: {w}" for w in warnings)


def _report_failure(args, photo: Path, message: str, result: Optional[dict]) -> None:
    if args.json:
        print(json.dumps({"ok": False, "input": str(photo), "error": message, "result": result}, ensure_ascii=False))
        return
    note = "\n  작업하던 사진은 Photoshop에 열어 두었습니다." if result and result.get("leftOpen") else ""
    _error(f"{photo}: {message}{note}{_warnings_text(result)}")


def _print(args, data: dict, text: str) -> None:
    print(json.dumps(data, ensure_ascii=False) if args.json else text)


def _error(message: str) -> None:
    print(f"오류: {message}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
