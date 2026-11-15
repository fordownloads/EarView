#!/usr/bin/env python3
"""Device worker for the iTiMO / MZ otoscope viewer.

Video: UDP 8031, 24-byte packet header + JPEG fragments.
Control: UDP 50000, 0x9999 command packets.
"""
import json
import socket
import struct
import threading
import time
import traceback

from PySide6.QtCore import QObject, Signal, Qt

import wifi_rssi
from protocol import cmd_packet, jpeg_end, jpeg_frames
from soulear import (SoulearWorker, probe_soulear, SOULEAR_HOST,
                    local_ipv4_addresses, _log as _slog)

HOST = "192.168.10.123"
VIDEO_PORT = 8031
CMD_PORT = 50000
PKT_MAGIC = 0x66

CMD_GET_BATTERY = 0x1017
CMD_SET_LAMP = 0x1050
CMD_GET_BOARD_INFO = 0x1060


def probe_itimo(host=HOST, local_ip=None, timeout=1.0):
    candidates = [local_ip] if local_ip else local_ipv4_addresses()
    prefix = ".".join(host.split(".")[:3]) + "."
    candidates = [a for a in candidates if a.startswith(prefix)] + \
                 [a for a in candidates if not a.startswith(prefix)]
    pkt = cmd_packet(CMD_GET_BATTERY, 1)
    for lip in candidates:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.15)
        try:
            s.bind((lip, 0))
        except OSError:
            s.close()
            continue
        try:
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
                if len(data) >= 4 and data[:4] == pkt[:4]:
                    return lip
        finally:
            s.close()
    return None


def _battery_percent(value):
    """Port of the app's battery ADC -> percent mapping (low 16 bits)."""
    v = value & 0xFFFF
    if v >= 0xFD2:       # 4050
        pct = 100.0
    elif v >= 0xE74:     # 3700
        pct = 50.0 + (v - 0xE74) * 0.14
    elif v >= 0xDC0:     # 3520
        pct = 20.0 + (v - 0xDCA) * 0.165
    elif v >= 0xD7A:     # 3450
        pct = 10.0 + (v - 0xD7A) * 0.14
    elif v >= 0xD3E:     # 3390
        pct = 1.0 + (v - 0xD3E) * 0.15
    else:
        pct = 0.0
    return max(0, min(100, int(round(pct))))


def _decode_board_info(reply):
    if len(reply) < 24:
        return None
    length = struct.unpack_from("<I", reply, 12)[0]
    if not (0 < length <= 8192) or len(reply) < 24 + length:
        return None
    payload = reply[24:24 + length]
    try:
        text = payload.decode("utf-8", "ignore").strip("\x00").strip()
    except Exception:
        text = bytes(b for b in payload if 32 <= b < 127).decode("ascii", "ignore").strip()
    if not text:
        return None
    checksum = struct.unpack_from("<I", reply, 8)[0] & 0xFF
    x = 0
    for b in payload:
        x ^= b
    if x != checksum and len(text) < 2:
        return None
    # Newer firmware returns a JSON board-info document.
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            for key in ("model", "brand", "name", "ssid_head"):
                value = data.get(key)
                if value:
                    return str(value).strip()
    except Exception:
        pass
    return text


