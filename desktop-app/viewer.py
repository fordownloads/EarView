#!/usr/bin/env python3
"""iTiMO / Soulear otoscope viewer with OSD and live ffmpeg recording.

Run:  python viewer.py            (auto-connect)
      python viewer.py --no-device (show the disconnected screen)
"""
import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

from PySide6.QtCore import (
    QPointF, QRectF, QSize, Qt, QTimer, Signal
)
from PySide6.QtGui import (
    QColor, QFont, QFontMetrics, QIcon, QImage, QKeySequence, QPainter, QPixmap,
    QShortcut, QTransform
)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QAbstractButton, QApplication, QFrame, QHBoxLayout, QLabel, QMainWindow,
    QMessageBox, QPushButton, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget
)

from device import DeviceManager

# ----------------------------------------------------------------------------
# paths / config
# ----------------------------------------------------------------------------

def _app_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _resource_dir():
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
    return Path(__file__).resolve().parent


APP_DIR = _app_dir()
ICON_DIR = _resource_dir() / "icons"
CONFIG_PATH = APP_DIR / "config.json"
CAPTURE_DIR = APP_DIR / "captures"
RECORD_DIR = APP_DIR / "recordings"

VIEWER_DEBUG = bool(os.environ.get("VIEWER_DEBUG"))
VIEWER_DEBUG_LOG = os.environ.get("VIEWER_DEBUG_LOG", "viewer_debug.log")


def _vlog(msg):
    if not VIEWER_DEBUG:
        return
    try:
        with open(VIEWER_DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass

OSD_HEIGHT = 56
BUTTON_SIZE = 40
ICON_SIZE = 24
FONT_PT = 10


def _fallback_config_path():
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "iTiMOViewer" / "config.json"


def load_config():
    rotation, mirror = 0, False
    for path in (CONFIG_PATH, _fallback_config_path()):
        try:
            data = json.loads(path.read_text("utf-8"))
            r = int(data.get("rotation", 0)) % 360
            rotation = r if r in (0, 90, 180, 270) else 0
            mirror = bool(data.get("mirror", False))
            break
        except Exception:
            continue
    return rotation, mirror


def save_config(rotation, mirror):
    data = {"rotation": int(rotation) % 360, "mirror": bool(mirror)}
    for path in (CONFIG_PATH, _fallback_config_path()):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2), "utf-8")
            os.replace(tmp, path)
            return True
        except Exception:
            continue
    return False


# ----------------------------------------------------------------------------
# Windows dark title bar
# ----------------------------------------------------------------------------

def apply_dark_title_bar(widget):
    if sys.platform != "win32":
        return
    try:
        import ctypes
        hwnd = int(widget.winId())
        dwm = ctypes.windll.dwmapi
        dark = ctypes.c_int(1)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE
            if dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(dark),
                                         ctypes.sizeof(dark)) == 0:
                break
        # Windows 11: exact black caption / white text / black border
        black = ctypes.c_uint(0x00000000)
        white = ctypes.c_uint(0x00FFFFFF)
        for attr, value in ((35, black), (36, white), (34, black)):
            dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(value), 4)
    except Exception:
        pass


# ----------------------------------------------------------------------------
# SVG icon cache
# ----------------------------------------------------------------------------

class Icons:
    def __init__(self):
        self._cache = {}

    def pixmap(self, name, color=QColor(255, 255, 255), size=ICON_SIZE,
               dpr=1.0):
        key = (name, color.rgba() if color else None, size, round(dpr, 2))
        if key in self._cache:
            return self._cache[key]
        path = ICON_DIR / name
        if not path.exists():
            pm = QPixmap(int(size * dpr), int(size * dpr))
            pm.fill(Qt.transparent)
            pm.setDevicePixelRatio(dpr)
            self._cache[key] = pm
            return pm
        renderer = QSvgRenderer(str(path))
        pm = QPixmap(int(round(size * dpr)), int(round(size * dpr)))
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        target = QRectF(0, 0, size * dpr, size * dpr)
        renderer.render(p, target)
        p.end()
        if color is not None:
            p = QPainter(pm)
            p.setCompositionMode(QPainter.CompositionMode_SourceIn)
            p.fillRect(pm.rect(), color)
            p.end()
        pm.setDevicePixelRatio(dpr)
        self._cache[key] = pm
        return pm


