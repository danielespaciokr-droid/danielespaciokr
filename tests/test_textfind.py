"""Finding the credit text in photos (ps_remover.textfind), on made-up football photos."""

import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ps_remover import shapes, textfind

try:
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
except ImportError:  # pragma: no cover - numpy or Pillow missing
    np = None


def _font(size):
    for name in ("DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "Arial.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)  # Pillow 10.1 and later
    except TypeError:
        return None


FONT_OK = np is not None and _font(20) is not None


def photo(size, seed=1, stripes=True):
    """A pitch: blurred stands at the top, mowed grass with blades, maybe a striped shirt."""
    rng = random.Random(seed)
    width, height = size
    small = Image.new("RGB", (max(2, width // 40), max(2, height // 40)))
    small.putdata([tuple(rng.randint(30, 220) for _ in range(3)) for _ in range(small.width * small.height)])
    image = small.resize(size, Image.BICUBIC).filter(ImageFilter.GaussianBlur(width / 150))
    draw = ImageDraw.Draw(image)
    top = int(height * 0.55)
    stripe = max(8, width // 9)
    for x in range(0, width, stripe):
        green = 110 + (25 if (x // stripe) % 2 else 0)
        draw.rectangle((x, top, x + stripe, height), fill=(40, green, 45))
    for _ in range(width * (height - top) // 80):
        x, y = rng.randrange(width), rng.randrange(top, height)
        shade = rng.randint(50, 190)
        draw.line((x, y, x, y - rng.randint(2, 6)), fill=(shade // 3, shade, shade // 3))
    if stripes:
        x0, x1 = int(width * 0.3), int(width * 0.55)
        for i, x in enumerate(range(x0, x1, max(4, width // 40))):
            draw.rectangle((x, int(height * 0.4), x + width // 40, height), fill=(20, 20, 20) if i % 2 else (230, 230, 230))
    return image


def write(image, text, size, right, bottom, fill=(255, 255, 255), shadow=True):
    """Writes ``text`` ending ``right`` px from the right and ``bottom`` px from the bottom; returns its box."""
    draw = ImageDraw.Draw(image)
    font = _font(size)
    left, top, r, b = draw.textbbox((0, 0), text, font=font)
    x = image.width - right - r
    y = image.height - bottom - b
    if shadow:
        draw.text((x + 2, y + 2), text, font=font, fill=(0, 0, 0))
    draw.text((x, y), text, font=font, fill=fill)
    return (x + left, y + top, x + r, y + b)


def pixels(image):
    """The photo as textfind reads it, without a file."""
    width, height = image.size
    scale = min(1.0, textfind.WORK_SHORT_SIDE / min(width, height))
    gray = image.convert("L")
    if scale < 1:
        gray = gray.resize((round(width * scale), round(height * scale)), Image.BILINEAR)
    return textfind.PhotoPixels((width, height), gray.width / width, np.asarray(gray, dtype=np.uint8))


def covered(area, size, box):
    """How much of ``box`` the area removes."""
    mask = np.asarray(shapes.render_mask(area.on_photo(size), size)) > 0
    left, top, right, bottom = (int(round(v)) for v in box)
    return float(mask[top:bottom, left:right].mean())


CREDIT = "Photo by Marco Rossi/LaPresse"


@unittest.skipUnless(FONT_OK, "numpy, Pillow or a font is missing")
class FindTextTests(unittest.TestCase):
    def reference_area(self):
        """An area drawn around the credit on a 1920x1080 photo, with where the text is inside it."""
        image = photo((1920, 1080), seed=3)
        text = write(image, CREDIT, 26, 40, 24)
        height = text[3] - text[1]
        drawn = shapes.rect(text[0] - 0.6 * height, text[1] - 0.5 * height, text[2] + 0.5 * height, text[3] + 0.5 * height)
        area = shapes.Area([drawn], (1920, 1080), "anchor")
        box = textfind.learn_text_box(pixels(image), area)
        self.assertIsNotNone(box)
        for mine, real in zip(box, text):  # the text and its shadow, not the room drawn around it
            self.assertAlmostEqual(mine, real, delta=0.5 * height)
        return shapes.Area([drawn], (1920, 1080), "anchor", text_box=box), text

    def test_finds_a_credit_near_the_bottom_right_corner(self):
        image = photo((1600, 1067), seed=5)
        text = write(image, "Getty Images", 30, 36, 30)
        placement = textfind.place(pixels(image), None)
        self.assertIsNotNone(placement.found)
        self.assertEqual(placement.note, "글자 찾음")
        self.assertGreaterEqual(covered(placement.area, image.size, text), 0.99)
        self.assertEqual(placement.area.image_size, (1600, 1067))
        self.assertEqual(placement.area.fit, "exact")  # already on this photo's pixels

    def test_follows_the_text_on_photos_of_other_sizes(self):
        area, _ = self.reference_area()
        cases = [((1080, 1350), 26, 60, 50), ((2000, 3000), 48, 30, 90), ((1200, 800), 16, 20, 12), ((1920, 1080), 26, 90, 70)]
        for size, letter, right, bottom in cases:
            with self.subTest(size=size, right=right, bottom=bottom):
                image = photo(size, seed=sum(size))
                text = write(image, CREDIT, letter, right, bottom)
                placement = textfind.place(pixels(image), area)
                self.assertIsNotNone(placement.found)
                self.assertGreaterEqual(covered(placement.area, size, text), 0.99)
                # The room drawn around the text comes along, scaled with the letters (a little more
                # for small text, whose shadow and edges count for more of its height).
                bounds = textfind._bounds(placement.area.shapes)
                self.assertLess((bounds[2] - bounds[0]) * (bounds[3] - bounds[1]),
                                8 * (text[2] - text[0]) * (text[3] - text[1]))

    def test_text_partly_hidden_is_widened_to_its_learned_width(self):
        area, _ = self.reference_area()
        image = photo((1920, 1080), seed=7, stripes=False)
        text = write(image, CREDIT, 26, 50, 30, fill=(250, 250, 250), shadow=False)
        # A white shirt behind the first half: white on white cannot be seen, but is still there.
        ImageDraw.Draw(image).rectangle((text[0] - 10, text[1] - 30, (text[0] + text[2]) // 2, image.height), fill=(250, 250, 250))
        write(image, CREDIT, 26, 50, 30, fill=(250, 250, 250), shadow=False)
        placement = textfind.place(pixels(image), area)
        self.assertIsNotNone(placement.found)
        self.assertGreaterEqual(covered(placement.area, image.size, text), 0.97)

    def test_a_sponsor_on_a_shirt_is_not_taken_for_the_credit(self):
        area, _ = self.reference_area()
        image = photo((1920, 1080), seed=11)
        ImageDraw.Draw(image).text((1250, 700), "Emirates", font=_font(70), fill=(255, 255, 255))
        text = write(image, CREDIT, 26, 40, 24)
        placement = textfind.place(pixels(image), area)
        self.assertGreaterEqual(covered(placement.area, image.size, text), 0.99)
        self.assertLess(covered(placement.area, image.size, (1250, 700, 1500, 770)), 0.05)

    def test_without_text_the_saved_area_is_used(self):
        area, _ = self.reference_area()
        pair = shapes.AreaSet.single(area)
        for seed in range(4):
            with self.subTest(seed=seed):
                placement = textfind.place(pixels(photo((1920, 1080), seed=20 + seed)), pair)
                self.assertIsNone(placement.found)
                self.assertIs(placement.area, area)  # placed on the photo as saved, by Photoshop
                self.assertEqual(placement.note, "글자 못 찾음: 저장된 위치")
        nothing = textfind.place(pixels(photo((800, 600), seed=30)), None)
        self.assertEqual((nothing.area, nothing.found, nothing.note), (None, None, "글자 못 찾음"))

    def test_area_saved_without_a_text_box(self):
        _, text = self.reference_area()
        height = text[3] - text[1]
        old = shapes.Area([shapes.rect(text[0] - height, text[1] - height, text[2] + height, text[3] + height)],
                          (1920, 1080), "anchor")
        image = photo((1920, 1080), seed=13)
        moved = write(image, CREDIT, 26, 70, 40)  # a bit off from where the area was drawn
        placement = textfind.place(pixels(image), old)
        self.assertIsNotNone(placement.found)
        self.assertGreaterEqual(covered(placement.area, image.size, moved), 0.99)

    def test_the_bottom_right_is_searched_only(self):
        image = photo((1600, 1067), seed=17)
        ImageDraw.Draw(image).text((60, 60), "TOP LEFT TITLE", font=_font(40), fill=(255, 255, 255))
        self.assertIsNone(textfind.place(pixels(image), None).found)


@unittest.skipUnless(np is not None, "numpy or Pillow is missing")
class ReadingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_rotated_photo_is_read_like_photoshop_opens_it(self):
        path = self.dir / "rotated.jpg"
        exif = Image.Exif()
        exif[0x0112] = 6  # shown turned by 90 degrees
        Image.new("RGB", (400, 300), (90, 120, 60)).save(path, exif=exif)
        loaded = textfind.load_photo(path)
        self.assertEqual(loaded.size, (300, 400))
        self.assertEqual(loaded.gray.shape, (400, 300))

    def test_big_photo_is_searched_smaller(self):
        path = self.dir / "big.jpg"
        Image.new("RGB", (4800, 3200), (90, 120, 60)).save(path, quality=70)
        loaded = textfind.load_photo(path)
        self.assertEqual(loaded.size, (4800, 3200))
        self.assertEqual(min(loaded.gray.shape), textfind.WORK_SHORT_SIDE)
        self.assertAlmostEqual(loaded.scale, textfind.WORK_SHORT_SIDE / 3200, places=3)

    def test_16_bit_photo(self):
        path = self.dir / "deep.png"
        Image.new("I;16", (60, 40), 65535).save(path)
        loaded = textfind.load_photo(path)
        self.assertEqual(int(loaded.gray.max()), 255)

    def test_unreadable_photo(self):
        path = self.dir / "raw.cr2"
        path.write_bytes(b"not a picture Pillow knows")
        with self.assertRaises(textfind.TextFindUnavailable):
            textfind.load_photo(path)

    def test_without_numpy(self):
        with mock.patch.object(textfind.importlib.util, "find_spec", return_value=None):
            self.assertFalse(textfind.available())
        with mock.patch.dict("sys.modules", {"numpy": None}):
            with self.assertRaisesRegex(textfind.TextFindUnavailable, "numpy"):
                textfind.load_photo(self.dir / "x.jpg")

    def test_shapes_in_a_mask(self):
        mask = np.zeros((6, 12), dtype=bool)
        mask[1:4, 1:3] = True      # a block
        mask[4, 3] = True          # touching it corner to corner
        mask[0:6, 8] = True        # a line on its own
        found = sorted(map(tuple, textfind._components(mask, np).tolist()))
        self.assertEqual(found, [(1, 1, 4, 5, 7), (8, 0, 9, 6, 6)])
        self.assertEqual(len(textfind._components(np.zeros((3, 3), dtype=bool), np)), 0)

    def test_letters_group_into_lines(self):
        boxes = np.array([
            [10, 10, 18, 22, 50], [20, 10, 28, 22, 50], [30, 11, 37, 22, 40],  # one word
            [45, 10, 53, 22, 50],                                              # the next word, close
            [150, 10, 158, 22, 50],                                            # far away: another piece
            [20, 60, 60, 120, 900],                                            # much taller: not a letter here
        ])
        groups = sorted(len(g) for g in textfind._group_lines(boxes, np))
        self.assertEqual(groups, [1, 1, 4])


class AreaTextBoxTests(unittest.TestCase):
    def test_round_trip_and_checks(self):
        area = shapes.Area([shapes.rect(0, 0, 10, 10)], (100, 80), "anchor", text_box=(2, 3, 8.5, 7))
        data = area.to_dict()
        self.assertEqual(data["text_box"], [2.0, 3.0, 8.5, 7.0])
        self.assertEqual(shapes.Area.from_dict(data), area)
        self.assertNotIn("text_box", shapes.Area([shapes.rect(0, 0, 1, 1)]).to_dict())
        for bad in ([1, 2, 3], [5, 5, 1, 9]):
            with self.subTest(bad=bad), self.assertRaises(shapes.SelectionError):
                shapes.Area.from_dict(dict(data, text_box=bad))


if __name__ == "__main__":
    unittest.main()
