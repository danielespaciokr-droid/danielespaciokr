import os
import tempfile
import threading
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

from ps_remover import settings, shapes


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
        home = mock.patch.dict(os.environ, {"PS_REMOVER_HOME": os.path.join(self.tmp.name, "home")})
        home.start()
        self.addCleanup(home.stop)
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
        self.assertEqual((len(args[1].shapes), args[1].image_size, args[1].fit), (1, (800, 600), "exact"))
        self.assertEqual(args[2], self.photo.with_name("photo_removed.jpg"))
        self.assertEqual(kwargs, {"overwrite": False})
        self.assertIn("완료", self.app.status.get())

    def test_action_method_and_settings_dialog(self):
        app = self.app
        app.method.set("action")
        self.assertIn("[설정...]", app.status.get())
        app.open_action_settings()
        self.pump(0.1)
        self.assertTrue(app._action_dialog.winfo_exists())
        app.action_name.set("  지우기 ")
        self.drag((100, 100), (300, 200))
        with mock.patch.object(gui.api, "remove_area", return_value={"ok": True, "output": "x"}) as remove:
            app.run_remove()
            self.wait_idle()
        options = remove.call_args.args[3]
        self.assertEqual((options.method, options.action_set, options.action_name), ("action", "ps-remover", "지우기"))

    def test_check_action_reports_into_dialog(self):
        app = self.app
        app.open_action_settings()
        info = {"setFound": True, "found": True, "stepCount": 1, "steps": ["제거"]}
        with mock.patch.object(gui.api, "find_recorded_action", return_value=info) as find:
            app.check_action()
            self.wait_idle()
        self.assertEqual(find.call_args.args, ("ps-remover", "제거"))
        self.assertIn("녹화된 단계: 제거", app.action_status.get())
        self.assertEqual(app.method.get(), "action")  # a working action is selected for the user
        missing = {"setFound": False, "found": False, "stepCount": None, "steps": []}
        with mock.patch.object(gui.api, "find_recorded_action", return_value=missing):
            app.check_action()
            self.wait_idle()
        self.assertIn("세트가 없습니다", app.action_status.get())

    def test_adjustment_is_sent_and_reported(self):
        app = self.app
        self.drag((100, 100), (300, 200))
        app.adjust.set(True)
        self.assertIn("보정", app.status.get())
        result = {"ok": True, "output": "photo_removed.jpg", "warnings": [], "adjustSteps": ["Camera Raw 필터"]}
        with mock.patch.object(gui.api, "remove_area", return_value=result) as remove:
            app.run_remove()
            self.wait_idle()
        options = remove.call_args.args[3]
        self.assertEqual((options.adjust, options.adjust_set, options.adjust_name), (True, "ps-remover", "보정"))
        self.assertIn("지우고 보정까지 했습니다", app.status.get())

    def test_adjustment_settings_dialog_checks_the_action(self):
        app = self.app
        app.open_adjust_settings()
        self.pump(0.1)
        self.assertTrue(app._adjust_dialog.winfo_exists())
        app.open_adjust_settings()  # a second click brings the same dialog forward
        self.assertEqual(len([w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel)]), 1)
        info = {"setFound": True, "found": True, "stepCount": 1, "steps": ["Camera Raw 필터"]}
        with mock.patch.object(gui.api, "find_recorded_action", return_value=info) as find:
            app.check_adjust()
            self.wait_idle()
        self.assertEqual(find.call_args.args, ("ps-remover", "보정"))
        self.assertIn("녹화된 단계: Camera Raw 필터", app.adjust_status.get())
        self.assertTrue(app.adjust.get())  # a working adjustment is switched on
        self.assertEqual(app.method.get(), "content-aware")  # the removal method is left alone
        self.assertEqual(app.action_status.get(), "")

    def test_find_text_here(self):
        app = self.app
        found = gui.textfind.FoundText((600, 560, 780, 585), 18.0, 0.9)
        area = shapes.Area([shapes.rect(590, 550, 790, 595)], (800, 600))
        with mock.patch.object(gui.textfind, "place", return_value=gui.textfind.Placement(area, found, "글자 찾음")) as place:
            app.find_text_here()
            self.wait_idle()
        self.assertEqual(place.call_args.args, (self.photo, None))  # no common area chosen: just the corner
        self.assertEqual([s.box for s in app.shapes], [(590, 550, 790, 595)])
        self.assertIn("글자를 찾았습니다 (가로 180 x 세로 25 px)", app.status.get())
        # With a common area chosen, the text is looked for where that area expects it.
        settings.save_preset("글씨", shapes.Area([shapes.rect(600, 450, 700, 500)], (800, 600), "anchor"))
        app._refresh_presets(select="글씨")
        missed = gui.textfind.Placement(settings.load_preset("글씨").for_size((800, 600)), None, "글자 못 찾음")
        with mock.patch.object(gui.textfind, "place", return_value=missed) as place:
            app.find_text_here()
            self.wait_idle()
        self.assertEqual(set(place.call_args.args[1].areas), {"landscape"})
        self.assertEqual([s.box for s in app.shapes], [(600, 450, 700, 500)])  # where it was saved
        self.assertIn("글자를 찾지 못해서", app.status.get())
        with mock.patch.object(gui.textfind, "available", return_value=False), \
                mock.patch.object(gui.messagebox, "showinfo") as showinfo:
            app.find_text_here()
        self.assertIn("numpy", showinfo.call_args.args[1])

    def test_find_a_credit_box_here(self):
        app = self.app
        area = shapes.Area([shapes.rect(470, 380, 800, 450)], (800, 600), "anchor", band_box=(480, 390, 800, 440))
        settings.save_preset("게티", area)
        app._refresh_presets(select="게티")
        found = gui.textfind.FoundBox((480.0, 392.0, 800.0, 440.0), 2, 0.3)
        box = gui.textfind.Placement(shapes.Area([shapes.rect(476, 388, 804, 444)], (800, 600)), found, "상자 찾음", "box")
        with mock.patch.object(gui.textfind, "place", return_value=box):
            app.find_text_here()
            self.wait_idle()
        self.assertEqual([s.box for s in app.shapes], [(476, 388, 804, 444)])
        self.assertIn("워터마크 상자를 찾았습니다 (가로 320 x 세로 48 px)", app.status.get())
        saved = settings.load_preset("게티").for_size((800, 600))
        with mock.patch.object(gui.textfind, "place", return_value=gui.textfind.Placement(saved, None, "상자 못 찾음", "box")):
            app.find_text_here()
            self.wait_idle()
        self.assertEqual(shapes.bounds(app.shapes), (470, 380, 800, 450))  # as saved, out to the right edge
        self.assertIn("워터마크 상자를 찾지 못해서", app.status.get())

    def test_saving_an_area_on_the_edge_remembers_the_box(self):
        app = self.app
        self.drag((520, 380), (900, 450))  # past the photo's right edge
        drawn = app.shapes[0].box
        self.assertGreaterEqual(drawn[2], 800)
        with mock.patch.object(gui.textfind, "learn_band_box", return_value=(480.0, 390.0, 800.0, 440.0)) as learn, \
                mock.patch.object(gui.textfind, "learn_text_box") as learn_text:
            dialog = app.save_preset()
            dialog.name.set("게티")
            dialog.save()
        self.assertEqual(learn.call_args.args[0], self.photo)
        learn_text.assert_not_called()  # the credit's text is not followed on the edge
        saved = settings.load_preset("게티").for_orientation("landscape")
        self.assertEqual((saved.band_box, saved.text_box), ((480.0, 390.0, 800.0, 440.0), None))
        self.assertIn("워터마크 상자(가로 320 x 세로 50 px)도 기억해서", app.status.get())
        with mock.patch.object(gui.textfind, "learn_band_box", return_value=None):
            dialog = app.save_preset()
            dialog.name.set("상자 없음")
            dialog.save()
        self.assertIsNone(settings.load_preset("상자 없음").for_orientation("landscape").band_box)
        self.assertIn("사진 끝까지 지웁니다", app.status.get())
        app.open_batch_window()
        window = app._batch_window
        window.preset.set("게티")
        window._show_area_info()
        self.assertIn("상자 위치 기억함", window.area_info.get())
        window.close()

    def test_saving_a_common_area_remembers_where_the_text_is(self):
        app = self.app
        self.drag((600, 520), (760, 570))  # clear of the photo's edges
        with mock.patch.object(gui.textfind, "learn_text_box", return_value=(610.0, 550.0, 770.0, 580.0)) as learn:
            dialog = app.save_preset()
            dialog.name.set("글씨")
            dialog.save()
        self.assertEqual(learn.call_args.args[0], self.photo)
        saved = settings.load_preset("글씨").for_orientation("landscape")
        self.assertEqual(saved.text_box, (610.0, 550.0, 770.0, 580.0))
        self.assertIn("글자 위치도 기억해서", app.status.get())
        with mock.patch.object(gui.textfind, "learn_text_box", return_value=None):
            dialog = app.save_preset()
            dialog.name.set("글자 없음")
            dialog.save()
        self.assertIsNone(settings.load_preset("글자 없음").for_orientation("landscape").text_box)
        self.assertNotIn("글자 위치도", app.status.get())

    def test_removal_settings_are_remembered(self):
        app = self.app
        app.method.set("action")
        app.expand.set("9")
        app.keep_open.set(False)
        app.action_name.set("지우기")
        app.save_preferences()
        other = gui.RemoverApp(tk.Toplevel(self.root))
        self.assertEqual((other.method.get(), other.expand.get(), other.keep_open.get(), other.action_name.get()),
                         ("action", "9", False, "지우기"))
        settings.save_settings("main", {"method": "nonsense", "expand": 3})
        third = gui.RemoverApp(tk.Toplevel(self.root))
        self.assertEqual((third.method.get(), third.expand.get()), ("content-aware", "4"))  # bad values ignored

    def test_settings_are_saved_as_they_change(self):
        app = self.app
        app.adjust.set(True)
        app.adjust_name.set("필름")
        self.pump(1.2)  # saved shortly after the last change, without closing the window
        saved = settings.load_settings("main")
        self.assertEqual((saved["adjust"], saved["adjust_name"]), (True, "필름"))

    def test_save_and_apply_common_area(self):
        app = self.app
        self.drag((650, 500), (790, 590))  # bottom-right corner
        dialog = app.save_preset()
        self.pump(0.05)
        self.assertEqual(dialog.anchor.get(), "오른쪽 아래")  # detected from where the shapes are
        dialog.name.set("워터마크")
        dialog.save()
        self.assertEqual(settings.list_presets(), ["워터마크"])
        self.assertEqual(app.preset.get(), "워터마크")
        area = settings.load_preset("워터마크").areas["landscape"]
        self.assertEqual((area.image_size, area.fit, area.anchor), ((800, 600), "anchor", (1.0, 1.0)))
        box = app.shapes[0].box

        # On a portrait photo the area lands in the same corner.
        portrait = Path(self.tmp.name) / "portrait.jpg"
        Image.new("RGB", (600, 800)).save(portrait)
        app.load_photo(portrait)
        self.assertEqual(app.shapes, [])
        app.apply_preset()
        left, top, right, bottom = app.shapes[0].box
        self.assertAlmostEqual(right, 600 - (800 - box[2]), delta=0.01)
        self.assertAlmostEqual(bottom, 800 - (600 - box[3]), delta=0.01)
        self.assertIn("워터마크", app.status.get())

        with mock.patch.object(gui.messagebox, "askyesno", return_value=True):
            app.delete_preset()
        self.assertEqual((settings.list_presets(), app.preset.get()), ([], ""))

    def test_landscape_and_portrait_versions_of_a_common_area(self):
        app = self.app
        self.drag((650, 500), (790, 590))  # landscape photo: text at the bottom right
        dialog = app.save_preset()
        dialog.name.set("글씨")
        dialog.save()
        self.assertIn("세로 사진의 영역이 다르다면", app.status.get())
        portrait = Path(self.tmp.name) / "portrait.jpg"
        Image.new("RGB", (600, 800)).save(portrait)
        app.load_photo(portrait)
        self.drag((20, 700), (300, 780))  # portrait photo: text at the bottom left
        dialog = app.save_preset()
        self.assertEqual(dialog.name.get(), "글씨")  # adds to the common area in use
        dialog.save()
        area_set = settings.load_preset("글씨")
        self.assertEqual(list(area_set.areas), ["landscape", "portrait"])
        self.assertIn("모두 저장되어", app.status.get())
        # Each orientation gets its own version back.
        app.clear_shapes()
        app.apply_preset()
        self.assertEqual(app.shapes[0].box, area_set.areas["portrait"].shapes[0].box)
        app.load_photo(self.photo)
        app.apply_preset()
        self.assertEqual(app.shapes[0].box, area_set.areas["landscape"].shapes[0].box)
        # Saving the same orientation again asks before replacing it.
        dialog = app.save_preset()
        with mock.patch.object(gui.messagebox, "askyesno", return_value=False) as ask:
            dialog.save()
        self.assertIn("가로 사진용 영역을", ask.call_args.args[1])
        dialog.window.destroy()

    def test_saving_needs_a_drawn_area(self):
        with mock.patch.object(gui.messagebox, "showinfo") as showinfo:
            self.assertIsNone(self.app.save_preset())
        showinfo.assert_called_once()

    def test_open_selects_the_drawn_area(self):
        with mock.patch.object(gui.api, "open_photo", return_value={"ok": True}) as open_photo:
            self.app.run_open()
            self.wait_idle()
        self.assertIsNone(open_photo.call_args.args[1])  # nothing drawn: just open
        self.drag((100, 100), (300, 200))
        with mock.patch.object(gui.api, "open_photo", return_value={"ok": True, "selectionBounds": [1, 2, 3, 4]}) as open_photo:
            self.app.run_open()
            self.wait_idle()
        photo, area, options = open_photo.call_args.args
        self.assertEqual((photo, len(area.shapes), area.image_size, options.expand), (self.photo, 1, (800, 600), 4))
        self.assertIn("[제거]", self.app.status.get())

    def test_batch_window(self):
        folder = Path(self.tmp.name) / "사진"
        folder.mkdir()
        for name in ("a.jpg", "b.jpg"):
            path = folder / name
            Image.new("RGB", (800, 600)).save(path)
            stamp = time.time() - 60
            os.utime(path, (stamp, stamp))
        settings.save_preset("워터마크", shapes.Area([shapes.rect(0, 0, 10, 10)], (800, 600), "anchor"))
        self.app._refresh_presets()
        self.app.method.set("action")
        self.app.open_batch_window()
        window = self.app._batch_window
        self.assertEqual(window.preset.get(), "워터마크")
        self.assertEqual(window.method.get(), "action")  # starts from the main window's choice
        self.assertTrue(window.watch.get())  # watching is the usual way to use it
        window.watch.set(False)  # here: just the photos already in the folder
        window.input_dir.set(str(folder))

        def fake_remove(photo, area, output, options, **kwargs):
            output.write_bytes(b"result")
            return {"ok": True, "output": str(output), "warnings": ["참고할 점"]}

        with mock.patch.object(gui.api, "remove_area", side_effect=fake_remove) as remove:
            window.start()
            for _ in range(200):
                self.pump(0.02)
                if not window.running:
                    break
        self.assertFalse(window.running)
        self.assertEqual(remove.call_count, 2)
        self.assertEqual(remove.call_args.args[3].method, "action")
        log = window.log.get("1.0", "end")
        self.assertIn("완료  a.jpg → a_removed.jpg", log)
        self.assertIn("참고: 참고할 점", log)
        self.assertIn("2장 완료", window.status.get())
        self.assertEqual(settings.load_settings("batch")["input_dir"], str(folder))
        # A second run finds nothing new.
        with mock.patch.object(gui.api, "remove_area", side_effect=fake_remove) as remove:
            window.start()
            for _ in range(200):
                self.pump(0.02)
                if not window.running:
                    break
        remove.assert_not_called()
        self.assertIn("새로 지울 사진이 없습니다", window.status.get())
        window.close()
        self.assertFalse(window.window.winfo_exists())

    def test_closing_the_batch_window_while_running(self):
        folder = Path(self.tmp.name) / "사진"
        folder.mkdir()
        path = folder / "a.jpg"
        path.write_bytes(b"x")
        stamp = time.time() - 60
        os.utime(path, (stamp, stamp))
        settings.save_preset("워터마크", shapes.Area([shapes.rect(0, 0, 10, 10)], (800, 600), "anchor"))
        self.app._refresh_presets()
        self.app.open_batch_window()
        window = self.app._batch_window
        window.input_dir.set(str(folder))
        window.watch.set(True)
        started = threading.Event()

        def slow_remove(photo, area, output, options, **kwargs):
            started.set()
            time.sleep(0.3)
            return {"ok": True, "output": str(output), "warnings": []}

        with mock.patch.object(gui.api, "remove_area", side_effect=slow_remove), \
                mock.patch.object(gui.messagebox, "askyesno", return_value=True):
            window.start()
            self.assertTrue(started.wait(5))
            runner = window._runner
            window.close()
            self.pump(0.6)  # no Tk errors from a poll scheduled on the closed window
        self.assertFalse(window.window.winfo_exists())
        self.assertTrue(runner.stopping)

    def test_batch_window_needs_a_preset_and_folder(self):
        self.app.open_batch_window()
        window = self.app._batch_window
        with mock.patch.object(gui.messagebox, "showinfo") as showinfo:
            window.start()
        self.assertIn("공통 영역", showinfo.call_args.args[1])
        self.assertFalse(window.running)
        settings.save_preset("워터마크", shapes.Area([shapes.rect(0, 0, 10, 10)], (800, 600), "anchor"))
        window.preset.set("워터마크")
        with mock.patch.object(gui.messagebox, "showinfo") as showinfo:
            self.assertFalse(window.start(quiet=True))  # quiet: the problem goes to the window
        showinfo.assert_not_called()
        self.assertIn("폴더를 고르세요", window.status.get())

    def test_batch_window_adjustment(self):
        folder = Path(self.tmp.name) / "사진"
        folder.mkdir()
        path = folder / "a.jpg"
        Image.new("RGB", (800, 600)).save(path)
        stamp = time.time() - 60
        os.utime(path, (stamp, stamp))
        settings.save_preset("워터마크", shapes.Area([shapes.rect(0, 0, 10, 10)], (800, 600), "anchor"))
        self.app._refresh_presets()
        self.app.adjust.set(True)
        self.app.adjust_name.set("필름")
        self.app.open_batch_window()
        window = self.app._batch_window
        self.assertTrue(window.adjust.get())  # starts from the main window's choice
        self.assertIn("'ps-remover > 필름'", window.adjust_info.get())
        window.watch.set(False)
        window.input_dir.set(str(folder))

        def fake_remove(photo, area, output, options, **kwargs):
            output.write_bytes(b"result")
            return {"ok": True, "output": str(output), "warnings": [], "areaUsed": "landscape",
                    "adjustSteps": ["Camera Raw 필터"]}

        with mock.patch.object(gui.api, "remove_area", side_effect=fake_remove) as remove:
            window.start()
            for _ in range(200):
                self.pump(0.02)
                if not window.running:
                    break
        options = remove.call_args.args[3]
        self.assertEqual((options.adjust, options.adjust_name), (True, "필름"))
        log = window.log.get("1.0", "end")
        self.assertIn("지우기 + 보정)", log)
        # The credit is looked for by default (a box, the area being on the photo's edge); a plain test
        # photo has none, so the saved area was used.
        self.assertIn("완료  a.jpg → a_removed.jpg (가로 사진용 영역, 상자 못 찾음: 저장된 위치, 보정)", log)
        self.assertTrue(settings.load_settings("batch")["adjust"])
        # Everything shown in the window also goes to today's log file.
        logs = list(settings.log_dir().glob("auto-*.log"))
        self.assertEqual(len(logs), 1)
        self.assertIn("완료  a.jpg → a_removed.jpg", logs[0].read_text(encoding="utf-8"))
        window.close()

    def test_batch_window_can_leave_the_text_alone(self):
        folder = Path(self.tmp.name) / "사진"
        folder.mkdir()
        path = folder / "a.jpg"
        Image.new("RGB", (800, 600)).save(path)
        stamp = time.time() - 60
        os.utime(path, (stamp, stamp))
        area = shapes.Area([shapes.rect(700, 560, 790, 590)], (800, 600), "anchor", text_box=(705, 565, 785, 585))
        settings.save_preset("워터마크", area)
        self.app._refresh_presets()
        self.app.open_batch_window()
        window = self.app._batch_window
        self.assertTrue(window.find_text.get())  # on unless switched off
        self.assertIn("글자 위치 기억함", window.area_info.get())
        window.find_text.set(False)
        window.watch.set(False)
        window.input_dir.set(str(folder))

        def fake_remove(photo, area, output, options, **kwargs):
            output.write_bytes(b"result")
            return {"ok": True, "output": str(output), "warnings": []}

        with mock.patch.object(gui.api, "remove_area", side_effect=fake_remove) as remove, \
                mock.patch.object(gui.textfind, "place") as place:
            window.start()
            for _ in range(200):
                self.pump(0.02)
                if not window.running:
                    break
        place.assert_not_called()
        self.assertIsInstance(remove.call_args.args[1], shapes.AreaSet)  # the saved area, as it is
        self.assertIn("완료  a.jpg → a_removed.jpg\n", window.log.get("1.0", "end"))
        self.assertFalse(settings.load_settings("batch")["find_text"])
        window.close()

    def test_batch_window_picks_the_area_for_each_orientation(self):
        landscape = shapes.Area([shapes.rect(470, 380, 800, 450)], (800, 600), "anchor")
        portrait = shapes.Area([shapes.rect(250, 500, 600, 560)], (600, 800), "anchor")
        settings.save_preset("워터마크 가로형", landscape)
        settings.save_preset("워터마크 세로형", portrait)
        settings.save_preset("로고", shapes.Area([shapes.rect(10, 10, 60, 40)], (800, 600), "anchor"))
        # Chosen before landscape and portrait photos had a choice each: its partner comes along by itself.
        settings.save_settings("batch", {"preset": "워터마크 가로형", "watch": False, "find_text": False})
        self.app.open_batch_window()
        window = self.app._batch_window
        self.assertEqual((window.preset.get(), window.portrait_preset.get()), ("워터마크 가로형", "워터마크 세로형"))
        self.assertIn("가로·세로를 알아서 구분해서", window.area_info.get())
        folder = Path(self.tmp.name) / "사진"
        folder.mkdir()
        stamp = time.time() - 60
        for name, size in (("wide.jpg", (800, 600)), ("tall.jpg", (600, 800))):
            Image.new("RGB", size).save(folder / name)
            os.utime(folder / name, (stamp, stamp))
        window.input_dir.set(str(folder))

        def fake_remove(photo, area, output, options, **kwargs):
            output.write_bytes(b"result")
            used = area.for_size(Image.open(photo).size)
            return {"ok": True, "output": str(output), "warnings": [],
                    "areaUsed": shapes.orientation_of(used.image_size)}

        with mock.patch.object(gui.api, "remove_area", side_effect=fake_remove) as remove:
            window.start()
            for _ in range(200):
                self.pump(0.02)
                if not window.running:
                    break
        pair = shapes.AreaSet({"landscape": landscape, "portrait": portrait})
        self.assertEqual([c.args[1] for c in remove.call_args_list], [pair, pair])
        log = window.log.get("1.0", "end")
        self.assertIn("가로 사진 '워터마크 가로형', 세로 사진 '워터마크 세로형'", log)
        self.assertIn("tall_removed.jpg (세로 사진용 영역", log)
        self.assertIn("wide_removed.jpg (가로 사진용 영역", log)
        self.assertEqual(settings.load_settings("batch")["portrait_preset"], "워터마크 세로형")
        # Another choice for landscape photos brings its own portrait area (here: none, so it is fitted).
        window.preset.set("로고")
        self.assertEqual(window.portrait_preset.get(), "로고")
        self.assertIn("세로 사진에는 가로 사진용 영역을 맞춰서 씁니다", window.area_info.get())
        # The portrait one chosen for landscape photos: the pair is put right.
        window.preset.set("워터마크 세로형")
        self.assertEqual((window.preset.get(), window.portrait_preset.get()), ("워터마크 가로형", "워터마크 세로형"))
        # The portrait choice can be made by hand.
        window.portrait_preset.set("로고")
        self.assertEqual(window.preset.get(), "워터마크 가로형")
        self.assertIn("세로 사진에는 가로 사진용 영역을 맞춰서 씁니다", window.area_info.get())
        window.close()

    def test_apply_on_a_portrait_photo_takes_the_portrait_partner(self):
        app = self.app
        settings.save_preset("워터마크 가로형", shapes.Area([shapes.rect(470, 380, 800, 450)], (800, 600), "anchor"))
        settings.save_preset("워터마크 세로형", shapes.Area([shapes.rect(250, 500, 600, 560)], (600, 800), "anchor"))
        app._refresh_presets(select="워터마크 가로형")
        tall = Path(self.tmp.name) / "tall.jpg"
        Image.new("RGB", (600, 800), (40, 120, 200)).save(tall)
        app.load_photo(tall)
        self.pump(0.1)
        app.apply_preset()
        self.assertEqual(shapes.bounds(app.shapes), (250, 500, 600, 560))
        self.assertIn("'워터마크 세로형'의 세로 사진용 영역을 놓았습니다", app.status.get())
        with mock.patch.object(gui.textfind, "place", side_effect=lambda photo, area: gui.textfind.Placement(
                area.for_size((600, 800)), None, "상자 못 찾음", "box")) as place:
            app.find_text_here()
            self.wait_idle()
        self.assertEqual(set(place.call_args.args[1].areas), {"landscape", "portrait"})
        self.assertIn("'워터마크 세로형'", app.status.get())

    def test_watcher_restarts_itself_after_an_unexpected_error(self):
        self.app.open_batch_window()
        window = self.app._batch_window
        window._watching = True
        window._handle({"type": "finished", "done": 0, "failed": 0, "error": "예상하지 못한 오류: X",
                        "crashed": True})
        self.assertIsNotNone(window._restart_after)
        self.assertIn("60초 뒤에 다시 시작합니다", window.log.get("1.0", "end"))
        with mock.patch.object(window, "start") as start:
            window._restart()
        start.assert_called_once_with(quiet=True)
        # Stopped by the user: no restart.
        window._handle({"type": "finished", "done": 0, "failed": 0, "error": "X", "crashed": True})
        window.stop()
        self.assertIsNone(window._restart_after)
        window._user_stopped = True
        window._handle({"type": "finished", "done": 0, "failed": 0, "error": "X", "crashed": True})
        self.assertIsNone(window._restart_after)
        window.close()

    def test_watching_keeps_the_computer_awake(self):
        folder = Path(self.tmp.name) / "사진"
        folder.mkdir()
        settings.save_preset("워터마크", shapes.Area([shapes.rect(0, 0, 10, 10)], (800, 600), "anchor"))
        self.app._refresh_presets()
        self.app.open_batch_window()
        window = self.app._batch_window
        window.input_dir.set(str(folder))
        window.watch.set(True)
        with mock.patch.object(window._awake, "start", return_value=True) as awake_start, \
                mock.patch.object(window._awake, "stop") as awake_stop:
            window.start()
            self.pump(0.3)
            awake_start.assert_called_once()
            self.assertIn("절전 모드로 들어가지 않습니다", window.log.get("1.0", "end"))
            window.stop()
            for _ in range(100):
                self.pump(0.02)
                if not window.running:
                    break
            awake_stop.assert_called()
            window.close()

    def test_batch_window_says_it_will_carry_on_after_photoshop_trouble(self):
        self.app.open_batch_window()
        window = self.app._batch_window
        window._handle({"type": "error", "error": "Photoshop에 연결하지 못했습니다 (0x80080005).", "retry": True})
        log = window.log.get("1.0", "end")
        self.assertIn("0x80080005", log)
        self.assertIn("저절로 다시 이어서 처리합니다", log)
        self.assertIn("기다리는 중", window.status.get())

    def wait_idle(self):
        for _ in range(100):
            self.pump(0.05)
            if not self.app._busy:
                return
        self.fail("job did not finish")

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
        shapes.save_area(path, shapes.Area([shapes.rect(10, 10, 110, 60)], (400, 300)))  # drawn on a half-size copy
        with mock.patch.object(gui.filedialog, "askopenfilename", return_value=str(path)):
            self.app.load_selection()
        self.assertEqual(self.app.shapes[0].box, (20, 20, 220, 120))


