"""The Linux link against the real receiver.py over loopback, in the PC's place: handshake,
input with sequence numbers, the ack heartbeat, the clipboard on the way out, and the PC
sending input home. No X needed.  python3 linux_app/test_link.py"""

import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.append(str(Path(__file__).resolve().parent.parent / "mac_app"))

import link  # noqa: E402
import protocol  # noqa: E402
import receiver  # noqa: E402
import return_edge  # noqa: E402

PORT = 24890
TOKEN = "t" * 43


class FakeClipboard:
    def __init__(self, text=None):
        self.text, self.set = text, []

    def changed_contents(self):
        text, self.text = self.text, None
        return text, None

    def set_contents(self, text, image):
        self.set.append(text)
        return True

    def forget_sync(self):
        pass


def wait_for(condition, seconds=3.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def main():
    injected = []
    injector = types.SimpleNamespace(
        inject_key=lambda name, down, us=None: injected.append(("key", name, down, us)),
        inject_mouse_move=lambda dx, dy: injected.append(("move", dx, dy)),
        inject_mouse_button=lambda button, down: injected.append(("button", button, down)),
        inject_scroll=lambda dy, dx, mode: injected.append(("scroll", dy, dx)),
        release_all=lambda: None,
    )
    screen = [return_edge.Rect(0, 0, 1920, 1080)]
    desktop = types.SimpleNamespace(monitors=lambda: screen, cursor_position=lambda: (960, 540),
                                    set_cursor_position=lambda x, y: None)
    pc_clipboard = FakeClipboard()
    focus = []
    server = receiver.ReceiverServer(lambda state, detail: None, clipboard=pc_clipboard,
                                     unlock=types.SimpleNamespace(is_locked=lambda: False), desktop=desktop,
                                     injector=injector, focus_callback=focus.append)
    server.start(types.SimpleNamespace(port=PORT, auth_token=TOKEN))

    grabs = []
    cfg = types.SimpleNamespace(host="127.0.0.1", port=PORT, auth_token=TOKEN, mac_edge="right", resistance_px=120)
    linux_clipboard = FakeClipboard("from linux")
    client = link.Link(cfg, linux_clipboard, desktop, lambda value, pin: grabs.append(value) or True)
    client.start()
    try:
        assert wait_for(lambda: client.connected), "never connected"
        assert server.peer_platform == "linux", "the hello did not say this is Linux"
        assert client.set_redirecting(True, "left", 0.5)
        assert wait_for(lambda: focus == ["windows"]), focus
        assert server.return_edge == "left", "the way home was not armed on the PC"
        assert wait_for(lambda: pc_clipboard.set == ["from linux"]), pc_clipboard.set
        client.send_input({"type": protocol.MSG_KEYDOWN, "data": {"key": "c", "us": "c"}})
        client.send_input({"type": protocol.MSG_KEYUP, "data": {"key": "c", "us": "c"}})
        client.send_input({"type": protocol.MSG_MOUSEMOVE, "data": {"dx": -3, "dy": 4}})
        client.send_input(protocol.scroll_msg(1.0, 0.0, "line"))
        assert wait_for(lambda: len(injected) == 4), injected
        assert injected == [("key", "c", True, "c"), ("key", "c", False, "c"), ("move", -3, 4), ("scroll", 1.0, 0.0)], injected

        time.sleep(2.5)  # longer than the watchdog: only the PC's acks keep the link up
        assert client.connected and client.redirecting, "the idle link was dropped"

        assert server.send_home(), "the PC could not send input home"
        assert wait_for(lambda: not client.redirecting), "the PC's switch did not bring input home"
        assert grabs == [True, False], grabs
        assert wait_for(lambda: focus[-1] == "mac"), focus
    finally:
        client.stop()
        server.stop()
    print("ok")


if __name__ == "__main__":
    main()
