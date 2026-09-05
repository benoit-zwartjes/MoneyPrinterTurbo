"""
Persistent state for the Reddit recap pipeline.

Four jobs, one file at ``storage/reddit_recaps.json``:

* remember which post IDs have already been seen, so a listing is never paid
  for, split or offered twice — a story enters this file the moment a search
  turns it up, not when something renders it;
* hold the backlog of stories that passed the filters and split cleanly but
  have not been rendered yet, until they are promoted or archived;
* hold rendered parts in a review queue until they are approved;
* track the Upload-Post job for each part once it has been scheduled.

Kept separate from ``topic_backlog`` on purpose — that store is keyed on a
subject string and caps at 500 entries, neither of which fits a per-post record
carrying task IDs, file paths and upload jobs.

Writes go through a temp file and ``os.replace`` so a crash mid-write leaves
the previous file intact, matching how the topic backlog and config are saved.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time

from loguru import logger

from app.utils import utils


QUEUE_FILE_NAME = "reddit_recaps.json"
MAX_POSTS = 1000
# The narration is kept with the part so a story survives its video being
# discarded, and so a backlog entry can be rendered later without fetching the
# thread again — which makes the cap a correctness limit, not a display one:
# anything trimmed here is narration the promoted video would lose. The page
# allows at most 180 seconds and 260 words per minute per part, so no part the
# splitter can produce reaches 6000 characters. Capped all the same because the
# queue holds up to 1000 posts and is read back in full on every access.
MAX_STORED_SCRIPT_CHARS = 8000

# A part starts discovered — found, split, nothing rendered — and then moves
# rendering → rendered → approved → scheduled → uploaded, dropping out to
# failed or rejected at any point. The WebUI submits renders to the background
# pool and so passes through 'rendering'; the CLI runner blocks on cli.py and
# records parts as 'rendered' directly.
#
# 'archived' is the other way out of the backlog: a story nobody wants to make.
# It differs from 'rejected' in what it says about the footage — a rejected
# part had a video and lost it, an archived one was never rendered — and both
# keep the record, which is what stops the story being offered again.
STATUS_DISCOVERED = "discovered"
STATUS_ARCHIVED = "archived"
STATUS_RENDERING = "rendering"
STATUS_RENDERED = "rendered"
STATUS_APPROVED = "approved"
STATUS_SCHEDULED = "scheduled"
STATUS_UPLOADED = "uploaded"
STATUS_FAILED = "failed"
STATUS_REJECTED = "rejected"

PART_STATUSES = (
    STATUS_DISCOVERED,
    STATUS_ARCHIVED,
    STATUS_RENDERING,
    STATUS_RENDERED,
    STATUS_APPROVED,
    STATUS_SCHEDULED,
    STATUS_UPLOADED,
    STATUS_FAILED,
    STATUS_REJECTED,
)

# A read-modify-write sequence has to be serialised end to end, or two
# concurrent approvals overwrite each other.
_queue_lock = threading.RLock()


def _queue_path() -> str:
    return os.path.join(utils.storage_dir("", create=True), QUEUE_FILE_NAME)


def _normalize_part(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    try:
        index = int(raw.get("index"))
    except (TypeError, ValueError):
        return None
    if index < 1:
        return None

    status = raw.get("status")
    if status not in PART_STATUSES:
        status = STATUS_RENDERED

    def _int_or(value, fallback):
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback

    def _str_or_none(value):
        value = str(value).strip() if value is not None else ""
        return value or None

    return {
        "index": index,
        "total": _int_or(raw.get("total"), index),
        "status": status,
        "task_id": _str_or_none(raw.get("task_id")),
        "video_path": _str_or_none(raw.get("video_path")),
        "subject": str(raw.get("subject", "") or ""),
        "script": str(raw.get("script", "") or "")[:MAX_STORED_SCRIPT_CHARS],
        "estimated_seconds": raw.get("estimated_seconds"),
        "job_id": _str_or_none(raw.get("job_id")),
        "scheduled_for": _str_or_none(raw.get("scheduled_for")),
        "error": _str_or_none(raw.get("error")),
        "discarded": bool(raw.get("discarded")),
    }


def _normalize_post(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    post_id = str(raw.get("post_id", "") or "").strip()
    if not post_id:
        return None

    parts = [p for p in (_normalize_part(p) for p in raw.get("parts") or []) if p]
    parts.sort(key=lambda part: part["index"])

    def _float_or(value, fallback):
        try:
            return float(value)
        except (TypeError, ValueError):
            return fallback

    return {
        "post_id": post_id,
        "subreddit": str(raw.get("subreddit", "") or ""),
        "title": str(raw.get("title", "") or ""),
        "permalink": str(raw.get("permalink", "") or ""),
        "score": int(_float_or(raw.get("score"), 0)),
        "created_at": _float_or(raw.get("created_at"), time.time()),
        "truncated": bool(raw.get("truncated")),
        "parts": parts,
    }


def load_queue() -> dict:
    """Read the queue; a missing or corrupt file yields an empty one."""
    path = _queue_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return {"version": 1, "posts": {}}
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        logger.warning(f"failed to read the reddit queue, starting empty: {exc}")
        return {"version": 1, "posts": {}}

    raw_posts = payload.get("posts") if isinstance(payload, dict) else None
    if not isinstance(raw_posts, dict):
        return {"version": 1, "posts": {}}

    posts = {}
    for post_id, raw in raw_posts.items():
        normalized = _normalize_post(raw)
        if normalized:
            posts[normalized["post_id"]] = normalized
    return {"version": 1, "posts": posts}


def _save_queue(queue: dict) -> bool:
    path = _queue_path()
    temp_path = None
    posts = queue.get("posts", {})

    if len(posts) > MAX_POSTS:
        # Drop the oldest records first; their only remaining job is dedup and
        # a thread that fell off the listing months ago will not resurface.
        ordered = sorted(posts.values(), key=lambda p: p["created_at"], reverse=True)
        posts = {p["post_id"]: p for p in ordered[:MAX_POSTS]}

    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=os.path.dirname(path),
            prefix=".reddit_recaps-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = handle.name
            json.dump(
                {"version": 1, "posts": posts},
                handle,
                ensure_ascii=False,
                indent=2,
            )
        os.replace(temp_path, path)
        temp_path = None
        return True
    except OSError as exc:
        logger.warning(f"failed to write the reddit queue: {exc}")
        return False
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def seen_ids() -> set[str]:
    """Every post ID already recorded, whatever became of it."""
    return set(load_queue()["posts"].keys())


def record_post(split_result: dict, parts: list[dict]) -> bool:
    """
    Store one post and the outcome of rendering its parts.

    ``parts`` carries the render results — ``index``, ``task_id``,
    ``video_path``, ``status`` and ``error`` — merged onto the script metadata
    already in ``split_result``.
    """
    post_id = str(split_result.get("post_id", "") or "").strip()
    if not post_id:
        return False

    by_index = {}
    for part in parts:
        normalized = _normalize_part(part)
        if normalized:
            by_index[normalized["index"]] = normalized

    for script_part in split_result.get("parts") or []:
        index = script_part.get("index")
        if index in by_index:
            by_index[index]["subject"] = script_part.get("subject", "")
            by_index[index]["script"] = str(script_part.get("script", "") or "")[
                :MAX_STORED_SCRIPT_CHARS
            ]
            by_index[index]["estimated_seconds"] = script_part.get("estimated_seconds")
            by_index[index]["total"] = script_part.get("total", by_index[index]["total"])

    with _queue_lock:
        queue = load_queue()
        existing = queue["posts"].get(post_id)
        queue["posts"][post_id] = {
            "post_id": post_id,
            "subreddit": str(split_result.get("subreddit", "") or ""),
            "title": str(split_result.get("title", "") or ""),
            "permalink": str(split_result.get("permalink", "") or ""),
            "score": int(split_result.get("score", 0) or 0),
            # A story promoted out of the backlog is already on file, and the
            # moment it was found is the interesting one — it is what "Found"
            # reads in the library. Overwriting it would make a story that has
            # been waiting for days look like it turned up seconds ago.
            "created_at": existing["created_at"] if existing else time.time(),
            "truncated": bool(split_result.get("truncated")),
            "parts": sorted(by_index.values(), key=lambda p: p["index"]),
        }
        return _save_queue(queue)


def record_discovered(split_result: dict) -> bool:
    """
    Put a story the search turned up into the backlog, before anything renders.

    This is what makes a story fetched once stay fetched: ``seen_ids`` reads
    every post in this file, so a story waiting in the backlog — or archived
    out of it — is filtered out of the next listing instead of being split and
    offered a second time.

    A post ID already on file is left exactly as it is and False comes back: a
    story that has been rendered, published, rejected or archived must never be
    dragged back into the backlog by a later search that happens to see it
    again. True means a new backlog entry was written.
    """
    post_id = str(split_result.get("post_id", "") or "").strip()
    if not post_id:
        return False

    entries = []
    for part in split_result.get("parts") or []:
        normalized = _normalize_part({**part, "status": STATUS_DISCOVERED})
        if normalized:
            entries.append(normalized)
    # A story with nothing narratable is not a backlog entry; it is a story the
    # splitter rejected, and recording it would put an un-renderable row in
    # front of the user for ever.
    if not entries:
        return False

    with _queue_lock:
        queue = load_queue()
        if post_id in queue["posts"]:
            return False

        queue["posts"][post_id] = {
            "post_id": post_id,
            "subreddit": str(split_result.get("subreddit", "") or ""),
            "title": str(split_result.get("title", "") or ""),
            "permalink": str(split_result.get("permalink", "") or ""),
            "score": int(split_result.get("score", 0) or 0),
            "created_at": time.time(),
            "truncated": bool(split_result.get("truncated")),
            "parts": sorted(entries, key=lambda part: part["index"]),
        }
        return _save_queue(queue)


def update_part(post_id: str, index: int, **fields) -> bool:
    """Patch one part in place. Unknown keys are ignored."""
    allowed = {
        "status",
        "task_id",
        "video_path",
        "job_id",
        "scheduled_for",
        "error",
        "discarded",
    }
    with _queue_lock:
        queue = load_queue()
        post = queue["posts"].get(str(post_id))
        if not post:
            return False
        for part in post["parts"]:
            if part["index"] != int(index):
                continue
            for key, value in fields.items():
                if key not in allowed:
                    continue
                if key == "status" and value not in PART_STATUSES:
                    continue
                part[key] = value
            return _save_queue(queue)
    return False


def _iter_parts(status: str | None = None):
    queue = load_queue()
    for post in sorted(queue["posts"].values(), key=lambda p: p["created_at"]):
        for part in post["parts"]:
            if status is None or part["status"] == status:
                yield post, part


def parts_with_status(status: str) -> list[dict]:
    """Flat view of parts in one state, oldest post first, part order kept."""
    return [
        {
            "post_id": post["post_id"],
            "subreddit": post["subreddit"],
            "title": post["title"],
            "permalink": post["permalink"],
            **part,
        }
        for post, part in _iter_parts(status)
    ]


def pending_review() -> list[dict]:
    return parts_with_status(STATUS_RENDERED)


def approved_parts() -> list[dict]:
    return parts_with_status(STATUS_APPROVED)


def set_post_status(post_id: str, status: str, only_from=None) -> int:
    """
    Move every part of one post to ``status``; returns how many changed.

    ``only_from`` is one status or several, and guards against re-approving
    something already scheduled.
    """
    if status not in PART_STATUSES:
        return 0

    sources = None
    if only_from is not None:
        sources = {only_from} if isinstance(only_from, str) else set(only_from)

    with _queue_lock:
        queue = load_queue()
        post = queue["posts"].get(str(post_id))
        if not post:
            return 0

        changed = 0
        for part in post["parts"]:
            if sources is not None and part["status"] not in sources:
                continue
            if part["status"] == status:
                continue
            part["status"] = status
            changed += 1

        if changed:
            _save_queue(queue)
        return changed


def approve_post(post_id: str) -> int:
    return set_post_status(post_id, STATUS_APPROVED, only_from=STATUS_RENDERED)


def backlog(newest_first: bool = True) -> list[dict]:
    """
    Stories that passed the filters and split cleanly, waiting to be promoted.

    Read off the stage rather than off individual parts, so a story part-way
    into rendering never appears here as if it were still untouched.
    """
    return [
        post for post in all_posts(newest_first) if post_stage(post) == STATUS_DISCOVERED
    ]


def archived_posts(newest_first: bool = True) -> list[dict]:
    return [
        post for post in all_posts(newest_first) if post_stage(post) == STATUS_ARCHIVED
    ]


def archive_post(post_id: str) -> int:
    """
    Set a backlog story aside without rendering it.

    Only ``discovered`` parts move, so archiving cannot reach into a story that
    is already rendering, published or under review. The record stays, which is
    the point: an archived story is never fetched or offered again.
    """
    return set_post_status(post_id, STATUS_ARCHIVED, only_from=STATUS_DISCOVERED)


def restore_post(post_id: str) -> int:
    """Put an archived story back in the backlog. Archiving is a one-click
    action on a long list, so it needs a way back that is not editing JSON."""
    return set_post_status(post_id, STATUS_DISCOVERED, only_from=STATUS_ARCHIVED)


def backlog_ids() -> list[str]:
    return [post["post_id"] for post in backlog()]


def _task_folder_for(part: dict) -> str | None:
    """
    The storage/tasks folder one part rendered into, or None.

    Resolved from the task id when there is one and from the recorded video
    path otherwise, then checked to be strictly inside the tasks root: the
    queue is a JSON file on disk, and a hand-edited or corrupted path must
    never be able to delete something else. Same guard the task manager and
    the delete-video endpoint use.
    """
    tasks_root = os.path.realpath(utils.task_dir())

    task_id = str(part.get("task_id") or "").strip()
    video_path = str(part.get("video_path") or "").strip()
    if task_id:
        candidate = os.path.realpath(os.path.join(tasks_root, task_id))
    elif video_path:
        candidate = os.path.realpath(os.path.dirname(video_path))
    else:
        return None

    if not candidate.startswith(tasks_root + os.sep):
        logger.warning(f"refusing to delete outside the task directory: {candidate}")
        return None
    return candidate


def delete_render_files(part: dict) -> bool:
    """
    Remove everything one part's render wrote.

    The whole task folder goes, not just the final mp4: the combined video, the
    narration and the subtitles sit beside it and are the larger half of what a
    discarded render costs in disk.
    """
    folder = _task_folder_for(part)
    if not folder or not os.path.isdir(folder):
        return False

    try:
        shutil.rmtree(folder)
    except OSError as exc:
        logger.warning(f"failed to delete {folder}: {exc}")
        return False

    logger.info(f"discarded render files: {folder}")
    return True


def reject_post(post_id: str, discard_video: bool = False) -> int:
    """
    Reject one story.

    By default only parts waiting for review are rejected, and their files are
    left alone. With ``discard_video`` the whole story stops: parts still
    rendering are rejected too, so nothing promotes them into the queue later,
    and every video already written is deleted. The record itself — title,
    permalink and the narration of every part — stays, which is the point: the
    story is kept, the footage is not.
    """
    if not discard_video:
        return set_post_status(post_id, STATUS_REJECTED, only_from=STATUS_RENDERED)

    with _queue_lock:
        queue = load_queue()
        post = queue["posts"].get(str(post_id))
        if not post:
            return 0

        changed = 0
        for part in post["parts"]:
            if part["status"] in (STATUS_UPLOADED, STATUS_REJECTED):
                # Something already published cannot be un-published by deleting
                # the local copy, so leave it and its files alone.
                continue
            if part["status"] != STATUS_RENDERING:
                # A render still in flight owns its folder — ffmpeg is writing
                # into it. The worker deletes that one once the task settles.
                delete_render_files(part)
                part["video_path"] = None
            part["discarded"] = True
            part["status"] = STATUS_REJECTED
            changed += 1

        if changed:
            _save_queue(queue)
        return changed


def discarded_parts() -> list[dict]:
    """
    Rejected parts whose render may still be running.

    A part rejected mid-render is left by a worker pass, but the render itself
    keeps going and writes a file nobody asked for; this is what finds it.
    """
    return [
        part
        for part in parts_with_status(STATUS_REJECTED)
        if part.get("discarded") and part.get("task_id")
    ]


def approve_all() -> int:
    approved = 0
    for post_id in {part["post_id"] for part in pending_review()}:
        approved += approve_post(post_id)
    return approved


# What a story is waiting on, least advanced first. A failed part outranks
# everything so a broken story is never displayed as if it were progressing,
# and archived and rejected rank last so setting a story aside does not hold up
# the stage of the parts that did go out.
_STAGE_ORDER = (
    STATUS_FAILED,
    STATUS_DISCOVERED,
    STATUS_RENDERING,
    STATUS_RENDERED,
    STATUS_APPROVED,
    STATUS_SCHEDULED,
    STATUS_UPLOADED,
    STATUS_ARCHIVED,
    STATUS_REJECTED,
)


def all_posts(newest_first: bool = True) -> list[dict]:
    """Every story ever recorded, with its parts. The library view reads this."""
    posts = list(load_queue()["posts"].values())
    posts.sort(key=lambda post: post["created_at"], reverse=newest_first)
    return posts


def post_stage(post: dict) -> str:
    """One label for a whole story: the state its least advanced part is in."""
    parts = post.get("parts") or []
    if not parts:
        return STATUS_FAILED
    return min(
        (part["status"] for part in parts),
        key=lambda status: _STAGE_ORDER.index(status)
        if status in _STAGE_ORDER
        else len(_STAGE_ORDER),
    )


def post_counts(post: dict) -> dict:
    """Parts per status for one story."""
    counts = {status: 0 for status in PART_STATUSES}
    for part in post.get("parts") or []:
        counts[part["status"]] = counts.get(part["status"], 0) + 1
    return counts


def get_post(post_id: str) -> dict | None:
    return load_queue()["posts"].get(str(post_id))


def summary() -> dict:
    """Counts per part status, for the CLI's status output."""
    counts = {status: 0 for status in PART_STATUSES}
    queue = load_queue()
    for post in queue["posts"].values():
        for part in post["parts"]:
            counts[part["status"]] = counts.get(part["status"], 0) + 1
    return {"posts": len(queue["posts"]), "parts": counts}
