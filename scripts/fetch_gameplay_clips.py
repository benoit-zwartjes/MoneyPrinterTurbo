"""
Fill the gameplay background library from YouTube.

A fresh server renders no Reddit recaps until there is parkour footage in
``storage/local_videos/gameplay``, and the gameplay background is the default.
This is the one-command way to get there:

    uv run python scripts/fetch_gameplay_clips.py
    uv run python scripts/fetch_gameplay_clips.py --count 30 --query "Subway Surfers gameplay no copyright"
    uv run python scripts/fetch_gameplay_clips.py --list

Safe to re-run: clips are named after their YouTube ID and one already in the
library is skipped, so this tops up rather than duplicating.

Only a segment of each video is taken — see ``app/services/gameplay_fetch.py``
for why. The defaults land around 90 MB per clip.
"""

import argparse
import json
import os
import sys

from loguru import logger

root_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if root_dir in sys.path:
    sys.path.remove(root_dir)
sys.path.insert(0, root_dir)

from app.services import gameplay_fetch, gameplay_library  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download gameplay background clips from YouTube."
    )
    parser.add_argument(
        "--query",
        default=gameplay_fetch.DEFAULT_QUERY,
        help=f"YouTube search (default: {gameplay_fetch.DEFAULT_QUERY!r})",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=gameplay_fetch.DEFAULT_COUNT,
        help=f"How many new clips to end up with (default: {gameplay_fetch.DEFAULT_COUNT})",
    )
    parser.add_argument(
        "--segment-seconds",
        type=int,
        default=gameplay_fetch.DEFAULT_SEGMENT_SECONDS,
        help=(
            "Seconds taken from each video "
            f"(default: {gameplay_fetch.DEFAULT_SEGMENT_SECONDS})"
        ),
    )
    parser.add_argument(
        "--segment-start",
        type=int,
        default=gameplay_fetch.DEFAULT_SEGMENT_START,
        help=(
            "Seconds skipped before the segment, past the intro "
            f"(default: {gameplay_fetch.DEFAULT_SEGMENT_START})"
        ),
    )
    parser.add_argument(
        "--max-height",
        type=int,
        default=gameplay_fetch.DEFAULT_MAX_HEIGHT,
        help=f"Resolution ceiling (default: {gameplay_fetch.DEFAULT_MAX_HEIGHT})",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Print the library and exit, downloading nothing.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what the search found and exit, downloading nothing.",
    )
    return parser


def print_library() -> None:
    clips = gameplay_library.clips()
    total = sum(clip["size_bytes"] for clip in clips)
    print(f"{len(clips)} clips, {total / (1024 ** 3):.2f} GB")
    print(f"in {gameplay_library.library_dir(create=False)}")
    for clip in clips:
        size = clip["size_bytes"] // (1024 * 1024)
        print(f"  {clip['name']}  {size:5d} MB  {clip['display_name'][:60]}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list:
        print_library()
        return 0

    if not gameplay_fetch.is_available():
        logger.error(gameplay_fetch.unavailable_reason())
        return 2

    if args.dry_run:
        held = gameplay_fetch.existing_video_ids()
        found = gameplay_fetch.search(args.query, max(args.count * 3, 1))
        print(
            json.dumps(
                {
                    "query": args.query,
                    "results": [
                        {
                            **video,
                            "already_held": video["id"] in held,
                            "too_short": bool(
                                video["duration"]
                                and video["duration"] < gameplay_fetch.MIN_SOURCE_SECONDS
                            ),
                        }
                        for video in found
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    def progress(done: int, wanted: int, title: str) -> None:
        logger.info(f"[{done + 1}/{wanted}] {title[:70]}")

    outcome = gameplay_fetch.fill_library(
        query=args.query,
        count=args.count,
        segment_seconds=args.segment_seconds,
        segment_start=args.segment_start,
        max_height=args.max_height,
        on_progress=progress,
    )

    if outcome["error"]:
        logger.warning(outcome["error"])

    print(json.dumps({k: v for k, v in outcome.items() if v is not None}))
    print_library()

    # A run that downloaded nothing and wanted something is a failure worth a
    # non-zero exit, so a cron job or a CI step notices.
    if not outcome["downloaded"] and args.count:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