ICONS = Icons()


# ----------------------------------------------------------------------------
# widgets
# ----------------------------------------------------------------------------

class SvgIconWidget(QWidget):
    def __init__(self, name, size=96, color=QColor(255, 255, 255), parent=None):
        super().__init__(parent)
        self._name = name
        self._size = size
        self._color = color
        self.setFixedSize(size, size)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pm = ICONS.pixmap(self._name, self._color, self._size,
                          self.devicePixelRatioF())
        p.drawPixmap(QPointF(0, 0), pm)


class IconButton(QAbstractButton):
    def __init__(self, icon_name, tooltip, icons=ICONS, parent=None):
        super().__init__(parent)
        self._icons = icons
        self._icon_name = icon_name
        self._active = False
        self._active_color = QColor(255, 255, 255, 26)
        self.setFixedSize(BUTTON_SIZE, BUTTON_SIZE)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(tooltip)
        self.setFocusPolicy(Qt.NoFocus)

    def set_icon(self, name):
        if name != self._icon_name:
            self._icon_name = name
            self.update()

    def set_active(self, active, color=None):
        self._active = bool(active)
        if color is not None:
            self._active_color = color
        self.update()

    def set_active_color(self, color):
        self._active_color = color
        self.update()

    def enterEvent(self, event):
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        enabled = self.isEnabled()
        rect = self.rect().adjusted(2, 2, -2, -2)
        if enabled and self._active:
            p.setBrush(self._active_color)
            p.setPen(Qt.NoPen)
            p.drawEllipse(rect)
        if enabled and self.underMouse():
            alpha = 60 if self.isDown() else 38
            p.setBrush(QColor(255, 255, 255, alpha))
            p.setPen(Qt.NoPen)
            p.drawEllipse(rect)
        p.setOpacity(1.0 if enabled else 0.33)
        pm = self._icons.pixmap(self._icon_name,
                                None if self._icon_name == "record.svg" else QColor(255, 255, 255),
                                ICON_SIZE, self.devicePixelRatioF())
        x = (self.width() - ICON_SIZE) / 2.0
        y = (self.height() - ICON_SIZE) / 2.0
        p.drawPixmap(QPointF(x, y), pm)


class BatteryWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._pct = None
        self._charging = False
        self.setFixedSize(70, 28)

    def set_value(self, pct, charging=False):
        self._pct = pct
        self._charging = bool(charging)
        self.update()

    def _color(self):
        if self._pct is None:
            return QColor(255, 255, 255, 110)
        if self._pct >= 50:
            return QColor("#3DDC84")
        if self._pct >= 20:
            return QColor("#FFC107")
        return QColor("#FF5252")

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        color = self._color()
        # fill inside the white battery shell
        if self._pct is not None:
            fill_w = 12.5 * max(0, min(100, self._pct)) / 100.0
            if fill_w > 0:
                p.setBrush(color)
                p.setPen(Qt.NoPen)
                p.drawRoundedRect(QRectF(4.5, 10.5, fill_w, 7.0), 1.2, 1.2)
        pm = ICONS.pixmap("battery_empty.svg",
                          QColor(255, 255, 255, 255 if self._pct is not None else 150),
                          ICON_SIZE, self.devicePixelRatioF())
        p.drawPixmap(QPointF(0, 2), pm)
        if self._charging:
            p.setBrush(QColor("#3DDC84"))
            p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(19.5, 5.5), 2.4, 2.4)
        p.setPen(color if self._pct is not None else QColor(255, 255, 255, 140))
        f = QFont("Segoe UI", FONT_PT)
        f.setBold(True)
        p.setFont(f)
        text = "--" if self._pct is None else "%d%%" % self._pct
        p.drawText(QRectF(28, 0, 42, 28), Qt.AlignVCenter | Qt.AlignLeft, text)


