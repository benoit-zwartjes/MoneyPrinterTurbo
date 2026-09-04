"""
The Reddit Recaps page must never do slow work inside the page script.

A fetch or an upload run there dies with the websocket and leaves nothing
behind, which is what made "Find stories" look like it never finished. These
tests pin the two properties that fix: long work is handed to
``reddit_jobs``, and everything the page shows is read back from server state
rather than from ``st.session_state``.
"""

import ast
import tempfile
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from app.config import config
from app.services import reddit_jobs, reddit_queue


ROOT_DIR = Path(__file__).parent.parent.parent
PAGE = ROOT_DIR / "webui" / "pages" / "1_Reddit_Recaps.py"
PAGE_SOURCE = PAGE.read_text(encoding="utf-8")


def _function(tree, name):
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def _calls(node) -> set[str]:
    names = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
            value = child.func.value
            if isinstance(value, ast.Name):
                names.add(f"{value.id}.{child.func.attr}")
    return names


class PageTestCase:
    """Runs the page against a throwaway storage directory."""

    def setup_method(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self._patchers = [
            patch.object(
                module.utils, "storage_dir", return_value=self._temp_dir.name
            )
            for module in (reddit_jobs, reddit_queue)
        ]
        # The worker is a process-wide thread; a test must not leave one running.
        self._patchers.append(patch.object(reddit_jobs, "ensure_worker"))
        # The page's own "back to Main" link needs the multipage context a real
        # server builds; AppTest runs one script on its own.
        self._patchers.append(patch("streamlit.page_link"))
        self._patchers.append(patch.object(config, "save_config"))
        self._patchers.append(patch.object(config, "update_config_nonblocking"))
        for patcher in self._patchers:
            patcher.start()
        reddit_jobs._job_threads.clear()

    def teardown_method(self):
        for patcher in self._patchers:
            patcher.stop()
        self._temp_dir.cleanup()

    def run_page(self, **session_state):
        app = AppTest.from_file(str(PAGE), default_timeout=30)
        for key, value in session_state.items():
            app.session_state[key] = value
        app.run()
        return app


def test_find_hands_the_fetch_to_a_background_job():
    """The page must start a job, never fetch inside the script run."""
    tree = ast.parse(PAGE_SOURCE)
    find = _function(tree, "_render_find")
    calls = _calls(find)

    assert "reddit_jobs.start_discovery" in calls
    assert "reddit_pipeline.discover" not in calls
    assert "reddit_pipeline.discover_report" not in calls


def test_scheduling_hands_the_uploads_to_a_background_job():
    tree = ast.parse(PAGE_SOURCE)
    calls = _calls(_function(tree, "_render_schedule"))

    assert "reddit_jobs.start_scheduling" in calls
    assert "upload_post.cross_post_video" not in calls


def test_the_worker_is_started_on_every_page_load():
    """Renders and uploads have to advance with no browser attached."""
    calls = _calls(_function(ast.parse(PAGE_SOURCE), "main"))
    assert "reddit_jobs.ensure_worker" in calls


def test_results_are_not_kept_in_session_state():
    """Anything held in session_state is lost on refresh; that was the bug."""
    uses_session_state = any(
        isinstance(node, ast.Attribute)
        and node.attr == "session_state"
        and isinstance(node.value, ast.Name)
        and node.value.id == "st"
        for node in ast.walk(ast.parse(PAGE_SOURCE))
    )
    assert not uses_session_state


class TestPageRuns(PageTestCase):
    def test_the_page_renders_with_an_empty_queue(self):
        app = self.run_page()
        assert not app.exception
        assert any("Reddit Recaps" in title.value for title in app.title)

    def test_the_find_button_starts_a_job(self):
        app = self.run_page()
        button = next(b for b in app.button if b.label == "Find stories")

        with patch.object(reddit_jobs, "start_discovery") as start:
            button.click().run()

        start.assert_called_once()
        assert not app.exception

    def test_candidates_from_a_finished_job_survive_a_fresh_page_load(self):
        reddit_jobs._write_job(
            reddit_jobs.JOB_DISCOVER,
            job_id="done",
            status=reddit_jobs.STATUS_COMPLETED,
            finished_at=1_000.0,
            result={
                "candidates": [
                    {
                        "post_id": "abc123",
                        "subreddit": "AmItheAsshole",
                        "title": "A findable story",
                        "permalink": "https://reddit.example/abc123",
                        "score": 900,
                        "parts": [
                            {
                                "index": 1,
                                "total": 1,
                                "estimated_seconds": 50.0,
                                "subject": "A findable story (Part 1/1)",
                                "script": "Once upon a time.",
                            }
                        ],
                    }
                ],
                "fetched": 20,
                "matched": 1,
                "skipped_empty": 0,
                "skipped_truncated": 0,
            },
        )

        app = self.run_page()
        assert not app.exception
        rendered = " ".join(item.value for item in app.markdown) + " ".join(
            str(item.label) for item in app.checkbox
        )
        assert "A findable story" in rendered

    def test_a_failed_job_is_reported_rather_than_hidden(self):
        reddit_jobs._write_job(
            reddit_jobs.JOB_DISCOVER,
            job_id="broken",
            status=reddit_jobs.STATUS_FAILED,
            finished_at=1_000.0,
            error="RuntimeError: apify token rejected",
        )

        app = self.run_page()
        assert not app.exception
        assert any("apify token rejected" in error.value for error in app.error)

    def test_the_library_lists_every_recorded_story(self):
        reddit_queue.record_post(
            {
                "post_id": "abc123",
                "subreddit": "tifu",
                "title": "An already made story",
                "permalink": "https://reddit.example/abc123",
                "score": 4200,
                "truncated": False,
                "parts": [{"index": 1, "total": 1}],
            },
            [
                {
                    "index": 1,
                    "total": 1,
                    "status": reddit_queue.STATUS_UPLOADED,
                    "video_path": "/tmp/part1.mp4",
                    "job_id": "job-1",
                }
            ],
        )

        app = self.run_page()
        assert not app.exception
        expanders = " ".join(str(item.label) for item in app.expander)
        assert "An already made story" in expanders
