"""This machine's own keyboard and pointer, read on one thread with its own X connection.

XInput2 raw events carry the hand's movement even when the pointer is pinned against a screen
edge or warped back, which is what the edge push and the pinned pointer while redirecting both
need. While input goes to the PC the keyboard and pointer are grabbed, so nothing here sees
them, and keys and buttons are read from the grab's core events instead: those carry the
autorepeat and the wheel clicks a touchpad's scrolling becomes, which raw events do not.
"""

import ctypes
import logging
import select
import threading
import time

import x11
from x11 import X, XI

LOGGER = logging.getLogger(__name__)

GenericEvent, KeyPress, KeyRelease, ButtonPress, ButtonRelease = 35, 2, 3, 4, 5
XI_RawKeyPress, XI_RawKeyRelease, XI_RawButtonPress, XI_RawButtonRelease, XI_RawMotion = 13, 14, 15, 16, 17
XIAllMasterDevices, XIAllDevices = 1, 0
GrabModeAsync, GrabSuccess = 1, 0
POINTER_GRAB_MASK = (1 << 2) | (1 << 3) | (1 << 6)  # ButtonPress, ButtonRelease, PointerMotion
ShiftMask, Mod5Mask = 1 << 0, 1 << 7
DOUBLE_TAP_SECONDS = 0.4
WHEEL = {4: (1.0, 0.0), 5: (-1.0, 0.0), 6: (0.0, -1.0), 7: (0.0, 1.0)}
BUTTON_NAMES = {1: "left", 2: "middle", 3: "right", 8: "back", 9: "forward"}


class XKeyEvent(ctypes.Structure):
    # XButtonEvent has the same layout, with `button` where `keycode` is.
    _fields_ = [
        ("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p), ("window", ctypes.c_ulong), ("root", ctypes.c_ulong),
        ("subwindow", ctypes.c_ulong), ("time", ctypes.c_ulong), ("x", ctypes.c_int),
        ("y", ctypes.c_int), ("x_root", ctypes.c_int), ("y_root", ctypes.c_int),
        ("state", ctypes.c_uint), ("keycode", ctypes.c_uint), ("same_screen", ctypes.c_int),
    ]


class XGenericEventCookie(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p), ("extension", ctypes.c_int), ("evtype", ctypes.c_int),
        ("cookie", ctypes.c_uint), ("data", ctypes.c_void_p),
    ]


class XEvent(ctypes.Union):
    _fields_ = [("type", ctypes.c_int), ("xkey", XKeyEvent), ("xcookie", XGenericEventCookie), ("pad", ctypes.c_long * 24)]


class XIValuatorState(ctypes.Structure):
    _fields_ = [("mask_len", ctypes.c_int), ("mask", ctypes.POINTER(ctypes.c_ubyte)), ("values", ctypes.POINTER(ctypes.c_double))]


class XIRawEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p), ("extension", ctypes.c_int), ("evtype", ctypes.c_int),
        ("time", ctypes.c_ulong), ("deviceid", ctypes.c_int), ("sourceid", ctypes.c_int),
        ("detail", ctypes.c_int), ("flags", ctypes.c_int), ("valuators", XIValuatorState),
        ("raw_values", ctypes.POINTER(ctypes.c_double)),
    ]


class XIEventMask(ctypes.Structure):
    _fields_ = [("deviceid", ctypes.c_int), ("mask_len", ctypes.c_int), ("mask", ctypes.POINTER(ctypes.c_ubyte))]


class XIDeviceInfo(ctypes.Structure):
    _fields_ = [
        ("deviceid", ctypes.c_int), ("name", ctypes.c_char_p), ("use", ctypes.c_int),
        ("attachment", ctypes.c_int), ("enabled", ctypes.c_int), ("num_classes", ctypes.c_int),
        ("classes", ctypes.c_void_p),
    ]


X.XQueryExtension.argtypes = [ctypes.c_void_p, ctypes.c_char_p] + [ctypes.POINTER(ctypes.c_int)] * 3
X.XPending.argtypes = X.XConnectionNumber.argtypes = [ctypes.c_void_p]
X.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.POINTER(XEvent)]
X.XGetEventData.argtypes = X.XFreeEventData.argtypes = [ctypes.c_void_p, ctypes.POINTER(XGenericEventCookie)]
X.XGrabKeyboard.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_ulong]
X.XGrabPointer.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_uint, ctypes.c_int, ctypes.c_int,
                           ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
