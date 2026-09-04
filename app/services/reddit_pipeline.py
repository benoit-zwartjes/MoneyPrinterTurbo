"""
Shared orchestration for Reddit recaps.

Everything both entry points need lives here so the WebUI page and the CLI
runner cannot drift apart. They differ only in how a part is rendered: the
WebUI submits to the in-process task pool and watches task state, while the CLI
writes a manifest and shells out to ``cli.py``.

    discover()        fetch → filter → split, skipping anything already made
    submit_parts()    hand parts to a renderer and record them as rendering
    sync_rendering()  promote in-flight parts once their task finishes
    schedule_parts()  hand approved parts to Upload-Post on a calendar
    sync_uploads()    promote scheduled parts once Upload-Post has published

The last two used to be written out twice, in the WebUI page and in
scripts/reddit_publish.py. They live here for the same reason as the rest: one
copy of the rules for what a part's state means.
"""

from __future__ import annotations

import os
from uuid import uuid4

from loguru import logger

from app.config import config
from app.models import const
from app.models.schema import VideoParams
from app.services import (
    reddit_apify,
    reddit_queue,
    reddit_script,
    reddit_source,
    upload_post,
)


PROVIDER_APIFY = "apify"
PROVIDER_OFFICIAL = "official"
PROVIDERS = (PROVIDER_APIFY, PROVIDER_OFFICIAL)
DEFAULT_PROVIDER = PROVIDER_APIFY

DEFAULT_VIDEO_TERMS = "calm abstract background, slow motion texture, ambient loop"
DEFAULT_VIDEO_SOURCE = "pexels"
DEFAULT_VIDEO_ASPECT = "9:16"
DEFAULT_VOICE_NAME = "en-US-AriaNeural-Female"
DEFAULT_CAPTION_TEMPLATE = "{title} (Part {index}/{total}) #reddit #story"

# Upload-Post reports a scheduled job's outcome as a free-text status. Anything
# outside these two sets means "not settled yet", including a failed poll: a
# network blip must never be recorded as a failed publish.
UPLOAD_DONE_STATES = ("completed", "complete", "success", "succeeded", "published", "uploaded", "done")
UPLOAD_FAILED_STATES = ("failed", "error", "errored", "cancelled", "canceled")


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


def discover_report(options: dict) -> dict:
    """
    Fetch, filter and split until ``max_posts`` usable stories are found.

    Returns the candidates alongside what happened to everything else. A run
    that finds nothing is the common case and the counts are the only way to
    tell the reasons apart: no posts fetched points at credentials or the
    network, posts fetched but none matched points at the filters.

    Never raises on a listing outage — a scheduled run should do nothing that
    day, not crash.
    """
    report = {
        "candidates": [],
        "fetched": 0,
        "matched": 0,
        "skipped_empty": 0,
        "skipped_truncated": 0,
    }

    posts = fetch_posts(options)
    report["fetched"] = len(posts)
    if not posts:
        logger.warning("no posts returned; check credentials, network or filters")
        return report

    candidates = reddit_source.filter_posts(
        posts,
        min_score=options["min_score"],
        min_words=options["min_words"],
        max_words=options["max_words"],
        allow_nsfw=options["allow_nsfw"],
        exclude_ids=reddit_queue.seen_ids(),
    )
    report["matched"] = len(candidates)
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
            report["skipped_empty"] += 1
            logger.info(f"skipping {post['id']}: nothing narratable after cleaning")
            continue
        if split["truncated"] and options["skip_truncated"]:
            report["skipped_truncated"] += 1
            logger.info(
                f"skipping {post['id']}: needs more than {options['max_parts']} parts"
            )
            continue
        splits.append(split)

    report["candidates"] = splits
    return report


def discover(options: dict) -> list[dict]:
    """Just the usable stories; see ``discover_report`` for the counts."""
    return discover_report(options)["candidates"]


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


def caption_for(part: dict) -> str:
    """
    The caption one part publishes with.

    The part number belongs in the caption as well as in the video: on a feed
    the viewer decides whether to go looking for part 1 before they hear
    anything. A template with an unknown placeholder falls back to the part
    subject rather than blocking the upload.
    """
    template = str(_setting("caption_template", DEFAULT_CAPTION_TEMPLATE) or "")
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


