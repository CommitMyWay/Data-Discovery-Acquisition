import importlib
import unittest


class ProcessingTests(unittest.TestCase):
    def setUp(self):
        self.processing = importlib.import_module("scripts.processing")

    def test_make_review_fills_schema_and_generates_id(self):
        raw = {
            "source": "voz", "app": "MoMo", "author": "user1",
            "content": "  Chuyển tiền nhanh, không lỗi.  ",
            "date": "2026-05-01", "url": "https://voz.vn/t/abc",
        }
        r = self.processing.make_review(raw)
        self.assertEqual(r["source"], "voz")
        self.assertEqual(r["app"], "MoMo")
        self.assertEqual(r["content"], "Chuyển tiền nhanh, không lỗi.")
        self.assertTrue(r["id"].startswith("sha256:"))
        self.assertIsNone(r["rating"])
        self.assertIsNone(r["qualified"])
        self.assertEqual(r["disqualification_reasons"], [])
        self.assertIn("thread_title", r["metadata"])

    def test_make_review_preserves_existing_id_and_casts_rating(self):
        raw = {
            "id": "sha256:keep", "source": "app_store", "app": "MoMo",
            "rating": "4", "content": "thanh toán tốt",
            "metadata": {"review_title": "ok"},
        }
        r = self.processing.make_review(raw)
        self.assertEqual(r["id"], "sha256:keep")
        self.assertEqual(r["rating"], 4)
        self.assertEqual(r["metadata"]["review_title"], "ok")

    def test_make_review_handles_bad_rating_and_default_app(self):
        raw = {"source": "reddit", "rating": "n/a", "content": "x"}
        r = self.processing.make_review(raw, default_app="ZaloPay")
        self.assertIsNone(r["rating"])
        self.assertEqual(r["app"], "ZaloPay")

    def test_to_iso_handles_epoch_and_none(self):
        self.assertIsNone(self.processing.to_iso(None))
        self.assertTrue(self.processing.to_iso(0).startswith("1970-01-01"))


if __name__ == "__main__":
    unittest.main()
