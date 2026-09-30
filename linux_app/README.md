# Beamer for Linux (X11)

Takes the Mac's place opposite the Windows app, in both directions, with the Windows app's own
settings window and crossing animations. It reuses `win_app`'s shared modules (`protocol.py`,
`pairing.py`, `receiver.py`, `return_edge.py`, the effects) and its Qt interface pieces
unchanged. What is Linux-shaped (input capture and injection, clipboard, tray) lives here.

## Needs

- An **X11** session. Wayland is not supported yet: on Pop!_OS or Ubuntu, pick the Xorg session
  from the gear on the login screen.
- `sudo apt install python3-venv libxcb-cursor0 xclip`
- Beamer 1.4.3 or later on the PC.
- Inbound TCP 24820 and UDP 24821 open on this machine, if a firewall is on:
  `sudo ufw allow 24820/tcp && sudo ufw allow 24821/udp`

## Run from source

    python3 -m venv --system-site-packages .venv
    .venv/bin/pip install -r linux_app/requirements-linux.txt
    .venv/bin/python linux_app/beamer_linux.py

The window opens on Overview. On the PC, press **Pair a Mac**, choose the PC in the list here and
type its code. Closing the window keeps Beamer in the tray; Overview can start it at login. On the
PC, set Keyboard to **Same positions**, so the PC's Ctrl arrives here as Ctrl.

Push the pointer through the edge that faces the PC (Crossing sets which), or double-tap Right
Ctrl. The mouse or trackpad you touch always wins: pushing it out while the other machine drives
this one sends that machine's input home first.

Other ways to run it: `--hidden` (tray only, what start at login uses), `--headless` (no window),
`pair` (pair on the command line). Settings are in `~/.config/beamer/config.json`, the log in
`~/.local/state/beamer/beamer.log`.

`python3 linux_app/test_link.py` checks the link against the real receiver over loopback.

## Not here yet

Wayland, gestures, wake-on-LAN, corner and part-of-edge crossing, keys that stay on this machine,
a choice of arrival style for the shortcut (it plays the crossing style's), and pairing with a Mac.
A character the PC sends that this keymap cannot type (a dead-key accent, say) is dropped and logged.
