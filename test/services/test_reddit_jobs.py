import os
import tempfile
import time
import unittest
from unittest.mock import patch

from app.models import const
from app.services import reddit_jobs, reddit_pipeline, reddit_queue


class _AliveWorker:
    """A worker handle that is always alive, without a thread behind it."""

    @staticmethod
    def is_alive() -> bool:
        return True


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    """Background jobs are threads; poll rather than sleeping a fixed amount."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class RedditJobsTestCase(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        for module in (reddit_jobs, reddit_queue):
            patcher = patch.object(
                module.utils, "storage_dir", return_value=self._temp_dir.name
            )
            patcher.start()
            self.addCleanup(patcher.stop)

        # Each test starts from an empty registry so a thread left over from a
        # previous test cannot be mistaken for this test's job.
        reddit_jobs._job_threads.clear()
        self.addCleanup(reddit_jobs._job_threads.clear)

    def _finished(self, kind):
        self.assertTrue(
            _wait_for(lambda: not reddit_jobs.is_running(kind)),
            f"job '{kind}' never finished",
        )
        return reddit_jobs.get_job(kind)


class TestJobLifecycle(RedditJobsTestCase):
    def test_discovery_result_survives_the_page(self):
        """The whole point: the result is on disk, not in a browser session."""
        report = {
            "candidates": [{"post_id": "abc", "parts": []}],
            "fetched": 12,
            "matched": 3,
            "skipped_empty": 1,
            "skipped_truncated": 0,
        }
        with patch.object(reddit_pipeline, "discover_report", return_value=report):
            reddit_jobs.start_discovery({"max_posts": 2})
            job = self._finished(reddit_jobs.JOB_DISCOVER)

        self.assertEqual(job["status"], reddit_jobs.STATUS_COMPLETED)
        self.assertEqual(job["result"]["fetched"], 12)
        self.assertEqual(job["result"]["matched"], 3)
        self.assertEqual(reddit_jobs.discovered_candidates(), report["candidates"])

        # A fresh read of the file, as a second browser tab would do.
        reloaded = reddit_jobs.load_jobs()["jobs"][reddit_jobs.JOB_DISCOVER]
        self.assertEqual(reloaded["result"]["candidates"], report["candidates"])

    def test_discovery_options_are_snapshotted_at_start(self):
        seen = {}

        def capture(options):
            seen.update(options)
            return {
                "candidates": [],
                "fetched": 0,
                "matched": 0,
                "skipped_empty": 0,
                "skipped_truncated": 0,
            }

        options = {"max_posts": 3}
        with patch.object(reddit_pipeline, "discover_report", side_effect=capture):
            reddit_jobs.start_discovery(options)
            options["max_posts"] = 99  # the user edits the form while it runs
            self._finished(reddit_jobs.JOB_DISCOVER)

        self.assertEqual(seen["max_posts"], 3)

    def test_a_crashing_job_still_reaches_a_terminal_state(self):
        with patch.object(
            reddit_pipeline, "discover_report", side_effect=RuntimeError("boom")
        ):
            reddit_jobs.start_discovery({})
            job = self._finished(reddit_jobs.JOB_DISCOVER)

        self.assertEqual(job["status"], reddit_jobs.STATUS_FAILED)
        self.assertIn("boom", job["error"])
        self.assertIsNotNone(job["finished_at"])

    def test_empty_discovery_reports_why(self):
        with patch.object(
            reddit_pipeline,
            "discover_report",
            return_value={
                "candidates": [],
                "fetched": 40,
                "matched": 0,
                "skipped_empty": 0,
                "skipped_truncated": 0,
            },
        ):
            reddit_jobs.start_discovery({})
            job = self._finished(reddit_jobs.JOB_DISCOVER)

        self.assertEqual(job["status"], reddit_jobs.STATUS_COMPLETED)
        self.assertEqual(job["result"]["fetched"], 40)
        self.assertEqual(job["message"], "No story matched the filters")

    def test_second_start_is_ignored_while_one_runs(self):
        release = __import__("threading").Event()
        calls = []

        def blocking(options):
            calls.append(options)
            release.wait(5)
            return {
                "candidates": [],
                "fetched": 0,
                "matched": 0,
                "skipped_empty": 0,
                "skipped_truncated": 0,
            }

        with patch.object(reddit_pipeline, "discover_report", side_effect=blocking):
            first = reddit_jobs.start_discovery({})
            second = reddit_jobs.start_discovery({})
            self.assertEqual(first["job_id"], second["job_id"])
            release.set()
            self._finished(reddit_jobs.JOB_DISCOVER)

        self.assertEqual(len(calls), 1)

    def test_a_job_left_running_by_a_dead_process_is_marked_failed(self):
        """A restart mid-fetch must not leave the page waiting forever."""
        reddit_jobs._write_job(
            reddit_jobs.JOB_DISCOVER,
            job_id="stale",
            status=reddit_jobs.STATUS_RUNNING,
            started_at=time.time(),
            pid=os.getpid() + 12345,
        )

        job = reddit_jobs.get_job(reddit_jobs.JOB_DISCOVER)
        self.assertEqual(job["status"], reddit_jobs.STATUS_FAILED)
        self.assertIn("server stopped", job["error"])
        self.assertFalse(reddit_jobs.is_running(reddit_jobs.JOB_DISCOVER))

    def test_clear_candidates_keeps_the_rest_of_the_record(self):
        with patch.object(
            reddit_pipeline,
            "discover_report",
            return_value={
                "candidates": [{"post_id": "abc"}],
                "fetched": 5,
                "matched": 1,
                "skipped_empty": 0,
                "skipped_truncated": 0,
            },
        ):
            reddit_jobs.start_discovery({})
            self._finished(reddit_jobs.JOB_DISCOVER)

        reddit_jobs.clear_candidates()
        job = reddit_jobs.get_job(reddit_jobs.JOB_DISCOVER)
        self.assertEqual(reddit_jobs.discovered_candidates(), [])
        self.assertEqual(job["result"]["fetched"], 5)
        # Marked consumed, so the page says "those went to render" rather than
        # re-reading the counts and reporting a search that found nothing.
        self.assertTrue(job["result"]["consumed"])

    def test_corrupt_store_reads_as_empty(self):
        with open(
            os.path.join(self._temp_dir.name, reddit_jobs.JOBS_FILE_NAME),
            "w",
            encoding="utf-8",
        ) as handle:
            handle.write("{not json")
        self.assertEqual(reddit_jobs.load_jobs()["jobs"], {})
        self.assertIsNone(reddit_jobs.get_job(reddit_jobs.JOB_DISCOVER))


class TestScheduling(RedditJobsTestCase):
    def test_scheduling_runs_in_the_background_and_reports(self):
        outcome = {"scheduled": 2, "failed": 0, "errors": []}
        with patch.object(
            reddit_pipeline, "schedule_parts", return_value=outcome
        ) as scheduler:
            reddit_jobs.start_scheduling(
                [{"post_id": "a", "index": 1, "total": 2, "title": "t"}],
                ["2026-01-01T10:00:00Z"],
                ["tiktok"],
            )
            job = self._finished(reddit_jobs.JOB_SCHEDULE)

        self.assertEqual(job["status"], reddit_jobs.STATUS_COMPLETED)
        self.assertEqual(job["result"]["scheduled"], 2)
        self.assertEqual(job["message"], "2 parts scheduled")
        self.assertEqual(scheduler.call_args[0][1], ["2026-01-01T10:00:00Z"])


class TestWorker(RedditJobsTestCase):
    def test_sync_once_promotes_renders_without_the_page(self):
        with (
            patch.object(
                reddit_pipeline, "sync_rendering", return_value={"rendered": 2, "failed": 1}
            ),
            patch.object(reddit_pipeline, "sync_uploads", return_value={"uploaded": 1, "failed": 0}),
            patch.object(reddit_jobs.upload_post.upload_post_service, "is_configured", return_value=True),
        ):
            result = reddit_jobs.sync_once(poll_uploads=True)

        self.assertEqual(result["rendered"], 2)
        self.assertEqual(result["render_failed"], 1)
        self.assertEqual(result["uploaded"], 1)

    def test_uploads_are_not_polled_when_upload_post_is_not_configured(self):
        with (
            patch.object(
                reddit_pipeline, "sync_rendering", return_value={"rendered": 0, "failed": 0}
            ),
            patch.object(reddit_pipeline, "sync_uploads") as uploads,
            patch.object(reddit_jobs.upload_post.upload_post_service, "is_configured", return_value=False),
        ):
            reddit_jobs.sync_once(poll_uploads=True)

        uploads.assert_not_called()

    def test_refresh_leaves_the_upload_poll_to_the_worker(self):
        """A Refresh click must not do a network call per scheduled part."""
        with (
            # A stand-in for a live worker: starting a real one would race the
            # assertion below against its own first pass.
            patch.object(reddit_jobs, "_worker", _AliveWorker()),
            patch.object(
                reddit_pipeline, "sync_rendering", return_value={"rendered": 1, "failed": 0}
            ),
            patch.object(reddit_pipeline, "sync_uploads") as uploads,
            patch.object(reddit_jobs.upload_post.upload_post_service, "is_configured", return_value=True),
        ):
            outcome = reddit_jobs.refresh_now()

        self.assertEqual(outcome["rendered"], 1)
        uploads.assert_not_called()
        # …but the next worker pass now polls instead of waiting out the interval.
        self.assertEqual(reddit_jobs._last_upload_sync, 0.0)

    def test_refresh_polls_inline_when_no_worker_is_running(self):
        reddit_jobs.stop_worker()
        with (
            patch.object(
                reddit_pipeline, "sync_rendering", return_value={"rendered": 0, "failed": 0}
            ),
            patch.object(reddit_pipeline, "sync_uploads", return_value={"uploaded": 1, "failed": 0}) as uploads,
            patch.object(reddit_jobs.upload_post.upload_post_service, "is_configured", return_value=True),
        ):
            outcome = reddit_jobs.refresh_now()

        uploads.assert_called_once()
        self.assertEqual(outcome["uploaded"], 1)

    def test_ensure_worker_starts_one_thread_per_process(self):
        self.addCleanup(reddit_jobs.stop_worker)
        with patch.object(reddit_jobs, "sync_once", return_value={}):
            reddit_jobs.ensure_worker()
            first = reddit_jobs._worker
            reddit_jobs.ensure_worker()
            self.assertIs(reddit_jobs._worker, first)
            self.assertTrue(first.is_alive())
            self.assertTrue(first.daemon)


class TestWholeWorkflow(RedditJobsTestCase):
    """
    One story from search to published, with only the outside world stubbed.

    Each step is driven the way the page drives it, so this fails if any of
    them stops handing its state to the next.
    """

    def _split(self):
        return {
            "post_id": "abc123",
            "subreddit": "tifu",
            "title": "A story",
            "permalink": "https://reddit.example/abc123",
            "score": 4200,
            "truncated": False,
            "total_words": 300,
            "parts": [
                {
                    "index": 1,
                    "total": 1,
                    "subject": "A story (Part 1/1)",
                    "script": "Once upon a time.",
                    "estimated_seconds": 50.0,
                }
            ],
        }

    def test_search_render_review_schedule_publish(self):
        options = {
            "video_terms": "calm", "video_source": "pexels",
            "video_aspect": "9:16", "voice_name": "v", "subtitle_enabled": True,
        }

        # 1. Find, in the background.
        with patch.object(
            reddit_pipeline,
            "discover_report",
            return_value={
                "candidates": [self._split()],
                "fetched": 10,
                "matched": 1,
                "skipped_empty": 0,
                "skipped_truncated": 0,
            },
        ):
            reddit_jobs.start_discovery(options)
            self._finished(reddit_jobs.JOB_DISCOVER)

        candidates = reddit_jobs.discovered_candidates()
        self.assertEqual(len(candidates), 1)

        # 2. Render: submitted to the task pool, recorded as rendering.
        submitted = []
        reddit_pipeline.submit_parts(
            candidates,
            options,
            lambda task_id, params, part, split: submitted.append(task_id),
        )
        reddit_jobs.clear_candidates()
        post = reddit_queue.get_post("abc123")
        self.assertEqual(post["parts"][0]["status"], reddit_queue.STATUS_RENDERING)
        self.assertEqual(reddit_queue.post_stage(post), reddit_queue.STATUS_RENDERING)

        # 3. The worker promotes the finished render with nobody watching.
        video = os.path.join(self._temp_dir.name, "part1.mp4")
        open(video, "wb").close()
        task = {"state": const.TASK_STATE_COMPLETE, "videos": [video]}
        with (
            patch.object(reddit_jobs.sm.state, "get_task", return_value=task),
            patch.object(reddit_jobs.upload_post.upload_post_service, "is_configured", return_value=False),
        ):
            reddit_jobs.sync_once(poll_uploads=False)
        self.assertEqual(
            reddit_queue.get_post("abc123")["parts"][0]["status"],
            reddit_queue.STATUS_RENDERED,
        )

        # 4. Review.
        reddit_queue.approve_post("abc123")
        approved = reddit_queue.approved_parts()
        self.assertEqual(len(approved), 1)

        # 5. Schedule, in the background.
        with patch.object(
            reddit_pipeline.upload_post,
            "cross_post_video",
            return_value={"success": True, "job_id": "job-1"},
        ):
            reddit_jobs.start_scheduling(
                approved, ["2026-01-01T10:00:00Z"], ["tiktok"]
            )
            job = self._finished(reddit_jobs.JOB_SCHEDULE)
        self.assertEqual(job["result"]["scheduled"], 1)

        part = reddit_queue.get_post("abc123")["parts"][0]
        self.assertEqual(part["status"], reddit_queue.STATUS_SCHEDULED)
        self.assertEqual(part["job_id"], "job-1")

        # 6. The worker notices Upload-Post published it.
        with (
            patch.object(reddit_jobs.sm.state, "get_task", return_value=None),
            patch.object(reddit_jobs.upload_post.upload_post_service, "is_configured", return_value=True),
            patch.object(
                reddit_jobs.upload_post.upload_post_service,
                "check_status",
                return_value={"status": "completed"},
            ),
        ):
            reddit_jobs.sync_once(poll_uploads=True)

        post = reddit_queue.get_post("abc123")
        self.assertEqual(post["parts"][0]["status"], reddit_queue.STATUS_UPLOADED)
        self.assertEqual(reddit_queue.post_stage(post), reddit_queue.STATUS_UPLOADED)
        # …and the story is in the library with its whole history.
        self.assertEqual(
            [p["post_id"] for p in reddit_queue.all_posts()], ["abc123"]
        )


if __name__ == "__main__":
    unittest.main()