@unittest.skipUnless(_display_available(), "no display for Tk")
class WatchWindowTests(unittest.TestCase):
    """The auto-processing window on its own (auto_windows.bat, sign-in start)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        home = mock.patch.dict(os.environ, {"PS_REMOVER_HOME": str(self.home)})
        home.start()
        self.addCleanup(home.stop)
        self.folder = Path(self.tmp.name) / "사진"
        self.folder.mkdir()
        for name, size in (("wide.jpg", (800, 600)), ("tall.jpg", (600, 800))):
            path = self.folder / name
            Image.new("RGB", size).save(path)
            stamp = time.time() - 60
            os.utime(path, (stamp, stamp))
        landscape = shapes.Area([shapes.rect(700, 550, 90, 40)], (800, 600), "anchor")
        portrait = shapes.Area([shapes.rect(500, 750, 90, 40)], (600, 800), "anchor")
        settings.save_preset("글씨", shapes.AreaSet.single(landscape).with_area(portrait))
        settings.save_settings("main", {"method": "content-aware", "action_set": "내 동작", "action_name": "지우기",
                                        "adjust_set": "내 보정", "adjust_name": "필름"})

    def run_watch(self, start=False, until=None, seconds=5.0):
        """Runs gui.watch_main until ``until(window)`` holds; returns what the window showed."""
        created = []

        class Recording(gui.BatchWindow):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                created.append(self)

        seen = {}

        def mainloop(root, n=0):
            window = created[0]
            end = time.time() + seconds
            while time.time() < end:
                root.update()
                time.sleep(0.02)
                if until is None or until(window):
                    break
            seen.update(log=window.log.get("1.0", "end"), status=window.status.get(), running=window.running,
                        title=root.title())
            with mock.patch.object(gui.messagebox, "askyesno", return_value=True):
                window.close()
            seen["closed"] = True

        def fake_remove(photo, area, output, options, **kwargs):
            seen.setdefault("calls", []).append((photo.name, area, options))
            output.write_bytes(b"result")
            used = area.for_size(Image.open(photo).size)
            which = "portrait" if used is area.areas.get("portrait") else "landscape"
            return {"ok": True, "output": str(output), "warnings": [], "areaUsed": which}

        with mock.patch.object(gui, "BatchWindow", Recording), mock.patch.object(tk.Tk, "mainloop", mainloop), \
                mock.patch.object(gui.api, "remove_area", side_effect=fake_remove):
            self.assertEqual(gui.watch_main(start=start), 0)
        self.assertTrue(seen["closed"])
        return seen

    def test_starts_by_itself_with_the_saved_settings(self):
        settings.save_settings("batch", {"preset": "글씨", "input_dir": str(self.folder), "method": "action",
                                         "expand": "6", "adjust": True, "watch": True, "interval": "1",
                                         "autostart": True})
        seen = self.run_watch(until=lambda window: "기다리는 중" in window.log.get("1.0", "end"))
        self.assertEqual(seen["title"], gui.BatchWindow.TITLE)
        self.assertTrue(seen["running"])  # still watching for more photos
        self.assertEqual(sorted(name for name, _, _ in seen["calls"]), ["tall.jpg", "wide.jpg"])
        _, area, options = seen["calls"][0]
        self.assertEqual(set(area.areas), {"landscape", "portrait"})
        self.assertEqual((options.method, options.expand, options.action_set, options.action_name),
                         ("action", 6, "내 동작", "지우기"))  # the action is the one set up in the main window
        self.assertEqual((options.adjust, options.adjust_set, options.adjust_name), (True, "내 보정", "필름"))
        self.assertIn("완료  tall.jpg → tall_removed.jpg (세로 사진용 영역, 글자 못 찾음: 저장된 위치)", seen["log"])
        self.assertIn("완료  wide.jpg → wide_removed.jpg (가로 사진용 영역, 글자 못 찾음: 저장된 위치)", seen["log"])
        self.assertEqual(sorted(p.name for p in (self.folder / "지운 사진").iterdir()),
                         ["tall_removed.jpg", "wide_removed.jpg"])

    def test_waits_for_start_when_autostart_is_off(self):
        settings.save_settings("batch", {"preset": "글씨", "input_dir": str(self.folder), "autostart": False})
        seen = self.run_watch(until=lambda window: False, seconds=1.0)
        self.assertFalse(seen["running"])
        self.assertNotIn("calls", seen)
        # Started at sign-in, it starts anyway.
        seen = self.run_watch(start=True, until=lambda window: "기다리는 중" in window.log.get("1.0", "end")
                              or "끝났습니다" in window.log.get("1.0", "end"))
        self.assertEqual(len(seen["calls"]), 2)

    def test_problems_are_shown_in_the_window(self):
        settings.save_settings("batch", {"preset": "글씨", "input_dir": str(self.folder / "없는 폴더")})
        with mock.patch.object(gui.messagebox, "showinfo") as showinfo:
            seen = self.run_watch(until=lambda window: "찾을 수 없습니다" in window.status.get())
        showinfo.assert_not_called()  # nobody may be at the computer after sign-in
        self.assertFalse(seen["running"])
        self.assertIn("사진 폴더를 찾을 수 없습니다", seen["status"])

    def test_folder_that_is_not_there_yet_is_tried_again(self):
        later = Path(self.tmp.name) / "네트워크 드라이브" / "사진"
        settings.save_settings("batch", {"preset": "글씨", "input_dir": str(later), "autostart": True})
        root = tk.Tk()
        self.addCleanup(self._destroy, root)
        window = gui.BatchWindow(root, standalone=True)
        with mock.patch.object(gui.BatchWindow, "RESTART_SECONDS", 0.2):
            window.start_unattended()
            self.assertFalse(window.running)
            self.assertIsNotNone(window._restart_after)  # tries again by itself
            self.assertIn("다시 시도합니다", window.log.get("1.0", "end"))
            later.mkdir(parents=True)  # the drive is connected now
            end = time.time() + 5
            while time.time() < end and not window.running:
                root.update()
                time.sleep(0.02)
            self.assertTrue(window.running)
            self.assertEqual(window.log.get("1.0", "end").count("시작하지 못했습니다"), 1)
            window.stop()
            end = time.time() + 5
            while time.time() < end and window.running:
                root.update()
                time.sleep(0.02)
        # Nothing chosen yet: nothing to try again, the window says what is missing.
        window.input_dir.set("")
        window.start_unattended()
        self.assertIsNone(window._restart_after)
        self.assertIn("폴더를 고르세요", window.status.get())
        # Typing a folder stops the retries of the old one.
        window.input_dir.set(str(later / "없음"))
        window.start_unattended()
        self.assertIsNotNone(window._restart_after)
        window.input_dir.set("C:/")
        self.assertIsNone(window._restart_after)
        window.close()

    def test_start_at_sign_in_option(self):
        appdata = Path(self.tmp.name) / "AppData"
        settings.save_settings("batch", {"autostart": False})
        with mock.patch.object(gui.settings.sys, "platform", "win32"), \
                mock.patch.dict(os.environ, {"APPDATA": str(appdata)}):
            root = tk.Tk()
            self.addCleanup(self._destroy, root)
            window = gui.BatchWindow(root, standalone=True)
            self.assertFalse(window.login_start.get())
            window.login_start.set(True)
            window._toggle_login_start()
            self.assertTrue(settings.starts_at_login())
            self.assertTrue(window.autostart.get())  # sign-in start has to start watching by itself
            self.assertTrue(settings.load_settings("batch")["autostart"])
            window.login_start.set(False)
            window._toggle_login_start()
            self.assertFalse(settings.starts_at_login())
            window.close()

    @staticmethod
    def _destroy(root):
        try:
            root.destroy()
        except tk.TclError:
            pass  # closed by the test already


if __name__ == "__main__":
    unittest.main()
