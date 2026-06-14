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
            "scripts.crawl_client",
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
                    crawl_service_url="",
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

    def test_run_research_delegates_to_review_crawler_service(self):
        agent_api = importlib.import_module("scripts.agent_api")
        service_payload = {
            "subject": "ZaloPay",
            "market": "VN",
            "goal": "qa",
            "focus": "OTP",
            "reviews": [
                {
                    "id": "sha256:delegated",
                    "source": "google_play",
                    "subject": "ZaloPay",
                    "author": "user2",
                    "rating": 1,
                    "content": "OTP fails every time I try to login and complete payment.",
                    "date": "2026-06-02",
                    "url": "https://play.google.com/store/apps/details?id=x",
                    "language": "en",
                    "qualified": True,
                    "disqualification_reasons": [],
                    "metadata": {},
                }
            ],
            "references": [
                {
                    "source": "google_play",
                    "url": "https://play.google.com/store/apps/details?id=x",
                }
            ],
            "stats": {"google_play": {"raw": 1, "qualified": 1}},
            "outcomes": [{"source": "google_play", "result": "success"}],
        }

        async def fake_crawl_reviews(**kwargs):
            self.assertEqual(kwargs["base_url"], "http://crawler.test")
            self.assertEqual(kwargs["subject"], "ZaloPay")
            self.assertEqual(kwargs["market"], "VN")
            self.assertEqual(kwargs["goal"], "qa")
            self.assertEqual(kwargs["focus"], "OTP")
            self.assertEqual(kwargs["sources"], ["google_play"])
            self.assertEqual(kwargs["filters"]["extra"], "value")
            return dict(service_payload)

        with mock.patch("scripts.crawl_client.crawl_reviews", side_effect=fake_crawl_reviews):
            result = agent_api.asyncio.run(
                agent_api.run_research(
                    apps=["ZaloPay"],
                    goal="qa",
                    days_back=30,
                    sources=["google_play"],
                    focus_area="OTP",
                    crawl_service_url="http://crawler.test",
                    crawl_filters={"extra": "value"},
                )
            )

        self.assertEqual(result["reviews"][0]["app"], "ZaloPay")
        self.assertEqual(result["reviews_by_app"]["ZaloPay"][0]["id"], "sha256:delegated")
        self.assertEqual(result["stats"]["ZaloPay"]["qualified"], 1)
        self.assertEqual(result["service_results"]["ZaloPay"]["outcomes"][0]["result"], "success")

    def test_run_research_uses_deployed_crawler_service_by_default(self):
        agent_api = importlib.import_module("scripts.agent_api")
        service_payload = {
            "subject": "MoMo",
            "market": "VN",
            "goal": "qa",
            "focus": None,
            "reviews": [],
            "references": [],
            "stats": {},
            "outcomes": [],
        }

        async def fake_crawl_reviews(**kwargs):
            self.assertEqual(
                kwargs["base_url"],
                "https://endpoint-503c0bb0-c12f-4b54-919d-edc2c10b633e.agentbase-runtime.aiplatform.vngcloud.vn",
            )
            return dict(service_payload)

        with mock.patch.dict("os.environ", {}, clear=True):
            with mock.patch("scripts.crawl_client.crawl_reviews", side_effect=fake_crawl_reviews):
                result = agent_api.asyncio.run(
                    agent_api.run_research(
                        apps=["MoMo"],
                        goal="qa",
                        days_back=30,
                        sources=["voz"],
                    )
                )

        self.assertEqual(result["apps"], ["MoMo"])
        self.assertIn("MoMo", result["service_results"])


if __name__ == "__main__":
    unittest.main()
