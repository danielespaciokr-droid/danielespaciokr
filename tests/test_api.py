import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ps_remover import api, shapes

BOX = shapes.Area([shapes.rect(0, 0, 5, 5)])


class DefaultOutputPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_keeps_supported_format(self):
        self.assertEqual(api.default_output_path(self.dir / "cat.JPG"), self.dir / "cat_removed.jpg")
        self.assertEqual(api.default_output_path(self.dir / "cat.psd"), self.dir / "cat_removed.psd")

    def test_unsupported_formats(self):
        self.assertEqual(api.default_output_path(self.dir / "cat.heic"), self.dir / "cat_removed.jpg")
        self.assertEqual(api.default_output_path(self.dir / "cat.jpg", "transparent"), self.dir / "cat_removed.png")
        self.assertEqual(api.default_output_path(self.dir / "cat.tif", "transparent"), self.dir / "cat_removed.tif")

    def test_never_returns_an_existing_file(self):
        (self.dir / "cat_removed.jpg").touch()
        (self.dir / "cat_removed_2.jpg").touch()
        self.assertEqual(api.default_output_path(self.dir / "cat.jpg"), self.dir / "cat_removed_3.jpg")
        self.assertEqual(api.default_output_path(self.dir / "cat.jpg", unique=False), self.dir / "cat_removed.jpg")

    def test_directory_and_suffix(self):
        out = api.default_output_path(self.dir / "cat.png", suffix="-clean", directory=self.dir / "out")
        self.assertEqual(out, self.dir / "out" / "cat-clean.png")


class ConfigTests(unittest.TestCase):
    def test_remove_config(self):
        options = api.RemoveOptions(method="transparent", expand=7, feather=1.5, subject=True, keep_open=False)
        config = api.build_remove_config("in.jpg", "out.png", shapes.Area([shapes.rect(0, 0, 10, 10)], (640, 480)), options)
        self.assertEqual(config["action"], "remove")
        self.assertTrue(os.path.isabs(config["input"]))
        self.assertTrue(config["output"].endswith("out.png"))
        self.assertEqual(config["ops"], [{"op": "rect", "mode": "add", "box": [0, 0, 10, 10]}])
        self.assertEqual(config["expectedSize"], [640, 480])
        self.assertEqual((config["fit"], config["anchor"]), ("exact", None))
        self.assertEqual(
            {k: config[k] for k in ("method", "expand", "feather", "subject", "keepOpen", "report")},
            {"method": "transparent", "expand": 7, "feather": 1.5, "subject": True, "keepOpen": False, "report": "return"},
        )

    def test_action_method_config(self):
        options = api.RemoveOptions(method="action", action_set="내 동작", action_name="지우기")
        config = api.build_remove_config("in.jpg", "out.jpg", shapes.Area([shapes.rect(0, 0, 10, 10)]), options)
        self.assertEqual((config["method"], config["actionSet"], config["actionName"]), ("action", "내 동작", "지우기"))
        current = api.build_remove_current_config(options)
        self.assertEqual((current["actionSet"], current["actionName"]), ("내 동작", "지우기"))
        # The removal keeps the photo's format, like content-aware fill.
        self.assertEqual(api.default_output_path("x/cat.jpg", "action").name, "cat_removed.jpg")

    def test_action_method_needs_names(self):
        for options in (api.RemoveOptions(method="action", action_set=" "),
                        api.RemoveOptions(method="action", action_name="")):
            with self.subTest(options=options), self.assertRaises(api.JobError):
                options.validate()
        api.RemoveOptions(method="content-aware", action_name="").validate()  # only matters for "action"

    def test_adjustment_config(self):
        area = shapes.Area([shapes.rect(0, 0, 10, 10)])
        config = api.build_remove_config("in.jpg", "out.jpg", area, api.RemoveOptions())
        self.assertEqual((config["adjustSet"], config["adjustName"]), (None, None))  # off unless asked
        options = api.RemoveOptions(adjust=True)
        config = api.build_remove_config("in.jpg", "out.jpg", area, options)
        self.assertEqual((config["adjustSet"], config["adjustName"]), ("ps-remover", "보정"))
        options = api.RemoveOptions(adjust=True, adjust_set=" 내 보정 ", adjust_name=" 필름 ")
        config = api.build_remove_config("in.jpg", "out.jpg", area, options)
        self.assertEqual((config["adjustSet"], config["adjustName"]), ("내 보정", "필름"))
        for bad in (api.RemoveOptions(adjust=True, adjust_set=""), api.RemoveOptions(adjust=True, adjust_name=" ")):
            with self.subTest(options=bad), self.assertRaises(api.JobError):
                bad.validate()
        api.RemoveOptions(adjust=False, adjust_name="").validate()  # only matters when adjusting

    def test_remove_config_needs_an_area(self):
        with self.assertRaises(api.JobError):
            api.build_remove_config("in.jpg", "out.jpg", shapes.Area([]), api.RemoveOptions())
        with self.assertRaises(api.JobError):
            api.build_remove_config("in.jpg", "out.jpg", shapes.Area([shapes.rect(0, 0, 5, 5, mode="subtract")]),
                                    api.RemoveOptions())
        # Select Subject alone is enough.
        api.build_remove_config("in.jpg", "out.jpg", shapes.Area([]), api.RemoveOptions(subject=True))

    def test_invalid_options(self):
        for options in (
            api.RemoveOptions(method="blur"),
            api.RemoveOptions(expand=-1),
            api.RemoveOptions(expand=101),
            api.RemoveOptions(feather=300),
            api.RemoveOptions(jpeg_quality=13),
        ):
            with self.subTest(options=options), self.assertRaises(api.JobError):
                api.build_remove_config("in.jpg", "out.jpg", BOX, options)

    def test_output_format_must_be_saveable(self):
        with self.assertRaises(api.JobError):
            api.build_remove_config("in.jpg", "out.gif", BOX)
        with self.assertRaises(api.JobError):
            api.build_remove_current_config(output="out.bmp")

    def test_remove_current_config(self):
        config = api.build_remove_current_config(api.RemoveOptions(expand=0))
        self.assertEqual(config["action"], "remove_current")
        self.assertIsNone(config["output"])
        self.assertEqual(config["expand"], 0)


class RemoveAreaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.photo = Path(self.tmp.name) / "photo.jpg"
        self.photo.write_bytes(b"jpeg")

    def tearDown(self):
        self.tmp.cleanup()

    def test_runs_script_with_default_output(self):
        with mock.patch.object(api, "run_jsx", return_value={"ok": True}) as run:
            api.remove_area(self.photo, BOX, photoshop="Adobe Photoshop 2025", timeout=30)
        source = run.call_args.args[0]
        self.assertIn("photo_removed.jpg", source)
        self.assertEqual(run.call_args.kwargs, {"photoshop": "Adobe Photoshop 2025", "timeout": 30})

    def test_missing_photo(self):
        with self.assertRaises(api.JobError):
            api.remove_area(self.photo.with_name("nope.jpg"), BOX)

    def test_refuses_to_overwrite_without_permission(self):
        existing = self.photo.with_name("done.jpg")
        existing.write_bytes(b"x")
        with mock.patch.object(api, "run_jsx") as run:
            with self.assertRaises(api.JobError):
                api.remove_area(self.photo, BOX, output=existing)
            with self.assertRaisesRegex(api.JobError, "원본"):
                api.remove_area(self.photo, BOX, output=self.photo)
            run.assert_not_called()
            run.return_value = {"ok": True}
            api.remove_area(self.photo, BOX, output=self.photo, overwrite=True)
            run.assert_called_once()

    def test_open_photo(self):
        with mock.patch.object(api, "run_jsx", return_value={"ok": True}) as run:
            api.open_photo(self.photo)
        self.assertRegex(run.call_args.args[0], r'var PSR_CONFIG = \{"action":"open"')

    def test_find_recorded_action(self):
        info = {"setFound": True, "found": True, "stepCount": 1, "steps": ["제거"]}
        with mock.patch.object(api, "run_jsx", return_value={"ok": True, "recordedAction": info}) as run:
            self.assertEqual(api.find_recorded_action(), info)
        self.assertIn('"action":"find_action"', run.call_args.args[0])
        self.assertIn('"actionName":"\\uc81c\\uac70"', run.call_args.args[0])  # "제거", escaped

    def test_export_script(self):
        config = api.build_remove_config(self.photo, self.photo.with_name("o.jpg"), BOX)
        path = api.export_script(Path(self.tmp.name) / "job", config)
        self.assertEqual(path.suffix, ".jsx")
        source = path.read_text(encoding="ascii")
        self.assertIsNotNone(re.search(r'"report":"alert"', source))


if __name__ == "__main__":
    unittest.main()
