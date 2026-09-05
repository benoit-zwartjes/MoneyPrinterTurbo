import json
import os
import tempfile
import unittest
from unittest.mock import patch

from app.services import reddit_queue


class RedditQueueTestCase(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        patcher = patch.object(
            reddit_queue.utils, "storage_dir", return_value=self._temp_dir.name
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temp_dir.cleanup)

    def _queue_file(self):
        return os.path.join(self._temp_dir.name, reddit_queue.QUEUE_FILE_NAME)

    def _split(self, post_id="abc123", parts=2):
        return {
            "post_id": post_id,
            "subreddit": "AmItheAsshole",
            "title": "A story",
            "permalink": f"https://www.reddit.com/r/AmItheAsshole/comments/{post_id}/",
            "score": 900,
            "truncated": False,
            "parts": [
                {
                    "index": i,
                    "total": parts,
                    "subject": f"A story (Part {i}/{parts})",
                    "script": "Once upon a time.",
                    "estimated_seconds": 42.0,
                }
                for i in range(1, parts + 1)
            ],
        }

    def _rendered(self, parts=2, status=reddit_queue.STATUS_RENDERED):
        return [
            {
                "index": i,
                "total": parts,
                "status": status,
                "task_id": f"task-{i}",
                "video_path": f"/tmp/video-{i}.mp4",
            }
            for i in range(1, parts + 1)
        ]


class TestLoadAndSave(RedditQueueTestCase):
    def test_missing_file_reads_as_empty_queue(self):
        self.assertEqual(reddit_queue.load_queue(), {"version": 1, "posts": {}})
        self.assertEqual(reddit_queue.seen_ids(), set())

    def test_corrupt_file_reads_as_empty_rather_than_raising(self):
        with open(self._queue_file(), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertEqual(reddit_queue.load_queue()["posts"], {})

    def test_record_post_round_trips(self):
        reddit_queue.record_post(self._split(), self._rendered())

        stored = reddit_queue.get_post("abc123")
        self.assertEqual(stored["subreddit"], "AmItheAsshole")
        self.assertEqual(len(stored["parts"]), 2)
        self.assertEqual(stored["parts"][0]["task_id"], "task-1")
        self.assertEqual(stored["parts"][0]["subject"], "A story (Part 1/2)")
        self.assertEqual(reddit_queue.seen_ids(), {"abc123"})

    def test_record_post_writes_valid_json(self):
        reddit_queue.record_post(self._split(), self._rendered())
        with open(self._queue_file(), "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload["version"], 1)
        self.assertIn("abc123", payload["posts"])

    def test_post_without_an_id_is_rejected(self):
        split = self._split()
        split["post_id"] = ""
        self.assertFalse(reddit_queue.record_post(split, self._rendered()))


class TestReviewFlow(RedditQueueTestCase):
    def test_rendered_parts_await_review(self):
        reddit_queue.record_post(self._split(), self._rendered())
        pending = reddit_queue.pending_review()
        self.assertEqual(len(pending), 2)
        self.assertEqual(pending[0]["post_id"], "abc123")
        self.assertEqual(reddit_queue.approved_parts(), [])

    def test_approve_moves_every_rendered_part(self):
        reddit_queue.record_post(self._split(), self._rendered())
        self.assertEqual(reddit_queue.approve_post("abc123"), 2)
        self.assertEqual(reddit_queue.pending_review(), [])
        self.assertEqual(len(reddit_queue.approved_parts()), 2)

    def test_approve_does_not_touch_a_failed_part(self):
        parts = self._rendered()
        parts[1]["status"] = reddit_queue.STATUS_FAILED
        reddit_queue.record_post(self._split(), parts)

        self.assertEqual(reddit_queue.approve_post("abc123"), 1)
        statuses = {p["index"]: p["status"] for p in reddit_queue.get_post("abc123")["parts"]}
        self.assertEqual(statuses[1], reddit_queue.STATUS_APPROVED)
        self.assertEqual(statuses[2], reddit_queue.STATUS_FAILED)

    def test_approve_is_not_reapplied_to_scheduled_parts(self):
        reddit_queue.record_post(self._split(), self._rendered())
        reddit_queue.approve_post("abc123")
        reddit_queue.update_part(
            "abc123", 1, status=reddit_queue.STATUS_SCHEDULED, job_id="job-1"
        )
        # Nothing is in 'rendered' any more, so a second approve is a no-op
        # rather than dragging a scheduled part back into the queue.
        self.assertEqual(reddit_queue.approve_post("abc123"), 0)
        statuses = {p["index"]: p["status"] for p in reddit_queue.get_post("abc123")["parts"]}
        self.assertEqual(statuses[1], reddit_queue.STATUS_SCHEDULED)

    def test_approve_all_covers_every_post(self):
        reddit_queue.record_post(self._split("aaa"), self._rendered())
        reddit_queue.record_post(self._split("bbb"), self._rendered())
        self.assertEqual(reddit_queue.approve_all(), 4)

    def test_reject_takes_parts_out_of_review(self):
        reddit_queue.record_post(self._split(), self._rendered())
        self.assertEqual(reddit_queue.reject_post("abc123"), 2)
        self.assertEqual(reddit_queue.pending_review(), [])
        self.assertEqual(reddit_queue.approved_parts(), [])

    def test_unknown_post_is_a_no_op(self):
        self.assertEqual(reddit_queue.approve_post("nope"), 0)
        self.assertFalse(reddit_queue.update_part("nope", 1, status="approved"))


class TestUpdatePart(RedditQueueTestCase):
    def test_updates_scheduling_fields(self):
        reddit_queue.record_post(self._split(), self._rendered())
        self.assertTrue(
            reddit_queue.update_part(
                "abc123",
                2,
                status=reddit_queue.STATUS_SCHEDULED,
                job_id="job-9",
                scheduled_for="2026-09-10T18:00:00Z",
            )
        )
        part = reddit_queue.get_post("abc123")["parts"][1]
        self.assertEqual(part["job_id"], "job-9")
        self.assertEqual(part["scheduled_for"], "2026-09-10T18:00:00Z")

    def test_ignores_unknown_fields_and_bad_statuses(self):
        reddit_queue.record_post(self._split(), self._rendered())
        reddit_queue.update_part("abc123", 1, status="nonsense", nonsense_key="x")
        part = reddit_queue.get_post("abc123")["parts"][0]
        self.assertEqual(part["status"], reddit_queue.STATUS_RENDERED)
        self.assertNotIn("nonsense_key", part)


class TestSummary(RedditQueueTestCase):
    def test_counts_parts_by_status(self):
        reddit_queue.record_post(self._split("aaa"), self._rendered())
        reddit_queue.record_post(self._split("bbb"), self._rendered())
        reddit_queue.approve_post("aaa")

        summary = reddit_queue.summary()
        self.assertEqual(summary["posts"], 2)
        self.assertEqual(summary["parts"][reddit_queue.STATUS_APPROVED], 2)
        self.assertEqual(summary["parts"][reddit_queue.STATUS_RENDERED], 2)


class TestLibraryView(RedditQueueTestCase):
    def _set_created(self, post_id, created_at):
        """Pin the timestamps: two records written in the same millisecond
        would otherwise leave the order to chance."""
        queue = reddit_queue.load_queue()
        queue["posts"][post_id]["created_at"] = created_at
        reddit_queue._save_queue(queue)

    def test_all_posts_lists_newest_first(self):
        reddit_queue.record_post(self._split("aaa"), self._rendered())
        reddit_queue.record_post(self._split("bbb"), self._rendered())
        self._set_created("aaa", 1_000.0)
        self._set_created("bbb", 2_000.0)

        ids = [post["post_id"] for post in reddit_queue.all_posts()]
        self.assertEqual(ids, ["bbb", "aaa"])
        self.assertEqual(
            [post["post_id"] for post in reddit_queue.all_posts(newest_first=False)],
            ["aaa", "bbb"],
        )

    def test_stage_is_the_least_advanced_part(self):
        # Half a story published is still a story waiting on the other half.
        reddit_queue.record_post(self._split("aaa"), self._rendered())
        reddit_queue.update_part("aaa", 1, status=reddit_queue.STATUS_UPLOADED)

        post = reddit_queue.get_post("aaa")
        self.assertEqual(reddit_queue.post_stage(post), reddit_queue.STATUS_RENDERED)

    def test_a_failed_part_surfaces_over_progress(self):
        reddit_queue.record_post(self._split("aaa"), self._rendered())
        reddit_queue.update_part("aaa", 1, status=reddit_queue.STATUS_UPLOADED)
        reddit_queue.update_part("aaa", 2, status=reddit_queue.STATUS_FAILED)

        post = reddit_queue.get_post("aaa")
        self.assertEqual(reddit_queue.post_stage(post), reddit_queue.STATUS_FAILED)

    def test_a_rejected_part_does_not_hold_back_the_stage(self):
        reddit_queue.record_post(self._split("aaa"), self._rendered())
        reddit_queue.update_part("aaa", 1, status=reddit_queue.STATUS_UPLOADED)
        reddit_queue.update_part("aaa", 2, status=reddit_queue.STATUS_REJECTED)

        post = reddit_queue.get_post("aaa")
        self.assertEqual(reddit_queue.post_stage(post), reddit_queue.STATUS_UPLOADED)

    def test_counts_are_per_story(self):
        reddit_queue.record_post(self._split("aaa"), self._rendered())
        reddit_queue.update_part("aaa", 1, status=reddit_queue.STATUS_SCHEDULED)

        counts = reddit_queue.post_counts(reddit_queue.get_post("aaa"))
        self.assertEqual(counts[reddit_queue.STATUS_SCHEDULED], 1)
        self.assertEqual(counts[reddit_queue.STATUS_RENDERED], 1)
        self.assertEqual(counts[reddit_queue.STATUS_UPLOADED], 0)


class TestRejecting(RedditQueueTestCase):
    def _video(self, task_id="task-1", name="final-1.mp4"):
        """A render where a real one would be: its own storage/tasks folder."""
        task_dir = os.path.join(self._temp_dir.name, "tasks", task_id)
        os.makedirs(task_dir, exist_ok=True)
        path = os.path.join(task_dir, name)
        with open(path, "wb") as handle:
            handle.write(b"video")
        # The combined video, narration and subtitles sit beside the final cut.
        with open(os.path.join(task_dir, "combined-1.mp4"), "wb") as handle:
            handle.write(b"combined")
        return path

    def test_rejecting_keeps_the_videos_by_default(self):
        video = self._video()
        reddit_queue.record_post(
            self._split("aaa", parts=1),
            [{"index": 1, "total": 1, "status": reddit_queue.STATUS_RENDERED,
              "task_id": "task-1", "video_path": video}],
        )

        self.assertEqual(reddit_queue.reject_post("aaa"), 1)
        part = reddit_queue.get_post("aaa")["parts"][0]
        self.assertEqual(part["status"], reddit_queue.STATUS_REJECTED)
        self.assertEqual(part["video_path"], video)
        self.assertTrue(os.path.exists(video))
        self.assertFalse(part["discarded"])

    def test_discarding_deletes_the_video_and_keeps_the_story(self):
        video = self._video()
        reddit_queue.record_post(
            self._split("aaa", parts=1),
            [{"index": 1, "total": 1, "status": reddit_queue.STATUS_RENDERED,
              "task_id": "task-1", "video_path": video}],
        )

        self.assertEqual(reddit_queue.reject_post("aaa", discard_video=True), 1)
        post = reddit_queue.get_post("aaa")
        part = post["parts"][0]
        self.assertEqual(part["status"], reddit_queue.STATUS_REJECTED)
        self.assertIsNone(part["video_path"])
        self.assertTrue(part["discarded"])
        # The whole render folder goes, not just the final cut.
        self.assertFalse(os.path.exists(os.path.dirname(video)))
        # The story itself survives — that is the whole point.
        self.assertEqual(post["title"], "A story")
        self.assertEqual(post["permalink"], self._split("aaa")["permalink"])
        self.assertEqual(part["script"], "Once upon a time.")

    def test_discarding_stops_a_part_that_is_still_rendering(self):
        reddit_queue.record_post(
            self._split("aaa", parts=2),
            [
                {"index": 1, "total": 2, "status": reddit_queue.STATUS_RENDERED,
                 "video_path": None},
                {"index": 2, "total": 2, "status": reddit_queue.STATUS_RENDERING,
                 "task_id": "task-2"},
            ],
        )

        self.assertEqual(reddit_queue.reject_post("aaa", discard_video=True), 2)
        statuses = [p["status"] for p in reddit_queue.get_post("aaa")["parts"]]
        self.assertEqual(statuses, [reddit_queue.STATUS_REJECTED] * 2)
        # The render is still running, so the part stays findable for cleanup.
        self.assertEqual(reddit_queue.discarded_parts()[0]["task_id"], "task-2")

    def test_something_already_published_is_left_alone(self):
        video = self._video()
        reddit_queue.record_post(
            self._split("aaa", parts=2),
            [
                {"index": 1, "total": 2, "status": reddit_queue.STATUS_UPLOADED,
                 "task_id": "task-1", "video_path": video},
                {"index": 2, "total": 2, "status": reddit_queue.STATUS_RENDERED},
            ],
        )

        self.assertEqual(reddit_queue.reject_post("aaa", discard_video=True), 1)
        parts = reddit_queue.get_post("aaa")["parts"]
        self.assertEqual(parts[0]["status"], reddit_queue.STATUS_UPLOADED)
        self.assertTrue(os.path.exists(video))

    def test_files_outside_the_task_directory_are_never_deleted(self):
        outside_dir = os.path.join(self._temp_dir.name, "not-a-task")
        os.makedirs(outside_dir, exist_ok=True)
        outside = os.path.join(outside_dir, "video.mp4")
        with open(outside, "wb") as handle:
            handle.write(b"video")

        self.assertFalse(
            reddit_queue.delete_render_files({"video_path": outside})
        )
        self.assertFalse(
            reddit_queue.delete_render_files({"task_id": "../../not-a-task"})
        )
        self.assertTrue(os.path.exists(outside))

    def test_a_render_still_in_flight_keeps_its_folder(self):
        """ffmpeg is writing in there; the worker clears it once it settles."""
        video = self._video(task_id="task-2")
        reddit_queue.record_post(
            self._split("aaa", parts=1),
            [{"index": 1, "total": 1, "status": reddit_queue.STATUS_RENDERING,
              "task_id": "task-2"}],
        )

        reddit_queue.reject_post("aaa", discard_video=True)
        self.assertTrue(os.path.exists(os.path.dirname(video)))
        self.assertEqual(len(reddit_queue.discarded_parts()), 1)

    def test_the_script_is_stored_with_the_part(self):
        reddit_queue.record_post(self._split("aaa", parts=1), self._rendered(parts=1))
        self.assertEqual(
            reddit_queue.get_post("aaa")["parts"][0]["script"], "Once upon a time."
        )

    def test_a_very_long_script_is_capped(self):
        split = self._split("aaa", parts=1)
        split["parts"][0]["script"] = "x" * (reddit_queue.MAX_STORED_SCRIPT_CHARS + 500)
        reddit_queue.record_post(split, self._rendered(parts=1))
        self.assertEqual(
            len(reddit_queue.get_post("aaa")["parts"][0]["script"]),
            reddit_queue.MAX_STORED_SCRIPT_CHARS,
        )


class TestBacklog(RedditQueueTestCase):
    def test_a_discovered_story_is_seen_and_waiting(self):
        self.assertTrue(reddit_queue.record_discovered(self._split("aaa")))

        self.assertEqual(reddit_queue.seen_ids(), {"aaa"})
        self.assertEqual([p["post_id"] for p in reddit_queue.backlog()], ["aaa"])
        self.assertEqual(
            reddit_queue.post_stage(reddit_queue.get_post("aaa")),
            reddit_queue.STATUS_DISCOVERED,
        )

    def test_the_narration_is_kept_so_it_can_render_later(self):
        reddit_queue.record_discovered(self._split("aaa", parts=2))
        parts = reddit_queue.get_post("aaa")["parts"]
        self.assertEqual([part["script"] for part in parts], ["Once upon a time."] * 2)
        self.assertEqual(parts[0]["subject"], "A story (Part 1/2)")

    def test_a_story_already_on_file_is_left_alone(self):
        """
        A later search must not drag a made story back into the backlog.

        The filters exclude seen IDs, so this is the belt to that braces — and
        it is the difference between "already published" and "waiting to be
        made" when two searches overlap.
        """
        reddit_queue.record_post(self._split("aaa"), self._rendered())
        self.assertFalse(reddit_queue.record_discovered(self._split("aaa")))
        self.assertEqual(
            reddit_queue.get_post("aaa")["parts"][0]["status"],
            reddit_queue.STATUS_RENDERED,
        )
        self.assertEqual(reddit_queue.backlog(), [])

    def test_a_story_with_nothing_narratable_is_not_a_backlog_entry(self):
        split = self._split("aaa")
        split["parts"] = []
        self.assertFalse(reddit_queue.record_discovered(split))
        self.assertEqual(reddit_queue.seen_ids(), set())

    def test_archiving_moves_a_story_out_of_the_backlog_but_not_off_the_file(self):
        reddit_queue.record_discovered(self._split("aaa", parts=2))
        self.assertEqual(reddit_queue.archive_post("aaa"), 2)

        self.assertEqual(reddit_queue.backlog(), [])
        self.assertEqual([p["post_id"] for p in reddit_queue.archived_posts()], ["aaa"])
        # Still seen, so it is never fetched or offered again.
        self.assertEqual(reddit_queue.seen_ids(), {"aaa"})

    def test_restoring_puts_a_story_back(self):
        reddit_queue.record_discovered(self._split("aaa"))
        reddit_queue.archive_post("aaa")
        self.assertEqual(reddit_queue.restore_post("aaa"), 2)
        self.assertEqual([p["post_id"] for p in reddit_queue.backlog()], ["aaa"])

    def test_archiving_cannot_reach_a_story_that_is_already_being_made(self):
        reddit_queue.record_post(self._split("aaa"), self._rendered())
        self.assertEqual(reddit_queue.archive_post("aaa"), 0)
        self.assertEqual(
            reddit_queue.get_post("aaa")["parts"][0]["status"],
            reddit_queue.STATUS_RENDERED,
        )

    def test_promoting_keeps_the_moment_the_story_was_found(self):
        reddit_queue.record_discovered(self._split("aaa"))
        found_at = reddit_queue.get_post("aaa")["created_at"]

        reddit_queue.record_post(self._split("aaa"), self._rendered())
        self.assertEqual(reddit_queue.get_post("aaa")["created_at"], found_at)


if __name__ == "__main__":
    unittest.main()
