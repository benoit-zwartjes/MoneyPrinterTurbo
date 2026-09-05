"""
Filling the gameplay library from YouTube.

The network is never touched here: ``search`` and ``download_clip`` are the
seams, and everything above them is about which candidates get picked and what
is counted.
"""

import os
import tempfile
import unittest
from unittest.mock import patch

from app.services import gameplay_fetch, gameplay_library, material_upload


class GameplayFetchTestCase(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        # material_upload owns the library location; gameplay_fetch keeps the
        # ffmpeg link beside it.
        for module in (material_upload, gameplay_fetch):
            patcher = patch.object(
                module.utils, "storage_dir", return_value=self._temp_dir.name
            )
            patcher.start()
            self.addCleanup(patcher.stop)

    def _write_clip(self, name: str, size: int = 1024) -> str:
        path = os.path.join(gameplay_library.library_dir(create=True), name)
        with open(path, "wb") as handle:
            handle.write(b"\0" * size)
        return path

    def _video(self, video_id: str, duration: float = 1800.0) -> dict:
        return {"id": video_id, "title": f"Parkour {video_id}", "duration": duration}


class TestExistingVideoIds(GameplayFetchTestCase):
    def test_ids_are_read_off_the_filenames(self):
        """
        A clip copied in over a volume counts too, so the listing is the source
        of truth rather than the index.
        """
        self._write_clip(gameplay_fetch.stored_name("abcdefghijk"))
        self.assertEqual(gameplay_fetch.existing_video_ids(), {"abcdefghijk"})

    def test_hand_added_clips_are_ignored(self):
        self._write_clip("my-own-parkour.mp4")
        self.assertEqual(gameplay_fetch.existing_video_ids(), set())

    def test_a_filename_that_only_looks_like_an_id_is_ignored(self):
        # Eleven characters is the rule; this is the guard against a name that
        # would otherwise be fed back as a YouTube ID.
        self._write_clip("yt-not-an-id.mp4")
        self.assertEqual(gameplay_fetch.existing_video_ids(), set())


class TestSearch(GameplayFetchTestCase):
    def _with_entries(self, entries):
        class _FakeYDL:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *_):
                return False

            def extract_info(self_inner, *_args, **_kwargs):
                return {"entries": entries}

        return patch("yt_dlp.YoutubeDL", return_value=_FakeYDL())

    def test_results_are_normalized(self):
        with self._with_entries(
            [{"id": "abcdefghijk", "title": "Parkour", "duration": 900}]
        ):
            results = gameplay_fetch.search("parkour", 5)

        self.assertEqual(
            results, [{"id": "abcdefghijk", "title": "Parkour", "duration": 900.0}]
        )

    def test_entries_without_a_usable_id_are_dropped(self):
        with self._with_entries(
            [{"id": "short", "title": "x"}, None, {"title": "no id"}]
        ):
            self.assertEqual(gameplay_fetch.search("parkour", 5), [])

    def test_a_failed_search_is_empty_rather_than_raising(self):
        """A listing outage must not take the page down with it."""
        with patch("yt_dlp.YoutubeDL", side_effect=RuntimeError("boom")):
            self.assertEqual(gameplay_fetch.search("parkour", 5), [])


