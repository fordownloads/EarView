#!/usr/bin/env python3
"""Minimal proof of concept: grab one JPEG frame from the otoscope over TCP.

This is the smallest expression of the reconstructed protocol:
the camera pushes MJPEG on TCP 7060 as soon as the socket is accepted.

    python minimal_tcp.py [host] [output.jpg]
"""
import socket
import sys

host = sys.argv[1] if len(sys.argv) > 1 else "192.168.10.123"
out = sys.argv[2] if len(sys.argv) > 2 else "first.jpg"

s = socket.create_connection((host, 7060), timeout=5)
s.settimeout(5)
buf = b""

while True:
    chunk = s.recv(65536)
    if not chunk:
        raise SystemExit("connection closed before a JPEG arrived")
    buf += chunk
    soi = buf.find(b"\xff\xd8\xff")
    if soi < 0:
        continue
    # find EOI (good enough for a first-frame demo)
    eoi = buf.find(b"\xff\xd9", soi + 3)
    if eoi < 0:
        continue
    jpeg = buf[soi:eoi + 2]
    open(out, "wb").write(jpeg)
    print("saved %d bytes to %s" % (len(jpeg), out))
    break

s.close()
