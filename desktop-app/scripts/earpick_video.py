#!/usr/bin/env python3
"""
earpick_video.py - standalone video capture for the "iTiMO" / MZ WiFi otoscope
(com.molink.john.itimo, native lib: libmlcamera-2.5.so)

Reconstructed from the decompiled APK / native library. No Android app needed.

Protocol (see ../../README.md for evidence):

  Command channel : UDP  <device>:50000
      packet: <HHI  magic=0x9999, cmd, seq> + payload (24 bytes total, or more)
      reply : same first 4 bytes, value(s) start at offset 8

  Video (series 5 / MZ)  : TCP <device>:7060
      push MJPEG, multipart boundary "BoundaryS"; just scan the byte stream
      for JPEG SOI (FF D8 FF) .. EOI (FF D9).

  Video (series 3 / dual): UDP <device>:8031 (main) and :8030 (second lens)
      send one 24-byte command: 99 99 01 00 00*20
      then receive packets whose 24-byte header is:
          +0  u8  0x66 magic
          +1  u8  type (1=frame start, 3=continuation, 2=frame end)
          +2  u8  (format/quality byte seen by app)
          +3  u8  frame id
          +4  u32 total frame length
          +8  u32 (unused)
          +12 u16 fragment sequence
          +14 u16 payload length
          +16 u32 timestamp (unused here)
          +20 u32 (unused)
          +24 ... JPEG payload

Usage:
  python earpick_video.py probe
  python earpick_video.py tcp   --out frames --seconds 20
  python earpick_video.py udp   --out frames --port 8031 --seconds 20
  python earpick_video.py auto  --out frames --seconds 20
  python earpick_video.py cmd   --id 0x1000
  python earpick_video.py decode --id 0x1000 --value 1   # set MJPEG

Requires: Python 3.8+, no third-party modules.
"""

import argparse
import os
import socket
import struct
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from protocol import cmd_packet, jpeg_end, jpeg_frames

DEFAULT_HOST = "192.168.10.123"
CMD_PORT = 50000
TCP_VIDEO_PORT = 7060
UDP_VIDEO_PORT = 8031
UDP_VIDEO_PORT2 = 8030

MAGIC = 0x9999
PKT_MAGIC = 0x66

CMD_GET_VIDEO_FORMAT = 0x1000
CMD_SET_VIDEO_FORMAT = 0x1001
CMD_SET_STREAM_PASSWD = 0x1010
CMD_GET_BATTERY = 0x1017
CMD_SET_LAMP = 0x1050
CMD_SET_LEVEL = 0x1054
CMD_SET_SHUTDOWN = 0x1019


# ----------------------------------------------------------------------------
# low level helpers
# ----------------------------------------------------------------------------

def send_cmd(host, cmd, payload=b"", port=CMD_PORT, tries=3, timeout=0.7, verbose=True):
    """Send a command to the UDP command port, return list of raw replies."""
    replies = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
    s.settimeout(timeout)
    seq = int(time.time()) & 0xFFFFFFFF
    pkt = cmd_packet(cmd, seq, payload)
    try:
        for attempt in range(tries):
            try:
                s.sendto(pkt, (host, port))
            except OSError as e:
                if verbose:
                    print("  ! send failed: %s" % e)
                return replies
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    data, addr = s.recvfrom(2048)
                except socket.timeout:
                    break
                except OSError:
                    break
                if len(data) >= 4 and data[0:4] == pkt[0:4]:
                    replies.append((data, addr))
                    if verbose:
                        print("  <- %s %s" % (addr, data.hex()))
                    return replies
                else:
                    if verbose:
                        print("  <- (stale/other) %s %s" % (addr, data[:32].hex()))
    finally:
        s.close()
    return replies


def hexdump(data, width=16):
    out = []
    for off in range(0, len(data), width):
        chunk = data[off:off + width]
        hx = " ".join("%02x" % b for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append("%08x  %-*s  %s" % (off, width * 3 - 1, hx, asc))
    return "\n".join(out)


def local_ip_for(host):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 9))
        return s.getsockname()[0]
    except OSError:
        return "?"
    finally:
        s.close()