class TestFillLibrary(GameplayFetchTestCase):
    def test_downloads_until_the_count_is_reached(self):
        found = [self._video(f"video{i:06d}") for i in range(10)]
        downloaded = []

        with (
            patch.object(gameplay_fetch, "search", return_value=found),
            patch.object(
                gameplay_fetch,
                "download_clip",
                side_effect=lambda video, *a, **k: downloaded.append(video["id"]) or "x",
            ),
        ):
            outcome = gameplay_fetch.fill_library(count=3)

        self.assertEqual(outcome["downloaded"], 3)
        self.assertEqual(len(downloaded), 3)

    def test_a_clip_already_in_the_library_is_skipped(self):
        """Re-running tops the library up; it does not download it again."""
        self._write_clip(gameplay_fetch.stored_name("video000000"))
        found = [self._video("video000000"), self._video("video000001")]
        downloaded = []

        with (
            patch.object(gameplay_fetch, "search", return_value=found),
            patch.object(
                gameplay_fetch,
                "download_clip",
                side_effect=lambda video, *a, **k: downloaded.append(video["id"]) or "x",
            ),
        ):
            outcome = gameplay_fetch.fill_library(count=2)

        self.assertEqual(downloaded, ["video000001"])
        self.assertEqual(outcome["skipped"], 1)
        self.assertEqual(outcome["downloaded"], 1)

    def test_a_video_too_short_to_cut_is_skipped(self):
        found = [self._video("video000000", duration=30.0)]
        with (
            patch.object(gameplay_fetch, "search", return_value=found),
            patch.object(gameplay_fetch, "download_clip") as download,
        ):
            outcome = gameplay_fetch.fill_library(count=1)

        download.assert_not_called()
        self.assertEqual(outcome["skipped"], 1)

    def test_a_failed_download_is_counted_and_the_run_continues(self):
        found = [self._video(f"video{i:06d}") for i in range(3)]
        results = [None, "clip.mp4", "clip.mp4"]

        with (
            patch.object(gameplay_fetch, "search", return_value=found),
            patch.object(gameplay_fetch, "download_clip", side_effect=results),
        ):
            outcome = gameplay_fetch.fill_library(count=2)

        self.assertEqual(outcome["failed"], 1)
        self.assertEqual(outcome["downloaded"], 2)

    def test_an_empty_search_explains_itself(self):
        with patch.object(gameplay_fetch, "search", return_value=[]):
            outcome = gameplay_fetch.fill_library(count=5)

        self.assertEqual(outcome["downloaded"], 0)
        self.assertIn("no results", outcome["error"])

    def test_running_out_of_new_results_is_reported(self):
        found = [self._video("video000000")]
        with (
            patch.object(gameplay_fetch, "search", return_value=found),
            patch.object(gameplay_fetch, "download_clip", return_value="clip.mp4"),
        ):
            outcome = gameplay_fetch.fill_library(count=5)

        self.assertEqual(outcome["downloaded"], 1)
        self.assertIn("does not already have", outcome["error"])

    def test_a_missing_yt_dlp_is_a_sentence_rather_than_a_crash(self):
        with patch.object(gameplay_fetch, "is_available", return_value=False):
            outcome = gameplay_fetch.fill_library(count=5)

        self.assertEqual(outcome["downloaded"], 0)
        self.assertIn("yt-dlp", outcome["error"])


class TestFfmpegDir(GameplayFetchTestCase):
    def test_a_bundled_binary_is_exposed_under_its_real_name(self):
        """
        yt-dlp looks for a file called ``ffmpeg``.

        imageio-ffmpeg ships ``ffmpeg-macos-aarch64-v7.1``, which yt-dlp does
        not recognise — so a server that renders video perfectly well cannot
        cut a downloaded segment.
        """
        binary = os.path.join(self._temp_dir.name, "ffmpeg-linux-x86_64-v7.1")
        with open(binary, "wb") as handle:
            handle.write(b"#!/bin/sh\n")

        with patch.object(gameplay_fetch.utils, "get_ffmpeg_binary", return_value=binary):
            directory = gameplay_fetch.ffmpeg_dir()

        linked = os.path.join(directory, "ffmpeg")
        self.assertTrue(os.path.exists(linked))
        self.assertEqual(os.path.realpath(linked), os.path.realpath(binary))

    def test_a_binary_already_called_ffmpeg_is_used_where_it_is(self):
        binary = os.path.join(self._temp_dir.name, "ffmpeg")
        with open(binary, "wb") as handle:
            handle.write(b"#!/bin/sh\n")

        with patch.object(gameplay_fetch.utils, "get_ffmpeg_binary", return_value=binary):
            self.assertEqual(gameplay_fetch.ffmpeg_dir(), self._temp_dir.name)

    def test_nothing_to_link_leaves_yt_dlp_to_search_the_path(self):
        with patch.object(
            gameplay_fetch.utils, "get_ffmpeg_binary", return_value="ffmpeg"
        ):
            self.assertEqual(gameplay_fetch.ffmpeg_dir(), "")


class TestRecordDownloaded(GameplayFetchTestCase):
    def test_the_youtube_title_is_what_the_library_shows(self):
        name = gameplay_fetch.stored_name("abcdefghijk")
        self._write_clip(name)
        gameplay_library.record_downloaded(name, "Parkour, 1 hour", "abcdefghijk")

        clip = next(c for c in gameplay_library.clips() if c["name"] == name)
        self.assertEqual(clip["display_name"], "Parkour, 1 hour")


if __name__ == "__main__":
    unittest.main()
