"""Find the credit to remove in each photo: a see-through box on the photo's edge, or text.

Photos come in many sizes, so an area saved once is not always where the credit
is. Here it is looked for in each photo:

* A see-through box, like Getty Images' grey credit box on the right edge: an
  area drawn against an edge of its photo is looked for as such a box. Along
  its top edge every pixel is the pixel above it mixed with the same grey,
  all the way to the photo's edge, which a photo itself hardly ever does.
* Text (a credit line, a watermark): letters are small shapes with sharp
  edges, standing side by side on one line and all about as tall. Grass,
  crowds and blurred stadiums rarely look like that.

The removal area is then put around what was found. Only the photo's pixels
are used (Pillow and numpy); Photoshop is not needed.
"""

from __future__ import annotations

import importlib.util
import math
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple, Union

from .shapes import Area, AreaSet, Box, SelectionError, bounds, rect

WORK_SHORT_SIDE = 1600      # photos are searched at most this big (shorter side, pixels)
CORNER = (0.6, 0.4)         # without other hints: the right 60% and the bottom 40% of the photo
MIN_LETTER = 0.005          # letter heights as a part of the photo's shorter side
MAX_LETTER = 0.12
CONTRAST_LEVELS = (100, 70, 45, 28)  # how much strokes must stand out, tried strongest first
STROKE_SIZES = (9, 21)      # strokes thinner than this (work pixels) are looked at, when the size is unknown
MIN_CONFIDENCE = 0.3        # 0..1: how much a line has to look like text to be considered
ACCEPT_CONFIDENCE = 0.5     # ... and to move the area there on its own (lines of grass and crowd stay below)
MAX_SHAPES = 6000           # more shapes than this at one level: too busy to be text
WORD_GAP = 1.6              # letters and words further apart than this (letter heights) are separate pieces
JOIN_GAP = 4.0              # ... which still join a line of text they stand exactly in line with
DEFAULT_MARGIN = 0.5        # around the text found, in letter heights, when the area knows no text
BOX_MARGIN = 0.03           # room left around a see-through box found, as a part of its height (3 px at least)
EDGE_CLIP = 20.0            # grey levels: the most one pixel can say about a box edge
EDGE_SEEN = 2.0             # what the pixels along a row must say (median, grey levels) for a box edge to show there
EDGE_AGAINST = -4.0         # ... and below this, they say there is no box edge there
EDGE_END = 0.75             # ... on both sides of where a box's edges end, on average
EDGE_MARGIN = 2.0           # grey levels: columns saying less than this either way about a box's side tell nothing

Picture = Union[str, "PhotoPixels"]


class TextFindUnavailable(RuntimeError):
    """The photo cannot be searched here (numpy missing, or a format Pillow cannot read)."""


@dataclass
class PhotoPixels:
    """A photo as Photoshop opens it (EXIF rotation applied), in gray, maybe scaled down."""

    size: Tuple[int, int]  # the photo's own size (width, height)
    scale: float           # pixels of ``gray`` per photo pixel
    gray: "object"         # numpy array, uint8, rows x columns


@dataclass
class FoundText:
    box: Box               # around the text, in photo pixels (left, top, right, bottom)
    letter_height: float   # in photo pixels
    confidence: float      # 0..1

    @property
    def width(self) -> float:
        return self.box[2] - self.box[0]

    @property
    def height(self) -> float:
        return self.box[3] - self.box[1]


@dataclass
class FoundBox:
    box: Box               # the see-through box, in photo pixels, reaching the photo edges it sits on
    edges: int             # 2: its top and bottom edge were seen; 1: one of them, the other from the known height
    alpha: float           # how much of the box's grey is mixed into the photo (0..1)

    @property
    def width(self) -> float:
        return self.box[2] - self.box[0]

    @property
    def height(self) -> float:
        return self.box[3] - self.box[1]


@dataclass
class Placement:
    """Where to remove on one photo."""

    area: Optional[Area]         # to select on this photo; None when there is nothing to select
    found: Union[FoundText, FoundBox, None]  # what was found; None: not found, ``area`` is the saved one
    note: str                    # short, for the log
    kind: str = "text"           # what was looked for: "text", or "box" for a see-through box


def available() -> bool:
    return importlib.util.find_spec("numpy") is not None


# ----------------------------------------------------------------- placing


def place(photo: Picture, area: Union[Area, AreaSet, None], margin: float = DEFAULT_MARGIN) -> Placement:
    """The area to remove on ``photo``, following the credit found near where ``area`` expects it.

    An area drawn against an edge of its photo is looked for as a see-through
    box on that edge (see :func:`_place_box`); other areas follow the text.
    An area that knows where the text was on its own photo (``text_box``) is
    moved and scaled with the text, so the margins drawn around the text stay
    the same. Otherwise the text found is removed with ``margin`` letter
    heights around it. Without text found, ``area`` is used as saved. Without
    an area the text is looked for near the bottom-right corner.
    """
    pixels = photo if isinstance(photo, PhotoPixels) else load_photo(photo)
    size = pixels.size
    chosen = area.for_size(size) if isinstance(area, AreaSet) else area
    if chosen is not None and chosen.shapes and any(chosen.glued_sides()):
        others = [a for a in area.areas.values() if a is not chosen] if isinstance(area, AreaSet) else []
        return _place_box(pixels, chosen, others)
    expected_area, expected_text = _expected_boxes(chosen, size) if chosen else (None, None)
    hint = expected_text or expected_area
    tall = None
    if expected_text:
        tall = expected_text[3] - expected_text[1]
    elif expected_area:
        tall = 0.6 * (expected_area[3] - expected_area[1])  # drawn with some room around the text
    window = _search_window(size, expected_area)
    found = find_text(pixels, window, hint, tall)
    if found is not None and found.confidence < ACCEPT_CONFIDENCE:
        found = None  # not sure enough: the saved position is the safer guess
    if found is not None and hint and not _plausible(found.box, hint, size):
        found = None  # text, but not where this area's text goes (e.g. a sponsor on a shirt)
    if found is None:
        return Placement(chosen, None, "글자 못 찾음: 저장된 위치" if chosen else "글자 못 찾음")
    if chosen and chosen.text_box and chosen.shapes:
        ref = chosen.text_box
        ratio = found.height / max(1.0, ref[3] - ref[1])
        scale = min(2.5, max(0.4, ratio))
        found = _complete(found, (ref[2] - ref[0]) * scale, expected_text)
        # The text's bottom-right corner keeps its place; the drawn margins scale with the letters.
        shapes = [shape.transformed(scale, scale, found.box[2] - ref[2] * scale, found.box[3] - ref[3] * scale)
                  for shape in chosen.shapes]
        pad = 0.25 * found.letter_height
    elif chosen and expected_area and _overlap(found.box, expected_area):
        # Where the text sits inside an area saved before text positions were kept is not known. The
        # area where it would have gone and the text found agree, so both go: that covers text a
        # little off the area, and text too faint to be found all of.
        shapes = chosen.on_photo(size)
        pad = margin * found.letter_height
    else:
        shapes = []
        pad = margin * found.letter_height
    left, top, right, bottom = found.box
    shapes.append(rect(left - pad, top - pad, right + pad, bottom + pad))  # all of the text, always
    return Placement(Area(shapes, size, "exact"), found, "글자 찾음")


