"""A scripted fake WebSocket server on a socketpair - no ports, no network.

FakeServer plays the server half of one connection in a thread: it reads the client's
upgrade request, answers it (101 with a correct or deliberately wrong accept key, or an
error status with a body), then runs a script of raw frames to send and client frames to
read. Frames the client sends are recorded (unmasked) in `client_frames`.
"""
import re
import socket
import threading
import time

from alerts_bot.solami import websocket as ws


def server_frame(opcode, payload=b"", fin=True, masked=False, rsv=0):
    first = (0x80 if fin else 0x00) | rsv | opcode
    length = len(payload)
    mask_bit = 0x80 if masked else 0x00
    if length < 126:
        header = bytes([first, mask_bit | length])
    elif length < 65536:
        header = bytes([first, mask_bit | 126]) + length.to_bytes(2, "big")
    else:
        header = bytes([first, mask_bit | 127]) + length.to_bytes(8, "big")
    if masked:
        mask = b"\x0a\x0b\x0c\x0d"
        return header + mask + ws.apply_mask(payload, mask)
    return header + payload


def text(payload, fin=True):
    return server_frame(ws.OP_TEXT, payload.encode("utf-8"), fin=fin)


def close_frame(code, reason=""):
    return server_frame(ws.OP_CLOSE, code.to_bytes(2, "big") + reason.encode("utf-8"))


def _recv_exact(sock, n):
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise EOFError("client closed")
        data += chunk
    return data


def read_client_frame(sock):
    first, second = _recv_exact(sock, 2)
    opcode = first & 0x0F
    length = second & 0x7F
    masked = bool(second & 0x80)
    if length == 126:
        length = int.from_bytes(_recv_exact(sock, 2), "big")
    elif length == 127:
        length = int.from_bytes(_recv_exact(sock, 8), "big")
    mask = _recv_exact(sock, 4) if masked else None
    payload = _recv_exact(sock, length)
    if mask:
        payload = ws.apply_mask(payload, mask)
    return opcode, payload, masked


class FakeServer:
    def __init__(self, script=(), status=101, accept_ok=True, extra_headers="", body=b"",
                 with_handshake=b""):
        self.client_sock, self.server_sock = socket.socketpair()
        self.script = list(script)
        self.status = status
        self.accept_ok = accept_ok
        self.extra_headers = extra_headers
        self.body = body
        self.with_handshake = with_handshake
        self.request = ""
        self.address = None
        self.client_frames = []
        self._thread = threading.Thread(target=self._run, daemon=True)

    def create_connection(self, address, timeout=None):
        self.address = address
        self._thread.start()
        return self.client_sock

    def _run(self):
        data = b""
        try:
            while b"\r\n\r\n" not in data:
                chunk = self.server_sock.recv(4096)
                if not chunk:
                    return
                data += chunk
            self.request = data.decode("latin-1")
            key = re.search(r"Sec-WebSocket-Key: (\S+)", self.request).group(1)
            if self.status == 101:
                accept = ws.accept_key(key) if self.accept_ok else "bm90LXRoZS1yaWdodC1rZXk="
                response = ("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                            "Sec-WebSocket-Accept: %s\r\n%s\r\n" % (accept, self.extra_headers)).encode("latin-1")
                self.server_sock.sendall(response + self.with_handshake)
            else:
                response = ("HTTP/1.1 %d Unauthorized\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n"
                            % (self.status, len(self.body))).encode("latin-1")
                self.server_sock.sendall(response + self.body)
                return
            for step in self.script:
                if isinstance(step, bytes):
                    self.server_sock.sendall(step)
                elif step == "read":
                    self.client_frames.append(read_client_frame(self.server_sock))
                elif step == "hangup":
                    self.server_sock.close()
                    return
                elif isinstance(step, float):
                    time.sleep(step)
            self.server_sock.settimeout(2.0)
            while True:
                frame = read_client_frame(self.server_sock)
                self.client_frames.append(frame)
                if frame[0] == ws.OP_CLOSE:          # a real server echoes the close frame
                    self.server_sock.sendall(server_frame(ws.OP_CLOSE, frame[1][:2]))
                    return
        except (EOFError, OSError):
            pass

    def join(self, timeout=3.0):
        self._thread.join(timeout)

    def close(self):
        for sock in (self.client_sock, self.server_sock):
            try:
                sock.close()
            except OSError:
                pass