class ElidedLabel(QWidget):
    def __init__(self, text="", max_width=260, parent=None):
        super().__init__(parent)
        self._text = text
        self._max_width = max_width
        self.setFixedHeight(28)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.setMinimumWidth(0)

    def setText(self, text):
        self._text = text or ""
        self.updateGeometry()
        self.update()

    def text(self):
        return self._text

    def sizeHint(self):
        fm = QFontMetrics(QFont("Segoe UI", FONT_PT))
        return QSize(min(self._max_width,
                         max(140, fm.horizontalAdvance(self._text) + 8)), 28)

    def minimumSizeHint(self):
        return QSize(0, 28)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.TextAntialiasing)
        p.setPen(QColor("#E8E8E8"))
        p.setFont(QFont("Segoe UI", FONT_PT))
        fm = p.fontMetrics()
        text = fm.elidedText(self._text, Qt.ElideRight, max(0, self.width() - 2))
        p.drawText(self.rect(), Qt.AlignVCenter | Qt.AlignLeft, text)


class VideoWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._source = QImage()
        self._image = QImage()
        self._rotation = 0
        self._mirror = False
        self.last_decode_ms = 0.0
        self.last_paint_ms = 0.0
        self.setAutoFillBackground(False)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def has_frame(self):
        return not self._image.isNull()

    def set_frame(self, data):
        t0 = time.perf_counter()
        img = QImage.fromData(data)
        if img.isNull():
            return False
        self._source = img
        self._rebuild()
        self.last_decode_ms = (time.perf_counter() - t0) * 1000.0
        self.update()
        return True

    def set_rotation(self, degrees):
        self._rotation = int(degrees) % 360
        self._rebuild()
        self.update()

    def set_mirror(self, mirror):
        self._mirror = bool(mirror)
        self._rebuild()
        self.update()

    def rotation(self):
        return self._rotation

    def mirror(self):
        return self._mirror

    def _rebuild(self):
        if self._source.isNull():
            self._image = QImage()
            return
        img = self._source
        if self._rotation:
            img = img.transformed(QTransform().rotate(self._rotation),
                                  Qt.SmoothTransformation)
        if self._mirror:
            img = img.flipped(Qt.Horizontal)
        self._image = img

    def transformed_image(self):
        return self._image

    def paintEvent(self, _):
        t0 = time.perf_counter()
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("black"))
        if self._image.isNull():
            self.last_paint_ms = (time.perf_counter() - t0) * 1000.0
            return
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        iw, ih = self._image.width(), self._image.height()
        if iw <= 0 or ih <= 0:
            self.last_paint_ms = (time.perf_counter() - t0) * 1000.0
            return
        scale = min(self.width() / iw, self.height() / ih)
        tw, th = iw * scale, ih * scale
        x = (self.width() - tw) / 2.0
        y = (self.height() - th) / 2.0
        p.drawImage(QRectF(x, y, tw, th), self._image)
        self.last_paint_ms = (time.perf_counter() - t0) * 1000.0


