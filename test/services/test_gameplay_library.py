import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import gameplay_library, material_upload


class GameplayLibraryTestCase(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        patcher = patch.object(
            material_upload.utils, "storage_dir", return_value=self._temp_dir.name
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _drop(self, name: str, content: bytes = b"clip") -> Path:
        """A clip that arrived over a mounted volume rather than the page."""
        path = Path(gameplay_library.library_dir()) / name
        path.write_bytes(content)
        return path


class TestLocation(GameplayLibraryTestCase):
    def test_the_library_lives_inside_the_local_material_root(self):
        # video.preprocess_video refuses to read a material from anywhere else,
        # so a library outside it would pass every check here and be dropped at
        # render time.
        root = os.path.realpath(material_upload.uploaded_material_dir())
        library = os.path.realpath(gameplay_library.library_dir())
        self.assertEqual(os.path.commonpath([root, library]), root)
        self.assertNotEqual(root, library)


class TestListing(GameplayLibraryTestCase):
    def test_an_empty_library_is_not_ready(self):
        self.assertEqual(gameplay_library.clips(), [])
        self.assertFalse(gameplay_library.is_ready())

    def test_files_dropped_into_the_folder_are_listed(self):
        # Parkour clips are usually too big for a browser upload, so the folder
        # is a first-class way in, not a fallback.
        self._drop("parkour.mp4")
        clips = gameplay_library.clips()
        self.assertEqual([clip["display_name"] for clip in clips], ["parkour.mp4"])
        self.assertTrue(gameplay_library.is_ready())

    def test_unsupported_files_are_ignored(self):
        self._drop("notes.txt")
        self._drop("thumbnail.png")
        self.assertEqual(gameplay_library.clips(), [])

    def test_the_index_file_is_not_listed_as_a_clip(self):
        gameplay_library._save_index({})
        self.assertEqual(gameplay_library.clips(), [])

    def test_listing_order_is_stable(self):
        for name in ("c.mp4", "a.mp4", "b.mp4"):
            self._drop(name)
        self.assertEqual(
            [clip["name"] for clip in gameplay_library.clips()],
            ["a.mp4", "b.mp4", "c.mp4"],
        )


class TestUploads(GameplayLibraryTestCase):
    def test_an_upload_keeps_its_original_name_for_display(self):
        with patch.object(material_upload, "_validate_video"):
            gameplay_library.add_clip("Minecraft Parkour 4K.mp4", io.BytesIO(b"data"))

        clip = gameplay_library.clips()[0]
        self.assertEqual(clip["display_name"], "Minecraft Parkour 4K.mp4")
        # …while the file itself is stored under the generated name.
        self.assertNotEqual(clip["name"], "Minecraft Parkour 4K.mp4")
        self.assertTrue(os.path.isfile(clip["path"]))

    def test_an_upload_lands_in_the_library_not_the_shared_material_folder(self):
        with patch.object(material_upload, "_validate_video"):
            gameplay_library.add_clip("parkour.mp4", io.BytesIO(b"data"))

        self.assertEqual(len(gameplay_library.clips()), 1)
        loose = [
            name
            for name in os.listdir(material_upload.uploaded_material_dir())
            if os.path.isfile(os.path.join(material_upload.uploaded_material_dir(), name))
        ]
        self.assertEqual(loose, [])

    def test_a_clip_with_no_index_entry_falls_back_to_its_filename(self):
        self._drop("parkour.mp4")
        with patch.object(material_upload, "_validate_video"):
            gameplay_library.add_clip("uploaded.mp4", io.BytesIO(b"data"))

        names = {clip["display_name"] for clip in gameplay_library.clips()}
        self.assertEqual(names, {"parkour.mp4", "uploaded.mp4"})

    def test_a_corrupt_index_does_not_hide_the_clips(self):
        self._drop("parkour.mp4")
        Path(gameplay_library.library_dir(), gameplay_library.INDEX_FILE_NAME).write_text(
            "{not json", encoding="utf-8"
        )
        self.assertEqual(len(gameplay_library.clips()), 1)


class TestRemoval(GameplayLibraryTestCase):
    def test_removing_a_clip_deletes_the_file_and_the_entry(self):
        with patch.object(material_upload, "_validate_video"):
            gameplay_library.add_clip("parkour.mp4", io.BytesIO(b"data"))
        clip = gameplay_library.clips()[0]

        self.assertTrue(gameplay_library.remove_clip(clip["name"]))
        self.assertEqual(gameplay_library.clips(), [])
        self.assertFalse(os.path.exists(clip["path"]))
        self.assertEqual(gameplay_library._load_index(), {})

    def test_a_path_outside_the_library_is_refused(self):
        outside = Path(self._temp_dir.name) / "secret.mp4"
        outside.write_bytes(b"data")

        self.assertFalse(gameplay_library.remove_clip("../secret.mp4"))
        self.assertFalse(gameplay_library.remove_clip(str(outside)))
        self.assertTrue(outside.exists())

    def test_removing_something_that_is_not_there_is_not_an_error(self):
        self.assertFalse(gameplay_library.remove_clip("nothing.mp4"))


class TestClipChoice(GameplayLibraryTestCase):
    def test_an_empty_library_yields_no_clip(self):
        self.assertEqual(gameplay_library.choose_clip([], "anything"), "")

    def test_the_same_part_always_gets_the_same_clip(self):
        # Re-rendering one part of a published set must not change its look.
        clips = ["/a.mp4", "/b.mp4", "/c.mp4"]
        first = gameplay_library.choose_clip(clips, "A story (Part 2/3)")
        self.assertEqual(first, gameplay_library.choose_clip(clips, "A story (Part 2/3)"))
        self.assertIn(first, clips)

    def test_different_parts_spread_across_the_library(self):
        clips = [f"/{letter}.mp4" for letter in "abcdefgh"]
        chosen = {
            gameplay_library.choose_clip(clips, f"A story (Part {i}/8)")
            for i in range(1, 9)
        }
        self.assertGreater(len(chosen), 1)


if __name__ == "__main__":
    unittest.main()
