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
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

Point = Tuple[float, float]
Box = Tuple[float, float, float, float]

KINDS = ("rect", "ellipse", "polygon", "brush")
MODES = ("add", "subtract")
FITS = ("exact", "anchor", "stretch")
ORIENTATIONS = ("landscape", "portrait")
ORIENTATION_LABELS = {"landscape": "가로 사진", "portrait": "세로 사진"}
SELECTION_FILE_VERSION = 1
AREA_SET_FILE_VERSION = 2

# Anchor points for fit "anchor": (0, 0) is the top-left corner, (1, 1) the bottom-right.
ANCHOR_LABELS = {
    (0.0, 0.0): "왼쪽 위", (0.5, 0.0): "위 가운데", (1.0, 0.0): "오른쪽 위",
    (0.0, 0.5): "왼쪽 가운데", (0.5, 0.5): "가운데", (1.0, 0.5): "오른쪽 가운데",
    (0.0, 1.0): "왼쪽 아래", (0.5, 1.0): "아래 가운데", (1.0, 1.0): "오른쪽 아래",
}


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

    def transformed(self, sx: float, sy: float, tx: float = 0.0, ty: float = 0.0) -> "Shape":
        """The shape moved by ``x -> sx * x + tx``, ``y -> sy * y + ty``."""
        points = [(x * sx + tx, y * sy + ty) for x, y in self.points]
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


# -------------------------------------------------------------------- areas


