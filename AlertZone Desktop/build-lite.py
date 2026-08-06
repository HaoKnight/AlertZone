"""构建只包含 AlertZone Desktop 必需运行组件的精简包。"""

from __future__ import annotations

import argparse
import importlib.util
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

import build as standard_build

APP_NAME = standard_build.APP_NAME
APP_VERSION = standard_build.APP_VERSION
ROOT_DIR = standard_build.ROOT_DIR
ENTRY_FILE = standard_build.ENTRY_FILE
ICON_DIR = standard_build.ICON_DIR
AUDIO_DIR = standard_build.AUDIO_DIR
WINDOWS_VERSION_FILE = standard_build.WINDOWS_VERSION_FILE

DIST_DIR = ROOT_DIR / "dist-lite"
WORK_DIR = ROOT_DIR / "build" / "pyinstaller-lite"
SPEC_DIR = ROOT_DIR / "build" / "lite"
CONFIG_DIR = ROOT_DIR / "build" / "pyinstaller-config-lite"

# 当前界面直接使用 QtCore、QtGui、QtWidgets、QtNetwork 和 QtMultimedia。
# QtConcurrent 和 QtDBus 的 Python 包装没有直接使用，但本版 Qt 原生库
# 依赖其 framework，因此仅排除包装模块，不在构建后删除对应 framework。
EXCLUDED_QT_MODULES = (
    "PySide6.QtConcurrent",
    "PySide6.QtDBus",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuickControls2",
    "PySide6.QtQuickWidgets",
    "PySide6.QtSvg",
    "PySide6.QtSvgWidgets",
    "PySide6.QtVirtualKeyboard",
)

REMOVABLE_QT_COMPONENTS = (
    "QtMultimediaWidgets",
    "QtOpenGL",
    "QtOpenGLWidgets",
    "QtPdf",
    "QtPdfWidgets",
    "QtQml",
    "QtQmlMeta",
    "QtQmlModels",
    "QtQmlWorkerScript",
    "QtQuick",
    "QtQuickControls2",
    "QtQuickWidgets",
    "QtSvg",
    "QtSvgWidgets",
    "QtVirtualKeyboard",
    "QtVirtualKeyboardQml",
)

MACOS_FFMPEG_LIBRARIES = (
    "libavcodec.61.dylib",
    "libavformat.61.dylib",
    "libavutil.59.dylib",
    "libswresample.5.dylib",
    "libswscale.8.dylib",
)

# 英文翻译文件是 Qt 的空占位文件；保留简体和繁体中文，供原生 Qt
# 对话框在中文系统中显示。应用自身的中文文案编译在业务代码中。
KEPT_TRANSLATION_SUFFIXES = ("_en.qm", "_zh_CN.qm", "_zh_TW.qm")


def build_arguments(onefile: bool, console: bool) -> list[str]:
    """返回精简构建使用的 PyInstaller 参数。"""
    arguments = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--name",
        APP_NAME,
        "--icon",
        str(standard_build.platform_icon()),
        # 运行时只读取 PNG；ICNS/ICO 仅由 PyInstaller 用作包图标，没必要
        # 再复制到 icon/ 资源目录。
        "--add-data",
        f"{ICON_DIR / 'icon.png'}{os.pathsep}icon",
        "--add-data",
        f"{AUDIO_DIR / 'audio.mp3'}{os.pathsep}audio",
        "--hidden-import",
        "PySide6.QtMultimedia",
        "--optimize",
        "1",
        "--distpath",
        str(DIST_DIR),
        "--workpath",
        str(WORK_DIR),
        "--specpath",
        str(SPEC_DIR),
    ]
    for module_name in EXCLUDED_QT_MODULES:
        arguments.extend(("--exclude-module", module_name))
    arguments.append("--console" if console else "--windowed")
    if onefile:
        arguments.append("--onefile")

    system = platform.system()
    if system == "Windows":
        arguments.extend(
            ("--version-file", str(WINDOWS_VERSION_FILE))
        )
    elif system == "Darwin":
        arguments.extend(
            (
                "--osx-bundle-identifier",
                "com.hknight.alertzone.desktop",
                "--hidden-import",
                "AppKit",
                "--hidden-import",
                "Foundation",
                "--hidden-import",
                "objc",
            )
        )
    arguments.append(str(ENTRY_FILE))
    return arguments


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="构建移除未使用 Qt 组件的 AlertZone Desktop 精简包",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--onefile",
        dest="package_mode",
        action="store_const",
        const="onefile",
        help="生成单文件版本；非 macOS 单文件无法执行构建后资源清理",
    )
    mode_group.add_argument(
        "--onedir",
        dest="package_mode",
        action="store_const",
        const="onedir",
        help="生成目录版本（默认，精简效果最好）",
    )
    parser.add_argument(
        "--console",
        action="store_true",
        help="保留控制台窗口，便于排查启动问题",
    )
    parser.add_argument(
        "--keep-ffmpeg",
        action="store_true",
        help="macOS 同时保留 FFmpeg 后端；默认仅保留原生 Darwin 后端",
    )
    parser.add_argument(
        "--keep-all-translations",
        action="store_true",
        help="保留 Qt 的全部 124 个语言文件",
    )
    return parser.parse_args()


