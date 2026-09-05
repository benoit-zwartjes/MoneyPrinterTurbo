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
        "background": "pexels",
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

    def test_every_matching_story_is_filed_even_past_max_posts(self):
        """
        max_posts caps what a run renders, not what it remembers.

        Dropping the rest would throw away stories the fetch has already been
        paid for, and the next run would fetch and split them all over again.
        """
        posts = [_post(f"post{i}") for i in range(5)]
        with patch.object(reddit_pipeline, "fetch_posts", return_value=posts):
            report = reddit_pipeline.discover_report(_options(max_posts=2))

        self.assertEqual(report["added"], 5)
        self.assertEqual(len(reddit_queue.backlog()), 5)

    def test_a_second_search_offers_nothing_it_already_found(self):
        """The point of the backlog: a story is fetched, split and offered once."""
        posts = [_post("a"), _post("b")]
        with patch.object(reddit_pipeline, "fetch_posts", return_value=posts):
            first = reddit_pipeline.discover_report(_options())
            second = reddit_pipeline.discover_report(_options())

        self.assertEqual(first["added"], 2)
        self.assertEqual(second["fetched"], 2)
        self.assertEqual(second["matched"], 0)
        self.assertEqual(second["added"], 0)
        self.assertEqual(len(reddit_queue.backlog()), 2)

    def test_an_archived_story_is_never_offered_again(self):
        with patch.object(reddit_pipeline, "fetch_posts", return_value=[_post("a")]):
            reddit_pipeline.discover_report(_options())
            reddit_queue.archive_post("a")
            report = reddit_pipeline.discover_report(_options())

        self.assertEqual(report["added"], 0)
        self.assertEqual(reddit_queue.backlog(), [])
        self.assertEqual(
            [post["post_id"] for post in reddit_queue.archived_posts()], ["a"]
        )


class TestBacklog(PipelineTestCase):
    def _discover(self, *post_ids, **overrides):
        posts = [_post(post_id) for post_id in post_ids]
        with patch.object(reddit_pipeline, "fetch_posts", return_value=posts):
            return reddit_pipeline.discover_report(_options(**overrides))

    def test_discover_takes_the_best_of_the_whole_backlog(self):
        """
        A leftover from an earlier run is still made later.

        Taking only this run's results would strand everything past max_posts
        for ever: those stories are on file, so they are never fetched again.
        """
        self._discover("a", "b", "c")
        with patch.object(reddit_pipeline, "fetch_posts", return_value=[]):
            splits = reddit_pipeline.discover(_options(max_posts=2))

        self.assertEqual(len(splits), 2)
        self.assertTrue(all(split["parts"] for split in splits))

    def test_promoting_renders_the_stored_narration(self):
        """
        A backlog entry renders from what was stored, not from Reddit.

        By the time a story is promoted the thread may be edited or deleted,
        so going back for it would be a different video — or none.
        """
        self._discover("a")
        scripts = []
        result = reddit_pipeline.promote_posts(
            ["a"],
            _options(),
            lambda task_id, params, part, split: scripts.append(params.video_script),
        )

        self.assertEqual(result["posts"], 1)
        self.assertTrue(scripts)
        self.assertTrue(all(script.strip() for script in scripts))
        self.assertEqual(
            reddit_queue.get_post("a")["parts"][0]["status"],
            reddit_queue.STATUS_RENDERING,
        )
        self.assertEqual(reddit_queue.backlog(), [])

    def test_promoting_twice_does_not_render_twice(self):
        self._discover("a")
        submitted = []
        submit = lambda task_id, params, part, split: submitted.append(task_id)
        reddit_pipeline.promote_posts(["a"], _options(), submit)
        first = len(submitted)

        result = reddit_pipeline.promote_posts(["a"], _options(), submit)
        self.assertEqual(result["submitted"], 0)
        self.assertEqual(len(submitted), first)

    def test_promoting_keeps_the_moment_the_story_was_found(self):
        self._discover("a")
        found_at = reddit_queue.get_post("a")["created_at"]
        reddit_pipeline.promote_posts(
            ["a"], _options(), lambda task_id, params, part, split: None
        )
        self.assertEqual(reddit_queue.get_post("a")["created_at"], found_at)

    def test_archiving_leaves_a_rendering_story_alone(self):
        """Archive reaches into the backlog only; it is not a stop button."""
        self._discover("a")
        reddit_pipeline.promote_posts(
            ["a"], _options(), lambda task_id, params, part, split: None
        )
        self.assertEqual(reddit_queue.archive_post("a"), 0)
        self.assertEqual(
            reddit_queue.get_post("a")["parts"][0]["status"],
            reddit_queue.STATUS_RENDERING,
        )

    def test_archiving_can_be_undone(self):
        self._discover("a")
        reddit_queue.archive_post("a")
        self.assertEqual(reddit_queue.backlog(), [])

        reddit_queue.restore_post("a")
        self.assertEqual([post["post_id"] for post in reddit_queue.backlog()], ["a"])


