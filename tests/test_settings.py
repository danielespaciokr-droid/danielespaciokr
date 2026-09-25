import os
import tempfile
import time
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
        self.assertEqual(settings.load_preset("워터마크"), shapes.AreaSet.single(AREA))
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

    def test_watcher_log(self):
        day = time.mktime((2026, 9, 25, 13, 5, 9, 0, 0, -1))
        settings.append_log("완료  a.jpg → a_removed.jpg", now=day)
        settings.append_log("여러 줄\n오류", now=day + 1)
        path = settings.log_dir() / "auto-2026-09-25.log"
        self.assertEqual(path.read_text(encoding="utf-8"),
                         "[2026-09-25 13:05:09] 완료  a.jpg → a_removed.jpg\n[2026-09-25 13:05:10] 여러 줄\n오류\n")
        old = settings.log_dir() / "auto-2026-08-01.log"
        old.write_text("x", encoding="utf-8")
        os.utime(old, (day - 40 * 86400, day - 40 * 86400))
        os.utime(path, (day, day))
        settings.prune_logs(now=day)
        self.assertEqual([p.name for p in settings.log_dir().iterdir()], ["auto-2026-09-25.log"])

    def test_start_at_login_is_windows_only(self):
        with mock.patch.object(settings.sys, "platform", "linux"):
            self.assertIsNone(settings.startup_script_path())
            self.assertFalse(settings.starts_at_login())
            with self.assertRaises(OSError):
                settings.set_start_at_login(True)

    def test_start_at_login(self):
        appdata = Path(self.tmp.name) / "AppData"
        with mock.patch.object(settings.sys, "platform", "win32"), \
                mock.patch.dict(os.environ, {"APPDATA": str(appdata)}):
            path = settings.startup_script_path()
            self.assertEqual(path.parent, appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup")
            self.assertFalse(settings.starts_at_login())
            settings.set_start_at_login(True)
            self.assertTrue(settings.starts_at_login())
            raw = path.read_bytes()
            self.assertIn(b"\r\n", raw)  # a batch file needs Windows line endings
            text = raw.decode("utf-8")
            self.assertIn("chcp 65001", text)  # the project folder may have a Korean name
            self.assertIn(f'cd /d "{Path(settings.__file__).resolve().parent.parent}"', text)
            self.assertIn("-m ps_remover watch --start", text)
            settings.set_start_at_login(True)  # again: still one file
            settings.set_start_at_login(False)
            self.assertFalse(path.exists())
            settings.set_start_at_login(False)  # nothing to remove


if __name__ == "__main__":
    unittest.main()
