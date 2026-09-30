"""The Linux app's window and tray icon, built from the Windows app's own Qt pieces: its theme,
widgets, the edge glow, and the crossing effects' overlay and previews. Everything Beamer does is
in beamer_linux.App; this only shows it and changes its settings.

Closing the window keeps Beamer running in the tray. A second launch shows the first one's window
instead of starting another.
"""

import os
import shlex
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

import app_config
import effects
import motion
import pages_win
import pairing
import return_edge
import theme
import tokens
import widgets
from edge_glow import EdgeGlow, PreviewLoop
from effect_overlay import EffectOverlay, logical_point
from effect_previews import EffectStill, TileHover

PAGES = (
    ("overview", "Overview", "Where input is right now, and the controls you reach for every day. Pair with your PC here first."),
    ("crossing", "Crossing", "Where your PC is, how hard the edge pushes back, and the key that switches."),
    ("pointer", "Pointer", "How fast your PC's pointer and scroll move on this screen."),
    ("design", "Design", "How crossing looks on this screen: the light as you push toward your PC, and where the pointer lands."),
    ("connection", "Connection", "Your PC's address and port, the shared token, and the log."),
)
EDGE_NAMES = (("left", "Left"), ("right", "Right"), ("top", "Above"), ("bottom", "Below"))
WHERE = {
    "here": ("signal", "Input is here"),
    "pc": ("signal", "Input is on your PC"),
    "driven": ("amber", "Your PC is driving this machine"),
}
AUTOSTART_PATH = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "autostart" / "beamer.desktop"


def launch_command():
    """What starts this Beamer again: the AppImage, the installed binary, or this script."""
    if os.environ.get("APPIMAGE"):
        return [os.environ["APPIMAGE"]]
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, str(Path(sys.argv[0]).resolve())]


def icon_path(root):
    for candidate in (root / "Beamer.png", Path("/usr/share/icons/hicolor/256x256/apps/beamer.png")):
        if candidate.exists():
            return candidate
    return None


def set_autostart(on, root):
    if not on:
        AUTOSTART_PATH.unlink(missing_ok=True)
        return
    AUTOSTART_PATH.parent.mkdir(parents=True, exist_ok=True)
    icon = icon_path(root)
    AUTOSTART_PATH.write_text(
        "[Desktop Entry]\nType=Application\nName=Beamer\nComment=One keyboard and mouse for this machine and your PC\n"
        f"Exec={shlex.join(launch_command() + ['--hidden'])}\n"
        + (f"Icon={icon}\n" if icon else "")
        + "X-GNOME-Autostart-enabled=true\n"
    )


class Bridge(QObject):
    """Worker threads to the GUI thread."""

    changed = Signal()
    pressure = Signal(str, float, bool, object)
    arrived = Signal(str, str, float, float)
    paired = Signal(object)


