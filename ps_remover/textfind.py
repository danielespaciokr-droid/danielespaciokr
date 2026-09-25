"""Find the English text (a credit, a watermark) near the bottom-right corner of a photo.

Photos come in many sizes, so an area saved once is not always where the text
is. Here the text itself is looked for: letters are small shapes with sharp
edges, standing side by side on one line and all about as tall. Grass, crowds
and blurred stadiums rarely look like that. The removal area is then put
around the text that was found.

Only the photo's pixels are used (Pillow and numpy); Photoshop is not needed.
"""

from __future__ import annotations

import importlib.util
import math
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple, Union

from .shapes import Area, AreaSet, Box, SelectionError, Shape, fit_transform, rect

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
class Placement:
    """Where to remove on one photo."""

    area: Optional[Area]         # to select on this photo; None when there is nothing to select
    found: Optional[FoundText]   # None: the text was not found, ``area`` is the saved one
    note: str                    # short, for the log


def available() -> bool:
    return importlib.util.find_spec("numpy") is not None


# ----------------------------------------------------------------- placing


def place(photo: Picture, area: Union[Area, AreaSet, None], margin: float = DEFAULT_MARGIN) -> Placement:
    """The area to remove on ``photo``, following the text found near where ``area`` expects it.

    An area that knows where the text was on its own photo (``text_box``) is
    moved and scaled with the text, so the margins drawn around the text stay
    the same. Otherwise the text found is removed with ``margin`` letter
    heights around it. Without text found, ``area`` is used as saved. Without
    an area the text is looked for near the bottom-right corner.
    """
    pixels = photo if isinstance(photo, PhotoPixels) else load_photo(photo)
    size = pixels.size
    chosen = area.for_size(size) if isinstance(area, AreaSet) else area
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
    bounds = _bounds(area.on_photo(pixels.size))
    width, height = bounds[2] - bounds[0], bounds[3] - bounds[1]
    window = (bounds[0] - 0.25 * width, bounds[1] - 0.25 * height, bounds[2] + 0.25 * width, bounds[3] + 0.25 * height)
    # The area was drawn around all of the text: every piece on its line inside the area belongs to it.
    found = find_text(pixels, window, bounds, within=bounds)
    if found is None:
        return None
    left, top, right, bottom = found.box
    # Only what lies inside the drawn area counts.
    box = (max(left, bounds[0]), max(top, bounds[1]), min(right, bounds[2]), min(bottom, bounds[3]))
    if box[2] - box[0] < 2 or box[3] - box[1] < 2:
        return None
    return tuple(round(v, 1) for v in box)


def _expected_boxes(area: Area, size: Tuple[int, int]) -> Tuple[Optional[Box], Optional[Box]]:
    """Where ``area`` and the text it knows about land on a photo of ``size``, if that can be said."""
    if not area.shapes:
        return None, None
    try:
        expected_area = _bounds(area.on_photo(size))
    except SelectionError:  # fit "exact" on a photo of another shape
        return None, None
    expected_text = None
    if area.text_box and area.image_size:
        sx, sy, tx, ty = fit_transform(area.image_size, size, area.fit, area.anchor)
        left, top, right, bottom = area.text_box
        expected_text = (left * sx + tx, top * sy + ty, right * sx + tx, bottom * sy + ty)
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


def _bounds(shapes: Sequence[Shape]) -> Box:
    boxes = [shape.box for shape in shapes if shape.mode == "add"] or [shape.box for shape in shapes]
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


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


Locate = Callable[[str, Union[Area, AreaSet]], Placement]
