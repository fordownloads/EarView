#!/usr/bin/env python3
"""Soulear / i4season WiFi otoscope backend.

Control and video share one UDP socket.  On the tested firmware (HKV07F)
the camera streams video back to the source port of the open-video command
and ignores the picPort field, so a separate video socket never receives
anything.  Binding the socket to PIC_PORT and passing the same port in the
payload works for both behaviours.

Protocol:
  control UDP camera:10005, magic 0xFFEEFFEE, 12-byte little-endian header
  open video UDP camera:10006, payload u16 picPort, u16 audioPort, u32 clientId
  video UDP local port, 16/28-byte header + JPEG chunks
  LED UDP 10005, command 0x000A, payload op/status/brightness
"""
import os
import random
import socket
import struct
import threading
import time
import traceback

from PySide6.QtCore import QObject, Signal

from protocol import jpeg_end

SOULEAR_HOST = "192.168.1.1"
CMD_PORT = 10005
VIDEO_CMD_PORT = 10006
PIC_PORT = 40000
MAGIC = 0xFFEEFFEE

CMD_DEVINFO = 0x0001
CMD_LICGET = 0x0002
CMD_OPEN_VIDEO = 0x0004
CMD_LED = 0x000A

HEADER_SIZE = 12

DEBUG = bool(os.environ.get("SOULEAR_DEBUG"))
DEBUG_LOG = os.environ.get("SOULEAR_DEBUG_LOG", "soulear_debug.log")


def _log(msg):
    if not DEBUG:
        return
    try:
        with open(DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def _request_packet(seq, cmd, payload=b""):
    return struct.pack("<IHHBBH", MAGIC, seq & 0xFFFF, cmd & 0xFFFF,
                       1, 0, len(payload)) + payload


def _parse_reply(data, seq, cmd):
    if len(data) < HEADER_SIZE:
        return None
    magic, rseq, rcmd, req, status, plen = struct.unpack_from("<IHHBBH", data, 0)
    if magic != MAGIC or rseq != (seq & 0xFFFF) or rcmd != (cmd & 0xFFFF):
        return None
    return status, data[HEADER_SIZE:HEADER_SIZE + plen]


def _parse_devinfo(payload):
    if len(payload) < 0x80:
        return None

    def cstr(off, maxlen):
        raw = payload[off:off + maxlen].split(b"\x00", 1)[0]
        return raw.decode("latin-1", "ignore").strip()

    status = payload[0x77] | (payload[0x78] << 8)
    return {
        "vendor": cstr(0x01, 32),
        "product": cstr(0x21, 32),
        "firmware": cstr(0x41, 16),
        "ssid": cstr(0x51, 32),
        "battery": max(0, min(100, status >> 9)),
    }


class SoulearFrameAssembler:
    def __init__(self):
        self.frame_id = -1
        self.last_seq = -1
        self.broken = False
        self.chunks = []

    def feed(self, pkt):
        if not pkt:
            return None
        ptype = pkt[0]
        hsize = 16 if ptype == 1 else (28 if ptype == 6 else 0)
        if not hsize or len(pkt) < hsize:
            return None
        seq = pkt[1]
        fid = pkt[2]
        last = pkt[3] != 0
        count = pkt[4]
        if fid != self.frame_id:
            self.frame_id = fid
            self.chunks = []
            self.broken = False
        elif seq != ((self.last_seq + 1) & 0xFF):
            self.broken = True
        self.last_seq = seq
        self.chunks.append(pkt[hsize:])
        if not last:
            return None

        complete = (not self.broken) and len(self.chunks) == count
        data = b"".join(self.chunks)
        self.frame_id = -1
        self.chunks = []
        self.broken = False
        if not complete or len(data) < 2 or data[0] != 0xFF or data[1] != 0xD8:
            return None
        end = jpeg_end(data, 0)
        if end is not None:
            data = data[:end]
        return data


def local_ipv4_addresses():
    """All local IPv4 addresses, most useful first for camera probing."""
    addrs = []
    try:
        for a in socket.gethostbyname_ex(socket.gethostname())[2]:
            if a and a not in addrs:
                addrs.append(a)
    except OSError:
        pass
    if "127.0.0.1" not in addrs:
        addrs.append("127.0.0.1")
    return addrs


def _probe_candidates(host, local_ip=None):
    if local_ip:
        return [local_ip]
    prefix = ".".join(host.split(".")[:3]) + "."
    addrs = local_ipv4_addresses()
    same = [a for a in addrs if a.startswith(prefix)]
    other = [a for a in addrs if a not in same]
    return same + other


def probe_soulear(host=SOULEAR_HOST, local_ip=None, timeout=0.6):
    """Return the local IPv4 that can reach the camera, or None.

    Binding the probe to a specific local address is important when another
    interface owns the same subnet (for example Ethernet 192.168.1.60 and the
    Soulear AP 192.168.1.10 both use 192.168.1.0/24).
    """
    for lip in _probe_candidates(host, local_ip):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.15)
        try:
            s.bind((lip, 0))
        except OSError:
            s.close()
            continue
        try:
            seq = random.randint(1, 0xFFFF)
            pkt = _request_packet(seq, CMD_DEVINFO)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    s.sendto(pkt, (host, CMD_PORT))
                except OSError:
                    break
                try:
                    data, _ = s.recvfrom(2048)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if _parse_reply(data, seq, CMD_DEVINFO) is not None:
                    return lip
        finally:
            s.close()
    return None


