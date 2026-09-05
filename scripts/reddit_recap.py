#!/usr/bin/env python3
"""
Fetch Reddit story posts, split them into Shorts-sized parts and render them.

Runs the whole ingest side of the recap pipeline and then hands off to cli.py
over its documented batch contract — manifest in, JSON summary out. Nothing in
``app/`` is patched by this flow, so tracking upstream stays clean.

    uv run python scripts/reddit_recap.py --max-posts 3
    uv run python scripts/reddit_recap.py --dry-run
    uv run python scripts/reddit_recap.py -- --video-source pexels --voice-name ...

Rendered parts land in the review queue as ``rendered``; nothing publishes
until ``scripts/reddit_publish.py`` schedules it.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loguru import logger  # noqa: E402

from app.services import reddit_pipeline, reddit_queue, reddit_source  # noqa: E402
from app.services import upload_post  # noqa: E402
from app.utils import utils  # noqa: E402


ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reddit_recap",
        description=(
            "Fetch Reddit story posts, split them into parts and render each "
            "part through cli.py. Options default to the [app] reddit_* keys "
            "in config.toml."
        ),
        epilog=(
            "Arguments after -- are forwarded verbatim to cli.py, so any "
            "VideoParams option the CLI accepts can be set for the whole batch. "
            "The manifest carries the background, its clips and the subtitle "
            "style from config, and a manifest field beats a forwarded option."
        ),
    )

    source = parser.add_argument_group("source")
    source.add_argument("--subreddits", default=None, help="comma-separated list")
    source.add_argument("--listing", default=None, choices=reddit_source.LISTINGS)
    source.add_argument("--time-filter", default=None, choices=reddit_source.TIME_FILTERS)
    source.add_argument("--fetch-limit", type=int, default=None,
                        help="posts requested per subreddit (max 100)")

    filters = parser.add_argument_group("filters")
    filters.add_argument("--max-posts", type=int, default=None,
                         help="posts actually rendered this run")
    filters.add_argument("--min-score", type=int, default=None)
    filters.add_argument("--min-words", type=int, default=None)
    filters.add_argument("--max-words", type=int, default=None)
    filters.add_argument("--allow-nsfw", action="store_true", default=None)
    filters.add_argument("--skip-truncated", action="store_true", default=None,
                         help="drop posts too long to fit --max-parts")

    shape = parser.add_argument_group("part shape")
    shape.add_argument("--part-seconds", type=int, default=None,
                       help="target narration seconds per part")
    shape.add_argument("--max-parts", type=int, default=None)
    shape.add_argument("--video-terms", default=None,
                       help="material search terms shared by every part")

    execution = parser.add_argument_group("execution")
    execution.add_argument("--dry-run", action="store_true",
                           help="fetch and split only; render nothing")
    execution.add_argument("--manifest-only", action="store_true",
                           help="write the manifest and stop")
    execution.add_argument("--allow-auto-upload", action="store_true",
                           help="proceed even when Upload-Post auto-upload is on")
    execution.add_argument("cli_args", nargs=argparse.REMAINDER,
                           help="arguments after -- are passed to cli.py")

    return parser


def resolve_options(args: argparse.Namespace) -> dict:
    """Turn CLI flags into overrides; the service fills in config and defaults."""
    return reddit_pipeline.resolve_options(
        subreddits=args.subreddits,
        listing=args.listing,
        time_filter=args.time_filter,
        fetch_limit=args.fetch_limit,
        max_posts=args.max_posts,
        min_score=args.min_score,
        min_words=args.min_words,
        max_words=args.max_words,
        allow_nsfw=args.allow_nsfw,
        skip_truncated=args.skip_truncated,
        part_seconds=args.part_seconds,
        max_parts=args.max_parts,
        video_terms=args.video_terms,
    )


def manifest_row(part: dict, options: dict, key: str = "") -> dict:
    """
    One part as VideoParams overrides for cli.py.

    Built from the same ``build_video_params`` the WebUI submits, so a cron run
    and a click produce the same video: the same background, the same clip, the
    same subtitle style. ``key`` identifies the story, so all of its parts play
    over one clip.
    """
    params = reddit_pipeline.build_video_params(part, options, key)
    row = {
        "video_subject": params.video_subject,
        "video_script": params.video_script,
        "video_terms": params.video_terms,
        "video_source": params.video_source,
        "font_name": params.font_name,
        "font_size": params.font_size,
        "text_fore_color": params.text_fore_color,
        "stroke_color": params.stroke_color,
        "stroke_width": params.stroke_width,
    }
    if params.video_materials:
        # Absolute paths: a manifest resolves relative material paths against
        # its own directory, and the library is nowhere near it.
        row["video_materials"] = [
            {"provider": material.provider, "url": material.url}
            for material in params.video_materials
        ]
    return row


def write_manifest(splits: list[dict], options: dict) -> str:
    """One JSONL line per part, in the order they should be published."""
    manifest_dir = utils.storage_dir("reddit/manifests", create=True)
    path = os.path.join(manifest_dir, f"{time.strftime('%Y%m%d-%H%M%S')}.jsonl")

    with open(path, "w", encoding="utf-8") as handle:
        for split in splits:
            key = reddit_pipeline.story_key(split)
            for part in split["parts"]:
                handle.write(
                    json.dumps(manifest_row(part, options, key), ensure_ascii=False)
                    + "\n"
                )
    return path


def run_cli(manifest_path: str, extra_args: list[str]) -> dict | None:
    """Run cli.py in batch mode and return its JSON summary."""
    command = [
        sys.executable,
        os.path.join(ROOT_DIR, "cli.py"),
        "--batch-file",
        manifest_path,
        *extra_args,
    ]
    logger.info(
        "rendering via cli.py"
        + (f" with {len(extra_args)} forwarded options" if extra_args else "")
    )

    completed = subprocess.run(
        command,
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.stderr:
        # cli.py logs to stderr; surface it so a scheduled run is diagnosable.
        sys.stderr.write(completed.stderr)

    stdout = (completed.stdout or "").strip()
    if not stdout:
        logger.error(f"cli.py produced no summary (exit {completed.returncode})")
        return None

    try:
        return json.loads(stdout.splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as exc:
        logger.error(f"could not parse the cli.py summary: {exc}")
        return None


def record_results(splits: list[dict], summary: dict) -> None:
    """Map the flat batch summary back onto posts and parts, in manifest order."""
    tasks = summary.get("tasks") or []
    cursor = 0

    for split in splits:
        parts_meta = []
        for part in split["parts"]:
            task = tasks[cursor] if cursor < len(tasks) else {}
            cursor += 1

            result = task.get("result") if isinstance(task.get("result"), dict) else {}
            videos = result.get("videos") or []
            succeeded = task.get("status") == "succeeded" and bool(videos)

            parts_meta.append(
                {
                    "index": part["index"],
                    "total": part["total"],
                    "status": (
                        reddit_queue.STATUS_RENDERED
                        if succeeded
                        else reddit_queue.STATUS_FAILED
                    ),
                    "task_id": task.get("task_id"),
                    "video_path": videos[0] if videos else None,
                    "error": task.get("error"),
                }
            )

        reddit_queue.record_post(split, parts_meta)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # argparse.REMAINDER keeps the "--" separator; cli.py should not see it.
    extra_args = [a for a in (args.cli_args or []) if a != "--"]

    if not reddit_source.is_configured():
        logger.error(
            "reddit_client_id and reddit_client_secret are not set under [app] "
            "in config.toml; register a script app at "
            "https://www.reddit.com/prefs/apps"
        )
        return 2

    auto_upload = (
        upload_post.upload_post_service.is_configured()
        and upload_post.upload_post_service.auto_upload
    )
    if auto_upload and not args.allow_auto_upload:
        logger.error(
            "Upload-Post auto-upload is enabled, so every rendered part would "
            "publish immediately and the review queue would be pointless. Set "
            "upload_post_auto_upload = false in config.toml, or pass "
            "--allow-auto-upload to override."
        )
        return 2

    options = reddit_pipeline.with_gameplay_snapshot(resolve_options(args))

    for issue in reddit_pipeline.background_issues(options):
        # Rendering would otherwise fail once per part at the materials stage,
        # after paying for the fetch.
        logger.error(issue)
        return 2

    splits = reddit_pipeline.discover(options)
    if not splits:
        print(json.dumps({"posts": 0, "parts": 0, "rendered": 0, "failed": 0}))
        return 0

    total_parts = sum(len(s["parts"]) for s in splits)
    for split in splits:
        logger.info(
            f"r/{split['subreddit']} {split['post_id']}: "
            f"{len(split['parts'])} parts, {split['total_words']} words — "
            f"{split['title'][:70]}"
        )

    if args.dry_run:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "posts": len(splits),
                    "parts": total_parts,
                    "splits": splits,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    manifest_path = write_manifest(splits, options)
    logger.info(f"manifest written: {manifest_path}")

    if args.manifest_only:
        print(json.dumps({"manifest": manifest_path, "parts": total_parts}))
        return 0

    summary = run_cli(manifest_path, extra_args)
    if summary is None:
        return 1

    record_results(splits, summary)

    rendered = summary.get("succeeded", 0)
    failed = summary.get("failed", 0)
    print(
        json.dumps(
            {
                "posts": len(splits),
                "parts": total_parts,
                "rendered": rendered,
                "failed": failed,
                "manifest": manifest_path,
                "review": "uv run python scripts/reddit_publish.py list",
            },
            ensure_ascii=False,
        )
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