X.XUngrabKeyboard.argtypes = X.XUngrabPointer.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
X.XkbSetDetectableAutoRepeat.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
XI.XIQueryVersion.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
XI.XISelectEvents.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(XIEventMask), ctypes.c_int]
XI.XIQueryDevice.restype = ctypes.POINTER(XIDeviceInfo)
XI.XIQueryDevice.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
XI.XIFreeDeviceInfo.argtypes = [ctypes.POINTER(XIDeviceInfo)]


def motion_of(raw):
    """(dx, dy) from a raw motion event: valuators 0 and 1, packed in the order their mask bits
    are set. The accelerated values, as the pointer on screen would have moved."""
    dx = dy = 0.0
    index = 0
    for bit in range(raw.valuators.mask_len * 8):
        if raw.valuators.mask[bit >> 3] & (1 << (bit & 7)):
            if bit == 0:
                dx = raw.valuators.values[index]
            elif bit == 1:
                dy = raw.valuators.values[index]
            index += 1
    return dx, dy


class Capture:
    """Calls, on its own thread:
    `on_motion(dx, dy)` for every movement of this machine's pointer,
    `on_key(name, down, us)` while grabbed, with the wire name or character,
    `on_button(name, down)` and `on_scroll(dy, dx)` while grabbed,
    `on_trigger()` when the trigger key is double-tapped, grabbed or not.
    `buttons_down` is what this machine's hand is holding, for "never while dragging"."""

    def __init__(self, trigger_key, on_motion, on_key, on_button, on_scroll, on_trigger):
        self.trigger_code = x11.EVDEV_BY_NAME[trigger_key] + 8
        self.on_motion, self.on_key, self.on_button = on_motion, on_key, on_button
        self.on_scroll, self.on_trigger = on_scroll, on_trigger
        self.buttons_down = set()
        self._want_grab = False
        self._grabbed = False
        self._grab_result = None
        self._grab_done = threading.Event()
        self._stop = threading.Event()
        self._tap_at = 0.0
        self._tap_clean = False
        self._keys_down = {}
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name="Beamer-capture", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def grab(self, value):
        """Take this machine's keyboard and pointer, or give them back. Runs on the capture
        thread, which owns the connection; True when it holds what was asked for."""
        self._grab_done.clear()
        self._want_grab = bool(value)
        if threading.current_thread() is self._thread:
            self._apply_grab()
        elif not self._grab_done.wait(1.0):
            return False
        return self._grabbed == bool(value)

    def _apply_grab(self):
        if self._want_grab and not self._grabbed:
            keyboard = X.XGrabKeyboard(self._display, self._root, 0, GrabModeAsync, GrabModeAsync, 0)
            pointer = X.XGrabPointer(self._display, self._root, 0, POINTER_GRAB_MASK, GrabModeAsync, GrabModeAsync, 0, 0, 0)
            if keyboard == GrabSuccess and pointer == GrabSuccess:
                self._grabbed = True
                self._keys_down.clear()
            else:
                # Another program holds a grab, an open menu say: stay here rather than half-grabbed.
                LOGGER.warning("Could not grab the keyboard (%s) and pointer (%s)", keyboard, pointer)
                self._want_grab = False
                X.XUngrabKeyboard(self._display, 0)
                X.XUngrabPointer(self._display, 0)
        elif not self._want_grab and self._grabbed:
            X.XUngrabKeyboard(self._display, 0)
            X.XUngrabPointer(self._display, 0)
            self._grabbed = False
        X.XFlush(self._display)
        self._grab_done.set()

    def _run(self):
        self._display = x11.open_display()
        self._root = X.XDefaultRootWindow(self._display)
        opcode, event, error = ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
        if not X.XQueryExtension(self._display, b"XInputExtension", ctypes.byref(opcode), ctypes.byref(event), ctypes.byref(error)):
            raise OSError("the X server has no XInput extension")
        # 2.2 or later, or raw events stop reaching this client while it holds a grab.
        major, minor = ctypes.c_int(2), ctypes.c_int(2)
        XI.XIQueryVersion(self._display, ctypes.byref(major), ctypes.byref(minor))
        self._xtest = self._xtest_devices()
        bits = 0
        for kind in (XI_RawKeyPress, XI_RawKeyRelease, XI_RawButtonPress, XI_RawButtonRelease, XI_RawMotion):
            bits |= 1 << kind
        mask_bytes = (ctypes.c_ubyte * 4)(*bits.to_bytes(4, "little"))
        mask = XIEventMask(XIAllMasterDevices, 4, mask_bytes)
        XI.XISelectEvents(self._display, self._root, ctypes.byref(mask), 1)
        X.XkbSetDetectableAutoRepeat(self._display, 1, None)
        X.XFlush(self._display)
        fd = X.XConnectionNumber(self._display)
        ev = XEvent()
        try:
            while not self._stop.is_set():
                if self._want_grab != self._grabbed:
                    self._apply_grab()
                if not X.XPending(self._display):
                    select.select([fd], [], [], 0.02)
                    continue
                X.XNextEvent(self._display, ctypes.byref(ev))
                try:
                    self._dispatch(ev, opcode.value)
                except Exception:
                    LOGGER.exception("input event handling failed")
        finally:
            X.XUngrabKeyboard(self._display, 0)
            X.XUngrabPointer(self._display, 0)
            X.XFlush(self._display)

    def _xtest_devices(self):
        """The XTEST devices, whose events are the peer's injected input, not this hand's."""
        count = ctypes.c_int()
        info = XI.XIQueryDevice(self._display, XIAllDevices, ctypes.byref(count))
        ids = {info[i].deviceid for i in range(count.value) if b"XTEST" in (info[i].name or b"")}
        XI.XIFreeDeviceInfo(info)
        return ids

    def _dispatch(self, ev, opcode):
        if ev.type == GenericEvent and ev.xcookie.extension == opcode:
            cookie = ev.xcookie
            if not X.XGetEventData(self._display, ctypes.byref(cookie)):
                return
            try:
                raw = ctypes.cast(cookie.data, ctypes.POINTER(XIRawEvent)).contents
                if raw.sourceid not in self._xtest:
                    self._raw(cookie.evtype, raw)
            finally:
                X.XFreeEventData(self._display, ctypes.byref(cookie))
        elif self._grabbed and ev.type in (KeyPress, KeyRelease):
            self._grabbed_key(ev.xkey, ev.type == KeyPress)
        elif self._grabbed and ev.type in (ButtonPress, ButtonRelease):
            self._grabbed_button(ev.xkey.keycode, ev.type == ButtonPress)

    def _raw(self, evtype, raw):
        if evtype == XI_RawMotion:
            dx, dy = motion_of(raw)
            if dx or dy:
                self.on_motion(dx, dy)
        elif evtype in (XI_RawButtonPress, XI_RawButtonRelease) and raw.detail not in WHEEL:
            (self.buttons_down.add if evtype == XI_RawButtonPress else self.buttons_down.discard)(raw.detail)
        elif evtype in (XI_RawKeyPress, XI_RawKeyRelease):
            self._trigger(raw.detail, evtype == XI_RawKeyPress)

    def _trigger(self, keycode, down):
        """A double tap: press, release, press within DOUBLE_TAP_SECONDS, with no other key between."""
        if keycode != self.trigger_code:
            self._tap_clean = False
            return
        if not down:
            return
        now = time.monotonic()
        if self._tap_clean and now - self._tap_at <= DOUBLE_TAP_SECONDS:
            self._tap_clean = False
            self.on_trigger()
            return
        self._tap_at, self._tap_clean = now, True

    def _grabbed_key(self, event, down):
        keycode = event.keycode
        if keycode == self.trigger_code:
            return
        if not down:
            name, us = self._keys_down.pop(keycode, (None, None))
            if name is not None:
                self.on_key(name, False, us)
            return
        # A repeat goes out as what the first press sent, so its release matches.
        held = self._keys_down.get(keycode)
        if held is None:
            held = self._translate(keycode, event.state)
            if held is None:
                return
            self._keys_down[keycode] = held
        self.on_key(held[0], True, held[1])

    def _translate(self, keycode, state):
        """(wire name or character, US place) for a key. Chord modifiers do not change the
        character, Shift and AltGr do, and Caps Lock is left to the PC's own state."""
        evdev = keycode - 8
        name = x11.NAME_BY_EVDEV.get(evdev)
        if name is not None:
            return name, None
        group = (state >> 13) & 3
        level = (1 if state & ShiftMask else 0) + (2 if state & Mod5Mask else 0)
        keysym = X.XkbKeycodeToKeysym(self._display, keycode, group, level) or X.XkbKeycodeToKeysym(self._display, keycode, group, 0)
        name = x11.NAME_BY_KEYSYM.get(keysym)
        if name is not None:
            return name, None
        character = x11.keysym_to_char(keysym)
        if character is None:
            return None
        return character, x11.US_BY_EVDEV.get(evdev)

    def _grabbed_button(self, number, down):
        if number in WHEEL:
            if down:
                self.on_scroll(*WHEEL[number])
            return
        name = BUTTON_NAMES.get(number)
        if name is not None:
            self.on_button(name, down)
