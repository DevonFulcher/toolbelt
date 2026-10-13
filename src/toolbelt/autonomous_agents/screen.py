"""Whether someone is at the laptop: the screen is on and unlocked.

Autonomous agents run only then. Read through CoreGraphics with ``ctypes`` (no
extra dependency): the main display's sleep state, and the login session's
screen-lock flag.
"""

import ctypes
import ctypes.util
from dataclasses import dataclass

_UTF8 = 0x08000100


@dataclass(frozen=True)
class ScreenState:
    #: The main display is on.
    awake: bool
    #: The login session's screen is locked.
    locked: bool

    @property
    def in_use(self) -> bool:
        return self.awake and not self.locked


def read_screen_state() -> ScreenState:
    core_graphics = ctypes.CDLL(ctypes.util.find_library("CoreGraphics"))
    core_foundation = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))

    core_graphics.CGMainDisplayID.restype = ctypes.c_uint32
    core_graphics.CGDisplayIsAsleep.argtypes = [ctypes.c_uint32]
    core_graphics.CGDisplayIsAsleep.restype = ctypes.c_int
    asleep = core_graphics.CGDisplayIsAsleep(core_graphics.CGMainDisplayID())

    core_graphics.CGSessionCopyCurrentDictionary.restype = ctypes.c_void_p
    core_foundation.CFStringCreateWithCString.restype = ctypes.c_void_p
    core_foundation.CFStringCreateWithCString.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_uint32,
    ]
    core_foundation.CFDictionaryGetValue.restype = ctypes.c_void_p
    core_foundation.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    core_foundation.CFBooleanGetValue.restype = ctypes.c_bool
    core_foundation.CFBooleanGetValue.argtypes = [ctypes.c_void_p]
    session = core_graphics.CGSessionCopyCurrentDictionary()
    locked = False
    if session:
        key = core_foundation.CFStringCreateWithCString(
            None, b"CGSSessionScreenIsLocked", _UTF8
        )
        value = core_foundation.CFDictionaryGetValue(session, key)
        # The key is absent while the screen is unlocked.
        locked = bool(value) and core_foundation.CFBooleanGetValue(value)
    return ScreenState(awake=not asleep, locked=locked)