def tcp_connect(host, port, timeout=3.0):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect((host, port))
    return s


# ----------------------------------------------------------------------------
# JPEG slicing
# ----------------------------------------------------------------------------

def save_jpeg(path, data):
    with open(path, "wb") as f:
        f.write(data)


# ----------------------------------------------------------------------------
# TCP MJPEG capture (port 7060)
# ----------------------------------------------------------------------------

def capture_tcp(host, outdir=None, seconds=20.0, max_frames=0, verbose=True,
                on_frame=None, save=True, keep=True):
    if save:
        os.makedirs(outdir, exist_ok=True)
    if verbose:
        print("[tcp] connecting to %s:%d ..." % (host, TCP_VIDEO_PORT))
    s = tcp_connect(host, TCP_VIDEO_PORT, timeout=5.0)
    s.settimeout(1.0)
    if verbose:
        print("[tcp] connected, local %s:%d" % s.getsockname())
    buf = bytearray()
    frames = []
    count = 0
    start = time.time()
    last_data = start
    raw_path = os.path.join(outdir, "stream_7060.bin") if save else None
    raw = open(raw_path, "wb") if save else None
    stop = False
    try:
        while seconds <= 0 or time.time() - start < seconds:
            if max_frames and count >= max_frames:
                break
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                if time.time() - last_data > 3.0 and verbose:
                    print("[tcp] no data for %.1fs" % (time.time() - last_data))
                    last_data = time.time()
                continue
            if not chunk:
                if verbose:
                    print("[tcp] peer closed")
                break
            last_data = time.time()
            if raw:
                raw.write(chunk)
            buf.extend(chunk)
            # streaming JPEG slicer: keeps only an incomplete SOI..(no EOI) tail
            while True:
                soi = buf.find(b"\xff\xd8\xff")
                if soi < 0:
                    if len(buf) > 2:
                        del buf[:-2]
                    break
                if soi > 0:
                    del buf[:soi]
                end = jpeg_end(buf, 0)
                if end is None:
                    break
                frame = bytes(buf[:end])
                del buf[:end]
                count += 1
                if keep:
                    frames.append(frame)
                if save:
                    path = os.path.join(outdir, "frame_%06d.jpg" % count)
                    save_jpeg(path, frame)
                if verbose:
                    print("[tcp] frame %d: %d bytes%s" %
                          (count, len(frame), (" -> " + path) if save else ""))
                if on_frame is not None and on_frame(frame) is False:
                    stop = True
                    break
                if max_frames and count >= max_frames:
                    break
            if stop or (max_frames and count >= max_frames):
                break
    finally:
        if raw:
            raw.close()
        s.close()
    if verbose:
        print("[tcp] captured %d frames%s" %
              (count, (", raw stream in " + raw_path) if save else ""))
    return frames


# ----------------------------------------------------------------------------
# UDP stream capture (ports 8031 / 8030)
# ----------------------------------------------------------------------------

def stream_start_packet():
    return cmd_packet(1, 0)


