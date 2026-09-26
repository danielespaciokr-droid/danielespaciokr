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
        """An area drawn around the credit on a 1920x1080 photo, with where the text is inside it.

        It stays clear of the photo's edges: an area drawn against an edge is looked for as a box there.
        """
        image = photo((1920, 1080), seed=3)
        text = write(image, CREDIT, 26, 80, 60)
        height = text[3] - text[1]
        drawn = shapes.rect(text[0] - 0.6 * height, text[1] - 0.5 * height, text[2] + 0.5 * height, text[3] + 0.5 * height)
        area = shapes.Area([drawn], (1920, 1080), "anchor")
        box = textfind.learn_text_box(pixels(image), area)
        self.assertIsNotNone(box)
        for mine, real in zip(box, text):  # the text and its shadow, not the room drawn around it
            self.assertAlmostEqual(mine, real, delta=0.5 * height)
        area = shapes.Area([drawn], (1920, 1080), "anchor", text_box=box)
        self.assertFalse(any(area.glued_sides()))
        return area, text

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
                bounds = shapes.bounds(placement.area.shapes)
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
        text = write(image, CREDIT, 26, 80, 60)
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
        moved = write(image, CREDIT, 26, 110, 76)  # a bit off from where the area was drawn
        placement = textfind.place(pixels(image), old)
        self.assertIsNotNone(placement.found)
        self.assertGreaterEqual(covered(placement.area, image.size, moved), 0.99)

    def test_the_bottom_right_is_searched_only(self):
        image = photo((1600, 1067), seed=17)
        ImageDraw.Draw(image).text((60, 60), "TOP LEFT TITLE", font=_font(40), fill=(255, 255, 255))
        self.assertIsNone(textfind.place(pixels(image), None).found)


def getty(image, credit="Credit: Insidefoto", grey=50, alpha=0.35, flat_below=False):
    """Getty Images' credit box: see-through grey, on the right edge, 800 px of 2000 wide, 144 px tall,
    centred at two thirds of the height. Returns the photo with it and the box."""
    width, height = image.size
    s = max(width, height) / 2000
    box = (width - round(800 * s), round(2 * height / 3 - 72 * s), width, round(2 * height / 3 + 72 * s))
    values = np.asarray(image, dtype=np.float64).copy()
    if flat_below:  # the photo about as grey as the box along its bottom edge: that edge does not show
        values[box[3] - round(30 * s):box[3] + round(30 * s)] = grey
    inside = values[box[1]:box[3], box[0]:box[2]]
    values[box[1]:box[3], box[0]:box[2]] = inside * (1 - alpha) + grey * alpha
    image = Image.fromarray(values.clip(0, 255).astype(np.uint8))
    draw = ImageDraw.Draw(image)
    draw.text((box[0] + 25 * s, box[1] + 20 * s), "gettyimages", font=_font(round(36 * s)), fill=(255, 255, 255))
    draw.text((box[0] + 25 * s, box[1] + 80 * s), credit, font=_font(round(24 * s)), fill=(255, 255, 255))
    return image, box


def fits_around(test, placement, size, box, room=20):
    """The area removes all of ``box``, and little more."""
    test.assertEqual(covered(placement.area, size, box), 1.0)
    outer = shapes.bounds(placement.area.on_photo(size))
    test.assertLessEqual(box[0] - outer[0], room)
    test.assertLessEqual(box[1] - outer[1], room)
    test.assertLessEqual(outer[3] - box[3], room)