class NoDeviceWidget(QWidget):
    retryClicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAutoFillBackground(False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 20, 40, 20)
        layout.setSpacing(10)
        layout.addStretch(1)

        icon = SvgIconWidget("no_connection.svg", 96, QColor(255, 255, 255))
        layout.addWidget(icon, 0, Qt.AlignHCenter)

        title = QLabel("Device is not connected")
        title.setAlignment(Qt.AlignHCenter)
        title.setStyleSheet("color:#F2F2F2;font-size:18pt;font-weight:600;")
        layout.addWidget(title)

        help_label = QLabel(
            "Power on the device and connect to its Wi-Fi network\n"
        )
        help_label.setAlignment(Qt.AlignHCenter)
        help_label.setWordWrap(True)
        help_label.setMinimumWidth(560)
        help_label.setMaximumWidth(620)
        help_label.setStyleSheet("color:#A8A8A8;font-size:10.5pt;")
        layout.addWidget(help_label, 0, Qt.AlignHCenter)

        self.retry_btn = QPushButton("Retry")
        self.retry_btn.setCursor(Qt.PointingHandCursor)
        self.retry_btn.setFixedHeight(38)
        self.retry_btn.setMinimumWidth(130)
        self.retry_btn.setStyleSheet("""
            QPushButton {
                color: black; background: #fff;
                border-radius: 19px; padding: 0 26px;
                font-size: 10.5pt;
            }
            QPushButton:hover { background: #eee; }
            QPushButton:pressed { background: #ddd; }
            QPushButton:disabled { background: #aaa; }
        """)
        self.retry_btn.clicked.connect(self.retryClicked)
        layout.addWidget(self.retry_btn, 0, Qt.AlignHCenter)
        layout.addStretch(1)

    def set_searching(self, searching):
        self.retry_btn.setDisabled(searching)
        self.retry_btn.setText("Searching\u2026" if searching else "Retry")

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("black"))


class OsdBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(OSD_HEIGHT)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(8)

        self.battery = BatteryWidget()
        self.model = ElidedLabel("iTiMO", 250)
        self.fps = QLabel("0 fps")
        for w in (self.fps,):
            w.setStyleSheet("color:#E8E8E8;font-size:10pt;")
            w.setFixedHeight(28)
        self.fps.setMinimumWidth(52)
        self.fps.setAlignment(Qt.AlignVCenter | Qt.AlignRight)

        layout.addWidget(self.battery)
        layout.addWidget(self.model)
        layout.addWidget(self.fps)
        layout.addStretch(1)

        self.rotate_btn = IconButton("rotate.svg", "Rotate right 90\u00b0")
        self.mirror_btn = IconButton("flip.svg", "Mirror")
        self.mirror_btn.set_active_color(QColor(255, 255, 255, 26))
        self.light_btn = IconButton("lamp_off.svg", "Lamp")
        self.light_btn.set_active_color(QColor(255, 176, 0, 58))
        self.capture_btn = IconButton("capture.svg", "Capture frame")
        self.record_btn = IconButton("record.svg", "Record")
        self.record_btn.set_active_color(QColor(234, 51, 35, 95))
        self.fullscreen_btn = IconButton("fullscreen_off.svg", "Fullscreen")

        layout.addWidget(self.rotate_btn)
        layout.addWidget(self.mirror_btn)
        layout.addWidget(self._separator())
        layout.addWidget(self.light_btn)
        layout.addWidget(self._separator())
        layout.addWidget(self.capture_btn)
        layout.addWidget(self.record_btn)
        layout.addWidget(self._separator())
        layout.addWidget(self.fullscreen_btn)

        self._device_buttons = [
            self.rotate_btn, self.mirror_btn, self.light_btn,
            self.capture_btn, self.record_btn,
        ]

    def _separator(self):
        line = QFrame()
        line.setFixedSize(1, 24)
        line.setStyleSheet("background: rgba(255,255,255,56);")
        return line

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("black"))
        p.setPen(QColor(255, 255, 255, 28))
        p.drawLine(0, 0, self.width(), 0)

    def set_connected(self, connected):
        for b in self._device_buttons:
            b.setEnabled(bool(connected))
        if not connected:
            self.set_battery(None, False)
            self.model.setText("\u2014")
            self.fps.setText("0 fps")

    def set_battery(self, pct, charging=False):
        self.battery.set_value(pct, charging)

    def set_model(self, text):
        self.model.setText(text or "iTiMO")

    def set_fps(self, fps):
        self.fps.setText("%d fps" % int(round(fps)))

    def set_lamp(self, on):
        self.light_btn.set_icon("lamp_on.svg" if on else "lamp_off.svg")
        self.light_btn.set_active(bool(on), QColor(255, 176, 0, 58))

    def set_recording(self, active):
        self.record_btn.set_active(bool(active), QColor(234, 51, 35, 95))

    def set_fullscreen(self, active):
        self.fullscreen_btn.set_icon("fullscreen_on.svg" if active else "fullscreen_off.svg")