@dataclass
class Area:
    """A reusable selection: shapes plus how to place them on photos of other sizes.

    ``image_size`` is the size of the photo the shapes were drawn on.

    fit "exact":   the same size, or the same aspect ratio scaled uniformly; other
                   photos are refused.
    fit "anchor":  keep the shapes' distance to ``anchor`` ((0, 0) top-left,
                   (1, 1) bottom-right, (0.5, 0.5) centre), scaled by the ratio of
                   the longer sides. A watermark in the bottom-right corner stays
                   there on a portrait photo too.
    fit "stretch": scale each axis to the photo's size.

    Whatever the fit, an area that touches an edge of its photo keeps touching
    that edge on every photo (see :meth:`glued_sides`): a credit box that
    starts at the right edge is never cut short of it.

    ``text_box`` is where the text to remove was on that photo, and
    ``band_box`` where a see-through box (e.g. Getty Images' credit box) was,
    if known: the area can then follow them on other photos (ps_remover.textfind).
    """

    shapes: List[Shape]
    image_size: Optional[Tuple[int, int]] = None
    fit: str = "exact"
    anchor: Optional[Tuple[float, float]] = None
    text_box: Optional[Box] = None
    band_box: Optional[Box] = None

    def __post_init__(self) -> None:
        self.shapes = list(self.shapes)
        if self.fit not in FITS:
            raise SelectionError(f"맞추는 방법은 {', '.join(FITS)} 중 하나여야 합니다: {self.fit!r}")
        if self.image_size is not None:
            width, height = (int(_number(v)) for v in self.image_size)
            if width <= 0 or height <= 0:
                raise SelectionError("사진 크기는 0보다 커야 합니다.")
            self.image_size = (width, height)
        elif self.fit != "exact":
            raise SelectionError("다른 크기의 사진에 맞추려면 영역을 그린 사진의 크기가 필요합니다.")
        if self.fit == "anchor" and self.anchor is None:
            self.anchor = auto_anchor(self.shapes, self.image_size)
        if self.anchor is not None:
            ax, ay = (_number(v) for v in self.anchor)
            if not (0 <= ax <= 1 and 0 <= ay <= 1):
                raise SelectionError("anchor 값은 0~1 사이여야 합니다.")
            self.anchor = (ax, ay)
        self.text_box = _box_field(self.text_box, "text_box")
        self.band_box = _box_field(self.band_box, "band_box")

    def glued_sides(self) -> Tuple[bool, bool, bool, bool]:
        """``(left, top, right, bottom)``: the edges of its photo the area touches.

        An area within EDGE_TOUCH of an edge was drawn to it, e.g. around a
        credit box that starts at the photo's right edge.
        """
        if not self.image_size or not self.shapes:
            return (False, False, False, False)
        width, height = self.image_size
        x0, y0, x1, y1 = bounds(self.shapes)
        near_x, near_y = max(4.0, EDGE_TOUCH * width), max(4.0, EDGE_TOUCH * height)
        return (x0 <= near_x, y0 <= near_y, x1 >= width - near_x, y1 >= height - near_y)

    def placing_anchor(self) -> Optional[Tuple[float, float]]:
        """The anchor used for fit "anchor": the edges the area touches win over the saved anchor.

        Along the other direction, such an area keeps its place in proportion to
        the photo: Getty Images' box is centred at two thirds of the height on
        every photo, on the right edge.
        """
        if self.fit != "anchor":
            return self.anchor
        ax, ay = self.anchor if self.anchor is not None else (0.5, 0.5)
        left, top, right, bottom = sides = self.glued_sides()
        if not any(sides):
            return (ax, ay)
        width, height = self.image_size
        x0, y0, x1, y1 = bounds(self.shapes)
        ax = 0.0 if left and not right else 1.0 if right and not left else min(1.0, max(0.0, (x0 + x1) / 2 / width))
        ay = 0.0 if top and not bottom else 1.0 if bottom and not top else min(1.0, max(0.0, (y0 + y1) / 2 / height))
        return (ax, ay)

    def placement(self, size: Tuple[int, int]) -> Tuple[float, float, float, float]:
        """``(sx, sy, tx, ty)`` placing the area on a photo of ``size``."""
        if not self.image_size:
            return (1.0, 1.0, 0.0, 0.0)
        return fit_transform(self.image_size, size, self.fit, self.placing_anchor())

    def map_box(self, box: Box, size: Tuple[int, int]) -> Box:
        """``box`` on the area's photo placed on a photo of ``size``, reaching the edges the area touches."""
        sx, sy, tx, ty = self.placement(size)
        placed = (box[0] * sx + tx, box[1] * sy + ty, box[2] * sx + tx, box[3] * sy + ty)
        return _to_edges(self.glued_sides(), placed, size)

    def on_photo(self, size: Tuple[int, int]) -> List[Shape]:
        """The shapes placed on a photo of ``size`` (width, height).

        Keep in sync with psrPlaceOps() in jsx/ps_remover.jsx.
        """
        if not self.image_size:
            return list(self.shapes)
        sx, sy, tx, ty = self.placement(size)
        placed = [shape.transformed(sx, sy, tx, ty) for shape in self.shapes]
        return placed + glue_fill(self.glued_sides(), bounds(placed), size)

    def describe(self) -> str:
        """How the area adapts to other photos, in words."""
        if self.fit == "anchor":
            return f"{ANCHOR_LABELS.get(self.anchor, '기준점')} 기준으로 맞춤"
        if self.fit == "stretch":
            return "사진 크기에 비례해서 늘림"
        return "같은 비율의 사진에만"

    def to_dict(self) -> dict:
        data: dict = {
            "version": SELECTION_FILE_VERSION,
            "image_size": list(self.image_size) if self.image_size else None,
            "fit": self.fit,
        }
        if self.anchor is not None:
            data["anchor"] = [self.anchor[0], self.anchor[1]]
        if self.text_box is not None:
            data["text_box"] = [_r(v) for v in self.text_box]
        if self.band_box is not None:
            data["band_box"] = [_r(v) for v in self.band_box]
        data["shapes"] = [shape.to_dict() for shape in self.shapes]
        return data

    @classmethod
    def from_dict(cls, data) -> "Area":
        if isinstance(data, list):  # a bare list of shapes
            data = {"shapes": data}
        if not isinstance(data, dict) or not isinstance(data.get("shapes"), list):
            raise SelectionError("선택 영역 파일 형식이 올바르지 않습니다.")
        size = data.get("image_size")
        if size is not None and (not isinstance(size, (list, tuple)) or len(size) != 2):
            raise SelectionError("image_size는 [너비, 높이] 형식이어야 합니다.")
        anchor = data.get("anchor")
        if anchor is not None and (not isinstance(anchor, (list, tuple)) or len(anchor) != 2):
            raise SelectionError("anchor는 [x, y] 형식이어야 합니다.")
        boxes = {}
        for key in ("text_box", "band_box"):
            value = data.get(key)
            if value is not None and (not isinstance(value, (list, tuple)) or len(value) != 4):
                raise SelectionError(f"{key}는 [왼쪽, 위, 오른쪽, 아래] 형식이어야 합니다.")
            boxes[key] = tuple(value) if value is not None else None
        return cls(
            [Shape.from_dict(item) for item in data["shapes"]],
            tuple(size) if size is not None else None,
            data.get("fit", "exact"),
            tuple(anchor) if anchor is not None else None,
            **boxes,
        )


