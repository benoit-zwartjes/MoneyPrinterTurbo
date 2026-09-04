"""
Reddit Recaps — the whole pipeline as a page.

Find stories, render them into Shorts-sized parts, review what came out, then
hand the approved parts to Upload-Post on a schedule.

Kept as a separate page rather than folded into Main.py on purpose: Main.py is
the file upstream changes most, and a new file never conflicts on merge. The
shared work lives in app/services/reddit_pipeline.py, so this page and
scripts/reddit_recap.py cannot drift apart.
"""

import os
import sys
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path

import streamlit as st
from loguru import logger

# Match Main.py: the project root must outrank third-party packages, or a
# dependency shipping its own "app" package shadows ours.
root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
if root_dir in sys.path:
    sys.path.remove(root_dir)
sys.path.insert(0, root_dir)

from app.config import config  # noqa: E402
from app.models.schema import VideoAspect  # noqa: E402
from app.services import (  # noqa: E402
    reddit_pipeline,
    reddit_queue,
    reddit_source,  # noqa: F401  (provider labels + official backend)
    state as sm,
    upload_post,
    webui_task,
)

st.set_page_config(page_title="Reddit Recaps", page_icon="🧵", layout="wide")

_style_file = Path(__file__).parent.parent / "styles.css"
if _style_file.exists():
    st.markdown(
        f"<style>{_style_file.read_text(encoding='utf-8')}</style>",
        unsafe_allow_html=True,
    )

_VIDEO_SOURCES = ("pexels", "pixabay", "coverr")
_LISTING_HELP = "top uses the time window below; hot, new and rising ignore it."


def _set_config(key: str, value) -> None:
    """
    Persist one reddit_* setting the way Main.py does.

    Rendering holds ``runtime_config_lock`` for the whole task, and this page
    starts renders in the background. A blocking save would freeze the browser
    for as long as a video takes; the non-blocking path queues the value and
    applies it when the lock frees up.
    """
    if config.app.get(key) != value:
        config.update_config_nonblocking(config.app, key, value)


# -----------------------------------------------------------------------------
# Setup
# -----------------------------------------------------------------------------


_PROVIDER_LABELS = {
    reddit_pipeline.PROVIDER_APIFY: "Apify · reddit-scraper-lite",
    reddit_pipeline.PROVIDER_OFFICIAL: "Reddit API · script app",
}


def _render_setup() -> bool:
    """Credentials and the auto-upload conflict. Returns True when good to go."""
    ready = True
    providers = list(reddit_pipeline.PROVIDERS)
    current = config.app.get("reddit_provider", reddit_pipeline.DEFAULT_PROVIDER)

    with st.expander(
        "Connection", expanded=not reddit_pipeline.is_configured()
    ):
        provider = st.radio(
            "Fetch backend",
            options=providers,
            index=providers.index(current) if current in providers else 0,
            format_func=lambda value: _PROVIDER_LABELS[value],
            horizontal=True,
            key="reddit_provider_input",
        )
        _set_config("reddit_provider", provider)

        if provider == reddit_pipeline.PROVIDER_APIFY:
            st.caption("Runs the trudax/reddit-scraper-lite actor. No Reddit app registration and no per-client rate limit.")
            col_token, col_timeout = st.columns([2, 1])
            with col_token:
                token = st.text_input(
                    "Apify API token",
                    value=config.app.get("reddit_apify_token", ""),
                    type="password",
                    help="From console.apify.com under Settings, Integrations.",
                    key="reddit_apify_token_input",
                )
                _set_config("reddit_apify_token", token)
            with col_timeout:
                timeout = st.number_input(
                    "Run timeout (s)",
                    min_value=60, max_value=3600, step=30,
                    value=int(config.app.get("reddit_apify_timeout_seconds", 300)),
                    help="How long to wait for one actor run. Results stay in the Apify dataset either way, so a timeout costs the wait, not the run.",
                    key="reddit_apify_timeout_input",
                )
                _set_config("reddit_apify_timeout_seconds", int(timeout))
            st.caption("This actor is pay-per-result, so the fetch limit below is a cost control as well as a size one. Comments are never fetched.")
        else:
            st.caption("Register a script app at reddit.com/prefs/apps. The username only builds the User-Agent Reddit requires.")
            col_id, col_secret, col_user = st.columns(3)
            with col_id:
                client_id = st.text_input(
                    "Client ID",
                    value=config.app.get("reddit_client_id", ""),
                    type="password",
                    key="reddit_client_id_input",
                )
                _set_config("reddit_client_id", client_id)
            with col_secret:
                client_secret = st.text_input(
                    "Client secret",
                    value=config.app.get("reddit_client_secret", ""),
                    type="password",
                    key="reddit_client_secret_input",
                )
                _set_config("reddit_client_secret", client_secret)
            with col_user:
                username = st.text_input(
                    "Reddit username",
                    value=config.app.get("reddit_username", ""),
                    help="Used only in the User-Agent. Generic agents are throttled hard.",
                    key="reddit_username_input",
                )
                _set_config("reddit_username", username)

        for issue in reddit_pipeline.blocking_issues(provider):
            st.warning(issue)
            ready = False

    # Auto-upload would publish each part the moment it renders, which makes
    # the review queue below meaningless. Flag it rather than silently racing.
    if (
        upload_post.upload_post_service.is_configured()
        and upload_post.upload_post_service.auto_upload
    ):
        st.error("Upload-Post auto-publish is on, so every part would publish the moment it renders and never reach the review queue.")
        if st.button("Turn off auto-publish", key="disable_auto_upload"):
            _set_config("upload_post_auto_upload", False)
            st.rerun()
        ready = False

    return ready


