#!/usr/bin/env python3
"""Beamer for Linux (X11): takes the Mac's place opposite the Windows app.

    python3 beamer_linux.py pair [--host PC_ADDRESS]   pair with the PC's six-digit code
    python3 beamer_linux.py                            run: both directions, until Ctrl+C

The wire, the pairing and the receiver are mac_app's own modules, used unchanged; this folder
only holds what is Linux-shaped. Settings live in ~/.config/beamer/config.json.
"""

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.append(str(HERE.parent / "mac_app"))

import pairing  # noqa: E402
import protocol  # noqa: E402
import return_edge  # noqa: E402

LOGGER = logging.getLogger("beamer")
CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "beamer" / "config.json"
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
}


def load_config():
    raw = {}
    if CONFIG_PATH.exists():
        raw = json.loads(CONFIG_PATH.read_text())
    cfg = types.SimpleNamespace(**{**DEFAULTS, **raw})
    if cfg.mac_edge not in return_edge.EDGES:
        raise SystemExit(f"mac_edge must be one of {', '.join(return_edge.EDGES)}")
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
    print("On the PC, open Beamer and press Pair a Mac. Waiting for it to show a code...")
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


class App:
    def __init__(self, cfg):
        import capture
        import clipboard_linux
        import link
        import receiver
        import x11

        self.cfg = cfg
        self.x11 = x11
        self.receiving = False
        self.pin = None
        self._carry = [0.0, 0.0]
        self._monitors, self._monitors_at = None, 0.0
        self._rearm()
        self.link = link.Link(cfg, clipboard_linux, x11, self._on_redirect)
        self.link.on_arrangement = self._on_arrangement
        self.capture = capture.Capture(cfg.trigger_key, self._on_motion, self._on_key, self._on_button,
                                       self._on_scroll, self._on_trigger)
        self.receiver = receiver.ReceiverServer(
            lambda state, detail: LOGGER.info("from the PC: %s", detail),
            clipboard=clipboard_linux,
            unlock=types.SimpleNamespace(is_locked=lambda: False, ensure_unlocked=lambda: True),
            desktop=x11,
            injector=x11,
            focus_callback=self._on_focus,
            arrangement_callback=self._on_arrangement,
            self_name="Linux machine",
            peer_name="PC",
            self_target="mac",
            peer_target="windows",
        )

    def run(self):
        self.capture.start()
        self.link.start()
        self.receiver.start(self.cfg)
        stop = threading.Event()
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        LOGGER.info("Beamer for Linux running; the PC is past the %s edge. Double-tap %s to switch.",
                    self.cfg.mac_edge, self.cfg.trigger_key)
        stop.wait()
        self.link.stop()
        self.receiver.stop()
        self.capture.stop()

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
        self.pin = pin
        self._carry = [0.0, 0.0]
        if not value:
            self._rearm()
        return self.capture.grab(value)

    def _on_motion(self, dx, dy):
        if self.link.redirecting:
            x, y = dx + self._carry[0], dy + self._carry[1]
            whole_x, whole_y = int(x), int(y)
            self._carry = [x - whole_x, y - whole_y]
            if whole_x or whole_y:
                self.link.send_input({"type": protocol.MSG_MOUSEMOVE, "data": {"dx": whole_x, "dy": whole_y}})
            if self.pin is not None:
                self.x11.set_cursor_position(*self.pin)
            return
        if not self.link.connected or self.capture.buttons_down:
            # Never across while dragging.
            return
        # This runs while the PC drives this machine too: the PC's moves come through XTEST and
        # never arrive here, so this is this machine's own hand pushing out.
        outcome = self.edge.feed(self._monitors_now(), self.x11.cursor_position(), dx, dy)
        if outcome.action == return_edge.CROSS:
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
        if self.receiving:
            self.receiver.send_home()
        else:
            self.link.set_redirecting(not self.link.redirecting)

    # -- from the PC -----------------------------------------------------------------------------

    def _on_focus(self, target):
        self.receiving = target == "mac"
        LOGGER.info("the PC is %s this machine", "driving" if self.receiving else "no longer driving")

    def _on_arrangement(self, mac_edge, set_at):
        if not protocol.arrangement_wins(set_at, self.cfg.arrangement_set_at):
            return
        self.cfg.mac_edge, self.cfg.arrangement_set_at = mac_edge, set_at
        save_config(self.cfg)
        self._rearm()
        LOGGER.info("the PC moved the border: it is now past this machine's %s edge", mac_edge)


def main():
    parser = argparse.ArgumentParser(description="Beamer for Linux (X11)")
    parser.add_argument("command", nargs="?", default="run", choices=("run", "pair"))
    parser.add_argument("--host", help="the PC's address, when its beacon does not reach this machine")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.command == "pair":
        pair(args)
        return
    if os.environ.get("XDG_SESSION_TYPE") == "wayland":
        raise SystemExit("Beamer for Linux needs an X11 session; log in with 'Ubuntu on Xorg' or similar.")
    cfg = load_config()
    if not cfg.auth_token or not cfg.host:
        raise SystemExit("Not paired yet: run `python3 beamer_linux.py pair` first.")
    App(cfg).run()


if __name__ == "__main__":
    main()
