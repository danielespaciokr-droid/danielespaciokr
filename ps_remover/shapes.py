"""Selection shapes in image pixel coordinates.

A selection is an ordered list of shapes. Each shape either adds to or
subtracts from the selection, just like holding Shift or Alt with a Photoshop
selection tool. Shapes are turned into "ops" (rectangles, ellipses and
polygons) that the Photoshop script can select directly.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

Point = Tuple[float, float]
Box = Tuple[float, float, float, float]

KINDS = ("rect", "ellipse", "polygon", "brush")
MODES = ("add", "subtract")
SELECTION_FILE_VERSION = 1


class SelectionError(ValueError):
    """Invalid selection data."""


@dataclass
class Shape:
    """One selection shape.

    rect / ellipse: ``points`` holds two opposite corners.
    polygon:        ``points`` holds the outline (closed automatically).
    brush:          ``points`` holds the stroke path; ``radius`` is half the brush size.
    """

    kind: str
    points: List[Point]
    mode: str = "add"
    radius: float = 0.0

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise SelectionError(f"알 수 없는 도형 종류입니다: {self.kind!r}")
        if self.mode not in MODES:
            raise SelectionError(f"알 수 없는 선택 모드입니다: {self.mode!r} (add 또는 subtract)")
        self.points = [_point(p) for p in self.points]
        self.radius = _number(self.radius)
        if self.kind in ("rect", "ellipse"):
            if len(self.points) != 2:
                raise SelectionError(f"{self.kind}에는 모서리 두 점이 필요합니다.")
            (x0, y0), (x1, y1) = self.points
            self.points = [(min(x0, x1), min(y0, y1)), (max(x0, x1), max(y0, y1))]
            left, top, right, bottom = self.box
            if right - left <= 0 or bottom - top <= 0:
                raise SelectionError(f"{self.kind}의 너비와 높이는 0보다 커야 합니다.")
        elif self.kind == "polygon":
            if len(self.points) < 3:
                raise SelectionError("다각형(올가미)에는 점이 3개 이상 필요합니다.")
        else:
            if not self.points:
                raise SelectionError("브러시 획에는 점이 1개 이상 필요합니다.")
            if self.radius <= 0:
                raise SelectionError("브러시 크기는 0보다 커야 합니다.")

    @property
    def box(self) -> Box:
        """Bounding box as (left, top, right, bottom), including the brush radius."""
        xs = [p[0] for p in self.points]
        ys = [p[1] for p in self.points]
        r = self.radius if self.kind == "brush" else 0.0
        return (min(xs) - r, min(ys) - r, max(xs) + r, max(ys) + r)

    def scaled(self, sx: float, sy: float) -> "Shape":
        """The same shape on a photo resized by ``sx`` and ``sy``."""
        points = [(x * sx, y * sy) for x, y in self.points]
        return Shape(self.kind, points, self.mode, self.radius * (sx + sy) / 2)

    def to_ops(self) -> List[dict]:
        """Primitive selection operations understood by the Photoshop script."""
        if self.kind in ("rect", "ellipse"):
            return [{"op": self.kind, "mode": self.mode, "box": [_r(v) for v in self.box]}]
        if self.kind == "polygon":
            return [{"op": "polygon", "mode": self.mode, "points": [[_r(x), _r(y)] for x, y in self.points]}]
        return [dict(op, mode=self.mode) for op in _brush_ops(self.points, self.radius)]

    def to_dict(self) -> dict:
        data: dict = {"type": self.kind, "mode": self.mode}
        if self.kind in ("rect", "ellipse"):
            data["box"] = [_r(v) for v in self.box]
        else:
            data["points"] = [[_r(x), _r(y)] for x, y in self.points]
        if self.kind == "brush":
            data["radius"] = _r(self.radius)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Shape":
        if not isinstance(data, dict):
            raise SelectionError("도형은 JSON 객체여야 합니다.")
        kind = data.get("type")
        mode = data.get("mode", "add")
        if kind in ("rect", "ellipse"):
            if "box" in data:
                box = data["box"]
                if not isinstance(box, (list, tuple)) or len(box) != 4:
                    raise SelectionError(f"{kind}의 box는 [left, top, right, bottom] 형식이어야 합니다.")
                left, top, right, bottom = box
                return cls(kind, [(left, top), (right, bottom)], mode)
            if all(k in data for k in ("x", "y", "width", "height")):
                x, y = _number(data["x"]), _number(data["y"])
                return cls(kind, [(x, y), (x + _number(data["width"]), y + _number(data["height"]))], mode)
            raise SelectionError(f"{kind}에는 box 또는 x/y/width/height가 필요합니다.")
        if kind in ("polygon", "brush"):
            points = data.get("points")
            if not isinstance(points, (list, tuple)):
                raise SelectionError(f"{kind}에는 points 목록이 필요합니다.")
            return cls(kind, list(points), mode, data.get("radius", 0.0))
        raise SelectionError(f"알 수 없는 도형 종류입니다: {kind!r}")


def rect(left: float, top: float, right: float, bottom: float, mode: str = "add") -> Shape:
    return Shape("rect", [(left, top), (right, bottom)], mode)


def ellipse(left: float, top: float, right: float, bottom: float, mode: str = "add") -> Shape:
    return Shape("ellipse", [(left, top), (right, bottom)], mode)


def polygon(points: Iterable[Point], mode: str = "add") -> Shape:
    return Shape("polygon", list(points), mode)


def brush(points: Iterable[Point], radius: float, mode: str = "add") -> Shape:
    return Shape("brush", list(points), mode, radius)


def selection_ops(shapes: Sequence[Shape]) -> List[dict]:
    ops: List[dict] = []
    for shape in shapes:
        ops.extend(shape.to_ops())
    return ops


def has_area(shapes: Sequence[Shape]) -> bool:
    """True if at least one shape adds to the selection."""
    return any(shape.mode == "add" for shape in shapes)


# ------------------------------------------------------------ command line


def parse_box(text: str) -> Box:
    """Parse ``"x,y,width,height"`` into (left, top, right, bottom)."""
    values = _numbers(text, 4, "x,y,너비,높이")
    x, y, width, height = values
    if width <= 0 or height <= 0:
        raise SelectionError(f"너비와 높이는 0보다 커야 합니다: {text!r}")
    return (x, y, x + width, y + height)


def parse_points(text: str) -> List[Point]:
    """Parse ``"x1,y1 x2,y2 x3,y3 ..."`` (``;`` also separates points)."""
    points: List[Point] = []
    for chunk in text.replace(";", " ").split():
        x, y = _numbers(chunk, 2, "x,y")
        points.append((x, y))
    if len(points) < 3:
        raise SelectionError(f"다각형에는 점이 3개 이상 필요합니다: {text!r}")
    return points


# ---------------------------------------------------------- selection files


def save_selection(path, shapes: Sequence[Shape], image_size: Optional[Tuple[int, int]] = None) -> None:
    data = {
        "version": SELECTION_FILE_VERSION,
        "image_size": list(image_size) if image_size else None,
        "shapes": [shape.to_dict() for shape in shapes],
    }
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_selection(path) -> Tuple[List[Shape], Optional[Tuple[int, int]]]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise SelectionError(f"선택 영역 파일을 읽을 수 없습니다: {path} ({exc})") from exc
    if isinstance(data, list):  # a bare list of shapes
        data = {"shapes": data}
    if not isinstance(data, dict) or not isinstance(data.get("shapes"), list):
        raise SelectionError(f"선택 영역 파일 형식이 올바르지 않습니다: {path}")
    shapes = [Shape.from_dict(item) for item in data["shapes"]]
    size = data.get("image_size")
    if size is not None:
        if not isinstance(size, (list, tuple)) or len(size) != 2:
            raise SelectionError("image_size는 [너비, 높이] 형식이어야 합니다.")
        size = (int(_number(size[0])), int(_number(size[1])))
    return shapes, size


# ----------------------------------------------------------------- geometry


def simplify_path(points: Sequence[Point], tolerance: float) -> List[Point]:
    """Drop points that deviate less than ``tolerance`` from the path (Ramer-Douglas-Peucker)."""
    pts = _dedupe(points)
    if len(pts) < 3 or tolerance <= 0:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        start, end = stack.pop()
        best, best_dist = -1, tolerance
        for i in range(start + 1, end):
            dist = _segment_distance(pts[i], pts[start], pts[end])
            if dist > best_dist:
                best, best_dist = i, dist
        if best >= 0:
            keep[best] = True
            stack.append((start, best))
            stack.append((best, end))
    return [p for p, k in zip(pts, keep) if k]


def capsule(a: Point, b: Point, radius: float, segments: Optional[int] = None) -> List[Point]:
    """Convex outline of a round-capped line from ``a`` to ``b``."""
    if segments is None:
        segments = 6 if radius < 8 else 10 if radius < 40 else 16
    angle = math.atan2(b[1] - a[1], b[0] - a[0])
    outline: List[Point] = []
    for center, start in ((b, angle - math.pi / 2), (a, angle + math.pi / 2)):
        for i in range(segments + 1):
            theta = start + math.pi * i / segments
            outline.append((center[0] + radius * math.cos(theta), center[1] + radius * math.sin(theta)))
    return outline


def _brush_ops(points: Sequence[Point], radius: float) -> List[dict]:
    pts = _dedupe(points)
    if len(pts) == 1:
        x, y = pts[0]
        return [{"op": "ellipse", "box": [_r(x - radius), _r(y - radius), _r(x + radius), _r(y + radius)]}]
    # Photoshop has no "stroke to selection", so a stroke becomes a union of
    # round-capped segments.
    return [
        {"op": "polygon", "points": [[_r(x), _r(y)] for x, y in capsule(a, b, radius)]}
        for a, b in zip(pts, pts[1:])
    ]


# --------------------------------------------------------------- rendering


def render_mask(shapes: Sequence[Shape], size: Tuple[int, int], scale: float = 1.0):
    """Rasterize the selection into a Pillow ``L`` image (255 = selected).

    ``scale`` maps image pixels to mask pixels, e.g. for a preview.
    """
    from PIL import Image, ImageDraw

    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    for shape in shapes:
        fill = 255 if shape.mode == "add" else 0
        for op in shape.to_ops():
            if op["op"] == "polygon":
                draw.polygon([(x * scale, y * scale) for x, y in op["points"]], fill=fill)
                continue
            left, top, right, bottom = (v * scale for v in op["box"])
            box = [left, top, max(left, right - 1), max(top, bottom - 1)]
            if op["op"] == "rect":
                draw.rectangle(box, fill=fill)
            else:
                draw.ellipse(box, fill=fill)
    return mask


# ------------------------------------------------------------------ helpers


def _number(value) -> float:
    if isinstance(value, bool):
        raise SelectionError(f"숫자가 아닙니다: {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise SelectionError(f"숫자가 아닙니다: {value!r}") from None
    if not math.isfinite(number):
        raise SelectionError(f"유한한 숫자가 아닙니다: {value!r}")
    return number


def _point(value) -> Point:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise SelectionError(f"점은 [x, y] 형식이어야 합니다: {value!r}")
    return (_number(value[0]), _number(value[1]))


def _numbers(text: str, count: int, form: str) -> List[float]:
    parts = [p.strip() for p in str(text).split(",")]
    if len(parts) != count or not all(parts):
        raise SelectionError(f"'{form}' 형식이어야 합니다: {text!r}")
    return [_number(p) for p in parts]


def _dedupe(points: Sequence[Point]) -> List[Point]:
    out: List[Point] = []
    for p in points:
        p = (float(p[0]), float(p[1]))
        if not out or math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) > 1e-6:
            out.append(p)
    return out


def _segment_distance(p: Point, a: Point, b: Point) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        return math.hypot(p[0] - a[0], p[1] - a[1])
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length_sq))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


def _r(value: float) -> float:
    """Round coordinates to keep the generated script small."""
    rounded = round(float(value), 2)
    return 0.0 if rounded == 0 else rounded  # avoid "-0.0"