# -----------------------------------------------------------------------------
# Find stories
# -----------------------------------------------------------------------------


def _render_options() -> dict:
    with st.expander("Story filters", expanded=False):
        col_a, col_b, col_c = st.columns(3)

        with col_a:
            subreddits = st.text_input(
                "Subreddits",
                value=", ".join(
                    config.app.get("reddit_subreddits", list(reddit_source.DEFAULT_SUBREDDITS))
                    if isinstance(config.app.get("reddit_subreddits"), list)
                    else [str(config.app.get("reddit_subreddits", ""))]
                ),
                help="Comma separated. Story subreddits work best — the pipeline narrates the post body.",
                key="reddit_subreddits_input",
            )
            listing = st.selectbox(
                "Listing",
                options=list(reddit_source.LISTINGS),
                index=list(reddit_source.LISTINGS).index(
                    config.app.get("reddit_listing", reddit_source.DEFAULT_LISTING)
                    if config.app.get("reddit_listing") in reddit_source.LISTINGS
                    else reddit_source.DEFAULT_LISTING
                ),
                help=_LISTING_HELP,
                key="reddit_listing_input",
            )
            time_filter = st.selectbox(
                "Time window",
                options=list(reddit_source.TIME_FILTERS),
                index=list(reddit_source.TIME_FILTERS).index(
                    config.app.get("reddit_time_filter", reddit_source.DEFAULT_TIME_FILTER)
                    if config.app.get("reddit_time_filter") in reddit_source.TIME_FILTERS
                    else reddit_source.DEFAULT_TIME_FILTER
                ),
                disabled=listing != "top",
                key="reddit_time_filter_input",
            )

        with col_b:
            min_score = st.number_input(
                "Minimum score",
                min_value=0, step=50,
                value=int(config.app.get("reddit_min_score", 500)),
                key="reddit_min_score_input",
            )
            min_words = st.number_input(
                "Minimum words",
                min_value=0, step=20,
                value=int(config.app.get("reddit_min_words", 120)),
                key="reddit_min_words_input",
            )
            max_words = st.number_input(
                "Maximum words",
                min_value=0, step=100,
                value=int(config.app.get("reddit_max_words", 1500)),
                help="Longer stories need more parts. 0 removes the limit.",
                key="reddit_max_words_input",
            )

        with col_c:
            max_posts = st.number_input(
                "Stories per run",
                min_value=1, max_value=20, step=1,
                value=int(config.app.get("reddit_max_posts_per_run", 3)),
                key="reddit_max_posts_input",
            )
            allow_nsfw = st.checkbox(
                "Allow NSFW",
                value=bool(config.app.get("reddit_allow_nsfw", False)),
                key="reddit_allow_nsfw_input",
            )
            skip_truncated = st.checkbox(
                "Skip overlong stories",
                value=bool(config.app.get("reddit_skip_truncated", True)),
                help="Drop a story needing more than the maximum parts, rather than publishing one that stops mid-plot.",
                key="reddit_skip_truncated_input",
            )

    with st.expander("Parts and video", expanded=False):
        col_d, col_e, col_f = st.columns(3)

        with col_d:
            part_seconds = st.number_input(
                "Seconds per part",
                min_value=10, max_value=180, step=5,
                value=int(config.app.get("reddit_part_seconds", 55)),
                help="Narration target. The title and the follow-on line are charged against it, so parts land under the limit.",
                key="reddit_part_seconds_input",
            )
            max_parts = st.number_input(
                "Maximum parts",
                min_value=1, max_value=10, step=1,
                value=int(config.app.get("reddit_max_parts", 4)),
                key="reddit_max_parts_input",
            )
            words_per_minute = st.number_input(
                "Narration words per minute",
                min_value=80, max_value=260, step=5,
                value=int(config.app.get("reddit_words_per_minute", 150)),
                help="Used to turn a word count into seconds. Raise it if your voice rate is above 1.0.",
                key="reddit_wpm_input",
            )

        with col_e:
            aspect_values = [a.value for a in VideoAspect]
            video_aspect = st.selectbox(
                "Video Aspect Ratio",
                options=aspect_values,
                index=aspect_values.index(
                    config.app.get("reddit_video_aspect", VideoAspect.portrait.value)
                    if config.app.get("reddit_video_aspect") in aspect_values
                    else VideoAspect.portrait.value
                ),
                key="reddit_video_aspect_input",
            )
            video_source = st.selectbox(
                "Video Source",
                options=list(_VIDEO_SOURCES),
                index=list(_VIDEO_SOURCES).index(
                    config.app.get("reddit_video_source", "pexels")
                    if config.app.get("reddit_video_source") in _VIDEO_SOURCES
                    else "pexels"
                ),
                key="reddit_video_source_input",
            )
            subtitle_enabled = st.checkbox(
                "Enable Subtitles",
                value=bool(config.app.get("reddit_subtitle_enabled", True)),
                key="reddit_subtitle_enabled_input",
            )

        with col_f:
            voice_name = st.text_input(
                "Voice",
                value=config.app.get(
                    "reddit_voice_name", reddit_pipeline.DEFAULT_VOICE_NAME
                ),
                help="Any voice the audio settings on the main page accept.",
                key="reddit_voice_name_input",
            )
            video_terms = st.text_area(
                "Material search terms",
                value=config.app.get(
                    "reddit_video_terms", reddit_pipeline.DEFAULT_VIDEO_TERMS
                ),
                height=88,
                help="Shared by every part. Story recaps use filler b-roll, so generic calm footage reads better than terms from the story.",
                key="reddit_video_terms_input",
            )

    subreddit_list = [s.strip() for s in subreddits.split(",") if s.strip()]
    for key, value in {
        "reddit_subreddits": subreddit_list,
        "reddit_listing": listing,
        "reddit_time_filter": time_filter,
        "reddit_min_score": int(min_score),
        "reddit_min_words": int(min_words),
        "reddit_max_words": int(max_words),
        "reddit_max_posts_per_run": int(max_posts),
        "reddit_allow_nsfw": bool(allow_nsfw),
        "reddit_skip_truncated": bool(skip_truncated),
        "reddit_part_seconds": int(part_seconds),
        "reddit_max_parts": int(max_parts),
        "reddit_words_per_minute": int(words_per_minute),
        "reddit_video_aspect": video_aspect,
        "reddit_video_source": video_source,
        "reddit_subtitle_enabled": bool(subtitle_enabled),
        "reddit_voice_name": voice_name,
        "reddit_video_terms": video_terms,
    }.items():
        _set_config(key, value)

    return reddit_pipeline.resolve_options()


