"""AlertZone Client 的独立局域网桌面前端。

界面完全由 PySide6 原生控件绘制，只通过 Client 的 JSON、JPEG 和控制接口
获取状态、预览与报警事件，不加载或依赖 Client 的 Web 页面。
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
    QEvent,
    QObject,
    QPoint,
    QSettings,
    QSize,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import QCloseEvent, QColor, QIcon, QPalette, QPixmap
from PySide6.QtNetwork import (
    QNetworkAccessManager,
    QNetworkReply,
    QNetworkRequest,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSlider,
    QStackedWidget,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
except ImportError:
    # 部分 Conda 的 PySide6 只包含基础组件。界面仍可正常启动，
    # 提示音会自动回退到系统默认声音。
    QAudioOutput = None
    QMediaPlayer = None

APP_NAME = "AlertZone Desktop"
ORGANIZATION_NAME = "AlertZone"
DEFAULT_PORT = 8765
POLL_INTERVAL_MS = 600
REQUEST_TIMEOUT_MS = 3500
ALERT_DISPLAY_OPTIONS = (
    ("zoom", "放大人物"),
    ("live", "实时预览"),
    ("zoom-red", "全屏红色且放大人物"),
    ("live-red", "全屏红色且实时预览"),
)
ALERT_IMAGE_MODES = {"zoom", "zoom-red", "live", "live-red"}
ALERT_LIVE_MODES = {"live", "live-red"}

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


def normalize_alert_display_mode(value: Any) -> str:
    """迁移旧设置并限制为与 Web 端一致的四种告警显示方式。"""
    mode = str(value)
    if mode == "red":
        return "zoom-red"
    if mode == "yellow":
        return "zoom"
    return mode if mode in dict(ALERT_DISPLAY_OPTIONS) else "zoom"


class DownwardComboBox(QComboBox):
    """始终从控件下方展开的圆角下拉框。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.view().setObjectName("comboPopupView")
        popup_palette = self.view().palette()
        popup_palette.setColor(
            QPalette.ColorRole.Highlight,
            QColor("#16a34a"),
        )
        popup_palette.setColor(
            QPalette.ColorRole.HighlightedText,
            QColor("#ffffff"),
        )
        self.view().setPalette(popup_palette)

    def showPopup(self) -> None:
        super().showPopup()
        QTimer.singleShot(0, self._place_popup_below)

    def _place_popup_below(self) -> None:
        popup = self.view().window()
        anchor = self.mapToGlobal(QPoint(0, self.height() + 3))
        popup_width = max(self.width(), popup.width())
        popup_height = popup.height()
        popup_x = anchor.x()
        popup_y = anchor.y()
        screen = QApplication.screenAt(anchor)
        if screen is not None:
            available = screen.availableGeometry()
            popup_width = min(popup_width, available.width())
            popup_x = min(
                max(popup_x, available.left()),
                available.right() - popup_width + 1,
            )
            row_height = max(self.view().sizeHintForRow(0), 28)
            available_height = max(
                row_height * 2 + 10,
                available.bottom() - popup_y + 1,
            )
            popup_height = min(popup_height, available_height)
        popup.resize(popup_width, popup_height)
        popup.move(popup_x, popup_y)


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
        self._theme = "light"
        self._display_mode = normalize_alert_display_mode(
            settings.value("alert/display_mode", "zoom")
        )
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
        """应用桌面主题；具体告警配色由当前显示方式决定。"""
        self._theme = "dark" if theme == "dark" else "light"
        self._apply_visual_style()

    def set_display_mode(self, mode: str) -> None:
        """立即切换小窗显示方式，使其跟随主页选择。"""
        self._display_mode = normalize_alert_display_mode(mode)
        self._image.setVisible(self._display_mode in ALERT_IMAGE_MODES)
        self._apply_visual_style()

    def display_mode(self) -> str:
        return self._display_mode

    def is_alert_active(self) -> bool:
        return self.isVisible() and not self._placement_mode

    def _apply_visual_style(self) -> None:
        mode = self._display_mode
        self._image.setVisible(mode in ALERT_IMAGE_MODES)
        if mode in {"zoom-red", "live-red"}:
            background = "#e00018"
            title = "#ffffff"
            detail = "#ffffff"
            image_background = "#290006"
            image_text = "#ffd7dc"
            border = "#ffffff"
            button = "#ffffff"
            button_hover = "#ffe5e8"
            button_text = "#a50012"
        elif self._theme == "dark":
            background = "#24171a"
            title = "#ff8f95"
            detail = "#f3c7ca"
            image_background = "#10090b"
            image_text = "#d99da2"
            border = "#713a40"
            button = "#e0444d"
            button_hover = "#f05a62"
            button_text = "#ffffff"
        else:
            background = "#fff5f5"
            title = "#b91c1c"
            detail = "#4b1919"
            image_background = "#2b1111"
            image_text = "#e9b8bc"
            border = "#efb4b4"
            button = "#dc2626"
            button_hover = "#b91c1c"
            button_text = "#ffffff"
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
                color: {button_text};
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
        self.set_display_mode(
            normalize_alert_display_mode(
                self._settings.value("alert/display_mode", "zoom")
            )
        )
        self.restore_saved_geometry()
        self._title.setText("移动或缩放这个报警小窗")
        self._detail.setText("拖动标题栏调整位置，拖动窗口边缘调整大小。")
        self._image_pixmap = None
        self._image.setPixmap(QPixmap())
        self._image.setText(
            "这里将显示实时预览"
            if self._display_mode in ALERT_LIVE_MODES
            else "这里将显示报警截图"
        )
        self._button.setText("确定位置和大小")
        self.show()
        self.raise_()
        self.activateWindow()

    def show_alert(self, people_count: int, event_time: str = "") -> None:
        self._placement_mode = False
        self.set_display_mode(
            normalize_alert_display_mode(
                self._settings.value("alert/display_mode", "zoom")
            )
        )
        self.restore_saved_geometry()
        self._title.setText("⚠️ 警告 ⚠️")
        people_text = f"检测到 {max(people_count, 1)} 人进入监控区域"
        self._detail.setText(
            f"{people_text}\n{event_time}" if event_time else people_text
        )
        self._image_pixmap = None
        self._image.setPixmap(QPixmap())
        self._image.setText(
            "正在获取实时预览…"
            if self._display_mode in ALERT_LIVE_MODES
            else "正在获取报警截图…"
        )
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


