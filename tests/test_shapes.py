import json
import math
import tempfile
import unittest
from pathlib import Path

from ps_remover import shapes
from ps_remover.shapes import SelectionError, Shape

try:
    import PIL  # noqa: F401
except ImportError:  # pragma: no cover
    PIL = None


class ShapeTests(unittest.TestCase):
    def test_rect_corners_are_normalized(self):
        shape = shapes.rect(200, 150, 100, 50)
        self.assertEqual(shape.box, (100, 50, 200, 150))
        self.assertEqual(shape.to_ops(), [{"op": "rect", "mode": "add", "box": [100, 50, 200, 150]}])

    def test_empty_rect_is_rejected(self):
        with self.assertRaises(SelectionError):
            shapes.rect(10, 10, 10, 50)

    def test_polygon_needs_three_points(self):
        with self.assertRaises(SelectionError):
            shapes.polygon([(0, 0), (10, 10)])

    def test_unknown_kind_and_mode(self):
        with self.assertRaises(SelectionError):
            Shape("star", [(0, 0)])
        with self.assertRaises(SelectionError):
            shapes.rect(0, 0, 5, 5, mode="xor")

    def test_non_finite_coordinates(self):
        with self.assertRaises(SelectionError):
            shapes.rect(0, 0, float("nan"), 5)
        with self.assertRaises(SelectionError):
            shapes.polygon([(0, 0), (1, "a"), (2, 2)])

    def test_brush_single_point_is_a_circle(self):
        ops = shapes.brush([(50, 60)], 10).to_ops()
        self.assertEqual(ops, [{"op": "ellipse", "mode": "add", "box": [40, 50, 60, 70]}])

    def test_brush_stroke_becomes_capsules(self):
        ops = shapes.brush([(0, 0), (100, 0), (100, 100)], 10, mode="subtract").to_ops()
        self.assertEqual(len(ops), 2)
        for op in ops:
            self.assertEqual(op["op"], "polygon")
            self.assertEqual(op["mode"], "subtract")
        xs = [p[0] for p in ops[0]["points"]]
        ys = [p[1] for p in ops[0]["points"]]
        self.assertAlmostEqual(min(xs), -10, places=1)
        self.assertAlmostEqual(max(xs), 110, places=1)
        self.assertAlmostEqual(min(ys), -10, places=1)
        self.assertAlmostEqual(max(ys), 10, places=1)

    def test_capsule_is_convex_and_keeps_radius(self):
        a, b, r = (10.0, 20.0), (70.0, 50.0), 12.0
        outline = shapes.capsule(a, b, r)
        for x, y in outline:
            distance = min(math.hypot(x - a[0], y - a[1]), math.hypot(x - b[0], y - b[1]))
            self.assertAlmostEqual(distance, r, places=6)
        # Convex polygon: all cross products share one sign.
        signs = set()
        n = len(outline)
        for i in range(n):
            (x1, y1), (x2, y2), (x3, y3) = outline[i], outline[(i + 1) % n], outline[(i + 2) % n]
            cross = (x2 - x1) * (y3 - y2) - (y2 - y1) * (x3 - x2)
            if abs(cross) > 1e-9:
                signs.add(cross > 0)
        self.assertEqual(len(signs), 1)

    def test_brush_box_includes_radius(self):
        self.assertEqual(shapes.brush([(10, 10), (20, 30)], 5).box, (5, 5, 25, 35))

    def test_simplify_path_removes_collinear_points(self):
        path = [(x, 0.0) for x in range(0, 101, 5)] + [(100.0, 50.0)]
        self.assertEqual(shapes.simplify_path(path, 0.5), [(0.0, 0.0), (100.0, 0.0), (100.0, 50.0)])

    def test_simplify_path_keeps_corners_and_drops_duplicates(self):
        path = [(0, 0), (0, 0), (10, 10), (20, 0)]
        self.assertEqual(shapes.simplify_path(path, 1), [(0.0, 0.0), (10.0, 10.0), (20.0, 0.0)])

    def test_dict_round_trip(self):
        original = [
            shapes.rect(1, 2, 30, 40),
            shapes.ellipse(5, 5, 15, 25, mode="subtract"),
            shapes.polygon([(0, 0), (10, 0), (5, 8)]),
            shapes.brush([(1, 1), (9, 9)], 3.5),
        ]
        restored = [Shape.from_dict(json.loads(json.dumps(s.to_dict()))) for s in original]
        self.assertEqual([s.to_dict() for s in restored], [s.to_dict() for s in original])

    def test_from_dict_accepts_x_y_width_height(self):
        shape = Shape.from_dict({"type": "ellipse", "x": 10, "y": 20, "width": 30, "height": 40})
        self.assertEqual(shape.box, (10, 20, 40, 60))

    def test_selection_ops_flattens_in_order(self):
        ops = shapes.selection_ops([shapes.rect(0, 0, 5, 5), shapes.brush([(1, 1), (2, 2), (3, 1)], 1)])
        self.assertEqual([op["op"] for op in ops], ["rect", "polygon", "polygon"])

    def test_has_area(self):
        self.assertFalse(shapes.has_area([]))
        self.assertFalse(shapes.has_area([shapes.rect(0, 0, 5, 5, mode="subtract")]))
        self.assertTrue(shapes.has_area([shapes.rect(0, 0, 5, 5)]))


