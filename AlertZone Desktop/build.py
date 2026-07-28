"""使用 PyInstaller 打包 AlertZone Desktop。"""

from __future__ import annotations

import argparse
import importlib.util
import os
import platform
import subprocess
import sys
from pathlib import Path

APP_NAME = "AlertZone Desktop"
ROOT_DIR = Path(__file__).resolve().parent
ENTRY_FILE = ROOT_DIR / "start.py"
ICON_DIR = ROOT_DIR / "icon"


def platform_icon() -> Path:
    system = platform.system()
    if system == "Darwin":
        return ICON_DIR / "icon.icns"
    if system == "Windows":
        return ICON_DIR / "icon.ico"
    return ICON_DIR / "icon.png"


def build_arguments(onefile: bool, console: bool) -> list[str]:
    icon_path = platform_icon()
    arguments = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--name",
        APP_NAME,
        "--icon",
        str(icon_path),
        "--add-data",
        f"{ICON_DIR}{os.pathsep}icon",
        "--hidden-import",
        "PySide6.QtMultimedia",
        "--distpath",
        str(ROOT_DIR / "dist"),
        "--workpath",
        str(ROOT_DIR / "build" / "pyinstaller"),
        "--specpath",
        str(ROOT_DIR / "build"),
    ]
    arguments.append("--console" if console else "--windowed")
    if onefile:
        arguments.append("--onefile")
    if platform.system() == "Darwin":
        arguments.extend(
            [
                "--osx-bundle-identifier",
                "com.hknight.alertzone.desktop",
            ]
        )
    arguments.append(str(ENTRY_FILE))
    return arguments


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="打包 AlertZone Desktop 可执行程序",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--onefile",
        dest="package_mode",
        action="store_const",
        const="onefile",
        help="生成单文件版本；默认生成启动更快的目录版本",
    )
    mode_group.add_argument(
        "--onedir",
        dest="package_mode",
        action="store_const",
        const="onedir",
        help="生成包含运行组件的文件夹版本",
    )
    parser.add_argument(
        "--console",
        action="store_true",
        help="保留控制台窗口，便于排查打包后的启动问题",
    )
    return parser.parse_args()


def select_onefile(package_mode: str | None) -> bool:
    if package_mode is not None:
        return package_mode == "onefile"
    if platform.system() != "Windows" or not sys.stdin.isatty():
        return False
    print(
        "\n请选择 Windows 打包格式：\n"
        "  1. 单文件格式（便于传输，首次启动较慢）\n"
        "  2. 文件夹格式（推荐，启动更快）"
    )
    while True:
        selection = input("请输入 1 或 2，直接回车默认选择 2：").strip()
        if selection in {"", "2"}:
            return False
        if selection == "1":
            return True
        print("输入无效，请输入 1 或 2。")


def main() -> int:
    args = parse_args()
    if importlib.util.find_spec("PyInstaller") is None:
        print(
            "未安装 PyInstaller，请先运行：\n"
            "python -m pip install -r requirements-build.txt",
            file=sys.stderr,
        )
        return 2
    missing = [
        path
        for path in (ENTRY_FILE, platform_icon(), ICON_DIR)
        if not path.exists()
    ]
    if missing:
        print(
            "缺少打包文件："
            + "、".join(str(path) for path in missing),
            file=sys.stderr,
        )
        return 2
    (ROOT_DIR / "build").mkdir(parents=True, exist_ok=True)
    subprocess.run(
        build_arguments(
            select_onefile(args.package_mode),
            args.console,
        ),
        cwd=ROOT_DIR,
        check=True,
    )
    print(f"打包完成：{ROOT_DIR / 'dist'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
