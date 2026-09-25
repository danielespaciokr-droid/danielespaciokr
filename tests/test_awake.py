import ctypes
import types
import unittest
from unittest import mock

from ps_remover import awake


class KeepAwakeTests(unittest.TestCase):
    def test_windows(self):
        calls = []
        with mock.patch.object(awake.sys, "platform", "win32"), \
                mock.patch.object(awake, "_set_thread_execution_state", side_effect=lambda f: calls.append(f) or 1):
            keeper = awake.KeepAwake()
            self.assertTrue(keeper.start())
            self.assertTrue(keeper.start())  # already on: asked once
            keeper.stop()
            keeper.stop()
        self.assertEqual(calls, [0x80000001, 0x80000000])  # system required while watching, then released
        self.assertFalse(keeper.active)

    def test_windows_refusal(self):
        with mock.patch.object(awake.sys, "platform", "win32"), \
                mock.patch.object(awake, "_set_thread_execution_state", return_value=0):
            keeper = awake.KeepAwake()
            self.assertFalse(keeper.start())
            self.assertFalse(keeper.active)

    def test_macos(self):
        process = mock.Mock()
        with mock.patch.object(awake.sys, "platform", "darwin"), \
                mock.patch.object(awake.subprocess, "Popen", return_value=process) as popen, \
                mock.patch.object(awake.os, "getpid", return_value=4321):
            keeper = awake.KeepAwake()
            self.assertTrue(keeper.start())
            keeper.stop()
        self.assertEqual(popen.call_args.args[0], ["caffeinate", "-i", "-w", "4321"])  # ends with this program
        process.terminate.assert_called_once()

    def test_macos_without_caffeinate(self):
        with mock.patch.object(awake.sys, "platform", "darwin"), \
                mock.patch.object(awake.subprocess, "Popen", side_effect=FileNotFoundError("caffeinate")):
            self.assertFalse(awake.KeepAwake().start())

    def test_other_systems(self):
        with mock.patch.object(awake.sys, "platform", "linux"):
            keeper = awake.KeepAwake()
            self.assertFalse(keeper.start())
            keeper.stop()

    def test_win32_call(self):
        seen = {}

        def set_thread_execution_state(flags):
            seen["flags"] = flags
            return 0x80000000

        kernel32 = types.SimpleNamespace(SetThreadExecutionState=set_thread_execution_state)
        with mock.patch.object(ctypes, "WinDLL", lambda name: kernel32, create=True):
            self.assertEqual(awake._set_thread_execution_state(0x80000001), 0x80000000)
        self.assertEqual(seen["flags"], 0x80000001)
        # The flags do not fit a signed C int, so the argument type must be unsigned.
        self.assertEqual(set_thread_execution_state.argtypes, [ctypes.c_uint32])


if __name__ == "__main__":
    unittest.main()
