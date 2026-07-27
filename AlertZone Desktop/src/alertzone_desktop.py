"""AlertZone Client 的局域网桌面前端。

主窗口嵌入 AlertZone Client 提供的网页；独立的状态轮询器负责在主窗口隐藏时
继续接收报警事件，并通过置顶小窗口和系统声音通知用户。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from PySide6.QtCore import (
    QByteArray,
    QObject,
    QSettings,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import QAction, QCloseEvent, QIcon, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtNetwork import (
    QNetworkAccessManager,
    QNetworkReply,
    QNetworkRequest,
)
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QSystemTrayIcon,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

APP_NAME = "AlertZone Desktop"
ORGANIZATION_NAME = "AlertZone"
DEFAULT_PORT = 8765
POLL_INTERVAL_MS = 600
REQUEST_TIMEOUT_MS = 3500

APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent
ROOT_DIR = PROJECT_DIR.parent
ICON_CANDIDATES = (
    PROJECT_DIR / "icon.png",
    ROOT_DIR / "AlertZone Client" / "icon" / "icon.png",
    ROOT_DIR / "icon" / "icon.png",
    APP_DIR / "icon.png",
)


def app_icon_path() -> Path | None:
    """返回源码环境或打包环境中的程序图标。"""
    return next((path for path in ICON_CANDIDATES if path.is_file()), None)


def setting_bool(settings: QSettings, key: str, default: bool) -> bool:
    """可靠读取不同平台 QSettings 返回的布尔值。"""
    value = settings.value(key, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def normalize_server_url(raw_value: str) -> str:
    """规范化局域网地址，并在未填写端口时使用 8765。"""
    value = raw_value.strip()
    if not value:
        raise ValueError("请输入 AlertZone Client 的局域网地址")
    if "://" not in value:
        value = f"http://{value}"

    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("地址只支持 http:// 或 https://")
    if not parsed.hostname:
        raise ValueError("局域网地址格式不正确")
    if parsed.username or parsed.password:
        raise ValueError("地址中不能包含用户名或密码")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("端口号格式不正确") from error

    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host}:{port or DEFAULT_PORT}"
    return urlunsplit((parsed.scheme.lower(), netloc, "", "", ""))


class ConnectionPage(QWidget):
    """首次启动时显示的局域网地址输入页。"""

    connect_requested = Signal(str)

    def __init__(self, settings: QSettings) -> None:
        super().__init__()
        self._settings = settings
        self.setObjectName("connectionPage")

        card = QFrame()
        card.setObjectName("connectionCard")
        card.setMinimumWidth(520)
        card.setMaximumWidth(620)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(36, 34, 36, 34)
        card_layout.setSpacing(14)

        title = QLabel("连接 AlertZone Client")
        title.setObjectName("connectionTitle")
        subtitle = QLabel(
            "输入 AlertZone Client 主界面左下角显示的局域网地址。"
            "\n本机需要与 Client 处于同一局域网。"
        )
        subtitle.setObjectName("connectionSubtitle")
        subtitle.setWordWrap(True)
        subtitle.setMinimumHeight(48)

        self.address_edit = QLineEdit()
        self.address_edit.setPlaceholderText("例如：http://192.168.1.20:8765")
        self.address_edit.setText(str(settings.value("connection/server_url", "")))
        self.address_edit.returnPressed.connect(self._submit)

        self.connect_button = QPushButton("连接")
        self.connect_button.setObjectName("primaryButton")
        self.connect_button.clicked.connect(self._submit)

        self.status_label = QLabel("")
        self.status_label.setObjectName("connectionStatus")
        self.status_label.setWordWrap(True)

        card_layout.addWidget(title)
        card_layout.addWidget(subtitle)
        card_layout.addSpacing(6)
        card_layout.addWidget(self.address_edit)
        card_layout.addWidget(self.connect_button)
        card_layout.addWidget(self.status_label)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.addStretch()
        outer.addWidget(card, 0, Qt.AlignmentFlag.AlignHCenter)
        outer.addStretch()

    def _submit(self) -> None:
        self.connect_requested.emit(self.address_edit.text())

    def set_connecting(self, connecting: bool, message: str = "") -> None:
        self.address_edit.setEnabled(not connecting)
        self.connect_button.setEnabled(not connecting)
        self.connect_button.setText("正在连接…" if connecting else "连接")
        self.status_label.setText(message)


class AlertPopup(QWidget):
    """不唤醒主窗口的置顶报警小窗，也用于位置和尺寸预览。"""

    dismissed = Signal()
    placement_confirmed = Signal(QByteArray)

    def __init__(self, settings: QSettings) -> None:
        super().__init__(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowCloseButtonHint,
        )
        self._settings = settings
        self._placement_mode = False
        self.setWindowTitle("AlertZone 报警")
        self.setMinimumSize(320, 210)
        self.resize(460, 310)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)

        self._title = QLabel("⚠ 检测到有人进入监控区域")
        self._title.setObjectName("alertTitle")
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._detail = QLabel("")
        self._detail.setObjectName("alertDetail")
        self._detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._detail.setWordWrap(True)
        self._image = QLabel("等待报警截图")
        self._image.setObjectName("alertImage")
        self._image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image.setMinimumHeight(90)
        self._image.setScaledContents(False)
        self._image_pixmap: QPixmap | None = None

        self._button = QPushButton("我知道了")
        self._button.setObjectName("alertButton")
        self._button.clicked.connect(self._accept)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 16)
        layout.setSpacing(10)
        layout.addWidget(self._title)
        layout.addWidget(self._detail)
        layout.addWidget(self._image, 1)
        layout.addWidget(self._button)

        self.apply_theme("light")

    def apply_theme(self, theme: str) -> None:
        """让报警小窗在浅色和深色桌面主题下都保持足够对比度。"""
        if theme == "dark":
            background = "#24171a"
            title = "#ff8f95"
            detail = "#f3c7ca"
            image_background = "#10090b"
            image_text = "#d99da2"
            border = "#713a40"
            button = "#e0444d"
            button_hover = "#f05a62"
        else:
            background = "#fff5f5"
            title = "#b91c1c"
            detail = "#4b1919"
            image_background = "#2b1111"
            image_text = "#e9b8bc"
            border = "#efb4b4"
            button = "#dc2626"
            button_hover = "#b91c1c"
        self.setStyleSheet(
            f"""
            AlertPopup {{ background: {background}; }}
            #alertTitle {{
                color: {title};
                font-size: 20px;
                font-weight: 800;
            }}
            #alertDetail {{ color: {detail}; font-size: 14px; }}
            #alertImage {{
                color: {image_text};
                background: {image_background};
                border: 1px solid {border};
                border-radius: 8px;
            }}
            #alertButton {{
                min-height: 36px;
                color: white;
                background: {button};
                border: 0;
                border-radius: 7px;
                font-weight: 700;
            }}
            #alertButton:hover {{ background: {button_hover}; }}
            """
        )

    def restore_saved_geometry(self) -> None:
        geometry = self._settings.value("popup/geometry")
        if (
            isinstance(geometry, QByteArray)
            and not geometry.isEmpty()
            and self.restoreGeometry(geometry)
        ):
            return
        self.resize(460, 310)
        screen = QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            self.move(
                available.right() - self.width() - 24,
                available.bottom() - self.height() - 24,
            )

    def show_placement_preview(self) -> None:
        self._placement_mode = True
        self.restore_saved_geometry()
        self._title.setText("移动或缩放这个报警小窗")
        self._detail.setText("拖动标题栏调整位置，拖动窗口边缘调整大小。")
        self._image_pixmap = None
        self._image.setPixmap(QPixmap())
        self._image.setText("这里将显示报警截图")
        self._button.setText("确定位置和大小")
        self.show()
        self.raise_()
        self.activateWindow()

    def show_alert(self, people_count: int, event_time: str = "") -> None:
        self._placement_mode = False
        self.restore_saved_geometry()
        self._title.setText("⚠ 检测到有人进入监控区域")
        people_text = f"检测到 {max(people_count, 1)} 人进入监控区域"
        self._detail.setText(
            f"{people_text}\n{event_time}" if event_time else people_text
        )
        self._image_pixmap = None
        self._image.setPixmap(QPixmap())
        self._image.setText("正在获取报警截图…")
        self._button.setText("我知道了")
        self.show()
        self.raise_()
        self.activateWindow()

    def set_event_image(self, image_data: bytes) -> None:
        pixmap = QPixmap()
        if not pixmap.loadFromData(image_data):
            self._image.setText("报警截图不可用")
            return
        self._image_pixmap = pixmap
        self._update_scaled_pixmap()

    def _update_scaled_pixmap(self) -> None:
        if self._image_pixmap is None:
            return
        size = self._image.size()
        self._image.setPixmap(
            self._image_pixmap.scaled(
                size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._update_scaled_pixmap()

    def _accept(self) -> None:
        if self._placement_mode:
            geometry = self.saveGeometry()
            self._settings.setValue("popup/geometry", geometry)
            self._settings.sync()
            self._placement_mode = False
            self.hide()
            self.placement_confirmed.emit(geometry)
            return
        self.hide()
        self.dismissed.emit()

    def closeEvent(self, event: QCloseEvent) -> None:
        event.ignore()
        self._accept()


class SettingsDialog(QDialog):
    """桌面端后台报警、弹窗及提示音设置。"""

    settings_changed = Signal()

    def __init__(
        self,
        settings: QSettings,
        popup: AlertPopup,
        sound_preview: Any,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._popup = popup
        self._sound_preview = sound_preview
        self.setWindowTitle("AlertZone Desktop 设置")
        self.setMinimumWidth(580)
        self.setModal(False)

        self.theme_mode = QComboBox()
        self.theme_mode.addItem("跟随 Web 页面", "follow-web")
        self.theme_mode.addItem("浅色", "light")
        self.theme_mode.addItem("深色", "dark")
        theme_index = self.theme_mode.findData(
            str(settings.value("appearance/theme_mode", "follow-web"))
        )
        self.theme_mode.setCurrentIndex(max(theme_index, 0))

        self.alert_enabled = QCheckBox("启用桌面端后台报警小窗")
        self.alert_enabled.setChecked(setting_bool(settings, "alert/enabled", True))
        self.continuous_enabled = QCheckBox(
            "小窗关闭后持续重新布防（人物未离开也可再次报警）"
        )
        self.continuous_enabled.setChecked(
            setting_bool(settings, "alert/continuous", False)
        )

        self.confirm_seconds = QComboBox()
        for value, label in (
            (0.0, "立即"),
            (0.2, "0.2 秒"),
            (0.5, "0.5 秒"),
            (1.0, "1 秒"),
            (2.0, "2 秒"),
        ):
            self.confirm_seconds.addItem(label, value)
        saved_confirm = float(settings.value("alert/confirm_seconds", 0.2))
        selected = self.confirm_seconds.findData(saved_confirm)
        self.confirm_seconds.setCurrentIndex(max(selected, 0))

        self.auto_close_seconds = QSpinBox()
        self.auto_close_seconds.setRange(0, 300)
        self.auto_close_seconds.setSuffix(" 秒")
        self.auto_close_seconds.setSpecialValueText("不自动关闭")
        self.auto_close_seconds.setValue(
            int(settings.value("popup/auto_close_seconds", 10))
        )

        placement_button = QPushButton("设置弹窗位置和大小…")
        placement_button.clicked.connect(self._popup.show_placement_preview)

        self.sound_mode = QComboBox()
        self.sound_mode.addItem("电脑默认提示音", "default")
        self.sound_mode.addItem("自定义声音文件", "custom")
        mode_index = self.sound_mode.findData(
            str(settings.value("sound/mode", "default"))
        )
        self.sound_mode.setCurrentIndex(max(mode_index, 0))
        self.sound_mode.currentIndexChanged.connect(self._sync_sound_controls)

        self.sound_path = QLineEdit(str(settings.value("sound/custom_path", "")))
        browse_button = QPushButton("选择…")
        browse_button.clicked.connect(self._choose_sound)
        sound_file_row = QWidget()
        sound_file_layout = QHBoxLayout(sound_file_row)
        sound_file_layout.setContentsMargins(0, 0, 0, 0)
        sound_file_layout.addWidget(self.sound_path, 1)
        sound_file_layout.addWidget(browse_button)
        self._browse_button = browse_button

        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(int(settings.value("sound/volume", 80)))

        test_sound_button = QPushButton("试听提示音")
        test_sound_button.clicked.connect(self._preview_current_sound)

        form = QFormLayout()
        form.setSpacing(12)
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.addRow("界面主题", self.theme_mode)
        form.addRow(self.alert_enabled)
        form.addRow(self.continuous_enabled)
        form.addRow("报警确认时间", self.confirm_seconds)
        form.addRow("小窗自动关闭", self.auto_close_seconds)
        form.addRow("报警小窗", placement_button)
        form.addRow("提示音", self.sound_mode)
        form.addRow("自定义文件", sound_file_row)
        form.addRow("音量", self.volume)
        form.addRow(test_sound_button)

        cancel_button = QPushButton("取消")
        cancel_button.clicked.connect(self.reject)
        save_button = QPushButton("保存")
        save_button.setObjectName("primaryButton")
        save_button.clicked.connect(self._save)
        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(cancel_button)
        buttons.addWidget(save_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 18)
        layout.addLayout(form)
        layout.addSpacing(12)
        layout.addLayout(buttons)
        self._sync_sound_controls()

    def _sync_sound_controls(self) -> None:
        enabled = self.sound_mode.currentData() == "custom"
        self.sound_path.setEnabled(enabled)
        self._browse_button.setEnabled(enabled)

    def _choose_sound(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择报警声音",
            self.sound_path.text(),
            "音频文件 (*.wav *.mp3 *.m4a *.aac *.flac *.ogg);;所有文件 (*)",
        )
        if path:
            self.sound_path.setText(path)

    def _preview_current_sound(self) -> None:
        self._sound_preview(
            str(self.sound_mode.currentData()),
            self.sound_path.text().strip(),
            self.volume.value(),
        )

    def _save(self) -> None:
        if (
            self.sound_mode.currentData() == "custom"
            and self.sound_path.text().strip()
            and not Path(self.sound_path.text().strip()).is_file()
        ):
            QMessageBox.warning(self, "声音文件不可用", "所选声音文件不存在。")
            return
        self._settings.setValue("alert/enabled", self.alert_enabled.isChecked())
        self._settings.setValue(
            "appearance/theme_mode", self.theme_mode.currentData()
        )
        self._settings.setValue("alert/continuous", self.continuous_enabled.isChecked())
        self._settings.setValue(
            "alert/confirm_seconds", self.confirm_seconds.currentData()
        )
        self._settings.setValue(
            "popup/auto_close_seconds", self.auto_close_seconds.value()
        )
        self._settings.setValue("sound/mode", self.sound_mode.currentData())
        self._settings.setValue("sound/custom_path", self.sound_path.text().strip())
        self._settings.setValue("sound/volume", self.volume.value())
        self._settings.sync()
        self.settings_changed.emit()
        self.accept()


class StatusMonitor(QObject):
    """使用 Qt 网络栈异步轮询状态，主窗口隐藏时仍保持工作。"""

    connection_changed = Signal(bool, str)
    intrusion_detected = Signal(dict)

    def __init__(self, settings: QSettings, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._manager = QNetworkAccessManager(self)
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._poll)
        self._server_url = ""
        self._request_active = False
        self._instance_id = ""
        self._last_event_id = 0
        self._online = False

    def set_server_url(self, server_url: str) -> None:
        changed = server_url != self._server_url
        self._server_url = server_url
        if changed:
            self._instance_id = ""
            self._last_event_id = 0
        if server_url:
            self._timer.start()
            self._poll()
        else:
            self._timer.stop()

    def _poll(self) -> None:
        if not self._server_url or self._request_active:
            return
        request = QNetworkRequest(
            QUrl(f"{self._server_url}/api/status?desktop={id(self)}")
        )
        request.setTransferTimeout(REQUEST_TIMEOUT_MS)
        request.setAttribute(
            QNetworkRequest.Attribute.CacheLoadControlAttribute,
            QNetworkRequest.CacheLoadControl.AlwaysNetwork,
        )
        self._request_active = True
        reply = self._manager.get(request)
        reply.finished.connect(lambda: self._handle_status(reply))

    def _handle_status(self, reply: QNetworkReply) -> None:
        self._request_active = False
        try:
            if reply.error() != QNetworkReply.NetworkError.NoError:
                self._set_online(False, reply.errorString())
                return
            payload = json.loads(bytes(reply.readAll()).decode("utf-8"))
            instance_id = str(payload.get("instance_id", ""))
            event_id = int(payload.get("intrusion_event_id", 0))
            if not instance_id:
                raise ValueError("响应缺少 instance_id")

            first_snapshot = instance_id != self._instance_id
            if first_snapshot:
                self._instance_id = instance_id
                self._last_event_id = event_id
            elif event_id > self._last_event_id:
                self._last_event_id = event_id
                if setting_bool(self._settings, "alert/enabled", True):
                    self.intrusion_detected.emit(payload)
            self._set_online(True, "已连接")
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            self._set_online(False, f"状态数据无效：{error}")
        finally:
            reply.deleteLater()

    def _set_online(self, online: bool, detail: str) -> None:
        if online != self._online:
            self._online = online
            self.connection_changed.emit(online, detail)
        elif not online:
            self.connection_changed.emit(False, detail)

    def request_rearm(self) -> None:
        if not self._server_url:
            return
        confirm = float(self._settings.value("alert/confirm_seconds", 0.2))
        url = QUrl(f"{self._server_url}/api/rearm-alert?confirm_seconds={confirm:g}")
        request = QNetworkRequest(url)
        request.setTransferTimeout(REQUEST_TIMEOUT_MS)
        request.setHeader(
            QNetworkRequest.KnownHeaders.ContentTypeHeader,
            "application/x-www-form-urlencoded",
        )
        reply = self._manager.post(request, QByteArray())
        reply.finished.connect(reply.deleteLater)


class MainWindow(QMainWindow):
    """桌面端主窗口、托盘与原生报警协调器。"""

    def __init__(self) -> None:
        super().__init__()
        self._settings = QSettings(ORGANIZATION_NAME, APP_NAME)
        self._quitting = False
        self._probe_reply: QNetworkReply | None = None
        self._server_url = ""
        self._current_alert_event = 0
        self._settings_dialog: SettingsDialog | None = None
        self._theme_mode = str(
            self._settings.value("appearance/theme_mode", "follow-web")
        )
        self._current_theme = ""

        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(780, 620)
        self.resize(1180, 760)
        icon_path = app_icon_path()
        self._icon = QIcon(str(icon_path)) if icon_path else QIcon()
        if not self._icon.isNull():
            self.setWindowIcon(self._icon)

        self._network = QNetworkAccessManager(self)
        self._audio_output = QAudioOutput(self)
        self._player = QMediaPlayer(self)
        self._player.setAudioOutput(self._audio_output)

        self._popup = AlertPopup(self._settings)
        if not self._icon.isNull():
            self._popup.setWindowIcon(self._icon)
        self._popup.dismissed.connect(self._on_popup_dismissed)

        self._monitor = StatusMonitor(self._settings, self)
        self._monitor.intrusion_detected.connect(self._show_intrusion)
        self._monitor.connection_changed.connect(self._on_monitor_connection_changed)

        self._connection_page = ConnectionPage(self._settings)
        self._connection_page.connect_requested.connect(self.connect_to_server)
        self._browser = QWebEngineView()
        # 命名 Profile 会持久保存网页的 localStorage，Web 端主题、警告方式和
        # 实时预览等选项在桌面程序重启后仍能保留。
        self._web_profile = QWebEngineProfile(
            "AlertZoneDesktop", self._browser
        )
        self._web_page = QWebEnginePage(self._web_profile, self._browser)
        self._browser.setPage(self._web_page)
        self._browser.titleChanged.connect(self._update_browser_title)
        self._browser.loadFinished.connect(self._on_web_page_loaded)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._connection_page)
        self._stack.addWidget(self._browser)
        self.setCentralWidget(self._stack)
        self._build_toolbar()
        self._build_tray()
        self._apply_theme(
            str(self._settings.value("appearance/last_theme", "light"))
        )
        self._theme_sync_timer = QTimer(self)
        self._theme_sync_timer.setInterval(800)
        self._theme_sync_timer.timeout.connect(self._sync_theme_with_web)
        self._theme_sync_timer.start()

        saved_geometry = self._settings.value("window/geometry")
        if isinstance(saved_geometry, QByteArray):
            self.restoreGeometry(saved_geometry)

        saved_url = str(self._settings.value("connection/server_url", "")).strip()
        if saved_url and setting_bool(self._settings, "connection/auto_connect", True):
            QTimer.singleShot(0, lambda: self.connect_to_server(saved_url))

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("主工具栏", self)
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.addToolBar(toolbar)

        self._back_action = QAction("返回", self)
        self._back_action.triggered.connect(self._browser.back)
        toolbar.addAction(self._back_action)

        self._reload_action = QAction("刷新", self)
        self._reload_action.triggered.connect(self._browser.reload)
        toolbar.addAction(self._reload_action)

        toolbar.addSeparator()
        self._address_label = QLabel("尚未连接")
        self._address_label.setObjectName("toolbarAddress")
        self._address_label.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        self._address_label.setMinimumWidth(92)
        toolbar.addWidget(self._address_label)

        spacer = QWidget()
        spacer.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        toolbar.addWidget(spacer)

        change_action = QAction("更换地址", self)
        change_action.triggered.connect(self.show_connection_page)
        toolbar.addAction(change_action)

        settings_action = QAction("桌面设置", self)
        settings_action.triggered.connect(self.open_settings)
        toolbar.addAction(settings_action)

        tray_action = QAction("后台运行", self)
        tray_action.triggered.connect(self.hide_to_tray)
        toolbar.addAction(tray_action)

        toolbar.setVisible(False)
        self._toolbar = toolbar

    def _build_tray(self) -> None:
        self._tray: QSystemTrayIcon | None = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        tray = QSystemTrayIcon(self._icon, self)
        tray.setToolTip(APP_NAME)
        menu = QMenu()
        open_action = menu.addAction("打开主界面")
        open_action.triggered.connect(self.show_main_window)
        self._tray_status_action = menu.addAction("尚未连接")
        self._tray_status_action.setEnabled(False)
        menu.addSeparator()
        settings_action = menu.addAction("桌面设置")
        settings_action.triggered.connect(self.open_settings)
        menu.addSeparator()
        quit_action = menu.addAction("退出")
        quit_action.triggered.connect(self.quit_application)
        tray.setContextMenu(menu)
        tray.activated.connect(self._tray_activated)
        tray.show()
        self._tray = tray

    def connect_to_server(self, raw_url: str) -> None:
        try:
            server_url = normalize_server_url(raw_url)
        except ValueError as error:
            self._connection_page.set_connecting(False, str(error))
            return

        if self._probe_reply is not None:
            self._probe_reply.abort()
            self._probe_reply.deleteLater()
            self._probe_reply = None
        self._connection_page.address_edit.setText(server_url)
        self._connection_page.set_connecting(True, "正在验证 AlertZone Client…")
        request = QNetworkRequest(QUrl(f"{server_url}/api/status"))
        request.setTransferTimeout(REQUEST_TIMEOUT_MS)
        reply = self._network.get(request)
        self._probe_reply = reply
        reply.finished.connect(lambda: self._finish_connection_probe(reply, server_url))

    def _finish_connection_probe(self, reply: QNetworkReply, server_url: str) -> None:
        if reply is not self._probe_reply:
            return
        self._probe_reply = None
        try:
            if reply.error() != QNetworkReply.NetworkError.NoError:
                raise ValueError(reply.errorString())
            payload = json.loads(bytes(reply.readAll()).decode("utf-8"))
            if not isinstance(payload, dict) or "instance_id" not in payload:
                raise ValueError("该地址不是可识别的 AlertZone Client")
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as error:
            self._connection_page.set_connecting(False, f"连接失败：{error}")
            return
        finally:
            reply.deleteLater()

        self._server_url = server_url
        self._settings.setValue("connection/server_url", server_url)
        self._settings.setValue("connection/auto_connect", True)
        self._settings.sync()
        self._connection_page.set_connecting(False, "")
        self._browser.setUrl(QUrl(f"{server_url}/"))
        self._monitor.set_server_url(server_url)
        QTimer.singleShot(0, self._monitor.request_rearm)
        self._address_label.setText("● 已连接")
        self._address_label.setToolTip(server_url)
        self._address_label.setProperty("online", True)
        self._address_label.style().unpolish(self._address_label)
        self._address_label.style().polish(self._address_label)
        if self._tray is not None:
            self._tray_status_action.setText(f"已连接：{server_url}")
            self._tray.setToolTip(f"{APP_NAME}\n{server_url}")
        self._stack.setCurrentWidget(self._browser)
        self._toolbar.setVisible(True)

    def show_connection_page(self) -> None:
        self._stack.setCurrentWidget(self._connection_page)
        self._toolbar.setVisible(False)
        self._connection_page.address_edit.setFocus()
        self._connection_page.address_edit.selectAll()

    def open_settings(self) -> None:
        if self._settings_dialog is not None:
            self._settings_dialog.show()
            self._settings_dialog.raise_()
            self._settings_dialog.activateWindow()
            return
        dialog = SettingsDialog(
            self._settings,
            self._popup,
            self._play_sound,
            self if self.isVisible() else None,
        )
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        if not self._icon.isNull():
            dialog.setWindowIcon(self._icon)
        dialog.settings_changed.connect(self._on_settings_changed)
        dialog.destroyed.connect(self._clear_settings_dialog)
        self._settings_dialog = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _clear_settings_dialog(self, _object: QObject | None = None) -> None:
        self._settings_dialog = None

    def _on_settings_changed(self) -> None:
        self._theme_mode = str(
            self._settings.value("appearance/theme_mode", "follow-web")
        )
        if self._theme_mode in {"light", "dark"}:
            self._apply_theme(self._theme_mode)
            self._set_web_theme(self._theme_mode)
        else:
            self._sync_theme_with_web()
        if not setting_bool(self._settings, "alert/enabled", True):
            self._popup.hide()
            self._stop_sound()
        elif self._server_url:
            self._monitor.request_rearm()

    def _show_intrusion(self, data: dict) -> None:
        event_id = int(data.get("intrusion_event_id", 0))
        self._current_alert_event = event_id
        count = int(data.get("intrusion_people_count") or data.get("people_count") or 1)
        timestamp = ""
        event_time = data.get("intrusion_time")
        if isinstance(event_time, (float, int)):
            timestamp = (
                datetime.fromtimestamp(event_time, tz=timezone.utc)
                .astimezone()
                .strftime("%Y-%m-%d %H:%M:%S")
            )
        self._popup.show_alert(count, timestamp)
        self._play_configured_sound()
        if data.get("intrusion_image_available") and self._server_url:
            request = QNetworkRequest(
                QUrl(f"{self._server_url}/api/intruder.jpg?event={event_id}")
            )
            request.setTransferTimeout(REQUEST_TIMEOUT_MS)
            reply = self._network.get(request)
            reply.finished.connect(lambda: self._finish_alert_image(reply, event_id))

        auto_close = int(self._settings.value("popup/auto_close_seconds", 10))
        if auto_close > 0:
            QTimer.singleShot(
                auto_close * 1000,
                lambda: self._auto_close_alert(event_id),
            )

    def _finish_alert_image(self, reply: QNetworkReply, event_id: int) -> None:
        try:
            if (
                reply.error() == QNetworkReply.NetworkError.NoError
                and event_id == self._current_alert_event
                and self._popup.isVisible()
            ):
                self._popup.set_event_image(bytes(reply.readAll()))
        finally:
            reply.deleteLater()

    def _auto_close_alert(self, event_id: int) -> None:
        if event_id == self._current_alert_event and self._popup.isVisible():
            self._popup.hide()
            self._on_popup_dismissed()

    def _on_popup_dismissed(self) -> None:
        self._stop_sound()
        if setting_bool(self._settings, "alert/continuous", False):
            self._monitor.request_rearm()

    def _play_configured_sound(self) -> None:
        self._play_sound(
            str(self._settings.value("sound/mode", "default")),
            str(self._settings.value("sound/custom_path", "")),
            int(self._settings.value("sound/volume", 80)),
        )

    def _play_sound(self, mode: str, path: str, volume: int) -> None:
        self._stop_sound()
        if mode == "custom" and path and Path(path).is_file():
            self._audio_output.setVolume(max(0, min(volume, 100)) / 100.0)
            self._player.setSource(QUrl.fromLocalFile(path))
            self._player.play()
        else:
            QApplication.beep()

    def _stop_sound(self) -> None:
        self._player.stop()

    def _on_monitor_connection_changed(self, online: bool, detail: str) -> None:
        if online:
            self._address_label.setText("● 已连接")
            self._address_label.setToolTip(self._server_url)
            self._address_label.setProperty("online", True)
            if self._tray is not None:
                self._tray_status_action.setText(f"已连接：{self._server_url}")
        else:
            self._address_label.setText(f"● 连接中断：{detail}")
            self._address_label.setProperty("online", False)
            if self._tray is not None:
                self._tray_status_action.setText("连接中断，正在重试")
        self._address_label.style().unpolish(self._address_label)
        self._address_label.style().polish(self._address_label)

    def _update_browser_title(self, title: str) -> None:
        if title:
            self.setWindowTitle(f"{title} — {APP_NAME}")

    def _on_web_page_loaded(self, loaded: bool) -> None:
        if not loaded:
            return
        if self._theme_mode in {"light", "dark"}:
            self._set_web_theme(self._theme_mode)
        else:
            self._sync_theme_with_web()

    def _sync_theme_with_web(self) -> None:
        """跟随嵌入页面的主题按钮，保持工具栏和原生窗口一致。"""
        if self._theme_mode != "follow-web" or not self._server_url:
            return
        self._browser.page().runJavaScript(
            "(() => {"
            "const button = document.querySelector('#themeToggle');"
            "if (button) button.hidden = false;"
            "return document.documentElement.dataset.theme || 'light';"
            "})()",
            self._receive_web_theme,
        )

    def _receive_web_theme(self, value: Any) -> None:
        theme = str(value)
        if theme in {"light", "dark"}:
            self._apply_theme(theme)

    def _set_web_theme(self, theme: str) -> None:
        if theme not in {"light", "dark"} or not self._server_url:
            return
        script = (
            f"localStorage.setItem('cameraWebTheme', '{theme}');"
            f"if (typeof applyWebTheme === 'function') "
            f"{{ applyWebTheme('{theme}'); }} "
            f"else {{ document.documentElement.dataset.theme = '{theme}'; }}"
            "const button = document.querySelector('#themeToggle');"
            "if (button) button.hidden = true;"
        )
        self._browser.page().runJavaScript(script)

    def hide_to_tray(self) -> None:
        if self._tray is None:
            QMessageBox.information(
                self,
                "无法后台运行",
                "当前系统未提供托盘区域，主窗口将保持打开。",
            )
            return
        self.hide()
        if not setting_bool(self._settings, "tray/hint_shown", False):
            self._tray.showMessage(
                APP_NAME,
                "程序正在后台运行；检测到报警时只会显示报警小窗。",
                QSystemTrayIcon.MessageIcon.Information,
                3500,
            )
            self._settings.setValue("tray/hint_shown", True)

    def show_main_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.show_main_window()

    def quit_application(self) -> None:
        self._quitting = True
        self._settings.setValue("window/geometry", self.saveGeometry())
        self._settings.sync()
        self._stop_sound()
        if self._tray is not None:
            self._tray.hide()
        QApplication.quit()

    def closeEvent(self, event: QCloseEvent) -> None:
        self._settings.setValue("window/geometry", self.saveGeometry())
        self._settings.sync()
        if self._quitting:
            event.accept()
            return
        if self._tray is None:
            event.accept()
            QTimer.singleShot(0, QApplication.quit)
            return
        event.ignore()
        self.hide_to_tray()

    def _apply_theme(self, theme: str) -> None:
        """统一应用到主窗口、连接页、设置页、工具栏和报警小窗。"""
        theme = "dark" if theme == "dark" else "light"
        if theme == self._current_theme:
            return
        self._current_theme = theme
        self._settings.setValue("appearance/last_theme", theme)

        if theme == "dark":
            colors = {
                "page": "#0f141c",
                "surface": "#171e29",
                "surface_alt": "#202937",
                "input": "#111821",
                "border": "#344154",
                "text": "#edf2f7",
                "muted": "#aab6c6",
                "disabled": "#68778b",
                "primary": "#4f8cff",
                "primary_hover": "#6ba0ff",
                "success": "#43c98d",
                "danger": "#ff7b83",
            }
        else:
            colors = {
                "page": "#eef2f7",
                "surface": "#ffffff",
                "surface_alt": "#f6f8fb",
                "input": "#ffffff",
                "border": "#cbd4e1",
                "text": "#172033",
                "muted": "#5f6d80",
                "disabled": "#9aa6b5",
                "primary": "#2563eb",
                "primary_hover": "#1d4ed8",
                "success": "#168653",
                "danger": "#c0392b",
            }

        style = f"""
            QMainWindow, QDialog, #connectionPage {{
                color: {colors["text"]};
                background: {colors["page"]};
            }}
            QWidget {{
                color: {colors["text"]};
            }}
            #connectionCard {{
                background: {colors["surface"]};
                border: 1px solid {colors["border"]};
                border-radius: 12px;
            }}
            #connectionTitle {{
                color: {colors["text"]};
                font-size: 25px;
                font-weight: 800;
            }}
            #connectionSubtitle, #connectionStatus {{
                color: {colors["muted"]};
                font-size: 13px;
            }}
            QToolBar {{
                min-height: 38px;
                padding: 3px 6px;
                spacing: 3px;
                color: {colors["text"]};
                background: {colors["surface"]};
                border: 0;
                border-bottom: 1px solid {colors["border"]};
            }}
            QToolButton {{
                min-height: 28px;
                padding: 0 9px;
                color: {colors["text"]};
                background: transparent;
                border: 1px solid transparent;
                border-radius: 6px;
            }}
            QToolButton:hover {{
                background: {colors["surface_alt"]};
                border-color: {colors["border"]};
            }}
            QLabel {{
                color: {colors["text"]};
            }}
            QCheckBox {{
                min-height: 28px;
                spacing: 8px;
                color: {colors["text"]};
            }}
            QLineEdit, QComboBox, QSpinBox {{
                min-height: 36px;
                padding: 0 9px;
                color: {colors["text"]};
                selection-color: white;
                selection-background-color: {colors["primary"]};
                background: {colors["input"]};
                border: 1px solid {colors["border"]};
                border-radius: 7px;
            }}
            QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{
                border: 1px solid {colors["primary"]};
            }}
            QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{
                color: {colors["disabled"]};
                background: {colors["surface_alt"]};
            }}
            QComboBox QAbstractItemView {{
                color: {colors["text"]};
                selection-color: white;
                selection-background-color: {colors["primary"]};
                background: {colors["surface"]};
                border: 1px solid {colors["border"]};
            }}
            QPushButton {{
                min-height: 34px;
                padding: 0 14px;
                color: {colors["text"]};
                background: {colors["surface_alt"]};
                border: 1px solid {colors["border"]};
                border-radius: 7px;
            }}
            QPushButton:hover {{
                border-color: {colors["primary"]};
                background: {colors["surface"]};
            }}
            QPushButton:disabled {{
                color: {colors["disabled"]};
                background: {colors["surface_alt"]};
                border-color: {colors["border"]};
            }}
            #primaryButton {{
                color: white;
                background: {colors["primary"]};
                border-color: {colors["primary"]};
                font-weight: 700;
            }}
            #primaryButton:hover {{
                background: {colors["primary_hover"]};
                border-color: {colors["primary_hover"]};
            }}
            QSlider::groove:horizontal {{
                height: 5px;
                background: {colors["border"]};
                border-radius: 2px;
            }}
            QSlider::sub-page:horizontal {{
                background: {colors["primary"]};
                border-radius: 2px;
            }}
            QSlider::handle:horizontal {{
                width: 16px;
                margin: -6px 0;
                background: {colors["surface"]};
                border: 2px solid {colors["primary"]};
                border-radius: 8px;
            }}
            #toolbarAddress {{
                color: {colors["danger"]};
                padding: 0 7px;
                font-weight: 700;
            }}
            #toolbarAddress[online="true"] {{
                color: {colors["success"]};
            }}
        """
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(style)
        self._popup.apply_theme(theme)


def main() -> int:
    QApplication.setOrganizationName(ORGANIZATION_NAME)
    QApplication.setApplicationName(APP_NAME)
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    icon_path = app_icon_path()
    if icon_path is not None:
        app.setWindowIcon(QIcon(str(icon_path)))
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