def orientation_of(size: Tuple[float, float]) -> str:
    """"portrait" for a photo taller than it is wide, otherwise "landscape"."""
    return "portrait" if size[1] > size[0] else "landscape"


@dataclass
class AreaSet:
    """A common area with its own :class:`Area` for landscape and/or portrait photos.

    The same text often sits somewhere else on portrait photos than on landscape
    ones, so each orientation can be drawn separately. A photo whose orientation
    has no area of its own uses the other one, placed by that area's fit.
    """

    areas: Dict[str, Area]

    def __post_init__(self) -> None:
        if not self.areas or set(self.areas) - set(ORIENTATIONS):
            raise SelectionError(f"공통 영역은 {', '.join(ORIENTATIONS)} 영역으로 이루어져야 합니다.")
        self.areas = {o: self.areas[o] for o in ORIENTATIONS if o in self.areas}

    @classmethod
    def single(cls, area: Area) -> "AreaSet":
        return cls({orientation_of(area.image_size) if area.image_size else "landscape": area})

    def for_orientation(self, orientation: str) -> Area:
        return self.areas.get(orientation) or next(iter(self.areas.values()))

    def for_size(self, size: Tuple[int, int]) -> Area:
        """The area to use on a photo of ``size``."""
        return self.for_orientation(orientation_of(size))

    def with_area(self, area: Area, orientation: Optional[str] = None) -> "AreaSet":
        """A copy with ``area`` for its photo's orientation (or ``orientation``)."""
        if orientation is None:
            orientation = orientation_of(area.image_size) if area.image_size else "landscape"
        return AreaSet(dict(self.areas, **{orientation: area}))

    def describe(self) -> str:
        return " / ".join(f"{ORIENTATION_LABELS[o]}: {a.describe()}" for o, a in self.areas.items())

    def to_dict(self) -> dict:
        data: dict = {"version": AREA_SET_FILE_VERSION}
        for orientation, area in self.areas.items():
            data[orientation] = {k: v for k, v in area.to_dict().items() if k != "version"}
        return data

    @classmethod
    def from_dict(cls, data) -> "AreaSet":
        if isinstance(data, dict) and any(o in data for o in ORIENTATIONS):
            return cls({o: Area.from_dict(data[o]) for o in ORIENTATIONS if o in data})
        return cls.single(Area.from_dict(data))  # a single-area file


def area_has_shapes(area: Union[Area, AreaSet]) -> bool:
    """True if the area (any of its orientations) adds something to the selection."""
    areas = area.areas.values() if isinstance(area, AreaSet) else [area]
    return any(has_area(a.shapes) for a in areas)


