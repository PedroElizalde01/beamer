#!/usr/bin/env python3
"""Beamer for Linux (X11): takes the Mac's place opposite the Windows app.

    beamer_linux.py                              the app: a tray icon and a settings window
    beamer_linux.py --hidden                     the same, tray only (what start at login runs)
    beamer_linux.py --headless                   no window, both directions, until Ctrl+C
    beamer_linux.py pair [--host PC_ADDRESS]     pair on the command line

The wire, the pairing, the receiver and the crossing animations are win_app's own modules, used
unchanged (win_app's shared files are byte-identical to mac_app's); this folder only holds what is
Linux-shaped. Settings live in ~/.config/beamer/config.json, the log in ~/.local/state/beamer.
"""

import argparse
import json
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time
import types
from pathlib import Path

if getattr(sys, "frozen", False):
    ROOT = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
else:
    HERE = Path(__file__).resolve().parent
    # win_app before the repository root: the root's theme.py is the Mac's.
    sys.path[1:1] = [str(HERE.parent / "win_app"), str(HERE.parent)]
    ROOT = HERE.parent

import pairing  # noqa: E402
import protocol  # noqa: E402
import return_edge  # noqa: E402

LOGGER = logging.getLogger("beamer")
VERSION = (ROOT / "VERSION").read_text().strip() if (ROOT / "VERSION").exists() else "dev"
CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "beamer" / "config.json"
LOG_PATH = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "beamer" / "beamer.log"
DEFAULTS = {
    "host": "",
    "port": protocol.DEFAULT_PORT,
    "auth_token": "",
    "pc_name": "",
    # The edge of this machine's screen that leads to the PC.
    "mac_edge": "right",
    "resistance_px": return_edge.DEFAULT_RESISTANCE_PX,
    "trigger_key": "ctrl_r",
    "arrangement_set_at": 0,
    # One switch per direction, as on the Mac and the PC.
    "send_to_windows": True,
    "allow_windows_to_drive": True,
    # How the PC's pointer and scroll feel here; see receiver.InputScale.
    "pointer_speed": 1.0,
    "scroll_speed": 1.0,
    "reverse_scroll": False,
    # How crossing looks on this screen: the Windows app's own settings and values.
    "edge_glow": True,
    "glow_style": "glow",
    "glow_colour": "signal",
    "shortcut_arrival": True,
    "shortcut_arrival_style": "match",
    "effect_length": "normal",
    "appearance": "system",
}
# The keys a double tap can be, by wire name: ones that type nothing.
TRIGGER_KEYS = {
    "ctrl_r": "Right Ctrl", "alt_r": "Right Alt", "cmd_r": "Right Super", "menu": "Menu",
    "scroll_lock": "Scroll Lock", "pause": "Pause", "f12": "F12",
}


def load_config():
    raw = {}
    if CONFIG_PATH.exists():
        raw = json.loads(CONFIG_PATH.read_text())
    cfg = types.SimpleNamespace(**{**DEFAULTS, **raw})
    if cfg.mac_edge not in return_edge.EDGES:
        cfg.mac_edge = DEFAULTS["mac_edge"]
    if cfg.trigger_key not in TRIGGER_KEYS:
        cfg.trigger_key = DEFAULTS["trigger_key"]
    return cfg