def _render_find(options: dict) -> None:
    st.subheader("Find stories")

    col_button, col_note = st.columns([1, 3])
    with col_button:
        find_clicked = st.button(
            "Find stories", type="primary", use_container_width=True
        )
    with col_note:
        st.caption("Fetches the listings, drops anything already made, and splits what is left into parts. Nothing renders yet.")

    if find_clicked:
        with st.spinner("Fetching stories…"):
            try:
                st.session_state["reddit_candidates"] = reddit_pipeline.discover(options)
            except Exception as exc:
                logger.exception(f"reddit discovery failed: {exc}")
                st.session_state["reddit_candidates"] = []
                st.error(f"Fetch failed: {type(exc).__name__}: {exc}")

    candidates = st.session_state.get("reddit_candidates") or []
    if not candidates:
        if find_clicked:
            st.info("Nothing matched. Loosen the score or word filters, or widen the time window.")
        return

    st.caption(f"{len(candidates)} stories ready. Untick any you do not want.")

    selected: list[dict] = []
    for split in candidates:
        total_seconds = sum(p["estimated_seconds"] for p in split["parts"])
        header = (
            f"r/{split['subreddit']} · {split['score']:,} · "
            f"{len(split['parts'])} parts · {total_seconds:.0f}s — "
            f"{split['title'][:80]}"
        )
        with st.container(border=True):
            keep = st.checkbox(header, value=True, key=f"pick_{split['post_id']}")
            st.caption(split["permalink"])
            for part in split["parts"]:
                with st.expander(
                    f"Part {part['index']}/{part['total']} · "
                    f"{part['estimated_seconds']:.0f}s",
                    expanded=False,
                ):
                    st.write(part["script"])
            if keep:
                selected.append(split)

    st.divider()
    if st.button(
        f"Render {len(selected)} stories",
        type="primary",
        disabled=not selected,
    ):
        _submit_renders(selected, options)