def select_onefile(package_mode: str | None) -> bool:
    """Lite 构建默认使用便于清理和快速启动的目录模式。"""
    return package_mode == "onefile"


def generated_output(onefile: bool) -> Path:
    system = platform.system()
    if system == "Darwin":
        return DIST_DIR / f"{APP_NAME}.app"
    if onefile:
        suffix = ".exe" if system == "Windows" else ""
        return DIST_DIR / f"{APP_NAME}{suffix}"
    return DIST_DIR / APP_NAME


def logical_size(path: Path) -> int:
    """计算目录内实体文件的逻辑大小，不重复跟随符号链接。"""
    if path.is_file():
        return path.stat().st_size
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for filename in files:
            candidate = Path(root) / filename
            if not candidate.is_symlink():
                total += candidate.stat().st_size
    return total


def remove_path(path: Path) -> bool:
    """删除构建产物中的一个明确路径。"""
    if path.is_symlink() or path.is_file():
        path.unlink()
        return True
    if path.is_dir():
        shutil.rmtree(path)
        return True
    return False


def pyqt_roots(output_path: Path) -> list[Path]:
    """找出真实的 PySide6 目录，忽略指向同一目录的链接。"""
    roots: list[Path] = []
    seen: set[Path] = set()
    if not output_path.is_dir():
        return roots
    for path in output_path.rglob("PySide6"):
        if not path.is_dir() or path.is_symlink():
            continue
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            roots.append(path)
    return roots


def prune_translations(output_path: Path) -> int:
    removed = 0
    for pyside_root in pyqt_roots(output_path):
        translations = pyside_root / "Qt" / "translations"
        if not translations.is_dir():
            continue
        for translation in translations.glob("*.qm"):
            if not translation.name.endswith(KEPT_TRANSLATION_SUFFIXES):
                removed += int(remove_path(translation))
    return removed


def prune_qt_components(output_path: Path) -> int:
    removed = 0
    for pyside_root in pyqt_roots(output_path):
        qt_lib = pyside_root / "Qt" / "lib"
        for component in REMOVABLE_QT_COMPONENTS:
            removed += int(
                remove_path(pyside_root / f"{component}.abi3.so")
            )
            removed += int(remove_path(qt_lib / f"{component}.framework"))

        # QtMultimedia 需要 QtConcurrent framework，但程序没有使用其
        # Python API，因此只删除绑定扩展。
        removed += int(remove_path(pyside_root / "QtConcurrent.abi3.so"))
    return removed


def prune_plugins(output_path: Path, keep_ffmpeg: bool) -> int:
    removed = 0
    system = platform.system()
    for pyside_root in pyqt_roots(output_path):
        plugins = pyside_root / "Qt" / "plugins"
        if not plugins.is_dir():
            continue

        # 业务预览图为 JPEG，PNG 支持内置在 QtGui；其余图片格式、SVG
        # 图标引擎、触摸和虚拟键盘插件均未被使用。
        imageformats = plugins / "imageformats"
        if imageformats.is_dir():
            jpeg_names = {
                "libqjpeg.dylib",
                "qjpeg.dll",
                "libqjpeg.so",
            }
            for plugin in imageformats.iterdir():
                if plugin.name not in jpeg_names:
                    removed += int(remove_path(plugin))

        for directory_name in (
            "generic",
            "iconengines",
            "platforminputcontexts",
        ):
            removed += int(remove_path(plugins / directory_name))

        # macOS 上 QMediaPlayer 可使用 AVFoundation/Darwin 原生后端播放
        # MP3；移除 FFmpeg 后端及其五个动态库可显著降低体积。
        if system == "Darwin" and not keep_ffmpeg:
            removed += int(
                remove_path(
                    plugins
                    / "multimedia"
                    / "libffmpegmediaplugin.dylib"
                )
            )
            qt_lib = pyside_root / "Qt" / "lib"
            for library_name in MACOS_FFMPEG_LIBRARIES:
                removed += int(remove_path(qt_lib / library_name))
    return removed