def capture_udp(host, outdir=None, port=UDP_VIDEO_PORT, seconds=20.0, max_frames=0,
                verbose=True, on_frame=None, save=True, keep=True):
    if save:
        os.makedirs(outdir, exist_ok=True)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
    s.bind(("", 0))
    s.settimeout(0.5)
    if verbose:
        lip, lport = s.getsockname()
        print("[udp] socket %s:%d -> %s:%d" % (lip, lport, host, port))
    start_pkt = stream_start_packet()
    try:
        s.sendto(start_pkt, (host, port))
    except OSError as e:
        print("[udp] send failed: %s" % e)
        s.close()
        return []

    frames = []
    count = 0
    raw = open(os.path.join(outdir, "stream_%d.bin" % port), "wb") if save else None
    stats = {}
    buf = None
    total_len = 0
    frame_id = None
    last_seq = None
    dropping = False
    started = time.time()
    last_tx = started
    last_rx = started
    stop = False

    def emit(frame):
        nonlocal count, stop
        count += 1
        if keep:
            frames.append(frame)
        path = None
        if save:
            path = os.path.join(outdir, "frame_%06d.jpg" % count)
            save_jpeg(path, frame)
        if verbose:
            print("[udp] frame %d: %d bytes%s" %
                  (count, len(frame), (" -> " + path) if path else ""))
        if on_frame is not None and on_frame(frame) is False:
            stop = True

    try:
        while seconds <= 0 or time.time() - started < seconds:
            if stop or (max_frames and count >= max_frames):
                break
            # native code re-sends the start command if it sees nothing for ~0.5 s
            if time.time() - last_rx > 0.5:
                try:
                    s.sendto(start_pkt, (host, port))
                except OSError:
                    pass
                last_tx = time.time()
            try:
                pkt, addr = s.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            last_rx = time.time()
            if raw:
                raw.write(pkt)
            if len(pkt) < 4:
                continue
            p0 = pkt[0]
            if p0 != PKT_MAGIC:
                stats["other"] = stats.get("other", 0) + 1
                if verbose and stats["other"] <= 5:
                    print("[udp] non-frame packet %d bytes from %s: %s" % (len(pkt), addr, pkt[:32].hex()))
                continue
            if len(pkt) < 24:
                stats["short"] = stats.get("short", 0) + 1
                continue
            typ = pkt[1]
            fid = pkt[3]
            tot = struct.unpack_from("<I", pkt, 4)[0]
            seq = struct.unpack_from("<H", pkt, 12)[0]
            plen = struct.unpack_from("<H", pkt, 14)[0]
            payload = pkt[24:24 + plen]
            stats["type%d" % typ] = stats.get("type%d" % typ, 0) + 1
            if verbose and stats["type%d" % typ] <= 3:
                print("[udp] type=%d id=%d total=%d seq=%d plen=%d hdr=%s" %
                      (typ, fid, tot, seq, plen, pkt[:24].hex()))
            if typ == 1:
                if tot > (4 << 20):
                    stats["oversize"] = stats.get("oversize", 0) + 1
                    dropping = True
                    buf = None
                    continue
                buf = bytearray(payload)
                total_len = tot
                frame_id = fid
                last_seq = seq
                dropping = False
            elif typ == 3:
                if dropping or buf is None:
                    continue
                if last_seq is not None and seq != ((last_seq + 1) & 0xFFFF):
                    stats["gaps"] = stats.get("gaps", 0) + 1
                    if verbose:
                        print("[udp] fragment gap: got %d expected %d -> dropping frame" %
                              (seq, (last_seq + 1) & 0xFFFF))
                    buf = None
                    total_len = 0
                    frame_id = None
                    last_seq = None
                    dropping = True
                    continue
                buf.extend(payload)
                last_seq = seq
            elif typ == 2:
                if dropping:
                    dropping = False
                    buf = None
                    total_len = 0
                    frame_id = None
                    last_seq = None
                    continue
                if buf is None:
                    buf = bytearray(payload)
                    total_len = tot
                    frame_id = fid
                else:
                    if last_seq is not None and seq != ((last_seq + 1) & 0xFFFF):
                        stats["gaps"] = stats.get("gaps", 0) + 1
                        if verbose:
                            print("[udp] end fragment gap: got %d expected %d -> dropping frame" %
                                  (seq, (last_seq + 1) & 0xFFFF))
                        buf = None
                        total_len = 0
                        frame_id = None
                        last_seq = None
                        continue
                    buf.extend(payload)
                if total_len and len(buf) != total_len:
                    stats["badlen"] = stats.get("badlen", 0) + 1
                    if verbose:
                        print("[udp] frame length mismatch: got %d expected %d -> dropping" %
                              (len(buf), total_len))
                # validate / extract JPEG
                elif buf[:3] == b"\xff\xd8\xff" and buf[-2:] == b"\xff\xd9":
                    emit(bytes(buf))
                else:
                    found = list(jpeg_frames(bytes(buf)))
                    if found:
                        for fr, _ in found:
                            emit(fr)
                    elif verbose:
                        print("[udp] end packet but not JPEG: %d bytes head=%s tail=%s" %
                              (len(buf), buf[:8].hex(), buf[-8:].hex()))
                buf = None
                total_len = 0
                frame_id = None
                last_seq = None
    finally:
        if raw:
            raw.close()
        s.close()
    if verbose:
        print("[udp] captured %d frames, stats=%s" % (count, stats))
    return frames


