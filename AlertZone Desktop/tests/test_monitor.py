"""后台报警事件轮询测试。"""

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PySide6.QtCore import QCoreApplication, QSettings, QTimer

from src.alertzone_desktop import StatusMonitor


class _StatusHandler(BaseHTTPRequestHandler):
    event_id = 0

    def do_GET(self) -> None:
        if not self.path.startswith("/api/status"):
            self.send_error(404)
            return
        payload = json.dumps(
            {
                "instance_id": "test-instance",
                "intrusion_event_id": self.event_id,
                "intrusion_people_count": 1,
                "intrusion_image_available": False,
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class StatusMonitorTests(unittest.TestCase):
    def test_ignores_baseline_then_emits_new_event(self) -> None:
        app = QCoreApplication.instance() or QCoreApplication([])
        server = ThreadingHTTPServer(("127.0.0.1", 0), _StatusHandler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        _StatusHandler.event_id = 4

        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(f"{temp_dir}/settings.ini", QSettings.Format.IniFormat)
            settings.setValue("alert/enabled", True)
            monitor = StatusMonitor(settings)
            received: list[int] = []

            def receive(payload: dict) -> None:
                received.append(int(payload["intrusion_event_id"]))
                app.quit()

            monitor.intrusion_detected.connect(receive)
            monitor.set_server_url(f"http://127.0.0.1:{server.server_port}")
            QTimer.singleShot(800, lambda: setattr(_StatusHandler, "event_id", 5))
            QTimer.singleShot(4000, app.quit)
            app.exec()
            self.assertEqual(received, [5])

        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    unittest.main()
