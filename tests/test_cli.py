import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ps_remover import cli, shapes
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
        photo, shape_list, output, options = args
        self.assertEqual(photo, self.photo)
        self.assertEqual([s.kind for s in shape_list], ["rect", "ellipse", "polygon"])
        self.assertEqual(shape_list[0].box, (100, 50, 300, 250))
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
        shapes.save_selection(selection, [shapes.rect(0, 0, 10, 10)], (640, 480))
        second = self.dir / "second.png"
        second.write_bytes(b"png")
        with mock.patch("ps_remover.api.remove_area", return_value={"ok": True, "output": "o"}) as remove:
            code, out, _ = run(["remove", str(self.photo), str(second), "--selection", str(selection),
                                "--output-dir", str(self.dir / "out"), "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(remove.call_count, 2)
        first_call = remove.call_args_list[0]
        self.assertEqual(first_call.kwargs["image_size"], (640, 480))
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

    @unittest.skipUnless(importlib.util.find_spec("tkinter"), "tkinter is not installed")
    def test_no_arguments_start_the_gui(self):
        with mock.patch("ps_remover.gui.main", return_value=0) as gui_main:
            self.assertEqual(cli.main([]), 0)
        gui_main.assert_called_once_with(None)


if __name__ == "__main__":
    unittest.main()