def schedule_parts(
    parts: list[dict],
    slots: list[str],
    platforms: list[str] | None = None,
    publish=None,
    on_part=None,
) -> dict:
    """
    Hand each approved part to Upload-Post for its slot.

    ``slots`` are ISO-8601 strings so the caller owns the calendar and this
    stays serialisable — a background job carries them across a thread.
    ``on_part(position, part)`` reports progress. Every part ends in a stored
    state: scheduled with its job id, or failed with the reason.
    """
    publish = publish or upload_post.cross_post_video
    platforms = list(
        platforms
        if platforms is not None
        else upload_post.upload_post_service.platforms
    )

    scheduled = 0
    failed = 0
    errors: list[str] = []

    def fail(part: dict, reason: str) -> None:
        nonlocal failed
        reddit_queue.update_part(
            part["post_id"], part["index"],
            status=reddit_queue.STATUS_FAILED,
            error=reason,
        )
        errors.append(
            f"{part.get('post_id')} part {part.get('index')}/{part.get('total')}: {reason}"
        )
        failed += 1

    for position, (part, slot) in enumerate(zip(parts, slots), start=1):
        if on_part:
            on_part(position, part)

        video_path = part.get("video_path")
        if not video_path or not os.path.exists(video_path):
            fail(part, "video file missing at schedule time")
            continue

        caption = caption_for(part)
        result = publish(
            video_path=video_path,
            title=caption,
            platforms=platforms,
            scheduled_date=slot,
            youtube_extra={
                "youtube_title": caption[:100],
                "youtube_description": part.get("permalink", ""),
                "privacyStatus": upload_post.upload_post_service.youtube_privacy_status,
            },
        )

        job_id = result.get("job_id") if isinstance(result, dict) else None
        if isinstance(result, dict) and result.get("success") and job_id:
            reddit_queue.update_part(
                part["post_id"], part["index"],
                status=reddit_queue.STATUS_SCHEDULED,
                job_id=str(job_id),
                scheduled_for=slot,
                error=None,
            )
            scheduled += 1
            logger.info(
                f"{part['post_id']} part {part['index']}/{part['total']} → {slot}"
            )
            continue

        reason = "invalid Upload-Post response"
        if isinstance(result, dict):
            reason = str(
                result.get("error") or result.get("message") or reason
            )
        fail(part, reason)

    return {"scheduled": scheduled, "failed": failed, "errors": errors}


def upload_job_state(payload) -> str | None:
    """
    Read one Upload-Post status response.

    Returns ``uploaded``, ``failed``, or None while the job has not settled.
    An unreadable response is "not settled": a poll that failed on the network
    says nothing about the job, and marking the part failed there would strand
    a video that is about to publish.
    """
    if not isinstance(payload, dict):
        return None

    status = str(payload.get("status") or payload.get("state") or "").strip().lower()
    if status in UPLOAD_DONE_STATES:
        return reddit_queue.STATUS_UPLOADED
    if status in UPLOAD_FAILED_STATES:
        return reddit_queue.STATUS_FAILED
    return None


def sync_uploads(check_status=None) -> dict:
    """
    Ask Upload-Post what became of every scheduled part.

    ``check_status(job_id=...)`` defaults to the Upload-Post client. A part
    with no job id cannot be followed and is left alone rather than failed —
    it would need re-scheduling by hand either way.
    """
    check_status = check_status or (
        lambda job_id: upload_post.upload_post_service.check_status(job_id=job_id)
    )

    uploaded = 0
    failed = 0

    for part in reddit_queue.parts_with_status(reddit_queue.STATUS_SCHEDULED):
        job_id = part.get("job_id")
        if not job_id:
            continue

        try:
            payload = check_status(job_id=job_id)
        except Exception as exc:
            # A poll that raised leaves the part scheduled; the next pass of the
            # worker asks again.
            logger.warning(f"upload status poll failed for job {job_id}: {exc}")
            continue

        state = upload_job_state(payload)
        if state == reddit_queue.STATUS_UPLOADED:
            reddit_queue.update_part(
                part["post_id"], part["index"],
                status=reddit_queue.STATUS_UPLOADED,
                error=None,
            )
            uploaded += 1
            logger.info(f"{part['post_id']} part {part['index']} published")
        elif state == reddit_queue.STATUS_FAILED:
            error = "upload job failed"
            if isinstance(payload, dict):
                error = str(payload.get("error") or payload.get("message") or error)
            reddit_queue.update_part(
                part["post_id"], part["index"],
                status=reddit_queue.STATUS_FAILED,
                error=error,
            )
            failed += 1
            logger.warning(f"{part['post_id']} part {part['index']}: {error}")

    return {"uploaded": uploaded, "failed": failed}


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