def _submit_renders(splits: list[dict], options: dict) -> None:
    def submit(task_id, params, part, split):
        webui_task.submit_generation(
            task_id=task_id,
            params=params,
            capture_logs=not config.ui.get("hide_log", False),
        )

    result = reddit_pipeline.submit_parts(splits, options, submit)
    st.session_state["reddit_candidates"] = []

    if result["failed"]:
        st.warning(
            f"{result['submitted']} parts queued, "
            f"{result['failed']} could not start."
        )
    else:
        st.success(f"{result['submitted']} parts queued for rendering.")
    st.rerun()


# -----------------------------------------------------------------------------
# Review
# -----------------------------------------------------------------------------


def _render_review() -> None:
    # Promote anything whose render finished since the last page run, so the
    # queue reflects reality without the user knowing tasks exist.
    reddit_pipeline.sync_rendering(sm.state.get_task)

    rendering = reddit_queue.parts_with_status(reddit_queue.STATUS_RENDERING)
    pending = reddit_queue.pending_review()

    header = st.columns([3, 1])
    with header[0]:
        st.subheader("Review")
    with header[1]:
        if st.button("Refresh", use_container_width=True):
            st.rerun()

    if rendering:
        st.info(f"{len(rendering)} parts still rendering.")
        for part in rendering:
            task = sm.state.get_task(part["task_id"]) or {}
            st.progress(
                min(int(task.get("progress", 0) or 0), 100),
                text=f"{part['title'][:60]} · Part {part['index']}/{part['total']}",
            )

    if not pending:
        if not rendering:
            st.caption("Nothing waiting for review.")
        return

    by_post: dict[str, list[dict]] = {}
    for part in pending:
        by_post.setdefault(part["post_id"], []).append(part)

    for post_id, parts in by_post.items():
        head = parts[0]
        with st.container(border=True):
            st.markdown(f"**{head['title']}**")
            st.caption(f"r/{head['subreddit']} · {head['permalink']}")

            # Four previews to a row keeps each one wide enough to judge.
            for row_start in range(0, len(parts), 4):
                row = parts[row_start:row_start + 4]
                for column, part in zip(st.columns(len(row)), row):
                    with column:
                        st.caption(f"Part {part['index']}/{part['total']}")
                        video_path = part.get("video_path")
                        if video_path and os.path.exists(video_path):
                            st.video(video_path)
                        else:
                            st.warning("Video file missing")

            actions = st.columns([1, 1, 4])
            with actions[0]:
                if st.button("Approve", key=f"approve_{post_id}", type="primary"):
                    reddit_queue.approve_post(post_id)
                    st.rerun()
            with actions[1]:
                if st.button("Reject", key=f"reject_{post_id}"):
                    reddit_queue.reject_post(post_id)
                    st.rerun()

    if len(by_post) > 1 and st.button("Approve all"):
        reddit_queue.approve_all()
        st.rerun()


# -----------------------------------------------------------------------------
# Schedule
# -----------------------------------------------------------------------------


