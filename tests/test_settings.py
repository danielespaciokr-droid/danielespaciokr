import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ps_remover import settings, shapes
from ps_remover.shapes import SelectionError

AREA = shapes.Area([shapes.rect(3500, 2800, 3980, 2980)], (4000, 3000), "anchor")


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.dict(os.environ, {"PS_REMOVER_HOME": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_home_override(self):
        self.assertEqual(settings.config_dir(), Path(self.tmp.name))
        self.assertEqual(settings.presets_dir(), Path(self.tmp.name) / "presets")

    def test_default_locations(self):
        env = {k: v for k, v in os.environ.items() if k != "PS_REMOVER_HOME"}
        cases = [
            ("win32", {"APPDATA": "C:/Users/me/AppData/Roaming"}, Path("C:/Users/me/AppData/Roaming") / "ps-remover"),
            ("darwin", {}, Path.home() / "Library" / "Application Support" / "ps-remover"),
            ("linux", {"XDG_CONFIG_HOME": "/cfg"}, Path("/cfg") / "ps-remover"),
        ]
        for platform, extra, expected in cases:
            with self.subTest(platform=platform), mock.patch.dict(os.environ, dict(env, **extra), clear=True), \
                    mock.patch.object(settings.sys, "platform", platform):
                self.assertEqual(settings.config_dir(), expected)

    def test_preset_round_trip(self):
        self.assertEqual(settings.list_presets(), [])
        path = settings.save_preset(" 워터마크 ", AREA)
        self.assertEqual(path.name, "워터마크.json")
        settings.save_preset("logo", AREA)
        self.assertEqual(settings.list_presets(), ["logo", "워터마크"])
        self.assertEqual(settings.load_preset("워터마크"), AREA)
        settings.delete_preset("워터마크")
        self.assertEqual(settings.list_presets(), ["logo"])
        with self.assertRaisesRegex(SelectionError, "logo"):  # names the presets that do exist
            settings.load_preset("워터마크")

    def test_bad_names(self):
        for name in ("", "   ", "a/b", "a\\b", "what?", ".hidden", "x" * 61):
            with self.subTest(name=name), self.assertRaises(SelectionError):
                settings.save_preset(name, AREA)

    def test_settings_sections(self):
        self.assertEqual(settings.load_settings("batch"), {})
        settings.save_settings("batch", {"input_dir": "C:/사진", "watch": True})
        settings.save_settings("other", {"x": 1})
        self.assertEqual(settings.load_settings("batch"), {"input_dir": "C:/사진", "watch": True})
        (Path(self.tmp.name) / "settings.json").write_text("{broken", encoding="utf-8")
        self.assertEqual(settings.load_settings("batch"), {})
        settings.save_settings("batch", {"a": 1})  # recovers from a broken file
        self.assertEqual(settings.load_settings("batch"), {"a": 1})


if __name__ == "__main__":
    unittest.main()
