"""
Shared orchestration for Reddit recaps.

Everything both entry points need lives here so the WebUI page and the CLI
runner cannot drift apart. They differ only in how a part is rendered: the
WebUI submits to the in-process task pool and watches task state, while the CLI
writes a manifest and shells out to ``cli.py``.

    discover()        fetch → filter → split, skipping anything already made
    submit_parts()    hand parts to a renderer and record them as rendering
    sync_rendering()  promote in-flight parts once their task finishes
"""

from __future__ import annotations

from uuid import uuid4

from loguru import logger

from app.config import config
from app.models import const
from app.models.schema import VideoParams
from app.services import reddit_apify, reddit_queue, reddit_script, reddit_source


PROVIDER_APIFY = "apify"
PROVIDER_OFFICIAL = "official"
PROVIDERS = (PROVIDER_APIFY, PROVIDER_OFFICIAL)
DEFAULT_PROVIDER = PROVIDER_APIFY

DEFAULT_VIDEO_TERMS = "calm abstract background, slow motion texture, ambient loop"
DEFAULT_VIDEO_SOURCE = "pexels"
DEFAULT_VIDEO_ASPECT = "9:16"
DEFAULT_VOICE_NAME = "en-US-AriaNeural-Female"


def _setting(key: str, default=None):
    return config.app.get(f"reddit_{key}", default)


def _int_setting(key: str, default: int) -> int:
    try:
        return int(_setting(key, default))
    except (TypeError, ValueError):
        return default


def _bool_setting(key: str, default: bool) -> bool:
    value = _setting(key, default)
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "no", "off", "")
    return bool(value)


def _subreddit_list(raw) -> list[str] | None:
    if isinstance(raw, str):
        return [s.strip() for s in raw.split(",") if s.strip()] or None
    if isinstance(raw, (list, tuple)):
        return [str(s).strip() for s in raw if str(s).strip()] or None
    return None


def resolve_options(**overrides) -> dict:
    """
    Build the option set, taking each value from the first source that has it:
    an explicit override, then ``[app] reddit_*`` in config.toml, then a default.
    """
    provider = str(_setting("provider", DEFAULT_PROVIDER) or DEFAULT_PROVIDER).strip()
    options = {
        "provider": provider if provider in PROVIDERS else DEFAULT_PROVIDER,
        "subreddits": _subreddit_list(_setting("subreddits", None)),
        "listing": _setting("listing", reddit_source.DEFAULT_LISTING),
        "time_filter": _setting("time_filter", reddit_source.DEFAULT_TIME_FILTER),
        "fetch_limit": _int_setting("fetch_limit", reddit_source.DEFAULT_FETCH_LIMIT),
        "max_posts": _int_setting("max_posts_per_run", 3),
        "min_score": _int_setting("min_score", 500),
        "min_words": _int_setting("min_words", 120),
        "max_words": _int_setting("max_words", 1500),
        "allow_nsfw": _bool_setting("allow_nsfw", False),
        "skip_truncated": _bool_setting("skip_truncated", True),
        "part_seconds": _int_setting("part_seconds", reddit_script.DEFAULT_PART_SECONDS),
        "max_parts": _int_setting("max_parts", reddit_script.DEFAULT_MAX_PARTS),
        "video_terms": str(_setting("video_terms", DEFAULT_VIDEO_TERMS) or DEFAULT_VIDEO_TERMS),
        "video_source": str(_setting("video_source", DEFAULT_VIDEO_SOURCE) or DEFAULT_VIDEO_SOURCE),
        "video_aspect": str(_setting("video_aspect", DEFAULT_VIDEO_ASPECT) or DEFAULT_VIDEO_ASPECT),
        "voice_name": str(_setting("voice_name", DEFAULT_VOICE_NAME) or DEFAULT_VOICE_NAME),
        "subtitle_enabled": _bool_setting("subtitle_enabled", True),
    }

    for key, value in overrides.items():
        if value is None:
            continue
        if key == "subreddits":
            value = _subreddit_list(value) or options["subreddits"]
        options[key] = value

    return options


def fetch_posts(options: dict) -> list[dict]:
    """
    Fetch listings through whichever backend is selected.

    Both return the same normalised post shape, so nothing downstream — filters,
    splitting, the queue, the page — has to know which one ran.
    """
    if options.get("provider", DEFAULT_PROVIDER) == PROVIDER_APIFY:
        return reddit_apify.fetch_story_posts(
            subreddits=options.get("subreddits"),
            listing=options["listing"],
            time_filter=options["time_filter"],
            limit=options["fetch_limit"],
            # The actor filters NSFW during the scrape, so asking for it here
            # avoids paying for results the local filter would then discard.
            allow_nsfw=options["allow_nsfw"],
        )

    return reddit_source.fetch_story_posts(
        subreddits=options.get("subreddits"),
        listing=options["listing"],
        time_filter=options["time_filter"],
        limit=options["fetch_limit"],
    )