class Window(QWidget):
    def __init__(self, app, version, log_path, trigger_keys, root):
        super().__init__()
        self.app, self.cfg = app, app.cfg
        self.version, self.log_path, self.trigger_keys, self.root = version, log_path, trigger_keys, root
        self.glow = None
        self.effects = None
        self.discovery = None
        self.bridge = Bridge()
        self.bridge.changed.connect(self.reflect)
        self.bridge.pressure.connect(self._on_pressure)
        self.bridge.arrived.connect(self._on_arrival)
        self.bridge.paired.connect(self._on_paired)
        app.on_change = self.bridge.changed.emit
        app.on_pressure = self.bridge.pressure.emit
        app.on_arrival = self.bridge.arrived.emit

        theme.set_dark(theme.wants_dark(self.cfg.appearance, theme.system_dark()))
        self.setWindowTitle("Beamer")
        self.resize(820, 720)
        self.setMinimumSize(*tokens.MIN_WINDOW["windows"])
        icon = icon_path(root)
        if icon:
            self.setWindowIcon(QIcon(str(icon)))
        self.preview_loop = PreviewLoop(self)
        self.tile_hover = TileHover(self.preview_loop, self)
        self._build()
        QApplication.instance().setStyleSheet(theme.stylesheet())
        theme.watch_system(lambda: self._set_appearance(self.cfg.appearance))
        self._build_tray()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_pcs)
        self._timer.start(1000)
        self.reflect()

    # -- the window --------------------------------------------------------------------------------

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        row = QHBoxLayout()
        row.setSpacing(0)
        self.sidebar = widgets.Sidebar(PAGES, self._select, foot=f"Beamer {self.version}\nfor Linux")
        self.sidebar.setFixedWidth(theme.SIDEBAR_WIDTH)
        row.addWidget(self.sidebar)
        row.addWidget(widgets.rule())
        self.stack = QStackedWidget()
        row.addWidget(self.stack, 1)
        outer.addLayout(row, 1)
        footer = QFrame()
        footer.setProperty("vernier", "commit")
        footer_row = QHBoxLayout(footer)
        footer_row.setContentsMargins(16, 10, 16, 10)
        footer_row.addWidget(widgets.label(
            "Changes apply as you make them. Closing this window keeps Beamer running in the tray.", "note", wrap=True))
        outer.addWidget(footer)
        builders = {"overview": self._overview, "crossing": self._crossing, "pointer": self._pointer,
                    "design": self._design, "connection": self._connection}
        self._pages = {}
        for key, name, purpose in PAGES:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            page = QWidget()
            page.setProperty("vernier", "plain")
            layout = QVBoxLayout(page)
            layout.setContentsMargins(28, 24, 28, 24)
            layout.setSpacing(16)
            heading = widgets.label(name.upper(), "heading")
            heading.setFont(theme.font(theme.HEADING, 700))
            layout.addWidget(heading)
            layout.addWidget(widgets.label(purpose, "note", wrap=True))
            builders[key](layout)
            layout.addStretch(1)
            scroll.setWidget(page)
            self._pages[key] = self.stack.addWidget(scroll)
        self._select("overview")

    def _select(self, key):
        self.stack.setCurrentIndex(self._pages[key])
        self.sidebar.select(key)

    @staticmethod
    def _row(*items):
        row = QWidget()
        row.setProperty("vernier", "plain")
        column = QVBoxLayout(row)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)
        for item in items:
            (column.addLayout if isinstance(item, QHBoxLayout) else column.addWidget)(item)
        return row

    def _switch(self, text, checked, on_toggle):
        switch = widgets.Switch(text)
        switch.setFont(theme.font(theme.TYPE["body"]))
        switch.setChecked(checked)
        switch.toggled.connect(on_toggle)
        return switch

    def _set(self, name, value):
        setattr(self.cfg, name, value)
        self.app.apply()

    # -- Overview ------------------------------------------------------------------------------

    def _overview(self, layout):
        module = widgets.Module("Input")
        head = QHBoxLayout()
        self.where_led = widgets.Led()
        head.addWidget(self.where_led, 0, Qt.AlignmentFlag.AlignVCenter)
        self.where_word = widgets.label("", "heading")
        self.where_word.setFont(theme.font(theme.HEADING, 600))
        head.addWidget(self.where_word, 1)
        module.body.addLayout(head)
        self.link_line = widgets.label("", "note", wrap=True)
        self.receiver_line = widgets.label("", "note", wrap=True)
        module.body.addWidget(self.link_line)
        module.body.addWidget(self.receiver_line)
        buttons = QHBoxLayout()
        self.send_button = QPushButton("Send input to your PC")
        self.send_button.setProperty("vernier", "primary")
        self.send_button.clicked.connect(self.app.toggle)
        buttons.addWidget(self.send_button)
        buttons.addStretch(1)
        module.body.addLayout(buttons)
        self.pause_switch = self._switch("Pause crossing", False, self._pause)
        module.body.addWidget(self.pause_switch)
        module.body.addWidget(widgets.label(
            "Paused, the pointer stays on this screen at the edges; the shortcut still switches.", "small", wrap=True))
        layout.addWidget(module)

        module = widgets.Module("Directions")
        self.send_switch = self._switch("This machine drives your PC", self.cfg.send_to_windows,
                                        lambda on: self._set("send_to_windows", on))
        self.drive_switch = self._switch("Your PC drives this machine", self.cfg.allow_windows_to_drive,
                                         lambda on: self._set("allow_windows_to_drive", on))
        module.body.addWidget(self.send_switch)
        module.body.addWidget(self.drive_switch)
        layout.addWidget(module)

        layout.addWidget(self._pairing_module())

        module = widgets.Module("Start")
        module.body.addWidget(self._switch("Start Beamer when you log in", AUTOSTART_PATH.exists(),
                                           lambda on: set_autostart(on, self.root)))
        layout.addWidget(module)

    def _pairing_module(self):
        module = widgets.Module("Pairing")
        self.paired_line = widgets.label("", "note", wrap=True)
        module.body.addWidget(self.paired_line)
        module.body.addWidget(widgets.label(
            "On your PC, open Beamer and press Pair a Mac. It appears below while its code is on screen.",
            "small", wrap=True))
        self.pc_list = QListWidget()
        self.pc_list.setMaximumHeight(96)
        module.body.addWidget(self.pc_list)
        find = QHBoxLayout()
        self.find_entry = QLineEdit()
        self.find_entry.setProperty("vernier", "entry")
        self.find_entry.setPlaceholderText("PC not listed? Its address")
        find.addWidget(self.find_entry, 1)
        find_button = QPushButton("Find")
        find_button.clicked.connect(lambda: self.discovery and self.discovery.find(self.find_entry.text()))
        find.addWidget(find_button)
        module.body.addLayout(find)
        code = QHBoxLayout()
        self.code_entry = QLineEdit()
        self.code_entry.setProperty("vernier", "entry")
        self.code_entry.setPlaceholderText("Six-digit code")
        self.code_entry.setMaxLength(6)
        self.code_entry.returnPressed.connect(self._pair)
        code.addWidget(self.code_entry, 1)
        self.pair_button = QPushButton("Pair")
        self.pair_button.setProperty("vernier", "primary")
        self.pair_button.clicked.connect(self._pair)
        code.addWidget(self.pair_button)
        module.body.addLayout(code)
        self.pair_note = widgets.label("", "small", wrap=True)
        module.body.addWidget(self.pair_note)
        return module

    def _refresh_pcs(self):
        if self.discovery is None:
            return
        chosen = self.pc_list.currentItem()
        chosen = chosen.data(Qt.ItemDataRole.UserRole)["address"] if chosen else None
        self.pc_list.clear()
        for pc in self.discovery.pcs():
            showing = " · showing a code" if pc.get("pair_id") else ""
            item = QListWidgetItem(f"{pc['name']}  {pc['address']}{showing}")
            item.setData(Qt.ItemDataRole.UserRole, pc)
            self.pc_list.addItem(item)
            if pc["address"] == chosen or (chosen is None and showing and self.pc_list.currentItem() is None):
                self.pc_list.setCurrentItem(item)

    def _pair(self):
        item = self.pc_list.currentItem()
        code = self.code_entry.text().strip()
        if item is None:
            self.pair_note.setText("Choose your PC in the list first.")
            return
        if not (len(code) == 6 and code.isdigit()):
            self.pair_note.setText("The code is the six digits your PC shows.")
            return
        pc = item.data(Qt.ItemDataRole.UserRole)
        self.pair_button.setEnabled(False)
        self.pair_note.setText(f"Pairing with {pc['name']}…")
        discovery = self.discovery

        def work():
            try:
                token, name = discovery.pair(pc, code)
                self.bridge.paired.emit((pc, token, name))
            except Exception as exc:  # PairingError, or the socket going away
                self.bridge.paired.emit(exc)

        threading.Thread(target=work, name="Beamer-pair", daemon=True).start()

    def _on_paired(self, result):
        self.pair_button.setEnabled(True)
        self.code_entry.clear()
        if isinstance(result, Exception):
            reasons = {pairing.ERROR_REFUSED: "The code was not right. Press Pair a Mac on your PC for a new one.",
                       pairing.ERROR_NOT_PAIRING: "Your PC is no longer showing a code. Press Pair a Mac there again.",
                       "no_answer": "Your PC did not answer. Is Beamer open there, with its code showing?"}
            self.pair_note.setText(reasons.get(str(result), f"Pairing failed: {result}"))
            return
        pc, token, name = result
        self.app.paired_with(pc, token, name)
        self.pair_note.setText(f"Paired with {name}.")

    def _pause(self, on):
        self.app.paused = on
        self.reflect()

    # -- Crossing ------------------------------------------------------------------------------

    def _crossing(self, layout):
        module = widgets.Module("Where your PC is")
        self.edge_choice = widgets.Choice(EDGE_NAMES, 4, self.cfg.mac_edge, on_change=self._edge_chosen)
        self.edge_choice.set_names("Where your PC is")
        module.body.addWidget(self.edge_choice.view)
        module.body.addWidget(widgets.label(
            "Push the pointer off this edge of your screen to reach your PC. Your PC learns it too.", "small", wrap=True))
        layout.addWidget(module)

        module = widgets.Module("Resistance")
        self.resistance_value = widgets.label("", "readout")
        resistance = widgets.Ruler("Resistance", 500)
        resistance.setValue(int(self.cfg.resistance_px))
        resistance.valueChanged.connect(lambda value: self.resistance_value.setText(f"{value} px"))
        resistance.sliderReleased.connect(lambda: self._set("resistance_px", resistance.value()))
        self.resistance_value.setText(f"{resistance.value()} px")
        module.body.addWidget(self._row(self.resistance_value, resistance, widgets.label(
            "How far you push past the edge before input crosses, so reaching for something at the edge "
            "does not send you across. It is also what your PC's pointer meets on the way back.", "small", wrap=True)))
        layout.addWidget(module)

        module = widgets.Module("Shortcut")
        self.trigger_choice = widgets.Choice(tuple(self.trigger_keys.items()), 4, self.cfg.trigger_key,
                                             on_change=lambda key: self._set("trigger_key", key))
        self.trigger_choice.set_names("Double-tap")
        module.body.addWidget(self._row(widgets.label("Double-tap", "key"), self.trigger_choice.view, widgets.label(
            "Double-tap this key to send input to your PC and to bring it back. It is never sent to your PC.",
            "small", wrap=True)))
        layout.addWidget(module)

    def _edge_chosen(self, edge):
        self.cfg.mac_edge, self.cfg.arrangement_set_at = edge, int(time.time())
        self.app.apply()
        # Either link will do: whichever is up tells the PC.
        if not self.app.receiver.send_arrangement(edge, self.cfg.arrangement_set_at):
            self.app.link.send_arrangement(edge, self.cfg.arrangement_set_at)

    # -- Pointer -------------------------------------------------------------------------------

    def _pointer(self, layout):
        module = widgets.Module("Your PC's pointer here")
        for name, title in (("pointer_speed", "Pointer speed"), ("scroll_speed", "Scroll speed")):
            value = widgets.label("", "readout")
            ruler = widgets.Ruler(title, 400, minimum=25)
            ruler.setValue(int(round(getattr(self.cfg, name) * 100)))
            value.setText(f"{ruler.value()}%")
            ruler.valueChanged.connect(lambda v, label=value: label.setText(f"{v}%"))
            ruler.sliderReleased.connect(lambda n=name, r=ruler: self._set(n, r.value() / 100.0))
            module.body.addWidget(self._row(widgets.label(title, "key"), value, ruler))
        module.body.addWidget(self._switch("Reverse your PC's scrolling", self.cfg.reverse_scroll,
                                           lambda on: self._set("reverse_scroll", on)))
        module.body.addWidget(widgets.label(
            "Your PC sends what its own mouse did; these scale it on this screen.", "small", wrap=True))
        layout.addWidget(module)

    # -- Design --------------------------------------------------------------------------------

    def _design(self, layout):
        module = widgets.Module("On screen")
        self.glow_switch = self._switch("Animate crossings on this screen", self.cfg.edge_glow, lambda on: self._look())
        self.landing_switch = self._switch("Show where the pointer lands after a switch", self.cfg.shortcut_arrival,
                                           lambda on: self._look())
        module.body.addWidget(self.glow_switch)
        module.body.addWidget(self.landing_switch)
        layout.addWidget(module)

        module = widgets.Module("Style and colour")
        colours = app_config.palette_colours(self.cfg.glow_colour)
        self.stills = {}
        groups = []
        for title, items in pages_win.style_groups():
            groups.append((title, [(style, name, detail, self.stills.setdefault(style, EffectStill(style, colours)))
                                   for style, name, detail in items]))
        style = self.cfg.glow_style if self.cfg.glow_style in app_config.GLOW_STYLES else "glow"
        self.style_choice = widgets.TileGroups(groups, style, on_change=lambda _v: self._look())
        module.body.addWidget(self.style_choice.view)
        for value, picture in self.stills.items():
            self.tile_hover.track(self.style_choice.tile(value), picture)
        self.length_choice = widgets.Choice(effects.LENGTHS, 3, self.cfg.effect_length, on_change=lambda _v: self._look())
        module.body.addWidget(self._row(widgets.label("Length", "key"), self.length_choice.view))
        module.body.addWidget(widgets.label("Colour", "key"))
        colour = self.cfg.glow_colour if self.cfg.glow_colour in app_config.GLOW_COLOURS else "signal"
        self.colour_choice = widgets.SwatchGroups(
            [(title, [(value, name, app_config.palette_colours(value)) for value, name in items])
             for title, items in pages_win.colour_groups()],
            colour, on_change=lambda _v: self._look())
        module.body.addWidget(self.colour_choice.view)
        self.look_module = module
        layout.addWidget(module)

        module = widgets.Module("Appearance")
        choice = widgets.Choice((("system", "System"), ("light", "Light"), ("dark", "Dark")), 3, self.cfg.appearance,
                                on_change=self._appearance_chosen)
        module.body.addWidget(self._row(widgets.label("This window", "key"), choice.view))
        layout.addWidget(module)
        motion.set_shown(self.look_module, self.cfg.edge_glow)

    def _look(self):
        self.cfg.edge_glow = self.glow_switch.isChecked()
        self.cfg.shortcut_arrival = self.landing_switch.isChecked()
        self.cfg.glow_style = self.style_choice.value or "glow"
        self.cfg.glow_colour = self.colour_choice.value or "signal"
        self.cfg.effect_length = self.length_choice.value or "normal"
        motion.set_shown(self.look_module, self.cfg.edge_glow)
        colours = app_config.palette_colours(self.cfg.glow_colour)
        for still in self.stills.values():
            still.set_palette(colours)
            still.set_pace(effects.pace(self.cfg.effect_length))
        self._hide_crossing()
        self.app.apply()

    def _appearance_chosen(self, choice):
        self.cfg.appearance = choice
        self._set_appearance(choice)
        self.app.apply()

    def _set_appearance(self, choice):
        dark = theme.wants_dark(choice, theme.system_dark())
        if dark != theme.is_dark():
            theme.set_dark(dark)
            QApplication.instance().setStyleSheet(theme.stylesheet())
            widgets.refresh_colours(self)

    # -- Connection ----------------------------------------------------------------------------

    def _connection(self, layout):
        module = widgets.Module("Your PC")
        self.address_line = widgets.label("", "mono")
        module.body.addWidget(self._row(widgets.label("Address and port", "key"), self.address_line))
        self.token_line = widgets.label("", "mono")
        self.token_line.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        show = QPushButton("Show token")
        show.setCheckable(True)
        show.toggled.connect(lambda on: (show.setText("Hide token" if on else "Show token"), self.reflect()))
        self.show_token = show
        module.body.addWidget(self._row(widgets.label("Shared token", "key"), self.token_line, show))
        module.body.addWidget(widgets.label(
            "The token is what lets each machine type on the other. Pairing again replaces it.", "small", wrap=True))
        layout.addWidget(module)

        module = widgets.Module("Log")
        module.body.addWidget(widgets.label(str(self.log_path), "mono"))
        open_log = QPushButton("Open the log")
        open_log.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.log_path))))
        module.body.addWidget(open_log)
        layout.addWidget(module)

    # -- state ---------------------------------------------------------------------------------

    def reflect(self):
        """Every status line, from the app as it is now."""
        app, cfg = self.app, self.cfg
        tone, word = WHERE[app.where]
        if app.paused and app.where == "here":
            tone, word = "amber", "Crossing is paused"
        self.where_led.set_tone(tone)
        self.where_word.setText(word)
        self.sidebar.set_link("signal" if app.link.connected else "amber" if app.paired else "off",
                              "Linked" if app.link.connected else "Not linked" if app.paired else "Not paired")
        self.link_line.setText(f"To your PC: {app.link_status}")
        self.receiver_line.setText(f"From your PC: {app.receiver_status}")
        self.send_button.setText("Bring input back" if app.where != "here" else "Send input to your PC")
        self.send_button.setEnabled(app.link.connected or app.where != "here")
        self.paired_line.setText(f"Paired with {cfg.pc_name or cfg.host}." if app.paired else "Not paired yet.")
        self.address_line.setText(f"{cfg.host}:{cfg.port}" if cfg.host else "Not paired yet")
        token = cfg.auth_token or ""
        self.token_line.setText(token if self.show_token.isChecked() else "•" * min(len(token), 24) or "None")
        if hasattr(self, "tray"):
            self.tray.setToolTip(f"Beamer: {word}")
            self.status_action.setText(word)
            self.toggle_action.setText(self.send_button.text())
            self.pause_action.setChecked(app.paused)

    # -- the crossing animations, the Windows app's own ----------------------------------------------

    def _overlay(self, switching=False):
        """The effects' window when the style is one of them (or for a switch's arrival), or None
        for the plain glow."""
        cfg = self.cfg
        if not cfg.edge_glow or (not switching and not pages_win.is_effect(cfg.glow_style)):
            return None
        if self.effects is None:
            self.effects = EffectOverlay()
        if self.effects.failed:
            return None
        self.effects.configure(cfg.glow_style, cfg.glow_colour, cfg.effect_length)
        return None if self.effects.failed or self.effects._fx is None else self.effects

    def _on_pressure(self, edge, pressure, crossed, part):
        if not self.cfg.edge_glow:
            return
        overlay = self._overlay()
        if overlay is not None:
            overlay.push(edge, pressure, crossed, part=part)
            return
        if self.glow is None:
            self.glow = EdgeGlow()
        style = self.cfg.glow_style
        self.glow.configure(style if style in ("glow", "beam") else "glow", self.cfg.glow_colour, self.cfg.effect_length)
        self.glow.set_pressure(edge, pressure, crossed, part)

    def _on_arrival(self, method, edge, x, y):
        if method == "switch":
            overlay = self._overlay(switching=True) if self.cfg.shortcut_arrival else None
            if overlay is not None:
                overlay.switched(logical_point(x, y), self.cfg.shortcut_arrival_style)
            return
        overlay = self._overlay()
        if overlay is not None:
            overlay.arrive(method, edge, logical_point(x, y))
        elif self.effects is not None:
            self.effects.crossed_in()

    def _hide_crossing(self):
        if self.glow is not None:
            self.glow.hide()
        if self.effects is not None:
            self.effects.stop()

    # -- tray and lifetime ---------------------------------------------------------------------------

    def _build_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.tray_missing = True
            return
        self.tray_missing = False
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        menu = QMenu()
        menu.addAction(f"Beamer {self.version}").setEnabled(False)
        self.status_action = menu.addAction("")
        self.status_action.setEnabled(False)
        menu.addSeparator()
        menu.addAction("Open Beamer").triggered.connect(self.show_window)
        self.toggle_action = menu.addAction("Send input to your PC")
        self.toggle_action.triggered.connect(self.app.toggle)
        self.pause_action = menu.addAction("Pause crossing")
        self.pause_action.setCheckable(True)
        self.pause_action.triggered.connect(self.pause_switch.setChecked)
        menu.addSeparator()
        menu.addAction("Quit Beamer").triggered.connect(self.quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: self.show_window() if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray.show()

    def show_window(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def showEvent(self, event):
        # Discovery only while the window is up: it is for pairing and finding the PC.
        if self.discovery is None:
            self.discovery = pairing.Discovery()
            self.discovery.start()
        super().showEvent(event)

    def hideEvent(self, event):
        if self.discovery is not None:
            self.discovery.stop()
            self.discovery = None
        super().hideEvent(event)

    def closeEvent(self, event):
        if self.tray_missing:
            # With no tray to live in, closing is quitting.
            self.quit()
            return
        event.ignore()
        self.hide()

    def quit(self):
        self._hide_crossing()
        self.app.stop()
        if hasattr(self, "tray"):
            self.tray.hide()
        QApplication.instance().quit()


def run(app, hidden, version, log_path, trigger_keys, root):
    qt = QApplication(sys.argv[:1])
    qt.setApplicationName("Beamer")
    qt.setDesktopFileName("beamer")
    qt.setQuitOnLastWindowClosed(False)
    # One Beamer per user: a second launch shows the first one's window and leaves.
    name = f"beamer-linux-{os.getuid()}"
    probe = QLocalSocket()
    probe.connectToServer(name)
    if probe.waitForConnected(300):
        probe.write(b"show")
        probe.waitForBytesWritten(300)
        return 0
    QLocalServer.removeServer(name)
    server = QLocalServer()
    server.listen(name)
    theme.init_fonts()
    qt.setFont(theme.font(theme.TYPE["body"]))
    window = Window(app, version, log_path, trigger_keys, root)
    server.newConnection.connect(lambda: (server.nextPendingConnection(), window.show_window()))
    app.start()
    if not hidden or window.tray_missing or not app.paired:
        window.show_window()
    return qt.exec()
