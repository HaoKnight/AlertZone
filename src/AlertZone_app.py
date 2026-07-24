"""PySide6 摄像头人体检测与跟踪界面。

程序使用 YOLO + ByteTrack 框选并跟踪人体。
程序不进行人脸检测、身份识别或人员身份比对。
"""

from __future__ import annotations

import json
import math
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import cv2
import torch
from PySide6.QtCore import (
    QPoint,
    QRect,
    QSettings,
    Qt,
    QThread,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QCloseEvent,
    QCursor,
    QDesktopServices,
    QIcon,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyleOptionButton,
    QVBoxLayout,
    QWidget,
)
from ultralytics import YOLO


# 模型和跟踪器配置。COCO 数据集中类别 0 表示 person。
MODEL_NAME = "yolo11n.pt"
TRACKER_NAME = "bytetrack.yaml"
PERSON_CLASS_ID = 0
ALERT_CONFIRM_SECONDS = 1.0
ALERT_CONFIRM_OPTIONS_SECONDS = (0.0, 0.2, 0.5, 1.0, 2.0)

# 程序图标和局域网网页都相对于脚本目录定位，源码与打包环境均可读取。
APP_ROOT = Path(__file__).resolve().parent
MODEL_PATH = APP_ROOT / MODEL_NAME
# 源码目录中的图标位于项目根目录；打包后则位于应用资源根目录。
PACKAGED_ICON_PATH = APP_ROOT / "icon" / "icon.png"
SOURCE_ICON_PATH = APP_ROOT.parent / "icon" / "icon.png"
ICON_PATH = (
    PACKAGED_ICON_PATH
    if PACKAGED_ICON_PATH.is_file()
    else SOURCE_ICON_PATH
)
WEB_HOST = "0.0.0.0"
WEB_PORT = 8765
LAN_CLIENT_TTL_SECONDS = 3.5
WEB_ROOT = APP_ROOT / "web"
WEB_PREVIEW_INTERVAL_SECONDS = 0.08
WEB_PREVIEW_REQUEST_TTL_SECONDS = 1.2
WEB_PREVIEW_STOP_TOMBSTONE_SECONDS = 10.0
WEB_PREVIEW_STREAM_WAIT_SECONDS = 0.5
WEB_PREVIEW_MAX_WIDTH = 1280

# OpenCV 没有跨平台的“支持分辨率”枚举接口，因此扫描时逐一请求常见
# 视频模式，并只保留能够实际读出完全相同尺寸画面的模式。
CAMERA_RESOLUTION_CANDIDATES = (
    (320, 240),
    (640, 360),
    (640, 480),
    (800, 600),
    (1024, 576),
    (1024, 768),
    (1280, 720),
    (1280, 960),
    (1280, 1024),
    (1600, 900),
    (1920, 1080),
    (2560, 1440),
    (3840, 2160),
)
DEFAULT_CAMERA_RESOLUTION = (1280, 720)

# 使用系统原生位置保存桌面客户端设置，升级项目代码时不会丢失。
SETTINGS_ORGANIZATION = "CameraMonitor"
SETTINGS_APPLICATION = "CameraApp"

DetectionRegion = tuple[float, float, float, float]


def select_inference_device() -> tuple[str, str]:
    """选择 YOLO 推理设备，并返回传给模型的值和界面显示名称。"""
    if torch.cuda.is_available():
        device_index = 0
        device_name = torch.cuda.get_device_name(device_index)
        return f"cuda:{device_index}", device_name

    # Apple Silicon 使用 MPS；其余平台在没有可用 CUDA 时回退到 CPU。
    mps_backend = getattr(torch.backends, "mps", None)
    if mps_backend is not None and mps_backend.is_available():
        return "mps", "GPU-Apple MPS"

    if sys.platform == "win32":
        if torch.version.cuda is None:
            return "cpu", "CPU（当前 PyTorch 不含 CUDA）"
        return "cpu", "CPU（CUDA 不可用，请检查 NVIDIA 驱动）"
    return "cpu", "CPU"


def open_camera(camera_index: int) -> cv2.VideoCapture:
    """按平台打开摄像头；macOS 固定使用与设备排序一致的 AVFoundation。"""
    camera = cv2.VideoCapture()
    if sys.platform == "win32":
        backends = (cv2.CAP_MSMF, cv2.CAP_DSHOW)
    elif sys.platform == "darwin":
        backends = (cv2.CAP_AVFOUNDATION,)
    else:
        backends = (cv2.CAP_ANY,)
    for backend in backends:
        try:
            if camera.open(camera_index, backend):
                return camera
        except cv2.error:
            pass
        camera.release()
    return camera


def safe_camera_read(
    camera: cv2.VideoCapture,
) -> tuple[bool, Any]:
    """将部分 Windows 驱动抛出的 cv2.error 转换为普通读取失败。"""
    try:
        return camera.read()
    except cv2.error:
        return False, None


