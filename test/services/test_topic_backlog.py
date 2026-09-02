import json
import os
import tempfile
import unittest
from unittest.mock import patch

from app.services import topic_backlog


class TestTopicBacklog(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        patcher = patch.object(
            topic_backlog.utils, "storage_dir", return_value=self._temp_dir.name
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temp_dir.cleanup)

    def _backlog_file(self):
        return os.path.join(self._temp_dir.name, topic_backlog.BACKLOG_FILE_NAME)

    def test_missing_file_reads_as_empty_backlog(self):
        self.assertEqual(topic_backlog.load_backlog(), [])
        self.assertEqual(topic_backlog.pending_subjects(), [])

    def test_added_subjects_persist_across_reads(self):
        added = topic_backlog.add_subjects(["Why volcanoes erupt", "How yeast works"])

        self.assertEqual(added, 2)
        self.assertEqual(
            topic_backlog.pending_subjects(),
            ["Why volcanoes erupt", "How yeast works"],
        )

    def test_duplicate_subjects_are_not_added_twice(self):
        """
        重复运行热榜生成很常见。按大小写不敏感去重，避免同一条选题被反复
        排进待办，也避免已经拍过的选题重新出现。
        """
        topic_backlog.add_subjects(["Why volcanoes erupt"])
        added = topic_backlog.add_subjects(
            ["why VOLCANOES erupt", "How yeast works"]
        )

        self.assertEqual(added, 1)
        self.assertEqual(len(topic_backlog.load_backlog()), 2)

    def test_made_subjects_are_not_reoffered_by_a_later_generation(self):
        """已拍过的选题不能因为下一轮热榜生成又变回待办。"""
        topic_backlog.add_subjects(["Why volcanoes erupt"])
        topic_backlog.mark_made("Why volcanoes erupt", task_id="task-1")

        added = topic_backlog.add_subjects(["Why volcanoes erupt"])

        self.assertEqual(added, 0)
        self.assertEqual(topic_backlog.pending_subjects(), [])

    def test_mark_made_records_the_task_id(self):
        topic_backlog.add_subjects(["Why volcanoes erupt"])

        self.assertTrue(topic_backlog.mark_made("Why volcanoes erupt", "task-1"))

        entry = topic_backlog.load_backlog()[0]
        self.assertEqual(entry["status"], topic_backlog.STATUS_MADE)
        self.assertEqual(entry["task_id"], "task-1")
        self.assertIsNotNone(entry["made_at"])

    def test_mark_made_ignores_unknown_subjects(self):
        self.assertFalse(topic_backlog.mark_made("never added"))

    def test_mark_pending_restores_a_made_subject(self):
        topic_backlog.add_subjects(["Why volcanoes erupt"])
        topic_backlog.mark_made("Why volcanoes erupt", "task-1")

        self.assertTrue(topic_backlog.mark_pending("Why volcanoes erupt"))

        entry = topic_backlog.load_backlog()[0]
        self.assertEqual(entry["status"], topic_backlog.STATUS_PENDING)
        self.assertIsNone(entry["task_id"])
        self.assertIsNone(entry["made_at"])

    def test_remove_and_clear_made(self):
        topic_backlog.add_subjects(["a", "b", "c"])
        topic_backlog.mark_made("b")

        self.assertTrue(topic_backlog.remove_subject("a"))
        self.assertFalse(topic_backlog.remove_subject("a"))
        self.assertEqual(topic_backlog.clear_made(), 1)
        self.assertEqual(topic_backlog.pending_subjects(), ["c"])

    def test_corrupt_file_is_treated_as_empty_instead_of_crashing(self):
        """选题清单是辅助数据，损坏时不能让整个 WebUI 打不开。"""
        with open(self._backlog_file(), "w", encoding="utf-8") as handle:
            handle.write("{not json")

        self.assertEqual(topic_backlog.load_backlog(), [])

    def test_unusable_entries_are_dropped_on_read(self):
        with open(self._backlog_file(), "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "version": 1,
                    "entries": [
                        {"subject": "keep me", "status": "pending"},
                        {"subject": "   "},
                        "not an object",
                        {"no_subject": True},
                    ],
                },
                handle,
            )

        self.assertEqual(
            [entry["subject"] for entry in topic_backlog.load_backlog()], ["keep me"]
        )

    def test_unknown_status_falls_back_to_pending(self):
        with open(self._backlog_file(), "w", encoding="utf-8") as handle:
            json.dump(
                {"entries": [{"subject": "a", "status": "bogus"}]}, handle
            )

        self.assertEqual(
            topic_backlog.load_backlog()[0]["status"], topic_backlog.STATUS_PENDING
        )

    def test_subject_whitespace_is_normalized(self):
        topic_backlog.add_subjects(["  Why   volcanoes\nerupt  "])

        self.assertEqual(topic_backlog.pending_subjects(), ["Why volcanoes erupt"])

    def test_backlog_is_capped(self):
        limit = topic_backlog.MAX_BACKLOG_ENTRIES
        added = topic_backlog.add_subjects(f"subject {i}" for i in range(limit + 25))

        self.assertEqual(added, limit)
        self.assertEqual(len(topic_backlog.load_backlog()), limit)

    def test_blank_subjects_are_ignored(self):
        self.assertEqual(topic_backlog.add_subjects(["", "   ", None]), 0)


if __name__ == "__main__":
    unittest.main()