def discover(options: dict) -> list[dict]:
    """
    Fetch, filter and split until ``max_posts`` usable stories are found.

    Returns an empty list rather than raising when the fetch fails: a listing
    outage should leave a scheduled run doing nothing, not crashing.
    """
    posts = fetch_posts(options)
    if not posts:
        logger.warning("no posts returned; check credentials, network or filters")
        return []

    candidates = reddit_source.filter_posts(
        posts,
        min_score=options["min_score"],
        min_words=options["min_words"],
        max_words=options["max_words"],
        allow_nsfw=options["allow_nsfw"],
        exclude_ids=reddit_queue.seen_ids(),
    )
    logger.info(f"{len(candidates)} of {len(posts)} posts passed the filters")

    splits: list[dict] = []
    for post in candidates:
        if len(splits) >= options["max_posts"]:
            break
        split = reddit_script.split_post(
            post,
            part_seconds=options["part_seconds"],
            max_parts=options["max_parts"],
        )
        if not split["parts"]:
            logger.info(f"skipping {post['id']}: nothing narratable after cleaning")
            continue
        if split["truncated"] and options["skip_truncated"]:
            logger.info(
                f"skipping {post['id']}: needs more than {options['max_parts']} parts"
            )
            continue
        splits.append(split)

    return splits


def build_video_params(part: dict, options: dict) -> VideoParams:
    """
    One part becomes one video.

    The script is supplied directly, so the LLM script stage is skipped —
    the whole point of a recap is to narrate the post, not to write about it.
    """
    return VideoParams(
        video_subject=part["subject"],
        video_script=part["script"],
        video_terms=options["video_terms"],
        video_aspect=options["video_aspect"],
        video_source=options["video_source"],
        voice_name=options["voice_name"],
        subtitle_enabled=options["subtitle_enabled"],
        video_count=1,
    )


def submit_parts(splits: list[dict], options: dict, submit) -> dict:
    """
    Render every part of every split through ``submit``.

    ``submit(task_id, params, part, split)`` starts one render. Parts are
    recorded before the call returns so a page refresh mid-submit still shows
    them, and a submit failure marks that part failed rather than losing it.
    """
    submitted = 0
    failed = 0

    for split in splits:
        parts_meta = []
        for part in split["parts"]:
            task_id = str(uuid4())
            params = build_video_params(part, options)
            entry = {
                "index": part["index"],
                "total": part["total"],
                "task_id": task_id,
                "status": reddit_queue.STATUS_RENDERING,
                "video_path": None,
                "error": None,
            }
            try:
                submit(task_id, params, part, split)
                submitted += 1
            except Exception as exc:
                entry["status"] = reddit_queue.STATUS_FAILED
                entry["error"] = f"{type(exc).__name__}: {exc}"
                failed += 1
                logger.exception(
                    f"failed to submit reddit recap part: "
                    f"post={split['post_id']} part={part['index']} error={exc}"
                )
            parts_meta.append(entry)

        reddit_queue.record_post(split, parts_meta)

    return {"posts": len(splits), "submitted": submitted, "failed": failed}


def sync_rendering(get_task) -> dict:
    """
    Move in-flight parts to their final state by reading task state.

    ``get_task(task_id)`` returns the task record, or None when it is gone. A
    task the state store has forgotten — a restart while rendering — is marked
    failed rather than left rendering forever, which would strand the part
    outside the review queue with nothing able to move it.
    """
    promoted = 0
    failed = 0

    for part in reddit_queue.parts_with_status(reddit_queue.STATUS_RENDERING):
        task_id = part.get("task_id")
        task = get_task(task_id) if task_id else None

        if not task:
            reddit_queue.update_part(
                part["post_id"],
                part["index"],
                status=reddit_queue.STATUS_FAILED,
                error="render task is no longer tracked",
            )
            failed += 1
            continue

        state = task.get("state")
        if state == const.TASK_STATE_COMPLETE:
            videos = (task.get("videos") or []) if isinstance(task, dict) else []
            if videos:
                reddit_queue.update_part(
                    part["post_id"],
                    part["index"],
                    status=reddit_queue.STATUS_RENDERED,
                    video_path=videos[0],
                    error=None,
                )
                promoted += 1
            else:
                reddit_queue.update_part(
                    part["post_id"],
                    part["index"],
                    status=reddit_queue.STATUS_FAILED,
                    error="task completed without producing a video",
                )
                failed += 1
        elif state == const.TASK_STATE_FAILED:
            reddit_queue.update_part(
                part["post_id"],
                part["index"],
                status=reddit_queue.STATUS_FAILED,
                error=str(task.get("error") or "render failed"),
            )
            failed += 1

    return {"rendered": promoted, "failed": failed}


def is_configured(provider: str | None = None) -> bool:
    """Whether the selected backend has what it needs to fetch."""
    provider = provider or str(
        _setting("provider", DEFAULT_PROVIDER) or DEFAULT_PROVIDER
    )
    if provider == PROVIDER_APIFY:
        return reddit_apify.is_configured()
    return reddit_source.is_configured()


def blocking_issues(provider: str | None = None) -> list[str]:
    """
    Reasons the pipeline cannot run right now, in the order worth fixing them.

    Returned as plain sentences so both the CLI and the WebUI show the same
    wording without either owning the copy.
    """
    provider = provider or str(
        _setting("provider", DEFAULT_PROVIDER) or DEFAULT_PROVIDER
    )
    issues = []
    if provider == PROVIDER_APIFY:
        if not reddit_apify.is_configured():
            issues.append(
                "An Apify API token is missing. Copy it from "
                "console.apify.com/settings/integrations and set "
                "reddit_apify_token."
            )
    elif not reddit_source.is_configured():
        issues.append(
            "Reddit credentials are missing. Register a script app at "
            "reddit.com/prefs/apps and set reddit_client_id and "
            "reddit_client_secret."
        )
    return issues