def save_config(cfg):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    # The token is a password for both machines: readable by this user only.
    fd = os.open(CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(vars(cfg), handle, indent=2)


def pair(args):
    cfg = load_config()
    discovery = pairing.Discovery()
    discovery.start()
    if args.host:
        discovery.find(args.host)
    print("On the PC, open Beamer and press its Pair button. Waiting for it to show a code...")
    deadline = time.monotonic() + 60
    pcs = []
    while time.monotonic() < deadline and not pcs:
        if discovery.error:
            raise SystemExit(f"discovery failed: {discovery.error}")
        pcs = [pc for pc in discovery.pcs() if pc.get("pair_id")]
        time.sleep(0.5)
    if not pcs:
        raise SystemExit("No PC is showing a code. Is UDP 24821 open, or try --host with the PC's address.")
    for index, pc in enumerate(pcs, 1):
        print(f"  {index}. {pc['name']} at {pc['address']}:{pc['port']}")
    pc = pcs[0] if len(pcs) == 1 else pcs[int(input("Which PC? ")) - 1]
    code = input(f"Code shown on {pc['name']}: ").strip()
    try:
        token, name = discovery.pair(pc, code)
    except pairing.PairingError as exc:
        raise SystemExit(f"Pairing failed: {exc}")
    finally:
        discovery.stop()
    cfg.host, cfg.port, cfg.auth_token, cfg.pc_name = pc["address"], pc["port"], token, name
    save_config(cfg)
    print(f"Paired with {name}. Settings saved to {CONFIG_PATH}")


def _nothing(*_args):
    pass


class App:
    """Both directions, without a window. A window sets `on_change()` (status or where input is
    changed), `on_pressure(edge, pressure, crossed, part)` and `on_arrival(method, edge, x, y)`;
    all three are called from worker threads."""

    def __init__(self, cfg):
        import capture
        import clipboard_linux
        import link
        import receiver
        import x11

        self.cfg = cfg
        self.x11 = x11
        self.receiver_module = receiver
        self.receiving = False
        self.paused = False
        self.link_status = "Not paired yet"
        self.receiver_status = "Off"
        self.on_change = self.on_pressure = self.on_arrival = _nothing
        self._monitors, self._monitors_at = None, 0.0
        self._rearm()
        self.link = link.Link(cfg, clipboard_linux, x11, self._on_redirect, on_status=self._on_link_status)
        self.link.on_arrangement = self._on_arrangement
        self.link.on_arrival = lambda edge, x, y: self.on_arrival("switch" if edge is None else "edge", edge or "", x, y)
        self.capture = capture.Capture(cfg.trigger_key, self._on_motion, self._on_key, self._on_button,
                                       self._on_scroll, self._on_trigger)
        self.receiver = receiver.ReceiverServer(
            self._on_receiver_status,
            clipboard=clipboard_linux,
            unlock=types.SimpleNamespace(is_locked=lambda: False, ensure_unlocked=lambda: True),
            desktop=x11,
            injector=x11,
            focus_callback=self._on_focus,
            arrangement_callback=self._on_arrangement,
            pressure_callback=lambda edge, pressure, crossed, part=None: self.on_pressure(edge, pressure, crossed, part),
            arrival_callback=lambda edge, x, y: self.on_arrival("switch" if edge is None else "edge", edge or "", x, y),
            self_name="Linux machine",
            peer_name="PC",
            self_target="mac",
            peer_target="windows",
        )
        self.receiver.edges_held = lambda: self.paused

    @property
    def paired(self):
        return bool(self.cfg.host and self.cfg.auth_token)

    @property
    def where(self):
        """"pc" while this machine drives the PC, "driven" while the PC drives this one, else "here"."""
        return "pc" if self.link.redirecting else "driven" if self.receiving else "here"

    def start(self):
        self.capture.start()
        self.link.start()
        self.apply()

    def stop(self):
        self.link.stop()
        self.receiver.stop()
        self.capture.stop()

    def run(self):
        self.start()
        stop = threading.Event()
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        LOGGER.info("Beamer for Linux running; the PC is past the %s edge. Double-tap %s to switch.",
                    self.cfg.mac_edge, self.cfg.trigger_key)
        stop.wait()
        self.stop()

    def apply(self):
        """Brings everything in line with `cfg` after a change, and saves it."""
        self._rearm()
        self.capture.trigger_code = self.x11.EVDEV_BY_NAME[self.cfg.trigger_key] + 8
        self.receiver.input_scale = self.receiver_module.InputScale(
            self.cfg.pointer_speed, self.cfg.scroll_speed, self.cfg.reverse_scroll)
        # Never listening without a token: an empty one is a key anybody holds.
        if self.paired and self.cfg.allow_windows_to_drive:
            self.receiver.start(self.cfg)
        elif self.receiver.listening:
            self.receiver.stop()
        if not self.cfg.send_to_windows and self.link.redirecting:
            self.link.set_redirecting(False)
        save_config(self.cfg)
        self.on_change()

    def paired_with(self, pc, token, name):
        """A pairing made in the window: the new token for both directions."""
        self.cfg.host, self.cfg.port, self.cfg.auth_token, self.cfg.pc_name = pc["address"], pc["port"], token, name
        self.link.reconnect()
        if self.receiver.listening:
            self.receiver.stop()
        self.apply()

    def toggle(self):
        """Send input to the PC, or bring it home: the trigger key's and the menu's switch."""
        if self.receiving:
            self.receiver.send_home()
        elif self.link.redirecting:
            self.link.set_redirecting(False)
            self.on_arrival("switch", "", *self.x11.cursor_position())
        elif self.cfg.send_to_windows:
            self.link.set_redirecting(True)
        self.on_change()

    def _rearm(self):
        # A fresh model: one that crossed has disarmed itself.
        self.edge = return_edge.ReturnEdge(self.cfg.mac_edge, int(self.cfg.resistance_px))

    def _monitors_now(self):
        now = time.monotonic()
        if self._monitors is None or now - self._monitors_at > 2.0:
            self._monitors, self._monitors_at = self.x11.monitors(), now
        return self._monitors

    # -- capture callbacks, on the capture thread ------------------------------------------------

    def _on_redirect(self, value, pin):
        if not value:
            self._rearm()
        grabbed = self.capture.grab(value)
        self.on_change()
        return grabbed

    def _on_motion(self, dx, dy):
        if self.link.redirecting:
            # Whole pixels: while grabbed, capture measures moves on the screen itself.
            self.link.send_input({"type": protocol.MSG_MOUSEMOVE, "data": {"dx": int(dx), "dy": int(dy)}})
            return
        if not self.link.connected or not self.cfg.send_to_windows or self.paused or self.capture.buttons_down:
            # Never across while dragging.
            return
        # This runs while the PC drives this machine too: the PC's moves come through XTEST and
        # never arrive here, so this is this machine's own hand pushing out.
        outcome = self.edge.feed(self._monitors_now(), self.x11.cursor_position(), dx, dy)
        crossed = outcome.action == return_edge.CROSS
        if outcome.pressure > 0 or crossed:
            self.on_pressure(self.cfg.mac_edge, outcome.pressure, crossed, None)
        if crossed:
            if self.receiving:
                # As the Mac does: the PC's input goes home first, and this hand follows it across.
                if not self.receiver.send_home():
                    LOGGER.warning("cannot cross: the PC is driving this machine and cannot be reached")
                    self._rearm()
                    return
                self.receiving = False
            if not self.link.set_redirecting(True, outcome.edge, outcome.offset):
                self._rearm()

    def _on_key(self, name, down, us):
        data = {"key": name}
        if us is not None:
            data["us"] = us
        self.link.send_input({"type": protocol.MSG_KEYDOWN if down else protocol.MSG_KEYUP, "data": data})

    def _on_button(self, name, down):
        self.link.send_input({"type": protocol.MSG_MOUSEDOWN if down else protocol.MSG_MOUSEUP, "data": {"button": name}})

    def _on_scroll(self, dy, dx):
        self.link.send_input(protocol.scroll_msg(dy, dx, "line"))

    def _on_trigger(self):
        self.toggle()

    # -- from the PC and the links -----------------------------------------------------------------

    def _on_link_status(self, text):
        LOGGER.info("to the PC: %s", text)
        self.link_status = text
        self.on_change()

    def _on_receiver_status(self, state, detail):
        LOGGER.info("from the PC: %s", detail)
        self.receiver_status = detail
        self.on_change()

    def _on_focus(self, target):
        self.receiving = target == "mac"
        LOGGER.info("the PC is %s this machine", "driving" if self.receiving else "no longer driving")
        self.on_change()

    def _on_arrangement(self, mac_edge, set_at):
        if not protocol.arrangement_wins(set_at, self.cfg.arrangement_set_at):
            return
        self.cfg.mac_edge, self.cfg.arrangement_set_at = mac_edge, set_at
        LOGGER.info("the PC moved the border: it is now past this machine's %s edge", mac_edge)
        self.apply()


def setup_logging(verbose):
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handlers = [logging.StreamHandler(),
                logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=2)]
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main():
    parser = argparse.ArgumentParser(description="Beamer for Linux (X11)")
    parser.add_argument("command", nargs="?", default="run", choices=("run", "pair"))
    parser.add_argument("--host", help="the PC's address, when its beacon does not reach this machine")
    parser.add_argument("--hidden", action="store_true", help="start in the tray without opening the window")
    parser.add_argument("--headless", action="store_true", help="no window or tray icon")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    setup_logging(args.verbose)
    if args.command == "pair":
        pair(args)
        return
    if os.environ.get("XDG_SESSION_TYPE") == "wayland":
        raise SystemExit("Beamer for Linux needs an X11 session: choose the Xorg session at the login screen.")
    cfg = load_config()
    if args.headless:
        if not cfg.auth_token or not cfg.host:
            raise SystemExit("Not paired yet: run `beamer_linux.py pair` first.")
        App(cfg).run()
        return
    import gui

    sys.exit(gui.run(App(cfg), args.hidden, VERSION, LOG_PATH, TRIGGER_KEYS, ROOT))


if __name__ == "__main__":
    main()
