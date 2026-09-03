"""使用 PyInstaller 打包 AlertZone Server。"""

from __future__ import annotations

import argparse
import importlib.util
import os
import platform
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

APP_NAME = "AlertZone Server"
APP_VERSION = "1.0.0"
ROOT_DIR = Path(__file__).resolve().parent
SOURCE_DIR = ROOT_DIR / "src"
ENTRY_FILE = SOURCE_DIR / "AlertZone_Server.py"
MODEL_FILE = SOURCE_DIR / "yolo11n.pt"
WEB_DIR = SOURCE_DIR / "web"
ICON_DIR = ROOT_DIR / "icon"
WINDOWS_VERSION_FILE = ROOT_DIR / "build" / "windows_version_info.txt"


def version_tuple() -> tuple[int, int, int, int]:
    """将语义版本转换为 Windows 版本资源要求的四段数字。"""
    parts = [int(part) for part in APP_VERSION.split(".")]
    normalized = (parts + [0, 0, 0, 0])[:4]
    return normalized[0], normalized[1], normalized[2], normalized[3]


def write_windows_version_file() -> Path:
    """生成 Windows 可执行文件使用的 PyInstaller 版本资源。"""
    major, minor, patch, build = version_tuple()
    WINDOWS_VERSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    WINDOWS_VERSION_FILE.write_text(
        f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({major}, {minor}, {patch}, {build}),
    prodvers=({major}, {minor}, {patch}, {build}),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [StringStruct('CompanyName', 'H-Knight'),
         StringStruct('FileDescription', '{APP_NAME}'),
         StringStruct('FileVersion', '{APP_VERSION}'),
         StringStruct('InternalName', '{APP_NAME}'),
         StringStruct('OriginalFilename', '{APP_NAME}.exe'),
         StringStruct('ProductName', '{APP_NAME}'),
         StringStruct('ProductVersion', '{APP_VERSION}')]
      )
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""",
        encoding="utf-8",
    )
    return WINDOWS_VERSION_FILE


def write_macos_bundle_version(app_path: Path) -> Path:
    """把发布版本写入 macOS 应用包的 Info.plist。"""
    plist_path = app_path / "Contents" / "Info.plist"
    with plist_path.open("rb") as plist_file:
        info = plistlib.load(plist_file)
    info["CFBundleShortVersionString"] = APP_VERSION
    info["CFBundleVersion"] = APP_VERSION
    with plist_path.open("wb") as plist_file:
        plistlib.dump(info, plist_file, fmt=plistlib.FMT_XML, sort_keys=False)
    return plist_path


def platform_icon() -> Path:
    system = platform.system()
    if system == "Darwin":
        return ICON_DIR / "icon.icns"
    if system == "Windows":
        return ICON_DIR / "icon.ico"
    return ICON_DIR / "icon.png"


def build_arguments(onefile: bool, console: bool) -> list[str]:
    arguments = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--name",
        APP_NAME,
        "--icon",
        str(platform_icon()),
        "--add-data",
        f"{ICON_DIR}{os.pathsep}icon",
        "--add-data",
        f"{MODEL_FILE}{os.pathsep}.",
        "--add-data",
        f"{WEB_DIR}{os.pathsep}web",
        "--collect-all",
        "ultralytics",
        "--copy-metadata",
        "ultralytics",
        "--hidden-import",
        "lap",
        "--distpath",
        str(ROOT_DIR / "dist"),
        "--workpath",
        str(ROOT_DIR / "build" / "pyinstaller"),
        "--specpath",
        str(ROOT_DIR / "build"),
    ]
    arguments.append("--console" if console else "--windowed")
    arguments.append("--onefile" if onefile else "--onedir")
    system = platform.system()
    if system == "Windows":
        arguments.extend(
            ["--version-file", str(WINDOWS_VERSION_FILE)]
        )
    if system == "Darwin":
        arguments.extend(
            [
                "--osx-bundle-identifier",
                "com.hknight.alertzone.server",
            ]
        )
    arguments.append(str(ENTRY_FILE))
    return arguments


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="打包 AlertZone Server 可执行程序",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--onefile",
        dest="package_mode",
        action="store_const",
        const="onefile",
        help="生成单文件版本；体积较大且首次启动较慢",
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


def configure_macos_bundle() -> None:
    app_bundle = ROOT_DIR / "dist" / f"{APP_NAME}.app"
    plist_path = app_bundle / "Contents" / "Info.plist"
    if not plist_path.is_file():
        return
    write_macos_bundle_version(app_bundle)
    with plist_path.open("rb") as plist_file:
        info = plistlib.load(plist_file)
    info["NSCameraUsageDescription"] = (
        "AlertZone 需要访问摄像头以进行本地人体检测与区域告警。"
    )
    info["NSLocalNetworkUsageDescription"] = (
        "AlertZone 需要访问本地网络，以便局域网设备查看检测状态与告警页面。"
    )
    with plist_path.open("wb") as plist_file:
        plistlib.dump(info, plist_file)
    codesign = shutil.which("codesign")
    if codesign is not None:
        subprocess.run(
            [
                codesign,
                "--force",
                "--deep",
                "--sign",
                "-",
                str(app_bundle),
            ],
            check=True,
        )


def main() -> int:
    args = parse_args()
    if importlib.util.find_spec("PyInstaller") is None:
        print(
            "未安装 PyInstaller，请先运行：\n"
            "python -m pip install -r requirements-build.txt",
            file=sys.stderr,
        )
        return 2
    required_paths = (
        ENTRY_FILE,
        MODEL_FILE,
        WEB_DIR / "index.html",
        ICON_DIR,
        platform_icon(),
    )
    missing = [path for path in required_paths if not path.exists()]
    if missing:
        print(
            "缺少打包资源："
            + "、".join(str(path) for path in missing),
            file=sys.stderr,
        )
        return 2
    (ROOT_DIR / "build").mkdir(parents=True, exist_ok=True)
    system = platform.system()
    if system == "Windows":
        write_windows_version_file()
    subprocess.run(
        build_arguments(
            select_onefile(args.package_mode),
            args.console,
        ),
        cwd=ROOT_DIR,
        check=True,
    )
    if system == "Darwin" and not args.console:
        configure_macos_bundle()
    print(f"打包完成 v{APP_VERSION}：{ROOT_DIR / 'dist'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
