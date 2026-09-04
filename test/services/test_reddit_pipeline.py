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


class TestDiscoverReport(PipelineTestCase):
    def test_counts_explain_an_empty_result(self):
        # "Nothing found" reads very differently when nothing was fetched than
        # when 2 posts were fetched and the filters rejected both, so the page
        # needs the counts, not just the list.
        with patch.object(
            reddit_pipeline, "fetch_posts", return_value=[_post("a"), _post("b")]
        ):
            report = reddit_pipeline.discover_report(_options(min_score=99_999))

        self.assertEqual(report["fetched"], 2)
        self.assertEqual(report["matched"], 0)
        self.assertEqual(report["candidates"], [])

    def test_a_failed_fetch_reports_zero_rather_than_raising(self):
        with patch.object(reddit_pipeline, "fetch_posts", return_value=[]):
            report = reddit_pipeline.discover_report(_options())
        self.assertEqual(report["fetched"], 0)
        self.assertEqual(report["candidates"], [])

    def test_stories_dropped_while_splitting_are_counted(self):
        with patch.object(reddit_pipeline, "fetch_posts", return_value=[_post("a")]):
            report = reddit_pipeline.discover_report(
                _options(part_seconds=1, max_parts=1, skip_truncated=True)
            )
        self.assertEqual(report["matched"], 1)
        self.assertEqual(report["skipped_truncated"], 1)
        self.assertEqual(report["candidates"], [])

    def test_discover_returns_the_candidates_of_the_report(self):
        with patch.object(reddit_pipeline, "fetch_posts", return_value=[_post("a")]):
            options = _options()
            self.assertEqual(
                reddit_pipeline.discover(options),
                reddit_pipeline.discover_report(options)["candidates"],
            )


class TestSchedulingParts(PipelineTestCase):
    def _approved(self, video_path):
        return {
            "post_id": "aaa",
            "index": 1,
            "total": 1,
            "title": "A story",
            "subreddit": "x",
            "permalink": "https://reddit.example/aaa",
            "video_path": video_path,
        }

    def _record_approved(self, video_path):
        reddit_queue.record_post(
            {
                "post_id": "aaa", "subreddit": "x", "title": "A story",
                "permalink": "", "score": 0, "truncated": False,
                "parts": [{"index": 1, "total": 1}],
            },
            [{"index": 1, "total": 1, "status": reddit_queue.STATUS_APPROVED,
              "video_path": video_path}],
        )

    def test_a_scheduled_part_records_its_job_and_slot(self):
        video = os.path.join(self._temp_dir.name, "part1.mp4")
        open(video, "wb").close()
        self._record_approved(video)

        outcome = reddit_pipeline.schedule_parts(
            [self._approved(video)],
            ["2026-01-01T10:00:00Z"],
            ["tiktok"],
            publish=lambda **kwargs: {"success": True, "job_id": "job-1"},
        )

        self.assertEqual(outcome, {"scheduled": 1, "failed": 0, "errors": []})
        part = reddit_queue.get_post("aaa")["parts"][0]
        self.assertEqual(part["status"], reddit_queue.STATUS_SCHEDULED)
        self.assertEqual(part["job_id"], "job-1")
        self.assertEqual(part["scheduled_for"], "2026-01-01T10:00:00Z")

    def test_a_missing_video_fails_the_part_instead_of_uploading(self):
        self._record_approved("/nowhere/part1.mp4")
        calls = []

        outcome = reddit_pipeline.schedule_parts(
            [self._approved("/nowhere/part1.mp4")],
            ["2026-01-01T10:00:00Z"],
            ["tiktok"],
            publish=lambda **kwargs: calls.append(kwargs) or {"success": True, "job_id": "x"},
        )

        self.assertEqual(calls, [])
        self.assertEqual(outcome["failed"], 1)
        part = reddit_queue.get_post("aaa")["parts"][0]
        self.assertEqual(part["status"], reddit_queue.STATUS_FAILED)
        self.assertIn("video file missing", part["error"])

    def test_an_upload_error_is_kept_on_the_part(self):
        video = os.path.join(self._temp_dir.name, "part1.mp4")
        open(video, "wb").close()
        self._record_approved(video)

        reddit_pipeline.schedule_parts(
            [self._approved(video)],
            ["2026-01-01T10:00:00Z"],
            ["tiktok"],
            publish=lambda **kwargs: {"success": False, "error": "quota exceeded"},
        )

        part = reddit_queue.get_post("aaa")["parts"][0]
        self.assertEqual(part["status"], reddit_queue.STATUS_FAILED)
        self.assertEqual(part["error"], "quota exceeded")

    def test_progress_is_reported_per_part(self):
        video = os.path.join(self._temp_dir.name, "part1.mp4")
        open(video, "wb").close()
        self._record_approved(video)
        seen = []

        reddit_pipeline.schedule_parts(
            [self._approved(video)],
            ["2026-01-01T10:00:00Z"],
            ["tiktok"],
            publish=lambda **kwargs: {"success": True, "job_id": "job-1"},
            on_part=lambda position, part: seen.append(position),
        )

        self.assertEqual(seen, [1])

    def test_the_caption_template_drives_the_title(self):
        with patch.dict(
            reddit_pipeline.config.app,
            {"reddit_caption_template": "{title} — part {index} of {total}"},
            clear=False,
        ):
            caption = reddit_pipeline.caption_for(
                {"title": "A story", "index": 2, "total": 3}
            )
        self.assertEqual(caption, "A story — part 2 of 3")

    def test_an_unknown_placeholder_falls_back_to_the_subject(self):
        with patch.dict(
            reddit_pipeline.config.app,
            {"reddit_caption_template": "{nonsense}"},
            clear=False,
        ):
            caption = reddit_pipeline.caption_for({"subject": "A story (Part 1/2)"})
        self.assertEqual(caption, "A story (Part 1/2)")


