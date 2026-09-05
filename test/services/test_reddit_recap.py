import importlib.util
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from app.services import reddit_queue


def _load_script():
    """Load scripts/reddit_recap.py by path; it is a CLI, not a package module."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(root, "scripts", "reddit_recap.py")
    spec = importlib.util.spec_from_file_location("reddit_recap_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


reddit_recap = _load_script()


def _reddit_post(post_id: str, sentences: int = 6) -> dict:
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


class RedditRecapTestCase(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        for module in (reddit_queue, reddit_recap):
            patcher = patch.object(
                module.utils, "storage_dir", side_effect=self._storage_dir
            )
            patcher.start()
            self.addCleanup(patcher.stop)

    def _storage_dir(self, sub_dir: str = "", create: bool = False) -> str:
        path = os.path.join(self._temp_dir.name, sub_dir) if sub_dir else self._temp_dir.name
        if create:
            os.makedirs(path, exist_ok=True)
        return path



def _render_options(**overrides) -> dict:
    """The option set as resolve_options builds it, without touching config."""
    options = {
        "background": "pexels",
        "video_terms": "test terms",
        "video_aspect": "9:16",
        "voice_name": "en-US-AriaNeural-Female",
        "subtitle_enabled": True,
        "text_color": reddit_recap.reddit_pipeline.DEFAULT_TEXT_COLOR,
        "text_thickness": reddit_recap.reddit_pipeline.DEFAULT_TEXT_THICKNESS,
        "stroke_color": reddit_recap.reddit_pipeline.DEFAULT_STROKE_COLOR,
        "font_name": reddit_recap.reddit_pipeline.DEFAULT_FONT_NAME,
        "font_size": reddit_recap.reddit_pipeline.DEFAULT_FONT_SIZE,
    }
    options.update(overrides)
    return options


class TestWriteManifest(RedditRecapTestCase):
    def test_writes_one_jsonl_line_per_part(self):
        posts = [_reddit_post("aaa", sentences=20), _reddit_post("bbb", sentences=20)]
        with patch.object(reddit_recap.reddit_pipeline, "fetch_posts", return_value=posts):
            splits = reddit_recap.reddit_pipeline.discover(
                {
                    "subreddits": None, "listing": "top", "time_filter": "day",
                    "fetch_limit": 50, "max_posts": 2, "min_score": 0,
                    "min_words": 0, "max_words": 0, "allow_nsfw": False,
                    "skip_truncated": False, "part_seconds": 20, "max_parts": 10,
                    "video_terms": "test terms",
                }
            )

        options = _render_options(video_terms="calm background")
        path = reddit_recap.write_manifest(splits, options)
        with open(path, "r", encoding="utf-8") as handle:
            lines = [json.loads(line) for line in handle if line.strip()]

        self.assertEqual(len(lines), sum(len(s["parts"]) for s in splits))
        self.assertEqual(lines[0]["video_terms"], "calm background")
        self.assertEqual(lines[0]["video_source"], "pexels")
        self.assertTrue(lines[0]["video_script"])

    def test_the_manifest_carries_the_background_and_the_subtitle_style(self):
        """A cron run must produce the same video a click would."""
        part = {
            "index": 1, "total": 1, "subject": "A story (Part 1/1)",
            "script": "Once upon a time.", "estimated_seconds": 40.0,
        }
        row = reddit_recap.manifest_row(
            part,
            _render_options(
                background="gameplay",
                gameplay_clips=["/clips/parkour.mp4"],
                text_color="#FFFF00",
                text_thickness="thick",
            ),
        )

        self.assertEqual(row["video_source"], "local")
        self.assertEqual(
            row["video_materials"], [{"provider": "local", "url": "/clips/parkour.mp4"}]
        )
        self.assertEqual(row["text_fore_color"], "#FFFF00")
        self.assertEqual(
            row["stroke_width"],
            reddit_recap.reddit_pipeline.TEXT_THICKNESS["thick"],
        )


class TestRecordResults(RedditRecapTestCase):
    def _split(self, post_id: str, parts: int) -> dict:
        return {
            "post_id": post_id,
            "subreddit": "AmItheAsshole",
            "title": f"Story {post_id}",
            "permalink": f"https://www.reddit.com/r/AmItheAsshole/comments/{post_id}/",
            "score": 900,
            "truncated": False,
            "total_words": 200,
            "parts": [
                {
                    "index": i,
                    "total": parts,
                    "script": "text",
                    "subject": f"Story {post_id} (Part {i}/{parts})",
                    "estimated_seconds": 40.0,
                }
                for i in range(1, parts + 1)
            ],
        }

    def test_maps_a_flat_summary_onto_the_right_posts_and_parts(self):
        # Two posts of two and one parts: the summary is flat and in manifest
        # order, so an off-by-one here would file part 1 of story two against
        # story one.
        splits = [self._split("aaa", 2), self._split("bbb", 1)]
        summary = {
            "tasks": [
                {"task_id": "t1", "status": "succeeded", "result": {"videos": ["/v1.mp4"]}},
                {"task_id": "t2", "status": "succeeded", "result": {"videos": ["/v2.mp4"]}},
                {"task_id": "t3", "status": "succeeded", "result": {"videos": ["/v3.mp4"]}},
            ]
        }
        reddit_recap.record_results(splits, summary)

        first = reddit_queue.get_post("aaa")
        second = reddit_queue.get_post("bbb")
        self.assertEqual([p["task_id"] for p in first["parts"]], ["t1", "t2"])
        self.assertEqual([p["video_path"] for p in first["parts"]], ["/v1.mp4", "/v2.mp4"])
        self.assertEqual([p["task_id"] for p in second["parts"]], ["t3"])
        self.assertEqual(second["parts"][0]["video_path"], "/v3.mp4")

    def test_failed_task_marks_the_part_failed(self):
        splits = [self._split("aaa", 2)]
        summary = {
            "tasks": [
                {"task_id": "t1", "status": "succeeded", "result": {"videos": ["/v1.mp4"]}},
                {"task_id": "t2", "status": "failed", "result": {}, "error": "boom"},
            ]
        }
        reddit_recap.record_results(splits, summary)

        parts = reddit_queue.get_post("aaa")["parts"]
        self.assertEqual(parts[0]["status"], reddit_queue.STATUS_RENDERED)
        self.assertEqual(parts[1]["status"], reddit_queue.STATUS_FAILED)
        self.assertEqual(parts[1]["error"], "boom")

    def test_success_without_a_video_path_counts_as_failed(self):
        # --stop-at short of the video stage exits 0 with no file; treating
        # that as rendered would queue a part with nothing to upload.
        splits = [self._split("aaa", 1)]
        summary = {"tasks": [{"task_id": "t1", "status": "succeeded", "result": {}}]}
        reddit_recap.record_results(splits, summary)
        self.assertEqual(
            reddit_queue.get_post("aaa")["parts"][0]["status"],
            reddit_queue.STATUS_FAILED,
        )

    def test_truncated_summary_does_not_raise(self):
        splits = [self._split("aaa", 3)]
        reddit_recap.record_results(splits, {"tasks": []})
        parts = reddit_queue.get_post("aaa")["parts"]
        self.assertEqual(len(parts), 3)
        self.assertTrue(all(p["status"] == reddit_queue.STATUS_FAILED for p in parts))


if __name__ == "__main__":
    unittest.main()
