"""
Fill the gameplay library from YouTube.

The gameplay background is the default for Reddit recaps, so a fresh server
renders nothing until somebody has put parkour footage in
``storage/local_videos/gameplay``. Uploading twenty clips through a browser is
the slow way to do that; this searches YouTube and downloads them.

Two things keep the download honest about what it is for:

* **A segment, not the whole video.** Parkour uploads run to two hours and 4K.
  ``combine_videos`` slices whatever it is given into ``max_clip_duration``
  chunks and walks them, so a few minutes is already more footage than a
  one-minute narration can show — and twenty two-hour 4K files would be tens of
  gigabytes that moviepy then has to open.
* **Nothing is downloaded twice.** The YouTube ID goes in the filename and is
  checked against the library first, so re-running this tops the library up
  instead of duplicating it.

Licensing is the caller's to get right. "No copyright" in a video's title is a
claim by its uploader, not a licence, and this module takes it at face value —
it searches for what it is asked to search for.

yt-dlp is a normal dependency but a fast-moving one; if the import fails, say
so as a sentence the page can print rather than raising through the UI.
"""

from __future__ import annotations

import os
import re
import shutil
import threading
import time

from loguru import logger

from app.services import gameplay_library
from app.utils import utils


DEFAULT_QUERY = "Minecraft Parkour Gameplay No Copyright"
DEFAULT_COUNT = 20

# Seconds of footage taken from each video. ``combine_videos`` walks the source
# in five-second chunks, so two minutes is two dozen distinct chunks — already
# more than a one-minute part can show — while keeping twenty clips to a couple
# of gigabytes and quick for moviepy to open.
DEFAULT_SEGMENT_SECONDS = 120
# Skipped past the intro, where these uploads put a title card, a face cam or a
# subscribe animation — none of which belong behind a narration.
DEFAULT_SEGMENT_START = 60

# 1080p rather than 720p because the render crops to fill a 9:16 frame: a 16:9
# source loses its sides, so a 1280x720 clip is only 405px wide by the time it
# is upscaled to 1080x1920, and it shows. Above 1080p the extra pixels are
# thrown away by the same crop and only cost disk and decode time.
DEFAULT_MAX_HEIGHT = 1080

# A video shorter than this has nothing to take a segment from once the intro
# is skipped. Anything longer than the cap is fine — only a slice is taken.
MIN_SOURCE_SECONDS = 180

# YouTube IDs are 11 characters of [A-Za-z0-9_-]. Pinned down because the ID
# becomes part of a filename and is how a re-run recognises what it already has.
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")

_FILENAME_PREFIX = "yt-"

_fetch_lock = threading.Lock()


def is_available() -> bool:
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        return False
    return True


def unavailable_reason() -> str:
    return (
        "yt-dlp is not installed, so gameplay clips cannot be downloaded. "
        "Run `uv sync` on the server, or add clips by hand."
    )


def stored_name(video_id: str) -> str:
    return f"{_FILENAME_PREFIX}{video_id}.mp4"


def existing_video_ids() -> set[str]:
    """
    Which YouTube videos the library already holds.

    Read off the filenames rather than the index, so a clip copied in over a
    volume counts too — the same reason ``clips()`` lists the directory.
    """
    found = set()
    for clip in gameplay_library.clips():
        name, _ = os.path.splitext(clip["name"])
        if name.startswith(_FILENAME_PREFIX):
            candidate = name[len(_FILENAME_PREFIX):]
            if _VIDEO_ID.match(candidate):
                found.add(candidate)
    return found