class AlertSettingsDialog(QDialog):
    """告警显示、确认、自动退出和提示音设置。"""

    settings_changed = Signal()

    def __init__(
        self,
        settings: QSettings,
        parent: QWidget | None = None,
        sound_preview: Any | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self.setWindowTitle("告警设置")

        self.alert_display_mode = DownwardComboBox()
        self.alert_display_mode.setAccessibleName("告警显示方式")
        self.alert_display_mode.setMinimumWidth(240)
        self.alert_display_mode.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        self.alert_display_mode.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents
        )
        for mode, label in ALERT_DISPLAY_OPTIONS:
            self.alert_display_mode.addItem(label, mode)
        selected_mode = self.alert_display_mode.findData(
            normalize_alert_display_mode(
                settings.value("alert/display_mode", "zoom")
            )
        )
        self.alert_display_mode.setCurrentIndex(max(selected_mode, 0))

        self.confirm_seconds = DownwardComboBox()
        self.confirm_seconds.setMinimumWidth(130)
        self.confirm_seconds.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        for value, label in (
            (0.0, "立即"),
            (0.2, "0.2 秒"),
            (0.5, "0.5 秒"),
            (1.0, "1 秒"),
            (2.0, "2 秒"),
        ):
            self.confirm_seconds.addItem(label, value)
        selected = self.confirm_seconds.findData(
            float(settings.value("alert/confirm_seconds", 0.2))
        )
        self.confirm_seconds.setCurrentIndex(max(selected, 0))

        self.auto_exit_seconds = DownwardComboBox()
        self.auto_exit_seconds.setMinimumWidth(130)
        self.auto_exit_seconds.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        for value, label in (
            (5, "5 秒"),
            (10, "10 秒"),
            (15, "15 秒"),
            (30, "30 秒"),
            (60, "60 秒"),
            (0, "∞"),
        ):
            self.auto_exit_seconds.addItem(label, value)
        saved_auto_exit = int(
            settings.value(
                "alert/auto_exit_seconds",
                settings.value("popup/auto_close_seconds", 10),
            )
        )
        selected_auto_exit = self.auto_exit_seconds.findData(
            saved_auto_exit
        )
        self.auto_exit_seconds.setCurrentIndex(
            selected_auto_exit if selected_auto_exit >= 0 else 1
        )
        self.continuous_alert_display = QPushButton()
        self.continuous_alert_display.setObjectName("settingToggleButton")
        self.continuous_alert_display.setCheckable(True)
        self.continuous_alert_display.setMinimumWidth(72)
        self.continuous_alert_display.setChecked(
            setting_bool(
                settings,
                "alert/continuous_display",
                False,
            )
        )
        self.continuous_alert_display.toggled.connect(
            self._sync_continuous_display_button
        )
        self._sync_continuous_display_button(
            self.continuous_alert_display.isChecked()
        )

        title_label = QLabel("告警设置")
        title_label.setObjectName("dialogSectionTitle")

        settings_card = QFrame()
        settings_card.setObjectName("dialogCard")
        form = QFormLayout(settings_card)
        form.setContentsMargins(12, 10, 12, 10)
        form.setSpacing(8)
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        form.addRow("告警显示风格", self.alert_display_mode)
        form.addRow("告警触发时间", self.confirm_seconds)
        form.addRow("告警退出时长", self.auto_exit_seconds)
        form.addRow("连续告警显示", self.continuous_alert_display)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 14, 12, 12)
        layout.setSpacing(6)
        layout.addWidget(title_label)
        layout.addWidget(settings_card)
        sound_title = QLabel("提示音")
        sound_title.setObjectName("dialogSectionTitle")
        self.sound_settings = SoundSettingsSection(
            settings,
            sound_preview or (lambda *_args: None),
            self,
        )
        layout.addSpacing(2)
        layout.addWidget(sound_title)
        layout.addWidget(self.sound_settings)
        layout.addSpacing(6)
        layout.addLayout(self._dialog_buttons(self._save))
        self.adjustSize()

    def _sync_continuous_display_button(self, checked: bool) -> None:
        self.continuous_alert_display.setText(
            "开启" if checked else "关闭"
        )

    @staticmethod
    def _dialog_buttons(save_slot: Any) -> QHBoxLayout:
        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel_button = QPushButton("取消")
        save_button = QPushButton("保存")
        save_button.setObjectName("primaryButton")
        buttons.addWidget(cancel_button)
        buttons.addWidget(save_button)
        save_button.clicked.connect(save_slot)
        dialog = save_slot.__self__
        cancel_button.clicked.connect(dialog.reject)
        return buttons

    def _save(self) -> None:
        if not self.sound_settings.save():
            return
        self._settings.setValue(
            "alert/display_mode",
            normalize_alert_display_mode(
                self.alert_display_mode.currentData()
            ),
        )
        self._settings.setValue(
            "alert/confirm_seconds", self.confirm_seconds.currentData()
        )
        self._settings.setValue(
            "alert/auto_exit_seconds",
            self.auto_exit_seconds.currentData(),
        )
        self._settings.setValue(
            "alert/continuous_display",
            self.continuous_alert_display.isChecked(),
        )
        self._settings.remove("popup/auto_close_seconds")
        self._settings.sync()
        self.settings_changed.emit()
        self.accept()


class SoundSettingsSection(QFrame):
    """嵌入告警设置中的提示音配置区域。"""

    def __init__(
        self,
        settings: QSettings,
        sound_preview: Any,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._sound_preview = sound_preview
        self.setObjectName("dialogCard")

        self.sound_mode = DownwardComboBox()
        self.sound_mode.setMinimumWidth(240)
        self.sound_mode.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        self.sound_mode.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents
        )
        self.sound_mode.addItem("电脑默认提示音", "default")
        self.sound_mode.addItem("自定义声音文件", "custom")
        self.sound_mode.addItem("关闭提示音", "off")
        mode_index = self.sound_mode.findData(
            str(settings.value("sound/mode", "default"))
        )
        self.sound_mode.setCurrentIndex(max(mode_index, 0))
        self.sound_mode.currentIndexChanged.connect(self._sync_controls)

        self.sound_path = QLineEdit(
            str(settings.value("sound/custom_path", ""))
        )
        self.sound_path.setPlaceholderText("请选择本地声音文件")
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
        self.volume_value = QLabel(f"{self.volume.value()}%")
        self.volume_value.setMinimumWidth(38)
        self.volume_value.setAlignment(
            Qt.AlignmentFlag.AlignRight
            | Qt.AlignmentFlag.AlignVCenter
        )
        volume_row = QWidget()
        volume_layout = QHBoxLayout(volume_row)
        volume_layout.setContentsMargins(0, 0, 0, 0)
        volume_layout.setSpacing(8)
        volume_layout.addWidget(self.volume, 1)
        volume_layout.addWidget(self.volume_value)
        self.volume.valueChanged.connect(
            lambda value: self.volume_value.setText(f"{value}%")
        )

        self._test_button = QPushButton("试听提示音")
        self._test_button.clicked.connect(self._preview_current_sound)

        form = QFormLayout(self)
        form.setContentsMargins(12, 10, 12, 10)
        form.setSpacing(8)
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        form.addRow("提示音", self.sound_mode)
        form.addRow("自定义文件", sound_file_row)
        form.addRow("自定义音量", volume_row)
        form.addRow("", self._test_button)

        self.sound_path.textChanged.connect(self._sync_controls)

        self._sync_controls()

    def _sync_controls(self, *_args: Any) -> None:
        mode = str(self.sound_mode.currentData())
        custom = mode == "custom"
        sound_off = mode == "off"
        valid_custom_file = Path(
            self.sound_path.text().strip()
        ).is_file()
        self.sound_path.setEnabled(custom)
        self._browse_button.setEnabled(custom)
        self.volume.setEnabled(custom)
        self.volume_value.setEnabled(custom)
        self._test_button.setEnabled(
            not sound_off and (not custom or valid_custom_file)
        )

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
        if self.sound_mode.currentData() == "off":
            return
        self._sound_preview(
            str(self.sound_mode.currentData()),
            self.sound_path.text().strip(),
            self.volume.value(),
        )

    def save(self) -> bool:
        path = self.sound_path.text().strip()
        if (
            self.sound_mode.currentData() == "custom"
            and (not path or not Path(path).is_file())
        ):
            QMessageBox.warning(
                self,
                "声音文件不可用",
                "请先选择一个存在的声音文件。",
            )
            return False
        self._settings.setValue("sound/mode", self.sound_mode.currentData())
        self._settings.setValue("sound/custom_path", path)
        self._settings.setValue("sound/volume", self.volume.value())
        self._settings.sync()
        if self.sound_mode.currentData() == "off":
            self._sound_preview("off", "", self.volume.value())
        return True


