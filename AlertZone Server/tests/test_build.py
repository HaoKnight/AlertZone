"""Server 运行时与打包版本元数据测试。"""

import ast
import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import build


class BuildVersionTests(unittest.TestCase):
    def test_release_version_is_1_0_0(self) -> None:
        self.assertEqual(build.APP_VERSION, "1.0.0")
        self.assertEqual(build.version_tuple(), (1, 0, 0, 0))

    def test_runtime_version_matches_build_version(self) -> None:
        source_file = build.SOURCE_DIR / "AlertZone_Server.py"
        syntax_tree = ast.parse(source_file.read_text(encoding="utf-8"))
        runtime_version = None
        for statement in syntax_tree.body:
            if not isinstance(statement, ast.Assign):
                continue
            if any(
                isinstance(target, ast.Name)
                and target.id == "APP_VERSION"
                for target in statement.targets
            ):
                runtime_version = ast.literal_eval(statement.value)
                break
        self.assertEqual(runtime_version, build.APP_VERSION)

    def test_windows_arguments_include_version_resource(self) -> None:
        with patch("build.platform.system", return_value="Windows"):
            arguments = build.build_arguments(False, False)
        version_index = arguments.index("--version-file")
        self.assertEqual(
            arguments[version_index + 1],
            str(build.WINDOWS_VERSION_FILE),
        )

    def test_windows_version_resource_contains_release_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            version_path = Path(temp_dir) / "version_info.txt"
            with patch("build.WINDOWS_VERSION_FILE", version_path):
                generated_path = build.write_windows_version_file()
            content = generated_path.read_text(encoding="utf-8")
            self.assertIn(
                f"filevers={build.version_tuple()}",
                content,
            )
            self.assertIn(
                f"ProductVersion', '{build.APP_VERSION}'",
                content,
            )

    def test_macos_bundle_version_is_written_to_plist(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app_path = Path(temp_dir) / "AlertZone Server.app"
            contents_path = app_path / "Contents"
            contents_path.mkdir(parents=True)
            plist_path = contents_path / "Info.plist"
            with plist_path.open("wb") as plist_file:
                plistlib.dump(
                    {"CFBundleShortVersionString": "0.0.0"},
                    plist_file,
                )

            build.write_macos_bundle_version(app_path)

            with plist_path.open("rb") as plist_file:
                info = plistlib.load(plist_file)
            self.assertEqual(
                info["CFBundleShortVersionString"],
                build.APP_VERSION,
            )
            self.assertEqual(info["CFBundleVersion"], build.APP_VERSION)


if __name__ == "__main__":
    unittest.main()
