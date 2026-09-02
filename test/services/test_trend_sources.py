import unittest
from unittest.mock import MagicMock, patch

import requests

from app.services import trend_sources


def _response(*, json_payload=None, content=b""):
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.content = content
    if json_payload is None:
        response.json.side_effect = ValueError("not json")
    else:
        response.json.return_value = json_payload
    return response


def _wiki_payload(*articles):
    return {"items": [{"articles": [{"article": name} for name in articles]}]}


RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
<item><title>how volcanoes erupt</title></item>
<item><title>sourdough starter</title></item>
<item><title>   </title></item>
<item><title>how volcanoes erupt</title></item>
</channel></rss>"""


class TestTrendSources(unittest.TestCase):
    def test_wikipedia_titles_are_cleaned_and_deduped(self):
        payload = _wiki_payload(
            "Main_Page",
            "Special:Search",
            "Deep_sea_creature",
            "Sourdough",
            "deep_sea_creature",
        )
        with patch.object(
            trend_sources, "_request", return_value=_response(json_payload=payload)
        ):
            result = trend_sources.fetch_trending_tags("wikipedia", limit=10)

        # 导航页和命名空间页被剔除，下划线还原成空格，重复条目只保留一条。
        self.assertEqual(result, ["Deep sea creature", "Sourdough"])

    def test_wikipedia_respects_the_limit(self):
        payload = _wiki_payload("A", "B", "C", "D")
        with patch.object(
            trend_sources, "_request", return_value=_response(json_payload=payload)
        ):
            result = trend_sources.fetch_trending_tags("wikipedia", limit=2)

        self.assertEqual(result, ["A", "B"])

    def test_wikipedia_queries_the_previous_utc_day(self):
        """当天数据在 UTC 日切前不完整，直接查询会返回 404。"""
        captured = {}

        def fake_request(url):
            captured["url"] = url
            return _response(json_payload=_wiki_payload("A"))

        with patch.object(trend_sources, "_request", side_effect=fake_request):
            trend_sources.fetch_trending_tags("wikipedia", region="DE")

        from datetime import datetime, timedelta, timezone

        expected = datetime.now(timezone.utc).date() - timedelta(days=1)
        self.assertIn("de.wikipedia", captured["url"])
        self.assertIn(expected.strftime("%Y/%m/%d"), captured["url"])

    def test_google_trends_rss_is_parsed_and_deduped(self):
        with patch.object(
            trend_sources, "_request", return_value=_response(content=RSS)
        ):
            result = trend_sources.fetch_trending_tags("google_trends", limit=10)

        self.assertEqual(result, ["how volcanoes erupt", "sourdough starter"])

    def test_malformed_rss_returns_empty_list(self):
        with patch.object(
            trend_sources, "_request", return_value=_response(content=b"<rss")
        ):
            self.assertEqual(
                trend_sources.fetch_trending_tags("google_trends"), []
            )

    def test_wikipedia_non_json_payload_returns_empty_list(self):
        with patch.object(
            trend_sources, "_request", return_value=_response()
        ):
            self.assertEqual(trend_sources.fetch_trending_tags("wikipedia"), [])

    def test_network_failure_returns_empty_list_instead_of_raising(self):
        """
        热榜拉取是选题流程的辅助步骤。外部服务故障必须降级成"这次没取到"，
        不能中断用户手动粘贴标签的主路径。
        """
        with patch.object(
            trend_sources.requests,
            "get",
            side_effect=requests.ConnectionError("boom"),
        ):
            self.assertEqual(trend_sources.fetch_trending_tags("wikipedia"), [])
            self.assertEqual(trend_sources.fetch_trending_tags("google_trends"), [])

    def test_unsupported_source_returns_empty_list(self):
        self.assertEqual(trend_sources.fetch_trending_tags("tiktok"), [])

    def test_region_and_limit_are_normalized(self):
        self.assertEqual(trend_sources._normalize_region("de"), "DE")
        self.assertEqual(trend_sources._normalize_region("bogus"), "US")
        self.assertEqual(trend_sources._normalize_region(None), "US")
        # 0 属于假值，沿用仓库既有约定回退到默认值；负数才被夹到下界。
        self.assertEqual(
            trend_sources._normalize_limit(0), trend_sources.DEFAULT_LIMIT
        )
        self.assertEqual(trend_sources._normalize_limit(-5), 1)
        self.assertEqual(
            trend_sources._normalize_limit(9999), trend_sources.MAX_LIMIT
        )
        self.assertEqual(
            trend_sources._normalize_limit("x"), trend_sources.DEFAULT_LIMIT
        )

    def test_unknown_region_falls_back_to_english_wikipedia(self):
        captured = {}

        def fake_request(url):
            captured["url"] = url
            return _response(json_payload=_wiki_payload("A"))

        with patch.object(trend_sources, "_request", side_effect=fake_request):
            trend_sources.fetch_trending_tags("wikipedia", region="ZZ")

        self.assertIn("en.wikipedia", captured["url"])

    def test_request_sends_a_user_agent_and_honours_tls_config(self):
        """Wikimedia 要求带 User-Agent；TLS 校验必须沿用全局配置。"""
        with patch.object(trend_sources.requests, "get") as get:
            get.return_value = _response(json_payload=_wiki_payload("A"))
            trend_sources.fetch_trending_tags("wikipedia")

        kwargs = get.call_args.kwargs
        self.assertIn("MoneyPrinterTurbo", kwargs["headers"]["User-Agent"])
        self.assertTrue(kwargs["verify"])
        self.assertEqual(kwargs["timeout"], trend_sources._REQUEST_TIMEOUT)

    def test_output_feeds_the_topic_parser_without_losing_entries(self):
        """拉取结果每行一条，必须能被 parse_trending_input 完整接收。"""
        from app.services import llm

        payload = _wiki_payload("Deep_sea_creature", "Sourdough", "Urban_farming")
        with patch.object(
            trend_sources, "_request", return_value=_response(json_payload=payload)
        ):
            tags = trend_sources.fetch_trending_tags("wikipedia")

        self.assertEqual(
            llm.parse_trending_input("\n".join(tags)),
            ["Deep sea creature", "Sourdough", "Urban farming"],
        )


if __name__ == "__main__":
    unittest.main()