# ----------------------------------------------------------------------------
# recorder
# ----------------------------------------------------------------------------

def find_ffmpeg():
    for cand in (shutil.which("ffmpeg"), "ffmpeg.exe", "ffmpeg"):
        if not cand:
            continue
        try:
            subprocess.run([cand, "-version"], capture_output=True, timeout=5)
            return cand
        except Exception:
            pass
    return None


def record_filters(rotation, mirror):
    filters = []
    r = rotation % 360
    if r == 90:
        filters.append("transpose=1")
    elif r == 180:
        filters.extend(["hflip", "vflip"])
    elif r == 270:
        filters.append("transpose=2")
    if mirror:
        filters.append("hflip")
    return filters


class Recorder:
    def __init__(self, path, rotation=0, mirror=False):
        self.path = Path(path)
        self.rotation = rotation
        self.mirror = mirror
        self.proc = None
        self._queue = queue.Queue(maxsize=40)
        self._thread = None
        self._stop = threading.Event()

    def start(self):
        ff = find_ffmpeg()
        if not ff:
            return False, "ffmpeg not found"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [ff, "-y", "-hide_banner", "-loglevel", "error",
               "-f", "mjpeg", "-framerate", "15", "-i", "pipe:0"]
        filters = record_filters(self.rotation, self.mirror)
        if filters:
            cmd += ["-vf", ",".join(filters)]
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(self.path)]
        try:
            self.proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        except Exception as e:
            return False, str(e)
        self._thread = threading.Thread(target=self._writer, daemon=True)
        self._thread.start()
        return True, ""

    def _writer(self):
        while True:
            item = self._queue.get()
            if item is None:
                break
            try:
                self.proc.stdin.write(item)
                self.proc.stdin.flush()
            except Exception:
                break

    def write(self, frame):
        try:
            self._queue.put_nowait(frame)
        except queue.Full:
            pass

    def stop(self):
        if self._stop.is_set():
            return
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except Exception:
            pass
        if self._thread:
            self._thread.join(timeout=2)
        if self.proc:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            try:
                self.proc.wait(timeout=4)
            except Exception:
                self.proc.terminate()


