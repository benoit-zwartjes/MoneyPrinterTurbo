import unittest
from unittest.mock import patch

from app.services import reddit_apify


def _item(**overrides) -> dict:
    item = {
        "dataType": "post",
        "id": "t3_abc123",
        "parsedId": "abc123",
        "url": "https://www.reddit.com/r/AmItheAsshole/comments/abc123/thing/",
        "title": "AITA for saying no?",
        "body": "A story with plenty of words in it.",
        "username": "someone",
        "communityName": "r/AmItheAsshole",
        "parsedCommunityName": "AmItheAsshole",
        "numberOfComments": 340,
        "upVotes": 1200,
        "over18": False,
        "createdAt": "2026-01-15T10:30:00.000Z",
    }
    item.update(overrides)
    return item


class TestBuildInput(unittest.TestCase):
    def test_listing_url_carries_sort_and_time(self):
        payload = reddit_apify.build_input(
            ["AmItheAsshole", "tifu"], "top", "week", 25, False
        )
        urls = [entry["url"] for entry in payload["startUrls"]]
        self.assertEqual(
            urls,
            [
                "https://www.reddit.com/r/AmItheAsshole/top/?t=week",
                "https://www.reddit.com/r/tifu/top/?t=week",
            ],
        )

    def test_time_window_is_omitted_for_listings_that_ignore_it(self):
        payload = reddit_apify.build_input(["tifu"], "new", "week", 25, False)
        self.assertEqual(payload["startUrls"][0]["url"], "https://www.reddit.com/r/tifu/new/")

    def test_subreddit_prefixes_are_tolerated(self):
        payload = reddit_apify.build_input(["r/tifu", "/r/pics/"], "hot", "day", 10, False)
        urls = [entry["url"] for entry in payload["startUrls"]]
        self.assertEqual(
            urls,
            ["https://www.reddit.com/r/tifu/hot/", "https://www.reddit.com/r/pics/hot/"],
        )

    def test_comments_are_switched_off(self):
        # Every comment returned is billable on a pay-per-result actor, and a
        # story recap never narrates them.
        payload = reddit_apify.build_input(["tifu"], "top", "day", 25, False)
        self.assertTrue(payload["skipComments"])
        self.assertEqual(payload["maxComments"], 0)

    def test_media_links_are_requested_so_scores_come_back(self):
        # The actor only populates upVotes when includeMediaLinks is set;
        # without it every post scores 0 and fails the minimum-score filter.
        payload = reddit_apify.build_input(["tifu"], "top", "day", 25, False)
        self.assertTrue(payload["includeMediaLinks"])

    def test_nsfw_flag_is_passed_through(self):
        self.assertFalse(
            reddit_apify.build_input(["tifu"], "top", "day", 25, False)["includeNSFW"]
        )
        self.assertTrue(
            reddit_apify.build_input(["tifu"], "top", "day", 25, True)["includeNSFW"]
        )

    def test_max_items_scales_with_the_subreddit_count(self):
        payload = reddit_apify.build_input(["a", "b", "c"], "top", "day", 10, False)
        self.assertEqual(payload["maxItems"], 30)
        self.assertEqual(payload["maxPostCount"], 10)


class TestNormalizeItem(unittest.TestCase):
    def test_maps_actor_fields_onto_the_shared_shape(self):
        post = reddit_apify.normalize_item(_item())
        self.assertEqual(post["id"], "abc123")
        self.assertEqual(post["subreddit"], "AmItheAsshole")
        self.assertEqual(post["score"], 1200)
        self.assertEqual(post["num_comments"], 340)
        self.assertEqual(post["selftext"], "A story with plenty of words in it.")
        self.assertTrue(post["is_self"])
        self.assertEqual(
            post["permalink"],
            "https://www.reddit.com/r/AmItheAsshole/comments/abc123/thing/",
        )

    def test_produces_the_same_keys_as_the_official_backend(self):
        from app.services import reddit_source

        official = reddit_source._normalize_post(
            {
                "data": {
                    "id": "abc123", "subreddit": "AmItheAsshole", "title": "T",
                    "selftext": "B", "author": "a", "score": 1, "num_comments": 1,
                    "created_utc": 1.0, "permalink": "/r/x/", "over_18": False,
                    "spoiler": False, "stickied": False, "locked": False,
                    "is_self": True,
                }
            }
        )
        self.assertEqual(set(reddit_apify.normalize_item(_item())), set(official))

    def test_iso_timestamp_becomes_epoch_seconds(self):
        post = reddit_apify.normalize_item(_item())
        self.assertAlmostEqual(post["created_utc"], 1768473000.0, delta=1)

    def test_unparseable_timestamp_falls_back_to_zero(self):
        self.assertEqual(reddit_apify.normalize_item(_item(createdAt="soon"))["created_utc"], 0.0)
        self.assertEqual(reddit_apify.normalize_item(_item(createdAt=None))["created_utc"], 0.0)

    def test_community_name_falls_back_to_the_prefixed_form(self):
        item = _item()
        del item["parsedCommunityName"]
        self.assertEqual(reddit_apify.normalize_item(item)["subreddit"], "AmItheAsshole")

    def test_link_post_with_no_body_is_not_a_self_post(self):
        # The actor gives no is_self flag, so an empty body is the only signal
        # that this is a link post with nothing to narrate.
        self.assertFalse(reddit_apify.normalize_item(_item(body=""))["is_self"])

    def test_missing_upvotes_scores_zero_rather_than_raising(self):
        item = _item()
        del item["upVotes"]
        self.assertEqual(reddit_apify.normalize_item(item)["score"], 0)

    def test_non_post_items_are_dropped(self):
        self.assertIsNone(reddit_apify.normalize_item(_item(dataType="comment")))
        self.assertIsNone(reddit_apify.normalize_item(_item(dataType="community")))

    def test_items_without_an_id_or_title_are_dropped(self):
        self.assertIsNone(reddit_apify.normalize_item(_item(parsedId="", id="")))
        self.assertIsNone(reddit_apify.normalize_item(_item(title="")))
        self.assertIsNone(reddit_apify.normalize_item("nonsense"))