class DeviceWorker(QObject):
    frameReady = Signal(bytes)
    connectionChanged = Signal(bool)
    batteryChanged = Signal(int, bool)          # percent (-1 unknown), charging
    modelChanged = Signal(str)
    lampChanged = Signal(bool)
    logMessage = Signal(str)

    def __init__(self, host=HOST, local_ip=None):
        super().__init__()
        self.host = host
        self.local_ip = local_ip
        self._stop = threading.Event()
        self._reconnect = threading.Event()
        self._threads = []
        self._lamp_on = False
        self._last_false_emit = 0.0
        self._stat_pkts = 0
        self._stat_frames = 0
        self._stat_gaps = 0
        self._stat_bad = 0
        self._stat_dups = 0
        self._stat_starts = 0
        self._stat_maxgap = 0.0

    # ------------------------------------------------------------------
    def start(self):
        for fn, name in ((self._video_loop, "video"), (self._command_loop, "commands")):
            t = threading.Thread(target=fn, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self):
        self._stop.set()
        self._reconnect.set()

    def retry(self):
        self._last_false_emit = 0.0
        self._reconnect.set()

    # ------------------------------------------------------------------
    def _send_cmd(self, sock, cmd, payload=b"", tries=2, timeout=0.55):
        seq = int(time.time()) & 0xFFFFFFFF
        pkt = cmd_packet(cmd, seq, payload)
        for _ in range(tries):
            try:
                sock.sendto(pkt, (self.host, CMD_PORT))
            except OSError:
                return None
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    data, _ = sock.recvfrom(2048)
                except socket.timeout:
                    break
                except OSError:
                    return None
                if len(data) >= 4 and data[:4] == pkt[:4]:
                    return data
        return None

    def _query_battery(self, sock):
        reply = self._send_cmd(sock, CMD_GET_BATTERY)
        if not reply or len(reply) < 12:
            return
        value = struct.unpack_from("<I", reply, 8)[0]
        pct = _battery_percent(value)
        charging = bool((value >> 16) & 1)
        self.batteryChanged.emit(pct, charging)

    def _query_model(self, sock):
        reply = self._send_cmd(sock, CMD_GET_BOARD_INFO)
        if not reply:
            return
        text = _decode_board_info(reply)
        if text:
            self.modelChanged.emit(text)

    def _command_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        try:
            sock.bind((self.local_ip or "", 0))
        except OSError:
            sock.close()
            return
        sock.settimeout(0.55)
        last_battery = 0.0
        last_model = 0.0
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                if now - last_battery >= 5.0:
                    self._query_battery(sock)
                    last_battery = now
                if now - last_model >= 20.0:
                    self._query_model(sock)
                    last_model = now
                self._stop.wait(0.25)
        finally:
            sock.close()

    # ------------------------------------------------------------------
    def _video_loop(self):
        start_pkt = cmd_packet(1, 0)
        _slog("itimo video start host=%s" % self.host)
        while not self._stop.is_set():
            self._reconnect.clear()
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
            try:
                sock.bind((self.local_ip or "", 0))
            except OSError:
                sock.close()
                time.sleep(1)
                continue
            sock.settimeout(0.5)
            attempt_start = time.monotonic()
            last_pkt = attempt_start
            last_send = 0.0
            last_stat = attempt_start
            connected = False
            reported = False
            # Frames in flight, keyed by the camera's frame counter.  The
            # camera can interleave fragments of two frames (especially when
            # the host is busy), so each frame is reassembled independently.
            frames = {}
            last_fid = None
            try:
                try:
                    sock.sendto(start_pkt, (self.host, VIDEO_PORT))
                    last_send = time.monotonic()
                except OSError:
                    pass
                while not self._stop.is_set() and not self._reconnect.is_set():
                    now = time.monotonic()
                    if last_pkt and now - last_pkt > self._stat_maxgap:
                        self._stat_maxgap = now - last_pkt
                    if now - last_stat >= 2.0:
                        _slog("itimo stats pkts=%d frames=%d gaps=%d bad=%d dups=%d starts=%d maxgap=%.3f" % (
                            self._stat_pkts, self._stat_frames,
                            self._stat_gaps, self._stat_bad, self._stat_dups,
                            self._stat_starts, self._stat_maxgap))
                        self._stat_pkts = self._stat_frames = 0
                        self._stat_gaps = self._stat_bad = 0
                        self._stat_dups = 0
                        self._stat_starts = 0
                        self._stat_maxgap = 0.0
                        last_stat = now
                    if now - last_send > 2.0:
                        try:
                            sock.sendto(start_pkt, (self.host, VIDEO_PORT))
                            self._stat_starts += 1
                        except OSError:
                            pass
                        last_send = now
                    if not connected and not reported and now - attempt_start > 2.5 \
                            and now - self._last_false_emit > 2.5:
                        self.connectionChanged.emit(False)
                        self._last_false_emit = now
                        reported = True
                    if connected and now - last_pkt > 3.0:
                        connected = False
                        reported = True
                        self.connectionChanged.emit(False)
                        self._last_false_emit = now
                    try:
                        pkt, _ = sock.recvfrom(65536)
                    except socket.timeout:
                        continue
                    except OSError:
                        if now - self._last_false_emit > 2.5:
                            self.connectionChanged.emit(False)
                            self._last_false_emit = now
                        time.sleep(0.5)
                        break
                    last_pkt = time.monotonic()
                    self._stat_pkts += 1
                    if len(pkt) < 24 or pkt[0] != PKT_MAGIC:
                        continue
                    typ = pkt[1]
                    fid = pkt[3]
                    tot = struct.unpack_from("<I", pkt, 4)[0]
                    seq = struct.unpack_from("<H", pkt, 12)[0]
                    plen = struct.unpack_from("<H", pkt, 14)[0]
                    payload = pkt[24:24 + plen]
                    if typ == 1:
                        if tot > (4 << 20) or plen <= 0 or seq:
                            # A frame has to start at fragment 0.
                            frames.pop(fid, None)
                            continue
                        fr = {"parts": {0: payload}, "tot": tot,
                              "end": False, "t": now}
                        frames[fid] = fr
                        if len(frames) > 8:
                            oldest = min(frames, key=lambda k: frames[k]["t"])
                            if oldest != fid:
                                frames.pop(oldest, None)
                                self._stat_gaps += 1
                    else:
                        fr = frames.get(fid)
                        if fr is None:
                            continue
                        fr["t"] = now
                        if seq in fr["parts"]:
                            self._stat_dups += 1
                        else:
                            fr["parts"][seq] = payload
                        if typ == 2:
                            fr["end"] = True
                            fr["need"] = seq + 1
                    if fr is None or not fr["end"]:
                        continue
                    need = fr["need"]
                    parts = fr["parts"]
                    if any(i not in parts for i in range(need)):
                        # A fragment is still in flight; keep the frame
                        # around briefly so reordering does not kill it.
                        continue
                    data = b"".join(parts[i] for i in range(need))
                    frames.pop(fid, None)
                    if len(data) > fr["tot"]:
                        data = data[:fr["tot"]]
                    frame = None
                    if data[:3] == b"\xff\xd8\xff":
                        end = jpeg_end(data, 0)
                        if end is not None:
                            frame = data[:end]
                    if frame is None:
                        found = list(jpeg_frames(data))
                        if found:
                            frame = found[0][0]
                    if frame is not None:
                        # Frames are emitted in camera order; a late older
                        # frame would only make the picture jump backwards.
                        if last_fid is not None:
                            delta = (fid - last_fid) & 0xFF
                            if not 0 < delta < 128:
                                self._stat_bad += 1
                                continue
                        last_fid = fid
                        self._stat_frames += 1
                        if not connected:
                            connected = True
                            reported = True
                            self._last_false_emit = 0.0
                            self.connectionChanged.emit(True)
                        _slog("itimo frame %d" % len(frame))
                        self.frameReady.emit(frame)
                    else:
                        self._stat_bad += 1
            except Exception:
                _slog("itimo video exception:\n" + traceback.format_exc())
            finally:
                sock.close()
            if self._reconnect.is_set() and not self._stop.is_set():
                # let the UI show "Searching…" again
                self.connectionChanged.emit(False)
                continue
            break
    # ------------------------------------------------------------------
    def request_lamp_toggle(self):
        threading.Thread(target=self._lamp_worker, daemon=True).start()

    def _lamp_worker(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind((self.local_ip or "", 0))
        except OSError:
            sock.close()
            return
        sock.settimeout(0.55)
        try:
            reply = self._send_cmd(sock, CMD_SET_LAMP, tries=2)
            if reply is not None:
                self._lamp_on = not self._lamp_on
                self.lampChanged.emit(self._lamp_on)
        finally:
            sock.close()

    def toggle_lamp(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind((self.local_ip or "", 0))
        except OSError:
            sock.close()
            return None
        sock.settimeout(0.55)
        try:
            reply = self._send_cmd(sock, CMD_SET_LAMP, tries=2)
            if reply is not None:
                self._lamp_on = not self._lamp_on
                return self._lamp_on
        finally:
            sock.close()
        return None

# ----------------------------------------------------------------------------
# backend manager (auto-detect iTiMO vs Soulear)
# ----------------------------------------------------------------------------

class DeviceManager(QObject):
    frameReady = Signal(bytes)
    connectionChanged = Signal(bool)
    batteryChanged = Signal(int, bool)
    modelChanged = Signal(str)
    lampChanged = Signal(bool)
    backendChanged = Signal(str)

    _detected = Signal(str, object)

    def __init__(self, host=None, backend="auto", local_ip=None):
        super().__init__()
        self.host = host
        self.backend = backend
        self.local_ip = local_ip
        self._worker = None
        self._worker_type = None
        self._stopped = threading.Event()
        self._detecting = False
        self._detected.connect(self._on_detected, Qt.QueuedConnection)

    def start(self):
        self._stopped.clear()
        self._start_detection()

    def stop(self):
        self._stopped.set()
        if self._worker:
            self._worker.stop()
            self._worker = None
            self._worker_type = None

    def retry(self):
        if self._worker:
            self._worker.stop()
            self._worker = None
            self._worker_type = None
        self.connectionChanged.emit(False)
        self._start_detection()

    def request_lamp_toggle(self):
        if self._worker:
            self._worker.request_lamp_toggle()

    # ------------------------------------------------------------------
    def _start_detection(self):
        if self._detecting:
            return
        self._detecting = True
        threading.Thread(target=self._detect_thread, daemon=True).start()

    def _detect_thread(self):
        try:
            kind, lip = self._choose_backend()
        except Exception:
            kind, lip = "itimo", self.local_ip
        if not self._stopped.is_set():
            self._detected.emit(kind, lip)

    def _choose_backend(self):
        host_s = self.host or SOULEAR_HOST
        host_i = self.host or HOST

        if self.backend == "soulear":
            return "soulear", self.local_ip or probe_soulear(host_s)
        if self.backend == "itimo":
            return "itimo", self.local_ip or probe_itimo(host_i)

        ssid = None
        try:
            _bars, _rssi, _quality, ssid = wifi_rssi.get_signal()
        except Exception:
            pass
        low = (ssid or "").lower()
        if low.startswith("soulear"):
            return "soulear", self.local_ip or probe_soulear(host_s)
        if any(k in low for k in ("itimo", "mz", "endoscope")):
            return "itimo", self.local_ip or probe_itimo(host_i)

        if self.host:
            if self.host.startswith("192.168.1."):
                return "soulear", self.local_ip or probe_soulear(host_s)
            if self.host.startswith("192.168.10."):
                return "itimo", self.local_ip or probe_itimo(host_i)

        lip = self.local_ip or probe_soulear(host_s)
        if lip:
            return "soulear", lip
        lip = self.local_ip or probe_itimo(host_i)
        if lip:
            return "itimo", lip
        return "itimo", self.local_ip

    def _on_detected(self, kind, local_ip):
        self._detecting = False
        _slog("manager detected %s local=%s" % (kind, local_ip))
        if self._stopped.is_set() or self._worker is not None:
            return
        self._worker_type = kind
        self.backendChanged.emit(kind)
        self.modelChanged.emit("Soulear" if kind == "soulear" else "iTiMO")
        if kind == "soulear":
            host = self.host or SOULEAR_HOST
            worker = SoulearWorker(host, local_ip)
        else:
            host = self.host or HOST
            worker = DeviceWorker(host, local_ip)
        worker.frameReady.connect(self.frameReady, Qt.QueuedConnection)
        worker.connectionChanged.connect(self.connectionChanged, Qt.QueuedConnection)
        worker.batteryChanged.connect(self.batteryChanged, Qt.QueuedConnection)
        worker.modelChanged.connect(self.modelChanged, Qt.QueuedConnection)
        worker.lampChanged.connect(self.lampChanged, Qt.QueuedConnection)
        self._worker = worker
        worker.start()