# ----------------------------------------------------------------------------
# main window
# ----------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self, host=None, no_device=False, backend="auto",
                 local_ip=None):
        super().__init__()
        self.setWindowTitle("Ear View")
        self.resize(1280, 800)
        self.setMinimumSize(900, 620)
        self.setStyleSheet("QMainWindow { background: #000; }")

        self.no_device_mode = no_device
        self.backend = backend
        self.local_ip = local_ip
        self.rotation, self.mirror = load_config()
        self.mount_offset = 0
        self._recorder = None
        self._lamp_on = False
        self._fullscreen = False
        self._normal_geometry = None
        self._frame_times = deque()
        self._worker = None
        self._model = "iTiMO"
        self._ui_frames = 0
        self._fps_count = 0
        self._fps_window_start = None

        self.video = VideoWidget()
        self.video.set_rotation(self.rotation)
        self.video.set_mirror(self.mirror)

        self.no_device = NoDeviceWidget()
        self.no_device.retryClicked.connect(self.on_retry)

        self.content = QStackedWidget()
        self.content.addWidget(self.video)
        self.content.addWidget(self.no_device)
        self.content.setStyleSheet("background:#000;")

        self.osd = OsdBar()
        self.osd.set_connected(False)
        self.osd.set_lamp(False)
        self.osd.set_fullscreen(False)
        self.osd.mirror_btn.set_active(self.mirror, QColor(255, 255, 255, 26))

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.content, 1)
        layout.addWidget(self.osd, 0)
        self.setCentralWidget(central)

        self.osd.rotate_btn.clicked.connect(self.on_rotate)
        self.osd.mirror_btn.clicked.connect(self.on_mirror)
        self.osd.light_btn.clicked.connect(self.on_lamp)
        self.osd.capture_btn.clicked.connect(self.on_capture)
        self.osd.record_btn.clicked.connect(self.on_record)
        self.osd.fullscreen_btn.clicked.connect(self.toggle_fullscreen)

        QShortcut(QKeySequence("F11"), self, activated=self.toggle_fullscreen)
        QShortcut(QKeySequence("Alt+Return"), self, activated=self.toggle_fullscreen)
        QShortcut(QKeySequence("Esc"), self, activated=self._exit_fullscreen)

        self._fps_timer = QTimer(self)
        self._fps_timer.timeout.connect(self._update_fps)
        self._fps_timer.start(500)

        if no_device:
            self.content.setCurrentWidget(self.no_device)
            self.no_device.set_searching(False)
        else:
            self.content.setCurrentWidget(self.no_device)
            self.no_device.set_searching(True)
            self._worker = DeviceManager(host, backend, self.local_ip)
            self._worker.frameReady.connect(self.on_frame, Qt.QueuedConnection)
            self._worker.connectionChanged.connect(self.on_connection, Qt.QueuedConnection)
            self._worker.batteryChanged.connect(self.osd.set_battery, Qt.QueuedConnection)
            self._worker.modelChanged.connect(self.on_model_changed, Qt.QueuedConnection)
            self._worker.lampChanged.connect(self.on_lamp_changed, Qt.QueuedConnection)
            QTimer.singleShot(0, self._worker.start)

    # ------------------------------------------------------------------
    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, lambda: apply_dark_title_bar(self))

    def closeEvent(self, event):
        self._stop_recording()
        if self._worker:
            self._worker.stop()
        super().closeEvent(event)

    # ------------------------------------------------------------------
    def on_frame(self, data):
        if not self.video.set_frame(data):
            _vlog("on_frame: decode failed")
            return
        self._ui_frames += 1
        if self._ui_frames <= 3 or self._ui_frames % 50 == 0:
            _vlog("on_frame %d bytes ui=%d" % (len(data), self._ui_frames))
        now = time.monotonic()
        self._frame_times.append(now)
        if self._fps_window_start is None:
            self._fps_window_start = now
        self._fps_count += 1
        if now - self._fps_window_start >= 1.0:
            fps = self._fps_count / max(0.001, now - self._fps_window_start)
            self.osd.set_fps(fps)
            self._fps_count = 0
            self._fps_window_start = now
        if self._recorder:
            self._recorder.write(data)
        if self.content.currentWidget() is not self.video:
            self.content.setCurrentWidget(self.video)
            self.osd.set_connected(True)

    def on_connection(self, connected):
        _vlog("on_connection %s" % connected)
        if connected:
            self.content.setCurrentWidget(self.video)
            self.osd.set_connected(True)
            self.osd.set_model(self._model or "iTiMO")
            self.no_device.set_searching(False)
        else:
            self._stop_recording()
            self.content.setCurrentWidget(self.no_device)
            self.no_device.set_searching(False)
            self.osd.set_connected(False)
            self._frame_times.clear()
            self._fps_count = 0
            self._fps_window_start = None

    def on_model_changed(self, text):
        self._model = text or "iTiMO"
        upper = self._model.upper()
        offset = 180 if ("FBPRO" in upper or "R1" in upper) else 0
        if offset != self.mount_offset:
            self.mount_offset = offset
            self.video.set_rotation((self.rotation + self.mount_offset) % 360)
        if self.content.currentWidget() is self.video:
            self.osd.set_model(self._model)

    def _update_fps(self):
        if self._recorder and self._recorder.proc and self._recorder.proc.poll() is not None:
            self._stop_recording()
        now = time.monotonic()
        while self._frame_times and now - self._frame_times[0] > 2.0:
            self._frame_times.popleft()

    # ------------------------------------------------------------------
    def on_retry(self):
        if self.no_device_mode or not self._worker:
            return
        self.content.setCurrentWidget(self.no_device)
        self.no_device.set_searching(True)
        self._worker.retry()

    def on_rotate(self):
        self.rotation = (self.rotation + 90) % 360
        self.video.set_rotation((self.rotation + self.mount_offset) % 360)
        save_config(self.rotation, self.mirror)

    def on_mirror(self):
        self.mirror = not self.mirror
        self.video.set_mirror(self.mirror)
        self.osd.mirror_btn.set_active(self.mirror, QColor(255, 255, 255, 26))
        save_config(self.rotation, self.mirror)

    def on_lamp(self):
        if self._worker:
            self._worker.request_lamp_toggle()

    def on_lamp_changed(self, on):
        self._lamp_on = bool(on)
        self.osd.set_lamp(self._lamp_on)

    def on_capture(self):
        img = self.video.transformed_image()
        if img.isNull():
            return
        CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
        path = CAPTURE_DIR / ("iTiMO_%s.jpg" % time.strftime("%Y%m%d_%H%M%S"))
        img.save(str(path), "JPEG", 95)

    def on_record(self):
        if self._recorder:
            self._stop_recording()
            return
        if not self.video.has_frame():
            return
        RECORD_DIR.mkdir(parents=True, exist_ok=True)
        path = RECORD_DIR / ("iTiMO_%s.mp4" % time.strftime("%Y%m%d_%H%M%S"))
        rec = Recorder(path, self.rotation, self.mirror)
        ok, msg = rec.start()
        if not ok:
            QMessageBox.warning(self, "Recording", "Could not start ffmpeg:\n%s" % msg)
            return
        self._recorder = rec
        self.osd.set_recording(True)

    def _stop_recording(self):
        if self._recorder:
            self._recorder.stop()
            self._recorder = None
        self.osd.set_recording(False)

    # ------------------------------------------------------------------
    def toggle_fullscreen(self):
        if self._fullscreen:
            self._exit_fullscreen()
            return
        self._normal_geometry = self.geometry()
        self.showFullScreen()
        self._fullscreen = True
        self.osd.set_fullscreen(True)

    def _exit_fullscreen(self):
        if not self._fullscreen:
            return
        self.showNormal()
        if self._normal_geometry:
            self.setGeometry(self._normal_geometry)
        self._fullscreen = False
        self.osd.set_fullscreen(False)