class TestFetchStoryPosts(unittest.TestCase):
    def test_sorts_by_score_and_drops_unusable_items(self):
        items = [
            _item(parsedId="low", upVotes=10),
            _item(parsedId="high", upVotes=9000),
            _item(parsedId="comment", dataType="comment"),
        ]
        with patch.object(reddit_apify, "run_actor", return_value=items):
            posts = reddit_apify.fetch_story_posts(["tifu"])
        self.assertEqual([p["id"] for p in posts], ["high", "low"])

    def test_no_subreddits_means_no_run(self):
        with patch.object(reddit_apify, "run_actor") as run:
            with patch.dict(reddit_apify.config.app, {"reddit_subreddits": []}, clear=False):
                self.assertEqual(reddit_apify.fetch_story_posts([]), [])
        run.assert_not_called()


class TestRunActor(unittest.TestCase):
    def test_missing_token_skips_the_run(self):
        with patch.dict(reddit_apify.config.app, {"reddit_apify_token": ""}, clear=False):
            with patch.object(reddit_apify, "_post") as post:
                self.assertEqual(reddit_apify.run_actor({}), [])
            post.assert_not_called()

    def test_polls_until_the_run_reaches_a_terminal_state(self):
        with patch.dict(reddit_apify.config.app, {"reddit_apify_token": "tok"}, clear=False):
            with patch.object(reddit_apify, "_post") as post, \
                 patch.object(reddit_apify, "_get") as get, \
                 patch.object(reddit_apify.time, "sleep"):
                post.return_value = {
                    "data": {"id": "run1", "defaultDatasetId": "ds1", "status": "RUNNING"}
                }
                get.side_effect = [
                    {"data": {"status": "RUNNING"}},
                    {"data": {"status": "SUCCEEDED"}},
                    [_item()],
                ]
                items = reddit_apify.run_actor({})

        self.assertEqual(len(items), 1)
        self.assertEqual(get.call_args_list[-1].args[0], "/datasets/ds1/items")

    def test_a_failed_run_returns_nothing(self):
        with patch.dict(reddit_apify.config.app, {"reddit_apify_token": "tok"}, clear=False):
            with patch.object(reddit_apify, "_post") as post, \
                 patch.object(reddit_apify, "_get") as get, \
                 patch.object(reddit_apify.time, "sleep"):
                post.return_value = {
                    "data": {"id": "run1", "defaultDatasetId": "ds1", "status": "RUNNING"}
                }
                get.return_value = {"data": {"status": "FAILED"}}
                self.assertEqual(reddit_apify.run_actor({}), [])

    def test_a_run_that_never_starts_returns_nothing(self):
        with patch.dict(reddit_apify.config.app, {"reddit_apify_token": "tok"}, clear=False):
            with patch.object(reddit_apify, "_post", return_value=None):
                self.assertEqual(reddit_apify.run_actor({}), [])

    def test_token_is_sent_as_a_header_not_a_query_parameter(self):
        with patch.dict(reddit_apify.config.app, {"reddit_apify_token": "secret"}, clear=False):
            headers = reddit_apify._headers()
        self.assertEqual(headers["Authorization"], "Bearer secret")


if __name__ == "__main__":
    unittest.main()
