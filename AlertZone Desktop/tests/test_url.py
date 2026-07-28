"""局域网地址规范化测试。"""

import unittest

from src.AlertZone_Desktop import normalize_server_url


class NormalizeServerUrlTests(unittest.TestCase):
    def test_adds_scheme_and_default_port(self) -> None:
        self.assertEqual(
            normalize_server_url("192.168.1.20"),
            "http://192.168.1.20:8765",
        )

    def test_preserves_explicit_port(self) -> None:
        self.assertEqual(
            normalize_server_url("http://alertzone.local:9000/anything"),
            "http://alertzone.local:9000",
        )

    def test_supports_ipv6(self) -> None:
        self.assertEqual(
            normalize_server_url("http://[fe80::1]:8765"),
            "http://[fe80::1]:8765",
        )

    def test_rejects_invalid_scheme(self) -> None:
        with self.assertRaises(ValueError):
            normalize_server_url("ftp://192.168.1.20")


if __name__ == "__main__":
    unittest.main()
