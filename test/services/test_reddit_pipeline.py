import os
import tempfile
import unittest
from unittest.mock import patch

from app.models import const
from app.services import reddit_pipeline, reddit_queue


def _post(post_id: str, sentences: int = 6) -> dict:
    return {
        "id": post_id,
        "subreddit": "AmItheAsshole",
        "title": f"AITA about thing {post_id}?",
        "selftext": " ".join(
            f"This is sentence number {i} of the story." for i in range(sentences)
        ),
        "author": "someone",
        "score": 2000,
        "num_comments": 100,
        "created_utc": 1_700_000_000.0,
        "permalink": f"https://www.reddit.com/r/AmItheAsshole/comments/{post_id}/",
        "over_18": False,
        "spoiler": False,
        "stickied": False,
        "locked": False,
        "is_self": True,
    }


def _options(**overrides) -> dict:
    options = {
        "provider": reddit_pipeline.PROVIDER_APIFY,
        "subreddits": ["AmItheAsshole"],
        "listing": "top",
        "time_filter": "day",
        "fetch_limit": 50,
        "max_posts": 2,
        "min_score": 0,
        "min_words": 0,
        "max_words": 0,
        "allow_nsfw": False,
        "skip_truncated": True,
        "part_seconds": 20,
        "max_parts": 10,
        "video_terms": "test terms",
        "video_source": "pexels",
        "video_aspect": "9:16",
        "voice_name": "en-US-AriaNeural-Female",
        "subtitle_enabled": True,
    }
    options.update(overrides)
    return options