class OtherSettingsDialog(QDialog):
    """为后续扩展预留的其他配置菜单。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("其他配置")
        self.setMinimumWidth(420)

        title_label = QLabel("其他配置")
        title_label.setObjectName("dialogSectionTitle")
        description_label = QLabel("该区域已预留，后续配置将在这里提供。")
        description_label.setObjectName("dialogDescription")

        empty_card = QFrame()
        empty_card.setObjectName("dialogCard")
        empty_layout = QVBoxLayout(empty_card)
        empty_layout.setContentsMargins(18, 28, 18, 28)
        empty_label = QLabel("暂无可配置项")
        empty_label.setObjectName("dialogDescription")
        empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(empty_label)

        close_button = QPushButton("关闭")
        close_button.setObjectName("primaryButton")
        close_button.clicked.connect(self.accept)
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        button_layout.addWidget(close_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 18)
        layout.setSpacing(10)
        layout.addWidget(title_label)
        layout.addWidget(description_label)
        layout.addWidget(empty_card)
        layout.addSpacing(8)
        layout.addLayout(button_layout)


class CloseActionDialog(QDialog):
    """使用 Client 的控件风格呈现关闭窗口操作。"""

    def __init__(
        self,
        parent: QWidget,
        dark_mode: bool,
        icon: QIcon,
    ) -> None:
        super().__init__(parent)
        self.selected_action = "cancel"
        self.setObjectName("closeActionDialog")
        self.setWindowTitle(f"关闭 {APP_NAME}")
        if not icon.isNull():
            self.setWindowIcon(icon)
        self.setModal(True)
        self.setWindowFlag(
            Qt.WindowType.WindowContextHelpButtonHint,
            False,
        )
        self.setMinimumWidth(380)

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(10, 8, 10, 8)
        root_layout.setSpacing(7)

        header_layout = QHBoxLayout()
        header_layout.setSpacing(8)
        icon_label = QLabel()
        icon_label.setObjectName("closeDialogIcon")
        icon_label.setFixedSize(60, 60)
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if not icon.isNull():
            # QIcon.pixmap(width, height) 在 Retina 屏上返回 DPR=2 的位图，
            # 直接设置后只会显示为一半的逻辑尺寸。显式传入设备像素比，
            # 确保普通屏和高分屏都呈现为完整的 56×56。
            icon_pixmap = icon.pixmap(
                QSize(56, 56),
                max(self.devicePixelRatioF(), 1.0),
            )
            icon_label.setPixmap(icon_pixmap)
        else:
            icon_label.hide()
        header_layout.addWidget(
            icon_label,
            0,
            Qt.AlignmentFlag.AlignTop,
        )

        text_layout = QVBoxLayout()
        text_layout.setSpacing(2)
        title_label = QLabel(f"关闭 {APP_NAME}")
        title_label.setObjectName("closeDialogTitle")
        subtitle_label = QLabel("请选择关闭窗口后的操作")
        subtitle_label.setObjectName("closeDialogSubtitle")
        hint_label = QLabel(
            "后台静默运行会隐藏主窗口；已启用告警时才显示报警小窗。"
        )
        hint_label.setObjectName("closeDialogHint")
        hint_label.setWordWrap(True)
        text_layout.addWidget(title_label)
        text_layout.addWidget(subtitle_label)
        text_layout.addWidget(hint_label)
        text_layout.addStretch()
        header_layout.addLayout(text_layout, 1)
        root_layout.addLayout(header_layout)

        separator = QFrame()
        separator.setObjectName("closeDialogSeparator")
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setFrameShadow(QFrame.Shadow.Plain)
        root_layout.addWidget(separator)

        button_layout = QHBoxLayout()
        button_layout.setSpacing(6)
        background_button = QPushButton("后台静默运行")
        background_button.setObjectName("closeBackgroundButton")
        exit_button = QPushButton("退出应用程序")
        exit_button.setObjectName("closeExitButton")
        cancel_button = QPushButton("取消")
        cancel_button.setObjectName("closeCancelButton")
        for button in (
            background_button,
            exit_button,
            cancel_button,
        ):
            button.setMinimumHeight(31)
            button.setSizePolicy(
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Fixed,
            )
            button_layout.addWidget(button, 1)
        background_button.setDefault(True)
        background_button.clicked.connect(
            lambda: self._choose("background")
        )
        exit_button.clicked.connect(lambda: self._choose("exit"))
        cancel_button.clicked.connect(self.reject)
        root_layout.addLayout(button_layout)

        if dark_mode:
            colors = {
                "background": "#171a21",
                "text": "#e8eaf0",
                "muted": "#aab2bf",
                "border": "#303640",
                "secondary": "#272c35",
                "secondary_hover": "#343a45",
            }
        else:
            colors = {
                "background": "#ffffff",
                "text": "#20252d",
                "muted": "#667080",
                "border": "#d5dae1",
                "secondary": "#f4f6f8",
                "secondary_hover": "#e9edf2",
            }
        self.setStyleSheet(
            f"""
            QDialog#closeActionDialog {{
                color: {colors["text"]};
                background: {colors["background"]};
            }}
            QLabel#closeDialogTitle {{
                color: {colors["text"]};
                font-size: 15px;
                font-weight: 700;
            }}
            QLabel#closeDialogSubtitle {{
                color: {colors["text"]};
                font-size: 12px;
                font-weight: 600;
            }}
            QLabel#closeDialogHint {{
                color: {colors["muted"]};
                font-size: 10px;
            }}
            QFrame#closeDialogSeparator {{
                color: {colors["border"]};
                background: {colors["border"]};
                border: none;
                max-height: 1px;
            }}
            QPushButton {{
                min-height: 31px;
                padding: 0 4px;
                border: 1px solid {colors["border"]};
                border-radius: 5px;
                font-size: 12px;
                font-weight: 700;
            }}
            QPushButton#closeBackgroundButton {{
                color: #ffffff;
                background: #2563eb;
                border-color: #2563eb;
            }}
            QPushButton#closeBackgroundButton:hover {{
                background: #1d4ed8;
                border-color: #1d4ed8;
            }}
            QPushButton#closeExitButton {{
                color: #ffffff;
                background: #dc3545;
                border-color: #dc3545;
            }}
            QPushButton#closeExitButton:hover {{
                background: #bd2534;
                border-color: #bd2534;
            }}
            QPushButton#closeCancelButton {{
                color: {colors["text"]};
                background: {colors["secondary"]};
            }}
            QPushButton#closeCancelButton:hover {{
                background: {colors["secondary_hover"]};
            }}
            QPushButton:focus {{
                border: 2px solid #60a5fa;
            }}
            """
        )

    def _choose(self, action: str) -> None:
        self.selected_action = action
        self.accept()


class ScaledPixmapLabel(QLabel):
    """保持宽高比显示预览画面的标签。"""

    def __init__(self, placeholder: str) -> None:
        super().__init__(placeholder)
        self._source_pixmap: QPixmap | None = None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(320, 240)

    def set_image_data(self, image_data: bytes) -> bool:
        pixmap = QPixmap()
        if not pixmap.loadFromData(image_data):
            return False
        self._source_pixmap = pixmap
        self._update_scaled_pixmap()
        return True

    def clear_image(self, placeholder: str) -> None:
        self._source_pixmap = None
        self.setPixmap(QPixmap())
        self.setText(placeholder)

    def has_image(self) -> bool:
        return self._source_pixmap is not None

    def _update_scaled_pixmap(self) -> None:
        if self._source_pixmap is None:
            return
        self.setText("")
        self.setPixmap(
            self._source_pixmap.scaled(
                self.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._update_scaled_pixmap()


class NativeDashboard(QWidget):
    """完全由 Desktop 绘制并直接消费 Client API 的监控面板。"""

    alert_settings_requested = Signal()
    popup_settings_requested = Signal()
    other_settings_requested = Signal()
    alert_enabled_changed = Signal(bool)
    continuous_monitoring_changed = Signal(bool)

    def __init__(self, settings: QSettings) -> None:
        super().__init__()
        self._settings = settings
        self._server_url = ""
        self._view_active = False
        self._preview_request_active = False
        self._preview_sequence = 0
        self._network = QNetworkAccessManager(self)
        self._preview_timer = QTimer(self)
        self._preview_timer.setInterval(180)
        self._preview_timer.timeout.connect(self._request_preview)

        self.setObjectName("nativeDashboard")

        topbar = QFrame()
        topbar.setObjectName("dashboardCard")
        top_layout = QGridLayout(topbar)
        top_layout.setContentsMargins(16, 12, 16, 12)
        top_layout.setHorizontalSpacing(10)
        top_layout.setVerticalSpacing(10)

        self.alert_enabled_button = self._make_toggle_button("启用告警")
        self.continuous_button = self._make_toggle_button("连续监测")
        self.preview_button = self._make_toggle_button("实时预览")
        self.alert_settings_button = QPushButton("告警设置")
        self.popup_settings_button = QPushButton("弹窗位置")
        self.other_settings_button = QPushButton("其他配置")

        controls = QWidget()
        controls_layout = QHBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(8)
        controls_layout.addWidget(self.alert_enabled_button)
        controls_layout.addWidget(self.continuous_button)
        controls_layout.addWidget(self.preview_button)
        controls_layout.addWidget(self.popup_settings_button)
        controls_layout.addWidget(self.alert_settings_button)
        controls_layout.addWidget(self.other_settings_button)
        controls_layout.addStretch()
        top_layout.addWidget(controls, 0, 0)

        monitor = QFrame()
        monitor.setObjectName("monitorCard")
        monitor_layout = QVBoxLayout(monitor)
        monitor_layout.setContentsMargins(0, 0, 0, 0)

        self.monitor_stack = QStackedWidget()
        idle_panel = QWidget()
        idle_layout = QVBoxLayout(idle_panel)
        idle_layout.setContentsMargins(20, 20, 20, 20)
        idle_layout.addStretch()
        self.status_icon = QLabel("⌁")
        self.status_icon.setObjectName("monitorIcon")
        self.status_icon.setFixedSize(76, 76)
        self.status_icon.setProperty("state", "idle")
        self.status_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_title = QLabel("等待连接")
        self.status_title.setObjectName("monitorTitle")
        self.status_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_detail = QLabel("请先连接 AlertZone Client")
        self.status_detail.setObjectName("monitorDetail")
        self.status_detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_detail.setWordWrap(True)
        source_label = QLabel("数据由 AlertZone Client API 提供")
        source_label.setObjectName("dashboardSource")
        source_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        idle_layout.addWidget(
            self.status_icon,
            0,
            Qt.AlignmentFlag.AlignHCenter,
        )
        idle_layout.addWidget(self.status_title)
        idle_layout.addWidget(self.status_detail)
        idle_layout.addSpacing(6)
        idle_layout.addWidget(source_label)
        idle_layout.addStretch()

        self.preview_label = ScaledPixmapLabel("正在等待实时预览…")
        self.preview_label.setObjectName("previewLabel")
        self.monitor_stack.addWidget(idle_panel)
        self.monitor_stack.addWidget(self.preview_label)
        monitor_layout.addWidget(self.monitor_stack)

        stats = QFrame()
        stats.setObjectName("dashboardCard")
        stats_layout = QGridLayout(stats)
        stats_layout.setContentsMargins(18, 11, 18, 11)
        stats_layout.setHorizontalSpacing(12)
        self.people_value = self._make_stat(stats_layout, 0, "人数", "0")
        self.presence_value = self._make_stat(stats_layout, 1, "持续", "—")
        self.fps_value = self._make_stat(stats_layout, 2, "FPS", "—")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 10)
        layout.setSpacing(10)
        layout.addWidget(topbar)
        layout.addWidget(monitor, 1)
        layout.addWidget(stats)

        self.alert_enabled_button.toggled.connect(
            self._on_alert_enabled_toggled
        )
        self.continuous_button.toggled.connect(
            self._on_continuous_toggled
        )
        self.preview_button.toggled.connect(self._on_preview_toggled)
        self.alert_settings_button.clicked.connect(
            self.alert_settings_requested.emit
        )
        self.popup_settings_button.clicked.connect(
            self.popup_settings_requested.emit
        )
        self.other_settings_button.clicked.connect(
            self.other_settings_requested.emit
        )
        self.sync_controls()

    @staticmethod
    def _make_toggle_button(text: str) -> QPushButton:
        button = QPushButton(text)
        button.setCheckable(True)
        button.setObjectName("toggleButton")
        return button

    @staticmethod
    def _make_stat(
        layout: QGridLayout, column: int, label: str, value: str
    ) -> QLabel:
        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        row.addStretch()
        name_label = QLabel(f"{label}：")
        name_label.setObjectName("statName")
        value_label = QLabel(value)
        value_label.setObjectName("statValue")
        row.addWidget(name_label)
        row.addWidget(value_label)
        row.addStretch()
        layout.addWidget(container, 0, column)
        layout.setColumnStretch(column, 1)
        return value_label

    def sync_controls(self) -> None:
        alert_enabled = setting_bool(
            self._settings,
            "alert/enabled",
            False,
        )
        self.alert_enabled_button.blockSignals(True)
        self.alert_enabled_button.setChecked(alert_enabled)
        self.alert_enabled_button.blockSignals(False)
        checked = setting_bool(
            self._settings,
            "dashboard/preview_enabled",
            False,
        )
        self.preview_button.blockSignals(True)
        self.preview_button.setChecked(checked)
        self.preview_button.blockSignals(False)
        continuous = setting_bool(
            self._settings,
            "alert/continuous",
            False,
        )
        self.continuous_button.blockSignals(True)
        self.continuous_button.setChecked(continuous)
        self.continuous_button.blockSignals(False)
        self._update_preview_timer()

    def set_server_url(self, server_url: str) -> None:
        if server_url != self._server_url:
            self.preview_label.clear_image("正在等待实时预览…")
        self._server_url = server_url
        self._update_preview_timer()
        if server_url and self.preview_button.isChecked():
            self._request_preview()

    def set_view_active(self, active: bool) -> None:
        self._view_active = active
        self._update_preview_timer()
        if active and self.preview_button.isChecked():
            self._request_preview()

    def request_preview_now(self) -> None:
        if self.preview_button.isChecked():
            self._request_preview()

    def set_connection(self, online: bool, detail: str = "") -> None:
        if not online:
            self._set_monitor_state("idle", "⌁")
            self.status_title.setText("连接中断")
            self.status_detail.setText(detail or "正在尝试重新连接 Client")
            if self.preview_button.isChecked():
                self.preview_label.clear_image("连接中断，正在重试…")

    def update_status(self, payload: dict) -> None:
        running = bool(payload.get("detection_running", False))
        present = bool(payload.get("person_present", False))
        people = max(int(payload.get("people_count", 0)), 0)
        fps = max(float(payload.get("fps", 0.0)), 0.0)
        presence = max(float(payload.get("presence_seconds", 0.0)), 0.0)

        self.people_value.setText(str(people))
        self.presence_value.setText(
            self._format_duration(presence) if present else "—"
        )
        self.fps_value.setText(f"{fps:.1f}" if running else "—")

        if present:
            self._set_monitor_state("person", "!")
            self.status_title.setText("检测到人物")
            self.status_detail.setText(
                f"当前区域内有 {people} 人，已持续 "
                f"{self._format_duration(presence)}"
            )
        elif running:
            self._set_monitor_state("detecting", "✓")
            self.status_title.setText("检测运行中")
            self.status_detail.setText("当前未检测到人物，正在持续监测")
        else:
            self._set_monitor_state("idle", "⌁")
            self.status_title.setText("检测未启动")
            self.status_detail.setText(
                str(payload.get("status") or "请在 Client 中开始检测")
            )

    @staticmethod
    def _format_duration(seconds: float) -> str:
        total_seconds = max(int(seconds), 0)
        minutes, seconds_value = divmod(total_seconds, 60)
        hours, minutes_value = divmod(minutes, 60)
        if hours:
            return f"{hours}:{minutes_value:02d}:{seconds_value:02d}"
        return f"{minutes_value:02d}:{seconds_value:02d}"

    def _on_preview_toggled(self, checked: bool) -> None:
        self._settings.setValue("dashboard/preview_enabled", checked)
        self.monitor_stack.setCurrentIndex(1 if checked else 0)
        if not checked:
            self.preview_label.clear_image("实时预览已关闭")
        self._update_preview_timer()
        if checked:
            self._request_preview()

    def _on_alert_enabled_toggled(self, checked: bool) -> None:
        self._settings.setValue("alert/enabled", checked)
        self._settings.sync()
        self.alert_enabled_changed.emit(checked)

    def _on_continuous_toggled(self, checked: bool) -> None:
        self._settings.setValue("alert/continuous", checked)
        self._settings.sync()
        self.continuous_monitoring_changed.emit(checked)

    def _set_monitor_state(self, state: str, icon: str) -> None:
        self.status_icon.setText(icon)
        for widget in (self.status_icon, self.status_title):
            widget.setProperty("state", state)
            widget.style().unpolish(widget)
            widget.style().polish(widget)

    def _update_preview_timer(self) -> None:
        should_run = (
            self._view_active
            and bool(self._server_url)
            and self.preview_button.isChecked()
        )
        self.monitor_stack.setCurrentIndex(
            1 if self.preview_button.isChecked() else 0
        )
        if should_run:
            self._preview_timer.start()
        else:
            self._preview_timer.stop()

    def _request_preview(self) -> None:
        if (
            not self._view_active
            or not self._server_url
            or not self.preview_button.isChecked()
            or self._preview_request_active
        ):
            return
        self._preview_sequence += 1
        request = QNetworkRequest(
            QUrl(
                f"{self._server_url}/api/preview.jpg"
                f"?desktop={self._preview_sequence}"
            )
        )
        request.setTransferTimeout(REQUEST_TIMEOUT_MS)
        request.setAttribute(
            QNetworkRequest.Attribute.CacheLoadControlAttribute,
            QNetworkRequest.CacheLoadControl.AlwaysNetwork,
        )
        self._preview_request_active = True
        reply = self._network.get(request)
        reply.finished.connect(lambda: self._handle_preview(reply))

    def _handle_preview(self, reply: QNetworkReply) -> None:
        self._preview_request_active = False
        try:
            if reply.error() == QNetworkReply.NetworkError.NoError:
                if not self.preview_label.set_image_data(bytes(reply.readAll())):
                    self.preview_label.clear_image("预览图片格式无效")
                return
            status_code = reply.attribute(
                QNetworkRequest.Attribute.HttpStatusCodeAttribute
            )
            if status_code == 503 and not self.preview_label.has_image():
                self.preview_label.clear_image("等待 Client 生成预览画面…")
            elif not self.preview_label.has_image():
                self.preview_label.clear_image(
                    f"实时预览暂不可用\n{reply.errorString()}"
                )
        finally:
            reply.deleteLater()


class StatusMonitor(QObject):
    """使用 Qt 网络栈异步轮询状态，主窗口隐藏时仍保持工作。"""

    connection_changed = Signal(bool, str)
    status_received = Signal(dict)
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

    def poll_now(self) -> None:
        self._poll()

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
                if setting_bool(self._settings, "alert/enabled", False):
                    self.intrusion_detected.emit(payload)
            self.status_received.emit(payload)
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
        self._alert_settings_dialog: AlertSettingsDialog | None = None
        self._other_settings_dialog: OtherSettingsDialog | None = None
        self._alert_preview_reply: QNetworkReply | None = None
        self._alert_preview_sequence = 0
        self._latest_person_present = False
        self._background_mode = False
        # 每次启动默认跟随系统；主页按钮只在本次运行中切换浅色/深色。
        self._theme_mode = "follow-system"
        self._settings.remove("appearance/theme_mode")
        self._current_theme = ""

        self.setWindowTitle(APP_NAME)
        icon_path = app_icon_path()
        self._icon = QIcon(str(icon_path)) if icon_path else QIcon()
        if not self._icon.isNull():
            self.setWindowIcon(self._icon)

        self._network = QNetworkAccessManager(self)
        self._alert_preview_timer = QTimer(self)
        self._alert_preview_timer.setInterval(180)
        self._alert_preview_timer.timeout.connect(
            self._request_alert_live_preview
        )
        self._alert_exit_timer = QTimer(self)
        self._alert_exit_timer.setSingleShot(True)
        self._alert_exit_timer.timeout.connect(
            self._auto_exit_current_alert
        )
        self._audio_output: Any | None = None
        self._player: Any | None = None
        if QAudioOutput is not None and QMediaPlayer is not None:
            self._audio_output = QAudioOutput(self)
            self._player = QMediaPlayer(self)
            self._player.setAudioOutput(self._audio_output)

        self._popup = AlertPopup(self._settings)
        if not self._icon.isNull():
            self._popup.setWindowIcon(self._icon)
        self._popup.dismissed.connect(self._on_popup_dismissed)

        self._monitor = StatusMonitor(self._settings, self)
        self._monitor.intrusion_detected.connect(self._show_intrusion)
        self._monitor.status_received.connect(self._dashboard_status_received)
        self._monitor.connection_changed.connect(self._on_monitor_connection_changed)

        self._connection_page = ConnectionPage(self._settings)
        self._connection_page.connect_requested.connect(self.connect_to_server)
        self._dashboard = NativeDashboard(self._settings)
        self._dashboard.alert_settings_requested.connect(
            self.open_alert_settings
        )
        self._dashboard.popup_settings_requested.connect(
            self._popup.show_placement_preview
        )
        self._dashboard.other_settings_requested.connect(
            self.open_other_settings
        )
        self._dashboard.alert_enabled_changed.connect(
            self._on_alert_enabled_changed
        )
        self._dashboard.continuous_monitoring_changed.connect(
            self._on_continuous_monitoring_changed
        )

        self._stack = QStackedWidget()
        self._stack.addWidget(self._connection_page)
        self._stack.addWidget(self._dashboard)
        self._build_topbar()
        shell = QWidget()
        shell.setObjectName("mainShell")
        shell_layout = QVBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)
        shell_layout.addWidget(self._topbar)
        shell_layout.addWidget(self._stack, 1)
        self.setCentralWidget(shell)
        self._build_tray()
        self._apply_theme(self._resolved_theme())
        self._resize_to_button_fit_default()
        self._on_alert_display_mode_changed(
            normalize_alert_display_mode(
                self._settings.value("alert/display_mode", "zoom")
            )
        )
        style_hints = QApplication.styleHints()
        if hasattr(style_hints, "colorSchemeChanged"):
            style_hints.colorSchemeChanged.connect(
                self._on_system_theme_changed
            )

        self._settings.remove("window/geometry")

        saved_url = str(self._settings.value("connection/server_url", "")).strip()
        if saved_url and setting_bool(self._settings, "connection/auto_connect", True):
            QTimer.singleShot(0, lambda: self.connect_to_server(saved_url))

    def _build_topbar(self) -> None:
        """使用普通按钮构建单行顶栏，避开 macOS 原生 QToolButton 崩溃。"""
        topbar = QFrame()
        topbar.setObjectName("mainTopbar")
        layout = QHBoxLayout(topbar)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(4)

        refresh_button = QPushButton("刷新状态")
        refresh_button.setObjectName("topbarTextButton")
        refresh_button.clicked.connect(self._refresh_dashboard)
        layout.addWidget(refresh_button)

        layout.addWidget(self._topbar_separator())
        monitor_label = QLabel("Client连接状态：")
        monitor_label.setObjectName("toolbarTitle")
        layout.addWidget(monitor_label)
        self._address_label = QLabel("尚未连接")
        self._address_label.setObjectName("toolbarAddress")
        self._address_label.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Preferred,
        )
        self._address_label.setMinimumWidth(92)
        layout.addWidget(self._address_label)

        layout.addStretch()
        self._theme_button = QPushButton("深色主题")
        self._theme_button.setObjectName("topbarTextButton")
        self._theme_button.clicked.connect(self._toggle_theme)
        layout.addWidget(self._theme_button)
        change_button = QPushButton("更换地址")
        change_button.setObjectName("topbarTextButton")
        change_button.clicked.connect(self.show_connection_page)
        layout.addWidget(change_button)
        tray_button = QPushButton("后台运行")
        tray_button.setObjectName("topbarTextButton")
        tray_button.clicked.connect(self.hide_to_tray)
        layout.addWidget(tray_button)

        topbar.setVisible(False)
        self._topbar = topbar

    @staticmethod
    def _topbar_separator() -> QFrame:
        separator = QFrame()
        separator.setObjectName("topbarSeparator")
        separator.setFrameShape(QFrame.Shape.VLine)
        separator.setFrameShadow(QFrame.Shadow.Plain)
        return separator

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
        alert_settings_action = menu.addAction("告警设置")
        alert_settings_action.triggered.connect(self.open_alert_settings)
        other_settings_action = menu.addAction("其他配置")
        other_settings_action.triggered.connect(self.open_other_settings)
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
        self._dashboard.set_server_url(server_url)
        self._dashboard.set_connection(True, server_url)
        self._dashboard.update_status(payload)
        self._monitor.set_server_url(server_url)
        if self._alert_popup_allowed():
            QTimer.singleShot(0, self._monitor.request_rearm)
        self._address_label.setText("● 已连接")
        self._address_label.setToolTip(server_url)
        self._address_label.setProperty("online", True)
        self._address_label.style().unpolish(self._address_label)
        self._address_label.style().polish(self._address_label)
        if self._tray is not None:
            self._tray_status_action.setText(f"已连接：{server_url}")
            self._tray.setToolTip(f"{APP_NAME}\n{server_url}")
        self._stack.setCurrentWidget(self._dashboard)
        self._dashboard.set_view_active(self.isVisible())
        self._topbar.setVisible(True)

    def show_connection_page(self) -> None:
        self._dashboard.set_view_active(False)
        self._stack.setCurrentWidget(self._connection_page)
        self._topbar.setVisible(False)
        self._connection_page.address_edit.setFocus()
        self._connection_page.address_edit.selectAll()

    def open_alert_settings(self) -> None:
        if self._alert_settings_dialog is not None:
            self._raise_dialog(self._alert_settings_dialog)
            return
        dialog = AlertSettingsDialog(
            self._settings,
            self if self.isVisible() else None,
            self._play_sound,
        )
        self._prepare_dialog(dialog)
        dialog.settings_changed.connect(
            self._on_alert_settings_changed
        )
        dialog.destroyed.connect(
            lambda _object=None: setattr(
                self, "_alert_settings_dialog", None
            )
        )
        self._alert_settings_dialog = dialog
        self._raise_dialog(dialog)

    def open_other_settings(self) -> None:
        if self._other_settings_dialog is not None:
            self._raise_dialog(self._other_settings_dialog)
            return
        dialog = OtherSettingsDialog(
            self if self.isVisible() else None,
        )
        self._prepare_dialog(dialog)
        dialog.destroyed.connect(
            lambda _object=None: setattr(
                self, "_other_settings_dialog", None
            )
        )
        self._other_settings_dialog = dialog
        self._raise_dialog(dialog)

    def _prepare_dialog(self, dialog: QDialog) -> None:
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        if not self._icon.isNull():
            dialog.setWindowIcon(self._icon)

    @staticmethod
    def _raise_dialog(dialog: QDialog) -> None:
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _on_alert_settings_changed(self) -> None:
        self._on_alert_display_mode_changed(
            normalize_alert_display_mode(
                self._settings.value("alert/display_mode", "zoom")
            )
        )
        self._sync_alert_exit_timer(restart=True)
        if self._server_url and self._alert_popup_allowed():
            self._monitor.request_rearm()

    def _on_alert_enabled_changed(self, enabled: bool) -> None:
        if enabled:
            if self._server_url and self._alert_popup_allowed():
                self._monitor.request_rearm()
            return
        self._alert_exit_timer.stop()
        self._popup.hide()
        self._stop_alert_live_preview()
        self._stop_sound()

    def _on_continuous_monitoring_changed(self, enabled: bool) -> None:
        """连续监测开启后，让报警小窗从当前时刻开始重新布防。"""
        if (
            enabled
            and self._server_url
            and self._alert_popup_allowed()
            and not self._popup.is_alert_active()
        ):
            self._monitor.request_rearm()

    def _on_alert_display_mode_changed(self, mode: str) -> None:
        mode = normalize_alert_display_mode(mode)
        self._settings.setValue("alert/display_mode", mode)
        self._settings.sync()
        self._popup.set_display_mode(mode)
        if not self._popup.is_alert_active():
            return
        if mode in ALERT_LIVE_MODES:
            self._start_alert_live_preview()
        else:
            self._stop_alert_live_preview()
            if mode in {"zoom", "zoom-red"}:
                self._request_intrusion_image(self._current_alert_event)

    def _dashboard_status_received(self, payload: dict) -> None:
        self._latest_person_present = bool(
            payload.get("person_present", False)
        )
        self._dashboard.update_status(payload)
        if setting_bool(
            self._settings,
            "alert/continuous_display",
            False,
        ):
            self._sync_alert_exit_timer()

    def _refresh_dashboard(self) -> None:
        self._monitor.poll_now()
        self._dashboard.request_preview_now()

    def _show_intrusion(self, data: dict) -> None:
        if not self._alert_popup_allowed():
            return
        self._alert_exit_timer.stop()
        self._latest_person_present = bool(
            data.get("person_present", True)
        )
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
        mode = self._popup.display_mode()
        if mode in ALERT_LIVE_MODES:
            self._start_alert_live_preview()
        elif (
            mode in {"zoom", "zoom-red"}
            and data.get("intrusion_image_available")
        ):
            self._request_intrusion_image(event_id)

        self._sync_alert_exit_timer(restart=True)

    def _request_intrusion_image(self, event_id: int) -> None:
        if event_id <= 0 or not self._server_url:
            return
        request = QNetworkRequest(
            QUrl(f"{self._server_url}/api/intruder.jpg?event={event_id}")
        )
        request.setTransferTimeout(REQUEST_TIMEOUT_MS)
        reply = self._network.get(request)
        reply.finished.connect(
            lambda: self._finish_alert_image(reply, event_id)
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

    def _start_alert_live_preview(self) -> None:
        if not self._server_url or not self._popup.is_alert_active():
            return
        self._alert_preview_timer.start()
        self._request_alert_live_preview()

    def _stop_alert_live_preview(self) -> None:
        self._alert_preview_timer.stop()
        if self._alert_preview_reply is not None:
            self._alert_preview_reply.abort()
            self._alert_preview_reply.deleteLater()
            self._alert_preview_reply = None

    def _request_alert_live_preview(self) -> None:
        if (
            not self._server_url
            or not self._popup.is_alert_active()
            or self._popup.display_mode() not in ALERT_LIVE_MODES
        ):
            self._stop_alert_live_preview()
            return
        if self._alert_preview_reply is not None:
            return
        self._alert_preview_sequence += 1
        request = QNetworkRequest(
            QUrl(
                f"{self._server_url}/api/preview.jpg"
                f"?desktop_alert={self._alert_preview_sequence}"
            )
        )
        request.setTransferTimeout(REQUEST_TIMEOUT_MS)
        reply = self._network.get(request)
        self._alert_preview_reply = reply
        reply.finished.connect(
            lambda: self._finish_alert_live_preview(reply)
        )

    def _finish_alert_live_preview(self, reply: QNetworkReply) -> None:
        if reply is not self._alert_preview_reply:
            reply.deleteLater()
            return
        self._alert_preview_reply = None
        try:
            if (
                reply.error() == QNetworkReply.NetworkError.NoError
                and self._popup.is_alert_active()
                and self._popup.display_mode() in ALERT_LIVE_MODES
            ):
                self._popup.set_event_image(bytes(reply.readAll()))
        finally:
            reply.deleteLater()

    def _alert_auto_exit_seconds(self) -> int:
        return int(
            self._settings.value(
                "alert/auto_exit_seconds",
                self._settings.value("popup/auto_close_seconds", 10),
            )
        )

    def _sync_alert_exit_timer(self, restart: bool = False) -> None:
        if not self._popup.is_alert_active():
            self._alert_exit_timer.stop()
            return
        continuous_display = setting_bool(
            self._settings,
            "alert/continuous_display",
            False,
        )
        if continuous_display and self._latest_person_present:
            self._alert_exit_timer.stop()
            return
        if self._alert_exit_timer.isActive() and not restart:
            return
        self._alert_exit_timer.stop()
        auto_exit = self._alert_auto_exit_seconds()
        if auto_exit > 0:
            self._alert_exit_timer.start(auto_exit * 1000)

    def _auto_exit_current_alert(self) -> None:
        if self._popup.is_alert_active():
            self._popup.hide()
            self._on_popup_dismissed()

    def _on_popup_dismissed(self) -> None:
        self._alert_exit_timer.stop()
        self._stop_alert_live_preview()
        self._stop_sound()
        if (
            setting_bool(self._settings, "alert/continuous", False)
            and self._alert_popup_allowed()
        ):
            self._monitor.request_rearm()

    def _play_configured_sound(self) -> None:
        self._play_sound(
            str(self._settings.value("sound/mode", "default")),
            str(self._settings.value("sound/custom_path", "")),
            int(self._settings.value("sound/volume", 80)),
        )

    def _play_sound(self, mode: str, path: str, volume: int) -> None:
        self._stop_sound()
        if mode == "off":
            return
        if (
            mode == "custom"
            and path
            and Path(path).is_file()
            and self._audio_output is not None
            and self._player is not None
        ):
            self._audio_output.setVolume(max(0, min(volume, 100)) / 100.0)
            self._player.setSource(QUrl.fromLocalFile(path))
            self._player.play()
        else:
            QApplication.beep()

    def _stop_sound(self) -> None:
        if self._player is not None:
            self._player.stop()

    def _on_monitor_connection_changed(self, online: bool, detail: str) -> None:
        self._dashboard.set_connection(
            online,
            self._server_url if online else detail,
        )
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

    def _resolved_theme(self) -> str:
        if self._theme_mode in {"light", "dark"}:
            return self._theme_mode
        try:
            return (
                "dark"
                if QApplication.styleHints().colorScheme()
                == Qt.ColorScheme.Dark
                else "light"
            )
        except AttributeError:
            return str(
                self._settings.value("appearance/last_theme", "light")
            )

    def _resize_to_button_fit_default(self) -> None:
        """按两排按钮完整显示所需的最紧凑布局设置启动尺寸。"""
        self._topbar.ensurePolished()
        self._dashboard.ensurePolished()
        dashboard_hint = self._dashboard.minimumSizeHint()
        topbar_hint = self._topbar.sizeHint()
        width = max(
            self._topbar.layout().minimumSize().width(),
            dashboard_hint.width(),
        )
        self.resize(
            width,
            dashboard_hint.height() + topbar_hint.height() + 100,
        )

    def _set_explicit_theme(self, theme: str) -> None:
        self._theme_mode = "dark" if theme == "dark" else "light"
        self._apply_theme(self._theme_mode)

    def _toggle_theme(self) -> None:
        self._set_explicit_theme(
            "light" if self._current_theme == "dark" else "dark"
        )

    def _on_system_theme_changed(self, _scheme: Any) -> None:
        if self._theme_mode == "follow-system":
            self._apply_theme(self._resolved_theme())

    def hide_to_tray(self) -> None:
        if self._tray is None:
            QMessageBox.information(
                self,
                "无法后台运行",
                "当前系统未提供托盘区域，主窗口将保持打开。",
            )
            return
        self._set_background_mode(True)
        self._dashboard.set_view_active(False)
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
        self._set_background_mode(False)
        self.showNormal()
        if self._stack.currentWidget() is self._dashboard:
            self._dashboard.set_view_active(True)
        self.raise_()
        self.activateWindow()

    def _alert_popup_allowed(self) -> bool:
        return setting_bool(
            self._settings,
            "alert/enabled",
            False,
        ) and (self._background_mode or self.isMinimized())

    def _set_background_mode(self, enabled: bool) -> None:
        self._background_mode = enabled
        if enabled:
            if self._server_url and self._alert_popup_allowed():
                self._monitor.request_rearm()
        else:
            self._alert_exit_timer.stop()
            self._popup.hide()
            self._stop_alert_live_preview()
            self._stop_sound()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() != QEvent.Type.WindowStateChange:
            return
        if self.isMinimized():
            self._dashboard.set_view_active(False)
            if self._server_url and self._alert_popup_allowed():
                self._monitor.request_rearm()
            return
        if not self._background_mode:
            self._alert_exit_timer.stop()
            self._popup.hide()
            self._stop_alert_live_preview()
            self._stop_sound()
            if self._stack.currentWidget() is self._dashboard:
                self._dashboard.set_view_active(self.isVisible())

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.show_main_window()

    def quit_application(self) -> None:
        self._quitting = True
        self._settings.sync()
        self._alert_exit_timer.stop()
        self._stop_alert_live_preview()
        self._stop_sound()
        if self._tray is not None:
            self._tray.hide()
        QApplication.quit()

    def closeEvent(self, event: QCloseEvent) -> None:
        self._settings.sync()
        if self._quitting:
            event.accept()
            return

        dialog = CloseActionDialog(
            self,
            self._current_theme == "dark",
            self._icon,
        )
        dialog.exec()
        if dialog.selected_action == "background":
            if self._tray is None:
                QMessageBox.information(
                    self,
                    "无法后台运行",
                    "当前系统未提供托盘区域，主窗口将保持打开。",
                )
                event.ignore()
                return
            event.ignore()
            self.hide_to_tray()
            return
        if dialog.selected_action == "exit":
            event.accept()
            self.quit_application()
            return
        event.ignore()

    def _apply_theme(self, theme: str) -> None:
        """统一应用到主窗口、连接页、设置页、工具栏和报警小窗。"""
        theme = "dark" if theme == "dark" else "light"
        if theme == self._current_theme:
            return
        self._current_theme = theme
        self._settings.setValue("appearance/last_theme", theme)

        if theme == "dark":
            colors = {
                "page": "#0c0e12",
                "surface": "#171a21",
                "surface_alt": "#272c35",
                "input": "#20242c",
                "border": "#303640",
                "text": "#e8eaf0",
                "muted": "#aab2bf",
                "disabled": "#626a78",
                "disabled_bg": "#1b1e24",
                "disabled_border": "#282d35",
                "primary": "#16a34a",
                "primary_hover": "#15803d",
                "button_hover": "#343a45",
                "success": "#16a34a",
                "danger": "#ef6670",
                "warning": "#ffd42a",
            }
        else:
            colors = {
                "page": "#eef1f4",
                "surface": "#ffffff",
                "surface_alt": "#e8edf3",
                "input": "#ffffff",
                "border": "#d5dae1",
                "text": "#20252d",
                "muted": "#4c5563",
                "disabled": "#9aa4b2",
                "disabled_bg": "#edf0f4",
                "disabled_border": "#e0e4e9",
                "primary": "#16a34a",
                "primary_hover": "#15803d",
                "button_hover": "#dfe6ee",
                "success": "#16a34a",
                "danger": "#c0392b",
                "warning": "#b7791f",
            }

        style = f"""
            QMainWindow, QDialog, #mainShell, #connectionPage, #nativeDashboard {{
                color: {colors["text"]};
                background: {colors["page"]};
            }}
            QWidget {{
                color: {colors["text"]};
                font-size: 14px;
            }}
            QLabel, QCheckBox {{
                background: transparent;
                border: none;
            }}
            #dialogSectionTitle {{
                color: {colors["text"]};
                font-size: 19px;
                font-weight: 800;
            }}
            #dialogDescription {{
                color: {colors["muted"]};
                font-size: 13px;
            }}
            #dialogCard {{
                background: {colors["surface"]};
                border: 1px solid {colors["border"]};
                border-radius: 7px;
            }}
            #connectionCard {{
                background: {colors["surface"]};
                border: 1px solid {colors["border"]};
                border-radius: 6px;
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
            #dashboardCard {{
                background: {colors["surface"]};
                border: 1px solid {colors["border"]};
                border-radius: 6px;
            }}
            #monitorCard {{
                background: transparent;
                border: none;
                border-radius: 0;
            }}
            #monitorIcon {{
                color: {colors["muted"]};
                background: transparent;
                border: 1px solid {colors["border"]};
                border-radius: 38px;
                font-size: 34px;
                font-weight: 400;
            }}
            #monitorIcon[state="detecting"] {{
                color: #86efac;
                background: rgba(22, 163, 101, 26);
                border: 2px solid #168657;
            }}
            #monitorIcon[state="person"] {{
                color: {colors["warning"]};
                background: rgba(255, 212, 42, 26);
                border: 2px solid #d39f00;
            }}
            #monitorTitle {{
                color: {colors["text"]};
                font-size: 29px;
                font-weight: 800;
            }}
            #monitorTitle[state="person"] {{
                color: {colors["warning"]};
            }}
            #monitorDetail, #dashboardSource, #statName {{
                color: {colors["muted"]};
            }}
            #dashboardSource {{
                font-size: 12px;
            }}
            #monitorDetail {{
                font-size: 15px;
            }}
            #previewLabel {{
                color: {colors["muted"]};
                background: {colors["page"]};
                border: none;
                border-radius: 0;
                font-size: 16px;
            }}
            #statValue {{
                color: {colors["text"]};
                font-size: 16px;
                font-weight: 800;
            }}
            #toggleButton:checked {{
                color: white;
                background: #16a34a;
                border: 1px solid #0f7a3a;
                font-weight: 700;
            }}
            #toggleButton:checked:hover {{
                background: #15803d;
                border-color: #0b652f;
            }}
            #settingToggleButton {{
                min-height: 28px;
                padding: 0 12px;
                background: {colors["surface_alt"]};
                border: 1px solid {colors["border"]};
                border-radius: 14px;
                font-weight: 700;
            }}
            #settingToggleButton:checked {{
                color: white;
                background: #16a34a;
                border-color: #0f7a3a;
            }}
            #settingToggleButton:checked:hover {{
                background: #15803d;
                border-color: #0b652f;
            }}
            #mainTopbar {{
                min-height: 38px;
                color: {colors["text"]};
                background: {colors["surface"]};
                border: none;
                border-bottom: 1px solid {colors["border"]};
            }}
            #mainTopbar QPushButton {{
                min-height: 28px;
                padding: 0 9px;
                color: {colors["text"]};
                background: {colors["surface_alt"]};
                border: 1px solid {colors["border"]};
                border-radius: 5px;
            }}
            #mainTopbar QPushButton:hover {{
                background: {colors["button_hover"]};
            }}
            #mainTopbar QPushButton#topbarTextButton {{
                background: transparent;
                border: none;
                border-radius: 5px;
            }}
            #mainTopbar QPushButton#topbarTextButton:hover {{
                background: {colors["surface_alt"]};
            }}
            #topbarSeparator {{
                color: {colors["border"]};
                background: {colors["border"]};
                border: none;
                min-width: 1px;
                max-width: 1px;
            }}
            QLabel {{
                color: {colors["text"]};
            }}
            QLabel:disabled {{
                color: {colors["disabled"]};
            }}
            QCheckBox {{
                spacing: 5px;
                color: {colors["text"]};
            }}
            QLineEdit, QComboBox {{
                min-height: 28px;
                padding: 0 6px;
                color: {colors["text"]};
                selection-color: white;
                selection-background-color: {colors["primary"]};
                background: {colors["input"]};
                border: 1px solid {colors["border"]};
                border-radius: 5px;
            }}
            QLineEdit:focus, QComboBox:focus {{
                border: 1px solid #2563eb;
            }}
            QLineEdit:disabled, QComboBox:disabled {{
                color: {colors["disabled"]};
                background: {colors["disabled_bg"]};
                border-color: {colors["disabled_border"]};
            }}
            QComboBox {{
                min-height: 28px;
                padding: 0 9px;
                background: {colors["input"]};
                border: 1px solid {colors["border"]};
                border-radius: 8px;
            }}
            QComboBox:hover {{
                background: {colors["surface"]};
                border-color: {colors["muted"]};
            }}
            QComboBox:focus {{
                background: {colors["input"]};
                border-color: {colors["primary"]};
            }}
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
                padding: 5px;
                outline: 0;
                selection-color: white;
                selection-background-color: {colors["primary"]};
                background: {colors["surface"]};
                border: 1px solid {colors["border"]};
                border-radius: 8px;
            }}
            QComboBox QAbstractItemView::item {{
                min-height: 28px;
                padding: 0 8px;
                border: none;
                border-radius: 5px;
            }}
            QAbstractItemView#comboPopupView {{
                color: {colors["text"]};
                padding: 5px;
                outline: 0;
                background: {colors["surface"]};
                border: 1px solid {colors["border"]};
                border-radius: 8px;
            }}
            QAbstractItemView#comboPopupView::item {{
                min-height: 28px;
                padding: 0 8px;
                background: transparent;
                border: none;
                border-radius: 5px;
            }}
            QAbstractItemView#comboPopupView::item:hover {{
                color: {colors["text"]};
                background: {colors["surface_alt"]};
            }}
            QAbstractItemView#comboPopupView::item:selected {{
                color: white;
                background: {colors["primary"]};
            }}
            QPushButton {{
                min-height: 26px;
                padding: 0 10px;
                color: {colors["text"]};
                background: {colors["surface_alt"]};
                border: 1px solid {colors["border"]};
                border-radius: 5px;
            }}
            QPushButton:hover {{
                background: {colors["button_hover"]};
            }}
            QPushButton:disabled {{
                color: {colors["disabled"]};
                background: {colors["disabled_bg"]};
                border-color: {colors["disabled_border"]};
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
            QSlider::groove:horizontal:disabled,
            QSlider::sub-page:horizontal:disabled {{
                background: {colors["disabled_border"]};
            }}
            QSlider::handle:horizontal:disabled {{
                background: {colors["disabled_bg"]};
                border-color: {colors["disabled"]};
            }}
            #toolbarAddress {{
                color: {colors["danger"]};
                padding: 0 7px 0 2px;
                font-weight: 700;
            }}
            #toolbarTitle {{
                color: {colors["text"]};
                padding: 0 2px 0 7px;
                font-size: 14px;
                font-weight: 800;
            }}
            #toolbarAddress[online="true"] {{
                color: {colors["success"]};
            }}
        """
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(style)
        self._theme_button.setText(
            "浅色主题" if theme == "dark" else "深色主题"
        )
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
