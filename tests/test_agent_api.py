import builtins
import importlib
import sys
import unittest
from unittest import mock


# These crawl-only deps must NOT be required to import the module anymore.
OPTIONAL_IMPORTS = {"google_play_scraper", "yt_dlp", "requests", "bs4", "crawl4ai"}


class AgentApiTests(unittest.TestCase):
    def tearDown(self):
        for name in ["scripts.agent_api", "scripts.processing", "scripts.pipeline"]:
            sys.modules.pop(name, None)

    def test_import_does_not_require_crawl_dependencies(self):
        real_import = builtins.__import__

        def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
            root = name.split(".", 1)[0]
            if root in OPTIONAL_IMPORTS:
                raise ModuleNotFoundError(f"No module named '{root}'")
            return real_import(name, globals, locals, fromlist, level)

        for name in list(sys.modules):
            if name.split(".", 1)[0] in OPTIONAL_IMPORTS or name.startswith("scripts."):
                sys.modules.pop(name, None)

        with mock.patch("builtins.__import__", side_effect=guarded_import):
            module = importlib.import_module("scripts.agent_api")

        self.assertTrue(hasattr(module, "process_reviews"))

    def test_process_reviews_qualifies_and_builds_references(self):
        agent_api = importlib.import_module("scripts.agent_api")
        raw = {
            "id": "sha256:test",
            "source": "youtube",
            "app": "ZaloPay",
            "author": "user1",
            "rating": None,
            "content": "Đăng nhập OTP thường xuyên lỗi khi thanh toán tại quầy sau khi cập nhật ứng dụng.",
            "date": "2026-06-01T00:00:00+00:00",
            "url": "https://www.youtube.com/watch?v=abc",
            "metadata": {
                "video_title": "ZaloPay review",
                "video_url": "https://www.youtube.com/watch?v=abc",
                "is_transcript": False,
            },
        }

        result = agent_api.process_reviews(
            [raw], apps=["ZaloPay"], goal="product", days_back=30, focus_area=None
        )

        self.assertEqual(len(result["reviews"]), 1)
        self.assertEqual(result["reviews_by_app"]["ZaloPay"][0]["id"], "sha256:test")
        self.assertEqual(result["stats"]["ZaloPay"]["qualified"], 1)
        self.assertEqual(
            result["references"],
            [
                {
                    "source": "youtube",
                    "app": "ZaloPay",
                    "title": "ZaloPay review",
                    "url": "https://www.youtube.com/watch?v=abc",
                    "date": "2026-06-01T00:00:00+00:00",
                    "review_id": "sha256:test",
                }
            ],
        )

    def test_process_reviews_drops_unqualified(self):
        agent_api = importlib.import_module("scripts.agent_api")
        too_short = {"source": "reddit", "app": "MoMo", "author": "u",
                     "content": "ok", "date": "2026-06-01", "url": "https://r/x"}

        result = agent_api.process_reviews(
            [too_short], apps=["MoMo"], goal="qa", days_back=30
        )

        self.assertEqual(result["reviews"], [])
        self.assertEqual(result["stats"]["MoMo"]["total"], 1)
        self.assertEqual(result["stats"]["MoMo"]["qualified"], 0)

    def test_process_reviews_handles_empty_input(self):
        agent_api = importlib.import_module("scripts.agent_api")
        result = agent_api.process_reviews([], apps=["MoMo"], goal="product")
        self.assertEqual(result["reviews"], [])
        self.assertEqual(result["apps"], ["MoMo"])
        self.assertEqual(result["stats"]["MoMo"], {"total": 0, "qualified": 0, "by_source": {}})


if __name__ == "__main__":
    unittest.main()
