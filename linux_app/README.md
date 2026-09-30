# Beamer for Linux (X11)

Takes the Mac's place opposite the Windows app, in both directions. It reuses `mac_app`'s
`protocol.py`, `pairing.py`, `receiver.py` and `return_edge.py` unchanged. Everything here is
stdlib Python plus the X11 libraries a desktop already has.

## Needs

- An **X11** session. Wayland is not supported (on Pop!_OS or Ubuntu, pick the Xorg session at login).
- Python 3.10+, `python3-cryptography`, `python3-nacl` and `xclip`:
  `sudo apt install python3-cryptography python3-nacl xclip`
- Beamer 1.4.3 or later on the PC.
- Inbound TCP 24820 and UDP 24821 open on this machine, if a firewall is on:
  `sudo ufw allow 24820/tcp && sudo ufw allow 24821/udp`

## Use

1. On the PC, open Beamer and press **Pair a Mac**. Then, here:
   `python3 linux_app/beamer_linux.py pair` and type the code. Add `--host <PC address>` if the
   PC is not found.
2. Run `python3 linux_app/beamer_linux.py`. Push the pointer through the right-hand edge, or
   double-tap Right Ctrl, to drive the PC. Do the same on the PC's side to come back.
3. On the PC, set Keyboard to **Same positions**, so the PC's Ctrl arrives here as Ctrl.

Settings are in `~/.config/beamer/config.json`: `mac_edge` (the edge of this screen that leads
to the PC), `resistance_px` and `trigger_key` (a wire key name such as `ctrl_r`, `alt_r` or `f12`).

`python3 linux_app/test_link.py` checks the link against the real receiver over loopback.

## Not here yet

No window or tray, no crossing effects, no gestures, no wake-on-LAN, no corner or part-edge
crossing. A character the PC sends that this keymap cannot type (a dead-key accent, say) is
dropped and logged.