def remove_broken_symlinks(output_path: Path) -> int:
    removed = 0
    if not output_path.is_dir():
        return removed
    for root, dirs, files in os.walk(output_path, followlinks=False):
        for name in [*dirs, *files]:
            path = Path(root) / name
            if path.is_symlink() and not path.exists():
                path.unlink()
                removed += 1
    return removed


def prune_bundle(
    output_path: Path,
    *,
    keep_ffmpeg: bool,
    keep_all_translations: bool,
) -> tuple[int, int, int]:
    """清理目录包并返回清理前大小、清理后大小和删除项数。"""
    before = logical_size(output_path)
    removed = prune_qt_components(output_path)
    removed += prune_plugins(output_path, keep_ffmpeg)
    if not keep_all_translations:
        removed += prune_translations(output_path)
    removed += remove_broken_symlinks(output_path)
    return before, logical_size(output_path), removed


def finalize_macos_bundle(app_path: Path) -> None:
    """写入版本、重新进行 ad-hoc 签名并验证精简包。"""
    validate_macos_qt_dependencies(app_path)
    standard_build.write_macos_bundle_version(app_path)
    subprocess.run(
        (
            "/usr/bin/codesign",
            "--force",
            "--deep",
            "--sign",
            "-",
            str(app_path),
        ),
        check=True,
    )
    subprocess.run(
        (
            "/usr/bin/codesign",
            "--verify",
            "--deep",
            "--strict",
            str(app_path),
        ),
        check=True,
    )


def validate_macos_qt_dependencies(app_path: Path) -> None:
    """确认保留的 Mach-O 文件引用的 Qt framework 均存在。"""
    qt_libs = list(app_path.rglob("PySide6/Qt/lib"))
    available = {
        framework.name.removesuffix(".framework")
        for qt_lib in qt_libs
        for framework in qt_lib.glob("Qt*.framework")
        if framework.is_dir()
    }
    required: set[str] = set()
    dependency_pattern = re.compile(r"@rpath/(Qt[A-Za-z0-9]+)")
    candidates = [
        path
        for path in app_path.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and (
            path.suffix in {".dylib", ".so"}
            or ".framework/Versions/" in path.as_posix()
            or path.parent.name == "MacOS"
        )
    ]
    for candidate in candidates:
        result = subprocess.run(
            ("/usr/bin/otool", "-L", str(candidate)),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            required.update(dependency_pattern.findall(result.stdout))
    missing = sorted(required - available)
    if missing:
        raise RuntimeError(
            "Lite 清理删除了仍被原生库引用的 Qt 组件："
            + "、".join(missing)
        )


def validate_inputs() -> list[Path]:
    return [
        path
        for path in (
            ENTRY_FILE,
            standard_build.platform_icon(),
            ICON_DIR / "icon.png",
            AUDIO_DIR / "audio.mp3",
        )
        if not path.is_file()
    ]


def main() -> int:
    args = parse_args()
    if importlib.util.find_spec("PyInstaller") is None:
        print(
            "未安装 PyInstaller，请先运行：\n"
            "python -m pip install -r requirements-build.txt",
            file=sys.stderr,
        )
        return 2

    missing = validate_inputs()
    if missing:
        print(
            "缺少打包文件：" + "、".join(str(path) for path in missing),
            file=sys.stderr,
        )
        return 2

    onefile = select_onefile(args.package_mode)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    SPEC_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if platform.system() == "Windows":
        standard_build.write_windows_version_file()

    build_environment = os.environ.copy()
    # 避免 --clean 清理用户级 PyInstaller 缓存；Lite 使用独立项目缓存，
    # 不干扰普通版构建，也能在受限构建环境中工作。
    build_environment["PYINSTALLER_CONFIG_DIR"] = str(CONFIG_DIR)
    subprocess.run(
        build_arguments(onefile, args.console),
        cwd=ROOT_DIR,
        env=build_environment,
        check=True,
    )
    output_path = generated_output(onefile)

    if output_path.is_dir():
        before, after, removed = prune_bundle(
            output_path,
            keep_ffmpeg=args.keep_ffmpeg,
            keep_all_translations=args.keep_all_translations,
        )
        if platform.system() == "Darwin":
            finalize_macos_bundle(output_path)
        saved = before - after
        print(
            f"精简完成：删除 {removed} 项，"
            f"从 {before / 1024 / 1024:.2f} MiB 降至 "
            f"{after / 1024 / 1024:.2f} MiB，"
            f"节省 {saved / 1024 / 1024:.2f} MiB"
        )
    else:
        print("单文件构建完成；模块排除已生效，跳过目录资源清理。")

    print(f"AlertZone Desktop Lite v{APP_VERSION}：{output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
