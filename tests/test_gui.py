import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

try:
    import tkinter as tk
    from PIL import Image

    from ps_remover import gui
except ImportError:  # pragma: no cover - tkinter or Pillow missing
    gui = None

from ps_remover import shapes


@unittest.skipIf(gui is None, "tkinter or Pillow is not installed")
class ImageHelperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_exif_rotation_matches_photoshop(self):
        img = Image.new("RGB", (400, 200), (255, 0, 0))
        exif = Image.Exif()
        exif[0x0112] = 6  # rotate 90 degrees clockwise when displayed
        path = self.dir / "rotated.jpg"
        img.save(path, exif=exif)
        preview, size = gui.load_preview(path)
        self.assertEqual(size, (200, 400))
        self.assertEqual(preview.size, (200, 400))

    def test_large_photo_keeps_full_size(self):
        path = self.dir / "big.jpg"
        Image.new("RGB", (5000, 2500), (10, 20, 30)).save(path)
        preview, size = gui.load_preview(path, max_side=1000)
        self.assertEqual(size, (5000, 2500))
        self.assertLessEqual(max(preview.size), 1000)

    def test_16_bit_and_transparent_images(self):
        gray16 = self.dir / "gray16.png"
        Image.new("I;16", (50, 40), 65535).save(gray16)
        preview, size = gui.load_preview(gray16)
        self.assertEqual((preview.mode, size), ("RGB", (50, 40)))
        self.assertGreater(preview.getpixel((0, 0))[0], 250)

        rgba = self.dir / "alpha.png"
        Image.new("RGBA", (30, 30), (0, 0, 255, 0)).save(rgba)
        preview, _ = gui.load_preview(rgba)
        self.assertEqual(preview.mode, "RGBA")
        board = gui.fit_preview(preview, (30, 30))
        self.assertEqual(board.mode, "RGB")
        self.assertIn(board.getpixel((0, 0)), [(204, 204, 204), (245, 245, 245)])

    def test_unreadable_file_raises(self):
        path = self.dir / "broken.jpg"
        path.write_bytes(b"not an image")
        with self.assertRaises(Exception):
            gui.load_preview(path)

    def test_compose_overlay(self):
        base = Image.new("RGB", (20, 20), (0, 0, 0))
        mask = Image.new("L", (20, 20), 0)
        self.assertIs(gui.compose_overlay(base, mask), base)
        mask.paste(255, (5, 5, 15, 15))
        out = gui.compose_overlay(base, mask)
        self.assertEqual(out.getpixel((0, 0)), (0, 0, 0))
        self.assertGreater(out.getpixel((10, 10))[0], 100)  # tinted red
        self.assertEqual(out.getpixel((5, 10)), gui.EDGE)  # outline

    def test_polygon_area(self):
        self.assertEqual(gui.polygon_area([(0, 0), (10, 0), (10, 5), (0, 5)]), 50)


def _display_available():
    if gui is None:
        return False
    try:
        root = tk.Tk()
    except tk.TclError:
        return False
    root.destroy()
    return True


