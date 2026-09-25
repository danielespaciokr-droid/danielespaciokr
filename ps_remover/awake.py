"""Keep the computer from going to sleep while a folder is being watched.

A sleeping computer does not notice new photos. Only idle sleep is held off:
the screen can still turn off, and the user can still put the computer to
sleep on purpose.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


class KeepAwake:
    """Idle sleep is held off between :meth:`start` and :meth:`stop`.

    On Windows the request belongs to the calling thread, so both must be
    called from the same long-lived thread (the GUI thread).
    """

    def __init__(self) -> None:
        self.active = False
        self._caffeinate: Optional[subprocess.Popen] = None

    def start(self) -> bool:
        """Returns whether sleep is now held off (not supported everywhere)."""
        if self.active:
            return True
        try:
            if sys.platform == "win32":
                if not _set_thread_execution_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED):
                    return False
            elif sys.platform == "darwin":
                # caffeinate ends by itself when this program ends (-w).
                self._caffeinate = subprocess.Popen(
                    ["caffeinate", "-i", "-w", str(os.getpid())],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            else:
                return False
        except (OSError, AttributeError, ValueError):
            return False
        self.active = True
        return True

    def stop(self) -> None:
        if not self.active:
            return
        self.active = False
        try:
            if sys.platform == "win32":
                _set_thread_execution_state(ES_CONTINUOUS)
            elif self._caffeinate is not None:
                self._caffeinate.terminate()
        except (OSError, AttributeError, ValueError):
            pass
        self._caffeinate = None


def _set_thread_execution_state(flags: int) -> int:
    import ctypes

    function = ctypes.WinDLL("kernel32").SetThreadExecutionState
    function.argtypes = [ctypes.c_uint32]  # the flags do not fit a C int
    function.restype = ctypes.c_uint32
    return function(flags)
