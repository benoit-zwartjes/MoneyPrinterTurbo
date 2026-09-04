import unittest
from unittest.mock import patch

from app.services import reddit_source


def _child(**overrides) -> dict:
    data = {
        "id": "abc123",
        "subreddit": "AmItheAsshole",
        "title": "AITA for saying no?",
        "selftext": "A story with plenty of words in it.",
        "author": "someone",
        "score": 1200,
        "num_comments": 340,
        "created_utc": 1_700_000_000.0,
        "permalink": "/r/AmItheAsshole/comments/abc123/",
        "over_18": False,
        "spoiler": False,
        "stickied": False,
        "locked": False,
        "is_self": True,
    }
    data.update(overrides)
    return {"kind": "t3", "data": data}


def _post(**overrides) -> dict:
    post = reddit_source._normalize_post(_child(**overrides))
    assert post is not None
    return post


class TestNormalizePost(unittest.TestCase):
    def test_flattens_a_listing_child(self):
        post = _post()
        self.assertEqual(post["id"], "abc123")
        self.assertEqual(post["score"], 1200)
        self.assertTrue(post["is_self"])
        self.assertEqual(
            post["permalink"],
            "https://www.reddit.com/r/AmItheAsshole/comments/abc123/",
        )

    def test_rejects_children_without_an_id_or_title(self):
        self.assertIsNone(reddit_source._normalize_post(_child(id="")))
        self.assertIsNone(reddit_source._normalize_post(_child(title="")))
        self.assertIsNone(reddit_source._normalize_post({"data": "not a dict"}))
        self.assertIsNone(reddit_source._normalize_post("nonsense"))

    def test_non_numeric_score_falls_back_to_zero(self):
        self.assertEqual(_post(score=None)["score"], 0)
        self.assertEqual(_post(score="lots")["score"], 0)


class TestFilterPosts(unittest.TestCase):
    def test_keeps_a_normal_self_post(self):
        self.assertEqual(len(reddit_source.filter_posts([_post()], min_score=100)), 1)

    def test_drops_link_posts_and_stickied_announcements(self):
        self.assertEqual(reddit_source.filter_posts([_post(is_self=False)]), [])
        self.assertEqual(reddit_source.filter_posts([_post(stickied=True)]), [])

    def test_drops_removed_and_deleted_bodies(self):
        # Reddit keeps the listing entry after a removal and swaps the body
        # for a marker, so these arrive looking like ordinary self-posts.
        self.assertEqual(reddit_source.filter_posts([_post(selftext="[removed]")]), [])
        self.assertEqual(reddit_source.filter_posts([_post(selftext="[deleted]")]), [])
        self.assertEqual(reddit_source.filter_posts([_post(selftext="   ")]), [])

    def test_nsfw_is_excluded_unless_allowed(self):
        nsfw = [_post(over_18=True)]
        self.assertEqual(reddit_source.filter_posts(nsfw), [])
        self.assertEqual(len(reddit_source.filter_posts(nsfw, allow_nsfw=True)), 1)

    def test_score_and_word_bounds_are_applied(self):
        self.assertEqual(reddit_source.filter_posts([_post()], min_score=5000), [])
        self.assertEqual(reddit_source.filter_posts([_post()], min_words=100), [])
        self.assertEqual(reddit_source.filter_posts([_post()], max_words=3), [])

    def test_already_seen_ids_are_skipped(self):
        self.assertEqual(
            reddit_source.filter_posts([_post()], exclude_ids={"abc123"}), []
        )


class TestUserAgent(unittest.TestCase):
    def test_uses_the_format_reddit_requires(self):
        with patch.dict(
            reddit_source.config.app, {"reddit_username": "someone"}, clear=False
        ):
            agent = reddit_source.user_agent()
        self.assertTrue(agent.startswith("server:MoneyPrinterTurbo:"))
        self.assertTrue(agent.endswith("(by /u/someone)"))

    def test_a_configured_agent_wins(self):
        with patch.dict(
            reddit_source.config.app,
            {"reddit_user_agent": "custom:thing:1.0 (by /u/me)"},
            clear=False,
        ):
            self.assertEqual(
                reddit_source.user_agent(), "custom:thing:1.0 (by /u/me)"
            )


class TestIsConfigured(unittest.TestCase):
    def test_requires_both_halves_of_the_credential(self):
        with patch.dict(
            reddit_source.config.app,
            {"reddit_client_id": "id", "reddit_client_secret": ""},
            clear=False,
        ):
            self.assertFalse(reddit_source.is_configured())

        with patch.dict(
            reddit_source.config.app,
            {"reddit_client_id": "id", "reddit_client_secret": "secret"},
            clear=False,
        ):
            self.assertTrue(reddit_source.is_configured())


if __name__ == "__main__":
    unittest.main()