def _render_schedule() -> None:
    st.subheader("Schedule")

    approved = reddit_queue.approved_parts()
    scheduled = reddit_queue.parts_with_status(reddit_queue.STATUS_SCHEDULED)

    if not upload_post.upload_post_service.is_configured():
        st.info("Configure Upload-Post in the main settings dialog to schedule publishing.")
        return

    if not approved:
        st.caption("Approve something above to schedule it.")
    else:
        col_date, col_time, col_interval = st.columns(3)
        with col_date:
            start_date = st.date_input(
                "First slot date (UTC)",
                value=(datetime.now(timezone.utc) + timedelta(hours=1)).date(),
                key="reddit_start_date",
            )
        with col_time:
            start_time = st.time_input(
                "First slot time (UTC)",
                value=dt_time(
                    (datetime.now(timezone.utc) + timedelta(hours=1)).hour, 0
                ),
                key="reddit_start_time",
                help="Times are UTC, not your local time zone.",
            )
        with col_interval:
            interval_hours = st.number_input(
                "Hours between parts",
                min_value=0.5, max_value=168.0, step=0.5,
                value=float(config.app.get("reddit_publish_interval_hours", 8)),
                key="reddit_interval_hours",
            )
        _set_config("reddit_publish_interval_hours", float(interval_hours))

        start = datetime.combine(start_date, start_time).replace(tzinfo=timezone.utc)
        slots = [start + timedelta(hours=interval_hours * i) for i in range(len(approved))]

        st.caption(
            f"{len(approved)} parts, in story order so part 1 always lands first."
        )
        st.dataframe(
            [
                {
                    "When": slot.strftime("%Y-%m-%d %H:%M UTC"),
                    "Story": part["title"][:60],
                    "Part": f"{part['index']}/{part['total']}",
                }
                for part, slot in zip(approved, slots)
            ],
            use_container_width=True,
            hide_index=True,
        )

        platforms = list(upload_post.upload_post_service.platforms)
        st.caption(f"Publishing to {', '.join(platforms) or '—'}")

        if st.button("Schedule uploads", type="primary", disabled=not platforms):
            _schedule(approved, slots, platforms)

    if scheduled:
        st.divider()
        st.caption(f"{len(scheduled)} parts already scheduled")
        st.dataframe(
            [
                {
                    "When": part.get("scheduled_for") or "—",
                    "Story": part["title"][:60],
                    "Part": f"{part['index']}/{part['total']}",
                    "Job": part.get("job_id") or "—",
                }
                for part in scheduled
            ],
            use_container_width=True,
            hide_index=True,
        )


def _caption_for(part: dict) -> str:
    template = str(
        config.app.get(
            "reddit_caption_template", "{title} (Part {index}/{total}) #reddit #story"
        )
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
        return str(part.get("subject", ""))[:2200]


def _schedule(approved: list[dict], slots: list[datetime], platforms: list[str]) -> None:
    scheduled = 0
    failed = 0
    progress = st.progress(0.0)

    for position, (part, slot) in enumerate(zip(approved, slots), start=1):
        progress.progress(position / max(len(approved), 1))

        video_path = part.get("video_path")
        if not video_path or not os.path.exists(video_path):
            reddit_queue.update_part(
                part["post_id"], part["index"],
                status=reddit_queue.STATUS_FAILED,
                error="video file missing at schedule time",
            )
            failed += 1
            continue

        iso_slot = slot.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        caption = _caption_for(part)
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
                part["post_id"], part["index"],
                status=reddit_queue.STATUS_SCHEDULED,
                job_id=str(job_id),
                scheduled_for=iso_slot,
                error=None,
            )
            scheduled += 1
        else:
            error = "invalid Upload-Post response"
            if isinstance(result, dict):
                error = result.get("error") or result.get("message") or error
            reddit_queue.update_part(
                part["post_id"], part["index"],
                status=reddit_queue.STATUS_FAILED,
                error=str(error),
            )
            failed += 1

    if failed:
        st.warning(
            f"{scheduled} scheduled, {failed} failed."
        )
    else:
        st.success(f"{scheduled} parts scheduled.")
    st.rerun()


# -----------------------------------------------------------------------------


def main() -> None:
    # The sidebar that normally carries page navigation is unreachable on a
    # phone, so the way back has to live in the page body too.
    st.page_link("Main.py", label="Back to video generation", icon=":material/arrow_back:")
    st.title("Reddit Recaps")
    st.caption("Find Reddit stories, render them as Shorts-sized parts, review what came out, then schedule the approved ones.")

    ready = _render_setup()
    options = _render_options()

    summary = reddit_queue.summary()
    counts = summary["parts"]
    metrics = st.columns(5)
    for column, (label, value) in zip(
        metrics,
        [
            ("Rendering", counts.get(reddit_queue.STATUS_RENDERING, 0)),
            ("To review", counts.get(reddit_queue.STATUS_RENDERED, 0)),
            ("Approved", counts.get(reddit_queue.STATUS_APPROVED, 0)),
            ("Scheduled", counts.get(reddit_queue.STATUS_SCHEDULED, 0)),
            ("Failed", counts.get(reddit_queue.STATUS_FAILED, 0)),
        ],
    ):
        column.metric(label, value)

    st.divider()
    if ready:
        _render_find(options)
    st.divider()
    _render_review()
    st.divider()
    _render_schedule()


main()