# ----------------------------------------------------------------------------
# probe
# ----------------------------------------------------------------------------

def probe(host, set_mjpeg=False, seconds=3.0):
    print("== iTiMO / MZ endoscope probe ==")
    print("host: %s" % host)
    print("local address that would be used: %s" % local_ip_for(host))

    print("\n-- ICMP ping --")
    try:
        r = subprocess.run(["ping", "-n", "2", "-w", "1000", host],
                           capture_output=True, timeout=10)
        print("  ping exit code: %d (%s)" % (r.returncode,
              "reachable" if r.returncode == 0 else "no reply"))
    except Exception as e:
        print("  ping failed: %s" % e)

    print("\n-- TCP %d (MJPEG, BoundaryS) --" % TCP_VIDEO_PORT)
    try:
        s = tcp_connect(host, TCP_VIDEO_PORT, timeout=3.0)
        s.settimeout(0.5)
        print("  connected from %s:%d" % s.getsockname())
        data = bytearray()
        t0 = time.time()
        last = t0
        while time.time() - t0 < 3.0 and len(data) < (2 << 20):
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                if data and time.time() - last > 1.2:
                    break
                continue
            if not chunk:
                break
            data.extend(chunk)
            last = time.time()
        if data:
            print("  received %d bytes in %.2fs" % (len(data), time.time() - t0))
            print(hexdump(bytes(data[:256])))
            jf = list(jpeg_frames(bytes(data)))
            print("  JPEG frames found: %d" % len(jf))
            if jf:
                print("  first frame size: %d bytes" % len(jf[0][0]))
        else:
            print("  connected but no data within 3 s")
        s.close()
    except Exception as e:
        print("  connect/read failed: %s" % e)

    print("\n-- TCP 80 HTTP --")
    try:
        s = tcp_connect(host, 80, timeout=2.0)
        s.settimeout(1.5)
        s.sendall(b"GET / HTTP/1.0\r\nHost: %s\r\n\r\n" % host.encode())
        try:
            data = s.recv(1024)
        except socket.timeout:
            data = b""
        print("  %r" % data[:400])
        s.close()
    except Exception as e:
        print("  failed: %s" % e)

    print("\n-- TCP 554 RTSP OPTIONS --")
    try:
        s = tcp_connect(host, 554, timeout=2.0)
        s.settimeout(1.5)
        s.sendall(b"OPTIONS rtsp://%s/ RTSP/1.0\r\nCSeq: 1\r\n\r\n" % host.encode())
        try:
            data = s.recv(512)
        except socket.timeout:
            data = b""
        print("  %r" % data[:400])
        s.close()
    except Exception as e:
        print("  failed: %s" % e)

    print("\n-- UDP %d command channel --" % CMD_PORT)
    print("  GET_VIDEO_FORMAT (0x1000):")
    r = send_cmd(host, CMD_GET_VIDEO_FORMAT)
    if r:
        d = r[0][0]
        if len(d) >= 12:
            print("    format = %d" % struct.unpack_from("<I", d, 8)[0])
    print("  GET_BATTERY (0x1017):")
    r = send_cmd(host, CMD_GET_BATTERY)
    if r:
        d = r[0][0]
        print("    raw = %s" % d.hex())
        if len(d) >= 20:
            print("    values = %s" % (struct.unpack_from("<III", d, 8),))

    if set_mjpeg:
        print("\n-- set MJPEG (0x1001 = 1) --")
        send_cmd(host, CMD_SET_VIDEO_FORMAT, struct.pack("<I", 1))

    for port in (UDP_VIDEO_PORT, UDP_VIDEO_PORT2):
        print("\n-- UDP %d video stream --" % port)
        capture_udp(host, os.path.join("probe_out", "udp_%d" % port), port=port,
                    seconds=seconds, verbose=True)

    print("\nprobe done.")


