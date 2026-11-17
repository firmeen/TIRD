"""Network-free smoke tests for the original curl transport compatibility."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tird_collector as tird


class CollectorSmokeTests(unittest.TestCase):
    def test_response_json_text_plain_is_accepted(self):
        self.assertEqual(tird.parse_json_bytes(b'[{"Service_id":"1"}]'), [{"Service_id": "1"}])

    def test_buddhist_year_is_converted(self):
        self.assertEqual(tird.parse_thai_date("วันที่เผยแพร่ 29 ตุลาคม 2569").year, 2026)

    def test_curl_config_uses_empty_post_body_and_stdin_credentials(self):
        cfg = tird.Config(
            authorization="TEST_AUTH", cookie="TEST_COOKIE=abc",
            referer=tird.BASE_URL + "/Content", curl_executable="curl.exe",
            output=Path("."), timeout=10,
        )
        encoded = tird.CurlTransport(cfg).build_config(tird.API["catalog"], None, True)
        self.assertIn(b"content-length: 0", encoded)
        self.assertIn(b"TEST_AUTH", encoded)
        self.assertIn(b"TEST_COOKIE=abc", encoded)


if __name__ == "__main__":
    unittest.main()
