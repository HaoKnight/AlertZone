"""原生 Desktop 仪表板测试。"""

import base64
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, QSettings, QSize, Qt, QTimer
from PySide6.QtGui import QIcon, QMouseEvent, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QPushButton,
    QSystemTrayIcon,
    QWidget,
)

from src.AlertZone_Desktop import (
    WINDOW_TITLE,
    AlertPopup,
    AlertSettingsDialog,
    CloseActionDialog,
    ConnectionPage,
    CurrentPageStack,
    MainWindow,
    MarqueeLabel,
    NativeDashboard,
    OtherSettingsDialog,
    app_default_sound_path,
)

APP = QApplication.instance() or QApplication([])


class _PreviewHandler(BaseHTTPRequestHandler):
    request_count = 0
    image = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
        "AAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )

    def do_GET(self) -> None:
        if not self.path.startswith("/api/preview.jpg"):
            self.send_error(404)
            return
        type(self).request_count += 1
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(self.image)))
        self.end_headers()
        self.wfile.write(self.image)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class NativeDashboardTests(unittest.TestCase):
    def test_main_window_title_uses_server_credit(self) -> None:
        self.assertEqual(
            WINDOW_TITLE,
            "AlertZone Desktop · 服务端 · ©H-Knight",
        )

    def test_connection_page_cancel_button_emits_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(
                f"{temp_dir}/settings.ini", QSettings.Format.IniFormat
            )
            page = ConnectionPage(settings)
            cancel_requests = []
            page.cancel_requested.connect(
                lambda: cancel_requests.append(True)
            )

            self.assertEqual(page.cancel_button.text(), "取消")
            page.cancel_button.click()
            self.assertEqual(cancel_requests, [True])

    def test_tray_alert_switch_syncs_with_home_button(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(
                f"{temp_dir}/settings.ini", QSettings.Format.IniFormat
            )
            dashboard = NativeDashboard(settings)

            class FakeWindow:
                _dashboard = dashboard

            window = FakeWindow()
            MainWindow._on_tray_alert_toggled(window, True)
            self.assertTrue(dashboard.alert_enabled_button.isChecked())
            self.assertTrue(
                settings.value("alert/enabled", False, type=bool)
            )

            class FakeAction:
                checked = False

                @staticmethod
                def blockSignals(_blocked: bool) -> None:
                    return

                def setChecked(self, checked: bool) -> None:
                    self.checked = checked

            class FakeMainWindow:
                _tray_alert_action = FakeAction()
                _server_url = ""
                _alert_rearm_timer = type(
                    "FakeTimer",
                    (),
                    {"stop": lambda self: None},
                )()

            fake_main_window = FakeMainWindow()
            MainWindow._on_alert_enabled_changed(
                fake_main_window,
                True,
            )
            self.assertTrue(
                fake_main_window._tray_alert_action.checked
            )

    def test_tray_left_click_restores_and_right_click_opens_menu(self) -> None:
        class FakeMenu:
            popup_count = 0

            def popup(self, _position: object) -> None:
                self.popup_count += 1

        class FakeWindow:
            _tray_menu = FakeMenu()
            show_count = 0

            def show_main_window(self) -> None:
                self.show_count += 1

        window = FakeWindow()
        MainWindow._tray_activated(
            window,
            QSystemTrayIcon.ActivationReason.Trigger,
        )
        self.assertEqual(window.show_count, 1)
        self.assertEqual(window._tray_menu.popup_count, 0)

        MainWindow._tray_activated(
            window,
            QSystemTrayIcon.ActivationReason.Context,
        )
        self.assertEqual(window.show_count, 1)
        self.assertEqual(window._tray_menu.popup_count, 1)

    def test_marquee_scrolls_only_when_text_does_not_fit(self) -> None:
        label = MarqueeLabel("检测到 12 人进入监控区域")
        label.resize(70, 28)
        label.show()
        label.refresh_marquee()
        self.assertTrue(label._scroll_timer.isActive())
        self.assertEqual(label._scroll_timer.interval(), 20)
        self.assertNotEqual(label.displayed_text(), label.text())
        previous_x = label._text_label.x()
        label._advance_scroll()
        self.assertEqual(label._text_label.x(), previous_x - 1)

        label.resize(500, 28)
        label.refresh_marquee()
        self.assertFalse(label._scroll_timer.isActive())
        self.assertEqual(label.displayed_text(), label.text())
        label.hide()

    def test_stack_minimum_width_follows_current_page_only(self) -> None:
        class HintWidget(QWidget):
            def __init__(self, width: int) -> None:
                super().__init__()
                self._width = width

            def minimumSizeHint(self) -> QSize:
                return QSize(self._width, 100)

        stack = CurrentPageStack()
        wide_page = HintWidget(420)
        compact_page = HintWidget(240)
        stack.addWidget(wide_page)
        stack.addWidget(compact_page)

        self.assertEqual(stack.minimumSizeHint().width(), 420)
        stack.setCurrentWidget(compact_page)
        self.assertEqual(stack.minimumSizeHint().width(), 240)

    def test_close_dialog_offers_server_style_actions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(
                f"{temp_dir}/settings.ini", QSettings.Format.IniFormat
            )
            parent = NativeDashboard(settings)
            source_pixmap = QPixmap(256, 256)
            source_pixmap.fill(Qt.GlobalColor.blue)
            dialog = CloseActionDialog(
                parent,
                False,
                QIcon(source_pixmap),
            )
            background_button = dialog.findChild(
                QPushButton, "closeBackgroundButton"
            )
            exit_button = dialog.findChild(QPushButton, "closeExitButton")
            cancel_button = dialog.findChild(
                QPushButton, "closeCancelButton"
            )
            icon_label = dialog.findChild(QLabel, "closeDialogIcon")
            self.assertEqual(background_button.text(), "后台静默运行")
            self.assertEqual(exit_button.text(), "退出应用程序")
            self.assertEqual(cancel_button.text(), "取消")
            self.assertEqual(icon_label.width(), 60)
            self.assertEqual(icon_label.height(), 60)
            self.assertGreaterEqual(
                icon_label.pixmap().deviceIndependentSize().width(),
                56,
            )
            background_button.click()
            self.assertEqual(dialog.selected_action, "background")

    def test_background_mode_respects_alert_switch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(
                f"{temp_dir}/settings.ini", QSettings.Format.IniFormat
            )

            class FakePopup:
                hidden = False

                def hide(self) -> None:
                    self.hidden = True

            class FakeMonitor:
                rearm_count = 0

                def request_rearm(self) -> None:
                    self.rearm_count += 1

            class FakeTimer:
                @staticmethod
                def stop() -> None:
                    return

                @staticmethod
                def isActive() -> bool:
                    return False

            class FakeWindow:
                _settings = settings
                _server_url = "http://127.0.0.1:8765"
                _popup = FakePopup()
                _monitor = FakeMonitor()
                _alert_exit_timer = FakeTimer()
                _alert_rearm_timer = FakeTimer()
                _background_mode = False
                _alert_popup_allowed = MainWindow._alert_popup_allowed
                _alert_enabled = MainWindow._alert_enabled
                _request_alert_rearm = MainWindow._request_alert_rearm
                sync_count = 0

                @staticmethod
                def isMinimized() -> bool:
                    return False

                @staticmethod
                def _sync_alert_surface() -> None:
                    FakeWindow.sync_count += 1

            window = FakeWindow()
            with patch(
                "src.AlertZone_Desktop.set_macos_dock_icon_visible"
            ) as dock_visibility:
                MainWindow._set_background_mode(window, True)
                self.assertTrue(window._background_mode)
                self.assertFalse(
                    settings.value("alert/enabled", False, type=bool)
                )
                self.assertEqual(window._monitor.rearm_count, 0)

                settings.setValue("alert/enabled", True)
                MainWindow._set_background_mode(window, True)
                self.assertEqual(window._monitor.rearm_count, 1)

                MainWindow._set_background_mode(window, False)
                self.assertEqual(window.sync_count, 3)
                self.assertEqual(
                    [call.args[0] for call in dock_visibility.call_args_list],
                    [False, False, True],
                )

    def test_home_uses_dedicated_setting_buttons(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(
                f"{temp_dir}/settings.ini", QSettings.Format.IniFormat
            )
            dashboard = NativeDashboard(settings)
            self.assertEqual(
                dashboard.alert_settings_button.text(), "告警设置"
            )
            self.assertEqual(
                dashboard.popup_settings_button.text(), "弹窗位置"
            )
            dashboard.set_alert_display_mode("sound-only")
            self.assertFalse(dashboard.popup_settings_button.isEnabled())
            dashboard.set_alert_display_mode("zoom")
            self.assertTrue(dashboard.popup_settings_button.isEnabled())
            self.assertEqual(
                dashboard.other_settings_button.text(), "其他配置"
            )
            self.assertEqual(
                dashboard.alert_enabled_button.text(), "启用告警"
            )
            self.assertEqual(
                dashboard.continuous_button.text(), "连续监测"
            )
            self.assertFalse(hasattr(dashboard, "alert_display_combo"))
            self.assertFalse(hasattr(dashboard, "alert_button"))
            source_label = dashboard.findChild(QLabel, "dashboardSource")
            self.assertEqual(
                source_label.text(),
                "数据由 AlertZone Server API 提供",
            )
            self.assertIsNone(
                dashboard.findChild(QLabel, "dashboardFooter")
            )

    def test_narrow_window_wraps_controls_without_minimum_size(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(
                f"{temp_dir}/settings.ini", QSettings.Format.IniFormat
            )
            dashboard = NativeDashboard(settings)
            flow = dashboard._controls_layout
            wide_width = flow.preferred_single_row_width()
            dashboard._controls.sync_text_fit_widths()
            compact_width = dashboard._controls.minimum_full_width()
            for button in dashboard._controls._buttons:
                self.assertGreaterEqual(
                    button.minimumWidth(),
                    button.fontMetrics().horizontalAdvance(
                        button.text()
                    )
                    + 10,
                )

            self.assertGreater(
                flow.heightForWidth(wide_width // 2),
                flow.heightForWidth(wide_width),
            )
            self.assertLessEqual(compact_width, wide_width)
            self.assertEqual(
                flow.heightForWidth(compact_width),
                flow.heightForWidth(wide_width),
            )
            self.assertGreater(
                flow.heightForWidth(compact_width - 1),
                flow.heightForWidth(compact_width),
            )
            self.assertEqual(dashboard.preview_label.minimumWidth(), 0)
            self.assertEqual(
                MainWindow.minimumSizeHint(None),
                QSize(0, 0),
            )

    def test_short_monitor_uses_compact_status_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(
                f"{temp_dir}/settings.ini", QSettings.Format.IniFormat
            )
            dashboard = NativeDashboard(settings)

            dashboard.monitor_stack.resize(400, 190)
            dashboard._sync_status_density()
            self.assertEqual(dashboard._status_density, "compact")
            self.assertEqual(dashboard.status_icon.width(), 58)
            self.assertEqual(
                dashboard.status_title.property("statusDensity"),
                "compact",
            )
            self.assertEqual(
                dashboard._idle_layout.contentsMargins().top(),
                6,
            )

            dashboard.monitor_stack.resize(400, 300)
            dashboard._sync_status_density()
            self.assertEqual(dashboard._status_density, "normal")
            self.assertEqual(dashboard.status_icon.width(), 76)

    def test_alert_overlays_follow_actual_image_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(
                f"{temp_dir}/settings.ini", QSettings.Format.IniFormat
            )
            dashboard = NativeDashboard(settings)
            canvas = dashboard.alert_image
            canvas.resize(300, 300)
            canvas._source_pixmap = QPixmap(400, 200)
            canvas.configure_action("确定位置和大小", True)
            canvas._position_overlays()

            image_rect = canvas.displayed_pixmap_rect()
            self.assertEqual(image_rect.top(), 75)
            self.assertEqual(image_rect.height(), 150)
            compact_font_size = canvas.detail_label.font().pixelSize()
            compact_action_font_size = (
                canvas.exit_button.font().pixelSize()
            )
            self.assertEqual(compact_font_size, 10)
            self.assertEqual(compact_action_font_size, 12)
            self.assertLessEqual(
                canvas.exit_button.width(),
                image_rect.width(),
            )
            self.assertGreater(
                canvas.exit_button.geometry().center().y(),
                image_rect.center().y(),
            )
            self.assertLess(
                canvas.exit_button.geometry().bottom(),
                canvas.detail_label.geometry().top(),
            )
            self.assertEqual(
                canvas.detail_label.property("overlaySize"),
                "small",
            )
            self.assertIn(
                "font-size: 10px",
                canvas.detail_label.styleSheet(),
            )
            self.assertEqual(
                canvas.countdown_label.geometry().top(),
                image_rect.top() + 8,
            )
            self.assertEqual(
                canvas.countdown_label.geometry().center().x(),
                image_rect.center().x(),
            )
            for overlay in (
                canvas.detail_label,
                canvas.countdown_label,
                canvas.exit_button,
            ):
                self.assertGreaterEqual(
                    overlay.geometry().top(),
                    image_rect.top(),
                )
                self.assertLessEqual(
                    overlay.geometry().bottom(),
                    image_rect.bottom(),
                )

            canvas.resize(1000, 600)
            canvas._source_pixmap = QPixmap(1000, 600)
            canvas._position_overlays()
            self.assertEqual(
                canvas.detail_label.font().pixelSize(),
                20,
            )
            self.assertEqual(
                canvas.exit_button.font().pixelSize(),
                26,
            )
            self.assertGreater(
                canvas.exit_button.height(),
                36,
            )
            self.assertEqual(
                canvas.detail_label.property("overlaySize"),
                "large",
            )
            self.assertIn(
                "font-size: 20px",
                canvas.detail_label.styleSheet(),
            )
            self.assertGreater(
                canvas.detail_label.height(),
                compact_font_size + 12,
            )
            self.assertEqual(
                canvas.detail_label.displayed_text(),
                canvas.detail_label.text(),
            )

    def test_all_controls_reveal_from_topbar_hover_when_narrow(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(
                f"{temp_dir}/settings.ini", QSettings.Format.IniFormat
            )
            dashboard = NativeDashboard(settings)
            buttons = [
                dashboard.alert_enabled_button,
                dashboard.continuous_button,
                dashboard.preview_button,
                dashboard.popup_settings_button,
                dashboard.alert_settings_button,
                dashboard.other_settings_button,
            ]

            dashboard.resize(360, 600)
            dashboard._controls_trigger_hovered = False
            dashboard._controls_panel_hovered = False
            dashboard._sync_controls_card_visibility()
            self.assertTrue(dashboard._controls_card.isHidden())
            self.assertTrue(
                all(not button.isHidden() for button in buttons)
            )

            dashboard.set_controls_trigger_hovered(True)
            self.assertFalse(dashboard._controls_card.isHidden())

            dashboard.set_controls_trigger_hovered(False)
            dashboard._sync_controls_card_visibility()
            self.assertTrue(dashboard._controls_card.isHidden())

            dashboard.resize(
                dashboard.preferred_single_row_width() + 20,
                600,
            )
            dashboard._sync_controls_card_visibility()
            self.assertFalse(dashboard._controls_card.isHidden())

    def test_web_style_status_icon_and_popup_modes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(
                f"{temp_dir}/settings.ini", QSettings.Format.IniFormat
            )
            dashboard = NativeDashboard(settings)
            self.assertEqual(dashboard.status_icon.text(), "⌁")
            self.assertEqual(dashboard.status_icon.width(), 76)
            dashboard.update_status(
                {
                    "detection_running": True,
                    "person_present": False,
                    "people_count": 0,
                    "presence_seconds": 0,
                    "fps": 12.0,
                }
            )
            self.assertEqual(dashboard.status_icon.text(), "✓")
            dashboard.update_status(
                {
                    "detection_running": True,
                    "person_present": True,
                    "people_count": 1,
                    "presence_seconds": 1,
                    "fps": 12.0,
                }
            )
            self.assertEqual(dashboard.status_icon.text(), "!")

            popup = AlertPopup(settings)
            self.assertEqual(popup.minimumSize(), QSize(0, 0))
            self.assertEqual(popup.minimumSizeHint(), QSize(0, 0))
            popup.set_display_mode("red")
            self.assertEqual(popup.display_mode(), "zoom-red")
            for mode in ("zoom", "zoom-red", "live", "live-red"):
                popup.set_display_mode(mode)
                self.assertFalse(popup._image.isHidden())
            popup.show_alert(2, "2026-07-28 10:30:00")
            APP.processEvents()
            self.assertEqual(popup.minimumSize(), QSize(0, 0))
            popup.set_countdown_text("10 秒后退出告警")
            self.assertEqual(
                popup._title.text(),
                "⚠️⚠️⚠️ 警告 ⚠️⚠️⚠️",
            )
            self.assertIs(popup._title.parent(), popup)
            self.assertIs(popup._detail.parent(), popup._image)
            self.assertIs(popup._countdown.parent(), popup._image)
            self.assertIs(popup._button.parent(), popup._image)
            self.assertEqual(
                popup._detail.text(),
                "检测到 2 人进入",
            )
            self.assertEqual(
                popup._countdown.text(),
                "10 秒后退出告警",
            )
            self.assertEqual(popup._button.text(), "退出告警")
            self.assertTrue(popup._button.isHidden())
            popup.hide()

            popup.show_placement_preview()
            self.assertEqual(popup._title.text(), "弹窗位置")
            self.assertEqual(
                popup._button.text(),
                "确定位置和大小",
            )
            self.assertFalse(popup._button.isHidden())
            popup.hide()

    def test_enabled_alert_can_render_in_main_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(
                f"{temp_dir}/settings.ini", QSettings.Format.IniFormat
            )
            dashboard = NativeDashboard(settings)
            dismissed = []
            dashboard.alert_dismiss_requested.connect(
                lambda: dismissed.append(True)
            )

            dashboard.show_alert(
                2,
                "2026-07-28 10:30:00",
                "live-red",
            )

            self.assertTrue(dashboard.is_alert_active())
            self.assertEqual(
                dashboard.monitor_stack.currentWidget(),
                dashboard.alert_panel,
            )
            self.assertTrue(
                dashboard.alert_panel.property("redAlert")
            )
            self.assertEqual(
                dashboard.alert_title.text(),
                "⚠️⚠️⚠️ 警告 ⚠️⚠️⚠️",
            )
            self.assertIn("检测到 2 人", dashboard.alert_detail.text())
            self.assertIs(
                dashboard.alert_detail.parent(),
                dashboard.alert_image,
            )
            dashboard.set_alert_countdown("10 秒后退出告警")
            self.assertEqual(
                dashboard.alert_countdown.text(),
                "10 秒后退出告警",
            )
            self.assertIs(
                dashboard.alert_countdown.parent(),
                dashboard.alert_image,
            )
            self.assertTrue(dashboard.alert_exit_button.isHidden())
            hover_position = QPointF(
                dashboard.alert_image.width() / 2,
                dashboard.alert_image.height() / 2,
            )
            hover_event = QMouseEvent(
                QEvent.Type.MouseMove,
                hover_position,
                hover_position,
                hover_position,
                Qt.MouseButton.NoButton,
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
            )
            dashboard.alert_image.mouseMoveEvent(hover_event)
            self.assertFalse(dashboard.alert_exit_button.isHidden())
            dashboard.alert_exit_button.click()
            self.assertEqual(dismissed, [True])
            dashboard.hide_alert()
            self.assertFalse(dashboard.is_alert_active())

    def test_intrusion_is_not_ignored_while_main_window_is_open(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(
                f"{temp_dir}/settings.ini", QSettings.Format.IniFormat
            )
            settings.setValue("alert/enabled", True)

            class FakeTimer:
                stopped = False

                def stop(self) -> None:
                    self.stopped = True

                @staticmethod
                def isActive() -> bool:
                    return False

            class FakeWindow:
                _settings = settings
                _alert_exit_timer = FakeTimer()
                _alert_rearm_timer = FakeTimer()
                _latest_person_present = False
                _current_alert_event = 0
                _current_alert_people = 1
                _current_alert_timestamp = ""
                _alert_active = False
                surface_syncs = 0
                sound_plays = 0
                exit_syncs = 0
                _alert_enabled = MainWindow._alert_enabled

                def _sync_alert_surface(self) -> None:
                    self.surface_syncs += 1
                    self._sync_alert_sound()

                def _sync_alert_sound(self) -> None:
                    self.sound_plays += 1

                @staticmethod
                def _start_alert_live_preview() -> None:
                    return

                @staticmethod
                def _request_intrusion_image(_event_id: int) -> None:
                    return

                def _sync_alert_exit_timer(
                    self,
                    restart: bool = False,
                ) -> None:
                    self.exit_syncs += int(restart)

            window = FakeWindow()
            MainWindow._show_intrusion(
                window,
                {
                    "intrusion_event_id": 7,
                    "intrusion_people_count": 2,
                    "person_present": True,
                },
            )

            self.assertTrue(window._alert_active)
            self.assertEqual(window._current_alert_event, 7)
            self.assertEqual(window._current_alert_people, 2)
            self.assertEqual(window.surface_syncs, 1)
            self.assertEqual(window.sound_plays, 1)
            self.assertEqual(window.exit_syncs, 1)

    def test_intrusion_is_ignored_during_rearm_wait(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(
                f"{temp_dir}/settings.ini", QSettings.Format.IniFormat
            )
            settings.setValue("alert/enabled", True)

            class ActiveTimer:
                @staticmethod
                def isActive() -> bool:
                    return True

            class FakeWindow:
                _settings = settings
                _alert_rearm_timer = ActiveTimer()
                _alert_active = False
                _alert_enabled = MainWindow._alert_enabled

            window = FakeWindow()
            MainWindow._show_intrusion(
                window,
                {
                    "intrusion_event_id": 8,
                    "intrusion_people_count": 1,
                },
            )
            self.assertFalse(window._alert_active)

    def test_sound_only_alert_hides_all_visual_surfaces(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(
                f"{temp_dir}/settings.ini", QSettings.Format.IniFormat
            )
            settings.setValue("alert/enabled", True)
            settings.setValue("alert/display_mode", "sound-only")

            class HiddenSurface:
                hidden = False

                def hide(self) -> None:
                    self.hidden = True

                def hide_alert(self) -> None:
                    self.hidden = True

            class FakeWindow:
                _settings = settings
                _alert_active = True
                _popup = HiddenSurface()
                _dashboard = HiddenSurface()
                _alert_enabled = MainWindow._alert_enabled
                preview_stopped = False
                sound_synced = False

                def _stop_alert_live_preview(self) -> None:
                    self.preview_stopped = True

                def _sync_alert_sound(self) -> None:
                    self.sound_synced = True

            window = FakeWindow()
            MainWindow._sync_alert_surface(window)
            self.assertTrue(window._popup.hidden)
            self.assertTrue(window._dashboard.hidden)
            self.assertTrue(window.preview_stopped)
            self.assertTrue(window.sound_synced)

    def test_sound_follows_alert_surface_and_exit_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(
                f"{temp_dir}/settings.ini", QSettings.Format.IniFormat
            )
            settings.setValue("alert/enabled", True)
            settings.setValue("alert/display_mode", "zoom")
            settings.setValue("sound/mode", "app-default")

            class FakeSurface:
                active = False

                def is_alert_active(self) -> bool:
                    return self.active

            class FakeWindow:
                _settings = settings
                _alert_active = True
                _current_alert_event = 12
                _alert_sound_event = None
                _alert_sound_signature = None
                _popup = FakeSurface()
                _dashboard = FakeSurface()
                _alert_enabled = MainWindow._alert_enabled
                _alert_surface_is_visible = (
                    MainWindow._alert_surface_is_visible
                )
                played = 0
                stopped = 0

                def _play_configured_sound(self) -> None:
                    self.played += 1

                def _stop_sound(self) -> None:
                    self.stopped += 1

            window = FakeWindow()

            # 视觉告警尚未真正显示时，不提前发出声音。
            MainWindow._sync_alert_sound(window)
            self.assertEqual(window.played, 0)

            window._dashboard.active = True
            MainWindow._sync_alert_sound(window)
            MainWindow._sync_alert_sound(window)
            self.assertEqual(window.played, 1)
            self.assertEqual(window._alert_sound_event, 12)

            # 自动或手动退出后，声音与告警同时结束。
            window._alert_active = False
            MainWindow._sync_alert_sound(window)
            self.assertEqual(window.stopped, 1)
            self.assertIsNone(window._alert_sound_event)

            # “仅提示音”以声音本身作为告警，不要求视觉页面存在。
            settings.setValue("alert/display_mode", "sound-only")
            window._alert_active = True
            window._current_alert_event = 13
            window._dashboard.active = False
            MainWindow._sync_alert_sound(window)
            self.assertEqual(window.played, 2)

    def test_continuous_display_does_not_repeat_sound(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(
                f"{temp_dir}/settings.ini", QSettings.Format.IniFormat
            )
            settings.setValue("alert/enabled", True)
            settings.setValue("alert/continuous_display", True)

            class InactiveTimer:
                @staticmethod
                def isActive() -> bool:
                    return False

            class FakeWindow:
                _settings = settings
                _alert_rearm_timer = InactiveTimer()
                _alert_active = True
                _current_alert_event = 21
                _latest_person_present = True
                _alert_enabled = MainWindow._alert_enabled
                exit_syncs = 0
                surface_syncs = 0
                sound_plays = 0

                def _sync_alert_exit_timer(self) -> None:
                    self.exit_syncs += 1

                def _sync_alert_surface(self) -> None:
                    self.surface_syncs += 1

                def _play_configured_sound(self) -> None:
                    self.sound_plays += 1

            window = FakeWindow()
            MainWindow._show_intrusion(
                window,
                {
                    "intrusion_event_id": 22,
                    "intrusion_people_count": 2,
                    "person_present": True,
                },
            )

            self.assertEqual(window._current_alert_event, 21)
            self.assertTrue(window._latest_person_present)
            self.assertEqual(window.exit_syncs, 1)
            self.assertEqual(window.surface_syncs, 0)
            self.assertEqual(window.sound_plays, 0)

    def test_alert_frames_do_not_expand_the_window_size_hint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(
                f"{temp_dir}/settings.ini", QSettings.Format.IniFormat
            )
            dashboard = NativeDashboard(settings)
            preferred_size = dashboard.alert_image.sizeHint()

            for width, height in ((420, 260), (700, 480), (360, 220)):
                dashboard.alert_image.resize(width, height)
                self.assertTrue(
                    dashboard.set_alert_image(_PreviewHandler.image)
                )
                self.assertEqual(
                    dashboard.alert_image.sizeHint(),
                    preferred_size,
                )
                self.assertEqual(
                    dashboard.alert_image.minimumSizeHint(),
                    QSize(0, 0),
                )

    def test_alert_dialog_has_no_theme_or_enable_switch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(
                f"{temp_dir}/settings.ini", QSettings.Format.IniFormat
            )
            dialog = AlertSettingsDialog(settings)
            self.assertEqual(dialog.windowTitle(), "告警设置")
            self.assertEqual(dialog.alert_display_mode.count(), 5)
            self.assertEqual(
                [
                    dialog.alert_display_mode.itemData(index)
                    for index in range(dialog.alert_display_mode.count())
                ],
                [
                    "zoom",
                    "live",
                    "zoom-red",
                    "live-red",
                    "sound-only",
                ],
            )
            self.assertEqual(
                dialog.alert_display_mode.itemText(0),
                "放大人物",
            )
            self.assertEqual(
                dialog.alert_display_mode.itemText(
                    dialog.alert_display_mode.findData("live-red")
                ),
                "全屏红色且实时预览",
            )
            self.assertEqual(
                dialog.alert_display_mode.itemText(
                    dialog.alert_display_mode.findData("sound-only")
                ),
                "仅提示音提醒",
            )
            self.assertGreaterEqual(
                dialog.alert_display_mode.minimumWidth(),
                240,
            )
            self.assertEqual(dialog.auto_exit_seconds.currentData(), 10)
            self.assertEqual(dialog.rearm_delay_seconds.currentData(), 0)
            self.assertTrue(dialog.rearm_custom_seconds.isHidden())
            self.assertTrue(dialog.rearm_custom_unit.isHidden())
            self.assertFalse(dialog.continuous_alert_display.isChecked())
            self.assertEqual(
                [
                    dialog.auto_exit_seconds.itemData(index)
                    for index in range(dialog.auto_exit_seconds.count())
                ],
                [0, 2, 5, 10, 15, -1],
            )
            self.assertEqual(dialog.auto_exit_seconds.itemText(0), "立即")
            self.assertEqual(
                dialog.auto_exit_seconds.itemText(
                    dialog.auto_exit_seconds.count() - 1
                ),
                "∞",
            )
            self.assertEqual(
                [
                    dialog.rearm_delay_seconds.itemData(index)
                    for index in range(dialog.rearm_delay_seconds.count())
                ],
                [0, 5, 10, 20, 30, 60, "custom"],
            )
            self.assertFalse(hasattr(dialog, "continuous_enabled"))
            self.assertFalse(hasattr(dialog, "theme_mode"))
            self.assertFalse(hasattr(dialog, "alert_enabled"))
            dialog.continuous_alert_display.setChecked(True)
            dialog.rearm_delay_seconds.setCurrentIndex(
                dialog.rearm_delay_seconds.findData(20)
            )
            dialog._save()
            self.assertTrue(
                settings.value(
                    "alert/continuous_display",
                    False,
                    type=bool,
                )
            )
            self.assertEqual(
                settings.value(
                    "alert/rearm_delay_seconds",
                    0,
                    type=int,
                ),
                20,
            )

    def test_continuous_alert_display_waits_for_person_to_leave(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(
                f"{temp_dir}/settings.ini", QSettings.Format.IniFormat
            )
            settings.setValue("alert/continuous_display", True)
            settings.setValue("alert/auto_exit_seconds", 10)

            class FakePopup:
                @staticmethod
                def is_alert_active() -> bool:
                    return True

            class FakeTimer:
                def __init__(self) -> None:
                    self.active = False
                    self.starts = []

                def stop(self) -> None:
                    self.active = False

                def start(self, milliseconds: int = 0) -> None:
                    self.active = True
                    self.starts.append(milliseconds)

                def isActive(self) -> bool:
                    return self.active

            class FakeWindow:
                _settings = settings
                _popup = FakePopup()
                _alert_exit_timer = FakeTimer()
                _alert_countdown_timer = FakeTimer()
                _alert_active = True
                _latest_person_present = True
                _alert_auto_exit_seconds = (
                    MainWindow._alert_auto_exit_seconds
                )

                def __init__(self) -> None:
                    self.countdown_texts = []

                def _set_alert_countdown(self, text: str) -> None:
                    self.countdown_texts.append(text)

            window = FakeWindow()
            MainWindow._sync_alert_exit_timer(window)
            self.assertEqual(window._alert_exit_timer.starts, [])
            self.assertEqual(
                window.countdown_texts[-1],
                "等待人员离开",
            )

            window._latest_person_present = False
            MainWindow._sync_alert_exit_timer(window)
            self.assertEqual(window._alert_exit_timer.starts, [10000])
            self.assertEqual(
                window.countdown_texts[-1],
                "10 秒后退出告警",
            )

            window._latest_person_present = True
            MainWindow._sync_alert_exit_timer(window)
            self.assertFalse(window._alert_exit_timer.isActive())

            settings.setValue("alert/continuous_display", False)
            MainWindow._sync_alert_exit_timer(window, restart=True)
            self.assertEqual(window._alert_exit_timer.starts, [10000, 10000])

    def test_immediate_and_infinite_alert_exit_options(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(
                f"{temp_dir}/settings.ini", QSettings.Format.IniFormat
            )
            settings.setValue("alert/continuous_display", False)
            settings.setValue("alert/auto_exit_seconds", 0)

            class FakeTimer:
                @staticmethod
                def stop() -> None:
                    return

                @staticmethod
                def isActive() -> bool:
                    return False

            class FakeWindow:
                _settings = settings
                _alert_exit_timer = FakeTimer()
                _alert_countdown_timer = FakeTimer()
                _alert_active = True
                _latest_person_present = False
                _alert_auto_exit_seconds = (
                    MainWindow._alert_auto_exit_seconds
                )
                exit_calls = 0

                def __init__(self) -> None:
                    self.countdown_texts = []

                def _set_alert_countdown(self, text: str) -> None:
                    self.countdown_texts.append(text)

                def _auto_exit_current_alert(self) -> None:
                    self.exit_calls += 1

            window = FakeWindow()
            scheduled = []
            with patch.object(
                QTimer,
                "singleShot",
                side_effect=lambda delay, callback: scheduled.append(
                    (delay, callback)
                ),
            ):
                MainWindow._sync_alert_exit_timer(window)
            self.assertEqual(window.countdown_texts[-1], "立即退出")
            self.assertEqual(scheduled[0][0], 0)
            scheduled[0][1]()
            self.assertEqual(window.exit_calls, 1)

            settings.setValue("alert/auto_exit_seconds", -1)
            MainWindow._sync_alert_exit_timer(window, restart=True)
            self.assertEqual(window.countdown_texts[-1], "手动退出")

    def test_custom_rearm_delay_is_saved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(
                f"{temp_dir}/settings.ini", QSettings.Format.IniFormat
            )
            dialog = AlertSettingsDialog(settings)
            dialog.rearm_delay_seconds.setCurrentIndex(
                dialog.rearm_delay_seconds.findData("custom")
            )
            self.assertFalse(dialog.rearm_custom_seconds.isHidden())
            self.assertFalse(dialog.rearm_custom_unit.isHidden())
            self.assertEqual(dialog.rearm_custom_unit.text(), "秒")
            dialog.rearm_custom_seconds.setText("75")
            dialog._save()

            self.assertEqual(
                settings.value(
                    "alert/rearm_delay_seconds",
                    0,
                    type=int,
                ),
                75,
            )

    def test_continuous_monitoring_is_controlled_from_home(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(
                f"{temp_dir}/settings.ini", QSettings.Format.IniFormat
            )
            dashboard = NativeDashboard(settings)
            changes = []
            dashboard.continuous_monitoring_changed.connect(changes.append)

            dashboard.continuous_button.click()

            self.assertTrue(
                settings.value("alert/continuous", False, type=bool)
            )
            self.assertEqual(changes, [True])

    def test_rearm_delay_waits_before_requesting_server(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(
                f"{temp_dir}/settings.ini", QSettings.Format.IniFormat
            )
            settings.setValue("alert/enabled", True)
            settings.setValue("alert/continuous", False)
            settings.setValue("alert/rearm_delay_seconds", 20)

            class FakeTimer:
                active = False
                starts = []

                def stop(self) -> None:
                    self.active = False

                def start(self, milliseconds: int) -> None:
                    self.active = True
                    self.starts.append(milliseconds)

                def isActive(self) -> bool:
                    return self.active

            class FakeMonitor:
                rearm_count = 0

                def request_rearm(self) -> None:
                    self.rearm_count += 1

            class FakeWindow:
                _settings = settings
                _server_url = "http://127.0.0.1:8765"
                _alert_rearm_timer = FakeTimer()
                _monitor = FakeMonitor()
                _alert_enabled = MainWindow._alert_enabled
                _alert_rearm_delay_seconds = (
                    MainWindow._alert_rearm_delay_seconds
                )
                _request_alert_rearm = MainWindow._request_alert_rearm

            window = FakeWindow()
            MainWindow._schedule_alert_rearm(window)
            self.assertEqual(window._alert_rearm_timer.starts, [20000])
            self.assertEqual(window._monitor.rearm_count, 0)

            window._alert_rearm_timer.active = False
            MainWindow._request_alert_rearm(window)
            self.assertEqual(window._monitor.rearm_count, 1)

            settings.setValue("alert/rearm_delay_seconds", 0)
            MainWindow._schedule_alert_rearm(window)
            self.assertEqual(window._monitor.rearm_count, 1)

    def test_alert_popup_switch_is_controlled_from_home(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(
                f"{temp_dir}/settings.ini", QSettings.Format.IniFormat
            )
            dashboard = NativeDashboard(settings)
            changes = []
            dashboard.alert_enabled_changed.connect(changes.append)

            dashboard.alert_enabled_button.click()

            self.assertTrue(
                settings.value("alert/enabled", False, type=bool)
            )
            self.assertEqual(changes, [True])

    def test_popup_warning_title_uses_compact_font(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(
                f"{temp_dir}/settings.ini", QSettings.Format.IniFormat
            )
            popup = AlertPopup(settings)
            self.assertIn("font-size: 15px;", popup.styleSheet())
            self.assertEqual(
                popup._title.text(),
                "⚠️⚠️⚠️ 警告 ⚠️⚠️⚠️",
            )

    def test_macos_popup_stays_visible_when_app_loses_focus(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(
                f"{temp_dir}/settings.ini", QSettings.Format.IniFormat
            )
            popup = AlertPopup(settings)
            if sys.platform == "darwin":
                self.assertTrue(
                    popup.testAttribute(
                        Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow
                    )
                )

    def test_theme_button_cycles_and_follow_system_updates_live(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(
                f"{temp_dir}/settings.ini", QSettings.Format.IniFormat
            )

            class FakeWindow:
                _set_theme_mode = MainWindow._set_theme_mode
                _update_theme_button = MainWindow._update_theme_button

                def __init__(self) -> None:
                    self._settings = settings
                    self._theme_mode = "light"
                    self._theme_button = QPushButton()
                    self.system_theme = "dark"
                    self.applied_themes = []

                def _resolved_theme(self) -> str:
                    if self._theme_mode in {"light", "dark"}:
                        return self._theme_mode
                    return self.system_theme

                def _apply_theme(self, theme: str) -> None:
                    self.applied_themes.append(theme)

            window = FakeWindow()
            window._update_theme_button()
            self.assertEqual(window._theme_button.text(), "浅色主题")

            MainWindow._toggle_theme(window)
            self.assertEqual(window._theme_mode, "dark")
            self.assertEqual(window._theme_button.text(), "深色主题")
            MainWindow._toggle_theme(window)
            self.assertEqual(window._theme_mode, "follow-system")
            self.assertEqual(window._theme_button.text(), "跟随系统")
            self.assertEqual(window.applied_themes[-1], "dark")

            window.system_theme = "light"
            MainWindow._on_system_theme_changed(window, None)
            self.assertEqual(window.applied_themes[-1], "light")

            MainWindow._toggle_theme(window)
            self.assertEqual(window._theme_mode, "light")
            self.assertEqual(
                settings.value("appearance/theme_mode"),
                "light",
            )

    def test_sound_can_be_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(
                f"{temp_dir}/settings.ini", QSettings.Format.IniFormat
            )
            previews = []
            dialog = AlertSettingsDialog(
                settings,
                sound_preview=lambda *args: previews.append(args),
            )
            sound_settings = dialog.sound_settings
            self.assertEqual(
                sound_settings.sound_mode.itemText(0),
                "关闭提示音",
            )
            self.assertEqual(
                sound_settings.sound_mode.itemData(0),
                "off",
            )
            self.assertEqual(
                [
                    sound_settings.sound_mode.itemData(index)
                    for index in range(sound_settings.sound_mode.count())
                ],
                ["off", "app-default", "default", "custom"],
            )
            sound_settings.sound_mode.setCurrentIndex(
                sound_settings.sound_mode.findData("off")
            )

            self.assertFalse(sound_settings.sound_path.isEnabled())
            self.assertFalse(sound_settings.volume.isEnabled())
            self.assertFalse(sound_settings._test_button.isEnabled())
            sound_settings._preview_current_sound()
            self.assertEqual(previews, [])

            dialog._save()
            self.assertEqual(settings.value("sound/mode"), "off")
            self.assertEqual(previews, [("off", "", 80)])

    def test_software_default_sound_uses_bundled_audio(self) -> None:
        bundled_sound = app_default_sound_path()
        self.assertIsNotNone(bundled_sound)
        self.assertTrue(bundled_sound.is_file())
        self.assertEqual(bundled_sound.name, "audio.mp3")

        class FakeAudioOutput:
            volume = 0.0

            def setVolume(self, volume: float) -> None:
                self.volume = volume

        class FakePlayer:
            source = None
            played = False

            def setSource(self, source: object) -> None:
                self.source = source

            def play(self) -> None:
                self.played = True

        class FakeWindow:
            _audio_output = FakeAudioOutput()
            _player = FakePlayer()

            @staticmethod
            def _stop_sound() -> None:
                return

        window = FakeWindow()
        MainWindow._play_sound(window, "app-default", "", 75)
        self.assertAlmostEqual(window._audio_output.volume, 0.75)
        self.assertTrue(window._player.played)
        self.assertEqual(
            window._player.source.toLocalFile(),
            str(bundled_sound),
        )

    def test_other_settings_is_reserved_placeholder(self) -> None:
        dialog = OtherSettingsDialog()
        self.assertEqual(dialog.windowTitle(), "其他配置")
        labels = [label.text() for label in dialog.findChildren(QLabel)]
        self.assertIn("暂无可配置项", labels)

    def test_renders_status_without_web_page(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(f"{temp_dir}/settings.ini", QSettings.Format.IniFormat)
            dashboard = NativeDashboard(settings)
            dashboard.update_status(
                {
                    "detection_running": True,
                    "person_present": True,
                    "people_count": 2,
                    "presence_seconds": 65.0,
                    "fps": 12.4,
                }
            )
            self.assertEqual(dashboard.status_title.text(), "检测到人物")
            self.assertEqual(dashboard.people_value.text(), "2")
            self.assertEqual(dashboard.presence_value.text(), "01:05")
            self.assertEqual(dashboard.fps_value.text(), "12.4")

    def test_preview_reads_server_jpeg_endpoint(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _PreviewHandler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        _PreviewHandler.request_count = 0

        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(f"{temp_dir}/settings.ini", QSettings.Format.IniFormat)
            settings.setValue("dashboard/preview_enabled", True)
            dashboard = NativeDashboard(settings)
            dashboard.set_view_active(True)
            dashboard.set_server_url(f"http://127.0.0.1:{server.server_port}")

            wait_timer = QTimer()

            def finish_when_ready() -> None:
                if dashboard.preview_label.has_image():
                    APP.quit()

            wait_timer.timeout.connect(finish_when_ready)
            wait_timer.start(30)
            QTimer.singleShot(2500, APP.quit)
            APP.exec()
            dashboard.set_view_active(False)
            self.assertTrue(dashboard.preview_label.has_image())
            self.assertGreater(_PreviewHandler.request_count, 0)

        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    unittest.main()
