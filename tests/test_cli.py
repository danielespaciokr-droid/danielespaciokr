import contextlib
import importlib.util
import io
import json
import os
import time
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ps_remover import cli, settings, shapes
from ps_remover.photoshop import PhotoshopNotFound, ScriptFailed


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.photo = self.dir / "photo.jpg"
        self.photo.write_bytes(b"jpeg")
        home = mock.patch.dict(os.environ, {"PS_REMOVER_HOME": str(self.dir / "home")})
        home.start()
        self.addCleanup(home.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_remove_passes_shapes_and_options(self):
        result = {"ok": True, "output": "x_removed.jpg", "selectionBounds": [96, 46, 304, 254], "warnings": ["w"]}
        with mock.patch("ps_remover.api.remove_area", return_value=result) as remove:
            code, out, err = run([
                "remove", str(self.photo), "--rect", "100,50,200,200", "--ellipse", "0,0,10,20",
                "--polygon", "1,1 9,1 5,8", "--method", "transparent", "--expand", "8", "--feather", "1",
            ])
        self.assertEqual(code, 0, err)
        args, kwargs = remove.call_args
        photo, area, output, options = args
        self.assertEqual(photo, self.photo)
        self.assertEqual([s.kind for s in area.shapes], ["rect", "ellipse", "polygon"])
        self.assertEqual(area.shapes[0].box, (100, 50, 300, 250))
        self.assertIsNone(area.image_size)
        self.assertEqual(output, self.dir / "photo_removed.png")  # transparent -> png
        self.assertEqual((options.method, options.expand, options.feather, options.keep_open),
                         ("transparent", 8, 1.0, True))
        self.assertIn("208x208", out)
        self.assertIn("참고: w", out)

    def test_action_method(self):
        with mock.patch("ps_remover.api.remove_area", return_value={"ok": True, "output": "o"}) as remove:
            code, _, err = run(["remove", str(self.photo), "--rect", "0,0,5,5", "--method", "action"])
        self.assertEqual(code, 0, err)
        options = remove.call_args.args[3]
        self.assertEqual((options.method, options.action_set, options.action_name), ("action", "ps-remover", "제거"))
        with mock.patch("ps_remover.api.remove_current_selection", return_value={"ok": True}) as current:
            run(["remove-selection", "--method", "action", "--action-set", "내 세트", "--action", "지우기"])
        options = current.call_args.args[0]
        self.assertEqual((options.action_set, options.action_name), ("내 세트", "지우기"))

    def test_check_action(self):
        found = {"setFound": True, "found": True, "stepCount": 1, "steps": ["제거"]}
        with mock.patch("ps_remover.api.find_recorded_action", return_value=found) as find:
            code, out, _ = run(["check-action"])
        self.assertEqual(code, 0)
        self.assertEqual(find.call_args.args, ("ps-remover", "제거"))
        self.assertIn("녹화된 단계: 제거", out)
        missing = {"setFound": False, "found": False, "stepCount": None, "steps": []}
        for info, message in (
            (missing, "세트가 없습니다"),
            (dict(missing, setFound=True), "동작이 없습니다"),
            (dict(missing, setFound=True, found=True, stepCount=0), "단계가 없습니다"),
        ):
            with self.subTest(message=message), \
                    mock.patch("ps_remover.api.find_recorded_action", return_value=info):
                code, out, _ = run(["check-action"])
            self.assertEqual(code, 1)
            self.assertIn(message, out)
            self.assertIn("[기록]", out)  # setup instructions
        with mock.patch("ps_remover.api.find_recorded_action", return_value=missing):
            code, out, _ = run(["check-action", "--json"])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out)["ok"])

    def test_selection_file_and_batch(self):
        selection = self.dir / "sel.json"
        shapes.save_area(selection, shapes.Area([shapes.rect(0, 0, 10, 10)], (640, 480)))
        second = self.dir / "second.png"
        second.write_bytes(b"png")
        with mock.patch("ps_remover.api.remove_area", return_value={"ok": True, "output": "o"}) as remove:
            code, out, _ = run(["remove", str(self.photo), str(second), "--selection", str(selection),
                                "--output-dir", str(self.dir / "out"), "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(remove.call_count, 2)
        first_call = remove.call_args_list[0]
        self.assertEqual(first_call.args[1].areas["landscape"].image_size, (640, 480))
        self.assertFalse(first_call.args[3].keep_open)  # several photos: do not leave them all open
        self.assertEqual(remove.call_args_list[1].args[2], self.dir / "out" / "second_removed.png")
        lines = [json.loads(line) for line in out.splitlines()]
        self.assertEqual([line["input"] for line in lines], [str(self.photo), str(second)])

    def test_wildcards_are_expanded(self):
        (self.dir / "b.jpg").write_bytes(b"jpeg")
        with mock.patch("ps_remover.api.remove_area", return_value={"ok": True, "output": "o"}) as remove:
            code, _, _ = run(["remove", str(self.dir / "*.jpg"), "--rect", "0,0,5,5"])
        self.assertEqual(code, 0)
        self.assertEqual([c.args[0].name for c in remove.call_args_list], ["b.jpg", "photo.jpg"])
        code, _, err = run(["remove", str(self.dir / "*.gif"), "--rect", "0,0,5,5"])
        self.assertEqual(code, 1)
        self.assertIn("맞는 파일이 없습니다", err)

    def test_needs_an_area(self):
        code, _, err = run(["remove", str(self.photo)])
        self.assertEqual(code, 1)
        self.assertIn("--rect", err)

    def test_bad_coordinates(self):
        code, _, err = run(["remove", str(self.photo), "--rect", "1,2,3"])
        self.assertEqual(code, 1)
        self.assertIn("오류", err)

    def test_output_only_for_one_photo(self):
        code, _, err = run(["remove", str(self.photo), str(self.photo), "--rect", "0,0,5,5", "-o", "x.jpg"])
        self.assertEqual(code, 1)
        self.assertIn("--output-dir", err)

    def test_script_failure_continues_with_next_photo(self):
        failure = ScriptFailed("선택 영역이 비어 있습니다.", {"ok": False, "leftOpen": True})
        with mock.patch("ps_remover.api.remove_area", side_effect=[failure, {"ok": True, "output": "o"}]) as remove:
            code, out, err = run(["remove", str(self.photo), str(self.photo), "--rect", "0,0,5,5"])
        self.assertEqual(code, 1)
        self.assertEqual(remove.call_count, 2)
        self.assertIn("선택 영역이 비어", err)
        self.assertIn("열어 두었습니다", err)
        self.assertIn("완료", out)

    def test_missing_photoshop_stops_the_batch(self):
        with mock.patch("ps_remover.api.remove_area", side_effect=PhotoshopNotFound("없음")) as remove:
            code, _, err = run(["remove", str(self.photo), str(self.photo), "--rect", "0,0,5,5"])
        self.assertEqual(code, 1)
        self.assertEqual(remove.call_count, 1)
        self.assertIn("1장은 처리하지 않았습니다", err)

    def test_overwrite_reuses_default_name(self):
        (self.dir / "photo_removed.jpg").write_bytes(b"old")
        with mock.patch("ps_remover.api.remove_area", return_value={"ok": True, "output": "o"}) as remove:
            run(["remove", str(self.photo), "--rect", "0,0,5,5", "--overwrite"])
        self.assertEqual(remove.call_args.args[2], self.dir / "photo_removed.jpg")
        self.assertTrue(remove.call_args.kwargs["overwrite"])

    def test_export_jsx(self):
        target = self.dir / "job.jsx"
        with mock.patch("ps_remover.photoshop.run_jsx") as run_jsx:
            code, out, _ = run(["remove", str(self.photo), "--rect", "0,0,5,5", "--export-jsx", str(target)])
        self.assertEqual(code, 0)
        run_jsx.assert_not_called()
        self.assertIn('"report":"alert"', target.read_text(encoding="ascii"))
        self.assertIn(str(target), out)

    def test_open(self):
        with mock.patch("ps_remover.api.open_photo", return_value={"ok": True}) as open_photo:
            code, _, _ = run(["open", str(self.photo), "--photoshop", "Adobe Photoshop 2025"])
        self.assertEqual(code, 0)
        self.assertEqual(open_photo.call_args.kwargs["photoshop"], "Adobe Photoshop 2025")

    def test_remove_selection(self):
        with mock.patch("ps_remover.api.remove_current_selection",
                        return_value={"ok": True, "output": "saved.png"}) as remove:
            code, out, _ = run(["remove-selection", "--expand", "0", "-o", "saved.png"])
        self.assertEqual(code, 0)
        options, output = remove.call_args.args
        self.assertEqual(options.expand, 0)
        self.assertEqual(output, "saved.png")
        self.assertIn("saved.png", out)

    def test_presets_command(self):
        code, out, _ = run(["presets"])
        self.assertEqual(code, 0)
        self.assertIn("저장된 공통 영역이 없습니다", out)
        settings.save_preset("워터마크", shapes.Area([shapes.rect(3500, 2800, 3980, 2980)], (4000, 3000), "anchor"))
        code, out, _ = run(["presets"])
        self.assertIn("- 워터마크\n    가로 사진용: 도형 1개, 4000x3000 사진 기준, 오른쪽 아래 기준으로 맞춤", out)
        self.assertIn("세로 사진에도 위 영역을 맞춰서 씁니다", out)
        portrait = shapes.Area([shapes.rect(0, 3800, 900, 3990)], (3000, 4000), "anchor")
        settings.save_preset("워터마크", settings.load_preset("워터마크").with_area(portrait))
        code, out, _ = run(["presets"])
        self.assertIn("    세로 사진용: 도형 1개, 3000x4000 사진 기준, 왼쪽 아래 기준으로 맞춤", out)
        code, out, _ = run(["presets", "--json"])
        self.assertEqual(json.loads(out)[0]["name"], "워터마크")
        code, _, err = run(["presets", "--delete", "없는이름"])
        self.assertEqual(code, 1)
        self.assertIn("워터마크", err)  # lists what exists
        code, out, _ = run(["presets", "--delete", "워터마크"])
        self.assertEqual((code, settings.list_presets()), (0, []))

    def test_remove_and_open_with_preset(self):
        area = shapes.Area([shapes.rect(3500, 2800, 3980, 2980)], (4000, 3000), "anchor")
        settings.save_preset("워터마크", area)
        with mock.patch("ps_remover.api.remove_area", return_value={"ok": True, "output": "o"}) as remove:
            code, _, err = run(["remove", str(self.photo), "--preset", "워터마크"])
        self.assertEqual(code, 0, err)
        self.assertEqual(remove.call_args.args[1], shapes.AreaSet.single(area))
        with mock.patch("ps_remover.api.open_photo", return_value={"ok": True, "selectionBounds": [1, 2, 3, 4]}) as open_:
            code, out, _ = run(["open", str(self.photo), "--preset", "워터마크", "--expand", "0"])
        self.assertEqual(code, 0)
        photo, opened_area, options = open_.call_args.args
        self.assertEqual((opened_area, options.expand), (shapes.AreaSet.single(area), 0))
        self.assertIn("선택해 두었습니다", out)
        with mock.patch("ps_remover.api.open_photo", return_value={"ok": True}) as open_:
            run(["open", str(self.photo)])
        self.assertIsNone(open_.call_args.args[1])  # nothing to select
        code, _, err = run(["remove", str(self.photo), "--preset", "워터마크", "--rect", "0,0,5,5"])
        self.assertEqual(code, 1)
        self.assertIn("더할 수는 없습니다", err)
        code, _, err = run(["remove", str(self.photo), "--preset", "없음"])
        self.assertEqual(code, 1)

    def test_batch_command(self):
        folder = self.dir / "사진"
        folder.mkdir()
        for name in ("a.jpg", "b.jpg"):
            path = folder / name
            path.write_bytes(b"jpeg")
            stamp = time.time() - 60
            os.utime(path, (stamp, stamp))
        settings.save_preset("워터마크", shapes.Area([shapes.rect(0, 0, 10, 10)], (100, 100), "anchor"))

        def fake_remove(photo, area, output, options, **kwargs):
            output.write_bytes(b"result")
            return {"ok": True, "output": str(output), "warnings": []}

        with mock.patch("ps_remover.api.remove_area", side_effect=fake_remove) as remove:
            code, out, err = run(["batch", str(folder), "--preset", "워터마크", "--method", "action", "--adjust"])
            self.assertEqual(code, 0, err)
            self.assertIn("[2/2] b.jpg", out)
            self.assertIn("끝났습니다: 2장 완료, 0장 실패", out)
            options = remove.call_args.args[3]
            self.assertEqual((options.method, options.adjust, options.adjust_set, options.adjust_name),
                             ("action", True, "ps-remover", "보정"))
            self.assertTrue((folder / "지운 사진" / "b_removed.jpg").exists())
            code, out, _ = run(["batch", str(folder), "--preset", "워터마크"])
            self.assertIn("새로 지울 사진이 없습니다", out)
            code, out, _ = run(["batch", str(folder), "--preset", "워터마크", "--reprocess", "--json"])
            events = [json.loads(line) for line in out.splitlines()]
            self.assertEqual([e["type"] for e in events].count("done"), 2)
            self.assertEqual(events[-1]["type"], "finished")

    def test_adjustment_options(self):
        with mock.patch("ps_remover.api.remove_area", return_value={"ok": True, "output": "x"}) as remove:
            code, _, err = run(["remove", str(self.photo), "--rect", "0,0,5,5", "--adjust",
                                "--adjust-set", "내 보정", "--adjust-action", "필름"])
            self.assertEqual(code, 0, err)
            options = remove.call_args.args[3]
            self.assertEqual((options.adjust, options.adjust_set, options.adjust_name), (True, "내 보정", "필름"))
            run(["remove", str(self.photo), "--rect", "0,0,5,5"])
            self.assertFalse(remove.call_args.args[3].adjust)  # off unless asked
        code, _, err = run(["remove", str(self.photo), "--rect", "0,0,5,5", "--adjust", "--adjust-action", " "])
        self.assertEqual(code, 1)
        self.assertIn("보정에 쓸 Photoshop 동작", err)
        with self.assertRaises(SystemExit):  # removing the current selection does not adjust
            run(["remove-selection", "--adjust"])

    def test_batch_errors(self):
        code, _, err = run(["batch", str(self.dir / "없는폴더"), "--rect", "0,0,5,5"])
        self.assertEqual(code, 1)
        self.assertIn("사진 폴더를 찾을 수 없습니다", err)
        stamp = time.time() - 60
        os.utime(self.photo, (stamp, stamp))
        with mock.patch("ps_remover.api.remove_area", side_effect=PhotoshopNotFound("Photoshop 없음")):
            code, out, err = run(["batch", str(self.dir), "--rect", "0,0,5,5"])
        self.assertEqual(code, 1)
        self.assertIn("Photoshop 없음", err)
        self.assertIn("멈췄습니다: 0장 완료, 0장 실패", out)

    @unittest.skipUnless(importlib.util.find_spec("tkinter"), "tkinter is not installed")
    def test_no_arguments_start_the_gui(self):
        with mock.patch("ps_remover.gui.main", return_value=0) as gui_main:
            self.assertEqual(cli.main([]), 0)
        gui_main.assert_called_once_with(None)

    @unittest.skipUnless(importlib.util.find_spec("tkinter"), "tkinter is not installed")
    def test_watch_opens_the_auto_processing_window(self):
        with mock.patch("ps_remover.gui.watch_main", return_value=0) as watch_main:
            self.assertEqual(cli.main(["watch"]), 0)
            self.assertEqual(cli.main(["watch", "--start"]), 0)
        self.assertEqual(watch_main.call_args_list, [mock.call(start=False), mock.call(start=True)])


if __name__ == "__main__":
    unittest.main()