def normalize_rotation_degrees(rotation_degrees: int) -> int:
    """把画面旋转角度归一化为 0、90、180 或 270 度。"""
    return (int(rotation_degrees) // 90 * 90) % 360


def rotate_frame_clockwise(frame: Any, rotation_degrees: int) -> Any:
    """按 90 度步长顺时针旋转 OpenCV 画面。"""
    rotation_degrees = normalize_rotation_degrees(rotation_degrees)
    if rotation_degrees == 90:
        return cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
    if rotation_degrees == 180:
        return cv2.rotate(frame, cv2.ROTATE_180)
    if rotation_degrees == 270:
        return cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return frame


# ---------- 局域网检测状态 ----------
class LanDetectionState:
    """保存网页需要读取的少量状态，不让网页直接接触摄像头和模型。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._preview_condition = threading.Condition(self._lock)
        self._instance_id = f"{int(time.time() * 1000)}-{id(self):x}"
        self._running = False
        self._status = "本地检测未启动"
        self._people_count = 0
        self._fps = 0.0
        self._presence_started_at: float | None = None
        self._updated_at = time.time()
        self._event_id = 0
        self._event_time: float | None = None
        self._event_people_count = 0
        self._intruder_jpeg: bytes | None = None
        self._alert_rearm_generation = 0
        self._alert_confirm_seconds = ALERT_CONFIRM_SECONDS
        self._preview_jpeg: bytes | None = None
        self._preview_updated_at: float | None = None
        self._preview_viewer_deadlines: dict[str, float] = {}
        self._stopped_preview_viewer_deadlines: dict[str, float] = {}
        self._preview_single_request_deadline = 0.0
        self._preview_sequence = 0
        self._online_clients: dict[str, float] = {}

    def touch_client(self, address: str) -> None:
        """记录网页设备心跳；同一 IP 的多个页面只计为一台设备。"""
        now = time.time()
        with self._lock:
            self._online_clients[address] = now + LAN_CLIENT_TTL_SECONDS
            self._remove_expired_clients(now)

    def online_client_count(self) -> int:
        """返回最近仍持续轮询状态接口的局域网设备数。"""
        now = time.time()
        with self._lock:
            self._remove_expired_clients(now)
            return len(self._online_clients)

    def clear_online_clients(self) -> None:
        """局域网服务停止时立即清空网页设备计数。"""
        with self._lock:
            self._online_clients.clear()

    def _remove_expired_clients(self, now: float) -> None:
        expired = [
            address
            for address, deadline in self._online_clients.items()
            if deadline <= now
        ]
        for address in expired:
            self._online_clients.pop(address, None)

    def set_running(self, running: bool, status: str) -> None:
        """更新本地检测是否运行；停止时同时清除当前在场状态。"""
        with self._lock:
            self._running = running
            self._status = status
            self._updated_at = time.time()
            # 开始或停止一次检测时清除旧画面，避免网页显示上一轮的帧。
            self._preview_jpeg = None
            self._preview_updated_at = None
            self._preview_single_request_deadline = 0.0
            if not running:
                self._people_count = 0
                self._fps = 0.0
                self._presence_started_at = None
            self._preview_condition.notify_all()

    def set_status(self, status: str) -> None:
        """同步本地模型加载、摄像头打开等文字状态。"""
        with self._lock:
            self._status = status
            self._updated_at = time.time()

    def update_stats(self, people_count: int, fps: float) -> None:
        """同步人数和 FPS，并记录人物连续出现的起始时间。"""
        now = time.time()
        with self._lock:
            if people_count > 0 and self._people_count <= 0:
                self._presence_started_at = now
            elif people_count <= 0:
                self._presence_started_at = None

            self._people_count = max(people_count, 0)
            self._fps = max(fps, 0.0)
            self._updated_at = now

    def reset_presence(self) -> None:
        """框选区域改变时清除旧的在场计时，下一帧按新范围重新统计。"""
        with self._lock:
            self._people_count = 0
            self._presence_started_at = None
            self._updated_at = time.time()

    def record_intrusion(self, people_count: int, jpeg: bytes) -> None:
        """记录一次已满足网页确认时间的入侵事件和当时所有人物的拼图。"""
        with self._lock:
            self._event_id += 1
            self._event_time = time.time()
            self._event_people_count = max(people_count, 1)
            self._intruder_jpeg = jpeg
            self._updated_at = self._event_time

    def rearm_alert(self, confirm_seconds: float | None = None) -> bool:
        """更新确认时间并重新布防，但不重置人物连续在场时间。"""
        now = time.time()
        with self._lock:
            if confirm_seconds is not None:
                self._alert_confirm_seconds = confirm_seconds
            if not self._running or self._people_count <= 0:
                return False
            self._alert_rearm_generation += 1
            self._updated_at = now
            return True

    def alert_rearm_generation(self) -> int:
        """返回警告重新布防序号，供检测线程安全读取。"""
        with self._lock:
            return self._alert_rearm_generation

    def alert_confirm_seconds(self) -> float:
        """返回网页当前选择的人物持续确认时间。"""
        with self._lock:
            return self._alert_confirm_seconds

    def snapshot(self) -> dict[str, Any]:
        """返回可以安全序列化成 JSON 的状态副本。"""
        now = time.time()
        with self._lock:
            presence_seconds = (
                max(now - self._presence_started_at, 0.0)
                if self._presence_started_at is not None
                else 0.0
            )
            return {
                "instance_id": self._instance_id,
                "detection_running": self._running,
                "status": self._status,
                "people_count": self._people_count,
                "fps": round(self._fps, 1),
                "person_present": self._people_count > 0,
                "presence_seconds": round(presence_seconds, 1),
                "updated_at": self._updated_at,
                "intrusion_event_id": self._event_id,
                "intrusion_time": self._event_time,
                "intrusion_people_count": self._event_people_count,
                "intrusion_image_available": self._intruder_jpeg is not None,
                "preview_available": self._preview_jpeg is not None,
                "preview_updated_at": self._preview_updated_at,
                "confirm_seconds": self._alert_confirm_seconds,
            }

    def intruder_image(self, event_id: int) -> bytes | None:
        """只返回指定事件的截图，避免新旧事件图片串用。"""
        with self._lock:
            if event_id != self._event_id:
                return None
            return self._intruder_jpeg

    def preview_requested_recently(self) -> bool:
        """仅有网页预览租约存活时，才允许检测线程编码画面。"""
        now = time.time()
        with self._lock:
            self._remove_expired_preview_viewers(now)
            return (
                self._running
                and (
                    bool(self._preview_viewer_deadlines)
                    or now <= self._preview_single_request_deadline
                )
            )

    def start_preview_viewer(self, viewer_id: str) -> bool:
        """建立一个网页预览会话；已停止的旧编号不得复活。"""
        with self._preview_condition:
            now = time.time()
            self._remove_expired_preview_viewers(now)
            if viewer_id in self._stopped_preview_viewer_deadlines:
                return False
            self._preview_viewer_deadlines[viewer_id] = (
                now + WEB_PREVIEW_REQUEST_TTL_SECONDS
            )
            self._preview_condition.notify_all()
            return True

    def renew_preview_viewer(self, viewer_id: str) -> bool:
        """续期网页预览会话，但不复活已显式停止的旧会话。"""
        with self._preview_condition:
            now = time.time()
            self._remove_expired_preview_viewers(now)
            if viewer_id in self._stopped_preview_viewer_deadlines:
                return False
            self._preview_viewer_deadlines[viewer_id] = (
                now + WEB_PREVIEW_REQUEST_TTL_SECONDS
            )
            self._preview_condition.notify_all()
            return True

    def stop_preview_viewer(self, viewer_id: str) -> None:
        """立即停止一个网页预览会话。"""
        with self._preview_condition:
            now = time.time()
            self._remove_expired_preview_viewers(now)
            self._preview_viewer_deadlines.pop(viewer_id, None)
            # 拦住已在网络途中的旧心跳或 GET，避免关闭后又复活。
            self._stopped_preview_viewer_deadlines[viewer_id] = (
                now + WEB_PREVIEW_STOP_TOMBSTONE_SECONDS
            )
            self._preview_condition.notify_all()

    def _remove_expired_preview_viewers(self, now: float) -> None:
        """清理没有心跳的网页；调用方必须已持有状态锁。"""
        expired_viewers = [
            viewer_id
            for viewer_id, deadline in self._preview_viewer_deadlines.items()
            if deadline < now
        ]
        for viewer_id in expired_viewers:
            del self._preview_viewer_deadlines[viewer_id]
        stopped_viewers = self._stopped_preview_viewer_deadlines
        expired_stopped_viewers = [
            viewer_id
            for viewer_id, deadline in stopped_viewers.items()
            if deadline < now
        ]
        for viewer_id in expired_stopped_viewers:
            del self._stopped_preview_viewer_deadlines[viewer_id]

    def update_preview_image(self, jpeg: bytes) -> None:
        """保存最新一张带检测框的网页预览帧。"""
        with self._lock:
            if not self._running:
                return
            self._preview_jpeg = jpeg
            self._preview_updated_at = time.time()
            self._preview_sequence += 1
            self._preview_condition.notify_all()

    def request_preview_image(self) -> bytes | None:
        """登记一次性预览需求，并返回当前可用的最新帧。"""
        with self._lock:
            self._preview_single_request_deadline = (
                time.time() + WEB_PREVIEW_REQUEST_TTL_SECONDS
            )
            if not self._running:
                return None
            return self._preview_jpeg

    def wait_for_preview_image(
        self,
        viewer_id: str,
        last_sequence: int,
        timeout: float,
    ) -> tuple[bytes | None, int, bool]:
        """等待新预览帧，供 MJPEG 长连接按产生速度持续推送。"""
        with self._preview_condition:
            now = time.time()
            self._remove_expired_preview_viewers(now)
            viewer_deadline = self._preview_viewer_deadlines.get(viewer_id)
            if viewer_deadline is None:
                return None, last_sequence, False

            if (
                self._preview_jpeg is None
                or self._preview_sequence <= last_sequence
            ):
                self._preview_condition.wait(
                    min(timeout, max(viewer_deadline - now, 0.0))
                )

            self._remove_expired_preview_viewers(time.time())
            if viewer_id not in self._preview_viewer_deadlines:
                return None, last_sequence, False
            if (
                self._preview_jpeg is None
                or self._preview_sequence <= last_sequence
            ):
                return None, last_sequence, True
            return self._preview_jpeg, self._preview_sequence, True


def local_network_ip() -> str:
    """尽量取得当前局域网地址，失败时退回本机地址。"""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # UDP connect 不会发送数据，仅用于让系统选择当前默认网卡。
        probe.connect(("8.8.8.8", 80))
        address = str(probe.getsockname()[0])
        if address and not address.startswith("127."):
            return address
    except OSError:
        pass
    finally:
        probe.close()

    try:
        address = socket.gethostbyname(socket.gethostname())
        if address:
            return address
    except OSError:
        pass
    return "127.0.0.1"


class LanHttpServer(ThreadingHTTPServer):
    """每个网页请求使用独立线程，避免请求阻塞检测界面。"""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.shutdown_event = threading.Event()
        super().__init__(*args, **kwargs)

    def server_close(self) -> None:
        """通知现有 MJPEG 连接退出，然后关闭监听端口。"""
        self.shutdown_event.set()
        super().server_close()


class LanRequestHandler(BaseHTTPRequestHandler):
    """提供网页、状态 JSON 和触发警告时的人物截图。"""

    server_version = "CameraAlert/1.0"

    @property
    def app_server(self) -> LanHttpServer:
        return self.server  # type: ignore[return-value]

    def do_GET(self) -> None:
        request = urlparse(self.path)

        if request.path in ("/", "/index.html"):
            self._send_bytes(
                200,
                self.app_server.index_page,  # type: ignore[attr-defined]
                "text/html; charset=utf-8",
            )
            return

        if request.path == "/api/status":
            self.app_server.detection_state.touch_client(  # type: ignore[attr-defined]
                str(self.client_address[0])
            )
            state = self.app_server.detection_state.snapshot()  # type: ignore[attr-defined]
            payload = json.dumps(
                state,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            self._send_bytes(200, payload, "application/json; charset=utf-8")
            return

        if request.path == "/api/intruder.jpg":
            event_values = parse_qs(request.query).get("event", [])
            try:
                event_id = int(event_values[0])
            except (IndexError, TypeError, ValueError):
                self._send_bytes(400, b"Missing event id", "text/plain")
                return

            image = self.app_server.detection_state.intruder_image(  # type: ignore[attr-defined]
                event_id
            )
            if image is None:
                self._send_bytes(404, b"Image not found", "text/plain")
            else:
                self._send_bytes(200, image, "image/jpeg")
            return

        if request.path == "/api/preview.mjpg":
            viewer_id = self._preview_viewer_id(request)
            if viewer_id is None:
                self._send_bytes(
                    400,
                    b"Missing or invalid preview viewer id",
                    "text/plain; charset=utf-8",
                )
                return
            self._serve_mjpeg(viewer_id)
            return

        if request.path == "/api/preview.jpg":
            # 请求本身就是预览心跳；没有网页请求时后台不会额外编码画面。
            image = self.app_server.detection_state.request_preview_image()  # type: ignore[attr-defined]
            if image is None:
                self._send_bytes(
                    503,
                    b"Preview is not ready",
                    "text/plain; charset=utf-8",
                )
            else:
                self._send_bytes(200, image, "image/jpeg")
            return

        if request.path == "/favicon.ico":
            self._send_bytes(204, b"", "image/x-icon")
            return

        self._send_bytes(404, b"Not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        request = urlparse(self.path)
        if request.path in (
            "/api/preview/heartbeat",
            "/api/preview/stop",
        ):
            viewer_id = self._preview_viewer_id(request)
            if viewer_id is None:
                self._send_bytes(
                    400,
                    b"Missing or invalid preview viewer id",
                    "text/plain; charset=utf-8",
                )
                return

            state = self.app_server.detection_state  # type: ignore[attr-defined]
            if request.path == "/api/preview/heartbeat":
                state.renew_preview_viewer(viewer_id)
            else:
                state.stop_preview_viewer(viewer_id)
            self._send_bytes(204, b"", "text/plain; charset=utf-8")
            return

        if request.path == "/api/rearm-alert":
            state = self.app_server.detection_state  # type: ignore[attr-defined]
            confirm_values = parse_qs(request.query).get(
                "confirm_seconds",
                [],
            )
            confirm_seconds: float | None = None
            if confirm_values:
                try:
                    requested_confirm_seconds = float(confirm_values[0])
                except (TypeError, ValueError):
                    self._send_bytes(
                        400,
                        b"Invalid confirm seconds",
                        "text/plain; charset=utf-8",
                    )
                    return
                confirm_seconds = next(
                    (
                        option
                        for option in ALERT_CONFIRM_OPTIONS_SECONDS
                        if math.isclose(
                            requested_confirm_seconds,
                            option,
                            abs_tol=1e-9,
                        )
                    ),
                    None,
                )
                if confirm_seconds is None:
                    self._send_bytes(
                        400,
                        b"Unsupported confirm seconds",
                        "text/plain; charset=utf-8",
                    )
                    return

            rearmed = state.rearm_alert(confirm_seconds)
            snapshot = state.snapshot()
            payload = json.dumps(
                {
                    "rearmed": rearmed,
                    "instance_id": snapshot["instance_id"],
                    "event_id": snapshot["intrusion_event_id"],
                },
                separators=(",", ":"),
            ).encode("utf-8")
            self._send_bytes(200, payload, "application/json; charset=utf-8")
            return

        self._send_bytes(404, b"Not found", "text/plain; charset=utf-8")

    @staticmethod
    def _preview_viewer_id(request: Any) -> str | None:
        """只接受短 ASCII 会话编号，避免无限制占用后端状态。"""
        viewer_values = parse_qs(
            request.query,
            keep_blank_values=True,
        ).get("viewer", [])
        if len(viewer_values) != 1:
            return None
        viewer_id = viewer_values[0]
        if not 1 <= len(viewer_id) <= 128:
            return None
        if not all(
            character.isascii()
            and (character.isalnum() or character in "-_.")
            for character in viewer_id
        ):
            return None
        return viewer_id

    def _serve_mjpeg(self, viewer_id: str) -> None:
        """保持一个 HTTP 连接，持续推送最新的带框 JPEG 帧。"""
        try:
            state = self.app_server.detection_state  # type: ignore[attr-defined]
            # GET 本身先建立一个短租约，之后必须由网页心跳续期。
            if not state.start_preview_viewer(viewer_id):
                self._send_bytes(
                    410,
                    b"Preview viewer was stopped",
                    "text/plain; charset=utf-8",
                )
                return
            self.send_response(200)
            self.send_header(
                "Content-Type",
                "multipart/x-mixed-replace; boundary=frame",
            )
            self.send_header("Cache-Control", "no-store, no-cache, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.flush()

            last_sequence = -1
            while not self.app_server.shutdown_event.is_set():
                image, sequence, viewer_active = state.wait_for_preview_image(
                    viewer_id,
                    last_sequence,
                    WEB_PREVIEW_STREAM_WAIT_SECONDS,
                )
                if not viewer_active:
                    break
                if image is None:
                    # 没有检测画面时发送空白心跳，以便及时发现浏览器已断开。
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                    continue

                header = (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    + f"Content-Length: {len(image)}\r\n".encode("ascii")
                    + f"X-Frame-Sequence: {sequence}\r\n\r\n".encode("ascii")
                )
                self.wfile.write(header)
                self.wfile.write(image)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
                last_sequence = sequence
        except (BrokenPipeError, ConnectionResetError, OSError):
            # 用户关闭预览、刷新页面或离开网页时连接会自然断开。
            return
        finally:
            self.close_connection = True

    def _send_bytes(
        self,
        status: int,
        payload: bytes,
        content_type: str,
    ) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if payload:
                self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            # 浏览器提前关闭连接很常见，不需要影响本地检测。
            return

    def log_message(self, _format: str, *_args: Any) -> None:
        """关闭逐请求终端日志，保持 VS Code 输出简洁。"""


class LanWebServer(QThread):
    """在独立线程中运行局域网 HTTP 服务。"""

    server_started = Signal(str)
    server_failed = Signal(str)

    def __init__(
        self,
        state: LanDetectionState,
        port: int = WEB_PORT,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.state = state
        self.port = port
        self._server: LanHttpServer | None = None
        self._stop_requested = threading.Event()

    def run(self) -> None:
        try:
            index_page = (WEB_ROOT / "index.html").read_bytes()
            server = LanHttpServer((WEB_HOST, self.port), LanRequestHandler)
            server.detection_state = self.state  # type: ignore[attr-defined]
            server.index_page = index_page  # type: ignore[attr-defined]
            self._server = server
            if self._stop_requested.is_set():
                return
            server.timeout = 0.2
            actual_port = int(server.server_address[1])
            self.server_started.emit(
                f"http://{local_network_ip()}:{actual_port}"
            )
            # 使用带超时的单次请求循环，关闭开关时最多 0.2 秒即可退出，
            # 同时避免 shutdown() 与服务启动瞬间竞争造成界面卡住。
            while not self._stop_requested.is_set():
                server.handle_request()
        except Exception as exc:
            self.server_failed.emit(str(exc))
        finally:
            server = self._server
            self._server = None
            if server is not None:
                server.server_close()

    def stop(self) -> None:
        """通知请求循环退出，不阻塞 Qt 主界面。"""
        self._stop_requested.set()


def normalize_detection_region(
    region: DetectionRegion | None,
) -> DetectionRegion | None:
    """规范化 0～1 范围内的框选坐标，过小区域视为未设置。"""
    if region is None:
        return None

    x1, y1, x2, y2 = region
    left = min(max(min(x1, x2), 0.0), 1.0)
    top = min(max(min(y1, y2), 0.0), 1.0)
    right = min(max(max(x1, x2), 0.0), 1.0)
    bottom = min(max(max(y1, y2), 0.0), 1.0)
    if right - left < 0.005 or bottom - top < 0.005:
        return None
    return left, top, right, bottom


def detection_region_from_display_rect(
    video_rect: tuple[int, int, int, int],
    selection_rect: tuple[int, int, int, int],
) -> DetectionRegion | None:
    """把 QLabel 中的拖拽矩形转换成不受显示缩放影响的 0～1 坐标。"""
    video_left, video_top, video_width, video_height = video_rect
    selection_left, selection_top, selection_right, selection_bottom = (
        selection_rect
    )
    if video_width <= 0 or video_height <= 0:
        return None

    return normalize_detection_region(
        (
            (selection_left - video_left) / video_width,
            (selection_top - video_top) / video_height,
            (selection_right - video_left) / video_width,
            (selection_bottom - video_top) / video_height,
        )
    )


def detection_region_pixel_bounds(
    frame: Any,
    region: DetectionRegion | None,
) -> tuple[int, int, int, int] | None:
    """把归一化框选区域换算成适合图像切片的像素边界。"""
    normalized_region = normalize_detection_region(region)
    if normalized_region is None:
        return None

    frame_height, frame_width = frame.shape[:2]
    if frame_width <= 0 or frame_height <= 0:
        return None

    left, top, right, bottom = normalized_region
    x1 = min(max(math.floor(left * frame_width), 0), frame_width - 1)
    y1 = min(max(math.floor(top * frame_height), 0), frame_height - 1)
    x2 = min(max(math.ceil(right * frame_width), x1 + 1), frame_width)
    y2 = min(max(math.ceil(bottom * frame_height), y1 + 1), frame_height)
    return x1, y1, x2, y2


def crop_frame_to_detection_region(
    frame: Any,
    region: DetectionRegion | None,
) -> Any:
    """裁出网页应该显示的框选画面；未设置区域时返回完整画面。"""
    bounds = detection_region_pixel_bounds(frame, region)
    if bounds is None:
        return frame
    x1, y1, x2, y2 = bounds
    return frame[y1:y2, x1:x2]


def boxes_in_detection_region(
    frame: Any,
    result: Any,
    region_enabled: bool,
    region: DetectionRegion | None,
) -> list[Any]:
    """返回需要参与人数和告警判断的人体框。

    开启框选后，以人体框中心点是否落在区域内作为判断，避免框外人物仅有
    少量边缘与区域重叠时误触发告警。尚未完成框选时不统计任何人物。
    """
    if result.boxes is None:
        return []

    boxes = list(result.boxes)
    if not region_enabled:
        return boxes

    normalized_region = normalize_detection_region(region)
    if normalized_region is None:
        return []

    frame_height, frame_width = frame.shape[:2]
    left, top, right, bottom = normalized_region
    region_x1 = left * frame_width
    region_y1 = top * frame_height
    region_x2 = right * frame_width
    region_y2 = bottom * frame_height
    selected_boxes: list[Any] = []

    for box in boxes:
        x1, y1, x2, y2 = box.xyxy[0].detach().cpu().tolist()
        center_x = (float(x1) + float(x2)) / 2
        center_y = (float(y1) + float(y2)) / 2
        if (
            region_x1 <= center_x <= region_x2
            and region_y1 <= center_y <= region_y2
        ):
            selected_boxes.append(box)

    return selected_boxes


def make_intruder_jpeg(
    frame: Any,
    boxes: list[Any],
    detection_region: DetectionRegion | None = None,
) -> bytes | None:
    """裁剪框选区域内的所有人物，并生成网页告警使用的网格拼图。"""
    if not boxes:
        return None

    frame_height, frame_width = frame.shape[:2]
    region_bounds = detection_region_pixel_bounds(frame, detection_region)
    if region_bounds is None:
        region_x1, region_y1 = 0, 0
        region_x2, region_y2 = frame_width, frame_height
    else:
        region_x1, region_y1, region_x2, region_y2 = region_bounds
    person_crops: list[Any] = []

    for box in boxes:
        x1, y1, x2, y2 = map(int, box.xyxy[0].detach().cpu().tolist())
        box_width = max(x2 - x1, 0)
        box_height = max(y2 - y1, 0)
        if box_width == 0 or box_height == 0:
            continue

        # 每个人物周围保留少量环境，避免裁剪得过紧。
        margin_x = max(box_width // 8, 12)
        margin_y = max(box_height // 8, 12)
        crop_x1 = max(x1 - margin_x, region_x1)
        crop_y1 = max(y1 - margin_y, region_y1)
        crop_x2 = min(x2 + margin_x, region_x2)
        crop_y2 = min(y2 + margin_y, region_y2)
        crop = frame[crop_y1:crop_y2, crop_x1:crop_x2]
        if crop.size > 0:
            person_crops.append(crop)

    if not person_crops:
        return None

    # 网页只需下载一张图片，因此把所有人物排列到 16:9 画布中。
    canvas_width = 1280
    canvas_height = 720
    person_count = len(person_crops)
    columns = min(
        person_count,
        max(1, math.ceil(math.sqrt(person_count * 16 / 9))),
    )
    rows = math.ceil(person_count / columns)
    outer_padding = 12
    gap = 10
    cell_width = max(
        (canvas_width - outer_padding * 2 - gap * (columns - 1)) // columns,
        1,
    )
    cell_height = max(
        (canvas_height - outer_padding * 2 - gap * (rows - 1)) // rows,
        1,
    )

    # 使用原帧创建相同数据类型的画布，再统一填充为深色背景。
    canvas = cv2.resize(
        frame,
        (canvas_width, canvas_height),
        interpolation=cv2.INTER_AREA,
    )
    canvas[:] = (11, 13, 16)

    for index, crop in enumerate(person_crops):
        row = index // columns
        column = index % columns
        cell_x = outer_padding + column * (cell_width + gap)
        cell_y = outer_padding + row * (cell_height + gap)

        crop_height, crop_width = crop.shape[:2]
        available_width = max(cell_width - 12, 1)
        available_height = max(cell_height - 12, 1)
        scale = min(
            available_width / crop_width,
            available_height / crop_height,
        )
        resized_width = max(int(crop_width * scale), 1)
        resized_height = max(int(crop_height * scale), 1)
        interpolation = cv2.INTER_CUBIC if scale > 1 else cv2.INTER_AREA
        resized_crop = cv2.resize(
            crop,
            (resized_width, resized_height),
            interpolation=interpolation,
        )

        image_x = cell_x + (cell_width - resized_width) // 2
        image_y = cell_y + (cell_height - resized_height) // 2
        canvas[
            image_y:image_y + resized_height,
            image_x:image_x + resized_width,
        ] = resized_crop
        cv2.rectangle(
            canvas,
            (cell_x, cell_y),
            (cell_x + cell_width - 1, cell_y + cell_height - 1),
            (0, 220, 80),
            2,
            cv2.LINE_AA,
        )

    encoded, jpeg = cv2.imencode(
        ".jpg",
        canvas,
        [int(cv2.IMWRITE_JPEG_QUALITY), 90],
    )
    return jpeg.tobytes() if encoded else None


# ---------- 检测结果绘制 ----------
def draw_tracking_boxes(frame: Any, boxes: list[Any]) -> int:
    """在画面上绘制人体框，返回当前检测到的人数。"""
    people_count = 0
    # OpenCV 的颜色顺序是 BGR，这里使用绿色绘制人体框和标签。
    color = (0, 220, 80)

    for box in boxes:
        # xyxy 是矩形框左上角和右下角坐标；id 是 ByteTrack 分配的临时编号。
        x1, y1, x2, y2 = map(int, box.xyxy[0].detach().cpu().tolist())
        confidence = float(box.conf[0].detach().cpu().item())
        track_id = (
            int(box.id[0].detach().cpu().item()) if box.id is not None else None
        )
        people_count += 1

        cv2.rectangle(
            frame,
            (x1, y1),
            (x2, y2),
            color,
            2,
            cv2.LINE_AA,
        )

        label = f"Person  {confidence:.2f}"
        if track_id is not None:
            label = f"Person #{track_id}  {confidence:.2f}"

        # 标签只绘制单层绿色文字，不使用底色、描边或阴影。
        font = cv2.FONT_HERSHEY_SIMPLEX
        font_scale = 0.55
        thickness = 2
        label_position = (x1, max(y1 - 8, 20))
        cv2.putText(
            frame,
            label,
            label_position,
            font,
            font_scale,
            color,
            thickness,
            cv2.LINE_AA,
        )

    return people_count


def draw_detection_region(
    frame: Any,
    region: DetectionRegion | None,
) -> None:
    """在本地和网页预览中绘制当前生效的黄色识别区域。"""
    normalized_region = normalize_detection_region(region)
    if normalized_region is None:
        return

    frame_height, frame_width = frame.shape[:2]
    left, top, right, bottom = normalized_region
    x1 = int(round(left * max(frame_width - 1, 0)))
    y1 = int(round(top * max(frame_height - 1, 0)))
    x2 = int(round(right * max(frame_width - 1, 0)))
    y2 = int(round(bottom * max(frame_height - 1, 0)))
    color = (0, 190, 255)
    cv2.rectangle(
        frame,
        (x1, y1),
        (x2, y2),
        color,
        2,
        cv2.LINE_AA,
    )


# ---------- 摄像头读取与人体检测线程 ----------
class CameraWorker(QThread):
    """在后台线程中读取摄像头并执行人体检测。"""

    # 后台线程不能直接操作界面，通过信号把画面和状态发送给主线程。
    frame_ready = Signal(QImage)
    stats_changed = Signal(int, float)
    intrusion_detected = Signal(int, object)
    status_changed = Signal(str)
    runtime_info_changed = Signal(str)
    error_occurred = Signal(str)

    def __init__(
        self,
        camera_index: int,
        camera_name: str,
        mirror: bool,
        frame_width: int,
        frame_height: int,
        rotation_degrees: int = 0,
        detection_region_enabled: bool = False,
        detection_region: DetectionRegion | None = None,
        lan_state: LanDetectionState | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.camera_index = camera_index
        self.camera_name = camera_name
        self.mirror = mirror
        self.rotation_degrees = normalize_rotation_degrees(rotation_degrees)
        self.frame_width = frame_width
        self.frame_height = frame_height
        self.lan_state = lan_state
        self._running = True
        self._detection_region_lock = threading.RLock()
        self._detection_region_enabled = detection_region_enabled
        self._detection_region = normalize_detection_region(detection_region)
        self._detection_region_generation = 0

    def stop(self) -> None:
        """通知循环安全退出，摄像头会在 finally 中释放。"""
        self._running = False

    def set_mirror(self, enabled: bool) -> None:
        """检测运行期间线程安全切换镜像，并重新开始人物持续计时。"""
        with self._detection_region_lock:
            if self.mirror == enabled:
                return
            self.mirror = enabled
            self._detection_region_generation += 1

    def mirror_enabled(self) -> bool:
        """返回当前帧应该使用的镜像状态。"""
        with self._detection_region_lock:
            return self.mirror

    def set_rotation_degrees(self, rotation_degrees: int) -> None:
        """检测运行期间线程安全切换画面方向。"""
        normalized_degrees = normalize_rotation_degrees(rotation_degrees)
        with self._detection_region_lock:
            if self.rotation_degrees == normalized_degrees:
                return
            self.rotation_degrees = normalized_degrees
            self._detection_region_generation += 1

    def frame_transform_state(self) -> tuple[bool, int]:
        """返回当前帧应该使用的一致镜像和旋转状态。"""
        with self._detection_region_lock:
            return self.mirror, self.rotation_degrees

    def set_detection_region(
        self,
        enabled: bool,
        region: DetectionRegion | None,
    ) -> None:
        """从界面线程安全更新框选开关和归一化区域。"""
        normalized_region = normalize_detection_region(region)
        with self._detection_region_lock:
            self._detection_region_enabled = enabled
            self._detection_region = normalized_region
            self._detection_region_generation += 1

    def detection_region_state(
        self,
    ) -> tuple[bool, DetectionRegion | None, int]:
        """返回单帧处理中使用的一致框选状态。"""
        with self._detection_region_lock:
            return (
                self._detection_region_enabled,
                self._detection_region,
                self._detection_region_generation,
            )

    def run(self) -> None:
        """QThread 的线程入口；耗时任务在这里执行，不阻塞主界面。"""
        camera: cv2.VideoCapture | None = None

        try:
            # .pt 文件是已经训练好的模型权重，Ultralytics 负责交给 PyTorch 加载。
            inference_device, device_label = select_inference_device()
            self.status_changed.emit("正在加载人体检测模型…")
            model = YOLO(str(MODEL_PATH))
            # 显式移动模型并在每次 track 调用中指定设备，避免 Windows 环境
            # 因默认设备选择或打包差异而悄悄回退到 CPU。
            model.to(inference_device)

            # 使用 OpenCV 打开指定编号的摄像头，并请求界面中选择的分辨率。
            self.status_changed.emit(f"正在打开{self.camera_name}…")
            camera = open_camera(self.camera_index)
            camera.set(cv2.CAP_PROP_FRAME_WIDTH, self.frame_width)
            camera.set(cv2.CAP_PROP_FRAME_HEIGHT, self.frame_height)
            # 尽量只保留最新帧，减少实时检测时的画面延迟。
            camera.set(cv2.CAP_PROP_BUFFERSIZE, 1)

            if not camera.isOpened():
                raise RuntimeError(
                    f"无法打开{self.camera_name}。"
                    "请检查摄像头是否被占用，以及 VS Code/Python 的摄像头权限。"
                )

            # 扫描阶段已经验证过该模式；启动时再次以实际帧严格确认，防止
            # 驱动静默回退到其他尺寸却仍在界面上显示为已选分辨率。
            initial_ok, initial_frame = safe_camera_read(camera)
            if not initial_ok or initial_frame is None:
                raise RuntimeError("无法读取摄像头画面。")
            actual_height, actual_width = initial_frame.shape[:2]
            if (actual_width, actual_height) != (
                self.frame_width,
                self.frame_height,
            ):
                raise RuntimeError(
                    f"{self.camera_name}未能提供请求的 "
                    f"{self.frame_width}×{self.frame_height} 分辨率，"
                    f"实际返回 {actual_width}×{actual_height}。请刷新摄像头列表。"
                )
            self.runtime_info_changed.emit(
                "视频分辨率："
                f"{actual_width}×{actual_height}"
                f" · 推理设备：{device_label}"
            )

            previous_time = time.perf_counter()
            displayed_fps = 0.0
            frame_number = 0
            person_visible_since: float | None = None
            intrusion_sent = False
            last_preview_encode_time = 0.0
            alert_rearm_generation = (
                self.lan_state.alert_rearm_generation()
                if self.lan_state is not None
                else 0
            )
            detection_region_generation = self.detection_region_state()[2]
            pending_frame: Any = initial_frame

            while self._running:
                if pending_frame is not None:
                    frame = pending_frame
                    pending_frame = None
                else:
                    ok, frame = safe_camera_read(camera)
                    if not ok:
                        raise RuntimeError("无法继续读取摄像头画面。")

                mirror_enabled, rotation_degrees = self.frame_transform_state()
                if mirror_enabled:
                    frame = cv2.flip(frame, 1)
                frame = rotate_frame_clockwise(frame, rotation_degrees)

                # persist=True 让 ByteTrack 保留前后帧状态，使矩形框跟随同一人物。
                # classes=[0] 表示只保留人体，忽略车辆、动物等其他 COCO 类别。
                result = model.track(
                    source=frame,
                    persist=True,
                    tracker=TRACKER_NAME,
                    classes=[PERSON_CLASS_ID],
                    conf=0.2,
                    imgsz=640,
                    device=inference_device,
                    verbose=False,
                )[0]
                (
                    detection_region_enabled,
                    detection_region,
                    current_detection_region_generation,
                ) = self.detection_region_state()
                selected_boxes = boxes_in_detection_region(
                    frame,
                    result,
                    detection_region_enabled,
                    detection_region,
                )
                people_count = len(selected_boxes)
                active_detection_region = (
                    detection_region if detection_region_enabled else None
                )

                # 人物连续出现满网页选择的确认时间后，才产生一次警告事件。
                # 网页退出警告后会重新布防；人物仍在时会按所选时间产生新事件。
                detection_time = time.perf_counter()
                intrusion_jpeg: bytes | None = None
                alert_confirm_seconds = ALERT_CONFIRM_SECONDS
                if (
                    current_detection_region_generation
                    != detection_region_generation
                ):
                    detection_region_generation = (
                        current_detection_region_generation
                    )
                    person_visible_since = (
                        detection_time if people_count > 0 else None
                    )
                    intrusion_sent = False
                if self.lan_state is not None:
                    alert_confirm_seconds = (
                        self.lan_state.alert_confirm_seconds()
                    )
                    current_rearm_generation = (
                        self.lan_state.alert_rearm_generation()
                    )
                    if current_rearm_generation != alert_rearm_generation:
                        alert_rearm_generation = current_rearm_generation
                        person_visible_since = (
                            detection_time if people_count > 0 else None
                        )
                        intrusion_sent = False
                if people_count > 0:
                    if person_visible_since is None:
                        person_visible_since = detection_time
                    if (
                        not intrusion_sent
                        and detection_time - person_visible_since
                        >= alert_confirm_seconds
                    ):
                        intrusion_jpeg = make_intruder_jpeg(
                            frame,
                            selected_boxes,
                            active_detection_region,
                        )
                        intrusion_sent = intrusion_jpeg is not None
                else:
                    person_visible_since = None
                    intrusion_sent = False

                people_count = draw_tracking_boxes(frame, selected_boxes)
                if detection_region_enabled:
                    draw_detection_region(frame, detection_region)
                if intrusion_jpeg is not None:
                    self.intrusion_detected.emit(people_count, intrusion_jpeg)

                now = time.perf_counter()
                if (
                    self.lan_state is not None
                    and self.lan_state.preview_requested_recently()
                    and now - last_preview_encode_time
                    >= WEB_PREVIEW_INTERVAL_SECONDS
                ):
                    # 框选生效时网页只接收所选区域；桌面仍显示完整摄像头画面。
                    preview_frame = crop_frame_to_detection_region(
                        frame,
                        active_detection_region,
                    )
                    preview_height, preview_width = preview_frame.shape[:2]
                    if preview_width > WEB_PREVIEW_MAX_WIDTH:
                        scale = WEB_PREVIEW_MAX_WIDTH / preview_width
                        preview_frame = cv2.resize(
                            preview_frame,
                            (
                                WEB_PREVIEW_MAX_WIDTH,
                                max(int(preview_height * scale), 1),
                            ),
                            interpolation=cv2.INTER_AREA,
                        )
                    encoded, preview_jpeg = cv2.imencode(
                        ".jpg",
                        preview_frame,
                        [int(cv2.IMWRITE_JPEG_QUALITY), 78],
                    )
                    last_preview_encode_time = now
                    if encoded:
                        self.lan_state.update_preview_image(
                            preview_jpeg.tobytes()
                        )

                instant_fps = 1.0 / max(now - previous_time, 1e-6)
                previous_time = now
                # 使用指数移动平均平滑 FPS，避免数字每帧剧烈跳动。
                displayed_fps = (
                    instant_fps
                    if frame_number == 0
                    else displayed_fps * 0.9 + instant_fps * 0.1
                )
                frame_number += 1

                # OpenCV 使用 BGR，Qt 使用 RGB，因此显示前需要转换颜色顺序。
                rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                height, width, channels = rgb_frame.shape
                image = QImage(
                    rgb_frame.data,
                    width,
                    height,
                    channels * width,
                    QImage.Format.Format_RGB888,
                    # copy() 让 QImage 拥有独立内存，避免下一帧覆盖当前画面。
                ).copy()

                self.frame_ready.emit(image)
                self.stats_changed.emit(people_count, displayed_fps)

        except Exception as exc:
            if self._running:
                self.error_occurred.emit(str(exc))
        finally:
            if camera is not None:
                camera.release()


# ---------- 本地摄像头扫描线程 ----------
class CameraScanWorker(QThread):
    """在后台扫描摄像头名称及经实际取帧验证的分辨率。"""

    cameras_found = Signal(object)

    def __init__(self, max_camera_count: int = 6, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.max_camera_count = max_camera_count
        self._running = True

    def stop(self) -> None:
        self._running = False

    def run(self) -> None:
        """QThread 的线程入口；耗时任务在这里执行，不阻塞主界面。"""
        camera_profiles: list[dict[str, Any]] = []
        camera_metadata = self._camera_metadata()
        scan_count = (
            min(self.max_camera_count, len(camera_metadata))
            if camera_metadata
            else self.max_camera_count
        )

        # Qt 能枚举设备时只探测对应数量，避免 Windows 对不存在的索引
        # 同时输出 DSHOW 和 MSMF 警告；枚举不可用时才回退扫描 0～5。
        for index in range(scan_count):
            if not self._running:
                break

            camera = open_camera(index)
            if not camera.isOpened():
                camera.release()
                continue
            # 分辨率逐项使用独立连接验证，因此先释放探测连接。
            camera.release()
            metadata = (
                camera_metadata[index]
                if index < len(camera_metadata)
                else None
            )
            advertised_resolutions = (
                metadata["resolutions"] if metadata else []
            )
            resolutions = self._supported_resolutions(
                index,
                advertised_resolutions,
            )
            if resolutions:
                name = metadata["name"] if metadata else "摄像头"
                camera_profiles.append(
                    {
                        "index": index,
                        "name": name,
                        "resolutions": resolutions,
                    }
                )

        if self._running:
            self.cameras_found.emit(camera_profiles)

    @staticmethod
    def _camera_metadata() -> list[dict[str, Any]]:
        """通过系统多媒体接口获取设备名称及驱动公布的视频尺寸。"""
        try:
            from PySide6.QtMultimedia import QMediaDevices

            metadata: list[dict[str, Any]] = []
            for device in QMediaDevices.videoInputs():
                resolutions = sorted(
                    {
                        (
                            camera_format.resolution().width(),
                            camera_format.resolution().height(),
                        )
                        for camera_format in device.videoFormats()
                        if camera_format.resolution().width() > 0
                        and camera_format.resolution().height() > 0
                    },
                    key=lambda size: (size[0] * size[1], size[0], size[1]),
                )
                metadata.append(
                    {
                        "device_id": bytes(device.id()),
                        "name": device.description().strip() or "摄像头",
                        "resolutions": resolutions,
                    }
                )
            return CameraScanWorker._metadata_in_opencv_order(metadata)
        except Exception:
            # 系统枚举失败仍可用 OpenCV 探测；界面不会退回数字索引。
            return []

    @staticmethod
    def _metadata_in_opencv_order(
        metadata: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """在 macOS 上按 AVFoundation 唯一 ID 对齐 OpenCV 的摄像头索引。"""
        if sys.platform != "darwin":
            return metadata

        # OpenCV 的 AVFoundation 后端先按 AVCaptureDevice.uniqueID 排序，
        # 再把排序后的位置作为 VideoCapture 的数字索引。Qt 返回的列表顺序
        # 不保证相同，因此必须用 QCameraDevice.id() 做相同排序。
        if not metadata or any(not item.get("device_id") for item in metadata):
            return metadata
        return sorted(metadata, key=lambda item: item["device_id"])

    def _supported_resolutions(
        self,
        camera_index: int,
        advertised_resolutions: list[tuple[int, int]],
    ) -> list[tuple[int, int]]:
        """用独立连接验证各模式，避免失败模式破坏后续 Windows 读取。"""
        supported: list[tuple[int, int]] = []
        candidates = (
            advertised_resolutions
            if advertised_resolutions
            else list(CAMERA_RESOLUTION_CANDIDATES)
        )
        for width, height in candidates:
            if not self._running:
                break
            camera = open_camera(camera_index)
            try:
                if not camera.isOpened():
                    continue
                camera.set(cv2.CAP_PROP_FRAME_WIDTH, width)
                camera.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
                ok, frame = safe_camera_read(camera)
                if not ok or frame is None:
                    continue
                actual_height, actual_width = frame.shape[:2]
                if (actual_width, actual_height) == (width, height):
                    supported.append((width, height))
            except cv2.error:
                continue
            finally:
                camera.release()

        if supported or not self._running:
            return supported

        # 某些驱动只公布或接受默认模式；取得默认尺寸后重新连接验证。
        camera = open_camera(camera_index)
        try:
            if not camera.isOpened():
                return []
            ok, frame = safe_camera_read(camera)
            if not ok or frame is None:
                return []
            actual_height, actual_width = frame.shape[:2]
        finally:
            camera.release()

        camera = open_camera(camera_index)
        try:
            if not camera.isOpened():
                return []
            camera.set(cv2.CAP_PROP_FRAME_WIDTH, actual_width)
            camera.set(cv2.CAP_PROP_FRAME_HEIGHT, actual_height)
            verify_ok, verify_frame = safe_camera_read(camera)
            if verify_ok and verify_frame is not None:
                verify_height, verify_width = verify_frame.shape[:2]
                if (verify_width, verify_height) == (
                    actual_width,
                    actual_height,
                ):
                    return [(actual_width, actual_height)]
        except cv2.error:
            return []
        finally:
            camera.release()
        return []


# ---------- 始终带清晰边框的复选框 ----------
class FramedCheckBox(QCheckBox):
    """在 Windows 上统一绘制边框和居中的对勾。"""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        # 隐藏系统自带的指示图形，但保留固定空间供自绘内容使用。
        self.setStyleSheet("""
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
                background: transparent;
                border: none;
            }
        """)

    def paintEvent(self, event: Any) -> None:
        super().paintEvent(event)

        option = QStyleOptionButton()
        self.initStyleOption(option)
        indicator_rect = self.style().subElementRect(
            QStyle.SubElement.SE_CheckBoxIndicator,
            option,
            self,
        ).adjusted(1, 1, -2, -2)

        border_color = QColor(
            "#ffffff" if self.property("darkTheme") else "#000000"
        )
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(border_color, 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(indicator_rect, 3, 3)

        if self.checkState() == Qt.CheckState.Checked:
            check_pen = QPen(border_color, 2)
            check_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            check_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(check_pen)
            check_path = QPainterPath()
            check_path.moveTo(
                indicator_rect.left() + indicator_rect.width() * 0.22,
                indicator_rect.top() + indicator_rect.height() * 0.52,
            )
            check_path.lineTo(
                indicator_rect.left() + indicator_rect.width() * 0.43,
                indicator_rect.top() + indicator_rect.height() * 0.73,
            )
            check_path.lineTo(
                indicator_rect.left() + indicator_rect.width() * 0.79,
                indicator_rect.top() + indicator_rect.height() * 0.27,
            )
            painter.drawPath(check_path)


# macOS 使用系统原生外观，只有 Windows 需要额外补画边框。
PlatformCheckBox = FramedCheckBox if sys.platform == "win32" else QCheckBox


# ---------- 始终向下并对齐的选择框 ----------
class AlignedComboBox(QComboBox):
    """让下拉列表与选择框等宽，并固定显示在正下方。"""

    def showPopup(self) -> None:
        """先显示列表，再在下一次事件循环中修正位置与内容宽度。"""
        super().showPopup()
        QTimer.singleShot(0, self._align_popup)

    def _align_popup(self) -> None:
        popup = self.view().window()
        if popup is None:
            return

        # 选择框的最小宽度已包含列表内部边距，此处保持左右严格对齐。
        popup.resize(self.width(), popup.height())
        below_left = self.mapToGlobal(QPoint(0, self.height()))
        popup.move(below_left)


# ---------- 可点击的信息标签 ----------
class ClickableLabel(QLabel):
    """可单击，并在文字过长时自动左右往返滚动。"""

    clicked = Signal()

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self._scroll_offset = 0
        self._scroll_direction = 1
        self._scroll_pause_ticks = 20
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setInterval(30)
        self._scroll_timer.timeout.connect(self._advance_scroll)
        self._refresh_scroll_state()

    def setText(self, text: str) -> None:
        super().setText(text)
        if hasattr(self, "_scroll_timer"):
            self._scroll_offset = 0
            self._scroll_direction = 1
            self._scroll_pause_ticks = 20
            self._refresh_scroll_state()

    def _maximum_scroll_offset(self) -> int:
        metrics = self.fontMetrics()
        segments = self._runtime_scroll_segments()
        if segments is not None:
            fixed_text, device_text = segments
            fixed_width = metrics.horizontalAdvance(fixed_text)
            device_view_width = max(
                self.contentsRect().width() - fixed_width,
                0,
            )
            return max(
                metrics.horizontalAdvance(device_text) - device_view_width,
                0,
            )

        text_width = metrics.horizontalAdvance(self.text())
        return max(text_width - self.contentsRect().width(), 0)

    def _runtime_scroll_segments(self) -> tuple[str, str] | None:
        """运行信息只将“推理设备：”之后的显卡名称作为滚动部分。"""
        marker = "推理设备："
        marker_start = self.text().find(marker)
        if marker_start < 0:
            return None
        device_start = marker_start + len(marker)
        return self.text()[:device_start], self.text()[device_start:]

    def _refresh_scroll_state(self) -> None:
        if self._maximum_scroll_offset() > 0:
            self._scroll_timer.start()
        else:
            self._scroll_timer.stop()
            self._scroll_offset = 0
        self.update()

    def _advance_scroll(self) -> None:
        maximum_offset = self._maximum_scroll_offset()
        if maximum_offset <= 0:
            self._refresh_scroll_state()
            return

        if self._scroll_pause_ticks > 0:
            self._scroll_pause_ticks -= 1
            return

        self._scroll_offset += self._scroll_direction
        if self._scroll_offset >= maximum_offset:
            self._scroll_offset = maximum_offset
            self._scroll_direction = -1
            self._scroll_pause_ticks = 20
        elif self._scroll_offset <= 0:
            self._scroll_offset = 0
            self._scroll_direction = 1
            self._scroll_pause_ticks = 20
        self.update()

    def paintEvent(self, event: Any) -> None:
        if self._maximum_scroll_offset() <= 0:
            super().paintEvent(event)
            return

        painter = QPainter(self)
        painter.setClipRect(self.contentsRect())
        painter.setPen(self.palette().color(self.foregroundRole()))
        paint_font = self.font()
        if paint_font.pointSizeF() <= 0 and paint_font.pixelSize() > 0:
            point_size = (
                paint_font.pixelSize()
                * 72.0
                / max(self.logicalDpiY(), 1)
            )
            paint_font.setPointSizeF(max(point_size, 1.0))
        if paint_font.pointSizeF() > 0:
            painter.setFont(paint_font)
        metrics = self.fontMetrics()
        baseline = (
            self.contentsRect().top()
            + (self.contentsRect().height() - metrics.height()) // 2
            + metrics.ascent()
        )
        segments = self._runtime_scroll_segments()
        if segments is None:
            painter.drawText(
                self.contentsRect().left() - self._scroll_offset,
                baseline,
                self.text(),
            )
            return

        fixed_text, device_text = segments
        fixed_left = self.contentsRect().left()
        painter.drawText(fixed_left, baseline, fixed_text)
        device_left = fixed_left + metrics.horizontalAdvance(fixed_text)
        device_rect = QRect(
            device_left,
            self.contentsRect().top(),
            max(self.contentsRect().right() - device_left + 1, 0),
            self.contentsRect().height(),
        )
        painter.setClipRect(device_rect)
        painter.drawText(
            device_left - self._scroll_offset,
            baseline,
            device_text,
        )

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._scroll_offset = min(
            self._scroll_offset,
            self._maximum_scroll_offset(),
        )
        self._refresh_scroll_state()

    def mouseReleaseEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


# ---------- 保持宽高比的视频显示控件 ----------
class PreviewLabel(QLabel):
    """保持视频宽高比的预览控件。"""

    double_clicked = Signal()
    detection_region_selected = Signal(object)

    def __init__(self) -> None:
        super().__init__("摄像头画面将在这里显示")
        self._source_pixmap: QPixmap | None = None
        self._drag_origin: QPoint | None = None
        self._detection_region_enabled = False
        self._detection_region: DetectionRegion | None = None
        self._selection_start: QPoint | None = None
        self._selection_end: QPoint | None = None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # 不限制预览区域最小尺寸，始终跟随窗口布局自动伸缩。
        self.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Ignored,
        )
        self.setObjectName("previewLabel")
        # 未显示摄像头画面时使用当前主题背景；收到视频帧后再切换为黑色。
        self.setProperty("hasVideo", False)
        # 无需按下鼠标也接收移动事件，用于显示小窗口悬浮按钮。
        self.setMouseTracking(True)

    def set_video_image(self, image: QImage) -> None:
        """保存最新原始画面；显示尺寸由绘制事件按当前区域决定。"""
        first_frame = self._source_pixmap is None
        self._source_pixmap = QPixmap.fromImage(image)
        self._set_has_video(True)
        if first_frame:
            # QLabel 不持有视频 pixmap，避免原始帧尺寸参与布局计算。
            self.clear()
            self.setText("")
        self.update()

    def show_placeholder(self) -> None:
        """停止检测时删除最后一帧，恢复空白提示。"""
        self._source_pixmap = None
        self._selection_start = None
        self._selection_end = None
        self._set_has_video(False)
        self.clear()
        self.setText("摄像头画面将在这里显示")

    def set_detection_region_enabled(self, enabled: bool) -> None:
        """开启或关闭画面拖拽框选，不改变已经保存的区域。"""
        self._detection_region_enabled = enabled
        self._selection_start = None
        self._selection_end = None
        self.setCursor(
            Qt.CursorShape.CrossCursor
            if enabled
            else Qt.CursorShape.ArrowCursor
        )
        self.update()

    def set_detection_region(
        self,
        region: DetectionRegion | None,
    ) -> None:
        """保存归一化识别区域，并在缩放后的画面上叠加显示。"""
        self._detection_region = normalize_detection_region(region)
        self.update()

    def _set_has_video(self, has_video: bool) -> None:
        """切换占位背景和视频背景，并立即刷新动态属性样式。"""
        if self.property("hasVideo") == has_video:
            return

        self.setProperty("hasVideo", has_video)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        # 视频在 paintEvent 中读取当前尺寸绘制，不生成中间缩放图。
        self.update()
        window = self.window()
        if getattr(window, "compact_mode", False) and hasattr(
            window, "position_compact_controls"
        ):
            window.position_compact_controls()

    def paintEvent(self, event: Any) -> None:
        """在视频上绘制正在拖拽或已经保存的识别区域。"""
        super().paintEvent(event)
        if self._source_pixmap is None:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawPixmap(self._video_display_rect(), self._source_pixmap)

        if not self._detection_region_enabled:
            return

        selection_rect: QRect | None = None
        if self._selection_start is not None and self._selection_end is not None:
            selection_rect = QRect(
                self._selection_start,
                self._selection_end,
            ).normalized()
        elif self._detection_region is not None:
            selection_rect = self._region_display_rect(
                self._detection_region
            )

        if selection_rect is None or selection_rect.isEmpty():
            return

        painter.fillRect(selection_rect, QColor(255, 190, 0, 38))
        pen = QPen(QColor(255, 190, 0), 2)
        pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.drawRect(selection_rect)

    def leaveEvent(self, event: Any) -> None:
        """鼠标真正离开小窗口后隐藏悬浮按钮。"""
        QTimer.singleShot(80, self._hide_compact_controls_if_outside)
        super().leaveEvent(event)

    def _hide_compact_controls_if_outside(self) -> None:
        window = self.window()
        if not getattr(window, "compact_mode", False):
            return

        local_position = self.mapFromGlobal(QCursor.pos())
        if not self.rect().contains(local_position):
            window.hide_compact_controls()


    def mouseDoubleClickEvent(self, event: Any) -> None:
        """双击小窗口画面时请求恢复完整界面。"""
        if (
            self._detection_region_enabled
            and not getattr(self.window(), "compact_mode", False)
        ):
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self.double_clicked.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mousePressEvent(self, event: Any) -> None:
        """普通模式拖拽识别区域；小窗口模式拖动整个窗口。"""
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._detection_region_enabled
            and self._source_pixmap is not None
            and not getattr(self.window(), "compact_mode", False)
        ):
            video_rect = self._video_display_rect()
            position = event.position().toPoint()
            if video_rect.contains(position):
                position = self._clamp_to_video(position)
                self._selection_start = position
                self._selection_end = position
                self.update()
                event.accept()
                return
        if (
            event.button() == Qt.MouseButton.LeftButton
            and getattr(self.window(), "compact_mode", False)
        ):
            self._drag_origin = event.globalPosition().toPoint()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: Any) -> None:
        window = self.window()
        if getattr(window, "compact_mode", False):
            window.update_compact_controls_visibility(event.position().toPoint())
        if (
            self._selection_start is not None
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            self._selection_end = self._clamp_to_video(
                event.position().toPoint()
            )
            self.update()
            event.accept()
            return
        if (
            self._drag_origin is not None
            and event.buttons() & Qt.MouseButton.LeftButton
            and getattr(self.window(), "compact_mode", False)
        ):
            current_position = event.globalPosition().toPoint()
            offset = current_position - self._drag_origin
            self.window().move(self.window().pos() + offset)
            self._drag_origin = current_position
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: Any) -> None:
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._selection_start is not None
        ):
            self._selection_end = self._clamp_to_video(
                event.position().toPoint()
            )
            selection_rect = QRect(
                self._selection_start,
                self._selection_end,
            ).normalized()
            self._selection_start = None
            self._selection_end = None

            if selection_rect.width() >= 8 and selection_rect.height() >= 8:
                video_rect = self._video_display_rect()
                region = detection_region_from_display_rect(
                    (
                        video_rect.left(),
                        video_rect.top(),
                        video_rect.width(),
                        video_rect.height(),
                    ),
                    (
                        selection_rect.left(),
                        selection_rect.top(),
                        selection_rect.right(),
                        selection_rect.bottom(),
                    ),
                )
                if region is not None:
                    self._detection_region = region
                    self.detection_region_selected.emit(region)
            self.update()
            event.accept()
            return

        self._drag_origin = None
        super().mouseReleaseEvent(event)

    def _video_display_rect(self) -> QRect:
        """返回保持宽高比后视频在 QLabel 中实际占据的矩形。"""
        if self._source_pixmap is None:
            return QRect()

        scaled_size = self._source_pixmap.size().scaled(
            self.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
        )
        left = (self.width() - scaled_size.width()) // 2
        top = (self.height() - scaled_size.height()) // 2
        return QRect(left, top, scaled_size.width(), scaled_size.height())

    def _clamp_to_video(self, position: QPoint) -> QPoint:
        """把鼠标位置限制在实际视频矩形内部。"""
        video_rect = self._video_display_rect()
        if video_rect.isEmpty():
            return position
        return QPoint(
            min(max(position.x(), video_rect.left()), video_rect.right()),
            min(max(position.y(), video_rect.top()), video_rect.bottom()),
        )

    def _region_display_rect(
        self,
        region: DetectionRegion,
    ) -> QRect:
        """把 0～1 的视频坐标转换成当前 QLabel 显示坐标。"""
        video_rect = self._video_display_rect()
        left, top, right, bottom = region
        video_width = max(video_rect.width() - 1, 0)
        video_height = max(video_rect.height() - 1, 0)
        return QRect(
            QPoint(
                video_rect.left() + round(left * video_width),
                video_rect.top() + round(top * video_height),
            ),
            QPoint(
                video_rect.left() + round(right * video_width),
                video_rect.top() + round(bottom * video_height),
            ),
        ).normalized()

# ---------- 主窗口与界面状态控制 ----------
class CameraWindow(QMainWindow):
    """程序主窗口，负责界面状态和后台线程的启动/停止。"""

    def __init__(self) -> None:
        super().__init__()
        # 保存当前活动线程的引用，用于启动、停止以及过滤过期信号。
        self.worker: CameraWorker | None = None
        self.scan_worker: CameraScanWorker | None = None
        self.lan_state = LanDetectionState()
        self.web_server: LanWebServer | None = None
        self.settings = QSettings(
            SETTINGS_ORGANIZATION,
            SETTINGS_APPLICATION,
        )
        self._preferred_camera_index: int | None = None
        self._preferred_resolution: tuple[int, int] | None = None
        self._camera_profiles: dict[int, dict[str, Any]] = {}
        self._lan_port = WEB_PORT
        self._pending_lan_restart = False
        # 记录界面模式，恢复普通窗口时需要使用这些状态。
        self.compact_mode = False
        self.dark_mode = False
        self.rotation_degrees = 0
        self.detection_region: DetectionRegion | None = None
        self._lan_info_text = "局域网：已关闭"
        self._lan_info_tooltip = ""
        self._runtime_info_text = ""
        self._show_runtime_info = False
        self._normal_geometry: Any = None
        self._normal_minimum_size: Any = None
        self._was_maximized = False
        self.setWindowTitle("AlertZone-人员进入检测与报警 · ©H-Knight")

        self.camera_combo = AlignedComboBox()
        # 摄像头和分辨率选择框随窗口宽度共同伸缩。
        self.camera_combo.setMinimumWidth(70)
        self.camera_combo.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Fixed,
        )

        self.quality_combo = AlignedComboBox()
        self.quality_combo.addItem("正在获取分辨率…", None)
        self.quality_combo.setEnabled(False)
        self.quality_combo.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Fixed,
        )
        self._sync_selector_widths()
        self.camera_field_label = QLabel("摄像头：")
        self.resolution_field_label = QLabel("分辨率：")

        self.refresh_button = QPushButton("刷新")
        self.refresh_button.clicked.connect(self.refresh_cameras)

        self.mirror_checkbox = PlatformCheckBox("镜像")
        self.mirror_checkbox.setChecked(True)
        self.mirror_checkbox.setToolTip("检测运行期间也可随时切换镜像画面")
        self.mirror_checkbox.toggled.connect(self.set_mirror_enabled)

        self.detection_region_button = QPushButton("范围识别")
        self.detection_region_button.setObjectName("regionButton")
        self.detection_region_button.setCheckable(True)
        self.detection_region_button.setToolTip(
            "开启后在画面上拖拽识别区域；只有中心点进入区域的人物才会计数和告警"
        )
        self.detection_region_button.toggled.connect(
            self.set_detection_region_enabled
        )

        self.compact_button = QPushButton("小窗口")
        self.compact_button.setToolTip("进入小窗口；拖动画面可移动，双击画面可恢复")
        self.compact_button.clicked.connect(self.enter_compact_mode)

        self.theme_button = QPushButton("黑色主题")
        self.theme_button.setToolTip("切换浅色或黑色界面")
        self.theme_button.setCheckable(True)
        self.theme_button.toggled.connect(self.set_dark_mode)

        self.start_button = QPushButton("开始检测")
        self.start_button.setObjectName("primaryButton")
        self.start_button.clicked.connect(self.start_detection)

        self.stop_button = QPushButton("停止")
        self.stop_button.setObjectName("dangerButton")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_stream)

        self.preview_label = PreviewLabel()
        self.preview_label.double_clicked.connect(self.restore_normal_mode)
        self.preview_label.detection_region_selected.connect(
            self.apply_detection_region
        )

        # 小窗口中央的悬浮操作按钮，默认隐藏。
        self.compact_controls = QFrame(self.preview_label)
        self.compact_controls.setObjectName("compactControls")
        compact_controls_layout = QHBoxLayout(self.compact_controls)
        compact_controls_layout.setContentsMargins(0, 0, 0, 0)
        compact_controls_layout.setSpacing(6)

        self.compact_start_button = QPushButton("开始检测")
        self.compact_start_button.setObjectName("primaryButton")
        self.compact_start_button.clicked.connect(self.start_detection)
        compact_controls_layout.addWidget(self.compact_start_button)

        self.compact_stop_button = QPushButton("停止")
        self.compact_stop_button.setObjectName("dangerButton")
        self.compact_stop_button.setEnabled(False)
        self.compact_stop_button.clicked.connect(self.stop_stream)
        compact_controls_layout.addWidget(self.compact_stop_button)
        self.compact_controls.hide()
        self.status_label = QLabel("等待操作")
        self.status_label.setObjectName("statusLabel")
        self.status_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        # 右侧状态只占文字所需宽度，其余空间优先留给左侧完整运行信息。
        self.status_label.setMinimumWidth(0)
        self.status_label.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Preferred,
        )
        self.running_indicator = QLabel()
        self.running_indicator.setObjectName("runningIndicator")
        self.running_indicator.setFixedSize(9, 9)
        self.running_indicator.setToolTip("检测正在运行")
        self.running_indicator.hide()
        self.lan_switch = PlatformCheckBox("局域网连接")
        self.lan_switch.setObjectName("lanSwitch")
        self.lan_switch.setToolTip("启动或停止局域网网页服务")
        self.lan_switch.toggled.connect(self.set_lan_server_enabled)
        self.rotation_button = QPushButton("画面旋转↻")
        self.rotation_button.setObjectName("rotationButton")
        self.rotation_button.setFixedWidth(90)
        self.rotation_button.setAccessibleName("顺时针旋转画面 90 度")
        self.rotation_button.clicked.connect(self.rotate_video_clockwise)
        self.update_rotation_button_tooltip()
        self.lan_devices_label = QLabel("在线设备：—")
        self.lan_devices_label.setObjectName("lanDevicesLabel")
        self.lan_devices_label.setToolTip("当前在线的局域网访问设备数量")
        self.lan_label = ClickableLabel(self._lan_info_text)
        self.lan_label.setObjectName("lanLabel")
        self.lan_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.lan_label.setMinimumWidth(0)
        self.lan_label.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        self.lan_label.clicked.connect(self.toggle_primary_info)
        self.lan_label.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.lan_label.customContextMenuRequested.connect(
            self.show_lan_context_menu
        )
        self.people_label = QLabel("人数：—")
        self.people_label.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Fixed,
        )
        self.people_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.fps_label = QLabel("FPS：—")
        self.fps_label.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Fixed,
        )
        self.fps_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )

        self._build_ui()
        self._apply_styles()
        self.setMinimumSize(460, 400)
        # 初次显示采用最小尺寸，宽度和高度之后都可以自由调整。
        self.resize(self.minimumSize())
        lan_enabled = self.restore_client_settings()
        # 恢复配置后再启动服务，避免先按默认值启动又立即关闭。
        self.lan_switch.setChecked(lan_enabled)
        self._lan_client_timer = QTimer(self)
        self._lan_client_timer.setInterval(1000)
        self._lan_client_timer.timeout.connect(self.update_online_lan_devices)
        self._lan_client_timer.start()
        # 窗口创建完成后自动扫描一次摄像头。
        self.refresh_cameras()
        self.camera_combo.currentIndexChanged.connect(
            self.update_resolution_list
        )

    def _build_ui(self) -> None:
        """创建顶部状态栏、中间视频区和底部控制栏。"""
        root = QWidget()
        root.setObjectName("rootWidget")
        self.root_layout = QVBoxLayout(root)
        self.root_layout.setContentsMargins(10, 10, 10, 10)
        self.root_layout.setSpacing(8)

        self.control_card = QFrame()
        self.control_card.setObjectName("controlCard")
        control_layout = QHBoxLayout(self.control_card)
        control_layout.setContentsMargins(8, 5, 8, 5)
        control_layout.setSpacing(6)

        # 五个操作按钮均匀铺满一栏，“开始检测”比其他按钮稍宽。
        operation_buttons = (
            self.theme_button,
            self.compact_button,
            self.detection_region_button,
            self.start_button,
            self.stop_button,
        )
        for button in operation_buttons:
            button.setSizePolicy(
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Fixed,
            )
        control_layout.addWidget(self.theme_button, 3)
        control_layout.addWidget(self.compact_button, 3)
        control_layout.addWidget(self.detection_region_button, 3)
        control_layout.addWidget(self.start_button, 4)
        control_layout.addWidget(self.stop_button, 3)

        self.status_card = QFrame()
        self.status_card.setObjectName("statusCard")
        self.status_layout = QGridLayout(self.status_card)
        self.status_layout.setContentsMargins(10, 6, 10, 6)
        self.status_layout.setHorizontalSpacing(6)
        self.status_layout.setVerticalSpacing(4)

        self.selector_row = QWidget()
        selector_layout = QHBoxLayout(self.selector_row)
        selector_layout.setContentsMargins(0, 0, 0, 0)
        selector_layout.setSpacing(0)
        selector_layout.addWidget(self.refresh_button)
        selector_layout.addSpacing(8)
        selector_layout.addWidget(self.camera_field_label)
        selector_layout.addSpacing(3)
        selector_layout.addWidget(self.camera_combo, 1)
        selector_layout.addSpacing(16)
        selector_layout.addWidget(self.resolution_field_label)
        selector_layout.addSpacing(3)
        selector_layout.addWidget(self.quality_combo, 1)
        selector_layout.addSpacing(8)
        selector_layout.addWidget(self.rotation_button)

        self.metrics_row = QWidget()
        metrics_layout = QHBoxLayout(self.metrics_row)
        metrics_layout.setContentsMargins(0, 0, 0, 0)
        metrics_layout.setSpacing(12)
        metrics_layout.addWidget(self.mirror_checkbox)
        metrics_layout.addWidget(self.lan_switch)
        metrics_layout.addStretch(1)
        metrics_layout.addWidget(self.lan_devices_label)
        metrics_layout.addWidget(self.people_label)
        metrics_layout.addWidget(self.fps_label)

        self._status_widgets = (
            self.selector_row,
            self.metrics_row,
        )
        self._status_two_rows: bool | None = None
        self._arrange_status_bar(two_rows=True)

        # 最底栏显示可切换的局域网/运行信息以及当前运行状态。
        self.lan_card = QFrame()
        self.lan_card.setObjectName("lanCard")
        lan_layout = QVBoxLayout(self.lan_card)
        lan_layout.setContentsMargins(10, 5, 10, 5)
        lan_layout.setSpacing(1)

        lan_primary_row = QWidget()
        lan_primary_layout = QHBoxLayout(lan_primary_row)
        lan_primary_layout.setContentsMargins(0, 0, 0, 0)
        lan_primary_layout.addWidget(self.lan_label, 1)
        lan_primary_layout.addWidget(self.status_label)
        lan_primary_layout.addSpacing(4)
        lan_primary_layout.addWidget(
            self.running_indicator,
            0,
            Qt.AlignmentFlag.AlignVCenter,
        )
        lan_layout.addWidget(lan_primary_row)

        # 显示栏放在画面上方，操作栏放在画面下方。
        self.root_layout.addWidget(self.status_card)
        self.root_layout.addWidget(self.preview_label, 1)
        self.root_layout.addWidget(self.control_card)
        self.root_layout.addWidget(self.lan_card)

        self.setCentralWidget(root)

    def _arrange_status_bar(self, two_rows: bool) -> None:
        """把选择控件固定在第一行，运行状态固定在第二行。"""
        two_rows = True
        if self._status_two_rows is two_rows:
            return

        for widget in self._status_widgets:
            self.status_layout.removeWidget(widget)
        self.status_layout.addWidget(self.selector_row, 0, 0)
        self.status_layout.addWidget(self.metrics_row, 1, 0)
        self.status_layout.setColumnStretch(0, 1)
        self._status_two_rows = two_rows
        self.status_card.updateGeometry()

    def _update_status_bar_layout(self) -> None:
        """保持顶部状态栏使用固定的两行信息层级。"""
        self._arrange_status_bar(two_rows=True)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        if hasattr(self, "status_layout"):
            self._update_status_bar_layout()

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        # Windows 会在窗口显示后最终确定字体，再测量一次避免数像素误差。
        self._sync_selector_widths()
        self._update_status_bar_layout()

    def _apply_styles(self) -> None:
        """根据 dark_mode 生成并应用整套 Qt 样式表。"""
        if self.dark_mode:
            colors = {
                "window": "#0c0e12",
                "text": "#e8eaf0",
                "card": "#171a21",
                "border": "#303640",
                "status": "#aab2bf",
                "input": "#20242c",
                "button": "#272c35",
                "button_hover": "#343a45",
                "disabled_text": "#626a78",
                "disabled_bg": "#1b1e24",
                "disabled_border": "#282d35",
                "menu_hover": "#26344d",
                "menu_hover_text": "#dbeafe",
            }
        else:
            colors = {
                "window": "#eef1f4",
                "text": "#20252d",
                "card": "#ffffff",
                "border": "#d5dae1",
                "status": "#4c5563",
                "input": "#ffffff",
                "button": "#e8edf3",
                "button_hover": "#dfe6ee",
                "disabled_text": "#9aa4b2",
                "disabled_bg": "#edf0f4",
                "disabled_border": "#e0e4e9",
                "menu_hover": "#eaf2ff",
                "menu_hover_text": "#1d4ed8",
            }

        # Windows 自定义复选框使用最清晰的黑白主题边框。
        for checkbox in (self.mirror_checkbox, self.lan_switch):
            checkbox.setProperty("darkTheme", self.dark_mode)
            checkbox.update()

        # 使用一份 QSS 配合颜色表，同时生成浅色和黑色两套主题。
        self.setStyleSheet(f"""
            QMainWindow, QWidget#rootWidget {{
                background: {colors["window"]};
            }}
            QDialog {{
                color: {colors["text"]};
                background: {colors["card"]};
            }}
            QWidget {{
                color: {colors["text"]};
                font-size: 14px;
            }}
            QLabel, QCheckBox {{
                background: transparent;
                border: none;
            }}
            QFrame#controlCard, QFrame#statusCard, QFrame#lanCard {{
                background: {colors["card"]};
                border: 1px solid {colors["border"]};
                border-radius: 6px;
            }}
            /* 底部操作栏使用更紧凑的字号。 */
            QFrame#controlCard QWidget {{
                font-size: 13px;
            }}
            QLabel#previewLabel {{
                background: {colors["window"]};
                color: {colors["status"]};
                border: none;
                border-radius: 0;
                font-size: 16px;
            }}
            QLabel#previewLabel[hasVideo="true"] {{
                background: #0b0d10;
                color: #8e96a3;
            }}
            QLabel#previewLabel[compact="true"] {{
                border: none;
                border-radius: 0;
            }}
            QFrame#compactControls {{
                background: transparent;
                border: none;
            }}
            QFrame#compactControls QPushButton {{
                min-height: 26px;
                padding: 0 8px;
                font-size: 13px;
            }}
            QLabel#statusLabel, QLabel#lanLabel {{
                color: {colors["status"]};
            }}
            QLabel#runningIndicator {{
                background: #22c55e;
                border: none;
                border-radius: 4px;
            }}
            QMenu {{
                color: {colors["text"]};
                background: {colors["card"]};
                border: 1px solid {colors["border"]};
                border-radius: 9px;
                padding: 6px;
            }}
            QMenu::item {{
                color: {colors["text"]};
                background: transparent;
                padding: 7px 12px;
                border-radius: 6px;
            }}
            QMenu::item:selected {{
                color: {colors["menu_hover_text"]};
                background: {colors["menu_hover"]};
            }}
            QComboBox {{
                combobox-popup: 0;
                min-height: 26px;
                padding: 0 6px;
                color: {colors["text"]};
                background: {colors["input"]};
                border: 1px solid {colors["border"]};
                border-radius: 5px;
            }}
            /* 隐藏下拉箭头；点击选择框本身仍可展开选项。 */
            QComboBox::drop-down {{
                width: 0;
                border: none;
            }}
            QComboBox::down-arrow {{
                image: none;
                width: 0;
                height: 0;
            }}
            QComboBox QAbstractItemView {{
                color: {colors["text"]};
                background: {colors["input"]};
                selection-color: white;
                selection-background-color: #2563eb;
            }}
            QSpinBox {{
                min-height: 28px;
                padding: 0 6px;
                color: {colors["text"]};
                background: {colors["input"]};
                border: 1px solid {colors["border"]};
                border-radius: 5px;
            }}
            QCheckBox {{
                spacing: 5px;
            }}
            QPushButton {{
                min-height: 26px;
                padding: 0 10px;
                color: {colors["text"]};
                background: {colors["button"]};
                border: 1px solid {colors["border"]};
                border-radius: 5px;
            }}
            QPushButton:hover {{
                background: {colors["button_hover"]};
            }}
            QPushButton:checked {{
                color: white;
                background: #4b5563;
            }}
            QPushButton#regionButton:checked {{
                color: white;
                background: #2563eb;
                border-color: #2563eb;
            }}
            QPushButton#regionButton:checked:hover {{
                background: #1d4ed8;
                border-color: #1d4ed8;
            }}
            QPushButton#primaryButton {{
                color: white;
                background: #16a34a;
                border-color: #16a34a;
            }}
            QPushButton#primaryButton:hover {{
                background: #15803d;
                border-color: #15803d;
            }}
            QPushButton#dangerButton {{
                color: white;
                background: #dc3545;
                border-color: #dc3545;
            }}
            QPushButton#dangerButton:hover {{
                background: #bd2534;
            }}
            QPushButton:disabled {{
                color: {colors["disabled_text"]};
                background: {colors["disabled_bg"]};
                border-color: {colors["disabled_border"]};
            }}
            QPushButton#primaryButton:disabled {{
                color: {colors["disabled_text"]};
                background: {colors["disabled_bg"]};
                border-color: {colors["disabled_border"]};
            }}
            QPushButton#dangerButton:disabled {{
                color: {colors["disabled_text"]};
                background: {colors["disabled_bg"]};
                border-color: {colors["disabled_border"]};
            }}
            """)

        # QSS 字体生效后重新测量，避免 Windows 字体比初始化阶段更宽。
        self._sync_selector_widths()
        self._stabilize_status_widths()
        self._update_status_bar_layout()

    def _sync_selector_widths(self) -> None:
        """忽略不同内容长度，让摄像头与分辨率选择框保持等宽。"""
        for combo in (self.camera_combo, self.quality_combo):
            combo.setMinimumWidth(40)
            combo.setSizePolicy(
                QSizePolicy.Policy.Ignored,
                QSizePolicy.Policy.Fixed,
            )

    def _stabilize_status_widths(self) -> None:
        """为 FPS 预留稳定宽度，数值刷新时不推动前方状态文字。"""
        fps_width = self.fps_label.fontMetrics().horizontalAdvance(
            "FPS：999.9"
        ) + 4
        self.fps_label.setFixedWidth(fps_width)

    @staticmethod
    def _setting_as_bool(value: Any, default: bool) -> bool:
        """兼容 QSettings 在不同平台返回的 bool、数字或字符串。"""
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        normalized = str(value).strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        return default

    def restore_client_settings(self) -> bool:
        """恢复上次关闭时的桌面配置，并返回局域网开关状态。"""
        camera_index = self.settings.value("camera_index")
        try:
            self._preferred_camera_index = (
                int(camera_index) if camera_index is not None else None
            )
        except (TypeError, ValueError):
            self._preferred_camera_index = None

        resolution_width = self.settings.value("resolution_width")
        resolution_height = self.settings.value("resolution_height")
        try:
            if resolution_width is not None and resolution_height is not None:
                self._preferred_resolution = (
                    int(resolution_width),
                    int(resolution_height),
                )
        except (TypeError, ValueError):
            self._preferred_resolution = None

        # 兼容旧版本按固定三档保存的索引。
        if self._preferred_resolution is None:
            old_resolutions = ((640, 360), (1280, 720), (1920, 1080))
            try:
                old_quality_index = int(
                    self.settings.value("quality_index", 2)
                )
                if 0 <= old_quality_index < len(old_resolutions):
                    self._preferred_resolution = old_resolutions[old_quality_index]
            except (TypeError, ValueError):
                pass

        try:
            saved_port = int(self.settings.value("lan_port", WEB_PORT))
            if 1024 <= saved_port <= 65535:
                self._lan_port = saved_port
        except (TypeError, ValueError):
            self._lan_port = WEB_PORT

        mirror_enabled = self._setting_as_bool(
            self.settings.value("mirror_enabled"),
            True,
        )
        self.mirror_checkbox.setChecked(mirror_enabled)

        try:
            saved_rotation = int(
                self.settings.value("rotation_degrees", 0)
            )
            self.rotation_degrees = (
                saved_rotation
                if saved_rotation in (0, 90, 180, 270)
                else 0
            )
        except (TypeError, ValueError):
            self.rotation_degrees = 0
        self.update_rotation_button_tooltip()

        region: DetectionRegion | None = None
        raw_region = self.settings.value("detection_region", "")
        if raw_region:
            try:
                parsed_region = json.loads(str(raw_region))
                if isinstance(parsed_region, list) and len(parsed_region) == 4:
                    region = normalize_detection_region(
                        tuple(float(value) for value in parsed_region)
                    )
            except (TypeError, ValueError, json.JSONDecodeError):
                region = None
        self.detection_region = region
        self.preview_label.set_detection_region(region)

        dark_enabled = self._setting_as_bool(
            self.settings.value("dark_mode"),
            False,
        )
        self.theme_button.setChecked(dark_enabled)

        region_enabled = self._setting_as_bool(
            self.settings.value("detection_region_enabled"),
            False,
        )
        self.detection_region_button.setChecked(region_enabled)

        raw_window_size = self.settings.value("window_size", "")
        if raw_window_size:
            try:
                saved_width, saved_height = json.loads(
                    str(raw_window_size)
                )
                width = max(int(saved_width), self.minimumWidth())
                height = max(int(saved_height), self.minimumHeight())
                screen = QApplication.primaryScreen()
                if screen is not None:
                    available = screen.availableGeometry()
                    width = min(width, available.width())
                    height = min(height, available.height())
                self.resize(width, height)
            except (
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ):
                pass

        return self._setting_as_bool(
            self.settings.value("lan_enabled"),
            True,
        )

    def save_client_settings(self) -> None:
        """保存下次启动需要恢复的桌面配置，但不保存检测运行状态。"""
        camera_index = self.camera_combo.currentData()
        if camera_index is not None:
            self.settings.setValue("camera_index", int(camera_index))
        self.settings.setValue(
            "lan_port",
            self._lan_port,
        )
        resolution = self.quality_combo.currentData()
        if resolution is not None:
            self.settings.setValue("resolution_width", int(resolution[0]))
            self.settings.setValue("resolution_height", int(resolution[1]))
        self.settings.setValue(
            "mirror_enabled",
            self.mirror_checkbox.isChecked(),
        )
        self.settings.setValue(
            "rotation_degrees",
            self.rotation_degrees,
        )
        self.settings.setValue("dark_mode", self.dark_mode)
        self.settings.setValue(
            "lan_enabled",
            self.lan_switch.isChecked(),
        )
        self.settings.setValue(
            "detection_region_enabled",
            self.detection_region_button.isChecked(),
        )
        self.settings.setValue(
            "detection_region",
            (
                json.dumps(self.detection_region)
                if self.detection_region is not None
                else ""
            ),
        )

        normal_geometry = (
            self._normal_geometry
            if self.compact_mode and self._normal_geometry is not None
            else self.geometry()
        )
        self.settings.setValue(
            "window_size",
            json.dumps(
                [normal_geometry.width(), normal_geometry.height()]
            ),
        )
        self.settings.sync()

    def _sync_compact_buttons(self) -> None:
        """让小窗口按钮与普通操作栏中的按钮状态保持一致。"""
        self.compact_start_button.setEnabled(self.start_button.isEnabled())
        self.compact_stop_button.setEnabled(self.stop_button.isEnabled())

    def position_compact_controls(self) -> None:
        """把悬浮操作条放到视频区域正中央。"""
        if not hasattr(self, "compact_controls"):
            return

        self.compact_controls.adjustSize()
        x = max((self.preview_label.width() - self.compact_controls.width()) // 2, 0)
        y = max((self.preview_label.height() - self.compact_controls.height()) // 2, 0)
        self.compact_controls.move(x, y)

    def update_compact_controls_visibility(self, position: QPoint) -> None:
        """鼠标进入小窗口中央区域时显示悬浮按钮。"""
        if not self.compact_mode:
            self.hide_compact_controls()
            return

        width = self.preview_label.width()
        height = self.preview_label.height()
        inside_center = (
            width // 5 <= position.x() <= width * 4 // 5
            and height // 4 <= position.y() <= height * 3 // 4
        )
        if inside_center:
            self._sync_compact_buttons()
            self.position_compact_controls()
            self.compact_controls.show()
            self.compact_controls.raise_()
        else:
            self.hide_compact_controls()

    def hide_compact_controls(self) -> None:
        """隐藏小窗口悬浮按钮。"""
        if hasattr(self, "compact_controls"):
            self.compact_controls.hide()

    def set_lan_server_enabled(self, enabled: bool) -> None:
        """响应局域网开关，不改变本地摄像头检测状态。"""
        self._pending_lan_restart = False
        if enabled:
            self.start_lan_server()
        else:
            self.stop_lan_server()

    def configure_lan_port(self) -> None:
        """从局域网信息右键菜单配置端口，不改变启用状态。"""
        dialog = QInputDialog(self)
        dialog.setWindowTitle("配置局域网端口")
        dialog.setLabelText("端口号（1024–65535）：")
        dialog.setInputMode(QInputDialog.InputMode.IntInput)
        dialog.setIntRange(1024, 65535)
        dialog.setIntValue(self._lan_port)
        dialog.setIntStep(1)
        # 显式沿用当前主题，避免 macOS/Windows 的默认对话框颜色割裂。
        dialog.setStyleSheet(self.styleSheet())
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        port = dialog.intValue()
        if not accepted or port == self._lan_port:
            return

        self._lan_port = port
        self.settings.setValue("lan_port", port)
        self.settings.sync()
        if not self.lan_switch.isChecked():
            self.set_lan_info(f"局域网：已关闭（端口 {port}）")
            return

        server = self.web_server
        if server is not None:
            self._pending_lan_restart = True
            self.set_lan_info(f"局域网：正在切换到端口 {port}…")
            self.stop_lan_server(update_status=False)
        else:
            self.start_lan_server()

    def update_online_lan_devices(self) -> None:
        """定时在桌面客户端显示仍在线的网页设备数量。"""
        count = (
            self.lan_state.online_client_count()
            if self.lan_switch.isChecked()
            else 0
        )
        displayed_count = str(count) if count > 0 else "—"
        self.lan_devices_label.setText(f"在线设备：{displayed_count}")

    def set_lan_info(self, text: str, tooltip: str = "") -> None:
        """保存局域网状态；当前显示局域网信息时同步刷新标签。"""
        self._lan_info_text = text
        self._lan_info_tooltip = tooltip
        if not self._runtime_info_text or not self._show_runtime_info:
            self.refresh_primary_info()

    def refresh_primary_info(self) -> None:
        """在第一行左侧显示运行信息或局域网信息。"""
        runtime_visible = bool(
            self._runtime_info_text and self._show_runtime_info
        )
        if runtime_visible:
            text = self._runtime_info_text
            tooltip = "点击切换至局域网信息"
        else:
            text = self._lan_info_text
            tooltip = self._lan_info_tooltip
            if self._runtime_info_text:
                switch_tip = "点击切换至分辨率和推理设备信息"
                tooltip = f"{tooltip}\n{switch_tip}" if tooltip else switch_tip

        self.lan_label.setText(text)
        self.lan_label.setToolTip(tooltip)
        if self._runtime_info_text:
            self.lan_label.setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.lan_label.unsetCursor()

    def toggle_primary_info(self) -> None:
        """检测运行时切换第一行左侧的两类信息。"""
        if not self._runtime_info_text:
            return
        self._show_runtime_info = not self._show_runtime_info
        self.refresh_primary_info()

    def current_lan_url(self) -> str:
        """从当前局域网状态中提取可由浏览器访问的 HTTP 地址。"""
        candidate = self._lan_info_text.partition("：")[2].strip()
        parsed = urlparse(candidate)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return candidate
        return ""

    def copy_lan_info(self) -> None:
        """只把当前局域网访问网址复制到系统剪贴板。"""
        url = self.current_lan_url()
        if url:
            QApplication.clipboard().setText(url)

    def open_lan_in_browser(self) -> None:
        """使用系统默认浏览器打开当前局域网访问地址。"""
        url = self.current_lan_url()
        if url and not QDesktopServices.openUrl(QUrl(url)):
            QMessageBox.warning(self, "打开失败", "无法调用默认浏览器。")

    def show_lan_context_menu(self, position: QPoint) -> None:
        """提供网址操作和局域网端口配置。"""
        url = self.current_lan_url()
        menu = QMenu(self)
        menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        if url:
            copy_action = menu.addAction("复制网址")
            open_action = menu.addAction("默认浏览器打开")
            copy_action.triggered.connect(self.copy_lan_info)
            open_action.triggered.connect(self.open_lan_in_browser)
            menu.addSeparator()
        port_action = menu.addAction(f"配置端口（当前 {self._lan_port}）")
        port_action.triggered.connect(self.configure_lan_port)
        menu.exec(self.lan_label.mapToGlobal(position))

    def start_lan_server(self) -> None:
        """启动只读局域网前端服务；它不打开摄像头也不执行模型推理。"""
        if self.web_server is not None:
            return

        self.set_lan_info("局域网：正在启动…")
        server = LanWebServer(
            self.lan_state,
            port=self._lan_port,
            parent=self,
        )
        server.server_started.connect(self.show_lan_url)
        server.server_failed.connect(self.show_lan_error)
        server.finished.connect(lambda: self.lan_server_finished(server))
        server.finished.connect(server.deleteLater)
        self.web_server = server
        server.start()

    def stop_lan_server(self, update_status: bool = True) -> None:
        """停止网页服务；摄像头识别线程继续按原状态运行。"""
        if update_status:
            self.set_lan_info("局域网：已关闭")
        self.lan_state.clear_online_clients()
        self.update_online_lan_devices()
        server = self.web_server
        if server is None:
            return

        # 停止期间暂时锁定开关，避免同一 QThread 尚未结束就重复启动。
        self.lan_switch.setEnabled(False)
        server.stop()

    def show_lan_url(self, url: str) -> None:
        """在本地状态栏显示其他局域网设备应该访问的地址。"""
        if not self.lan_switch.isChecked():
            return
        self.set_lan_info(
            f"局域网：{url}",
            f"同一局域网设备访问 {url}",
        )

    def show_lan_error(self, message: str) -> None:
        """网页服务失败不应中断本地摄像头检测。"""
        self._pending_lan_restart = False
        self.lan_state.clear_online_clients()
        self.update_online_lan_devices()
        self.set_lan_info("局域网：启动失败", message)
        self.lan_switch.blockSignals(True)
        self.lan_switch.setChecked(False)
        self.lan_switch.blockSignals(False)

    def lan_server_finished(self, finished_server: LanWebServer) -> None:
        """只清理当前网页服务的引用，避免旧线程覆盖新状态。"""
        if self.web_server is not finished_server:
            return

        self.web_server = None
        self.lan_state.clear_online_clients()
        self.update_online_lan_devices()
        self.lan_switch.setEnabled(True)
        if self._pending_lan_restart and self.lan_switch.isChecked():
            self._pending_lan_restart = False
            self.start_lan_server()
            return
        if self.lan_switch.isChecked():
            # 服务意外退出时让开关状态与实际状态重新一致。
            self.lan_switch.blockSignals(True)
            self.lan_switch.setChecked(False)
            self.lan_switch.blockSignals(False)
            self.set_lan_info("局域网：服务已停止")
        elif self._lan_info_text != "局域网：启动失败":
            self.set_lan_info("局域网：已关闭")

    def record_intrusion(
        self,
        source_worker: CameraWorker,
        people_count: int,
        jpeg: bytes,
    ) -> None:
        """把后台线程确认的入侵事件发布给局域网网页。"""
        if self.worker is source_worker and source_worker.isRunning():
            self.lan_state.record_intrusion(people_count, jpeg)

    def set_detection_region_enabled(self, enabled: bool) -> None:
        """切换识别框选；关闭后立即恢复全画面人数和告警判断。"""
        self.preview_label.set_detection_region_enabled(
            enabled and not self.compact_mode
        )
        worker = self.worker
        if worker is not None and worker.isRunning():
            worker.set_detection_region(enabled, self.detection_region)
            self.people_label.setText("人数：0")
        self.lan_state.reset_presence()

        if enabled:
            if self.detection_region is None:
                status = "识别框选已开启 · 请在画面上拖拽范围"
            else:
                status = "识别框选已开启 · 只统计框内人物"
        else:
            status = (
                "已恢复全画面识别"
                if worker is not None and worker.isRunning()
                else "识别框选已关闭"
            )
        self.status_label.setText(status)
        self.lan_state.set_status(status)

    def set_mirror_enabled(self, enabled: bool) -> None:
        """让正在运行的检测线程立即采用新的镜像状态。"""
        worker = self.worker
        if worker is None or not worker.isRunning():
            return

        worker.set_mirror(enabled)
        self.people_label.setText("人数：0")
        self.lan_state.reset_presence()

    def update_rotation_button_tooltip(self) -> None:
        """显示当前画面方向以及按钮的下一步操作。"""
        self.rotation_button.setToolTip(
            "顺时针旋转画面 90°"
            f"（当前相对原始画面旋转 {self.rotation_degrees}°）"
        )

    def rotate_video_clockwise(self) -> None:
        """每次点击都把桌面和局域网画面顺时针旋转 90 度。"""
        self.rotation_degrees = (self.rotation_degrees + 90) % 360
        self.update_rotation_button_tooltip()

        worker = self.worker
        if worker is not None and worker.isRunning():
            worker.set_rotation_degrees(self.rotation_degrees)
            self.people_label.setText("人数：0")
        self.lan_state.reset_presence()

        status = (
            "画面已恢复原始方向"
            if self.rotation_degrees == 0
            else f"画面已顺时针旋转至 {self.rotation_degrees}°"
        )
        self.status_label.setText(status)
        self.lan_state.set_status(status)

    def apply_detection_region(self, region: DetectionRegion) -> None:
        """保存用户拖拽的区域，并让检测线程从下一帧开始使用。"""
        normalized_region = normalize_detection_region(region)
        if normalized_region is None:
            return

        self.detection_region = normalized_region
        self.preview_label.set_detection_region(normalized_region)
        worker = self.worker
        if worker is not None and worker.isRunning():
            worker.set_detection_region(
                self.detection_region_button.isChecked(),
                normalized_region,
            )
            self.people_label.setText("人数：0")
        self.lan_state.reset_presence()
        status = "识别框选已更新 · 只统计框内人物"
        self.status_label.setText(status)
        self.lan_state.set_status(status)

    def set_dark_mode(self, enabled: bool) -> None:
        """在浅色和黑色主题之间切换。"""
        self.dark_mode = enabled
        self.theme_button.setText("浅色主题" if enabled else "黑色主题")
        self._apply_styles()

    def enter_compact_mode(self) -> None:
        """隐藏所有控件，只保留可拖动、可双击恢复的视频小窗。"""
        if self.compact_mode:
            return

        # 直接保存窗口矩形，避免 restoreGeometry 在小屏幕上自动压缩尺寸。
        self._normal_geometry = self.geometry()
        self._normal_minimum_size = self.minimumSize()
        self._was_maximized = self.isMaximized()
        self.compact_mode = True
        self._sync_compact_buttons()
        self.hide_compact_controls()
        # 小窗口继续显示检测线程画出的区域，但不接管鼠标拖拽。
        self.preview_label.set_detection_region_enabled(False)

        self.control_card.hide()
        self.status_card.hide()
        self.lan_card.hide()
        self.root_layout.setContentsMargins(0, 0, 0, 0)
        self.root_layout.setSpacing(0)
        self.preview_label.setProperty("compact", True)
        self.preview_label.style().unpolish(self.preview_label)
        self.preview_label.style().polish(self.preview_label)

        # 无边框并置顶，使小窗口只显示摄像头画面和检测框。
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setMinimumSize(160, 90)
        self.showNormal()
        self.resize(480, 270)

        # 默认把小窗口放到当前屏幕右下角。
        available = self.screen().availableGeometry()
        self.move(
            available.right() - self.width() - 20,
            available.bottom() - self.height() - 20,
        )

    def restore_normal_mode(self) -> None:
        """双击小窗口后恢复原始控件、尺寸和窗口状态。"""
        if not self.compact_mode:
            return

        # 记录界面模式，恢复普通窗口时需要使用这些状态。
        self.compact_mode = False
        self.hide_compact_controls()
        self.control_card.show()
        self.status_card.show()
        self.lan_card.show()
        self.root_layout.setContentsMargins(10, 10, 10, 10)
        self.root_layout.setSpacing(8)
        self.preview_label.setProperty("compact", False)
        self.preview_label.set_detection_region_enabled(
            self.detection_region_button.isChecked()
        )
        self.preview_label.style().unpolish(self.preview_label)
        self.preview_label.style().polish(self.preview_label)

        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, False)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, False)
        # 小窗口使用独立限制，退出后原样恢复主窗口此前的最小尺寸。
        if self._normal_minimum_size is not None:
            self.setMinimumSize(self._normal_minimum_size)

        if self._was_maximized:
            self.showMaximized()
        else:
            self.showNormal()
            if self._normal_geometry is not None:
                self.setGeometry(self._normal_geometry)

    def refresh_cameras(self) -> None:
        """启动后台扫描；扫描期间禁用相关按钮，防止重复操作。"""
        if self.scan_worker is not None and self.scan_worker.isRunning():
            return

        self.refresh_button.setEnabled(False)
        self.camera_combo.setEnabled(False)
        self.start_button.setEnabled(False)
        self._sync_compact_buttons()
        self.status_label.setText("正在扫描摄像头…")

        scan_worker = CameraScanWorker(parent=self)
        scan_worker.cameras_found.connect(self.apply_camera_list)
        scan_worker.finished.connect(lambda: self.scan_finished(scan_worker))
        scan_worker.finished.connect(scan_worker.deleteLater)
        self.scan_worker = scan_worker
        scan_worker.start()

    def apply_camera_list(
        self,
        camera_profiles: list[dict[str, Any]],
    ) -> None:
        """显示摄像头名称，并为当前设备加载已验证的分辨率。"""
        selected_index = self.camera_combo.currentData()
        if selected_index is None:
            selected_index = self._preferred_camera_index
        self._camera_profiles = {
            int(profile["index"]): profile for profile in camera_profiles
        }
        self.camera_combo.blockSignals(True)
        self.camera_combo.clear()

        for profile in camera_profiles:
            self.camera_combo.addItem(
                str(profile["name"]),
                int(profile["index"]),
            )

        if not camera_profiles:
            self.camera_combo.addItem("未检测到可用摄像头", None)
            self.status_label.setText("未探测到摄像头，请检查权限或设备占用")
        else:
            self.status_label.setText(f"发现 {len(camera_profiles)} 个摄像头")

        if selected_index is not None:
            restored = self.camera_combo.findData(selected_index)
            if restored >= 0:
                self.camera_combo.setCurrentIndex(restored)
        self.camera_combo.blockSignals(False)
        self.update_resolution_list()

        self._sync_selector_widths()
        self._update_status_bar_layout()

    def update_resolution_list(self, _index: int = -1) -> None:
        """只列出当前摄像头已经实际请求并成功取帧的分辨率。"""
        camera_index = self.camera_combo.currentData()
        profile = (
            self._camera_profiles.get(int(camera_index))
            if camera_index is not None
            else None
        )
        resolutions = list(profile.get("resolutions", [])) if profile else []

        current_resolution = self.quality_combo.currentData()
        self.quality_combo.blockSignals(True)
        self.quality_combo.clear()
        for width, height in resolutions:
            self.quality_combo.addItem(
                f"{width}×{height}",
                (int(width), int(height)),
            )

        if resolutions:
            # 首次取得列表时默认选择 1280×720；切换设备时优先保留
            # 当前选择。不可用时再尝试历史设置和最高可用模式。
            selected = -1
            for preferred in (
                current_resolution,
                DEFAULT_CAMERA_RESOLUTION,
                self._preferred_resolution,
            ):
                if preferred is None:
                    continue
                selected = next(
                    (
                        index
                        for index in range(self.quality_combo.count())
                        if self.quality_combo.itemData(index) == preferred
                    ),
                    -1,
                )
                if selected >= 0:
                    break
            self.quality_combo.setCurrentIndex(
                selected if selected >= 0 else len(resolutions) - 1
            )
            self.quality_combo.setEnabled(self.worker is None)
        else:
            self.quality_combo.addItem("无可用分辨率", None)
            self.quality_combo.setEnabled(False)
        self.quality_combo.blockSignals(False)
        self._sync_selector_widths()
        self._update_status_bar_layout()

    def scan_finished(self, finished_worker: CameraScanWorker) -> None:
        """扫描结束后恢复控件，并忽略已经过期的扫描线程信号。"""
        if self.scan_worker is not finished_worker:
            return

        self.scan_worker = None
        if self.worker is None:
            camera_available = self.camera_combo.currentData() is not None
            self.camera_combo.setEnabled(camera_available)
            self.refresh_button.setEnabled(True)
            self.start_button.setEnabled(
                camera_available
                and self.quality_combo.currentData() is not None
            )
            self._sync_compact_buttons()

    def start_detection(self) -> None:
        """响应“开始检测”按钮，创建并启动检测线程。"""
        self._start_stream()

    def _start_stream(self) -> None:
        """读取当前界面配置，锁定控件并启动摄像头检测。"""
        if self.worker is not None and self.worker.isRunning():
            return

        camera_index = self.camera_combo.currentData()
        if camera_index is None:
            QMessageBox.warning(self, "未选择摄像头", "请先选择一个摄像头。")
            return

        frame_size = self.quality_combo.currentData()
        if frame_size is None:
            QMessageBox.warning(
                self,
                "无可用分辨率",
                "当前摄像头没有经过验证的可用分辨率，请刷新摄像头列表。",
            )
            return
        frame_width, frame_height = frame_size

        worker = CameraWorker(
            camera_index=int(camera_index),
            camera_name=self.camera_combo.currentText(),
            mirror=self.mirror_checkbox.isChecked(),
            frame_width=int(frame_width),
            frame_height=int(frame_height),
            rotation_degrees=self.rotation_degrees,
            detection_region_enabled=(
                self.detection_region_button.isChecked()
            ),
            detection_region=self.detection_region,
            lan_state=self.lan_state,
            parent=self,
        )
        # lambda 捕获 worker，用于识别信号是否来自当前活动线程。
        # 这样点击停止后，消息队列中残留的旧帧不会重新显示。
        worker.frame_ready.connect(
            lambda image: self.display_frame(worker, image)
        )
        worker.stats_changed.connect(
            lambda people, fps: self.update_stats(worker, people, fps)
        )
        worker.intrusion_detected.connect(
            lambda people, jpeg: self.record_intrusion(
                worker,
                people,
                jpeg,
            )
        )
        worker.status_changed.connect(
            lambda text: self.update_worker_status(worker, text)
        )
        worker.runtime_info_changed.connect(
            lambda text: self.update_runtime_info(worker, text)
        )
        worker.error_occurred.connect(self.show_worker_error)
        worker.finished.connect(lambda: self.worker_finished(worker))
        worker.finished.connect(worker.deleteLater)
        self.worker = worker

        self.camera_combo.setEnabled(False)
        self.refresh_button.setEnabled(False)
        self.quality_combo.setEnabled(False)
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self._sync_compact_buttons()
        self.people_label.setText("人数：0")
        self.fps_label.setText("FPS：0.0")
        self.hide_runtime_info()
        self.lan_state.set_running(True, "正在启动人体检测")
        # 所有摄像头读取和模型推理都在 QThread 中进行，主界面不会被阻塞。
        worker.start()

    def stop_stream(self) -> None:
        """响应“停止”按钮，安全结束检测。"""
        self._stop_worker()

    def _stop_worker(self) -> bool:
        """停止当前检测线程并清空画面；成功停止时返回 True。"""
        worker = self.worker
        if worker is not None and worker.isRunning():
            self.status_label.setText("正在停止摄像头…")
            self.hide_runtime_info()
            worker.stop()
            # 最多等待 4 秒，让线程结束并执行 camera.release()。
            if not worker.wait(4000):
                QMessageBox.warning(
                    self,
                    "停止超时",
                    "摄像头线程仍在结束中，请稍后再试。",
                )
                return False

        self.worker = None
        self._set_idle_controls()
        # 清空保存的 QPixmap，确保停止后不保留最后一帧。
        self.preview_label.show_placeholder()
        self.status_label.setText("已停止")
        self.hide_runtime_info()
        self.people_label.setText("人数：—")
        self.fps_label.setText("FPS：—")
        self.lan_state.set_running(False, "本地检测已停止")

        return True

    def _set_idle_controls(self) -> None:
        """把按钮和选择框恢复到未检测时的可用状态。"""
        camera_available = self.camera_combo.currentData() is not None
        resolution_available = self.quality_combo.currentData() is not None
        self.camera_combo.setEnabled(camera_available)
        self.refresh_button.setEnabled(True)
        self.quality_combo.setEnabled(camera_available and resolution_available)
        self.mirror_checkbox.setEnabled(True)
        self.start_button.setEnabled(camera_available and resolution_available)
        self.stop_button.setEnabled(False)
        self._sync_compact_buttons()

    def display_frame(self, source_worker: CameraWorker, image: QImage) -> None:
        """只显示当前活动线程的帧，停止后的排队帧会被丢弃。"""
        if self.worker is source_worker and source_worker.isRunning():
            self.preview_label.set_video_image(image)

    def update_stats(
        self,
        source_worker: CameraWorker,
        people_count: int,
        fps: float,
    ) -> None:
        if self.worker is not source_worker or not source_worker.isRunning():
            return

        self.people_label.setText(f"人数：{people_count}")
        self.fps_label.setText(f"FPS：{fps:.1f}")
        self.lan_state.update_stats(people_count, fps)

    def update_worker_status(
        self,
        source_worker: CameraWorker,
        text: str,
    ) -> None:
        if self.worker is source_worker and source_worker.isRunning():
            displayed_text = text
            if (
                "人体检测运行中" in text
                and self.detection_region_button.isChecked()
                and self.detection_region is None
            ):
                displayed_text = f"{text} · 请在画面上拖拽框选范围"
            self.status_label.setText(displayed_text)
            self.lan_state.set_status(displayed_text)

    def update_runtime_info(
        self,
        source_worker: CameraWorker,
        text: str,
    ) -> None:
        """检测真正开始后，在第一行左侧优先显示完整运行信息。"""
        if self.worker is not source_worker or not source_worker.isRunning():
            return

        self.status_label.setText("运行中")
        self.status_label.setFixedWidth(self.status_label.sizeHint().width())
        self.running_indicator.show()
        self._runtime_info_text = text
        self._show_runtime_info = True
        self.refresh_primary_info()
        self.lan_state.set_status(text)

    def hide_runtime_info(self) -> None:
        """未检测或正在停止时恢复显示第一行左侧的局域网信息。"""
        self._runtime_info_text = ""
        self._show_runtime_info = False
        self.refresh_primary_info()
        self.status_label.setMinimumWidth(0)
        self.status_label.setMaximumWidth(16777215)
        self.running_indicator.hide()

    def show_worker_error(self, message: str) -> None:
        """在主线程中显示后台检测产生的错误。"""
        self.hide_runtime_info()
        self.lan_state.set_status(f"运行错误：{message}")
        QMessageBox.critical(self, "运行错误", message)

    def worker_finished(self, finished_worker: CameraWorker) -> None:
        """检测线程结束后恢复界面，并丢弃旧线程的结束信号。"""
        if self.worker is not finished_worker:
            return

        self.worker = None
        self._set_idle_controls()
        self.status_label.setText("摄像头已停止")
        self.hide_runtime_info()
        self.preview_label.show_placeholder()
        self.people_label.setText("人数：—")
        self.fps_label.setText("FPS：—")
        self.lan_state.set_running(False, "本地检测已停止")

    def closeEvent(self, event: QCloseEvent) -> None:
        """关闭窗口前等待后台线程退出并释放摄像头。"""
        # 停止线程前保存界面选项；运行/停止状态本身不会被记忆。
        self.save_client_settings()

        # 关闭窗口前先停止两个后台线程，避免出现 QThread 销毁警告。
        scan_worker = self.scan_worker
        if scan_worker is not None and scan_worker.isRunning():
            scan_worker.stop()
            scan_worker.wait(4000)

        worker = self.worker
        if worker is not None and worker.isRunning():
            worker.stop()
            worker.wait(4000)

        self.lan_state.set_running(False, "本地程序已关闭")
        self.lan_state.clear_online_clients()
        web_server = self.web_server
        if web_server is not None and web_server.isRunning():
            web_server.stop()
            web_server.wait(3000)
        event.accept()


# ---------- 程序入口 ----------
def main() -> int:
    """创建 Qt 应用和主窗口，然后进入事件循环。"""
    app = QApplication(sys.argv)
    app.setApplicationName("人体检测与跟踪")
    if ICON_PATH.is_file():
        app.setWindowIcon(QIcon(str(ICON_PATH)))
    window = CameraWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
