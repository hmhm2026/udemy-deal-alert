import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / "check_deals.py"
spec = importlib.util.spec_from_file_location("deal_bot", MODULE)
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)


class FakeResponse:
    def __init__(self, body, url="https://www.udemy.com/course/example/"):
        self.body = body.encode("utf-8")
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def geturl(self):
        return self.url

    def read(self):
        return self.body


class CouponVerificationTests(unittest.TestCase):
    url = "https://www.udemy.com/course/example/?couponCode=ABC123"

    def check(self, page):
        with patch.object(bot.urllib.request, "urlopen", return_value=FakeResponse(page)):
            return bot.verify_udemy_coupon(self.url)

    def test_explicit_zero_price_is_free(self):
        page = '<script type="application/ld+json">{"@type":"Course","offers":{"@type":"Offer","price":"0"}}</script>'
        self.assertEqual(self.check(page), "ACTIVE_FREE")

    def test_positive_price_is_not_free(self):
        page = '<script type="application/ld+json">{"@type":"Course","offers":{"@type":"Offer","price":"19.99"}}</script>'
        self.assertEqual(self.check(page), "NOT_FREE")

    def test_expired_coupon_is_not_free(self):
        self.assertEqual(self.check("Coupon code has expired"), "NOT_FREE")

    def test_unclear_page_fails_closed(self):
        self.assertEqual(self.check("Course landing page"), "UNVERIFIED")


if __name__ == "__main__":
    unittest.main()