class ParsingTests(unittest.TestCase):
    def test_parse_box(self):
        self.assertEqual(shapes.parse_box("120, 80,300,200.5"), (120, 80, 420, 280.5))

    def test_parse_box_errors(self):
        for text in ("1,2,3", "1,2,3,x", "1,2,0,5", "1,2,-5,5", ""):
            with self.subTest(text=text), self.assertRaises(SelectionError):
                shapes.parse_box(text)

    def test_parse_points(self):
        self.assertEqual(shapes.parse_points("10,10 50,10; 30,40"), [(10, 10), (50, 10), (30, 40)])
        with self.assertRaises(SelectionError):
            shapes.parse_points("10,10 50,10")


class AreaTests(unittest.TestCase):
    WATERMARK = [shapes.rect(3500, 2800, 3980, 2980)]  # bottom-right corner of a 4000x3000 photo

    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "선택.json"
            area = shapes.Area([shapes.rect(1, 2, 3, 4), shapes.brush([(5, 5)], 2)], (640, 480), "anchor", (1, 0.5))
            shapes.save_area(path, area)
            loaded = shapes.load_area(path)
        self.assertEqual(loaded, area)
        self.assertEqual([s.kind for s in loaded.shapes], ["rect", "brush"])

    def test_old_files_and_bare_lists_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            path.write_text('{"version": 1, "image_size": [640, 480], "shapes": [{"type": "rect", "box": [0, 0, 10, 10]}]}',
                            encoding="utf-8")
            area = shapes.load_area(path)
            self.assertEqual((area.image_size, area.fit, area.anchor), ((640, 480), "exact", None))
            path.write_text('[{"type": "rect", "box": [0, 0, 10, 10]}]', encoding="utf-8")
            area = shapes.load_area(path)
            self.assertEqual((len(area.shapes), area.image_size), (1, None))
            for text in ("{not json", '{"shapes": 3}', '{"shapes": [], "fit": "zoom"}', '{"shapes": [], "anchor": [1]}'):
                path.write_text(text, encoding="utf-8")
                with self.subTest(text=text), self.assertRaises(SelectionError):
                    shapes.load_area(path)
            with self.assertRaises(SelectionError):
                shapes.load_area(Path(tmp) / "missing.json")

    def test_validation(self):
        with self.assertRaises(SelectionError):
            shapes.Area(self.WATERMARK, None, "anchor")  # fitting needs the reference size
        with self.assertRaises(SelectionError):
            shapes.Area(self.WATERMARK, (4000, 3000), "anchor", (2, 0))
        with self.assertRaises(SelectionError):
            shapes.Area(self.WATERMARK, (0, 3000))

    def test_auto_anchor(self):
        size = (900, 900)
        cases = [((10, 10, 100, 100), (0.0, 0.0)), ((400, 10, 500, 60), (0.5, 0.0)), ((800, 800, 890, 890), (1.0, 1.0)),
                 ((400, 400, 500, 500), (0.5, 0.5)), ((10, 850, 60, 890), (0.0, 1.0))]
        for box, anchor in cases:
            with self.subTest(box=box):
                self.assertEqual(shapes.auto_anchor([shapes.rect(*box)], size), anchor)
        # Subtracted shapes do not count; an empty selection anchors to the centre.
        mixed = [shapes.rect(800, 800, 890, 890), shapes.rect(0, 0, 50, 50, mode="subtract")]
        self.assertEqual(shapes.auto_anchor(mixed, size), (1.0, 1.0))
        self.assertEqual(shapes.auto_anchor([], size), (0.5, 0.5))
        self.assertEqual(shapes.Area(self.WATERMARK, (4000, 3000), "anchor").anchor, (1.0, 1.0))

    def test_fit_anchor_keeps_the_corner(self):
        area = shapes.Area(self.WATERMARK, (4000, 3000), "anchor")
        self.assertEqual(area.on_photo((4000, 3000))[0].box, (3500, 2800, 3980, 2980))
        self.assertEqual(area.on_photo((3000, 4000))[0].box, (2500, 3800, 2980, 3980))  # portrait
        self.assertEqual(area.on_photo((2000, 1500))[0].box, (1750, 1400, 1990, 1490))  # half size
        centre = shapes.Area([shapes.ellipse(1900, 1400, 2100, 1600)], (4000, 3000), "anchor")
        self.assertEqual(centre.anchor, (0.5, 0.5))
        self.assertEqual(centre.on_photo((3000, 4000))[0].box, (1400, 1900, 1600, 2100))

    def test_fit_stretch_and_exact(self):
        stretch = shapes.Area(self.WATERMARK, (4000, 3000), "stretch")
        self.assertEqual(stretch.on_photo((2000, 3000))[0].box, (1750, 2800, 1990, 2980))
        exact = shapes.Area(self.WATERMARK, (4000, 3000))
        self.assertEqual(exact.on_photo((2000, 1500))[0].box, (1750, 1400, 1990, 1490))
        with self.assertRaises(SelectionError):
            exact.on_photo((3000, 4000))
        # Without a reference size the shapes stay where they are.
        self.assertEqual(shapes.Area(self.WATERMARK).on_photo((100, 100))[0].box, (3500, 2800, 3980, 2980))

    def test_transformed_brush_radius(self):
        moved = shapes.brush([(10, 10)], 4).transformed(2, 2, 5, 0)
        self.assertEqual((moved.points, moved.radius), ([(25.0, 20.0)], 8.0))

    def test_describe(self):
        self.assertIn("오른쪽 아래", shapes.Area(self.WATERMARK, (4000, 3000), "anchor").describe())
        self.assertIn("비례", shapes.Area(self.WATERMARK, (4000, 3000), "stretch").describe())


@unittest.skipIf(PIL is None, "Pillow is not installed")
class RenderMaskTests(unittest.TestCase):
    def test_add_and_subtract(self):
        mask = shapes.render_mask(
            [shapes.rect(10, 10, 90, 90), shapes.ellipse(40, 40, 60, 60, mode="subtract")], (100, 100)
        )
        self.assertEqual(mask.getpixel((20, 20)), 255)
        self.assertEqual(mask.getpixel((50, 50)), 0)
        self.assertEqual(mask.getpixel((5, 5)), 0)
        self.assertEqual(mask.getpixel((95, 50)), 0)

    def test_scale_and_brush(self):
        mask = shapes.render_mask([shapes.brush([(100, 100), (300, 100)], 20)], (200, 100), scale=0.5)
        self.assertEqual(mask.getpixel((100, 50)), 255)  # middle of the stroke
        self.assertEqual(mask.getpixel((100, 70)), 0)  # outside the brush radius
        self.assertEqual(mask.getpixel((42, 50)), 255)  # round cap


if __name__ == "__main__":
    unittest.main()
