"""The PC names the machine it is paired with: a Mac, or a Linux computer that says so in its hello.
Nothing the window shows may say "Mac" once the peer is a Linux computer, and a Mac, which never
says what it is, keeps every word it had."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import app_config
import peer
import protocol
import sender

try:
    from PySide6.QtWidgets import QAbstractButton, QApplication, QLabel, QWidget

    import kvm_bridge_win
    import theme
except ImportError:  # PySide6 is only in the Windows venv
    kvm_bridge_win = None


class PeerTest(unittest.TestCase):
    def test_a_hello_that_names_nothing_is_a_mac(self):
        self.assertEqual(peer.platform_of(protocol.hello_msg("left", 120)["data"]), "mac")
        self.assertEqual(peer.platform_of({"platform": "haiku"}), "mac")
        self.assertEqual(peer.platform_of(protocol.hello_msg("left", 120, platform="linux")["data"]), "linux")
        self.assertIs(peer.of("linux"), peer.LINUX)
        self.assertIs(peer.of(None), peer.MAC)

    def test_the_platform_is_saved_and_an_unknown_one_reads_as_a_mac(self):
        config = app_config.config_from_dict({"host": "", "port": 24820, "auth_token": "t" * 43, "peer_platform": "linux"})
        self.assertEqual(app_config.config_to_dict(config)["peer_platform"], "linux")
        config = app_config.config_from_dict({"host": "", "port": 24820, "auth_token": "t" * 43, "peer_platform": "beos"})
        self.assertEqual(config.peer_platform, "mac")

    def test_a_linux_computer_always_gets_keys_by_position(self):
        link = sender.MacSender()
        link._config = SimpleNamespace(modifier_style="semantic")
        self.assertEqual(link._wire_name("cmd"), "cmd", "a Mac on Same shortcuts gets Ctrl as Command")
        link.peer = peer.LINUX
        self.assertEqual(link._wire_name("cmd"), "ctrl", "Ctrl must arrive on Linux as Ctrl")

    def test_the_senders_status_names_the_peer(self):
        link = sender.MacSender()
        self.assertEqual(link.status, "Not connected to the Mac")
        link.peer = peer.LINUX
        self.assertEqual(link.peer.say(sender.NOT_CONNECTED), "Not connected to the Linux computer")


@unittest.skipIf(kvm_bridge_win is None, "needs PySide6")
class WindowNamesThePeerTest(unittest.TestCase):
    """The real window, built offscreen as test_pairing_window builds it."""

    def window(self, platform):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        self.app = QApplication.instance() or QApplication([])
        theme.init_fonts()
        self.config = Path(tempfile.mkdtemp()) / "config.json"
        self.config.write_text(json.dumps({
            "host": "192.0.2.20", "port": 24820, "auth_token": "t" * 43, "paired_with": "pop-os",
            "mac_host": "192.0.2.30", "mac_return_edge": "left", "peer_platform": platform,
        }))
        window = kvm_bridge_win.WindowsApplication(self.config)
        self.addCleanup(window.deleteLater)
        return window

    @staticmethod
    def naming_mac(texts):
        """The texts that name a Mac, not counting pairing's "Mac or Linux computer", said before
        anything is known about who will type the code."""
        return [text for text in texts if "Mac" in text.replace(peer.EITHER, "")]

    def texts(self, window):
        """Every word the window, its drawings and the tray menu can show or read out."""
        found = []
        for widget in (window, *window.findChildren(QWidget)):
            if isinstance(widget, (QLabel, QAbstractButton)):
                found.append(widget.text())
            found += [widget.accessibleName(), widget.accessibleDescription(), widget.toolTip()]
        found += [action.text() for action in window.tray.contextMenu().actions()]
        found += [window.arrangement_diagram.peer_label, window.resistance_strip.peer_label]
        return [text for text in found if text]

    def test_nothing_says_mac_to_someone_paired_with_linux(self):
        window = self.window("linux")
        window._refresh_window()
        self.assertEqual(self.naming_mac(self.texts(window)), [])
        self.assertIn("Send input to your Linux computer", self.texts(window))
        self.assertIn("Your Linux", self.texts(window))
        self.assertFalse(window.modifier_choice.view.isVisibleTo(window), "Linux has no modifier style to choose")

    def test_a_mac_keeps_its_words(self):
        window = self.window("mac")
        window._refresh_window()
        self.assertIn("Send input to your Mac", self.texts(window))
        self.assertIn("Your Mac drives this PC", self.texts(window))

    def test_the_hello_renames_everything_at_once_and_is_saved(self):
        window = self.window("mac")
        window._on_learned("192.0.2.30", "left", 120, "linux")
        window._refresh_window()
        self.assertEqual(self.naming_mac(self.texts(window)), [])
        self.assertEqual(json.loads(self.config.read_text())["peer_platform"], "linux")
        self.assertEqual(window.server.peer_name, "Linux computer")
        # And back: a Mac's hello names nothing.
        window._on_learned("192.0.2.30", "left", 120, None)
        self.assertIn("Your Mac drives this PC", self.texts(window))


if __name__ == "__main__":
    unittest.main()
