"""X11 through ctypes: the desktop geometry and the injector that receiver.py expects as modules,
and the key tables capture.py shares. Stdlib only; needs libX11, libXi, libXtst and libXrandr,
which every X11 desktop ships.

Named keys travel by physical position (X keycode = evdev code + 8), so they do not depend on the
layout. Characters are looked up in the current keymap, with the peer's US position as the
fallback and as the key for a chord, the same rule the Windows and Mac injectors follow.
"""

import ctypes
import ctypes.util
import logging
import threading

import return_edge

LOGGER = logging.getLogger(__name__)


def _lib(name):
    path = ctypes.util.find_library(name)
    if path is None:
        raise OSError(f"lib{name} not found; install it (e.g. apt install lib{name.lower()}6)")
    return ctypes.CDLL(path)


X = _lib("X11")
XI = _lib("Xi")
XTST = _lib("Xtst")
XRANDR = _lib("Xrandr")

Display_p = ctypes.c_void_p
X.XInitThreads()
X.XOpenDisplay.restype = Display_p
X.XOpenDisplay.argtypes = [ctypes.c_char_p]
X.XDefaultRootWindow.restype = ctypes.c_ulong
X.XDefaultRootWindow.argtypes = [Display_p]
X.XFlush.argtypes = [Display_p]
X.XSync.argtypes = [Display_p, ctypes.c_int]
X.XKeysymToKeycode.restype = ctypes.c_ubyte
X.XKeysymToKeycode.argtypes = [Display_p, ctypes.c_ulong]
X.XkbKeycodeToKeysym.restype = ctypes.c_ulong
X.XkbKeycodeToKeysym.argtypes = [Display_p, ctypes.c_ubyte, ctypes.c_int, ctypes.c_int]
X.XWarpPointer.argtypes = [Display_p, ctypes.c_ulong, ctypes.c_ulong] + [ctypes.c_int] * 2 + [ctypes.c_uint] * 2 + [ctypes.c_int] * 2
X.XQueryPointer.argtypes = [Display_p, ctypes.c_ulong] + [ctypes.POINTER(ctypes.c_ulong)] * 2 + [ctypes.POINTER(ctypes.c_int)] * 4 + [ctypes.POINTER(ctypes.c_uint)]
X.XDisplayWidth.argtypes = X.XDisplayHeight.argtypes = [Display_p, ctypes.c_int]
XTST.XTestFakeKeyEvent.argtypes = [Display_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
XTST.XTestFakeButtonEvent.argtypes = [Display_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
XTST.XTestFakeRelativeMotionEvent.argtypes = [Display_p, ctypes.c_int, ctypes.c_int, ctypes.c_ulong]


class XRRMonitorInfo(ctypes.Structure):
    _fields_ = [
        ("name", ctypes.c_ulong), ("primary", ctypes.c_int), ("automatic", ctypes.c_int),
        ("noutput", ctypes.c_int), ("x", ctypes.c_int), ("y", ctypes.c_int),
        ("width", ctypes.c_int), ("height", ctypes.c_int), ("mwidth", ctypes.c_int),
        ("mheight", ctypes.c_int), ("outputs", ctypes.c_void_p),
    ]


XRANDR.XRRGetMonitors.restype = ctypes.POINTER(XRRMonitorInfo)
XRANDR.XRRGetMonitors.argtypes = [Display_p, ctypes.c_ulong, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
XRANDR.XRRFreeMonitors.argtypes = [ctypes.POINTER(XRRMonitorInfo)]


def open_display():
    display = X.XOpenDisplay(None)
    if not display:
        raise OSError("cannot open the X display; Beamer for Linux needs an X11 session, not Wayland")
    return display


# One connection for everything outside the capture thread, which keeps its own.
_display = open_display()
_root = X.XDefaultRootWindow(_display)
_lock = threading.RLock()

# Wire name -> evdev code. The wire's names are the Windows injector's VK_MAP keys.
EVDEV_BY_NAME = {
    "esc": 1, "backspace": 14, "tab": 15, "enter": 28, "ctrl": 29, "shift": 42, "shift_r": 54,
    "alt": 56, "space": 57, "caps_lock": 58, "num_lock": 69, "scroll_lock": 70, "ctrl_r": 97,
    "print_screen": 99, "alt_r": 100, "home": 102, "up": 103, "page_up": 104, "left": 105,
    "right": 106, "end": 107, "down": 108, "page_down": 109, "insert": 110, "delete": 111,
    "volume_mute": 113, "volume_down": 114, "volume_up": 115, "pause": 119, "cmd": 125,
    "cmd_r": 126, "menu": 127, "browser_back": 158, "browser_forward": 159, "media_next": 163,
    "media_play_pause": 164, "media_prev": 165, "media_stop": 166,
}
EVDEV_BY_NAME.update({f"f{i}": 58 + i for i in range(1, 11)})
EVDEV_BY_NAME.update({"f11": 87, "f12": 88})
EVDEV_BY_NAME.update({f"f{i}": 183 + i - 13 for i in range(13, 25)})
EVDEV_BY_NAME["return"] = EVDEV_BY_NAME["enter"]
EVDEV_BY_NAME["escape"] = EVDEV_BY_NAME["esc"]
NAME_BY_EVDEV = {code: name for name, code in EVDEV_BY_NAME.items() if name not in ("return", "escape")}
NAME_BY_EVDEV[96] = "enter"  # keypad Enter
MODIFIER_NAMES = {"ctrl", "ctrl_r", "alt", "alt_r", "cmd", "cmd_r", "shift", "shift_r"}
CHORD_NAMES = MODIFIER_NAMES - {"shift", "shift_r"}

# What each evdev code types on a US keyboard, unshifted: the "us" field on the wire.
US_BY_EVDEV = {}
for _start, _row in ((2, "1234567890-="), (16, "qwertyuiop[]"), (30, "asdfghjkl;'`"), (43, "\\zxcvbnm,./")):
    for _i, _ch in enumerate(_row):
        US_BY_EVDEV[_start + _i] = _ch
EVDEV_BY_US = {ch: code for code, ch in US_BY_EVDEV.items()}

# Keypad keysyms with Num Lock off, which are named keys rather than characters.
NAME_BY_KEYSYM = {
    0xFF95: "home", 0xFF96: "left", 0xFF97: "up", 0xFF98: "right", 0xFF99: "down",
    0xFF9A: "page_up", 0xFF9B: "page_down", 0xFF9C: "end", 0xFF9E: "insert", 0xFF9F: "delete",
    0xFF8D: "enter",
}
KEYPAD_CHARS = {0xFFAA: "*", 0xFFAB: "+", 0xFFAC: ",", 0xFFAD: "-", 0xFFAE: ".", 0xFFAF: "/", 0xFF80: " "}


def keysym_to_char(keysym):
    """The character a keysym types, or None. ponytail: Latin-1, Unicode keysyms and the keypad
    only; the legacy Latin-2..8 keysym blocks are skipped, add a table if a layout needs them."""
    if 0x20 <= keysym <= 0x7E or 0xA0 <= keysym <= 0xFF:
        return chr(keysym)
    if 0x01000100 <= keysym <= 0x0110FFFF:
        return chr(keysym - 0x01000000)
    if 0xFFB0 <= keysym <= 0xFFB9:
        return str(keysym - 0xFFB0)
    return KEYPAD_CHARS.get(keysym)


def char_to_keysym(ch):
    code = ord(ch)
    return code if 0x20 <= code <= 0x7E or 0xA0 <= code <= 0xFF else 0x01000000 | code


# -- desktop: what receiver.py calls ------------------------------------------------------------


def monitors():
    with _lock:
        count = ctypes.c_int()
        found = XRANDR.XRRGetMonitors(_display, _root, 1, ctypes.byref(count))
        if found and count.value:
            rects = [return_edge.Rect(found[i].x, found[i].y, found[i].width, found[i].height) for i in range(count.value)]
            XRANDR.XRRFreeMonitors(found)
            return rects
        return [return_edge.Rect(0, 0, X.XDisplayWidth(_display, 0), X.XDisplayHeight(_display, 0))]


def cursor_position():
    with _lock:
        root, child = ctypes.c_ulong(), ctypes.c_ulong()
        rx, ry, wx, wy = ctypes.c_int(), ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
        mask = ctypes.c_uint()
        X.XQueryPointer(_display, _root, ctypes.byref(root), ctypes.byref(child), ctypes.byref(rx),
                        ctypes.byref(ry), ctypes.byref(wx), ctypes.byref(wy), ctypes.byref(mask))
        return rx.value, ry.value


def set_cursor_position(x, y):
    with _lock:
        X.XWarpPointer(_display, 0, _root, 0, 0, 0, 0, int(x), int(y))
        X.XFlush(_display)


# -- injector: what receiver.handle_message calls -----------------------------------------------

BUTTONS = {"left": 1, "middle": 2, "right": 3, "back": 8, "forward": 9}
PIXELS_PER_NOTCH = 20.0
_keys_down = {}  # wire key name -> X keycode it went down on
_mods_down = set()
_buttons_down = set()
_scroll_accum = [0.0, 0.0]
_warned = set()


def _key(keycode, down):
    XTST.XTestFakeKeyEvent(_display, keycode, int(bool(down)), 0)


def _level_of(keycode, keysym):
    for level in (0, 1):
        if X.XkbKeycodeToKeysym(_display, keycode, 0, level) == keysym:
            return level
    return None


def _plan(name, us):
    """(keycode, needs_shift) for a wire key, or None when nothing here can type it."""
    lowered = name.lower()
    if lowered in EVDEV_BY_NAME:
        return EVDEV_BY_NAME[lowered] + 8, False
    if len(name) != 1:
        return None
    # A chord is a shortcut: it goes to the key in the sender's US place, whatever this layout
    # puts there, so Ctrl+C is the C key on any layout.
    if us in EVDEV_BY_US and _mods_down & CHORD_NAMES:
        return EVDEV_BY_US[us] + 8, False
    keysym = char_to_keysym(name)
    keycode = X.XKeysymToKeycode(_display, keysym)
    if keycode:
        level = _level_of(keycode, keysym)
        return keycode, level == 1
    if us in EVDEV_BY_US:
        return EVDEV_BY_US[us] + 8, False
    return None


def inject_key(name, down=True, us=None):
    with _lock:
        lowered = name.lower()
        if lowered in MODIFIER_NAMES:
            (_mods_down.add if down else _mods_down.discard)(lowered)
        if not down and name in _keys_down:
            _key(_keys_down.pop(name), False)
            X.XFlush(_display)
            return
        plan = _plan(name, us)
        if plan is None:
            if name not in _warned:
                _warned.add(name)
                LOGGER.warning("No key on this layout types %r; dropped", name)
            return
        keycode, needs_shift = plan
        shift_held = bool(_mods_down & {"shift", "shift_r"})
        if down:
            _keys_down[name] = keycode
            if needs_shift and not shift_held:
                _key(EVDEV_BY_NAME["shift"] + 8, True)
                _key(keycode, True)
                _key(EVDEV_BY_NAME["shift"] + 8, False)
            else:
                _key(keycode, True)
        else:
            _key(keycode, False)
        X.XFlush(_display)


def inject_mouse_move(dx, dy):
    with _lock:
        XTST.XTestFakeRelativeMotionEvent(_display, int(dx), int(dy), 0)
        X.XFlush(_display)


def inject_mouse_button(button, down=True):
    number = BUTTONS.get(button)
    if number is None:
        LOGGER.warning("Unknown mouse button %r ignored", button)
        return
    with _lock:
        (_buttons_down.add if down else _buttons_down.discard)(number)
        XTST.XTestFakeButtonEvent(_display, number, int(bool(down)), 0)
        X.XFlush(_display)


def _clicks(value, axis, positive, negative):
    """Whole wheel clicks out of `value`, carrying the remainder to the next event."""
    _scroll_accum[axis] += value
    whole = int(_scroll_accum[axis])
    _scroll_accum[axis] -= whole
    button = positive if whole > 0 else negative
    for _ in range(abs(whole)):
        XTST.XTestFakeButtonEvent(_display, button, 1, 0)
        XTST.XTestFakeButtonEvent(_display, button, 0, 0)


def inject_scroll(dy, dx=0.0, mode="line"):
    scale = 1.0 / PIXELS_PER_NOTCH if mode == "pixel" else 1.0
    with _lock:
        # X buttons: 4 up, 5 down, 6 left, 7 right. On the wire dy > 0 is up and dx > 0 is right.
        _clicks(float(dy) * scale, 0, 4, 5)
        _clicks(float(dx) * scale, 1, 7, 6)
        X.XFlush(_display)


def inject_gesture(name):
    LOGGER.info("Gesture %r ignored: Beamer for Linux does not inject gestures", name)


def release_all():
    with _lock:
        for keycode in set(_keys_down.values()):
            _key(keycode, False)
        for number in _buttons_down:
            XTST.XTestFakeButtonEvent(_display, number, 0, 0)
        _keys_down.clear()
        _mods_down.clear()
        _buttons_down.clear()
        X.XFlush(_display)
