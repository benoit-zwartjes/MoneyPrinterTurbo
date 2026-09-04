#!/usr/bin/env python3
"""
Review rendered Reddit recap parts and schedule them for publishing.

Rendering leaves every part in the queue as ``rendered``. Nothing reaches a
platform until it is approved here and handed to Upload-Post with a
``scheduled_date``, so the release calendar lives on Upload-Post's side rather
than in a timer this project would have to keep running.

    uv run python scripts/reddit_publish.py list
    uv run python scripts/reddit_publish.py approve 1a2b3c
    uv run python scripts/reddit_publish.py schedule --interval-hours 8
    uv run python scripts/reddit_publish.py status

Parts are scheduled in story order, so part 1 always lands before part 2.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loguru import logger  # noqa: E402

from app.config import config  # noqa: E402
from app.services import reddit_queue, upload_post  # noqa: E402


DEFAULT_INTERVAL_HOURS = 8
DEFAULT_LEAD_MINUTES = 30
MAX_SCHEDULE_DAYS = 365


def _setting(key: str, default=None):
    return config.app.get(f"reddit_{key}", default)


def _caption(part: dict) -> str:
    """
    Build the post caption.

    The part number belongs in the caption as well as the video: on a feed the
    viewer decides whether to look for part 1 before they hear anything.
    """
    template = str(
        _setting("caption_template", "{title} (Part {index}/{total}) #reddit #story")
        or ""
    )
    try:
        return template.format(
            title=part.get("title", ""),
            subreddit=part.get("subreddit", ""),
            index=part.get("index", 1),
            total=part.get("total", 1),
        )[:2200]
    except (KeyError, IndexError):
        logger.warning("reddit_caption_template has an unknown placeholder")
        return str(part.get("subject", ""))[:2200]


def cmd_list(args: argparse.Namespace) -> int:
    pending = reddit_queue.pending_review()
    if args.json:
        print(json.dumps(pending, ensure_ascii=False, indent=2))
        return 0

    if not pending:
        print("Nothing waiting for review.")
        return 0

    by_post: dict[str, list[dict]] = {}
    for part in pending:
        by_post.setdefault(part["post_id"], []).append(part)

    for post_id, parts in by_post.items():
        head = parts[0]
        print(f"\n{post_id}  r/{head['subreddit']}  ({len(parts)} parts)")
        print(f"  {head['title'][:96]}")
        print(f"  {head['permalink']}")
        for part in parts:
            seconds = part.get("estimated_seconds")
            duration = f"{seconds:.0f}s" if isinstance(seconds, (int, float)) else "?"
            print(
                f"    part {part['index']}/{part['total']}  {duration:>5}  "
                f"{part.get('video_path') or 'no file'}"
            )

    print(f"\n{len(by_post)} posts, {len(pending)} parts awaiting approval.")
    print("Approve with: reddit_publish.py approve <post_id> [...]  (or --all)")
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    if args.all:
        approved = reddit_queue.approve_all()
        print(f"Approved {approved} parts.")
        return 0
    if not args.post_ids:
        logger.error("give at least one post id, or --all")
        return 2

    total = 0
    for post_id in args.post_ids:
        changed = reddit_queue.approve_post(post_id)
        if not changed:
            logger.warning(f"{post_id}: nothing in 'rendered' state to approve")
        total += changed
    print(f"Approved {total} parts.")
    return 0


def cmd_reject(args: argparse.Namespace) -> int:
    if not args.post_ids:
        logger.error("give at least one post id")
        return 2
    total = sum(reddit_queue.reject_post(post_id) for post_id in args.post_ids)
    print(f"Rejected {total} parts.")
    return 0


def _slot_times(count: int, start: datetime, interval_hours: float) -> list[datetime]:
    return [start + timedelta(hours=interval_hours * i) for i in range(count)]


def _parse_start(raw: str | None, lead_minutes: int) -> datetime:
    now = datetime.now(timezone.utc)
    if not raw:
        return now + timedelta(minutes=lead_minutes)

    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SystemExit(f"--start is not a valid ISO-8601 datetime: {exc}") from exc

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def cmd_schedule(args: argparse.Namespace) -> int:
    if not upload_post.upload_post_service.is_configured():
        logger.error(
            "Upload-Post is not configured; set upload_post_api_key, "
            "upload_post_username and upload_post_enabled under [app]"
        )
        return 2

    approved = reddit_queue.approved_parts()
    if not approved:
        print("Nothing approved to schedule.")
        return 0

    if args.limit:
        approved = approved[: args.limit]

    interval = args.interval_hours or float(
        _setting("publish_interval_hours", DEFAULT_INTERVAL_HOURS)
    )
    start = _parse_start(args.start, args.lead_minutes)
    slots = _slot_times(len(approved), start, interval)

    horizon = datetime.now(timezone.utc) + timedelta(days=MAX_SCHEDULE_DAYS)
    if slots and slots[-1] > horizon:
        logger.error(
            f"the last slot ({slots[-1].isoformat()}) is beyond Upload-Post's "
            f"{MAX_SCHEDULE_DAYS}-day limit; lower --interval-hours or --limit"
        )
        return 2

    platforms = list(upload_post.upload_post_service.platforms)
    if not platforms:
        logger.error("no upload_post_platforms configured")
        return 2

    if args.dry_run:
        for part, slot in zip(approved, slots):
            print(
                f"{slot.isoformat().replace('+00:00', 'Z')}  "
                f"{part['post_id']} part {part['index']}/{part['total']}  "
                f"{_caption(part)[:70]}"
            )
        print(f"\n{len(approved)} parts would be scheduled to {', '.join(platforms)}.")
        return 0

    scheduled = 0
    failed = 0
    for part, slot in zip(approved, slots):
        video_path = part.get("video_path")
        if not video_path or not os.path.exists(video_path):
            logger.error(
                f"{part['post_id']} part {part['index']}: video file missing "
                f"({video_path or 'no path recorded'})"
            )
            reddit_queue.update_part(
                part["post_id"],
                part["index"],
                status=reddit_queue.STATUS_FAILED,
                error="video file missing at schedule time",
            )
            failed += 1
            continue

        iso_slot = slot.isoformat().replace("+00:00", "Z")
        caption = _caption(part)
        result = upload_post.cross_post_video(
            video_path=video_path,
            title=caption,
            platforms=platforms,
            scheduled_date=iso_slot,
            youtube_extra={
                "youtube_title": caption[:100],
                "youtube_description": part.get("permalink", ""),
                "privacyStatus": upload_post.upload_post_service.youtube_privacy_status,
            },
        )

        job_id = result.get("job_id") if isinstance(result, dict) else None
        if isinstance(result, dict) and result.get("success") and job_id:
            reddit_queue.update_part(
                part["post_id"],
                part["index"],
                status=reddit_queue.STATUS_SCHEDULED,
                job_id=str(job_id),
                scheduled_for=iso_slot,
                error=None,
            )
            scheduled += 1
            logger.info(
                f"{part['post_id']} part {part['index']}/{part['total']} → {iso_slot}"
            )
        else:
            error = (
                result.get("error") or result.get("message") or "unknown upload error"
                if isinstance(result, dict)
                else "invalid Upload-Post response"
            )
            reddit_queue.update_part(
                part["post_id"],
                part["index"],
                status=reddit_queue.STATUS_FAILED,
                error=str(error),
            )
            failed += 1
            logger.error(f"{part['post_id']} part {part['index']}: {error}")

    print(json.dumps({"scheduled": scheduled, "failed": failed}))
    return 1 if failed else 0


def cmd_status(args: argparse.Namespace) -> int:
    summary = reddit_queue.summary()
    if not args.refresh:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    for part in reddit_queue.parts_with_status(reddit_queue.STATUS_SCHEDULED):
        job_id = part.get("job_id")
        if not job_id:
            continue
        result = upload_post.upload_post_service.check_status(job_id=job_id)
        state = str(result.get("status", "") or "").lower() if isinstance(result, dict) else ""
        if state in ("completed", "success", "published"):
            reddit_queue.update_part(
                part["post_id"], part["index"], status=reddit_queue.STATUS_UPLOADED
            )
            logger.info(f"{part['post_id']} part {part['index']} published")
        elif state in ("failed", "error"):
            reddit_queue.update_part(
                part["post_id"],
                part["index"],
                status=reddit_queue.STATUS_FAILED,
                error=str(result.get("error") or "upload job failed"),
            )
            logger.warning(f"{part['post_id']} part {part['index']} failed to publish")

    print(json.dumps(reddit_queue.summary(), ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reddit_publish",
        description="Review rendered Reddit recap parts and schedule uploads.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="show parts awaiting review")
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=cmd_list)

    p_approve = sub.add_parser("approve", help="approve one or more posts")
    p_approve.add_argument("post_ids", nargs="*")
    p_approve.add_argument("--all", action="store_true")
    p_approve.set_defaults(func=cmd_approve)

    p_reject = sub.add_parser("reject", help="reject one or more posts")
    p_reject.add_argument("post_ids", nargs="*")
    p_reject.set_defaults(func=cmd_reject)

    p_schedule = sub.add_parser("schedule", help="schedule approved parts")
    p_schedule.add_argument("--start", default=None,
                            help="ISO-8601 time for the first slot (UTC assumed)")
    p_schedule.add_argument("--interval-hours", type=float, default=None,
                            help=f"gap between slots (default {DEFAULT_INTERVAL_HOURS})")
    p_schedule.add_argument("--lead-minutes", type=int, default=DEFAULT_LEAD_MINUTES,
                            help="delay before the first slot when --start is omitted")
    p_schedule.add_argument("--limit", type=int, default=None,
                            help="schedule at most this many parts")
    p_schedule.add_argument("--dry-run", action="store_true",
                            help="print the calendar without uploading")
    p_schedule.set_defaults(func=cmd_schedule)

    p_status = sub.add_parser("status", help="queue counts, optionally refreshed")
    p_status.add_argument("--refresh", action="store_true",
                          help="poll Upload-Post for each scheduled job")
    p_status.set_defaults(func=cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
