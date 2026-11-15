#!/usr/bin/env python3
"""Local Soulear mock for testing the viewer protocol without hardware.

Binds 127.0.0.1:10005 (control), :10006 (open-video), and sends frames to
the picPort requested by the viewer.  Run viewer with:
    python viewer.py --host 127.0.0.1 --backend soulear
"""
import os
import socket
import struct
import threading
import time

HOST = "127.0.0.1"
CMD_PORT = 10005
VIDEO_CMD_PORT = 10006
MAGIC = 0xFFEEFFEE
JPEG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "..", "..", "sample", "live_frame.jpg")

try:
    JPEG = open(JPEG_PATH, "rb").read()
except OSError:
    JPEG = b"\xff\xd8\xff\xd9"
W, H = 640, 480
PIC_PORT = None
LED = {"on": False, "bright": 0}
STOP = threading.Event()


def reply(sock, addr, seq, cmd, payload=b"", status=0):
    pkt = struct.pack("<IHHBBH", MAGIC, seq, cmd, 1, status, len(payload)) + payload
    sock.sendto(pkt, addr)


def devinfo():
    p = bytearray(0x80)
    def put(off, text):
        b = text.encode("latin-1", "ignore")[:31]
        p[off:off + len(b)] = b
    put(0x01, "YPC")
    put(0x21, "BK7231U-XRH-FBPRO")
    put(0x41, "HKV41B")
    put(0x51, "Soulear-TEST")
    p[0x71:0x77] = bytes([0x02, 0x77, 0x9e, 0x66, 0x4c, 0x30])
    status = 77 << 9
    p[0x77] = status & 0xFF
    p[0x78] = (status >> 8) & 0xFF
    return bytes(p)


def control_server():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind((HOST, CMD_PORT))
    while not STOP.is_set():
        data, addr = s.recvfrom(2048)
        if len(data) < 12:
            continue
        magic, seq, cmd, req, _status, plen = struct.unpack_from("<IHHBBH", data, 0)
        if magic != MAGIC:
            continue
        payload = data[12:12 + plen]
        if cmd == 0x0001:
            reply(s, addr, seq, cmd, devinfo())
        elif cmd == 0x0002:
            reply(s, addr, seq, cmd, b"")
        elif cmd == 0x000A:
            if len(payload) >= 3:
                if payload[0] & 0x10:
                    LED["on"] = payload[1] != 0
                    LED["bright"] = payload[2]
                reply(s, addr, seq, cmd,
                      bytes([1, 1 if LED["on"] else 0, LED["bright"]]))
            else:
                reply(s, addr, seq, cmd, bytes([1, 1 if LED["on"] else 0, LED["bright"]]))
        else:
            reply(s, addr, seq, cmd, b"", status=1)


def sender(port, stop):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    fid = 0
    seq = 0
    n = 3
    step = (len(JPEG) + n - 1) // n
    while not stop.is_set() and not STOP.is_set():
        chunks = [JPEG[i:i + step] for i in range(0, len(JPEG), step)]
        for i, ch in enumerate(chunks):
            last = i == len(chunks) - 1
            if last:
                ch = ch + b"\x00" * 80
            hdr = struct.pack("<BBBBBB I 2s hh", 1, seq & 0xFF, fid & 0xFF,
                              1 if last else 0, len(chunks), 0, 0,
                              b"\x4c\x30", W, H)
            s.sendto(hdr + ch, (HOST, port))
            seq = (seq + 1) & 0xFF
            time.sleep(0.01)
        fid = (fid + 1) & 0xFF
        time.sleep(0.03)
    s.close()


def video_cmd_server():
    global PIC_PORT
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind((HOST, VIDEO_CMD_PORT))
    current = {"stop": threading.Event(), "thread": None}
    while not STOP.is_set():
        data, addr = s.recvfrom(2048)
        if len(data) < 12:
            continue
        magic, seq, cmd, req, _status, plen = struct.unpack_from("<IHHBBH", data, 0)
        if magic != MAGIC or cmd != 0x0004 or plen < 8:
            continue
        pic_port = struct.unpack_from("<H", data, 12)[0]
        reply(s, addr, seq, cmd, b"")
        current["stop"].set()
        if current["thread"]:
            current["thread"].join(timeout=1)
        stop = threading.Event()
        current["stop"] = stop
        current["thread"] = threading.Thread(target=sender, args=(pic_port, stop), daemon=True)
        current["thread"].start()


if __name__ == "__main__":
    threading.Thread(target=control_server, daemon=True).start()
    threading.Thread(target=video_cmd_server, daemon=True).start()
    print("Soulear mock on 127.0.0.1:10005/10006")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        STOP.set()