def _place_box(pixels: PhotoPixels, area: Area, others: Sequence[Area] = ()) -> Placement:
    """An area drawn against a photo edge: the see-through box there, with a little room around it.

    Its text is not followed: the credit's name is longer on one photo than on
    another, but the box stays on the edge. What is found only ever adds to
    where the box is expected (the box learned on the area's photo, placed
    on this one; else the area as drawn): a box found a little off never
    leaves a strip of the credit behind.
    """
    size = pixels.size
    sides = area.glued_sides()
    height: Optional[float] = None
    if area.band_box:
        near = area.map_box(area.band_box, size)
        height = (area.band_box[3] - area.band_box[1]) * area.placement(size)[1]
    else:
        near = bounds(area.on_photo(size))
        for other in others:  # the same box, seen on a photo of the other orientation
            if other.band_box and other.image_size:
                height = (other.band_box[3] - other.band_box[1]) * max(size) / max(other.image_size)
                break
    found = find_box(pixels, near, sides, height, known=bool(area.band_box))
    if found is None:
        return Placement(area, None, "상자 못 찾음: 저장된 위치", "box")
    room = max(3.0, BOX_MARGIN * found.height)
    shapes = [_with_room(found.box, room)]
    if area.band_box:
        shapes.append(_with_room(near, room))
    else:
        shapes.extend(area.on_photo(size))  # drawn with room of its own
    return Placement(Area(shapes, size, "exact"), found, "상자 찾음", "box")


def _with_room(box: Box, room: float):
    return rect(box[0] - room, box[1] - room, box[2] + room, box[3] + room)


def _overlap(a: Box, b: Box) -> bool:
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


def _plausible(box: Box, expected: Box, size: Tuple[int, int]) -> bool:
    """Whether text found at ``box`` can be the text expected at ``expected``.

    The saved area may be somewhat off on a photo of another size, but a
    credit line does not move by more than a few of its own heights, nor by
    more than its own width sideways.
    """
    width, height = expected[2] - expected[0], expected[3] - expected[1]
    dx = abs((box[0] + box[2]) - (expected[0] + expected[2])) / 2.0
    dy = abs((box[1] + box[3]) - (expected[1] + expected[3])) / 2.0
    return dx <= max(1.2 * width, 0.25 * size[0]) and dy <= max(4.0 * height, 0.1 * size[1])


def _complete(found: FoundText, width: float, expected: Optional[Box]) -> FoundText:
    """``found`` as wide as the text should be, when only a part of it could be seen.

    Where the text runs over something as light (or dark) as its letters, that
    part cannot be seen. The same text as on the reference photo is as wide as
    there, so the box is widened on the side the text is expected to go on:
    to the left by default, as text in the bottom-right corner ends there.
    """
    if found.width >= 0.85 * width:
        return found
    left, top, right, bottom = found.box
    to_left = (right - width, top, right, bottom)
    to_right = (left, top, left + width, bottom)
    box = to_left
    if expected:
        center = (expected[0] + expected[2]) / 2.0
        if abs((to_right[0] + to_right[2]) / 2.0 - center) < abs((to_left[0] + to_left[2]) / 2.0 - center):
            box = to_right
    return FoundText(box, found.letter_height, found.confidence)


def learn_text_box(photo: Picture, area: Area) -> Optional[Box]:
    """Where the text is inside ``area`` on the photo it was drawn on, or None."""
    if not area.shapes:
        return None
    pixels = photo if isinstance(photo, PhotoPixels) else load_photo(photo)
    drawn = bounds(area.on_photo(pixels.size))
    width, height = drawn[2] - drawn[0], drawn[3] - drawn[1]
    window = (drawn[0] - 0.25 * width, drawn[1] - 0.25 * height, drawn[2] + 0.25 * width, drawn[3] + 0.25 * height)
    # The area was drawn around all of the text: every piece on its line inside the area belongs to it.
    found = find_text(pixels, window, drawn, within=drawn)
    if found is None:
        return None
    left, top, right, bottom = found.box
    # Only what lies inside the drawn area counts.
    box = (max(left, drawn[0]), max(top, drawn[1]), min(right, drawn[2]), min(bottom, drawn[3]))
    if box[2] - box[0] < 2 or box[3] - box[1] < 2:
        return None
    return tuple(round(v, 1) for v in box)


def _expected_boxes(area: Area, size: Tuple[int, int]) -> Tuple[Optional[Box], Optional[Box]]:
    """Where ``area`` and the text it knows about land on a photo of ``size``, if that can be said."""
    if not area.shapes:
        return None, None
    try:
        expected_area = bounds(area.on_photo(size))
        expected_text = area.map_box(area.text_box, size) if area.text_box and area.image_size else None
    except SelectionError:  # fit "exact" on a photo of another shape
        return None, None
    return expected_area, expected_text


