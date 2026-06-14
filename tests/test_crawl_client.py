import asyncio
import unittest
from unittest import mock


class CrawlClientTests(unittest.TestCase):
    def test_crawl_reviews_posts_request_then_polls_until_completed(self):
        from scripts import crawl_client

        calls = []

        def fake_request(method, url, *, payload=None, token=None, timeout=30):
            calls.append((method, url, payload, token, timeout))
            if method == "POST":
                return {"job_id": "job-123", "status": "running"}
            return {
                "job_id": "job-123",
                "status": "completed",
                "subject": "TikTok Shop",
                "market": "UK",
                "reviews": [],
                "reviews_by_source": {},
                "references": [],
                "stats": {},
                "outcomes": [],
            }

        with mock.patch.object(crawl_client, "_request_json", side_effect=fake_request):
            result = asyncio.run(
                crawl_client.crawl_reviews(
                    base_url="https://crawler.example/",
                    subject="TikTok Shop",
                    market="UK",
                    goal="quality",
                    focus=None,
                    sources=["App Store", "CH Play"],
                    rating_min=1,
                    rating_max=5,
                    days_back=180,
                    filters={"x": "y"},
                    token="bearer-token",
                    poll_interval=0,
                )
            )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(calls[0][0], "POST")
        self.assertEqual(calls[0][1], "https://crawler.example/crawl")
        self.assertEqual(calls[0][2]["subject"], "TikTok Shop")
        self.assertEqual(calls[0][2]["data_source"], ["App Store", "CH Play"])
        self.assertEqual(calls[0][3], "bearer-token")
        self.assertEqual(calls[1][0], "GET")
        self.assertEqual(calls[1][1], "https://crawler.example/crawl/job-123")

    def test_crawl_reviews_raises_on_failed_job(self):
        from scripts import crawl_client

        def fake_request(method, url, *, payload=None, token=None, timeout=30):
            if method == "POST":
                return {"job_id": "job-123", "status": "running"}
            return {"job_id": "job-123", "status": "failed", "error": "boom"}

        with mock.patch.object(crawl_client, "_request_json", side_effect=fake_request):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                asyncio.run(
                    crawl_client.crawl_reviews(
                        base_url="https://crawler.example",
                        subject="MoMo",
                        market="VN",
                        goal="product",
                        focus=None,
                        sources=["Reddit"],
                        poll_interval=0,
                    )
                )


if __name__ == "__main__":
    unittest.main()
