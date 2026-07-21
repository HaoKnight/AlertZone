"""PySide6 开发预览：代码保存后自动重启界面。

运行一次本脚本即可。它只使用 Python 标准库，不需要安装额外依赖。
"""

from __future__ import annotations

import signal
import subprocess
import sys
import time
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
APP_FILE = PROJECT_DIR / "AlertZone_app.py"
CHECK_INTERVAL = 0.5


def request_shutdown(_signum: int, _frame: object) -> None:
    """把 VS Code 的停止信号转换为正常退出，确保子窗口一并关闭。"""
    raise KeyboardInterrupt


def source_snapshot() -> dict[Path, int]:
    """记录项目 Python 文件的修改时间，用于判断代码是否已保存。"""
    snapshot: dict[Path, int] = {}
    for path in PROJECT_DIR.glob("*.py"):
        if path.name == Path(__file__).name:
            continue
        try:
            snapshot[path] = path.stat().st_mtime_ns
        except FileNotFoundError:
            # 编辑器保存文件时可能会先删除再替换，下一轮检查即可恢复。
            continue
    return snapshot


def start_app() -> subprocess.Popen[bytes]:
    """使用当前虚拟环境里的 Python 启动主程序。"""
    print("\n[自动预览] 正在启动界面……", flush=True)
    return subprocess.Popen(
        [sys.executable, str(APP_FILE)],
        cwd=PROJECT_DIR,
    )


def stop_app(process: subprocess.Popen[bytes] | None) -> None:
    """先正常终止界面；超时后再强制结束，避免残留摄像头进程。"""
    if process is None or process.poll() is not None:
        return

    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def main() -> None:
    if not APP_FILE.exists():
        raise SystemExit(f"找不到主程序：{APP_FILE}")

    signal.signal(signal.SIGTERM, request_shutdown)
    print("[自动预览] 已监听项目中的 Python 文件。")
    print("[自动预览] 保存代码后界面会自动重启，按 Ctrl+C 退出。")

    process: subprocess.Popen[bytes] | None = None
    snapshot = source_snapshot()

    try:
        process = start_app()
        while True:
            time.sleep(CHECK_INTERVAL)
            new_snapshot = source_snapshot()
            if new_snapshot == snapshot:
                continue

            snapshot = new_snapshot
            print("[自动预览] 检测到代码变化，正在刷新……", flush=True)
            stop_app(process)
            # 给编辑器的原子保存操作留出很短的稳定时间。
            time.sleep(0.2)
            process = start_app()
    except KeyboardInterrupt:
        print("\n[自动预览] 已停止。")
    finally:
        stop_app(process)


if __name__ == "__main__":
    main()