def _search_window(size: Tuple[int, int], expected: Optional[Box]) -> Box:
    width, height = size
    left, top = width * (1 - CORNER[0]), height * (1 - CORNER[1])
    right, bottom = float(width), float(height)
    if expected:
        grow = max(expected[2] - expected[0], expected[3] - expected[1], 0.08 * min(size))
        left, top = min(left, expected[0] - grow), min(top, expected[1] - grow)
        right, bottom = max(right, expected[2] + grow), max(bottom, expected[3] + grow)
    return (max(0.0, left), max(0.0, top), min(float(width), right), min(float(height), bottom))


# ----------------------------------------------------------------- reading


def load_photo(path) -> PhotoPixels:
    """Reads the photo as Photoshop would open it, in gray and at most WORK_SHORT_SIDE big."""
    np = _numpy()
    from PIL import Image, ImageOps

    try:
        image = Image.open(path)
    except (OSError, ValueError) as exc:
        raise TextFindUnavailable(f"사진을 읽을 수 없습니다: {exc}") from None
    with image:
        try:
            orientation = image.getexif().get(0x0112, 1)
        except Exception:  # noqa: BLE001 - broken EXIF: Photoshop ignores it too
            orientation = 1
        width, height = image.size
        size = (height, width) if orientation in (5, 6, 7, 8) else (width, height)
        scale = min(1.0, WORK_SHORT_SIDE / min(size))
        try:
            if scale < 1:
                image.draft("L", (int(image.width * scale) + 1, int(image.height * scale) + 1))  # JPEG: decode smaller
            image = ImageOps.exif_transpose(image)
            gray = _gray(image, np)
        except (OSError, ValueError, SyntaxError) as exc:
            raise TextFindUnavailable(f"사진을 읽을 수 없습니다: {exc}") from None
    target = (max(1, round(size[0] * scale)), max(1, round(size[1] * scale)))
    if gray.size != target:
        gray = gray.resize(target, Image.BILINEAR)
    return PhotoPixels(size, target[0] / size[0], np.asarray(gray, dtype=np.uint8))


def _gray(image, np):
    from PIL import Image

    if image.mode in ("I;16", "I;16B", "I;16L", "I", "F"):  # 16-bit and float photos
        values = np.asarray(image, dtype=np.float64)
        top = 65535.0 if image.mode != "F" and values.max() > 255 else max(1.0, float(values.max()))
        return Image.fromarray(np.clip(values * (255.0 / top), 0, 255).astype(np.uint8), "L")
    return image.convert("L")


def _numpy():
    try:
        import numpy
    except ImportError:
        raise TextFindUnavailable("글자를 찾으려면 numpy가 필요합니다. run_windows.bat을 다시 실행하면 설치됩니다.") from None
    return numpy


# ----------------------------------------------------------------- finding


@dataclass
class _Line:
    box: Tuple[int, int, int, int]  # in the searched window's pixels, right/bottom exclusive
    letter_height: float
    confidence: float
    score: float
    level: int


def find_text(pixels: PhotoPixels, window: Box, expected: Optional[Box] = None,
              tall: Optional[float] = None, within: Optional[Box] = None) -> Optional[FoundText]:
    """The most text-like line (or lines stacked into a block) inside ``window``.

    ``expected`` (photo pixels) is where the text probably is and ``tall`` how
    tall the text probably is (top of the tallest letter to the bottom of the
    lowest); text near there and of that height wins over text elsewhere, e.g.
    a sponsor's name on a shirt. Pieces on the found line that lie ``within``
    a box join it however far apart they are.
    """
    np = _numpy()
    s = pixels.scale
    rows, cols = pixels.gray.shape
    left, top = max(0, int(window[0] * s)), max(0, int(window[1] * s))
    right, bottom = min(cols, int(math.ceil(window[2] * s))), min(rows, int(math.ceil(window[3] * s)))
    if right - left < 8 or bottom - top < 8:
        return None
    crop = pixels.gray[top:bottom, left:right]
    short = min(pixels.size) * s
    low, high = max(4.0, MIN_LETTER * short), MAX_LETTER * short
    near = None
    if expected:
        near = (expected[0] * s - left, expected[1] * s - top, expected[2] * s - left, expected[3] * s - top)
    tall_work = tall * s if tall else None
    if tall_work:
        sizes = (_odd(min(45.0, max(5.0, 0.3 * tall_work))),)  # strokes are thinner than a third of that
    else:
        sizes = STROKE_SIZES

    candidates: List[_Line] = []
    pieces_by_level = []
    for size in sizes:
        contrast = _stroke_contrast(crop, size, np)
        for threshold in CONTRAST_LEVELS:
            pieces = _pieces(contrast, contrast >= threshold, crop, low, high, len(pieces_by_level), np)
            for piece in pieces:
                if piece.confidence >= MIN_CONFIDENCE:
                    piece.score = piece.confidence * _prior(piece, near, tall_work, crop.shape)
                    candidates.append(piece)
            pieces_by_level.append(pieces)
    if not candidates:
        return None
    best = max(candidates, key=lambda line: line.score)
    if within:
        # Learning from an area drawn around the text: the most complete reading of the line wins
        # (grass or a crowd can break the line apart at one level but not at another).
        inside = (within[0] * s - left, within[1] * s - top, within[2] * s - left, within[3] * s - top)
        tall_line = best.box[3] - best.box[1]
        good = sorted((c for c in candidates if c.score >= 0.3 * best.score
                       and 0.75 <= (c.box[3] - c.box[1]) / tall_line <= 1.33), key=lambda c: -c.score)[:12]
        blocks = [(_grow_block(c, pieces_by_level[c.level], inside, stack=False), c) for c in good]
        block, best = max(blocks, key=lambda item: (item[0][2] - item[0][0], item[1].score))
    else:
        block = _grow_block(best, pieces_by_level[best.level])
    x0, y0, x1, y1 = block
    return FoundText(
        ((x0 + left) / s, (y0 + top) / s, (x1 + left) / s, (y1 + top) / s),
        best.letter_height / s,
        round(best.confidence, 3),
    )


