#!/usr/bin/env python3
"""Local mock of the iTiMO/MZ endoscope, used to validate earpick_video.py
without hardware.  Binds 127.0.0.1:50000 (cmd), :7060 (TCP MJPEG),
:8031/:8030 (UDP stream).  Run it, then run earpick_video.py --host 127.0.0.1.
"""
import os
import socket
import struct
import threading
import time

# A real (small) JPEG from the APK, so the PoC's marker parser is exercised.
_real = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..",
                     "res", "drawable-xhdpi", "tt_adapter_baidu_logo.jpeg")
try:
    JPEG = open(_real, "rb").read()
except OSError:
    JPEG = b"\xff\xd8\xff\xd9"
MAGIC = 0x9999


def tcp_server():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 7060))
    s.listen(4)
    while True:
        c, a = s.accept()
        try:
            c.sendall(b"Content-Type: multipart/x-mixed-replace; boundary=BoundaryS\r\n\r\n")
            for i in range(50):
                c.sendall(b"--BoundaryS\r\nContent-Type: image/jpeg\r\n\r\n")
                c.sendall(JPEG)
                c.sendall(b"\r\n")
                time.sleep(0.05)
        except OSError:
            pass
        finally:
            c.close()


def cmd_server():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 50000))
    while True:
        d, a = s.recvfrom(2048)
        if len(d) < 24:
            continue
        r = bytearray(d)
        struct.pack_into("<I", r, 8, 1)   # video format = MJPEG
        s.sendto(bytes(r), a)


def udp_stream(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", port))
    while True:
        d, a = s.recvfrom(2048)
        if len(d) < 24 or d[:2] != b"\x99\x99" or d[2:4] != b"\x01\x00":
            continue
        fid = (int(time.time() * 10) & 0xFF)
        total = len(JPEG)
        step = max(1, (total + 2) // 3)
        chunks = [JPEG[i:i + step] for i in range(0, total, step)]
        for i, ch in enumerate(chunks):
            typ = 1 if i == 0 else (2 if i == len(chunks) - 1 else 3)
            hdr = bytearray(24)
            hdr[0] = 0x66
            hdr[1] = typ
            hdr[2] = 1
            hdr[3] = fid
            struct.pack_into("<I", hdr, 4, total)
            struct.pack_into("<H", hdr, 12, i)
            struct.pack_into("<H", hdr, 14, len(ch))
            s.sendto(bytes(hdr) + ch, a)
            time.sleep(0.01)
        time.sleep(0.05)


if __name__ == "__main__":
    threading.Thread(target=tcp_server, daemon=True).start()
    threading.Thread(target=cmd_server, daemon=True).start()
    threading.Thread(target=udp_stream, args=(8031,), daemon=True).start()
    threading.Thread(target=udp_stream, args=(8030,), daemon=True).start()
    print("mock device running on 127.0.0.1 (Ctrl+C to stop)")
    while True:
        time.sleep(1)
