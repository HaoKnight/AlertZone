"""AlertZone Server 的独立局域网桌面前端。

界面完全由 PySide6 原生控件绘制，只通过服务端的 JSON、JPEG 和控制接口
获取状态、预览与报警事件，不加载或依赖服务端的 Web 页面。
"""

from __future__ import annotations

import json
import math
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
    QRect,
    QRectF,
    QSettings,
    QSize,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QCloseEvent,
    QColor,
    QCursor,
    QFontMetrics,
    QIcon,
    QIntValidator,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
)
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
    QLayout,
    QLayoutItem,
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

try:
    if sys.platform != "darwin":
        raise ImportError
    import AppKit
    import objc
except ImportError:
    AppKit = None
    objc = None

APP_NAME = "AlertZone Desktop"
WINDOW_TITLE = f"{APP_NAME} · 服务端 · ©H-Knight"
ORGANIZATION_NAME = "AlertZone"
DEFAULT_PORT = 8765
POLL_INTERVAL_MS = 600
REQUEST_TIMEOUT_MS = 3500
ALERT_DISPLAY_OPTIONS = (
    ("zoom", "放大人物"),
    ("live", "实时预览"),
    ("zoom-red", "全屏红色且放大人物"),
    ("live-red", "全屏红色且实时预览"),
    ("sound-only", "仅提示音提醒"),
)
ALERT_IMAGE_MODES = {"zoom", "zoom-red", "live", "live-red"}
ALERT_LIVE_MODES = {"live", "live-red"}
REARM_DELAY_OPTIONS = (
    (0, "立即"),
    (5, "5 秒"),
    (10, "10 秒"),
    (20, "20 秒"),
    (30, "30 秒"),
    (60, "60 秒"),
)
THEME_MODES = ("light", "dark", "follow-system")
THEME_LABELS = {
    "light": "浅色主题",
    "dark": "深色主题",
    "follow-system": "跟随系统",
}

APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent
ICON_CANDIDATES = (
    PROJECT_DIR / "icon" / "icon.png",
    APP_DIR / "icon" / "icon.png",
    PROJECT_DIR / "icon.png",
    APP_DIR / "icon.png",
)
APP_DEFAULT_SOUND_CANDIDATES = (
    PROJECT_DIR / "audio" / "audio.mp3",
    APP_DIR / "audio" / "audio.mp3",
)


def app_icon_path() -> Path | None:
    """返回源码环境或打包环境中的程序图标。"""
    return next((path for path in ICON_CANDIDATES if path.is_file()), None)


def app_default_sound_path() -> Path | None:
    """返回源码环境或打包环境中的软件默认提示音。"""
    return next(
        (
            path
            for path in APP_DEFAULT_SOUND_CANDIDATES
            if path.is_file()
        ),
        None,
    )


def set_macos_dock_icon_visible(visible: bool) -> None:
    """运行时切换 macOS Dock 图标；其他平台保持不变。"""
    if sys.platform != "darwin":
        return
    try:
        import ctypes

        objc = ctypes.cdll.LoadLibrary("/usr/lib/libobjc.A.dylib")
        ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/AppKit.framework/AppKit"
        )
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]

        message_send = objc.objc_msgSend
        message_send.restype = ctypes.c_void_p
        message_send.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        application = message_send(
            objc.objc_getClass(b"NSApplication"),
            objc.sel_registerName(b"sharedApplication"),
        )

        message_send.restype = ctypes.c_bool
        message_send.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_long,
        ]
        # Regular(0) 显示 Dock；Accessory(1) 保留菜单栏托盘但隐藏 Dock。
        message_send(
            application,
            objc.sel_registerName(b"setActivationPolicy:"),
            0 if visible else 1,
        )
    except (AttributeError, OSError):
        # 不让系统接口差异影响窗口恢复或后台报警。
        return


class NativeMacMenuAction:
    """为原生 NSMenuItem 提供主窗口所需的 QAction 兼容接口。"""

    def __init__(self, menu_item: Any) -> None:
        self._menu_item = menu_item

    def blockSignals(self, _blocked: bool) -> None:
        return

    def setChecked(self, checked: bool) -> None:
        if AppKit is not None:
            self._menu_item.setState_(
                AppKit.NSControlStateValueOn
                if checked
                else AppKit.NSControlStateValueOff
            )

    def setText(self, text: str) -> None:
        self._menu_item.setTitle_(text)


if AppKit is not None and objc is not None:

    class NativeMacTrayTarget(AppKit.NSObject):
        """接收 macOS 菜单栏图标及原生菜单事件。"""

        def initWithWindow_(self, window: Any) -> Any:
            self = objc.super(NativeMacTrayTarget, self).init()
            if self is not None:
                self._window = window
                self._status_item = None
                self._menu = None
            return self

        @objc.IBAction
        def statusItemClicked_(self, _sender: Any) -> None:
            event = AppKit.NSApp.currentEvent()
            event_type = event.type() if event is not None else None
            if event_type in {
                AppKit.NSEventTypeRightMouseDown,
                AppKit.NSEventTypeRightMouseUp,
            }:
                self._status_item.popUpStatusItemMenu_(self._menu)
                return
            QTimer.singleShot(0, self._window.show_main_window)

        @objc.IBAction
        def openMainWindow_(self, _sender: Any) -> None:
            QTimer.singleShot(0, self._window.show_main_window)

        @objc.IBAction
        def toggleAlert_(self, _sender: Any) -> None:
            self._window._on_tray_alert_toggled(
                not self._window._alert_enabled()
            )

        @objc.IBAction
        def openAlertSettings_(self, _sender: Any) -> None:
            QTimer.singleShot(0, self._window.open_alert_settings)

        @objc.IBAction
        def openOtherSettings_(self, _sender: Any) -> None:
            QTimer.singleShot(0, self._window.open_other_settings)

        @objc.IBAction
        def quitApplication_(self, _sender: Any) -> None:
            QTimer.singleShot(0, self._window.quit_application)


class NativeMacTrayIcon:
    """使用 AppKit 创建可区分左右键的原生 macOS 菜栏状态项。"""

    def __init__(self, window: Any, icon_path: Path | None) -> None:
        if AppKit is None or objc is None:
            raise RuntimeError("当前环境未安装 macOS Cocoa 支持")
        self._window = window
        self._status_bar = AppKit.NSStatusBar.systemStatusBar()
        self._status_item = self._status_bar.statusItemWithLength_(
            AppKit.NSVariableStatusItemLength
        )
        self._target = NativeMacTrayTarget.alloc().initWithWindow_(window)
        self._menu = AppKit.NSMenu.alloc().initWithTitle_(APP_NAME)
        self._menu.setAutoenablesItems_(False)

        button = self._status_item.button()
        button.setTarget_(self._target)
        button.setAction_("statusItemClicked:")
        button.sendActionOn_(
            AppKit.NSEventMaskLeftMouseUp
            | AppKit.NSEventMaskRightMouseUp
        )
        if icon_path is not None:
            image = AppKit.NSImage.alloc().initWithContentsOfFile_(
                str(icon_path)
            )
            if image is not None:
                image.setSize_(AppKit.NSMakeSize(18, 18))
                button.setImage_(image)
        button.setToolTip_(APP_NAME)

        self._add_action("打开主界面", "openMainWindow:")
        self.status_action = NativeMacMenuAction(
            self._add_action("尚未连接", None, enabled=False)
        )
        self._menu.addItem_(AppKit.NSMenuItem.separatorItem())
        alert_item = self._add_action(
            "启用告警",
            "toggleAlert:",
        )
        alert_item.setState_(
            AppKit.NSControlStateValueOn
            if window._alert_enabled()
            else AppKit.NSControlStateValueOff
        )
        self.alert_action = NativeMacMenuAction(alert_item)
        self._add_action("告警设置", "openAlertSettings:")
        self._add_action("其他配置", "openOtherSettings:")
        self._menu.addItem_(AppKit.NSMenuItem.separatorItem())
        self._add_action("退出", "quitApplication:")

        self._target._status_item = self._status_item
        self._target._menu = self._menu

    def _add_action(
        self,
        title: str,
        selector: str | None,
        enabled: bool = True,
    ) -> Any:
        item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            title,
            selector,
            "",
        )
        if selector is not None:
            item.setTarget_(self._target)
        item.setEnabled_(enabled)
        self._menu.addItem_(item)
        return item

    def setToolTip(self, text: str) -> None:
        self._status_item.button().setToolTip_(text)

    def showMessage(self, *_args: Any) -> None:
        # 原生菜单栏状态项不模拟 Qt 气泡，避免触发非原生通知样式。
        return

    def hide(self) -> None:
        if self._status_item is not None:
            self._status_bar.removeStatusItem_(self._status_item)
            self._status_item = None


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


