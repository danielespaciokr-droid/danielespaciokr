import sys
import unittest
from unittest import mock

from ps_remover import launcher


def fake_find_spec(available):
    return lambda name, *args: object() if name in available else None


class LauncherTests(unittest.TestCase):
    def test_missing_packages(self):
        with mock.patch.object(launcher.importlib.util, "find_spec", fake_find_spec({"PIL"})), \
                mock.patch.object(launcher.sys, "platform", "win32"):
            self.assertEqual(launcher.missing_packages(), ["pywin32"])
        with mock.patch.object(launcher.importlib.util, "find_spec", fake_find_spec(set())), \
                mock.patch.object(launcher.sys, "platform", "darwin"):
            self.assertEqual(launcher.missing_packages(), ["Pillow"])

    def test_install_command(self):
        with mock.patch.object(launcher.sys, "prefix", "/base"), mock.patch.object(launcher.sys, "base_prefix", "/base"):
            self.assertEqual(launcher.install_command(["Pillow"])[-2:], ["--user", "Pillow"])
        with mock.patch.object(launcher.sys, "prefix", "/venv"), mock.patch.object(launcher.sys, "base_prefix", "/base"):
            self.assertNotIn("--user", launcher.install_command(["Pillow"]))  # pip refuses --user in a venv
        self.assertEqual(launcher.install_command(["Pillow"])[:3], [sys.executable, "-m", "pip"])

    def test_starts_the_program_when_everything_is_there(self):
        with mock.patch.object(launcher, "missing_packages", return_value=[]), \
                mock.patch("ps_remover.cli.main", return_value=0) as cli_main:
            self.assertEqual(launcher.main(["gui"]), 0)
        cli_main.assert_called_once_with(["gui"])

    def test_installs_then_starts_in_a_new_interpreter(self):
        with mock.patch.object(launcher, "missing_packages", return_value=["Pillow", "pywin32"]), \
                mock.patch.object(launcher.subprocess, "call", side_effect=[0, 0]) as call, \
                mock.patch("ps_remover.cli.main") as cli_main:
            self.assertEqual(launcher.main([]), 0)
        install, start = (c.args[0] for c in call.call_args_list)
        self.assertEqual(install[-2:], ["Pillow", "pywin32"])
        self.assertEqual(start, [sys.executable, "-m", "ps_remover"])
        cli_main.assert_not_called()

    def test_install_failure_is_explained(self):
        with mock.patch.object(launcher, "missing_packages", return_value=["Pillow"]), \
                mock.patch.object(launcher.subprocess, "call", return_value=1), \
                mock.patch("sys.stderr") as stderr:
            self.assertEqual(launcher.main([]), 1)
        self.assertIn("인터넷", "".join(c.args[0] for c in stderr.write.call_args_list))

    def test_missing_tkinter_is_explained(self):
        with mock.patch.object(launcher.importlib.util, "find_spec", fake_find_spec({"PIL"})), \
                mock.patch("sys.stderr") as stderr:
            self.assertEqual(launcher.main([]), 1)
        self.assertIn("python.org", "".join(c.args[0] for c in stderr.write.call_args_list))


if __name__ == "__main__":
    unittest.main()
