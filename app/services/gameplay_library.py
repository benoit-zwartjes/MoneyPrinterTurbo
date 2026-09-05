"""
The gameplay background library.

Reddit recaps play over gameplay footage — Minecraft parkour by default —
rather than stock b-roll, so the clips have to live somewhere on the server and
be reusable by every render. They live in ``storage/local_videos/gameplay``.

That parent directory is not a detail: ``video.preprocess_video`` refuses to
read a local material from anywhere else, so a library outside it would pass
every check here and then be dropped at render time.

Two ways in, because a parkour clip is often too big to push through a browser:

* upload from the page, which reuses the validation and size limits in
  ``material_upload``;
* drop files into the directory over a mounted volume or SFTP, which the
  listing picks up whether or not this module ever saw them.

``library.json`` only remembers the original filename of an upload, since
uploads are stored under a generated name. A file with no entry is listed under
its own filename, so nothing depends on the index being complete or even
present.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import zlib
from typing import BinaryIO

from loguru import logger

from app.services import material_upload
from app.utils import file_security


LIBRARY_DIR_NAME = "gameplay"
INDEX_FILE_NAME = "library.json"

# Images make no sense as a gameplay background: a still frame behind a
# one-minute narration is not what anyone means by parkour footage.
SUPPORTED_EXTENSIONS = material_upload.SUPPORTED_VIDEO_EXTENSIONS

_index_lock = threading.RLock()


def library_dir(create: bool = True) -> str:
    return material_upload.resolve_upload_dir(LIBRARY_DIR_NAME, create=create)


def _index_path() -> str:
    return os.path.join(library_dir(create=True), INDEX_FILE_NAME)


def _load_index() -> dict:
    try:
        with open(_index_path(), "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        logger.warning(f"failed to read the gameplay library index: {exc}")
        return {}

    clips = payload.get("clips") if isinstance(payload, dict) else None
    if not isinstance(clips, dict):
        return {}
    return {
        str(name): entry for name, entry in clips.items() if isinstance(entry, dict)
    }


def _save_index(clips: dict) -> bool:
    path = _index_path()
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=os.path.dirname(path),
            prefix=".gameplay-library-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = handle.name
            json.dump({"version": 1, "clips": clips}, handle, ensure_ascii=False, indent=2)
        os.replace(temp_path, path)
        temp_path = None
        return True
    except OSError as exc:
        logger.warning(f"failed to write the gameplay library index: {exc}")
        return False
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def clips() -> list[dict]:
    """
    Every usable clip in the library, by filename.

    Sorted by name rather than by date so the order a render walks is stable
    between runs; a clip added today does not renumber the rest.
    """
    directory = library_dir(create=True)
    index = _load_index()

    found = []
    try:
        entries = os.scandir(directory)
    except OSError as exc:
        logger.warning(f"failed to list the gameplay library: {exc}")
        return []

    with entries:
        for entry in entries:
            if not entry.is_file():
                continue
            if os.path.splitext(entry.name)[1].lower() not in SUPPORTED_EXTENSIONS:
                continue
            try:
                stat = entry.stat()
            except OSError:
                continue
            recorded = index.get(entry.name) or {}
            found.append(
                {
                    "name": entry.name,
                    "display_name": str(recorded.get("original_name") or entry.name),
                    "path": os.path.join(directory, entry.name),
                    "size_bytes": stat.st_size,
                    "added_at": float(recorded.get("added_at") or stat.st_mtime),
                }
            )

    found.sort(key=lambda clip: clip["name"])
    return found


def paths() -> list[str]:
    return [clip["path"] for clip in clips()]


def is_ready() -> bool:
    return bool(paths())


def add_clip(filename: str, source: BinaryIO) -> dict:
    """
    Store one uploaded clip and remember what it was called.

    Validation, the size limit and the atomic write are ``material_upload``'s;
    this only records the original name, which that layer replaces with a
    generated one.
    """
    stored_name = material_upload.save_material_upload(
        filename, source, target_dir=library_dir(create=True)
    )

    with _index_lock:
        index = _load_index()
        index[stored_name] = {
            "original_name": material_upload.sanitize_material_filename(filename),
            "added_at": time.time(),
        }
        _save_index(index)

    logger.info(f"gameplay clip added: {stored_name}")
    return {"name": stored_name, "display_name": filename}


def remove_clip(name: str) -> bool:
    """Delete one clip. The name is resolved inside the library, never outside."""
    try:
        path = file_security.resolve_path_within_directory(library_dir(create=True), name)
    except ValueError as exc:
        logger.warning(f"refusing to remove a clip outside the library: {name} ({exc})")
        return False

    try:
        os.remove(path)
    except OSError as exc:
        logger.warning(f"failed to remove gameplay clip {name}: {exc}")
        return False

    with _index_lock:
        index = _load_index()
        if index.pop(os.path.basename(path), None) is not None:
            _save_index(index)

    logger.info(f"gameplay clip removed: {os.path.basename(path)}")
    return True


def choose_clip(available: list[str], key: str) -> str:
    """
    Pick which clip backs one part.

    Walking the library by a hash of the part's own subject spreads stories and
    their parts across the clips without a shared counter, and re-rendering the
    same part picks the same clip — a re-render should not silently change the
    look of one part in a published set.
    """
    if not available:
        return ""
    position = zlib.crc32(str(key).encode("utf-8", "replace")) % len(available)
    return available[position]