def _stroke_contrast(gray, size: int, np):
    """How much each pixel stands out as part of a light or dark stroke thinner than ``size``.

    (The larger of the white and black top-hat transforms.) Letters are made of
    thin strokes; shirts, stripes and the sky are wider, so they and their
    edges drop out even where the text is written over them. Light and dark
    together also keep the thin dark rim around light letters, which holds
    each letter together.
    """
    values = gray.astype(np.int16)
    opened = _max_filter(_min_filter(values, size, np), size, np)
    closed = _min_filter(_max_filter(values, size, np), size, np)
    return np.maximum(values - opened, closed - values)


def _pieces(contrast, mask, crop, low: float, high: float, level: int, np) -> List[_Line]:
    """The lines of letter-sized shapes in ``mask``, each with how much it looks like text."""
    boxes = _components(mask, np)
    if len(boxes) > MAX_SHAPES:
        return []  # far too busy to be text
    heights = boxes[:, 3] - boxes[:, 1]
    widths = boxes[:, 2] - boxes[:, 0]
    fill = boxes[:, 4] / np.maximum(1, widths * heights)
    keep = (heights >= low) & (heights <= high) & (widths <= 25 * heights) & (fill >= 0.08)
    return [_measure(members, crop, contrast, mask, level, np) for members in _group_lines(boxes[keep], np)]


def _min_filter(values, size: int, np):
    return _rank_filter(values, size, np.min, np)


def _max_filter(values, size: int, np):
    return _rank_filter(values, size, np.max, np)


def _rank_filter(values, size: int, reduce, np):
    """``reduce`` (min or max) over a size x size square, as two passes of a line."""
    from numpy.lib.stride_tricks import sliding_window_view

    half = size // 2
    for axis in (0, 1):
        pad = [(half, half) if a == axis else (0, 0) for a in range(2)]
        values = reduce(sliding_window_view(np.pad(values, pad, mode="edge"), size, axis=axis), axis=-1)
    return values


def _odd(value: float) -> int:
    number = int(round(value))
    return number if number % 2 else number + 1


def _components(mask, np):
    """The 8-connected shapes of ``mask``: rows of (x0, y0, x1, y1, pixels), right/bottom exclusive."""
    rows, cols = mask.shape
    edged = np.zeros((rows, cols + 2), dtype=np.int8)
    edged[:, 1:-1] = mask
    steps = np.diff(edged, axis=1)
    run_rows, run_starts = np.nonzero(steps == 1)
    _, run_ends = np.nonzero(steps == -1)
    count = len(run_rows)
    if count == 0:
        return np.zeros((0, 5), dtype=np.int64)
    parent = list(range(count))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    first = np.searchsorted(run_rows, np.arange(rows + 1))
    starts, ends = run_starts.tolist(), run_ends.tolist()
    for row in range(1, rows):
        lo, hi = int(first[row]), int(first[row + 1])
        prev_lo, prev_hi = int(first[row - 1]), int(first[row])
        if lo == hi or prev_lo == prev_hi:
            continue
        j = prev_lo
        for i in range(lo, hi):
            start, end = starts[i], ends[i]
            while j < prev_hi and ends[j] < start:  # ends before this run starts (diagonals touch)
                j += 1
            k = j
            while k < prev_hi and starts[k] <= end:
                a, b = find(i), find(k)
                if a != b:
                    parent[a] = b
                k += 1
    labels = np.array([find(i) for i in range(count)])
    _, index = np.unique(labels, return_inverse=True)
    shapes = index.max() + 1
    x0 = np.full(shapes, cols, dtype=np.int64)
    y0 = np.full(shapes, rows, dtype=np.int64)
    x1 = np.zeros(shapes, dtype=np.int64)
    y1 = np.zeros(shapes, dtype=np.int64)
    pixels = np.zeros(shapes, dtype=np.int64)
    np.minimum.at(x0, index, run_starts)
    np.maximum.at(x1, index, run_ends)
    np.minimum.at(y0, index, run_rows)
    np.maximum.at(y1, index, run_rows + 1)
    np.add.at(pixels, index, run_ends - run_starts)
    return np.stack([x0, y0, x1, y1, pixels], axis=1)


def _group_lines(boxes, np) -> List["object"]:
    """Shapes side by side, about as tall as each other and on one line: letters and words."""
    count = len(boxes)
    if count == 0:
        return []
    order = np.argsort(boxes[:, 0], kind="stable")
    boxes = boxes[order]
    x0, y0, x1, y1 = (boxes[:, i].tolist() for i in range(4))
    heights = [b - t for t, b in zip(y0, y1)]
    reach = WORD_GAP * max(heights)
    parent = list(range(count))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(count):
        for j in range(i + 1, count):
            if x0[j] > x1[i] + reach:
                break
            hi, hj = heights[i], heights[j]
            if max(hi, hj) > 2.2 * min(hi, hj):
                continue
            if x0[j] - x1[i] > WORD_GAP * max(hi, hj):
                continue
            if min(y1[i], y1[j]) - max(y0[i], y0[j]) < 0.5 * min(hi, hj):
                continue
            a, b = find(i), find(j)
            if a != b:
                parent[a] = b
    groups: dict = {}
    for i in range(count):
        groups.setdefault(find(i), []).append(i)
    return [boxes[members] for members in groups.values()]