@unittest.skipUnless(_display_available(), "no display for Tk")
class AppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.photo = Path(self.tmp.name) / "photo.jpg"
        Image.new("RGB", (800, 600), (40, 120, 200)).save(self.photo)
        self.root = tk.Tk()
        self.root.geometry("1000x700+0+0")
        self.app = gui.RemoverApp(self.root)
        self.app.load_photo(self.photo)
        self.pump()

    def tearDown(self):
        self.root.destroy()
        self.tmp.cleanup()

    def pump(self, seconds=0.2):
        end = time.time() + seconds
        while time.time() < end:
            self.root.update()
            time.sleep(0.01)

    def drag(self, *image_points):
        canvas = self.app.canvas
        points = [(int(self.app.offset[0] + x * self.app.scale), int(self.app.offset[1] + y * self.app.scale))
                  for x, y in image_points]
        canvas.event_generate("<ButtonPress-1>", x=points[0][0], y=points[0][1])
        for x, y in points[1:]:
            canvas.event_generate("<B1-Motion>", x=x, y=y)
        canvas.event_generate("<ButtonRelease-1>", x=points[-1][0], y=points[-1][1])
        self.pump(0.05)

    def test_drawing_tools_create_shapes_in_image_coordinates(self):
        app = self.app
        self.drag((100, 100), (300, 200))
        app.tool.set("ellipse")
        app.mode.set("subtract")
        self.drag((150, 120), (200, 160))
        app.mode.set("add")
        app.tool.set("lasso")
        self.drag((500, 300), (600, 300), (600, 400), (500, 400))
        app.tool.set("brush")
        self.drag((100, 500), (400, 500))
        self.assertEqual([(s.kind, s.mode) for s in app.shapes],
                         [("rect", "add"), ("ellipse", "subtract"), ("polygon", "add"), ("brush", "add")])
        left, top, right, bottom = app.shapes[0].box
        tolerance = 2 / app.scale
        for got, want in ((left, 100), (top, 100), (right, 300), (bottom, 200)):
            self.assertAlmostEqual(got, want, delta=tolerance)
        self.assertAlmostEqual(app.shapes[3].radius, 20 / app.scale, delta=0.01)  # 40 px brush on screen

        app.undo()
        self.assertEqual(len(app.shapes), 3)
        app.clear_shapes()
        self.assertEqual(app.shapes, [])

    def test_tiny_drags_are_ignored(self):
        self.drag((100, 100), (100.5, 100.5))
        self.assertEqual(self.app.shapes, [])

    def test_run_remove_sends_the_selection(self):
        self.drag((100, 100), (300, 200))
        result = {"ok": True, "output": "x", "warnings": []}
        with mock.patch.object(gui.api, "remove_area", return_value=result) as remove:
            self.app.run_remove()
            for _ in range(100):
                self.pump(0.05)
                if not self.app._busy:
                    break
        args, kwargs = remove.call_args
        self.assertEqual(args[0], self.photo)
        self.assertEqual(len(args[1]), 1)
        self.assertEqual(args[2], self.photo.with_name("photo_removed.jpg"))
        self.assertEqual(kwargs, {"image_size": (800, 600), "overwrite": False})
        self.assertIn("완료", self.app.status.get())

    def test_output_follows_method_until_edited(self):
        app = self.app
        self.assertTrue(app.output.get().endswith("photo_removed.jpg"))
        app.method.set("transparent")
        self.assertTrue(app.output.get().endswith("photo_removed.png"))
        app.output.set(os.path.join(self.tmp.name, "mine.jpg"))
        app.method.set("content-aware")
        self.assertTrue(app.output.get().endswith("mine.jpg"))

    def test_errors_are_shown(self):
        self.drag((100, 100), (300, 200))
        failure = gui.ScriptFailed("선택 영역이 비어 있습니다.", {"ok": False, "leftOpen": True, "warnings": []})
        with mock.patch.object(gui.api, "remove_area", side_effect=failure), \
                mock.patch.object(gui.messagebox, "showerror") as showerror:
            self.app.run_remove()
            for _ in range(100):
                self.pump(0.05)
                if not self.app._busy:
                    break
        message = showerror.call_args.args[1]
        self.assertIn("선택 영역이 비어", message)
        self.assertIn("열어 두었습니다", message)

    def test_selection_file_round_trip_with_scaling(self):
        path = Path(self.tmp.name) / "sel.json"
        shapes.save_selection(path, [shapes.rect(10, 10, 110, 60)], (400, 300))  # drawn on a half-size copy
        with mock.patch.object(gui.filedialog, "askopenfilename", return_value=str(path)):
            self.app.load_selection()
        self.assertEqual(self.app.shapes[0].box, (20, 20, 220, 120))


if __name__ == "__main__":
    unittest.main()