def search(query: str, limit: int) -> list[dict]:
    """
    Candidate videos for one query, newest search ranking first.

    Metadata only — no download — so an unusable result costs one listing entry
    rather than a few hundred megabytes.
    """
    import yt_dlp

    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        # Flat extraction returns the listing without resolving formats for
        # every hit, which is the difference between one request and fifty.
        "extract_flat": "in_playlist",
        "playlistend": max(int(limit), 1),
    }

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(
                f"ytsearch{max(int(limit), 1)}:{query}", download=False
            )
    except Exception as exc:
        logger.warning(f"youtube search failed: {type(exc).__name__}: {exc}")
        return []

    results = []
    for entry in (info or {}).get("entries") or []:
        if not isinstance(entry, dict):
            continue
        video_id = str(entry.get("id") or "")
        if not _VIDEO_ID.match(video_id):
            continue
        try:
            duration = float(entry.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0.0
        results.append(
            {
                "id": video_id,
                "title": str(entry.get("title") or video_id),
                "duration": duration,
            }
        )
    return results


def ffmpeg_dir() -> str:
    """
    A directory holding an executable literally called ``ffmpeg``.

    yt-dlp finds ffmpeg by name, and the binary this project falls back to is
    the one imageio-ffmpeg ships — named ``ffmpeg-macos-aarch64-v7.1`` and the
    like, which yt-dlp does not recognise, so cutting a segment fails with
    "ffmpeg is not installed" on a server that can render video perfectly well.
    A link under storage bridges the two without touching PATH.

    Returns "" when there is nothing to link, leaving yt-dlp to search PATH.
    """
    binary = utils.get_ffmpeg_binary()
    if not binary or binary == "ffmpeg":
        return ""

    name = os.path.basename(binary)
    if name in ("ffmpeg", "ffmpeg.exe"):
        return os.path.dirname(binary)

    link_dir = utils.storage_dir("ffmpeg-bin", create=True)
    link = os.path.join(link_dir, "ffmpeg.exe" if os.name == "nt" else "ffmpeg")

    if os.path.exists(link) and os.path.realpath(link) == os.path.realpath(binary):
        return link_dir

    try:
        if os.path.lexists(link):
            os.remove(link)
        # Windows needs a privilege for symlinks that a service account will
        # not have, so copy there and link everywhere else.
        if os.name == "nt":
            shutil.copy2(binary, link)
        else:
            os.symlink(binary, link)
    except OSError as exc:
        logger.warning(f"could not expose ffmpeg to yt-dlp: {exc}")
        return ""

    return link_dir


def _expose_ffmpeg_on_path() -> None:
    """
    Put the ffmpeg directory on PATH for this process.

    ``ffmpeg_location`` is not enough on its own. The guard that decides whether
    a partial download is possible calls ``FFmpegFD.available()`` with no path
    at all, so it searches PATH and reports "ffmpeg is not installed" on a
    server whose ffmpeg is the bundled one — the yt-dlp source marks this as a
    known bug. Prepending the directory satisfies the guard and leaves the
    actual cut to the binary handed over in the options.
    """
    directory = ffmpeg_dir()
    if not directory:
        return
    current = os.environ.get("PATH", "")
    if directory in current.split(os.pathsep):
        return
    os.environ["PATH"] = os.pathsep.join([directory, current]) if current else directory


def _download_options(destination: str, start: float, end: float, max_height: int) -> dict:
    """
    yt-dlp settings for one segment.

    ``download_ranges`` cuts with ffmpeg, so it is handed the same binary the
    renderer uses — the server may well have no ffmpeg on PATH and be running
    on the one bundled with imageio-ffmpeg.
    """
    import yt_dlp

    return {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "outtmpl": destination,
        # H.264 first, at or below the height cap. YouTube serves 1080p parkour
        # as AV1 by default, which moviepy decodes several times slower than
        # H.264 and which some ffmpeg builds cannot decode at all — a render
        # that fails on the server is a worse trade than a slightly larger file.
        # Audio is replaced by the narration but is kept anyway: a file with an
        # audio track is what every downstream reader expects.
        "format": (
            f"bestvideo[height<={max_height}][vcodec^=avc1]+bestaudio[ext=m4a]/"
            f"best[height<={max_height}][vcodec^=avc1]/"
            f"bestvideo[height<={max_height}][ext=mp4]+bestaudio[ext=m4a]/"
            f"best[height<={max_height}][ext=mp4]/"
            f"best[height<={max_height}]"
        ),
        "merge_output_format": "mp4",
        "download_ranges": yt_dlp.utils.download_range_func([], [[start, end]]),
        # Left off deliberately. Forcing keyframes re-encodes the segment, which
        # for 1080p60 parkour footage costs minutes of CPU and a third more disk
        # per clip; a stream copy starts at the nearest keyframe instead, and a
        # background nobody is cutting to the frame does not care where that is.
        "force_keyframes_at_cuts": False,
        "ffmpeg_location": ffmpeg_dir(),
        "noplaylist": True,
        "retries": 3,
        "socket_timeout": 30,
    }


def download_clip(
    video: dict,
    segment_seconds: int = DEFAULT_SEGMENT_SECONDS,
    segment_start: int = DEFAULT_SEGMENT_START,
    max_height: int = DEFAULT_MAX_HEIGHT,
) -> str | None:
    """
    Download one segment into the library. Returns the stored name, or None.

    Written to a temporary name first and moved into place only once yt-dlp has
    finished, so an interrupted download cannot leave a half-file that
    ``clips()`` would list and a render would then fail to open.
    """
    import yt_dlp

    video_id = video["id"]
    directory = gameplay_library.library_dir(create=True)
    final_path = os.path.join(directory, stored_name(video_id))
    temp_path = os.path.join(directory, f".{_FILENAME_PREFIX}{video_id}.part.mp4")

    duration = float(video.get("duration") or 0)
    start = float(segment_start)
    if duration and start + segment_seconds > duration:
        # Short enough that the intro skip would run off the end: take the
        # middle instead of failing.
        start = max(0.0, (duration - segment_seconds) / 2)
    end = start + float(segment_seconds)

    _expose_ffmpeg_on_path()
    options = _download_options(temp_path, start, end, max_height)

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
    except Exception as exc:
        logger.warning(
            f"failed to download {video_id}: {type(exc).__name__}: {exc}"
        )
        _remove_quietly(temp_path)
        return None

    if not os.path.exists(temp_path) or os.path.getsize(temp_path) == 0:
        logger.warning(f"{video_id} produced no file")
        _remove_quietly(temp_path)
        return None

    try:
        os.replace(temp_path, final_path)
    except OSError as exc:
        logger.warning(f"failed to store {video_id}: {exc}")
        _remove_quietly(temp_path)
        return None

    gameplay_library.record_downloaded(
        os.path.basename(final_path), video["title"], video_id
    )
    logger.info(
        f"gameplay clip downloaded: {os.path.basename(final_path)} "
        f"({os.path.getsize(final_path) // (1024 * 1024)} MB) — {video['title'][:60]}"
    )
    return os.path.basename(final_path)


def _remove_quietly(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def fill_library(
    query: str = DEFAULT_QUERY,
    count: int = DEFAULT_COUNT,
    segment_seconds: int = DEFAULT_SEGMENT_SECONDS,
    segment_start: int = DEFAULT_SEGMENT_START,
    max_height: int = DEFAULT_MAX_HEIGHT,
    on_progress=None,
) -> dict:
    """
    Search, then download until ``count`` new clips are in the library.

    ``on_progress(done, wanted, title)`` reports before each download. The
    search asks for more hits than are wanted, because some will already be in
    the library and some will be too short to take a segment from.

    Serialised: two of these running at once would fight over the same
    filenames and download the same videos twice.
    """
    if not is_available():
        return {"downloaded": 0, "skipped": 0, "failed": 0, "error": unavailable_reason()}

    wanted = max(int(count), 0)
    outcome = {"downloaded": 0, "skipped": 0, "failed": 0, "error": None}
    if not wanted:
        return outcome

    with _fetch_lock:
        # Three hits per wanted clip: enough slack for the already-held and the
        # too-short without making the listing request enormous.
        candidates = search(query, wanted * 3)
        if not candidates:
            outcome["error"] = (
                "YouTube returned no results. Check the server's network, or "
                "try a different search."
            )
            return outcome

        held = existing_video_ids()
        for video in candidates:
            if outcome["downloaded"] >= wanted:
                break

            if video["id"] in held:
                outcome["skipped"] += 1
                continue
            if video["duration"] and video["duration"] < MIN_SOURCE_SECONDS:
                outcome["skipped"] += 1
                continue

            if on_progress:
                on_progress(outcome["downloaded"], wanted, video["title"])

            if download_clip(video, segment_seconds, segment_start, max_height):
                outcome["downloaded"] += 1
                held.add(video["id"])
            else:
                outcome["failed"] += 1

        if outcome["downloaded"] < wanted and not outcome["failed"]:
            outcome["error"] = (
                f"Only {outcome['downloaded']} of {wanted} clips were new; the "
                "search ran out of results the library does not already have."
            )

    return outcome


def library_summary() -> dict:
    """Clip count and total size, for a page that has just filled the library."""
    stored = gameplay_library.clips()
    return {
        "count": len(stored),
        "bytes": sum(clip["size_bytes"] for clip in stored),
        "downloaded": len(existing_video_ids()),
        "checked_at": time.time(),
    }