def save_area(path, area: Union[Area, AreaSet]) -> None:
    Path(path).write_text(json.dumps(area.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_area_set(path) -> AreaSet:
    """Read a selection or common-area file (one area, or one per orientation)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise SelectionError(f"선택 영역 파일을 읽을 수 없습니다: {path} ({exc})") from exc
    try:
        return AreaSet.from_dict(data)
    except SelectionError as exc:
        raise SelectionError(f"{path}: {exc}") from None


def fit_transform(from_size: Tuple[float, float], to_size: Tuple[float, float], fit: str = "exact",
                  anchor: Optional[Tuple[float, float]] = None) -> Tuple[float, float, float, float]:
    """``(sx, sy, tx, ty)`` placing shapes drawn at ``from_size`` onto ``to_size``.

    Keep in sync with psrShapeTransform() in jsx/ps_remover.jsx, which does
    the same with the size Photoshop actually opened.
    """
    w, h = from_size
    width, height = to_size
    if abs(width - w) < 0.5 and abs(height - h) < 0.5:
        return (1.0, 1.0, 0.0, 0.0)
    if fit == "stretch":
        return (width / w, height / h, 0.0, 0.0)
    if fit == "anchor":
        ax, ay = anchor if anchor is not None else (0.5, 0.5)
        # Credits and watermarks go with the photo's longer side: Getty Images' box is the same
        # 800 pixels wide on a 2000x1333 and on a 1317x2000 photo.
        s = max(width, height) / max(w, h)
        return (s, s, ax * (width - s * w), ay * (height - s * h))
    if abs((width / height) / (w / h) - 1) > 0.01:
        raise SelectionError(f"사진 크기({width:g}x{height:g})의 가로세로 비율이 영역을 그린 사진"
                             f"({w:g}x{h:g})과 다릅니다. 공통 영역의 맞추는 방법을 바꿔 보세요.")
    return (width / w, height / h, 0.0, 0.0)


def auto_anchor(shapes: Sequence[Shape], size: Tuple[int, int]) -> Tuple[float, float]:
    """The corner, edge middle or centre the shapes are nearest to, e.g. (1.0, 1.0) for bottom-right."""
    added = [shape for shape in shapes if shape.mode == "add"] or list(shapes)
    if not added:
        return (0.5, 0.5)
    boxes = [shape.box for shape in added]
    cx = (min(b[0] for b in boxes) + max(b[2] for b in boxes)) / 2 / size[0]
    cy = (min(b[1] for b in boxes) + max(b[3] for b in boxes)) / 2 / size[1]
    return (_third(cx), _third(cy))


def _third(value: float) -> float:
    return 0.0 if value < 1 / 3 else 0.5 if value <= 2 / 3 else 1.0


EDGE_TOUCH = 0.02  # an area this close to an edge of its photo (part of the width or height) touches it


def bounds(shapes: Sequence[Shape]) -> Box:
    """The box around the shapes that add to the selection."""
    boxes = [shape.box for shape in shapes if shape.mode == "add"] or [shape.box for shape in shapes]
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def glue_fill(sides: Tuple[bool, bool, bool, bool], placed: Box, size: Tuple[int, int]) -> List[Shape]:
    """Rectangles from the placed area's box out to the photo edges in ``sides`` it does not reach."""
    left, top, right, bottom = sides
    x0, y0, x1, y1 = placed
    width, height = size
    nx0, ny0, nx1, ny1 = _to_edges(sides, placed, size)
    fill = []
    if right and x1 < width:
        fill.append(rect(x1 - 1, ny0, width, ny1))
    if left and x0 > 0:
        fill.append(rect(0, ny0, x0 + 1, ny1))
    if bottom and y1 < height:
        fill.append(rect(nx0, y1 - 1, nx1, height))
    if top and y0 > 0:
        fill.append(rect(nx0, 0, nx1, y0 + 1))
    return fill


def _to_edges(sides: Tuple[bool, bool, bool, bool], box: Box, size: Tuple[int, int]) -> Box:
    left, top, right, bottom = sides
    return (0.0 if left else box[0], 0.0 if top else box[1],
            float(size[0]) if right else box[2], float(size[1]) if bottom else box[3])


def _box_field(value, name: str) -> Optional[Box]:
    if value is None:
        return None
    left, top, right, bottom = (_number(v) for v in value)
    if right <= left or bottom <= top:
        raise SelectionError(f"{name}는 [왼쪽, 위, 오른쪽, 아래] 형식이어야 합니다.")
    return (left, top, right, bottom)


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