class FlowLayout(QLayout):
    """按可用宽度自动换行，避免小窗口压缩或截断按钮。"""

    def __init__(
        self,
        parent: QWidget | None = None,
        margins: tuple[int, int, int, int] = (0, 0, 0, 0),
        horizontal_spacing: int = 8,
        vertical_spacing: int = 8,
        expand_items: bool = False,
        justify_rows: bool = False,
        balanced_wrap: bool = False,
        split_index: int | None = None,
        center_vertically: bool = False,
        wrap_at_split: bool = False,
        compress_items: bool = False,
    ) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self._horizontal_spacing = horizontal_spacing
        self._vertical_spacing = vertical_spacing
        self._expand_items = expand_items
        self._justify_rows = justify_rows
        self._balanced_wrap = balanced_wrap
        self._split_index = split_index
        self._center_vertically = center_vertically
        self._wrap_at_split = wrap_at_split
        self._compress_items = compress_items
        self.setContentsMargins(*margins)

    def addItem(self, item: QLayoutItem) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int) -> QLayoutItem | None:
        return (
            self._items.pop(index)
            if 0 <= index < len(self._items)
            else None
        )

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self) -> QSize:
        return QSize(
            self.preferred_single_row_width(),
            self._single_row_height(),
        )

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._visible_items():
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QSize(
            margins.left() + margins.right(),
            margins.top() + margins.bottom(),
        )
        return size

    def preferred_single_row_width(self) -> int:
        margins = self.contentsMargins()
        items = self._visible_items()
        item_width = sum(
            item.sizeHint().width() for item in items
        )
        gaps = max(len(items) - 1, 0) * self._horizontal_spacing
        return (
            margins.left()
            + item_width
            + gaps
            + margins.right()
        )

    def minimum_single_row_width(self) -> int:
        margins = self.contentsMargins()
        items = self._visible_items()
        item_width = sum(
            item.minimumSize().width() for item in items
        )
        gaps = max(len(items) - 1, 0) * self._horizontal_spacing
        return (
            margins.left()
            + item_width
            + gaps
            + margins.right()
        )

    def _single_row_height(self) -> int:
        margins = self.contentsMargins()
        item_height = max(
            (
                item.sizeHint().height()
                for item in self._visible_items()
            ),
            default=0,
        )
        return margins.top() + item_height + margins.bottom()

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        margins = self.contentsMargins()
        effective = rect.adjusted(
            margins.left(),
            margins.top(),
            -margins.right(),
            -margins.bottom(),
        )
        available_width = max(effective.width(), 0)
        rows = self._layout_rows(available_width)
        content_height = sum(
            max(
                (item.sizeHint().height() for item in row),
                default=0,
            )
            for row in rows
        ) + self._vertical_spacing * max(len(rows) - 1, 0)
        y = effective.y()
        if self._center_vertically:
            y += max(
                (effective.height() - content_height) // 2,
                0,
            )

        for row_index, row in enumerate(rows):
            hints = [item.sizeHint() for item in row]
            widths = [hint.width() for hint in hints]
            line_height = max(
                (hint.height() for hint in hints),
                default=0,
            )
            base_gap = self._horizontal_spacing
            natural_width = sum(widths) + base_gap * max(
                len(row) - 1,
                0,
            )
            if (
                self._compress_items
                and len(rows) == 1
                and natural_width > available_width
            ):
                minimums = [
                    item.minimumSize().width() for item in row
                ]
                widths = self._shrink_widths(
                    widths,
                    minimums,
                    max(
                        available_width
                        - base_gap * max(len(row) - 1, 0),
                        0,
                    ),
                )
                natural_width = (
                    sum(widths)
                    + base_gap * max(len(row) - 1, 0)
                )
            extra = max(available_width - natural_width, 0)
            x = effective.x()
            gap = float(base_gap)
            split_spacer = 0

            if (
                len(rows) == 1
                and self._split_index is not None
                and 0 < self._split_index < len(row)
            ):
                split_spacer = extra
            elif self._expand_items and row:
                extra_per_item, remainder = divmod(extra, len(row))
                widths = [
                    width
                    + extra_per_item
                    + (1 if index < remainder else 0)
                    for index, width in enumerate(widths)
                ]
            elif self._justify_rows and len(row) > 1:
                gap += extra / (len(row) - 1)
            elif self._justify_rows and len(row) == 1:
                x += extra // 2

            for index, (item, hint, item_width) in enumerate(
                zip(row, hints, widths, strict=True)
            ):
                if (
                    split_spacer
                    and index == self._split_index
                ):
                    x += split_spacer
                if not test_only:
                    item.setGeometry(
                        QRect(
                            QPoint(
                                round(x),
                                y
                                + max(
                                    (
                                        line_height
                                        - hint.height()
                                    )
                                    // 2,
                                    0,
                                ),
                            ),
                            QSize(item_width, hint.height()),
                        )
                    )
                x += item_width + gap

            y += line_height
            if row_index < len(rows) - 1:
                y += self._vertical_spacing

        return (
            y
            - rect.y()
            + margins.bottom()
        )

    @staticmethod
    def _shrink_widths(
        widths: list[int],
        minimums: list[int],
        target_width: int,
    ) -> list[int]:
        result = widths.copy()
        overflow = max(sum(result) - target_width, 0)
        while overflow:
            flexible = [
                index
                for index, width in enumerate(result)
                if width > minimums[index]
            ]
            if not flexible:
                break
            share = max(math.ceil(overflow / len(flexible)), 1)
            for index in flexible:
                reduction = min(
                    share,
                    result[index] - minimums[index],
                    overflow,
                )
                result[index] -= reduction
                overflow -= reduction
                if not overflow:
                    break
        return result

    def _layout_rows(
        self,
        available_width: int,
    ) -> list[list[QLayoutItem]]:
        items = self._visible_items()
        if not items:
            return []
        minimum_content_width = (
            self.minimum_single_row_width()
            - self.contentsMargins().left()
            - self.contentsMargins().right()
        )
        if (
            self._compress_items
            and available_width >= minimum_content_width
        ):
            return [items]
        maximum_items = len(items)
        preferred_content_width = (
            self.preferred_single_row_width()
            - self.contentsMargins().left()
            - self.contentsMargins().right()
        )
        if self._balanced_wrap and available_width > 0:
            row_count = max(
                math.ceil(
                    preferred_content_width / available_width
                ),
                1,
            )
            maximum_items = max(
                math.ceil(len(items) / row_count),
                1,
            )

        rows: list[list[QLayoutItem]] = []
        current_row: list[QLayoutItem] = []
        current_width = 0
        force_split = (
            self._wrap_at_split
            and self._split_index is not None
            and preferred_content_width > available_width
        )
        for index, item in enumerate(items):
            if (
                force_split
                and index == self._split_index
                and current_row
            ):
                rows.append(current_row)
                current_row = []
                current_width = 0
            item_width = item.sizeHint().width()
            proposed_width = (
                current_width
                + (self._horizontal_spacing if current_row else 0)
                + item_width
            )
            if current_row and (
                proposed_width > available_width
                or len(current_row) >= maximum_items
            ):
                rows.append(current_row)
                current_row = []
                current_width = 0
            if current_row:
                current_width += self._horizontal_spacing
            current_row.append(item)
            current_width += item_width
        if current_row:
            rows.append(current_row)
        return rows

    def _visible_items(self) -> list[QLayoutItem]:
        return [item for item in self._items if not item.isEmpty()]