def _measure(members, crop, edges, mask, level: int, np) -> _Line:
    """How much a group of shapes looks like a line of text (``confidence``, 0..1)."""
    x0, y0 = int(members[:, 0].min()), int(members[:, 1].min())
    x1, y1 = int(members[:, 2].max()), int(members[:, 3].max())
    heights = members[:, 3] - members[:, 1]
    letter = float(np.median(heights))
    width = x1 - x0
    line = _Line((x0, y0, x1, y1), letter, 0.0, 0.0, level)
    if letter < 1 or width < 2 * letter:  # a line of text is wider than tall; a short piece may still join one
        return line
    centers = (members[:, 1] + members[:, 3]) / 2.0
    aligned = 1.0 - min(1.0, float(np.std(centers)) / letter / 0.35) if len(members) > 1 else 1.0
    wide = min(1.0, width / letter / 3.0)

    # Letters are made of strokes: across the line, light and dark keep taking turns.
    patch = crop[y0:y1, x0:x1]
    ink = patch > _otsu(patch, np)
    turns = 0.0
    for part in (0.35, 0.5, 0.65):
        row = ink[min(len(ink) - 1, int(part * len(ink)))]
        turns = max(turns, float(np.count_nonzero(row[1:] != row[:-1])))
    per_letter = turns / (width / letter)
    strokes = min(1.0, max(0.0, (per_letter - 0.8) / 1.7))

    inside = mask[y0:y1, x0:x1]
    strength = float(edges[y0:y1, x0:x1][inside].mean()) if inside.any() else 0.0
    contrast = min(1.0, max(0.0, (strength - 20.0) / 70.0))
    density = float(inside.mean())
    solid = 1.0 if 0.12 <= density <= 0.85 else 0.4

    confidence = aligned * wide * strokes * contrast * solid
    if len(members) < 2:
        confidence *= 0.7  # one shape could be anything; several in a row are convincing
    line.confidence = confidence
    return line


def _otsu(values, np) -> float:
    histogram = np.bincount(values.ravel(), minlength=256).astype(np.float64)
    total = histogram.sum()
    if total == 0:
        return 127.0
    levels = np.arange(256)
    weight = np.cumsum(histogram)
    mean = np.cumsum(histogram * levels)
    between = (mean[-1] * weight - mean * total) ** 2 / np.maximum(weight * (total - weight), 1e-9)
    return float(np.argmax(between))


def _prior(line: _Line, near: Optional[Box], tall: Optional[float], shape) -> float:
    """Text where it is expected, and as tall as expected, counts more.

    Up and down counts much more than sideways: a credit line keeps to its
    line, but may be longer or shorter, or start somewhere else.
    """
    x0, y0, x1, y1 = line.box
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    rows, cols = shape
    if near:
        width, height = max(1.0, near[2] - near[0]), max(1.0, near[3] - near[1])
        dx = abs(cx - (near[0] + near[2]) / 2.0) / max(0.75 * width, 0.05 * cols)
        dy = abs(cy - (near[1] + near[3]) / 2.0) / max(2.5 * (tall or height), 0.04 * rows)
        weight = 1.0 / (1.0 + dx * dx + dy * dy)
        if tall:
            ratio = max(y1 - y0, 1e-3) / tall
            weight *= math.exp(-(math.log(ratio) ** 2) / (2 * 0.45 ** 2))
        return weight
    distance = math.hypot(cols - cx, rows - cy) / math.hypot(cols, rows)
    return 1.0 / (1.0 + (distance / 0.5) ** 2)


def _grow_block(best: _Line, pieces: Sequence[_Line], inside: Optional[Box] = None,
                stack: bool = True) -> Tuple[int, int, int, int]:
    """``best`` with the rest of its text: pieces on the same line, and lines stacked on it.

    Words far apart, or a part of the text over something just as bright, leave
    a line in pieces. A piece joins when it stands exactly on the same line
    (as tall, same middle), and is not too far away - or anywhere ``inside``
    that box. Another line joins when it is right above or below, as tall, and
    starts, ends or centres where this one does.
    """
    x0, y0, x1, y1 = best.box
    letter = best.letter_height
    used = {id(best)}
    grown = True
    while grown:
        grown = False
        for piece in pieces:
            if id(piece) in used:
                continue
            px0, py0, px1, py1 = piece.box
            ratio = max(piece.letter_height, letter) / max(1e-3, min(piece.letter_height, letter))
            gap_x = max(px0 - x1, x0 - px1)
            gap_y = max(py0 - y1, y0 - py1)
            middle = abs((py0 + py1) - (y0 + y1)) / 2.0
            near = gap_x <= JOIN_GAP * letter or (inside is not None and _center_in(piece.box, inside))
            same_line = ratio <= 1.4 and middle <= 0.3 * letter and near and gap_y < 0
            lined_up = min(abs(px0 - x0), abs(px1 - x1), abs((px0 + px1) - (x0 + x1)) / 2.0) <= 1.5 * letter
            stacked = (stack and piece.confidence >= 0.5 and ratio <= 1.25 and lined_up
                       and 0 <= gap_y <= 0.8 * letter and min(x1, px1) > max(x0, px0))
            if same_line or stacked:
                x0, y0, x1, y1 = min(x0, px0), min(y0, py0), max(x1, px1), max(y1, py1)
                used.add(id(piece))
                grown = True
    return x0, y0, x1, y1


