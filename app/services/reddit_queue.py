"""
Persistent state for the Reddit recap pipeline.

Three jobs, one file at ``storage/reddit_recaps.json``:

* remember which post IDs have already been made, so a scheduled run never
  recaps the same thread twice;
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
import tempfile
import threading
import time

from loguru import logger

from app.utils import utils


QUEUE_FILE_NAME = "reddit_recaps.json"
MAX_POSTS = 1000

# A part moves rendering → rendered → approved → scheduled → uploaded, and can
# drop out to failed or rejected at any point. The WebUI submits renders to the
# background pool and so passes through 'rendering'; the CLI runner blocks on
# cli.py and records parts as 'rendered' directly.
STATUS_RENDERING = "rendering"
STATUS_RENDERED = "rendered"
STATUS_APPROVED = "approved"
STATUS_SCHEDULED = "scheduled"
STATUS_UPLOADED = "uploaded"
STATUS_FAILED = "failed"
STATUS_REJECTED = "rejected"

PART_STATUSES = (
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
        "estimated_seconds": raw.get("estimated_seconds"),
        "job_id": _str_or_none(raw.get("job_id")),
        "scheduled_for": _str_or_none(raw.get("scheduled_for")),
        "error": _str_or_none(raw.get("error")),
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
            by_index[index]["estimated_seconds"] = script_part.get("estimated_seconds")
            by_index[index]["total"] = script_part.get("total", by_index[index]["total"])

    with _queue_lock:
        queue = load_queue()
        queue["posts"][post_id] = {
            "post_id": post_id,
            "subreddit": str(split_result.get("subreddit", "") or ""),
            "title": str(split_result.get("title", "") or ""),
            "permalink": str(split_result.get("permalink", "") or ""),
            "score": int(split_result.get("score", 0) or 0),
            "created_at": time.time(),
            "truncated": bool(split_result.get("truncated")),
            "parts": sorted(by_index.values(), key=lambda p: p["index"]),
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


def set_post_status(post_id: str, status: str, only_from: str | None = None) -> int:
    """
    Move every part of one post to ``status``; returns how many changed.

    ``only_from`` guards against re-approving something already scheduled.
    """
    if status not in PART_STATUSES:
        return 0

    with _queue_lock:
        queue = load_queue()
        post = queue["posts"].get(str(post_id))
        if not post:
            return 0

        changed = 0
        for part in post["parts"]:
            if only_from is not None and part["status"] != only_from:
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


def reject_post(post_id: str) -> int:
    return set_post_status(post_id, STATUS_REJECTED, only_from=STATUS_RENDERED)


def approve_all() -> int:
    approved = 0
    for post_id in {part["post_id"] for part in pending_review()}:
        approved += approve_post(post_id)
    return approved


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