# ----------------------------------------------------------------------------
# auto mode
# ----------------------------------------------------------------------------

def auto(host, outdir, seconds):
    print("[auto] trying TCP %d first" % TCP_VIDEO_PORT)
    try:
        frames = capture_tcp(host, outdir, seconds=seconds)
        if frames:
            return "tcp", frames
    except Exception as e:
        print("[auto] TCP failed: %s" % e)
    for port in (UDP_VIDEO_PORT, UDP_VIDEO_PORT2):
        print("[auto] trying UDP %d" % port)
        try:
            frames = capture_udp(host, outdir, port=port, seconds=seconds)
            if frames:
                return "udp%d" % port, frames
        except Exception as e:
            print("[auto] UDP %d failed: %s" % (port, e))
    return None, []


def mux_mp4(outdir, fps=15.0):
    ff = None
    for cand in ("ffmpeg", "ffmpeg.exe"):
        try:
            subprocess.run([cand, "-version"], capture_output=True, timeout=5)
            ff = cand
            break
        except Exception:
            pass
    if not ff:
        print("ffmpeg not found; frames left in %s" % outdir)
        return None
    pattern = os.path.join(outdir, "frame_%06d.jpg")
    out = os.path.join(outdir, "video.mp4")
    cmd = [ff, "-y", "-framerate", str(fps), "-i", pattern,
           "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", out]
    print("running: %s" % " ".join(cmd))
    subprocess.run(cmd)
    return out


# ----------------------------------------------------------------------------
# live playback through ffplay
# ----------------------------------------------------------------------------

def find_ffplay(explicit=None):
    cands = []
    if explicit:
        cands.append(explicit)
    cands += ["ffplay", "ffplay.exe"]
    for cand in cands:
        try:
            subprocess.run([cand, "-version"], capture_output=True, timeout=5)
            return cand
        except Exception:
            pass
    return None


def start_ffplay(path, fps=30.0, nodisp=False, title="iTiMO live", wallclock=True):
    cmd = [path, "-hide_banner", "-loglevel", "error",
           "-fflags", "nobuffer", "-flags", "low_delay", "-framedrop",
           "-autoexit", "-an", "-window_title", title]
    if wallclock:
        # stamp each frame on arrival so ffplay stays real-time regardless of
        # the camera's actual frame rate
        cmd += ["-use_wallclock_as_timestamps", "1",
                "-vf", "setpts=PTS-STARTPTS"]
    else:
        cmd += ["-framerate", str(fps)]
    if nodisp:
        cmd.append("-nodisp")
    cmd += ["-f", "mjpeg", "-i", "pipe:0"]
    print("[play] %s" % " ".join(cmd))
    return subprocess.Popen(cmd, stdin=subprocess.PIPE)