def _center_in(box, outer) -> bool:
    cx, cy = (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0
    return outer[0] <= cx <= outer[2] and outer[1] <= cy <= outer[3]


# ----------------------------------------------------------- see-through boxes


def learn_band_box(photo: Picture, area: Area) -> Optional[Box]:
    """The see-through box inside ``area``, drawn against an edge on its own photo, or None."""
    sides = area.glued_sides()
    if not area.shapes or not any(sides):
        return None
    pixels = photo if isinstance(photo, PhotoPixels) else load_photo(photo)
    found = find_box(pixels, bounds(area.on_photo(pixels.size)), sides, drawn=True)
    return tuple(round(v, 1) for v in found.box) if found else None


def find_box(pixels: PhotoPixels, near: Box, sides: Tuple[bool, bool, bool, bool],
             height: Optional[float] = None, known: bool = False, drawn: bool = False) -> Optional[FoundBox]:
    """A see-through box about ``near`` (photo pixels) that sits on the photo edges in ``sides``.

    Its top and bottom edges are rows along which the photo is mixed with one
    grey: row by row, inside = k * outside + c over the whole width, the same
    way at both edges. The free side (the left one for a box on the right
    edge) is where the top and bottom edges end. ``height`` is how tall the
    box probably is.

    ``known``: ``near`` is where the box was found on another photo, placed
    on this one. ``drawn``: ``near`` was drawn around the box, a little bigger
    than it, with about as much room above it as below. Either way one clear
    edge where it is expected is enough, if the photo agrees with the other
    edge where it should be (the photo may be as grey as the box there) and
    the edge ends where the box's side should be.
    """
    np = _numpy()
    s = pixels.scale
    gray = pixels.gray
    rows, cols = gray.shape
    x0, y0, x1, y1 = (v * s for v in near)
    tall, wide = y1 - y0, x1 - x0
    if tall < 6 or wide < 20:
        return None
    left, top, right, bottom = sides
    # The edges are read along the whole box and along its half on the free side: the rows just above
    # and below the text there are plain, while a pattern may be drawn in the rest (Getty's triangles).
    if right and not left:
        strips = [(x0 + 0.15 * wide, cols - 3), (x0 + 0.03 * wide, x0 + 0.5 * wide)]
    elif left and not right:
        strips = [(3, x1 - 0.15 * wide), (x1 - 0.5 * wide, x1 - 0.03 * wide)]
    else:
        strips = [(x0 + 0.15 * wide, x1 - 0.15 * wide)]
    strips = [(int(max(1, a)), int(min(cols - 1, b))) for a, b in strips]
    strips = [(a, b) for a, b in strips if b - a >= 20]
    if not strips:
        return None
    along = (min(a for a, _ in strips), max(b for _, b in strips))
    want = height * s if height else None
    reach = max(0.35 * (want or tall), 0.02 * rows)
    entering = _strongest(_blend_rows(gray, y0, reach, a, b, np, inside_below=True) for a, b in strips)
    leaving = _strongest(_blend_rows(gray, y1, reach, a, b, np, inside_below=False) for a, b in strips)
    single = "drawn" if drawn else "known" if known and want else None
    edges = _box_rows(gray, entering, leaving, want, (y0, y1), along, single, np)
    if edges is None:
        return None
    box_top, box_bottom, seen = edges
    # The free side: where the top and bottom edges end. Drawn around the box, the area was a little
    # wider than it (or cut it a little); placing, the box is not made narrower than expected, only wider
    # where the photo clearly shows more of it.
    free_left, free_right = x0, x1
    if left != right:
        inside_right = right
        expected = x0 if inside_right else x1
        out, into = max((0.03 if drawn else 0.15) * wide, 10.0), max(0.1 * wide, 20.0)
        end = _edge_end(gray, seen, expected, out, into, np, inside_right) if drawn or len(seen) < 2 else None
        if len(seen) < 2 and end is None:
            return None  # one edge alone, and it does not end where the box's side should be
        if drawn:
            side = end if end is not None else expected
        else:
            side = _edge_beyond(gray, seen, expected, out, np, inside_right)
        free_left, free_right = (side, x1) if inside_right else (x0, side)
    box = (0.0 if left else free_left / s, box_top / s,
           float(pixels.size[0]) if right else free_right / s, box_bottom / s)
    if top:
        box = (box[0], 0.0, box[2], box[3])
    if bottom:
        box = (box[0], box[1], box[2], float(pixels.size[1]))
    return FoundBox(box, len(seen), round(1.0 - seen[0][2], 3))


# One edge of a box: (the row outside it, the row inside it, k, c) with inside = k * outside + c.
_Edge = Tuple[int, int, float, float]


def _box_rows(gray, entering, leaving, want: Optional[float], near: Tuple[float, float], along,
              single: Optional[str], np):
    """The box's top row and the row below it: ``(top, bottom, edges seen)``, or None.

    Best, a clear top and bottom edge with the same grey, each also explained
    by the other's grey. Else a clear edge whose other edge its grey finds,
    where no other grey shows. Boxes as tall as ``want`` and about where
    ``near`` (top row, row below) says win.

    Else, with ``single``, one clear edge will do, if the photo agrees with
    the other edge where it should be: it may be as grey as the box there,
    so that the edge does not show, but a row of the photo that only looks
    like a box edge (the top of the pitch, an advertising board) has plain
    photo there instead. ``single`` "known": ``near`` is where the box is
    expected, and the edge must be there; "drawn": ``near`` was drawn around
    the box, as far above it as below.
    """
    y0, y1 = near
    tall = y1 - y0
    rows = gray.shape[0]
    known = single == "known"
    spans = (0.88 * want - 2, 1.12 * want + 2) if want else (0.6 * tall, 1.15 * tall)
    stray = 0.03 * want + 2 if known else 0.1 * tall + 2  # how far the box's middle may be from near's

    def likely(top: float, bottom: float) -> float:
        """How well a box from row ``top`` to ``bottom`` fits what is expected (0..1)."""
        odds = math.exp(-0.5 * (((top + bottom) - (y0 + y1)) / 2.0 / stray) ** 2)
        if want:
            odds *= math.exp(-0.5 * ((bottom - top - want) / (0.03 * want + 1)) ** 2)
        return odds

    # (strength, row, k, c, is_top), all with inside = k * outside + c: ``row`` is the first row inside
    # the box for a top edge, the first one below it for a bottom edge.
    tops = [(strength, row, k, c, True) for row, k, c, strength in entering]
    bottoms = [(strength, row, 1 / k, -c / k, False) for row, k, c, strength in leaving]

    def same_grey(a, b) -> bool:
        return 0.75 <= a[2] / b[2] <= 1.33

    def edge(candidate, row=None, grey=None):
        k, c = (grey or candidate)[2:4]
        return _best_edge(gray, candidate[1] if row is None else row, candidate[4], k, c, along, np)

    # Where the box's own place is known, a box far from it is something else.
    least = 0.05 if known else 0.0
    pairs = sorted((((t[0] + b[0]) * likely(t[1], b[1]), t, b) for t in tops for b in bottoms
                    if spans[0] <= b[1] - t[1] <= spans[1] and b[1] - t[1] > 4 and same_grey(t, b)
                    and likely(t[1], b[1]) >= least), key=lambda pair: -pair[0])
    pair = None
    for _, t, b in pairs[:10]:
        if edge(t, grey=b)[0] >= 0 and edge(b, grey=t)[0] >= 0:
            pair = (likely(t[1], b[1]), edge(t)[1], edge(b)[1])
            break
    if pair is not None and pair[0] >= 0.3:
        return _outermost(gray, pair[1], pair[2], along, np)

    both = found_one = None
    for candidate in sorted(tops + bottoms, key=lambda c: -c[0])[:8]:
        strength, _, k, c, is_top = candidate
        (this_say, this), row = max(((edge(candidate, r), r) for r in range(candidate[1] - 1, candidate[1] + 2)),
                                    key=lambda item: item[0][0])
        if this_say < EDGE_SEEN:
            continue
        this_row = this[0] + 1 if is_top else this[0]
        step = 1 if is_top else -1
        other_greys = [o[1] for o in (bottoms if is_top else tops) if not same_grey(o, candidate)]
        for span in range(int(math.ceil(spans[0])), int(spans[1]) + 1):
            other = row + step * span
            if not 2 <= other < rows - 2 or any(abs(other - r) <= 1 for r in other_greys):
                continue  # a row that shows another grey is not this box's edge
            other_say, other_edge = _best_edge(gray, other, not is_top, k, c, along, np)
            if other_say < EDGE_SEEN:
                continue
            top_edge, bottom_edge = (this, other_edge) if is_top else (other_edge, this)
            odds = likely(top_edge[0] + 1, bottom_edge[0])
            if odds >= least and (both is None or (this_say + other_say) * odds > both[0]):
                both = ((this_say + other_say) * odds, top_edge, bottom_edge, odds)
        # One edge alone must be clear: on the real Getty photos box edges measure 0.27 to 0.9, other rows at most 0.22.
        if both is not None or pair is not None or not single or strength < 0.35 or found_one is not None:
            continue
        if known:
            if abs(this_row - (y0 if is_top else y1)) > 2 + 0.05 * want:
                continue  # not where the box is expected
            guess = row + step * want
        else:  # as much room above the box as below it, in the area drawn around it
            guess = y1 - (row - y0) if is_top else y0 + (y1 - row)
        if abs(guess - row) < 0.5 * (want or tall):
            continue
        guess = int(round(guess))
        says = [_best_edge(gray, r, not is_top, k, c, along, np)[0]
                for r in range(guess - 2, guess + 3) if 2 <= r < rows - 2]
        if not says or max(says) < EDGE_AGAINST:
            continue  # the photo goes on plainly where the box's other edge would be: no box
        found_one = (this_row, guess, [this]) if is_top else (guess, this_row, [this])
    # A pair of clear edges in an unlikely place, or edges found with their grey in a likelier one.
    if both is not None and (pair is None or both[3] > pair[0]):
        return _outermost(gray, both[1], both[2], along, np)
    if pair is not None:
        return _outermost(gray, pair[1], pair[2], along, np)
    return found_one


def _outermost(gray, top_edge: _Edge, bottom_edge: _Edge, along, np):
    """``(top, bottom, [top edge, bottom edge])`` for the box, taking its edges out to the outermost
    step with its grey close by: a line in the photo just inside the box can look like its edge too.
    """
    top, bottom = top_edge[0] + 1, bottom_edge[0]
    reach = max(2, int(0.08 * (bottom - top)))
    for row in range(top - reach, top):
        say, found = _best_edge(gray, row, True, top_edge[2], top_edge[3], along, np)
        if say >= EDGE_SEEN and found[0] + 1 < top:
            top_edge, top = found, found[0] + 1
            break
    for row in range(bottom + reach, bottom, -1):
        say, found = _best_edge(gray, row, False, bottom_edge[2], bottom_edge[3], along, np)
        if say >= EDGE_SEEN and found[0] > bottom:
            bottom_edge, bottom = found, found[0]
            break
    return top, bottom, [top_edge, bottom_edge]


def _best_edge(gray, row: int, is_top: bool, k: float, c: float, along, np) -> Tuple[float, _Edge]:
    """How much the photo says the box's top edge (``row`` its first row inside) or bottom edge (``row``
    the first row below it) runs there, and the rows it is read across.

    An edge in a photo is often spread over two rows (resized, JPEG): the rows
    right next to it are read across, and those one row further.
    """
    rows = gray.shape[0]
    if is_top:
        variants = [(row - 1, row), (row - 1, row + 1), (row - 2, row)]
    else:
        variants = [(row, row - 1), (row + 1, row - 1), (row, row - 2)]
    best = (-EDGE_CLIP, (row - 1, row, k, c) if is_top else (row, row - 1, k, c))
    for outside_row, inside_row in variants:
        if 0 <= outside_row < rows and 0 <= inside_row < rows:
            edge = (outside_row, inside_row, k, c)
            say = _edge_say(gray, edge, along[0], along[1], np)
            if say > best[0]:
                best = (say, edge)
    return best


def _edge_say(gray, edge: _Edge, c0: int, c1: int, np) -> float:
    """How much the photo along ``edge`` says a box edge runs there (> 0) or not (< 0), in grey levels."""
    return float(np.median(_edge_says(gray, edge, c0, c1, np)))


def _edge_says(gray, edge: _Edge, c0: int, c1: int, np):
    """Per column: the step from the row outside to the row inside, less what the box's grey leaves of it.

    Across a box edge the grey explains the step (> 0); where there is no
    box edge, the photo just goes on and the grey does not fit (< 0); where
    the photo is about as grey as the box, it tells nothing (0).
    """
    outside_row, inside_row, k, c = edge
    outside = gray[outside_row, c0:c1].astype(np.float64)
    inside = gray[inside_row, c0:c1].astype(np.float64)
    return np.clip(np.abs(inside - outside) - np.abs(inside - (k * outside + c)), -EDGE_CLIP, EDGE_CLIP)


def _edge_end(gray, seen: Sequence[_Edge], expected: float, out: float, into: float, np,
              inside_right: bool) -> Optional[float]:
    """Where the box's top and bottom edges end, up to ``out`` columns outside ``expected`` and ``into``
    columns inside it: the box's free side, or None.

    Outside the box the photo goes on plainly across the edges' rows, inside
    it the grey explains the steps across them; both have to show. Where the
    photo is about as grey as the box, it tells nothing: such columns are
    taken as the box's, so that the box found is never too narrow.
    """
    says, places = _free_side_says(gray, seen, expected, out, into, np, inside_right)
    count = len(says)
    if count < 10:
        return None
    # The box from the i-th column on: columns before it that do not look plainly outside, and columns
    # from it on that do, count against it.
    against_outside = np.concatenate([[0.0], np.cumsum(np.maximum(0.0, says + EDGE_MARGIN))])
    against_inside = np.concatenate([np.cumsum(np.maximum(0.0, -says - EDGE_MARGIN)[::-1])[::-1], [0.0]])
    cost = against_outside + against_inside
    cost[:3] = cost[count - 2:] = np.inf  # a few columns on either side at least
    i = int(np.argmin(cost))  # the first of equals: the widest box
    if not np.isfinite(cost[i]):
        return None
    outside, inside = -says[:i].mean(), says[i:].mean()
    if outside < EDGE_END or inside < EDGE_END:
        return None
    return float(places[i])


def _edge_beyond(gray, seen: Sequence[_Edge], expected: float, out: float, np, inside_right: bool) -> float:
    """``expected``, or the box's free side further out, where the photo clearly shows the box goes on
    (along all of its edges seen: a line in the photo may go on from one of them)."""
    says, places = _free_side_says(gray, seen, expected, out, 0.0, np, inside_right, agree=True)
    if not len(says):
        return expected
    # Taking the box out to column i: what the columns from there to ``expected`` say, beyond doubt.
    gain = np.concatenate([np.cumsum((says - EDGE_MARGIN)[::-1])[::-1], [0.0]])
    i = int(np.argmax(gain))
    return float(places[i]) if gain[i] > 0 else expected


def _free_side_says(gray, seen: Sequence[_Edge], expected: float, out: float, into: float, np,
                    inside_right: bool, agree: bool = False):
    """What the columns around the box's free side say across its edges (on average, or the least of
    them with ``agree``), outermost first, and the places between them (``places[i]``: the box from
    the i-th column on, inwards)."""
    cols = gray.shape[1]
    if inside_right:
        lo, hi = int(max(1, round(expected - out))), int(min(cols, round(expected + into)))
    else:
        lo, hi = int(max(1, round(expected - into))), int(min(cols, round(expected + out)))
    if hi <= lo:
        return np.zeros(0), np.zeros(1)
    each = [_edge_says(gray, edge, lo, hi, np) for edge in seen]
    says = np.minimum.reduce(each) if agree else sum(each) / len(each)
    places = np.arange(lo, hi + 1, dtype=np.float64)
    if not inside_right:
        says, places = says[::-1], places[::-1]
    return says, places


def _blend_rows(gray, center: float, reach: float, c0: int, c1: int, np, inside_below: bool):
    """Rows near ``center`` that are the row before them mixed with a grey: ``(row, k, c, strength)``.

    ``row`` is the first row inside the box (inside_below) or the first one
    below it; k < 1 going into the box, k > 1 coming out of it.
    """
    rows = gray.shape[0]
    first, last = int(max(1, center - reach)), int(min(rows - 1, center + reach))
    if last - first < 1:
        return []
    before = gray[first - 1:last - 1, c0:c1].astype(np.float64)
    after = gray[first:last, c0:c1].astype(np.float64)
    k, c, fit, spread_before, spread_after = _row_fits(before, after, np)
    spread = spread_before if inside_below else spread_after  # the photo's own row, outside the box
    found = []
    for i in range(len(k)):
        # A see-through grey keeps some of the photo (k > 0): a darker-for-lighter row is something else.
        blended = 0.05 <= k[i] <= 0.93 if inside_below else 1.07 <= k[i] <= 20.0
        if blended and fit[i] >= 0.9 and spread[i] >= 20:
            nearness = 1.0 - min(1.0, abs(first + i - center) / (2.0 * reach))
            found.append((first + i, float(k[i]), float(c[i]), float(fit[i] * abs(math.log(k[i])) * (0.5 + nearness))))
    return found


def _strongest(found_lists) -> list:
    """Candidates from several strips: for each row the strongest."""
    best: dict = {}
    for found in found_lists:
        for candidate in found:
            if candidate[0] not in best or candidate[3] > best[candidate[0]][3]:
                best[candidate[0]] = candidate
    return list(best.values())


def _row_fits(before, after, np, keep: float = 0.7):
    """Per row: ``after = k * before + c`` fitted twice, the second time without the 30% worst pixels
    (the text, and the pattern drawn inside Getty Images' box). Returns k, c, fit (r²) and both spreads."""
    k, c = _weighted_fit(before, after, None, np)[:2]
    miss = np.abs(after - (k[:, None] * before + c[:, None]))
    kept = (miss <= np.quantile(miss, keep, axis=1, keepdims=True)).astype(np.float64)
    return _weighted_fit(before, after, kept, np)


def _weighted_fit(a, b, weight, np):
    if weight is None:
        weight = np.ones_like(a)
    count = np.maximum(weight.sum(axis=1), 1.0)
    mean_a = (weight * a).sum(axis=1) / count
    mean_b = (weight * b).sum(axis=1) / count
    da, db = a - mean_a[:, None], b - mean_b[:, None]
    var_a = (weight * da * da).sum(axis=1) / count
    var_b = (weight * db * db).sum(axis=1) / count
    cov = (weight * da * db).sum(axis=1) / count
    k = cov / np.maximum(var_a, 1e-9)
    c = mean_b - k * mean_a
    fit = cov * cov / np.maximum(var_a * var_b, 1e-9)
    return k, c, fit, var_a, var_b


Locate = Callable[[str, Union[Area, AreaSet]], Placement]
