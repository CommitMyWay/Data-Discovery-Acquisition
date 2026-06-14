import builtins
import importlib
import sys
import unittest
from unittest import mock


OPTIONAL_IMPORTS = {"requests", "bs4", "crawl4ai"}


class AgentApiTests(unittest.TestCase):
    def tearDown(self):
        for name in [
            "scripts.agent_api",
            "scripts.sources",
            "scripts.sources.__init__",
        ]:
            sys.modules.pop(name, None)

    def test_agent_api_import_does_not_require_optional_crawler_dependencies(self):
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

        self.assertTrue(hasattr(module, "run_research"))

    def test_run_research_returns_references_for_qualified_reviews(self):
        agent_api = importlib.import_module("scripts.agent_api")
        raw_review = {
            "id": "sha256:test",
            "source": "youtube",
            "app": "ZaloPay",
            "author": "user1",
            "rating": None,
            "content": "Đăng nhập OTP thường xuyên lỗi khi thanh toán tại quầy sau khi cập nhật ứng dụng.",
            "date": "2026-06-01T00:00:00+00:00",
            "url": "https://www.youtube.com/watch?v=abc",
            "language": None,
            "qualified": None,
            "disqualification_reasons": [],
            "metadata": {
                "video_title": "ZaloPay review",
                "video_url": "https://www.youtube.com/watch?v=abc",
                "is_transcript": False,
            },
        }

        async def fake_crawl_app(app_cfg, sources, common_kwargs):
            return [dict(raw_review)]

        with mock.patch.object(agent_api, "_crawl_app", side_effect=fake_crawl_app):
            result = agent_api.asyncio.run(
                agent_api.run_research(
                    apps=["ZaloPay"],
                    goal="product",
                    days_back=30,
                    sources=["youtube"],
                )
            )

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


if __name__ == "__main__":
    unittest.main()