def play(host, source="auto", port=UDP_VIDEO_PORT, seconds=0.0, save_dir=None,
         ffplay_path=None, fps=30.0, nodisp=False, verbose=False, wallclock=True):
    ff = find_ffplay(ffplay_path)
    if not ff:
        print("ffplay not found. Install ffmpeg/ffplay or pass --ffplay PATH.")
        return
    proc = start_ffplay(ff, fps=fps, nodisp=nodisp, wallclock=wallclock)
    state = {"n": 0}

    def on_frame(frame):
        state["n"] += 1
        try:
            proc.stdin.write(frame)
            proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            return False
        return True

    chosen = source
    if source == "auto":
        # The otoscope may expose either TCP MJPEG or the UDP stream.
        try:
            t = tcp_connect(host, TCP_VIDEO_PORT, timeout=1.5)
            t.close()
            chosen = "tcp"
        except Exception:
            chosen = "udp"
        print("[play] auto-selected %s" % chosen)

    try:
        if chosen == "tcp":
            capture_tcp(host, save_dir, seconds=seconds, verbose=verbose,
                        on_frame=on_frame, save=bool(save_dir), keep=False)
        else:
            capture_udp(host, save_dir, port=port, seconds=seconds, verbose=verbose,
                        on_frame=on_frame, save=bool(save_dir), keep=False)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print("[play] %s failed: %s" % (chosen, e))
        if source == "auto" and chosen == "tcp" and state["n"] == 0:
            print("[play] falling back to UDP %d" % port)
            try:
                capture_udp(host, save_dir, port=port, seconds=seconds,
                            verbose=verbose, on_frame=on_frame,
                            save=bool(save_dir), keep=False)
            except KeyboardInterrupt:
                pass
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.terminate()
    print("[play] displayed %d frames" % state["n"])


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="iTiMO / MZ WiFi otoscope video capture")
    ap.add_argument("--host", default=DEFAULT_HOST)
    sub = ap.add_subparsers(dest="mode", required=True)

    p_probe = sub.add_parser("probe")
    p_probe.add_argument("--set-mjpeg", action="store_true")
    p_probe.add_argument("--seconds", type=float, default=3.0)

    p_play = sub.add_parser("play", help="live view in ffplay")
    p_play.add_argument("--source", choices=["auto", "tcp", "udp"], default="auto")
    p_play.add_argument("--port", type=int, default=UDP_VIDEO_PORT)
    p_play.add_argument("--seconds", type=float, default=0.0,
                        help="0 = until Ctrl+C / window close")
    p_play.add_argument("--save", metavar="DIR", default=None,
                        help="also save every JPEG frame here")
    p_play.add_argument("--ffplay", default=None)
    p_play.add_argument("--fps", type=float, default=30.0)
    p_play.add_argument("--nodisp", action="store_true",
                        help="decode without a window (not supported by all ffplay builds)")
    p_play.add_argument("--no-wallclock", action="store_true",
                        help="use fixed --fps instead of arrival timestamps")
    p_play.add_argument("--verbose", action="store_true")

    for name in ("tcp", "udp", "auto"):
        p = sub.add_parser(name)
        p.add_argument("--out", default="frames")
        p.add_argument("--seconds", type=float, default=20.0)
        p.add_argument("--max-frames", type=int, default=0)
        p.add_argument("--port", type=int, default=UDP_VIDEO_PORT)
        p.add_argument("--mp4", action="store_true")
        p.add_argument("--fps", type=float, default=15.0)

    p_cmd = sub.add_parser("cmd")
    p_cmd.add_argument("--id", type=lambda x: int(x, 0), required=True)
    p_cmd.add_argument("--hex", default="", help="payload hex")
    p_cmd.add_argument("--u32", type=lambda x: int(x, 0), default=None,
                       help="payload = little endian u32")
    p_cmd.add_argument("--port", type=int, default=CMD_PORT)
    p_cmd.add_argument("--tries", type=int, default=3)

    args = ap.parse_args()

    if args.mode == "probe":
        probe(args.host, set_mjpeg=args.set_mjpeg, seconds=args.seconds)
        return

    if args.mode == "play":
        play(args.host, source=args.source, port=args.port,
             seconds=args.seconds, save_dir=args.save,
             ffplay_path=args.ffplay, fps=args.fps,
             nodisp=args.nodisp, verbose=args.verbose,
             wallclock=not args.no_wallclock)
        return

    if args.mode == "cmd":
        payload = b""
        if args.hex:
            payload = bytes.fromhex(args.hex.replace(" ", ""))
        elif args.u32 is not None:
            payload = struct.pack("<I", args.u32)
        r = send_cmd(args.host, args.id, payload, port=args.port, tries=args.tries)
        if not r:
            print("no reply")
        return

    outdir = args.out
    if args.mode == "tcp":
        frames = capture_tcp(args.host, outdir, seconds=args.seconds,
                             max_frames=args.max_frames)
        used = "tcp"
    elif args.mode == "udp":
        frames = capture_udp(args.host, outdir, port=args.port,
                             seconds=args.seconds, max_frames=args.max_frames)
        used = "udp%d" % args.port
    else:
        used, frames = auto(args.host, outdir, seconds=args.seconds)

    print("\nCaptured %d frames via %s" % (len(frames), used))
    if frames and args.mp4:
        out = mux_mp4(outdir, args.fps)
        if out:
            print("video: %s" % out)


if __name__ == "__main__":
    main()

