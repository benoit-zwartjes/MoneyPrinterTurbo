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


if __name__ == "__main__":
    unittest.main()