# ----------------------------------------------------------------------------
# entry point
# ----------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="iTiMO / Soulear otoscope viewer")
    ap.add_argument("--host", default=None,
                    help="camera IP override (default: 192.168.10.123 / 192.168.1.1)")
    ap.add_argument("--local-ip", default=None,
                    help="bind to this local IPv4 (needed when another adapter shares the subnet)")
    ap.add_argument("--backend", choices=["auto", "itimo", "soulear"], default="auto",
                    help="force a camera protocol instead of auto-detecting")
    ap.add_argument("--no-device", action="store_true",
                    help="show the disconnected screen and do not connect")
    ap.add_argument("--screenshot", metavar="PATH", default=None,
                    help="save a window screenshot and exit (testing)")
    ap.add_argument("--screenshot-delay", type=float, default=1.5)
    args = ap.parse_args()

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName("Ear View")
    app.setFont(QFont("Segoe UI", FONT_PT))
    if VIEWER_DEBUG:
        def _hook(exc_type, exc, tb):
            import traceback
            _vlog("exception:\n" + "".join(traceback.format_exception(exc_type, exc, tb)))
        sys.excepthook = _hook
        threading.excepthook = lambda args: _vlog(
            "thread exception:\n" + "".join(
                __import__("traceback").format_exception(
                    args.exc_type, args.exc_value, args.exc_traceback)))
    icon_path = ICON_DIR / "app_icon.png"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    win = MainWindow(host=args.host, no_device=args.no_device,
                     backend=args.backend, local_ip=args.local_ip)
    win.show()

    if args.screenshot:
        def grab():
            pm = win.grab()
            pm.save(args.screenshot)
            print("screenshot:", args.screenshot)
            QTimer.singleShot(150, app.quit)
        QTimer.singleShot(int(args.screenshot_delay * 1000), grab)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