class HoverRevealControls(QWidget):
    """六个按钮的自适应流式布局容器。"""

    def __init__(
        self,
        buttons: list[QPushButton],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._buttons = buttons
        self._spacing = 6
        self.setObjectName("dashboardControls")

        self.flow_layout = FlowLayout(
            self,
            horizontal_spacing=self._spacing,
            vertical_spacing=4,
            expand_items=True,
            balanced_wrap=True,
            compress_items=True,
        )
        for button in buttons:
            button.setMinimumWidth(
                button.fontMetrics().horizontalAdvance(button.text())
                + 12
            )
            self.flow_layout.addWidget(button)

    def preferred_full_width(self) -> int:
        return (
            sum(button.sizeHint().width() for button in self._buttons)
            + max(len(self._buttons) - 1, 0) * self._spacing
        )

    def minimum_full_width(self) -> int:
        return self.flow_layout.minimum_single_row_width()

    def sync_text_fit_widths(self) -> None:
        """按当前主题字体计算安全宽度，避免压缩后遮挡四字标签。"""
        for button in self._buttons:
            button.ensurePolished()
            text_width = button.fontMetrics().horizontalAdvance(
                button.text()
            )
            button.setMinimumWidth(text_width + 10)
        self.flow_layout.invalidate()
        self.updateGeometry()

class HoverFrame(QFrame):
    """报告鼠标是否位于整个栏位（包含其子控件）内。"""

    hover_changed = Signal(bool)

    def enterEvent(self, event: Any) -> None:
        self.hover_changed.emit(True)
        super().enterEvent(event)

    def leaveEvent(self, event: Any) -> None:
        QTimer.singleShot(30, self._emit_leave_if_needed)
        super().leaveEvent(event)

    def _emit_leave_if_needed(self) -> None:
        if not self.underMouse():
            self.hover_changed.emit(False)


class CurrentPageStack(QStackedWidget):
    """只使用当前页面的最小尺寸，避免隐藏页面限制窗口缩放。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.currentChanged.connect(
            lambda _index: self.updateGeometry()
        )

    def minimumSizeHint(self) -> QSize:
        current = self.currentWidget()
        return (
            current.minimumSizeHint()
            if current is not None
            else QSize(0, 0)
        )


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
        raise ValueError("请输入 AlertZone Server 的局域网地址")
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
    cancel_requested = Signal()

    def __init__(self, settings: QSettings) -> None:
        super().__init__()
        self._settings = settings
        self.setObjectName("connectionPage")

        card = QFrame()
        card.setObjectName("connectionCard")
        card.setMaximumWidth(620)
        card.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(36, 34, 36, 34)
        card_layout.setSpacing(14)

        title = QLabel("连接 AlertZone Server")
        title.setObjectName("connectionTitle")
        subtitle = QLabel(
            "输入 AlertZone Server 主界面左下角显示的局域网地址。"
            "\n本机需要与服务端处于同一局域网。"
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
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setObjectName("connectionCancelButton")
        self.cancel_button.clicked.connect(self.cancel_requested.emit)
        button_row = QHBoxLayout()
        button_row.setContentsMargins(0, 0, 0, 0)
        button_row.setSpacing(8)
        button_row.addWidget(self.connect_button, 1)
        button_row.addWidget(self.cancel_button, 1)

        self.status_label = QLabel("")
        self.status_label.setObjectName("connectionStatus")
        self.status_label.setWordWrap(True)

        card_layout.addWidget(title)
        card_layout.addWidget(subtitle)
        card_layout.addSpacing(6)
        card_layout.addWidget(self.address_edit)
        card_layout.addLayout(button_row)
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


class MarqueeLabel(QLabel):
    """空间不足时以像素级动画滚动，空间恢复后居中显示。"""

    def __init__(
        self,
        text: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._full_text = ""
        self._scroll_offset = 0
        self._text_width = 0
        self._scroll_cycle_width = 0
        self._bubble_background = QColor(0, 0, 0, 0)
        self._bubble_border = QColor(0, 0, 0, 0)
        self._bubble_radius = 0.0
        self._text_label = QLabel(self)
        self._text_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft
            | Qt.AlignmentFlag.AlignVCenter
        )
        self._text_label.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents,
            True,
        )
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setInterval(20)
        self._scroll_timer.timeout.connect(self._advance_scroll)
        self.setText(text)

    def setText(self, text: str) -> None:
        self._full_text = str(text)
        self._scroll_offset = 0
        self._refresh_display()

    def text(self) -> str:
        return self._full_text

    def displayed_text(self) -> str:
        return self._text_label.text()

    def natural_text_width(self) -> int:
        return (
            self.fontMetrics().horizontalAdvance(self._full_text)
            + 22
        )

    def set_bubble_style(
        self,
        background: QColor,
        border: QColor,
    ) -> None:
        self._bubble_background = QColor(background)
        self._bubble_border = QColor(border)
        self.update()

    def set_bubble_radius(self, radius: float) -> None:
        self._bubble_radius = max(radius, 0.0)
        self.update()

    def paintEvent(self, event: Any) -> None:
        if self._bubble_background.alpha() > 0:
            painter = QPainter(self)
            painter.setRenderHint(
                QPainter.RenderHint.Antialiasing,
                True,
            )
            inset = 0.5 if self._bubble_border.alpha() > 0 else 0.0
            bubble_rect = QRectF(self.rect()).adjusted(
                inset,
                inset,
                -inset,
                -inset,
            )
            radius = min(
                self._bubble_radius,
                bubble_rect.height() / 2,
            )
            painter.setBrush(self._bubble_background)
            painter.setPen(
                QPen(self._bubble_border, 1)
                if self._bubble_border.alpha() > 0
                else Qt.PenStyle.NoPen
            )
            painter.drawRoundedRect(
                bubble_rect,
                radius,
                radius,
            )
            painter.end()
        super().paintEvent(event)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._refresh_display()

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        self._refresh_display()

    def hideEvent(self, event: Any) -> None:
        self._scroll_timer.stop()
        super().hideEvent(event)

    def refresh_marquee(self) -> None:
        self._refresh_display()

    def _refresh_display(self) -> None:
        super().setText("")
        self._text_label.setFont(self.font())
        text_color = self.palette().color(
            QPalette.ColorRole.WindowText
        )
        self._text_label.setStyleSheet(
            "background: transparent;"
            "border: none;"
            "padding: 0;"
            f"color: {text_color.name(QColor.NameFormat.HexArgb)};"
        )
        if not self._full_text or self.width() <= 0:
            self._scroll_timer.stop()
            self._text_label.setText(self._full_text)
            return
        available_width = max(self.width() - 18, 1)
        self._text_width = self.fontMetrics().horizontalAdvance(
            self._full_text
        )
        if self._text_width <= available_width:
            self._scroll_timer.stop()
            self._scroll_offset = 0
            self._text_label.setText(self._full_text)
            self._text_label.setGeometry(
                max((self.width() - self._text_width) // 2, 0),
                0,
                self._text_width,
                self.height(),
            )
            return
        gap_width = max(
            self.fontMetrics().horizontalAdvance("　　"),
            24,
        )
        self._scroll_cycle_width = self._text_width + gap_width
        self._text_label.setText(
            f"{self._full_text}　　{self._full_text}"
        )
        self._position_scrolling_text()
        if self.isVisible():
            self._scroll_timer.start()

    def _advance_scroll(self) -> None:
        if not self._scroll_cycle_width:
            return
        self._scroll_offset = (
            self._scroll_offset + 1
        ) % self._scroll_cycle_width
        self._position_scrolling_text()

    def _position_scrolling_text(self) -> None:
        repeated_width = (
            self._text_width * 2
            + max(self._scroll_cycle_width - self._text_width, 0)
        )
        self._text_label.setGeometry(
            9 - self._scroll_offset,
            0,
            repeated_width,
            self.height(),
        )


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
        self.setMinimumSize(0, 0)
        self.resize(460, 310)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)

        self._image = AlertCanvas()
        self._image.setObjectName("alertImage")
        self._image.setMinimumHeight(0)
        self._image.dismiss_requested.connect(self._accept)
        self._detail = self._image.detail_label
        self._countdown = self._image.countdown_label
        self._button = self._image.exit_button

        self._title = QLabel(
            "⚠️⚠️⚠️ 警告 ⚠️⚠️⚠️"
        )
        self._title.setObjectName("alertTitle")
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title.setMinimumWidth(0)
        self._title.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(3)
        layout.setSizeConstraint(
            QLayout.SizeConstraint.SetNoConstraint
        )
        layout.addWidget(self._title)
        layout.addWidget(self._image, 1)

        self.apply_theme("light")

    def minimumSizeHint(self) -> QSize:
        return QSize(0, 0)

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
            image_background = "#290006"
            image_text = "#ffd7dc"
            border = "#ffffff"
            button = "#ffffff"
            button_hover = "#ffe5e8"
            button_text = "#a50012"
        elif self._theme == "dark":
            background = "#24171a"
            title = "#ffb0b5"
            image_background = "#10090b"
            image_text = "#d99da2"
            border = "#713a40"
            button = "#e0444d"
            button_hover = "#f05a62"
            button_text = "#ffffff"
        else:
            background = "#fff5f5"
            title = "#b91c1c"
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
                min-height: 32px;
                color: {title};
                background: transparent;
                border: none;
                font-size: 22px;
                font-weight: 800;
            }}
            #alertImage {{
                color: {image_text};
                background: {image_background};
                border: 1px solid {border};
                border-radius: 8px;
            }}
            #mainAlertDetail, #mainAlertCountdown {{
                color: #ffffff;
                background: transparent;
                border: none;
                padding: 2px 9px;
                font-weight: 700;
            }}
            #mainAlertDetail[overlaySize="small"],
            #mainAlertCountdown[overlaySize="small"] {{
                border-radius: 10px;
            }}
            #mainAlertDetail[overlaySize="medium"],
            #mainAlertCountdown[overlaySize="medium"] {{
                border-radius: 12px;
            }}
            #mainAlertDetail[overlaySize="large"],
            #mainAlertCountdown[overlaySize="large"] {{
                border-radius: 15px;
            }}
            #mainAlertExitButton {{
                color: {button_text};
                background: {button};
                border: 1px solid rgba(255, 255, 255, 180);
                border-radius: 7px;
                font-weight: 800;
            }}
            #mainAlertExitButton:hover {{ background: {button_hover}; }}
            """
        )
        self._image.set_overlay_bubble_style(
            QColor(15, 23, 42, 205),
            QColor(255, 255, 255, 70),
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
        self._title.setText("弹窗位置")
        self._image.set_alert_detail(
            "拖动调整显示位置和大小。"
        )
        self._image.set_countdown_text("")
        self._image.clear_image(
            "这里将显示实时预览"
            if self._display_mode in ALERT_LIVE_MODES
            else "这里将显示报警截图"
        )
        self._image.configure_action("确定位置和大小", True)
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
        self._title.setText("⚠️⚠️⚠️ 警告 ⚠️⚠️⚠️")
        people_text = f"检测到 {max(people_count, 1)} 人进入监控区域"
        self._image.set_alert_detail(
            people_text,
            event_time,
        )
        self._image.set_countdown_text("")
        self._image.configure_action("退出告警", False)
        self._image.clear_image(
            "正在获取实时预览…"
            if self._display_mode in ALERT_LIVE_MODES
            else "正在获取报警截图…"
        )
        self.show()
        self.raise_()
        self.activateWindow()

    def set_event_image(self, image_data: bytes) -> None:
        if not self._image.set_image_data(image_data):
            self._image.clear_image("报警截图不可用")

    def set_countdown_text(self, text: str) -> None:
        self._image.set_countdown_text(
            "" if self._placement_mode else text
        )

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
            (2, "2 秒"),
            (5, "5 秒"),
            (10, "10 秒"),
            (15, "15 秒"),
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
            selected_auto_exit
            if selected_auto_exit >= 0
            else self.auto_exit_seconds.findData(10)
        )

        self.rearm_delay_seconds = DownwardComboBox()
        self.rearm_delay_seconds.setAccessibleName("等待再次监测")
        self.rearm_delay_seconds.setMinimumWidth(130)
        self.rearm_delay_seconds.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        for value, label in REARM_DELAY_OPTIONS:
            self.rearm_delay_seconds.addItem(label, value)
        try:
            saved_rearm_delay = max(
                int(settings.value("alert/rearm_delay_seconds", 0)),
                0,
            )
        except (TypeError, ValueError):
            saved_rearm_delay = 0
        selected_rearm_delay = self.rearm_delay_seconds.findData(
            saved_rearm_delay
        )
        self.rearm_delay_seconds.addItem("自定义：", "custom")
        self.rearm_custom_seconds = QLineEdit()
        self.rearm_custom_seconds.setObjectName("rearmCustomSeconds")
        self.rearm_custom_seconds.setPlaceholderText("输入秒数")
        self.rearm_custom_seconds.setValidator(
            QIntValidator(1, 86400, self.rearm_custom_seconds)
        )
        self.rearm_custom_seconds.setMaximumWidth(110)
        self.rearm_custom_seconds.setText(
            str(saved_rearm_delay)
            if saved_rearm_delay > 0
            and selected_rearm_delay < 0
            else ""
        )
        self.rearm_custom_unit = QLabel("秒")
        rearm_delay_field = QWidget()
        rearm_delay_layout = QHBoxLayout(rearm_delay_field)
        rearm_delay_layout.setContentsMargins(0, 0, 0, 0)
        rearm_delay_layout.setSpacing(6)
        rearm_delay_layout.addWidget(self.rearm_delay_seconds, 1)
        rearm_delay_layout.addWidget(self.rearm_custom_seconds)
        rearm_delay_layout.addWidget(self.rearm_custom_unit)
        if selected_rearm_delay < 0:
            selected_rearm_delay = self.rearm_delay_seconds.findData(
                "custom"
            )
        self.rearm_delay_seconds.setCurrentIndex(selected_rearm_delay)
        self.rearm_delay_seconds.currentIndexChanged.connect(
            self._sync_rearm_custom_input
        )
        self._sync_rearm_custom_input()

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
        form.addRow("等待重新布防", rearm_delay_field)
        form.addRow("持续跟随显示", self.continuous_alert_display)

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

    def _sync_rearm_custom_input(self, _index: int = -1) -> None:
        custom_selected = (
            self.rearm_delay_seconds.currentData() == "custom"
        )
        self.rearm_custom_seconds.setVisible(custom_selected)
        self.rearm_custom_unit.setVisible(custom_selected)
        if custom_selected and not self.rearm_custom_seconds.text():
            self.rearm_custom_seconds.setText("60")

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
        rearm_delay: Any = self.rearm_delay_seconds.currentData()
        if rearm_delay == "custom":
            try:
                rearm_delay = int(
                    self.rearm_custom_seconds.text().strip()
                )
            except (TypeError, ValueError):
                rearm_delay = 0
            if not 1 <= rearm_delay <= 86400:
                QMessageBox.warning(
                    self,
                    "自定义时间无效",
                    "请输入 1 至 86400 之间的秒数。",
                )
                self.rearm_custom_seconds.setFocus()
                self.rearm_custom_seconds.selectAll()
                return
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
            "alert/rearm_delay_seconds",
            max(int(rearm_delay), 0),
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
        self.sound_mode.addItem("关闭提示音", "off")
        self.sound_mode.addItem("软件默认提示音", "app-default")
        self.sound_mode.addItem("电脑默认提示音", "default")
        self.sound_mode.addItem("自定义声音文件", "custom")
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
        form.addRow("音量", volume_row)
        form.addRow("", self._test_button)

        self.sound_path.textChanged.connect(self._sync_controls)

        self._sync_controls()

    def _sync_controls(self, *_args: Any) -> None:
        mode = str(self.sound_mode.currentData())
        custom = mode == "custom"
        sound_off = mode == "off"
        uses_volume = mode in {"app-default", "custom"}
        valid_custom_file = Path(
            self.sound_path.text().strip()
        ).is_file()
        app_default_available = app_default_sound_path() is not None
        self.sound_path.setEnabled(custom)
        self._browse_button.setEnabled(custom)
        self.volume.setEnabled(uses_volume)
        self.volume_value.setEnabled(uses_volume)
        self._test_button.setEnabled(
            not sound_off
            and (not custom or valid_custom_file)
            and (mode != "app-default" or app_default_available)
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

        title_label = QLabel("其他配置")
        title_label.setObjectName("dialogSectionTitle")

        empty_card = QFrame()
        empty_card.setObjectName("dialogCard")
        empty_layout = QVBoxLayout(empty_card)
        empty_layout.setContentsMargins(12, 10, 12, 10)
        empty_label = QLabel("暂无可配置项")
        empty_label.setObjectName("dialogDescription")
        empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_label.setMinimumWidth(240)
        empty_label.setMinimumHeight(28)
        empty_layout.addWidget(empty_label)

        close_button = QPushButton("关闭")
        close_button.setObjectName("primaryButton")
        close_button.clicked.connect(self.accept)
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        button_layout.addWidget(close_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 14, 12, 12)
        layout.setSpacing(6)
        layout.addWidget(title_label)
        layout.addWidget(empty_card)
        layout.addSpacing(6)
        layout.addLayout(button_layout)
        self.adjustSize()


class CloseActionDialog(QDialog):
    """使用服务端的控件风格呈现关闭窗口操作。"""

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

    _PREFERRED_SIZE = QSize(320, 240)

    def __init__(self, placeholder: str) -> None:
        super().__init__(placeholder)
        self._source_pixmap: QPixmap | None = None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(0, 0)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )

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

    def displayed_pixmap_rect(self) -> QRect:
        """返回保持比例缩放后，图像在标签中的实际显示区域。"""
        if self._source_pixmap is None:
            return self.rect()
        scaled_size = self._source_pixmap.size().scaled(
            self.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
        )
        return QRect(
            max((self.width() - scaled_size.width()) // 2, 0),
            max((self.height() - scaled_size.height()) // 2, 0),
            scaled_size.width(),
            scaled_size.height(),
        )

    def sizeHint(self) -> QSize:
        """避免缩放后的帧反向撑大布局并形成窗口放大循环。"""
        return QSize(self._PREFERRED_SIZE)

    def minimumSizeHint(self) -> QSize:
        return QSize(0, 0)

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


class AlertCanvas(ScaledPixmapLabel):
    """以画面为主的告警区域，提示文字和退出按钮浮在画面上。"""

    dismiss_requested = Signal()

    def __init__(self) -> None:
        super().__init__("正在获取告警画面…")
        self.setMouseTracking(True)

        self.detail_label = MarqueeLabel(
            "检测到 1 人进入监控区域",
            self,
        )
        self.detail_label.setObjectName("mainAlertDetail")
        self.detail_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail_label.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents,
            True,
        )
        self.countdown_label = MarqueeLabel("", self)
        self.countdown_label.setObjectName("mainAlertCountdown")
        self.countdown_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.countdown_label.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents,
            True,
        )
        self.countdown_label.hide()

        self.exit_button = QPushButton("退出告警", self)
        self.exit_button.setObjectName("mainAlertExitButton")
        self.exit_button.setMinimumSize(0, 0)
        self.exit_button.setVisible(False)
        self.exit_button.clicked.connect(self.dismiss_requested.emit)
        self._action_always_visible = False
        self._overlay_font_size = 0
        self._action_style_key: tuple[int, int] | None = None
        self.set_overlay_bubble_style(
            QColor(0, 0, 0, 150),
            QColor(0, 0, 0, 0),
        )

    def set_overlay_bubble_style(
        self,
        background: QColor,
        border: QColor,
    ) -> None:
        for label in (self.detail_label, self.countdown_label):
            label.set_bubble_style(background, border)

    def reset_hover_controls(self) -> None:
        if not self._action_always_visible:
            self.exit_button.hide()

    def configure_action(
        self,
        text: str,
        always_visible: bool = False,
    ) -> None:
        self._action_always_visible = always_visible
        self.exit_button.setText(text)
        self.exit_button.setVisible(always_visible)
        self._position_overlays()

    def set_alert_detail(self, text: str, tooltip: str = "") -> None:
        self.detail_label.setText(text)
        self.detail_label.setToolTip(tooltip)
        self._position_overlays()

    def set_countdown_text(self, text: str) -> None:
        self.countdown_label.setText(text)
        self.countdown_label.setVisible(bool(text))
        self._position_overlays()

    def set_image_data(self, image_data: bytes) -> bool:
        loaded = super().set_image_data(image_data)
        self._position_overlays()
        return loaded

    def clear_image(self, placeholder: str) -> None:
        super().clear_image(placeholder)
        self._position_overlays()

    def mouseMoveEvent(self, event: Any) -> None:
        point = event.position().toPoint()
        content_rect = self.displayed_pixmap_rect()
        center_area = content_rect.adjusted(
            content_rect.width() // 4,
            content_rect.height() // 4,
            -(content_rect.width() // 4),
            -(content_rect.height() // 4),
        )
        self.exit_button.setVisible(
            self._action_always_visible
            or center_area.contains(point)
            or self.exit_button.underMouse()
        )
        super().mouseMoveEvent(event)

    def leaveEvent(self, event: Any) -> None:
        super().leaveEvent(event)
        QTimer.singleShot(80, self._hide_exit_after_leave)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._position_overlays()

    def _position_overlays(self) -> None:
        content_rect = self.displayed_pixmap_rect()
        self._sync_overlay_font(content_rect)
        available_width = max(content_rect.width() - 24, 0)
        edge_margin = max(
            min(content_rect.height() // 24, 16),
            8,
        )
        detail_height = max(
            self.detail_label.sizeHint().height(),
            self._overlay_font_size + 12,
        )
        detail_width = min(
            max(self.detail_label.natural_text_width(), 160),
            available_width,
        )
        self.detail_label.setGeometry(
            content_rect.x()
            + max((content_rect.width() - detail_width) // 2, 0),
            max(
                content_rect.bottom()
                - detail_height
                - edge_margin
                + 1,
                content_rect.top(),
            ),
            detail_width,
            detail_height,
        )
        self.detail_label.refresh_marquee()
        countdown_height = max(
            self.countdown_label.sizeHint().height(),
            self._overlay_font_size + 12,
        )
        countdown_width = min(
            max(self.countdown_label.natural_text_width(), 112),
            available_width,
        )
        self.countdown_label.setGeometry(
            content_rect.x()
            + max(
                (content_rect.width() - countdown_width) // 2,
                0,
            ),
            content_rect.y() + edge_margin,
            countdown_width,
            countdown_height,
        )
        self.countdown_label.refresh_marquee()
        self._position_action_button(content_rect, edge_margin)

    def _position_action_button(
        self,
        content_rect: QRect,
        edge_margin: int,
    ) -> None:
        available_width = max(
            content_rect.width() - edge_margin * 2,
            0,
        )
        available_height = max(
            content_rect.height() - edge_margin * 2,
            0,
        )
        target_size = max(
            12,
            min(
                round(
                    min(
                        content_rect.width() / 24,
                        content_rect.height() / 13,
                    )
                ),
                26,
            ),
        )
        font = self.exit_button.font()
        while True:
            font.setPixelSize(target_size)
            font.setBold(True)
            metrics = QFontMetrics(font)
            natural_width = (
                metrics.horizontalAdvance(self.exit_button.text())
                + max(target_size + 24, 36)
            )
            if natural_width <= available_width or target_size <= 8:
                break
            target_size -= 1
        self.exit_button.setFont(font)
        button_height = min(
            max(metrics.height() + 20, 36),
            available_height,
        )
        button_width = min(natural_width, available_width)
        radius = max(min(button_height // 4, 14), 6)
        style_key = (target_size, radius)
        if style_key != self._action_style_key:
            self._action_style_key = style_key
            self.exit_button.setStyleSheet(
                "min-width: 0px;"
                "min-height: 0px;"
                f"font-size: {target_size}px;"
                f"border-radius: {radius}px;"
                "padding: 0 10px;"
            )
        desired_y = (
            content_rect.top()
            + round(content_rect.height() * 0.66)
            - button_height // 2
        )
        minimum_y = content_rect.top() + edge_margin
        maximum_y = max(
            self.detail_label.geometry().top()
            - button_height
            - edge_margin,
            minimum_y,
        )
        self.exit_button.setGeometry(
            content_rect.x()
            + max(
                (content_rect.width() - button_width) // 2,
                0,
            ),
            min(max(desired_y, minimum_y), maximum_y),
            button_width,
            button_height,
        )

    def _sync_overlay_font(self, content_rect: QRect) -> None:
        target_size = max(
            10,
            min(
                round(
                    min(
                        content_rect.width() / 32,
                        content_rect.height() / 20,
                    )
                ),
                20,
            ),
        )
        if target_size == self._overlay_font_size:
            return
        self._overlay_font_size = target_size
        overlay_size = (
            "small"
            if target_size <= 11
            else "medium"
            if target_size <= 15
            else "large"
        )
        radius = max(
            10,
            min(round((target_size + 12) / 2), 15),
        )
        for label in (self.detail_label, self.countdown_label):
            font = label.font()
            font.setPixelSize(target_size)
            font.setBold(True)
            label.setFont(font)
            label.setProperty("overlaySize", overlay_size)
            label.setStyleSheet(
                f"font-size: {target_size}px;"
            )
            label.set_bubble_radius(radius)
            label.style().unpolish(label)
            label.style().polish(label)
            label.refresh_marquee()

    def _hide_exit_after_leave(self) -> None:
        if (
            not self._action_always_visible
            and not self.underMouse()
            and not self.exit_button.underMouse()
        ):
            self.exit_button.hide()


class NativeDashboard(QWidget):
    """完全由 Desktop 绘制并直接消费服务端 API 的监控面板。"""

    alert_settings_requested = Signal()
    popup_settings_requested = Signal()
    other_settings_requested = Signal()
    alert_dismiss_requested = Signal()
    alert_enabled_changed = Signal(bool)
    continuous_monitoring_changed = Signal(bool)

    def __init__(self, settings: QSettings) -> None:
        super().__init__()
        self._settings = settings
        self._server_url = ""
        self._view_active = False
        self._preview_request_active = False
        self._preview_sequence = 0
        self._controls_trigger_hovered = False
        self._controls_panel_hovered = False
        self._network = QNetworkAccessManager(self)
        self._preview_timer = QTimer(self)
        self._preview_timer.setInterval(180)
        self._preview_timer.timeout.connect(self._request_preview)

        self.setObjectName("nativeDashboard")

        topbar = HoverFrame()
        topbar.setObjectName("dashboardCard")
        topbar.hover_changed.connect(
            self._on_controls_panel_hovered
        )
        self._controls_card = topbar
        top_layout = QGridLayout(topbar)
        top_layout.setContentsMargins(10, 8, 10, 8)
        top_layout.setHorizontalSpacing(6)
        top_layout.setVerticalSpacing(4)

        self.alert_enabled_button = self._make_toggle_button("启用告警")
        self.continuous_button = self._make_toggle_button("连续监测")
        self.preview_button = self._make_toggle_button("实时预览")
        self.alert_settings_button = QPushButton("告警设置")
        self.popup_settings_button = QPushButton("弹窗位置")
        self.other_settings_button = QPushButton("其他配置")

        self._controls = HoverRevealControls(
            [
                self.alert_enabled_button,
                self.continuous_button,
                self.preview_button,
                self.popup_settings_button,
                self.alert_settings_button,
                self.other_settings_button,
            ]
        )
        self._controls_layout = self._controls.flow_layout
        top_layout.addWidget(self._controls, 0, 0)

        monitor = QFrame()
        monitor.setObjectName("monitorCard")
        monitor_layout = QVBoxLayout(monitor)
        monitor_layout.setContentsMargins(0, 0, 0, 0)

        self.monitor_stack = QStackedWidget()
        self._idle_panel = QWidget()
        self._idle_layout = QVBoxLayout(self._idle_panel)
        self._idle_layout.setContentsMargins(20, 20, 20, 20)
        self._idle_layout.setSpacing(4)
        self._idle_layout.addStretch()
        self.status_icon = QLabel("⌁")
        self.status_icon.setObjectName("monitorIcon")
        self.status_icon.setFixedSize(76, 76)
        self.status_icon.setProperty("state", "idle")
        self.status_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_title = QLabel("等待连接")
        self.status_title.setObjectName("monitorTitle")
        self.status_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_detail = QLabel("请先连接 AlertZone Server")
        self.status_detail.setObjectName("monitorDetail")
        self.status_detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_detail.setWordWrap(True)
        self._source_label = QLabel("数据由 AlertZone Server API 提供")
        self._source_label.setObjectName("dashboardSource")
        self._source_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._idle_layout.addWidget(
            self.status_icon,
            0,
            Qt.AlignmentFlag.AlignHCenter,
        )
        self._idle_layout.addWidget(self.status_title)
        self._idle_layout.addWidget(self.status_detail)
        self._idle_layout.addSpacing(2)
        self._idle_layout.addWidget(self._source_label)
        self._idle_layout.addStretch()

        self.preview_label = ScaledPixmapLabel("正在等待实时预览…")
        self.preview_label.setObjectName("previewLabel")

        self.alert_panel = QWidget()
        self.alert_panel.setObjectName("mainAlertPanel")
        self.alert_panel.setProperty("redAlert", False)
        alert_layout = QVBoxLayout(self.alert_panel)
        alert_layout.setContentsMargins(8, 8, 8, 8)
        alert_layout.setSpacing(3)
        self.alert_title = QLabel(
            "⚠️⚠️⚠️ 警告 ⚠️⚠️⚠️"
        )
        self.alert_title.setObjectName("mainAlertTitle")
        self.alert_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.alert_title.setMinimumWidth(0)
        self.alert_title.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        self.alert_image = AlertCanvas()
        self.alert_image.setObjectName("mainAlertImage")
        self.alert_detail = self.alert_image.detail_label
        self.alert_countdown = self.alert_image.countdown_label
        self.alert_exit_button = self.alert_image.exit_button
        self.alert_image.dismiss_requested.connect(
            self.alert_dismiss_requested.emit
        )
        alert_layout.addWidget(self.alert_title)
        alert_layout.addWidget(self.alert_image, 1)

        self.monitor_stack.addWidget(self._idle_panel)
        self.monitor_stack.addWidget(self.preview_label)
        self.monitor_stack.addWidget(self.alert_panel)
        monitor_layout.addWidget(self.monitor_stack)

        stats = QFrame()
        stats.setObjectName("dashboardCard")
        stats_layout = QGridLayout(stats)
        stats_layout.setContentsMargins(14, 7, 14, 7)
        stats_layout.setHorizontalSpacing(8)
        self.people_value = self._make_stat(stats_layout, 0, "人数", "0")
        self.presence_value = self._make_stat(stats_layout, 1, "持续", "—")
        self.fps_value = self._make_stat(stats_layout, 2, "FPS", "—")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 2, 10, 8)
        layout.setSpacing(6)
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
        self._status_density = ""

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

    def preferred_single_row_width(self) -> int:
        margins = self.layout().contentsMargins()
        return (
            self._controls.minimum_full_width()
            + margins.left()
            + margins.right()
            + 32
        )

    def set_controls_trigger_hovered(self, hovered: bool) -> None:
        self._controls_trigger_hovered = hovered
        if hovered:
            self._sync_controls_card_visibility()
        else:
            QTimer.singleShot(
                120,
                self._sync_controls_card_visibility,
            )

    def _on_controls_panel_hovered(self, hovered: bool) -> None:
        self._controls_panel_hovered = hovered
        if hovered:
            self._sync_controls_card_visibility()
        else:
            QTimer.singleShot(
                120,
                self._sync_controls_card_visibility,
            )

    def _sync_controls_card_visibility(self) -> None:
        outer_margins = self.layout().contentsMargins()
        card_margins = self._controls_card.layout().contentsMargins()
        available_width = max(
            self.width()
            - outer_margins.left()
            - outer_margins.right()
            - card_margins.left()
            - card_margins.right(),
            0,
        )
        narrow = (
            available_width < self._controls.minimum_full_width()
        )
        show_card = (
            not narrow
            or self._controls_trigger_hovered
            or self._controls_panel_hovered
        )
        if self._controls_card.isHidden() == show_card:
            self._controls_card.setVisible(show_card)
            self.updateGeometry()

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        QTimer.singleShot(
            0,
            self._sync_controls_card_visibility,
        )
        QTimer.singleShot(0, self._sync_status_density)

    def _sync_status_density(self) -> None:
        """在低矮监测区域中同步缩放状态元素，避免图文相互覆盖。"""
        monitor_height = self.monitor_stack.height()
        if monitor_height < 165:
            density = "tiny"
            icon_size = 46
            margins = (4, 3, 4, 3)
        elif monitor_height < 250:
            density = "compact"
            icon_size = 58
            margins = (8, 6, 8, 6)
        else:
            density = "normal"
            icon_size = 76
            margins = (20, 20, 20, 20)
        if density == self._status_density:
            return
        self._status_density = density
        self._idle_layout.setContentsMargins(*margins)
        self.status_icon.setFixedSize(icon_size, icon_size)
        for widget in (
            self.status_icon,
            self.status_title,
            self.status_detail,
            self._source_label,
        ):
            widget.setProperty("statusDensity", density)
            widget.style().unpolish(widget)
            widget.style().polish(widget)
        self._idle_panel.updateGeometry()

    def show_alert(
        self,
        people_count: int,
        event_time: str = "",
        mode: str = "zoom",
    ) -> None:
        """在主页面中显示与 Web 端一致的告警内容。"""
        self.set_alert_display_mode(mode)
        people_text = (
            f"检测到 {max(people_count, 1)} 人进入监控区域"
        )
        self.alert_image.set_alert_detail(people_text, event_time)
        self.alert_image.configure_action("退出告警", False)
        self.alert_image.reset_hover_controls()
        self.alert_image.clear_image(
            "正在获取实时预览…"
            if mode in ALERT_LIVE_MODES
            else "正在获取报警截图…"
        )
        self.monitor_stack.setCurrentWidget(self.alert_panel)
        self._update_preview_timer()

    def hide_alert(self) -> None:
        if not self.is_alert_active():
            return
        self.alert_image.reset_hover_controls()
        self.alert_image.clear_image("正在获取告警画面…")
        self._sync_monitor_page()
        self._update_preview_timer()

    def is_alert_active(self) -> bool:
        return self.monitor_stack.currentWidget() is self.alert_panel

    def set_alert_display_mode(self, mode: str) -> None:
        mode = normalize_alert_display_mode(mode)
        popup_position_available = mode != "sound-only"
        self.popup_settings_button.setEnabled(popup_position_available)
        self.popup_settings_button.setToolTip(
            ""
            if popup_position_available
            else "仅提示音提醒不会显示告警小窗"
        )
        self.alert_panel.setProperty(
            "redAlert",
            mode in {"zoom-red", "live-red"},
        )
        self.alert_panel.setProperty("displayMode", mode)
        self.alert_image.setVisible(mode in ALERT_IMAGE_MODES)
        for widget in (
            self.alert_panel,
            self.alert_image,
            self.alert_title,
            self.alert_detail,
            self.alert_exit_button,
        ):
            widget.style().unpolish(widget)
            widget.style().polish(widget)

    def set_alert_image(self, image_data: bytes) -> bool:
        return self.alert_image.set_image_data(image_data)

    def set_alert_countdown(self, text: str) -> None:
        self.alert_image.set_countdown_text(text)

    def set_connection(self, online: bool, detail: str = "") -> None:
        if not online:
            self._set_monitor_state("idle", "⌁")
            self.status_title.setText("连接中断")
            self.status_detail.setText(detail or "正在尝试重新连接服务端")
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
                str(payload.get("status") or "请在服务端中开始检测")
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
        if not self.is_alert_active():
            self._sync_monitor_page()
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
            and not self.is_alert_active()
        )
        if not self.is_alert_active():
            self._sync_monitor_page()
        if should_run:
            self._preview_timer.start()
        else:
            self._preview_timer.stop()

    def _sync_monitor_page(self) -> None:
        self.monitor_stack.setCurrentWidget(
            self.preview_label
            if self.preview_button.isChecked()
            else self.monitor_stack.widget(0)
        )

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
                self.preview_label.clear_image("等待服务端生成预览画面…")
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
        self._alert_active = False
        self._current_alert_people = 1
        self._current_alert_timestamp = ""
        self._current_alert_image_available = False
        self._current_alert_countdown = ""
        self._background_mode = False
        saved_theme_mode = str(
            self._settings.value(
                "appearance/theme_mode",
                "follow-system",
            )
        )
        self._theme_mode = (
            saved_theme_mode
            if saved_theme_mode in THEME_MODES
            else "follow-system"
        )
        self._current_theme = ""

        self.setWindowTitle(WINDOW_TITLE)
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
        self._alert_rearm_timer = QTimer(self)
        self._alert_rearm_timer.setSingleShot(True)
        self._alert_rearm_timer.timeout.connect(
            self._request_alert_rearm
        )
        self._alert_countdown_timer = QTimer(self)
        self._alert_countdown_timer.setInterval(250)
        self._alert_countdown_timer.timeout.connect(
            self._update_alert_countdown
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
        self._connection_page.cancel_requested.connect(
            self.cancel_connection
        )
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
        self._dashboard.alert_dismiss_requested.connect(
            self._dismiss_current_alert
        )
        self._dashboard.alert_enabled_changed.connect(
            self._on_alert_enabled_changed
        )
        self._dashboard.continuous_monitoring_changed.connect(
            self._on_continuous_monitoring_changed
        )

        self._stack = CurrentPageStack()
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
        """使用可换行的普通按钮顶栏，避开窄窗口文字挤压。"""
        topbar = HoverFrame()
        topbar.setObjectName("mainTopbar")
        topbar.hover_changed.connect(
            self._dashboard.set_controls_trigger_hovered
        )
        layout = FlowLayout(
            topbar,
            margins=(4, 0, 4, 0),
            horizontal_spacing=3,
            vertical_spacing=0,
            justify_rows=True,
            balanced_wrap=True,
            split_index=2,
            center_vertically=True,
            wrap_at_split=True,
        )

        connection_group = QWidget()
        connection_group.setObjectName("toolbarConnectionGroup")
        connection_layout = QHBoxLayout(connection_group)
        connection_layout.setContentsMargins(0, 0, 0, 0)
        connection_layout.setSpacing(0)
        self._monitor_label = QLabel("Server状态：")
        self._monitor_label.setObjectName("toolbarTitle")
        connection_layout.addWidget(self._monitor_label)
        self._address_label = QLabel("尚未连接")
        self._address_label.setObjectName("toolbarAddress")
        self._address_label.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Preferred,
        )
        self._address_label.setMinimumWidth(0)
        connection_layout.addWidget(self._address_label)
        layout.addWidget(connection_group)

        refresh_button = QPushButton("刷新")
        refresh_button.setObjectName("topbarTextButton")
        refresh_button.clicked.connect(self._refresh_dashboard)
        layout.addWidget(refresh_button)

        self._theme_button = QPushButton(
            THEME_LABELS[self._theme_mode]
        )
        self._theme_button.setObjectName("topbarTextButton")
        self._theme_button.setProperty("sectionStart", True)
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

    def _build_tray(self) -> None:
        self._tray: Any | None = None
        self._tray_menu: QMenu | None = None
        self._tray_alert_action: Any | None = None
        self._tray_status_action: Any | None = None
        if sys.platform == "darwin":
            # macOS 只使用 Cocoa 状态栏，绝不静默回退到 Qt QMenu。
            # 依赖缺失时保持主窗口可用，并由 hide_to_tray 阻止窗口失联。
            if AppKit is not None and objc is not None:
                tray = NativeMacTrayIcon(self, app_icon_path())
                self._tray_status_action = tray.status_action
                self._tray_alert_action = tray.alert_action
                self._tray = tray
            return
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        tray = QSystemTrayIcon(self._icon, self)
        tray.setToolTip(APP_NAME)
        menu = QMenu(self)
        open_action = menu.addAction("打开主界面")
        open_action.triggered.connect(self.show_main_window)
        self._tray_status_action = menu.addAction("尚未连接")
        self._tray_status_action.setEnabled(False)
        menu.addSeparator()
        self._tray_alert_action = menu.addAction("启用告警")
        self._tray_alert_action.setCheckable(True)
        self._tray_alert_action.setChecked(self._alert_enabled())
        self._tray_alert_action.toggled.connect(
            self._on_tray_alert_toggled
        )
        alert_settings_action = menu.addAction("告警设置")
        alert_settings_action.triggered.connect(self.open_alert_settings)
        other_settings_action = menu.addAction("其他配置")
        other_settings_action.triggered.connect(self.open_other_settings)
        menu.addSeparator()
        quit_action = menu.addAction("退出")
        quit_action.triggered.connect(self.quit_application)
        # 不交给系统自动弹出菜单，避免 macOS 左键也打开菜单。
        # 左右键行为统一在 activated 信号中区分。
        tray.activated.connect(self._tray_activated)
        tray.show()
        self._tray_menu = menu
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
        self._connection_page.set_connecting(True, "正在验证 AlertZone Server…")
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
                raise ValueError("该地址不是可识别的 AlertZone Server")
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
        if self._alert_enabled():
            self._alert_rearm_timer.stop()
            QTimer.singleShot(0, self._request_alert_rearm)
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

    def cancel_connection(self) -> None:
        """取消当前地址验证，并返回可稍后重新连接的主页面。"""
        if self._probe_reply is not None:
            reply = self._probe_reply
            self._probe_reply = None
            reply.abort()
            reply.deleteLater()
        if not self._server_url:
            self._settings.setValue("connection/auto_connect", False)
        self._settings.sync()
        self._connection_page.set_connecting(False, "")
        self._stack.setCurrentWidget(self._dashboard)
        self._dashboard.set_view_active(
            bool(self._server_url) and self.isVisible()
        )
        self._topbar.setVisible(True)
        if not self._server_url:
            self._dashboard.set_connection(
                False,
                "尚未连接 AlertZone Server",
            )

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
        if self._alert_rearm_timer.isActive():
            self._schedule_alert_rearm()
        elif self._server_url and self._alert_enabled():
            self._request_alert_rearm()

    def _on_alert_enabled_changed(self, enabled: bool) -> None:
        if self._tray_alert_action is not None:
            self._tray_alert_action.blockSignals(True)
            self._tray_alert_action.setChecked(enabled)
            self._tray_alert_action.blockSignals(False)
        if enabled:
            self._alert_rearm_timer.stop()
            if self._server_url:
                self._request_alert_rearm()
            return
        self._alert_rearm_timer.stop()
        self._dismiss_current_alert(rearm=False)

    def _on_tray_alert_toggled(self, enabled: bool) -> None:
        """让托盘菜单与主页告警开关共用同一状态和处理流程。"""
        if self._dashboard.alert_enabled_button.isChecked() != enabled:
            self._dashboard.alert_enabled_button.setChecked(enabled)

    def _on_continuous_monitoring_changed(self, enabled: bool) -> None:
        """连续监测开启后，让告警界面从当前时刻开始重新布防。"""
        if not enabled:
            return
        if (
            self._server_url
            and self._alert_enabled()
            and not self._alert_active
        ):
            self._request_alert_rearm()

    def _on_alert_display_mode_changed(self, mode: str) -> None:
        mode = normalize_alert_display_mode(mode)
        self._settings.setValue("alert/display_mode", mode)
        self._settings.sync()
        self._popup.set_display_mode(mode)
        self._dashboard.set_alert_display_mode(mode)
        if not self._alert_active:
            return
        self._sync_alert_surface()
        if mode == "sound-only":
            self._stop_alert_live_preview()
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
        if not self._alert_enabled():
            return
        if self._alert_rearm_timer.isActive():
            return
        self._alert_exit_timer.stop()
        self._latest_person_present = bool(
            data.get("person_present", True)
        )
        event_id = int(data.get("intrusion_event_id", 0))
        self._current_alert_event = event_id
        self._current_alert_image_available = bool(
            data.get("intrusion_image_available")
        )
        count = int(data.get("intrusion_people_count") or data.get("people_count") or 1)
        self._current_alert_people = max(count, 1)
        timestamp = ""
        event_time = data.get("intrusion_time")
        if isinstance(event_time, (float, int)):
            timestamp = (
                datetime.fromtimestamp(event_time, tz=timezone.utc)
                .astimezone()
                .strftime("%Y-%m-%d %H:%M:%S")
            )
        self._current_alert_timestamp = timestamp
        self._current_alert_countdown = ""
        self._alert_active = True
        self._sync_alert_surface()
        self._play_configured_sound()
        self._sync_alert_exit_timer(restart=True)

    def _alert_enabled(self) -> bool:
        return setting_bool(self._settings, "alert/enabled", False)

    def _main_alert_allowed(self) -> bool:
        return (
            self._alert_enabled()
            and not self._background_mode
            and self.isVisible()
            and not self.isMinimized()
            and self._stack.currentWidget() is self._dashboard
        )

    def _alert_surface_is_visible(self) -> bool:
        return (
            self._popup.is_alert_active()
            or self._dashboard.is_alert_active()
        )

    def _sync_alert_surface(self) -> None:
        """根据前台、最小化和后台状态选择主页面或报警小窗。"""
        if not self._alert_active or not self._alert_enabled():
            self._popup.hide()
            self._dashboard.hide_alert()
            return
        mode = normalize_alert_display_mode(
            self._settings.value("alert/display_mode", "zoom")
        )
        if mode == "sound-only":
            self._popup.hide()
            self._dashboard.hide_alert()
            self._stop_alert_live_preview()
            return
        surface_changed = False
        if self._alert_popup_allowed():
            self._dashboard.hide_alert()
            if not self._popup.is_alert_active():
                self._popup.show_alert(
                    self._current_alert_people,
                    self._current_alert_timestamp,
                )
                self._popup.set_countdown_text(
                    self._current_alert_countdown
                )
                surface_changed = True
        elif self._main_alert_allowed():
            self._popup.hide()
            if not self._dashboard.is_alert_active():
                self._dashboard.show_alert(
                    self._current_alert_people,
                    self._current_alert_timestamp,
                    mode,
                )
                self._dashboard.set_alert_countdown(
                    self._current_alert_countdown
                )
                surface_changed = True
        else:
            self._popup.hide()
            self._dashboard.hide_alert()
        if not surface_changed:
            return
        if mode in ALERT_LIVE_MODES:
            self._start_alert_live_preview()
        elif self._current_alert_image_available:
            self._request_intrusion_image(self._current_alert_event)

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
                and self._alert_active
            ):
                image_data = bytes(reply.readAll())
                if self._popup.is_alert_active():
                    self._popup.set_event_image(image_data)
                if self._dashboard.is_alert_active():
                    self._dashboard.set_alert_image(image_data)
        finally:
            reply.deleteLater()

    def _start_alert_live_preview(self) -> None:
        if (
            not self._server_url
            or not self._alert_active
            or not self._alert_surface_is_visible()
        ):
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
            or not self._alert_active
            or not self._alert_surface_is_visible()
            or normalize_alert_display_mode(
                self._settings.value("alert/display_mode", "zoom")
            )
            not in ALERT_LIVE_MODES
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
                and self._alert_active
                and self._alert_surface_is_visible()
                and normalize_alert_display_mode(
                    self._settings.value("alert/display_mode", "zoom")
                )
                in ALERT_LIVE_MODES
            ):
                image_data = bytes(reply.readAll())
                if self._popup.is_alert_active():
                    self._popup.set_event_image(image_data)
                if self._dashboard.is_alert_active():
                    self._dashboard.set_alert_image(image_data)
        finally:
            reply.deleteLater()

    def _alert_auto_exit_seconds(self) -> int:
        return int(
            self._settings.value(
                "alert/auto_exit_seconds",
                self._settings.value("popup/auto_close_seconds", 10),
            )
        )

    def _alert_rearm_delay_seconds(self) -> int:
        """返回退出告警后等待再次监测的秒数。"""
        try:
            return max(
                int(
                    self._settings.value(
                        "alert/rearm_delay_seconds",
                        0,
                    )
                ),
                0,
            )
        except (TypeError, ValueError):
            return 0

    def _request_alert_rearm(self) -> None:
        """等待期结束且配置仍允许时请求 Server 重新布防。"""
        if (
            self._alert_rearm_timer.isActive()
            or not self._server_url
            or not self._alert_enabled()
        ):
            return
        self._monitor.request_rearm()

    def _schedule_alert_rearm(self) -> None:
        """按等待设置重新布防；设置了等待时间时不受连续监测开关影响。"""
        self._alert_rearm_timer.stop()
        if (
            not self._server_url
            or not self._alert_enabled()
        ):
            return
        delay_seconds = self._alert_rearm_delay_seconds()
        if delay_seconds > 0:
            self._alert_rearm_timer.start(delay_seconds * 1000)
            return
        if setting_bool(
            self._settings,
            "alert/continuous",
            False,
        ):
            self._request_alert_rearm()

    def _sync_alert_exit_timer(self, restart: bool = False) -> None:
        if not self._alert_active:
            self._alert_exit_timer.stop()
            self._alert_countdown_timer.stop()
            self._set_alert_countdown("")
            return
        continuous_display = setting_bool(
            self._settings,
            "alert/continuous_display",
            False,
        )
        if continuous_display and self._latest_person_present:
            self._alert_exit_timer.stop()
            self._alert_countdown_timer.stop()
            self._set_alert_countdown("等待人员离开")
            return
        if self._alert_exit_timer.isActive() and not restart:
            self._update_alert_countdown()
            return
        self._alert_exit_timer.stop()
        self._alert_countdown_timer.stop()
        auto_exit = self._alert_auto_exit_seconds()
        if auto_exit > 0:
            self._alert_exit_timer.start(auto_exit * 1000)
            self._set_alert_countdown(
                f"{auto_exit} 秒后退出告警"
            )
            self._alert_countdown_timer.start()
        else:
            self._set_alert_countdown("手动退出")

    def _update_alert_countdown(self) -> None:
        if not self._alert_active:
            self._alert_countdown_timer.stop()
            self._set_alert_countdown("")
            return
        remaining_ms = self._alert_exit_timer.remainingTime()
        if remaining_ms < 0:
            return
        remaining_seconds = max(
            math.ceil(remaining_ms / 1000),
            0,
        )
        self._set_alert_countdown(
            f"{remaining_seconds} 秒后退出告警"
        )

    def _set_alert_countdown(self, text: str) -> None:
        self._current_alert_countdown = text
        self._dashboard.set_alert_countdown(text)
        self._popup.set_countdown_text(text)

    def _auto_exit_current_alert(self) -> None:
        if self._alert_active:
            self._dismiss_current_alert()

    def _on_popup_dismissed(self) -> None:
        self._dismiss_current_alert()

    def _dismiss_current_alert(self, rearm: bool = True) -> None:
        self._alert_active = False
        self._alert_exit_timer.stop()
        self._alert_countdown_timer.stop()
        self._set_alert_countdown("")
        self._popup.hide()
        self._dashboard.hide_alert()
        self._stop_alert_live_preview()
        self._stop_sound()
        if (
            rearm
            and self._server_url
            and self._alert_enabled()
        ):
            self._schedule_alert_rearm()

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
        sound_path: Path | None = None
        if mode == "app-default":
            sound_path = app_default_sound_path()
        elif mode == "custom" and path:
            custom_path = Path(path)
            if custom_path.is_file():
                sound_path = custom_path
        if (
            sound_path is not None
            and self._audio_output is not None
            and self._player is not None
        ):
            self._audio_output.setVolume(max(0, min(volume, 100)) / 100.0)
            self._player.setSource(QUrl.fromLocalFile(str(sound_path)))
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
        """按四字按钮完整显示的紧凑宽度设置启动尺寸。"""
        self._topbar.ensurePolished()
        self._dashboard.ensurePolished()
        dashboard_hint = self._dashboard.minimumSizeHint()
        topbar_hint = self._topbar.sizeHint()
        width = self._dashboard.preferred_single_row_width()
        self.resize(
            width,
            max(
                dashboard_hint.height() + topbar_hint.height() + 100,
                520,
            ),
        )

    def minimumSizeHint(self) -> QSize:
        """不锁定主窗口尺寸，由流式布局负责在窄窗口中换行。"""
        return QSize(0, 0)

    def _set_theme_mode(self, mode: str) -> None:
        self._theme_mode = (
            mode if mode in THEME_MODES else "follow-system"
        )
        self._settings.setValue(
            "appearance/theme_mode",
            self._theme_mode,
        )
        self._settings.sync()
        self._update_theme_button()
        self._apply_theme(self._resolved_theme())

    def _toggle_theme(self) -> None:
        current_index = THEME_MODES.index(self._theme_mode)
        self._set_theme_mode(
            THEME_MODES[(current_index + 1) % len(THEME_MODES)]
        )

    def _on_system_theme_changed(self, _scheme: Any) -> None:
        if self._theme_mode == "follow-system":
            self._apply_theme(self._resolved_theme())

    def _update_theme_button(self) -> None:
        self._theme_button.setText(
            THEME_LABELS.get(self._theme_mode, "跟随系统")
        )

    def hide_to_tray(self) -> None:
        if self._tray is None:
            detail = (
                "macOS 原生状态栏组件未加载，请安装 requirements.txt "
                "中的 Cocoa 依赖后重新启动。"
                if sys.platform == "darwin"
                else "当前系统未提供托盘区域，主窗口将保持打开。"
            )
            QMessageBox.information(
                self,
                "无法后台运行",
                detail,
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
        self._sync_alert_surface()
        if self._alert_active:
            self._start_alert_live_preview()
        self.raise_()
        self.activateWindow()

    def _alert_popup_allowed(self) -> bool:
        return self._alert_enabled() and (
            self._background_mode or self.isMinimized()
        )

    def _set_background_mode(self, enabled: bool) -> None:
        self._background_mode = enabled
        set_macos_dock_icon_visible(not enabled)
        if enabled and self._server_url and self._alert_popup_allowed():
            self._request_alert_rearm()
        self._sync_alert_surface()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() != QEvent.Type.WindowStateChange:
            return
        if self.isMinimized():
            self._dashboard.set_view_active(False)
            self._sync_alert_surface()
            if self._server_url and self._alert_popup_allowed():
                self._request_alert_rearm()
            return
        if not self._background_mode:
            if self._stack.currentWidget() is self._dashboard:
                self._dashboard.set_view_active(self.isVisible())
            self._sync_alert_surface()
            if self._alert_active:
                self._start_alert_live_preview()

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Context:
            if self._tray_menu is not None:
                self._tray_menu.popup(QCursor.pos())
            return
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.show_main_window()

    def quit_application(self) -> None:
        self._quitting = True
        self._settings.sync()
        self._alert_exit_timer.stop()
        self._alert_rearm_timer.stop()
        self._alert_countdown_timer.stop()
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
        self._update_theme_button()
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
            #monitorIcon[statusDensity="compact"] {{
                border-radius: 29px;
                font-size: 27px;
            }}
            #monitorIcon[statusDensity="tiny"] {{
                border-radius: 23px;
                font-size: 22px;
            }}
            #monitorTitle {{
                color: {colors["text"]};
                font-size: 29px;
                font-weight: 800;
            }}
            #monitorTitle[statusDensity="compact"] {{
                font-size: 23px;
            }}
            #monitorTitle[statusDensity="tiny"] {{
                font-size: 19px;
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
            #monitorDetail[statusDensity="compact"] {{
                font-size: 13px;
            }}
            #monitorDetail[statusDensity="tiny"],
            #dashboardSource[statusDensity="tiny"] {{
                font-size: 11px;
            }}
            #previewLabel {{
                color: {colors["muted"]};
                background: {colors["page"]};
                border: none;
                border-radius: 0;
                font-size: 16px;
            }}
            #mainAlertPanel {{
                color: white;
                background: #05080c;
                border: none;
            }}
            #mainAlertPanel[redAlert="true"] {{
                background: #e00018;
            }}
            #mainAlertImage {{
                color: #f6c8cd;
                background: #0b0d10;
                border: 3px solid #ff4152;
                border-radius: 9px;
                font-size: 15px;
            }}
            #mainAlertPanel[redAlert="true"] #mainAlertImage {{
                color: white;
                border-color: white;
            }}
            #mainAlertTitle {{
                color: white;
                min-height: 32px;
                padding: 0;
                background: transparent;
                border: none;
                font-size: 22px;
                font-weight: 800;
            }}
            #mainAlertDetail {{
                color: white;
                padding: 0 10px;
                background: transparent;
                border: none;
                font-weight: 700;
            }}
            #mainAlertCountdown {{
                color: white;
                padding: 0 9px;
                background: transparent;
                border: none;
                font-weight: 700;
            }}
            #mainAlertDetail[overlaySize="small"],
            #mainAlertCountdown[overlaySize="small"] {{
                border-radius: 10px;
            }}
            #mainAlertDetail[overlaySize="medium"],
            #mainAlertCountdown[overlaySize="medium"] {{
                border-radius: 12px;
            }}
            #mainAlertDetail[overlaySize="large"],
            #mainAlertCountdown[overlaySize="large"] {{
                border-radius: 15px;
            }}
            #mainAlertExitButton {{
                color: white;
                background: #20242b;
                border: 1px solid rgba(255, 255, 255, 210);
                border-radius: 8px;
                font-weight: 800;
            }}
            #mainAlertExitButton:hover {{
                background: rgba(190, 20, 32, 235);
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
            #dashboardControls QPushButton {{
                padding: 0 4px;
                font-size: 13px;
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
                min-height: 28px;
                color: {colors["text"]};
                background: {colors["surface"]};
                border: none;
                border-bottom: 1px solid {colors["border"]};
            }}
            #mainTopbar QPushButton {{
                min-height: 22px;
                padding: 0 4px;
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
            #mainTopbar QPushButton[sectionStart="true"] {{
                padding-left: 9px;
                border-left: 1px solid {colors["muted"]};
                border-radius: 0;
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
                min-height: 22px;
                padding: 0 7px 0 0;
                font-weight: 700;
            }}
            #toolbarTitle {{
                color: {colors["text"]};
                min-height: 22px;
                padding: 0;
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
        self._dashboard._controls.sync_text_fit_widths()
        self._dashboard._sync_controls_card_visibility()
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
