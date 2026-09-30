"""The link from this machine to the PC's receiver: this side's half of what mac_app/bridge.py
does, speaking the same wire. It connects, says hello, sends input with a sequence number while
redirecting, pings when idle, and drops back to local input the moment the PC stops answering.
"""

import logging
import queue
import socket
import struct
import threading
import time

import protocol
import return_edge

LOGGER = logging.getLogger(__name__)
CONNECT_TIMEOUT_SECONDS = 1.0
AUTH_TIMEOUT_SECONDS = 2.0
IO_TIMEOUT_SECONDS = 0.5
ACK_TIMEOUT_SECONDS = 2.0
PING_INTERVAL_SECONDS = 1.0
RECONNECT_SECONDS = 2.0
_CLIPBOARD_SENTINEL = "_local_clipboard"


class Link:
    """`cfg` needs host, port, auth_token, mac_edge and resistance_px. `on_redirect(bool, pin)`
    is called when input leaves or comes home, to grab or release this machine's input; it
    returns False when the grab failed. `on_status(text)` reports the link."""

    def __init__(self, cfg, clipboard, desktop, on_redirect, on_status=None):
        self.cfg = cfg
        self.clipboard = clipboard
        self.desktop = desktop
        self.on_redirect = on_redirect
        self.on_status = on_status or (lambda text: LOGGER.info("%s", text))
        # `on_arrangement(mac_edge, set_at)` when the PC moves the border between the machines.
        self.on_arrangement = None
        # `on_arrival(edge or None, x, y)` when the PC sends input home: where the pointer landed.
        self.on_arrival = None
        self.redirecting = False
        self._sock = None
        self._session = None
        self._sock_lock = threading.Lock()
        self._outbound = queue.Queue(maxsize=2048)
        self._seq = 0
        self._last_inbound = 0.0
        self._last_send = 0.0
        self._stop = threading.Event()

    @property
    def connected(self):
        return self._sock is not None

    @property
    def home_edge(self):
        """The PC's edge that leads back here: opposite the edge of this machine that leads there."""
        return return_edge.OPPOSITE[self.cfg.mac_edge]

    def start(self):
        for target, name in ((self._connect_worker, "connect"), (self._reader, "reader"),
                             (self._writer, "writer"), (self._watchdog, "watchdog")):
            threading.Thread(target=target, name=f"Beamer-{name}", daemon=True).start()

    def reconnect(self):
        """Drop the connection so the next one uses the settings as they are now."""
        self._fail("the pairing changed")

    def stop(self):
        self.set_redirecting(False)
        self._stop.set()
        self._drop("stopped")

    # -- switching -----------------------------------------------------------------------------

    def set_redirecting(self, value, edge=None, offset=None):
        """`edge` and `offset` say where on the PC a crossing arrives; absent for the shortcut."""
        value = bool(value)
        if value == self.redirecting:
            return False
        if value:
            if not self.connected:
                LOGGER.warning("cannot switch: %s", "the PC is not connected")
                return False
            if not self.on_redirect(True, self.desktop.cursor_position()):
                return False
            self.redirecting = True
            self._control({"type": _CLIPBOARD_SENTINEL, "data": {}})
            self._control(protocol.focus_msg("windows", edge=edge, offset=offset,
                                             return_edge=edge or self.home_edge,
                                             resistance_px=int(self.cfg.resistance_px)))
            LOGGER.info("input sent to the PC")
        else:
            self.redirecting = False
            self.on_redirect(False, None)
            # Sent although redirecting is off: control messages pass the gate.
            self._control(protocol.focus_msg("mac"))
            LOGGER.info("input back on this machine")
        return True

    def send_input(self, message):
        """One input event, dropped unless input is on the PC."""
        if self.redirecting:
            try:
                self._outbound.put_nowait(message)
            except queue.Full:
                self._fail("outbound queue full")

    def send_arrangement(self, mac_edge, set_at):
        self._send(protocol.arrangement_msg(mac_edge, set_at))

    def _control(self, message):
        message["_control"] = True
        self._outbound.put(message)

    # -- the connection ------------------------------------------------------------------------

    def _connect_worker(self):
        while not self._stop.is_set():
            if not self.connected and self.cfg.host and self.cfg.auth_token:
                self._connect_once()
            self._stop.wait(RECONNECT_SECONDS)

    def _connect_once(self):
        host, port, token = self.cfg.host, int(self.cfg.port), self.cfg.auth_token
        sock = None
        try:
            sock = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_SECONDS)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            session = protocol.SecureSession(token)
            sock.sendall(session.preamble())
            sock.settimeout(AUTH_TIMEOUT_SECONDS)
            protocol.recv_preamble(sock, session)
            protocol.send_msg(sock, session, protocol.hello_msg(
                return_edge=self.home_edge, resistance_px=int(self.cfg.resistance_px)))
            try:
                reply = protocol.recv_msg(sock, session)
            except (protocol.ConnectionClosed, protocol.AuthenticationError):
                raise protocol.ProtocolError("the PC refused the token; pair again") from None
            data = reply.get("data") if reply.get("type") == protocol.MSG_WELCOME else None
            if not isinstance(data, dict) or data.get("error") or data.get("version") != protocol.PROTOCOL_VERSION:
                raise protocol.ProtocolError(f"the PC did not welcome this machine: {data!r}; update both apps")
            sock.settimeout(IO_TIMEOUT_SECONDS)
        except (OSError, protocol.ProtocolError) as exc:
            if sock is not None:
                sock.close()
            self.on_status(f"not connected to {host}:{port}: {exc}")
            return False
        self.clipboard.forget_sync()
        now = time.monotonic()
        with self._sock_lock:
            self._sock, self._session = sock, session
            self._last_inbound = self._last_send = now
        self.on_status(f"connected to the PC at {host}:{port}")
        return True

    def _send(self, message):
        with self._sock_lock:
            sock, session = self._sock, self._session
            if sock is None:
                return False
            try:
                protocol.send_msg(sock, session, message)
                self._last_send = time.monotonic()
                return True
            except (OSError, protocol.ProtocolError) as exc:
                failure = exc
        self._fail(f"send failed: {failure}", sock)
        return False

    def _writer(self):
        while not self._stop.is_set():
            try:
                message = self._outbound.get(timeout=0.2)
            except queue.Empty:
                continue
            if message.pop("_control", False):
                if message["type"] == _CLIPBOARD_SENTINEL:
                    self._send_clipboard()
                else:
                    self._send(message)
            elif self.redirecting:
                self._seq += 1
                self._send({"type": message["type"], "data": dict(message.get("data", {}), seq=self._seq)})

    def _send_clipboard(self):
        text, image = self.clipboard.changed_contents()
        if text and len(text.encode("utf-8")) > protocol.CLIPBOARD_MAX_BYTES:
            text = None
        if image is not None and len(image) > protocol.CLIPBOARD_IMAGE_MAX_BYTES:
            image = None
        if text or image is not None:
            self._send(protocol.clipboard_msg(text or None, image))

    def _reader(self):
        buffer, current = bytearray(), None
        while not self._stop.is_set():
            sock, session = self._sock, self._session
            if sock is None:
                self._stop.wait(0.1)
                continue
            if sock is not current:
                buffer, current = bytearray(), sock
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                continue
            except OSError as exc:
                self._fail(f"receive failed: {exc}", sock)
                continue
            if not chunk:
                self._fail("the PC closed the connection", sock)
                continue
            buffer.extend(chunk)
            try:
                while len(buffer) >= protocol.HEADER_SIZE:
                    (length,) = struct.unpack_from(">I", buffer)
                    if not 1 <= length <= protocol.MAX_FRAME_BYTES:
                        raise protocol.ProtocolError(f"invalid frame length {length}")
                    if len(buffer) < protocol.HEADER_SIZE + length:
                        break
                    body = bytes(buffer[protocol.HEADER_SIZE:protocol.HEADER_SIZE + length])
                    del buffer[:protocol.HEADER_SIZE + length]
                    self._last_inbound = time.monotonic()
                    self._inbound(session.open(body))
            except Exception as exc:
                self._fail(f"bad frame from the PC: {exc}", sock)

    def _inbound(self, message):
        kind, data = message.get("type"), message.get("data")
        if kind == protocol.MSG_CLIPBOARD:
            text = data.get("text") if isinstance(data, dict) else None
            text = text if isinstance(text, str) and text and len(text.encode("utf-8")) <= protocol.CLIPBOARD_MAX_BYTES else None
            image = protocol.clipboard_image(data)
            if text is not None or image is not None:
                self.clipboard.set_contents(text, image)
        elif kind == protocol.MSG_SWITCH and isinstance(data, dict) and data.get("target") == "mac":
            self._switch_home(data.get("edge"), data.get("offset"))
        elif kind == protocol.MSG_ARRANGEMENT and self.on_arrangement is not None:
            read = protocol.read_arrangement(data)
            if read is not None:
                self.on_arrangement(*read)

    def _switch_home(self, edge, offset):
        """The PC's pointer was pushed back through its edge: come home, landing where it left."""
        self.set_redirecting(False)
        crossed = edge in return_edge.EDGES and isinstance(offset, (int, float)) and not isinstance(offset, bool)
        if crossed:
            x, y = return_edge.arrival_position(self.desktop.monitors(), edge, offset)
            self.desktop.set_cursor_position(x, y)
        else:
            x, y = self.desktop.cursor_position()
        if self.on_arrival is not None:
            self.on_arrival(edge if crossed else None, x, y)

    def _watchdog(self):
        while not self._stop.wait(0.1):
            if self._sock is None:
                continue
            now = time.monotonic()
            if now - self._last_inbound > ACK_TIMEOUT_SECONDS:
                self._fail("the PC stopped responding")
            elif now - self._last_send >= PING_INTERVAL_SECONDS:
                self._send(protocol.ping_msg())

    def _fail(self, reason, sock=None):
        """Every failure ends here: input comes home first, then the connection goes."""
        if self.redirecting:
            self.redirecting = False
            self.on_redirect(False, None)
            LOGGER.warning("input returned to this machine: %s", reason)
        self._drop(reason, sock)

    def _drop(self, reason, sock=None):
        with self._sock_lock:
            if sock is not None and sock is not self._sock:
                return
            sock, self._sock, self._session = self._sock, None, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
            self.on_status(f"disconnected: {reason}")
