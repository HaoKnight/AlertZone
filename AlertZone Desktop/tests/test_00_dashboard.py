"""原生 Desktop 仪表板测试。"""

import base64
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from src.alertzone_desktop import (
    AlertPopup,
    AlertSettingsDialog,
    CloseActionDialog,
    MainWindow,
    NativeDashboard,
    OtherSettingsDialog,
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
    def test_close_dialog_offers_client_style_actions(self) -> None:
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

            class FakeWindow:
                _settings = settings
                _server_url = "http://127.0.0.1:8765"
                _popup = FakePopup()
                _monitor = FakeMonitor()
                _alert_exit_timer = FakeTimer()
                _background_mode = False
                _alert_popup_allowed = MainWindow._alert_popup_allowed

                @staticmethod
                def isMinimized() -> bool:
                    return False

                @staticmethod
                def _stop_sound() -> None:
                    return

                @staticmethod
                def _stop_alert_live_preview() -> None:
                    return

            window = FakeWindow()
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
            self.assertTrue(window._popup.hidden)

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
                "数据由 AlertZone Client API 提供",
            )
            self.assertIsNone(
                dashboard.findChild(QLabel, "dashboardFooter")
            )

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
            popup.set_display_mode("red")
            self.assertEqual(popup.display_mode(), "zoom-red")
            for mode in ("zoom", "zoom-red", "live", "live-red"):
                popup.set_display_mode(mode)
                self.assertFalse(popup._image.isHidden())

    def test_alert_dialog_has_no_theme_or_enable_switch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(
                f"{temp_dir}/settings.ini", QSettings.Format.IniFormat
            )
            dialog = AlertSettingsDialog(settings)
            self.assertEqual(dialog.windowTitle(), "告警设置")
            self.assertEqual(dialog.alert_display_mode.count(), 4)
            self.assertEqual(
                [
                    dialog.alert_display_mode.itemData(index)
                    for index in range(dialog.alert_display_mode.count())
                ],
                ["zoom", "live", "zoom-red", "live-red"],
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
            self.assertGreaterEqual(
                dialog.alert_display_mode.minimumWidth(),
                240,
            )
            self.assertEqual(dialog.auto_exit_seconds.currentData(), 10)
            self.assertFalse(dialog.continuous_alert_display.isChecked())
            self.assertEqual(
                [
                    dialog.auto_exit_seconds.itemData(index)
                    for index in range(dialog.auto_exit_seconds.count())
                ],
                [5, 10, 15, 30, 60, 0],
            )
            self.assertFalse(hasattr(dialog, "continuous_enabled"))
            self.assertFalse(hasattr(dialog, "theme_mode"))
            self.assertFalse(hasattr(dialog, "alert_enabled"))
            dialog.continuous_alert_display.setChecked(True)
            dialog._save()
            self.assertTrue(
                settings.value(
                    "alert/continuous_display",
                    False,
                    type=bool,
                )
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

                def start(self, milliseconds: int) -> None:
                    self.active = True
                    self.starts.append(milliseconds)

                def isActive(self) -> bool:
                    return self.active

            class FakeWindow:
                _settings = settings
                _popup = FakePopup()
                _alert_exit_timer = FakeTimer()
                _latest_person_present = True
                _alert_auto_exit_seconds = (
                    MainWindow._alert_auto_exit_seconds
                )

            window = FakeWindow()
            MainWindow._sync_alert_exit_timer(window)
            self.assertEqual(window._alert_exit_timer.starts, [])

            window._latest_person_present = False
            MainWindow._sync_alert_exit_timer(window)
            self.assertEqual(window._alert_exit_timer.starts, [10000])

            window._latest_person_present = True
            MainWindow._sync_alert_exit_timer(window)
            self.assertFalse(window._alert_exit_timer.isActive())

            settings.setValue("alert/continuous_display", False)
            MainWindow._sync_alert_exit_timer(window, restart=True)
            self.assertEqual(window._alert_exit_timer.starts, [10000, 10000])

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

    def test_preview_reads_client_jpeg_endpoint(self) -> None:
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