class TestSyncUploads(PipelineTestCase):
    def _scheduled(self, job_id="job-1"):
        reddit_queue.record_post(
            {
                "post_id": "aaa", "subreddit": "x", "title": "t",
                "permalink": "", "score": 0, "truncated": False,
                "parts": [{"index": 1, "total": 1}],
            },
            [{"index": 1, "total": 1, "status": reddit_queue.STATUS_SCHEDULED,
              "job_id": job_id}],
        )

    def test_a_published_job_becomes_uploaded(self):
        self._scheduled()
        outcome = reddit_pipeline.sync_uploads(
            check_status=lambda job_id: {"status": "completed"}
        )
        self.assertEqual(outcome, {"uploaded": 1, "failed": 0})
        self.assertEqual(
            reddit_queue.get_post("aaa")["parts"][0]["status"],
            reddit_queue.STATUS_UPLOADED,
        )

    def test_a_failed_job_keeps_its_reason(self):
        self._scheduled()
        reddit_pipeline.sync_uploads(
            check_status=lambda job_id: {"status": "failed", "error": "rejected by tiktok"}
        )
        part = reddit_queue.get_post("aaa")["parts"][0]
        self.assertEqual(part["status"], reddit_queue.STATUS_FAILED)
        self.assertEqual(part["error"], "rejected by tiktok")

    def test_a_pending_job_is_left_scheduled(self):
        self._scheduled()
        outcome = reddit_pipeline.sync_uploads(
            check_status=lambda job_id: {"status": "pending"}
        )
        self.assertEqual(outcome, {"uploaded": 0, "failed": 0})
        self.assertEqual(
            reddit_queue.get_post("aaa")["parts"][0]["status"],
            reddit_queue.STATUS_SCHEDULED,
        )

    def test_a_poll_that_failed_says_nothing_about_the_job(self):
        # A network error must never be recorded as a failed publish: the video
        # may well go out on schedule.
        self._scheduled()
        reddit_pipeline.sync_uploads(
            check_status=lambda job_id: {"success": False, "error": "connection reset"}
        )
        self.assertEqual(
            reddit_queue.get_post("aaa")["parts"][0]["status"],
            reddit_queue.STATUS_SCHEDULED,
        )

    def test_a_raising_poll_leaves_the_part_alone(self):
        self._scheduled()

        def explode(job_id):
            raise RuntimeError("boom")

        outcome = reddit_pipeline.sync_uploads(check_status=explode)
        self.assertEqual(outcome, {"uploaded": 0, "failed": 0})
        self.assertEqual(
            reddit_queue.get_post("aaa")["parts"][0]["status"],
            reddit_queue.STATUS_SCHEDULED,
        )

    def test_a_part_without_a_job_id_is_skipped(self):
        self._scheduled(job_id=None)
        polled = []
        reddit_pipeline.sync_uploads(
            check_status=lambda job_id: polled.append(job_id) or {}
        )
        self.assertEqual(polled, [])


if __name__ == "__main__":
    unittest.main()