class PipelineTestCase(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        patcher = patch.object(
            reddit_queue.utils, "storage_dir", return_value=self._temp_dir.name
        )
        patcher.start()
        self.addCleanup(patcher.stop)


class TestProviderSwitch(PipelineTestCase):
    def test_apify_provider_uses_the_actor_backend(self):
        with patch.object(reddit_pipeline.reddit_apify, "fetch_story_posts", return_value=[]) as apify, \
             patch.object(reddit_pipeline.reddit_source, "fetch_story_posts", return_value=[]) as official:
            reddit_pipeline.fetch_posts(_options(provider="apify"))
        apify.assert_called_once()
        official.assert_not_called()

    def test_official_provider_uses_the_api_backend(self):
        with patch.object(reddit_pipeline.reddit_apify, "fetch_story_posts", return_value=[]) as apify, \
             patch.object(reddit_pipeline.reddit_source, "fetch_story_posts", return_value=[]) as official:
            reddit_pipeline.fetch_posts(_options(provider="official"))
        official.assert_called_once()
        apify.assert_not_called()

    def test_nsfw_preference_reaches_the_actor(self):
        # The actor filters NSFW during the scrape; passing it through avoids
        # paying for results the local filter would then throw away.
        with patch.object(reddit_pipeline.reddit_apify, "fetch_story_posts", return_value=[]) as apify:
            reddit_pipeline.fetch_posts(_options(provider="apify", allow_nsfw=True))
        self.assertTrue(apify.call_args.kwargs["allow_nsfw"])

    def test_unknown_provider_falls_back_to_the_default(self):
        with patch.dict(
            reddit_pipeline.config.app, {"reddit_provider": "nonsense"}, clear=False
        ):
            self.assertEqual(
                reddit_pipeline.resolve_options()["provider"],
                reddit_pipeline.DEFAULT_PROVIDER,
            )

    def test_blocking_issues_name_the_selected_backend(self):
        with patch.object(reddit_pipeline.reddit_apify, "is_configured", return_value=False):
            issues = reddit_pipeline.blocking_issues(provider="apify")
        self.assertEqual(len(issues), 1)
        self.assertIn("Apify", issues[0])

        with patch.object(reddit_pipeline.reddit_source, "is_configured", return_value=False):
            issues = reddit_pipeline.blocking_issues(provider="official")
        self.assertEqual(len(issues), 1)
        self.assertIn("reddit_client_id", issues[0])


class TestResolveOptions(PipelineTestCase):
    def test_overrides_beat_config(self):
        with patch.dict(reddit_pipeline.config.app, {"reddit_min_score": 500}, clear=False):
            self.assertEqual(reddit_pipeline.resolve_options()["min_score"], 500)
            self.assertEqual(
                reddit_pipeline.resolve_options(min_score=10)["min_score"], 10
            )

    def test_none_overrides_are_ignored(self):
        with patch.dict(reddit_pipeline.config.app, {"reddit_min_score": 500}, clear=False):
            self.assertEqual(
                reddit_pipeline.resolve_options(min_score=None)["min_score"], 500
            )

    def test_subreddits_accept_a_comma_separated_string(self):
        self.assertEqual(
            reddit_pipeline.resolve_options(subreddits="tifu, pics")["subreddits"],
            ["tifu", "pics"],
        )


class TestDiscover(PipelineTestCase):
    def test_stops_at_max_posts(self):
        posts = [_post(f"post{i}") for i in range(5)]
        with patch.object(reddit_pipeline, "fetch_posts", return_value=posts):
            self.assertEqual(len(reddit_pipeline.discover(_options(max_posts=2))), 2)

    def test_skips_posts_already_in_the_queue(self):
        reddit_queue.record_post(
            {"post_id": "seen", "parts": []},
            [{"index": 1, "total": 1, "status": reddit_queue.STATUS_RENDERED}],
        )
        with patch.object(
            reddit_pipeline, "fetch_posts", return_value=[_post("seen"), _post("fresh")]
        ):
            splits = reddit_pipeline.discover(_options())
        self.assertEqual([s["post_id"] for s in splits], ["fresh"])

    def test_skips_stories_too_long_for_max_parts(self):
        with patch.object(reddit_pipeline, "fetch_posts", return_value=[_post("long", 200)]):
            splits = reddit_pipeline.discover(_options(max_parts=2, skip_truncated=True))
        self.assertEqual(splits, [])

    def test_empty_fetch_yields_nothing(self):
        with patch.object(reddit_pipeline, "fetch_posts", return_value=[]):
            self.assertEqual(reddit_pipeline.discover(_options()), [])


class TestBuildVideoParams(PipelineTestCase):
    def test_supplies_the_script_directly(self):
        part = {"subject": "A story (Part 1/2)", "script": "Narration text."}
        params = reddit_pipeline.build_video_params(part, _options())
        # A supplied script skips LLM generation, which is the whole point of a
        # recap: narrate the post rather than write about it.
        self.assertEqual(params.video_script, "Narration text.")
        self.assertEqual(params.video_subject, "A story (Part 1/2)")
        self.assertEqual(params.video_aspect, "9:16")
        self.assertEqual(params.video_count, 1)


class TestSubmitParts(PipelineTestCase):
    def _split(self, post_id="aaa", parts=2) -> dict:
        return {
            "post_id": post_id,
            "subreddit": "AmItheAsshole",
            "title": "A story",
            "permalink": "https://www.reddit.com/r/x/",
            "score": 900,
            "truncated": False,
            "parts": [
                {
                    "index": i,
                    "total": parts,
                    "script": "text",
                    "subject": f"A story (Part {i}/{parts})",
                    "estimated_seconds": 40.0,
                }
                for i in range(1, parts + 1)
            ],
        }

    def test_records_every_part_as_rendering(self):
        calls = []
        result = reddit_pipeline.submit_parts(
            [self._split()],
            _options(),
            lambda task_id, params, part, split: calls.append(task_id),
        )
        self.assertEqual(result, {"posts": 1, "submitted": 2, "failed": 0})
        parts = reddit_queue.get_post("aaa")["parts"]
        self.assertTrue(all(p["status"] == reddit_queue.STATUS_RENDERING for p in parts))
        self.assertEqual([p["task_id"] for p in parts], calls)

    def test_a_submit_failure_marks_that_part_failed_and_keeps_going(self):
        def submit(task_id, params, part, split):
            if part["index"] == 1:
                raise RuntimeError("pool is full")

        result = reddit_pipeline.submit_parts([self._split()], _options(), submit)
        self.assertEqual(result, {"posts": 1, "submitted": 1, "failed": 1})
        parts = reddit_queue.get_post("aaa")["parts"]
        self.assertEqual(parts[0]["status"], reddit_queue.STATUS_FAILED)
        self.assertIn("pool is full", parts[0]["error"])
        self.assertEqual(parts[1]["status"], reddit_queue.STATUS_RENDERING)


class TestSyncRendering(PipelineTestCase):
    def _queue_rendering(self, parts=2):
        reddit_queue.record_post(
            {
                "post_id": "aaa", "subreddit": "x", "title": "t",
                "permalink": "", "score": 0, "truncated": False,
                "parts": [{"index": i, "total": parts} for i in range(1, parts + 1)],
            },
            [
                {
                    "index": i, "total": parts, "task_id": f"task-{i}",
                    "status": reddit_queue.STATUS_RENDERING,
                }
                for i in range(1, parts + 1)
            ],
        )

    def test_completed_task_becomes_rendered_with_its_video(self):
        self._queue_rendering(1)
        tasks = {"task-1": {"state": const.TASK_STATE_COMPLETE, "videos": ["/v1.mp4"]}}
        self.assertEqual(
            reddit_pipeline.sync_rendering(tasks.get), {"rendered": 1, "failed": 0}
        )
        part = reddit_queue.get_post("aaa")["parts"][0]
        self.assertEqual(part["status"], reddit_queue.STATUS_RENDERED)
        self.assertEqual(part["video_path"], "/v1.mp4")

    def test_failed_task_becomes_failed(self):
        self._queue_rendering(1)
        tasks = {"task-1": {"state": const.TASK_STATE_FAILED, "error": "boom"}}
        reddit_pipeline.sync_rendering(tasks.get)
        part = reddit_queue.get_post("aaa")["parts"][0]
        self.assertEqual(part["status"], reddit_queue.STATUS_FAILED)
        self.assertEqual(part["error"], "boom")

    def test_a_still_running_task_is_left_alone(self):
        self._queue_rendering(1)
        tasks = {"task-1": {"state": const.TASK_STATE_PROCESSING, "progress": 40}}
        self.assertEqual(
            reddit_pipeline.sync_rendering(tasks.get), {"rendered": 0, "failed": 0}
        )
        self.assertEqual(
            reddit_queue.get_post("aaa")["parts"][0]["status"],
            reddit_queue.STATUS_RENDERING,
        )

    def test_a_forgotten_task_fails_rather_than_rendering_forever(self):
        # A restart mid-render loses in-memory task state. Leaving the part in
        # 'rendering' would strand it outside the review queue with nothing
        # able to move it on.
        self._queue_rendering(1)
        reddit_pipeline.sync_rendering(lambda task_id: None)
        part = reddit_queue.get_post("aaa")["parts"][0]
        self.assertEqual(part["status"], reddit_queue.STATUS_FAILED)
        self.assertIn("no longer tracked", part["error"])

    def test_completed_task_without_a_video_fails(self):
        self._queue_rendering(1)
        tasks = {"task-1": {"state": const.TASK_STATE_COMPLETE, "videos": []}}
        reddit_pipeline.sync_rendering(tasks.get)
        self.assertEqual(
            reddit_queue.get_post("aaa")["parts"][0]["status"],
            reddit_queue.STATUS_FAILED,
        )


if __name__ == "__main__":
    unittest.main()