class TestGameplayBackground(PipelineTestCase):
    def _clips(self, count=8):
        return [f"/library/clip{i}.mp4" for i in range(count)]

    def _split(self, post_id="abc123", parts=4):
        return {
            "post_id": post_id,
            "subreddit": "tifu",
            "title": "A story",
            "permalink": f"https://reddit.example/{post_id}",
            "score": 900,
            "truncated": False,
            "parts": [
                {
                    "index": i,
                    "total": parts,
                    "subject": f"A story (Part {i}/{parts})",
                    "script": "Once upon a time.",
                    "estimated_seconds": 40.0,
                }
                for i in range(1, parts + 1)
            ],
        }

    def test_every_part_of_one_story_plays_over_the_same_clip(self):
        """
        Parts go out as a set, so the background has to be continuous.

        Keying on the part subject gave part 1 and part 2 different footage,
        which reads as a different video rather than the next instalment.
        """
        split = self._split(parts=4)
        options = _options(background="gameplay", gameplay_clips=self._clips())

        chosen = {
            reddit_pipeline.build_video_params(
                part, options, reddit_pipeline.story_key(split)
            ).video_materials[0].url
            for part in split["parts"]
        }
        self.assertEqual(len(chosen), 1)

    def test_different_stories_still_spread_across_the_library(self):
        options = _options(background="gameplay", gameplay_clips=self._clips())

        chosen = {
            reddit_pipeline.build_video_params(
                split["parts"][0], options, reddit_pipeline.story_key(split)
            ).video_materials[0].url
            for split in (self._split(f"post{i}") for i in range(20))
        }
        # Not a distribution test — just that one clip is not doing all the work.
        self.assertGreater(len(chosen), 1)

    def test_submitting_a_story_gives_all_its_parts_one_clip(self):
        split = self._split(parts=3)
        seen = []
        reddit_pipeline.submit_parts(
            [split],
            _options(background="gameplay", gameplay_clips=self._clips()),
            lambda task_id, params, part, s: seen.append(
                params.video_materials[0].url
            ),
        )
        self.assertEqual(len(seen), 3)
        self.assertEqual(len(set(seen)), 1)

    def test_a_story_keeps_its_clip_when_it_is_rendered_again(self):
        split = self._split()
        options = _options(background="gameplay", gameplay_clips=self._clips())
        key = reddit_pipeline.story_key(split)

        first = reddit_pipeline.gameplay_clip_for(split["parts"][0], options, key)
        second = reddit_pipeline.gameplay_clip_for(split["parts"][2], options, key)
        self.assertEqual(first, second)

    def test_a_caller_with_no_story_still_gets_a_clip(self):
        options = _options(background="gameplay", gameplay_clips=self._clips())
        self.assertIn(
            reddit_pipeline.gameplay_clip_for({"subject": "Something"}, options),
            self._clips(),
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


class TestBackground(PipelineTestCase):
    def _part(self, index=1, subject="A story (Part 1/2)"):
        return {
            "index": index,
            "total": 2,
            "subject": subject,
            "script": "Once upon a time.",
            "estimated_seconds": 40.0,
        }

    def test_gameplay_renders_a_local_clip_rather_than_a_stock_search(self):
        params = reddit_pipeline.build_video_params(
            self._part(),
            _options(background="gameplay", gameplay_clips=["/clips/parkour.mp4"]),
        )
        self.assertEqual(params.video_source, "local")
        self.assertEqual(params.video_materials[0].url, "/clips/parkour.mp4")
        self.assertEqual(params.video_materials[0].provider, "local")
        # Search terms are dead weight when the footage is already on disk.
        self.assertEqual(params.video_terms, "")

    def test_a_stock_background_is_still_a_stock_search(self):
        params = reddit_pipeline.build_video_params(
            self._part(), _options(background="pixabay")
        )
        self.assertEqual(params.video_source, "pixabay")
        self.assertIsNone(params.video_materials)
        self.assertEqual(params.video_terms, "test terms")

    def test_the_library_is_snapshotted_once_per_batch(self):
        # A clip removed mid-batch must not renumber what the remaining parts
        # were already assigned.
        seen = []
        with patch.object(
            reddit_pipeline.gameplay_library, "paths", return_value=["/a.mp4"]
        ) as paths:
            reddit_pipeline.submit_parts(
                [
                    {
                        "post_id": "aaa", "subreddit": "x", "title": "t",
                        "permalink": "", "score": 0, "truncated": False,
                        "parts": [self._part(1), self._part(2, "A story (Part 2/2)")],
                    }
                ],
                _options(background="gameplay"),
                lambda task_id, params, part, split: seen.append(params),
            )

        paths.assert_called_once()
        self.assertEqual(
            [params.video_materials[0].url for params in seen], ["/a.mp4", "/a.mp4"]
        )

    def test_an_empty_library_is_reported_before_rendering(self):
        with patch.object(
            reddit_pipeline.gameplay_library, "is_ready", return_value=False
        ):
            issues = reddit_pipeline.background_issues(_options(background="gameplay"))
        self.assertEqual(len(issues), 1)
        self.assertIn("gameplay library is empty", issues[0].lower())

    def test_a_stock_background_needs_no_library(self):
        with patch.object(
            reddit_pipeline.gameplay_library, "is_ready", return_value=False
        ):
            self.assertEqual(
                reddit_pipeline.background_issues(_options(background="pexels")), []
            )


class TestSubtitleStyle(PipelineTestCase):
    def _part(self):
        return {"index": 1, "total": 1, "subject": "s", "script": "text"}

    def test_the_default_is_thick_yellow(self):
        with patch.dict(reddit_pipeline.config.app, {}, clear=False):
            options = reddit_pipeline.resolve_options()
        params = reddit_pipeline.build_video_params(self._part(), options)

        self.assertEqual(params.text_fore_color, "#FFFF00")
        self.assertEqual(params.stroke_color, "#000000")
        self.assertEqual(
            params.stroke_width, reddit_pipeline.TEXT_THICKNESS["thick"]
        )

    def test_each_thickness_level_maps_to_an_outline_width(self):
        widths = [
            reddit_pipeline.stroke_width_for({"text_thickness": level})
            for level in reddit_pipeline.TEXT_THICKNESS
        ]
        self.assertEqual(widths, sorted(widths))
        self.assertEqual(len(set(widths)), len(widths))

    def test_an_unknown_thickness_falls_back_to_the_default(self):
        self.assertEqual(
            reddit_pipeline.stroke_width_for({"text_thickness": "nonsense"}),
            reddit_pipeline.TEXT_THICKNESS[reddit_pipeline.DEFAULT_TEXT_THICKNESS],
        )

    def test_the_chosen_colour_and_thickness_reach_the_render(self):
        params = reddit_pipeline.build_video_params(
            self._part(),
            _options(text_color="#00FF00", text_thickness="thin"),
        )
        self.assertEqual(params.text_fore_color, "#00FF00")
        self.assertEqual(params.stroke_width, reddit_pipeline.TEXT_THICKNESS["thin"])


class TestPurgeDiscarded(PipelineTestCase):
    def _discarded(self, video_path=None):
        reddit_queue.record_post(
            {
                "post_id": "aaa", "subreddit": "x", "title": "t",
                "permalink": "", "score": 0, "truncated": False,
                "parts": [{"index": 1, "total": 1}],
            },
            [{"index": 1, "total": 1, "status": reddit_queue.STATUS_RENDERING,
              "task_id": "task-1", "video_path": video_path}],
        )
        reddit_queue.reject_post("aaa", discard_video=True)

    def _rendered_file(self):
        task_dir = os.path.join(self._temp_dir.name, "tasks", "task-1")
        os.makedirs(task_dir, exist_ok=True)
        path = os.path.join(task_dir, "final-1.mp4")
        with open(path, "wb") as handle:
            handle.write(b"video")
        return path

    def test_a_render_that_finished_after_rejection_is_deleted(self):
        self._discarded()
        video = self._rendered_file()
        task = {"state": const.TASK_STATE_COMPLETE, "videos": [video]}

        self.assertEqual(reddit_pipeline.purge_discarded(lambda _: task)["purged"], 1)
        self.assertFalse(os.path.exists(os.path.dirname(video)))
        # Nothing left to follow, so later passes skip it.
        self.assertEqual(reddit_queue.discarded_parts(), [])

    def test_a_render_still_in_flight_is_left_to_finish(self):
        self._discarded()
        task = {"state": const.TASK_STATE_PROCESSING}

        self.assertEqual(reddit_pipeline.purge_discarded(lambda _: task)["purged"], 0)
        self.assertEqual(len(reddit_queue.discarded_parts()), 1)

    def test_a_forgotten_task_stops_being_followed(self):
        self._discarded()
        self.assertEqual(reddit_pipeline.purge_discarded(lambda _: None)["purged"], 0)
        self.assertEqual(reddit_queue.discarded_parts(), [])

    def test_parts_rejected_without_discarding_are_never_purged(self):
        reddit_queue.record_post(
            {
                "post_id": "bbb", "subreddit": "x", "title": "t",
                "permalink": "", "score": 0, "truncated": False,
                "parts": [{"index": 1, "total": 1}],
            },
            [{"index": 1, "total": 1, "status": reddit_queue.STATUS_RENDERED,
              "task_id": "task-1"}],
        )
        reddit_queue.reject_post("bbb")

        video = self._rendered_file()
        task = {"state": const.TASK_STATE_COMPLETE, "videos": [video]}
        self.assertEqual(reddit_pipeline.purge_discarded(lambda _: task)["purged"], 0)
        self.assertTrue(os.path.exists(video))


if __name__ == "__main__":
    unittest.main()
