#!/usr/bin/env python3
"""Small shared protocol helpers used by the viewer and the CLI scripts."""
import struct

MAGIC = 0x9999


def cmd_packet(cmd, seq, payload=b""):
    """iTiMO-style 24-byte command packet (payload starts at offset 8)."""
    body = bytearray(24)
    struct.pack_into("<HHI", body, 0, MAGIC, cmd & 0xFFFF, seq & 0xFFFFFFFF)
    body[8:8 + len(payload)] = payload
    return bytes(body)


def jpeg_end(buf, start=0):
    """Return the index just past the JPEG EOI starting at `start`, or None."""
    n = len(buf)
    if start + 2 > n or buf[start] != 0xFF or buf[start + 1] != 0xD8:
        return None
    i = start + 2
    while i + 1 < n:
        if buf[i] != 0xFF:
            j = buf.find(b"\xff", i)
            if j < 0:
                return None
            i = j
            continue
        while i < n and buf[i] == 0xFF:
            i += 1
        if i >= n:
            return None
        marker = buf[i]
        i += 1
        if marker == 0xD9:
            return i
        if marker == 0xD8:
            return None
        if (0xD0 <= marker <= 0xD7) or marker in (0x01, 0x00):
            continue
        if i + 2 > n:
            return None
        seglen = (buf[i] << 8) | buf[i + 1]
        if seglen < 2:
            return None
        i += seglen
    return None


def jpeg_frames(buf):
    """Yield (frame_bytes, start_index) for complete JPEGs found in buf."""
    i = 0
    n = len(buf)
    while True:
        soi = buf.find(b"\xff\xd8\xff", i)
        if soi < 0:
            return
        end = jpeg_end(buf, soi)
        if end is None:
            return
        yield buf[soi:end], soi
        i = end