class SoulearWorker(QObject):
    frameReady = Signal(bytes)
    connectionChanged = Signal(bool)
    batteryChanged = Signal(int, bool)
    modelChanged = Signal(str)
    lampChanged = Signal(bool)

    def __init__(self, host=SOULEAR_HOST, local_ip=None):
        super().__init__()
        self.host = host
        self.local_ip = local_ip
        self._stop = threading.Event()
        self._sock = None
        self._local_port = 0
        self._req_lock = threading.Lock()
        self._pending = {}
        self._seq = random.randint(1, 0xFFFF)
        self._client_id = random.getrandbits(32)
        self._lamp_on = False
        self._licensed = False
        self._model = "Soulear"
        self._threads = []
        self._assembler = SoulearFrameAssembler()
        self._connected = False
        self._reported = False
        self._last_false = 0.0
        self._last_video = 0.0
        self._started = 0.0

    # ------------------------------------------------------------------
    def start(self):
        bind_ip = self.local_ip or ""
        self._sock = self._new_socket(PIC_PORT, bind_ip=bind_ip, timeout=0.2)
        if self._sock is None:
            self._sock = self._new_socket(0, bind_ip=bind_ip, timeout=0.2)
        if self._sock is None and bind_ip:
            self._sock = self._new_socket(0, timeout=0.2)
        if self._sock is None:
            self.connectionChanged.emit(False)
            return
        self._local_port = self._sock.getsockname()[1]
        self._started = time.monotonic()
        _log("start host=%s local=%s port=%d" % (self.host, bind_ip, self._local_port))
        self.modelChanged.emit("Soulear")
        t = threading.Thread(target=self._reader_loop, name="soulear-reader", daemon=True)
        t.start()
        self._threads.append(t)
        # Vendor sequence: devinfo, licence, LED read, then open video.
        try:
            self._query_devinfo()
            self._request(CMD_LICGET, b"", CMD_PORT, attempts=3, timeout=0.15)
            self._licensed = True
            self._query_led()
        except Exception:
            _log("handshake exception:\n" + traceback.format_exc())
        for fn, name in ((self._monitor_loop, "soulear-monitor"),
                         (self._command_loop, "soulear-cmd")):
            t = threading.Thread(target=fn, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self):
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def retry(self):
        self._open_video()

    @staticmethod
    def _new_socket(port, bind_ip="", timeout=0.2, rcvbuf=8 << 20):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if rcvbuf:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        try:
            s.bind((bind_ip, port))
        except OSError:
            s.close()
            return None
        s.settimeout(timeout)
        return s

    # ------------------------------------------------------------------
    def _request(self, cmd, payload=b"", port=CMD_PORT, attempts=8, timeout=0.1):
        sock = self._sock
        if sock is None:
            return None
        with self._req_lock:
            self._seq = (self._seq + 1) & 0xFFFF
            seq = self._seq
            pkt = _request_packet(seq, cmd, payload)
            ev = threading.Event()
            box = [None]
            self._pending[seq] = (ev, box)
            try:
                for _ in range(attempts):
                    try:
                        sock.sendto(pkt, (self.host, port))
                    except OSError:
                        return None
                    if ev.wait(timeout):
                        return box[0]
                return None
            finally:
                self._pending.pop(seq, None)

    def _query_devinfo(self):
        body = self._request(CMD_DEVINFO)
        info = _parse_devinfo(body) if body else None
        if not info:
            return
        if info["product"]:
            self._model = info["product"]
            self.modelChanged.emit(self._model)
        self.batteryChanged.emit(info["battery"], False)

    def _query_led(self):
        body = self._request(CMD_LED, bytes([1, 0, 0]))
        if body and len(body) >= 3:
            self._lamp_on = body[1] != 0
            self.lampChanged.emit(self._lamp_on)

    def _set_led(self, on):
        body = self._request(CMD_LED, bytes([0x11, 1 if on else 0, 100 if on else 0]))
        if body and len(body) >= 3:
            self._lamp_on = body[1] != 0
            self.lampChanged.emit(self._lamp_on)

    def _open_video(self):
        payload = struct.pack("<HHI", self._local_port, 0, self._client_id)
        result = self._request(CMD_OPEN_VIDEO, payload, VIDEO_CMD_PORT, attempts=4)
        _log("open video result=%r" % (None if result is None else len(result),))
        if not self._licensed:
            self._request(CMD_LICGET, b"", CMD_PORT, attempts=2, timeout=0.1)
            self._licensed = True

    def request_lamp_toggle(self):
        threading.Thread(target=lambda: self._set_led(not self._lamp_on),
                         daemon=True).start()

    # ------------------------------------------------------------------
    def _reader_loop(self):
        assembler = self._assembler
        while not self._stop.is_set():
            sock = self._sock
            if sock is None:
                break
            try:
                data, _addr = sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if len(data) >= HEADER_SIZE and data[:4] == struct.pack("<I", MAGIC):
                magic, rseq, rcmd, req, status, plen = struct.unpack_from(
                    "<IHHBBH", data, 0)
                item = self._pending.get(rseq)
                if item is not None and rcmd is not None:
                    ev, box = item
                    box[0] = data[HEADER_SIZE:HEADER_SIZE + plen] if status == 0 else None
                    ev.set()
                continue
            jpeg = assembler.feed(data)
            if jpeg is None:
                continue
            self._last_video = time.monotonic()
            if not self._connected:
                self._connected = True
                self._reported = True
                self._last_false = 0.0
                self.connectionChanged.emit(True)
            self.frameReady.emit(jpeg)

    def _monitor_loop(self):
        last_open = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now - self._last_video > 1.0 and now - last_open > 1.0:
                self._open_video()
                last_open = now
            if not self._connected and not self._reported and now - self._started > 2.5 \
                    and now - self._last_false > 2.5:
                self.connectionChanged.emit(False)
                self._last_false = now
                self._reported = True
            if self._connected and now - self._last_video > 3.0:
                self._connected = False
                self._reported = True
                self.connectionChanged.emit(False)
                self._last_false = now
            self._stop.wait(0.2)

    def _command_loop(self):
        try:
            last_dev = time.monotonic()
            while not self._stop.is_set():
                now = time.monotonic()
                if now - last_dev >= 10.0:
                    self._query_devinfo()
                    last_dev = now
                self._stop.wait(0.25)
        except Exception:
            _log("command exception:\n" + traceback.format_exc())