@unittest.skipUnless(FONT_OK, "numpy, Pillow or a font is missing")
class FindBoxTests(unittest.TestCase):
    """Getty Images' see-through credit box on the right edge, whatever the credit's length."""

    def learned(self, size=(2000, 1333), seed=3, room=10):
        image, box = getty(photo(size, seed=seed))
        drawn = shapes.rect(box[0] - room, box[1] - room, size[0], box[3] + room)  # to the right edge
        area = shapes.Area([drawn], size, "anchor")
        self.assertEqual(area.glued_sides(), (False, False, True, False))
        band = textfind.learn_band_box(pixels(image), area)
        self.assertIsNotNone(band)
        for mine, real in zip(band, box):
            self.assertAlmostEqual(mine, real, delta=3)
        return shapes.Area([drawn], size, "anchor", band_box=band), box

    def test_learns_the_box_the_area_was_drawn_around(self):
        self.learned()
        self.learned((1335, 2000), seed=4)

    def test_finds_the_box_on_photos_of_other_sizes(self):
        area, _ = self.learned()
        credits = ["Credit: AFP", "Credit: Mondadori Portfolio via Getty Images", "Credit: NurPhoto"]
        for i, size in enumerate([(2000, 1333), (1335, 2000), (3000, 2000), (1600, 900), (1024, 683)]):
            with self.subTest(size=size):
                image, box = getty(photo(size, seed=40 + i), credit=credits[i % 3])
                placement = textfind.place(pixels(image), area)
                self.assertIsNotNone(placement.found)
                self.assertEqual((placement.note, placement.kind), ("상자 찾음", "box"))
                self.assertEqual(placement.found.box[2], size[0])  # out to the right edge
                fits_around(self, placement, size, box)

    def test_one_edge_is_enough_where_the_box_is_expected(self):
        area, _ = self.learned()
        image, box = getty(photo((2000, 1333), seed=50), flat_below=True)
        placement = textfind.place(pixels(image), area)
        self.assertIsNotNone(placement.found)
        self.assertEqual(placement.found.edges, 1)
        fits_around(self, placement, image.size, box)

    def test_without_the_box_the_saved_area_is_used(self):
        area, _ = self.learned()
        for seed in range(3):
            with self.subTest(seed=seed):
                placement = textfind.place(pixels(photo((2000, 1333), seed=60 + seed)), area)
                self.assertIsNone(placement.found)
                self.assertIs(placement.area, area)
                self.assertEqual((placement.note, placement.kind), ("상자 못 찾음: 저장된 위치", "box"))

    def test_area_saved_before_boxes_were_learned(self):
        # Drawn around the box, but saved without where the box is inside it: the box is found all the
        # same, and the area as drawn is removed too.
        drawn = shapes.rect(1180, 800, 2000, 980)
        old = shapes.Area([drawn], (2000, 1333), "anchor", text_box=(1225, 837, 1500, 930))
        image, box = getty(photo((1335, 2000), seed=70))
        placement = textfind.place(pixels(image), shapes.AreaSet.single(old))
        self.assertEqual(placement.kind, "box")  # its text is not followed: the credit's length varies
        self.assertIsNotNone(placement.found)
        fits_around(self, placement, image.size, box, room=30)  # the room drawn around it, and a little
        drawn, selected = shapes.bounds(old.on_photo(image.size)), shapes.bounds(placement.area.shapes)
        self.assertTrue(selected[0] <= drawn[0] and selected[1] <= drawn[1] and selected[3] >= drawn[3])

    def test_box_height_from_the_other_orientation(self):
        landscape, _ = self.learned()
        portrait = shapes.Area([shapes.rect(510, 1240, 1335, 1430)], (1335, 2000), "anchor")
        both = shapes.AreaSet({"landscape": landscape, "portrait": portrait})
        image, box = getty(photo((1317, 2000), seed=80))
        placement = textfind.place(pixels(image), both)
        self.assertIsNotNone(placement.found)
        fits_around(self, placement, image.size, box, room=30)


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
        area = shapes.Area([shapes.rect(0, 0, 10, 10)], (100, 80), "anchor", text_box=(2, 3, 8.5, 7),
                           band_box=(1, 2, 100, 9))
        data = area.to_dict()
        self.assertEqual((data["text_box"], data["band_box"]), ([2.0, 3.0, 8.5, 7.0], [1.0, 2.0, 100.0, 9.0]))
        self.assertEqual(shapes.Area.from_dict(data), area)
        plain = shapes.Area([shapes.rect(0, 0, 1, 1)]).to_dict()
        self.assertNotIn("text_box", plain)
        self.assertNotIn("band_box", plain)
        for key in ("text_box", "band_box"):
            for bad in ([1, 2, 3], [5, 5, 1, 9]):
                with self.subTest(key=key, bad=bad), self.assertRaises(shapes.SelectionError):
                    shapes.Area.from_dict(dict(data, **{key: bad}))


if __name__ == "__main__":
    unittest.main()
