"""
Reddit Recaps — the whole pipeline as a page.

Find stories, render them into Shorts-sized parts, review what came out, hand
the approved parts to Upload-Post on a schedule, and keep a record of every
story the pipeline has ever touched.

Every long step runs on the server, in a background thread owned by the
process, and reports through ``app/services/reddit_jobs.py``. This page only
reads state: nothing that matters lives in ``st.session_state``, so a refresh,
a dropped websocket or a second tab all show the same thing, and closing the
browser does not stop the work.

Kept as a separate page rather than folded into Main.py on purpose: Main.py is
the file upstream changes most, and a new file never conflicts on merge. The
shared work lives in app/services/reddit_pipeline.py, so this page and
scripts/reddit_recap.py cannot drift apart.
"""

import os
import re
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
    gameplay_fetch,
    gameplay_library,
    material_upload,
    reddit_jobs,
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

_LISTING_HELP = "top uses the time window below; hot, new and rising ignore it."
_BACKGROUND_LABELS = {
    reddit_pipeline.BACKGROUND_GAMEPLAY: "Gameplay footage (Minecraft parkour)",
    "pexels": "Pexels stock video",
    "pixabay": "Pixabay stock video",
    "coverr": "Coverr stock video",
}
_THICKNESS_LABELS = {
    "thin": "Thin",
    "medium": "Medium",
    "thick": "Thick",
    "extra thick": "Extra thick",
}
# Only polled while the server actually has work in flight; an idle page falls
# back to a static render and stops asking.
_LIVE_REFRESH_SECONDS = "2s"
# The backlog is permanent, so it grows without bound on a server that searches
# on a schedule. Rendering every entry would make the tab unusable long before
# anyone scrolled that far.
_BACKLOG_PAGE_SIZE = 50

_STAGE_LABELS = {
    reddit_queue.STATUS_DISCOVERED: "In the backlog",
    reddit_queue.STATUS_ARCHIVED: "Archived",
    reddit_queue.STATUS_RENDERING: "Rendering",
    reddit_queue.STATUS_RENDERED: "Waiting for review",
    reddit_queue.STATUS_APPROVED: "Approved",
    reddit_queue.STATUS_SCHEDULED: "Scheduled",
    reddit_queue.STATUS_UPLOADED: "Published",
    reddit_queue.STATUS_FAILED: "Failed",
    reddit_queue.STATUS_REJECTED: "Rejected",
}
_STAGE_ICONS = {
    reddit_queue.STATUS_DISCOVERED: "📥",
    reddit_queue.STATUS_ARCHIVED: "📦",
    reddit_queue.STATUS_RENDERING: "⏳",
    reddit_queue.STATUS_RENDERED: "👀",
    reddit_queue.STATUS_APPROVED: "✅",
    reddit_queue.STATUS_SCHEDULED: "🗓️",
    reddit_queue.STATUS_UPLOADED: "🚀",
    reddit_queue.STATUS_FAILED: "⚠️",
    reddit_queue.STATUS_REJECTED: "🗑️",
}


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


def _stage_label(status: str) -> str:
    return f"{_STAGE_ICONS.get(status, '•')} {_STAGE_LABELS.get(status, status)}"


def _ago(timestamp) -> str:
    """A finished-at stamp as something a human reads without doing subtraction."""
    if not timestamp:
        return "—"
    seconds = max(0, int(datetime.now(timezone.utc).timestamp() - float(timestamp)))
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return datetime.fromtimestamp(float(timestamp), timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )


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
# Background footage
# -----------------------------------------------------------------------------


def _preview_subtitle(color: str, thickness: str, font_size: int) -> None:
    """
    A rough sample of the caption over dark footage.

    Approximate on purpose — the browser is not MoviePy — but it answers the
    only question the two controls raise: is this readable over gameplay.
    """
    stroke = reddit_pipeline.TEXT_THICKNESS.get(
        thickness, reddit_pipeline.TEXT_THICKNESS[reddit_pipeline.DEFAULT_TEXT_THICKNESS]
    )
    # The colour reaches inline HTML, and config.toml can be edited by hand.
    # Anything that is not a hex colour is not one worth previewing.
    if not re.fullmatch(r"#[0-9A-Fa-f]{3,8}", str(color or "")):
        color = reddit_pipeline.DEFAULT_TEXT_COLOR
    # The video is 1080 wide and this strip is a few hundred pixels, so both
    # numbers are halved to keep the proportions honest.
    st.markdown(
        f"""
        <div style="background:#141414;border-radius:8px;padding:16px;text-align:center;">
          <span style="font-family:system-ui,sans-serif;font-weight:800;
                       font-size:{max(16, font_size // 2)}px;color:{color};
                       -webkit-text-stroke:{max(1, stroke // 2)}px {reddit_pipeline.DEFAULT_STROKE_COLOR};
                       paint-order:stroke fill;">AND THEN SHE SAID WHAT?</span>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.caption("Approximate preview of the subtitle style.")


def _render_background_library(background: str) -> None:
    """Manage the clips that play behind the narration."""
    if background != reddit_pipeline.BACKGROUND_GAMEPLAY:
        return

    library = gameplay_library.clips()
    with st.expander(
        f"Gameplay clips ({len(library)})", expanded=not library
    ):
        st.caption(
            "Each part plays over one of these clips. Minecraft parkour is the "
            "usual choice; anything long and visually busy works. Clips stay on "
            "the server and are reused by every render."
        )
        st.caption(
            f"Too big to upload? Drop files straight into `{gameplay_library.library_dir(create=False)}` "
            "on the server — they show up here."
        )

        _render_clip_download()

        with st.form("reddit_gameplay_upload", clear_on_submit=True):
            uploaded = st.file_uploader(
                "Add clips",
                type=[ext.lstrip(".") for ext in gameplay_library.SUPPORTED_EXTENSIONS],
                accept_multiple_files=True,
                key="reddit_gameplay_uploader",
            )
            if st.form_submit_button("Add to library", type="primary") and uploaded:
                _add_gameplay_clips(uploaded)

        if not library:
            st.warning("The library is empty, so nothing can render yet.")
            return

        for clip in library:
            row = st.columns([4, 1, 1])
            row[0].write(clip["display_name"])
            row[1].caption(f"{clip['size_bytes'] / (1024 * 1024):.0f} MB")
            if row[2].button("Remove", key=f"gameplay_remove_{clip['name']}"):
                gameplay_library.remove_clip(clip["name"])
                st.rerun()


def _render_clip_download() -> None:
    """
    Fill the library from YouTube, as a background job.

    Twenty clips is around twenty minutes of downloading, which is far past
    what a page script can hold open — so this only ever starts the job and
    reads its state back, the same as every other long step on this page.
    """
    if not gameplay_fetch.is_available():
        st.info(gameplay_fetch.unavailable_reason())
        return

    running = reddit_jobs.is_running(reddit_jobs.JOB_FETCH_CLIPS)

    row = st.columns([2, 1, 1])
    with row[0]:
        query = st.text_input(
            "Search YouTube",
            value=str(
                config.app.get("reddit_gameplay_query", gameplay_fetch.DEFAULT_QUERY)
            ),
            key="reddit_gameplay_query_input",
        )
    with row[1]:
        count = st.number_input(
            "How many",
            min_value=1,
            max_value=50,
            step=1,
            value=int(
                config.app.get("reddit_gameplay_count", gameplay_fetch.DEFAULT_COUNT)
            ),
            key="reddit_gameplay_count_input",
        )
    with row[2]:
        # Lines the button up with the inputs beside it rather than the labels.
        st.write("")
        if st.button(
            "Downloading…" if running else "Download",
            type="primary",
            use_container_width=True,
            disabled=running,
            key="reddit_gameplay_fetch_button",
        ):
            _set_config("reddit_gameplay_query", query)
            _set_config("reddit_gameplay_count", int(count))
            reddit_jobs.start_clip_fetch(query, int(count))
            st.rerun()

    st.caption(
        f"Downloads about {gameplay_fetch.DEFAULT_SEGMENT_SECONDS} seconds from each "
        "result — enough footage for any one part, without pulling two-hour "
        "uploads onto the server. Clips already in the library are skipped, so "
        "this tops up rather than duplicating. Roughly 90 MB per clip."
    )

    job = reddit_jobs.get_job(reddit_jobs.JOB_FETCH_CLIPS)
    if job and job["status"] == reddit_jobs.STATUS_FAILED:
        st.error(f"Last download failed {_ago(job['finished_at'])}: {job['error']}")
    elif job and job["status"] == reddit_jobs.STATUS_COMPLETED:
        result = job["result"]
        st.caption(
            f"Last download {_ago(job['finished_at'])} · "
            f"{result.get('downloaded', 0)} added · "
            f"{result.get('skipped', 0)} already held · "
            f"{result.get('failed', 0)} failed"
        )
        if result.get("error"):
            st.info(result["error"])


def _add_gameplay_clips(uploaded) -> None:
    added = 0
    for upload in uploaded:
        try:
            gameplay_library.add_clip(upload.name, upload)
            added += 1
        except material_upload.MaterialUploadError as exc:
            st.error(f"{upload.name}: {exc}")
        except material_upload.MaterialServiceError as exc:
            logger.exception(f"failed to store gameplay clip {upload.name}: {exc}")
            st.error(f"{upload.name} could not be stored on the server.")

    if added:
        st.success(f"{added} clips added.")
        st.rerun()


# -----------------------------------------------------------------------------
# Options
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
            backgrounds = list(reddit_pipeline.BACKGROUNDS)
            background = st.selectbox(
                "Background",
                options=backgrounds,
                index=backgrounds.index(
                    config.app.get(
                        "reddit_background", reddit_pipeline.DEFAULT_BACKGROUND
                    )
                    if config.app.get("reddit_background") in backgrounds
                    else reddit_pipeline.DEFAULT_BACKGROUND
                ),
                format_func=lambda value: _BACKGROUND_LABELS[value],
                help="Gameplay plays clips from the library below. The stock sources search for the material terms instead.",
                key="reddit_background_input",
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
                help="Only used by the stock sources. Story recaps use filler b-roll, so generic calm footage reads better than terms from the story.",
                disabled=background == reddit_pipeline.BACKGROUND_GAMEPLAY,
                key="reddit_video_terms_input",
            )
            caption_template = st.text_input(
                "Publish caption",
                value=config.app.get(
                    "reddit_caption_template",
                    reddit_pipeline.DEFAULT_CAPTION_TEMPLATE,
                ),
                help="Placeholders: {title}, {subreddit}, {index}, {total}.",
                key="reddit_caption_template_input",
            )

        st.markdown("**Subtitles**")
        col_g, col_h, col_i = st.columns(3)
        with col_g:
            text_color = st.color_picker(
                "Text colour",
                value=str(
                    config.app.get(
                        "reddit_text_color", reddit_pipeline.DEFAULT_TEXT_COLOR
                    )
                ),
                key="reddit_text_color_input",
            )
        with col_h:
            thickness_values = list(reddit_pipeline.TEXT_THICKNESS)
            current_thickness = str(
                config.app.get(
                    "reddit_text_thickness", reddit_pipeline.DEFAULT_TEXT_THICKNESS
                )
            ).lower()
            text_thickness = st.selectbox(
                "Text thickness",
                options=thickness_values,
                index=thickness_values.index(
                    current_thickness
                    if current_thickness in thickness_values
                    else reddit_pipeline.DEFAULT_TEXT_THICKNESS
                ),
                format_func=lambda value: _THICKNESS_LABELS[value],
                help="How heavy the outline around each word is. Thicker stays readable over busy gameplay footage.",
                key="reddit_text_thickness_input",
            )
        with col_i:
            font_size = st.number_input(
                "Text size",
                min_value=30, max_value=120, step=2,
                value=int(
                    config.app.get(
                        "reddit_font_size", reddit_pipeline.DEFAULT_FONT_SIZE
                    )
                ),
                key="reddit_font_size_input",
            )
        _preview_subtitle(text_color, text_thickness, int(font_size))

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
        "reddit_background": background,
        "reddit_subtitle_enabled": bool(subtitle_enabled),
        "reddit_text_color": text_color,
        "reddit_text_thickness": text_thickness,
        "reddit_font_size": int(font_size),
        "reddit_voice_name": voice_name,
        "reddit_video_terms": video_terms,
        "reddit_caption_template": caption_template,
    }.items():
        _set_config(key, value)

    return reddit_pipeline.resolve_options()


# -----------------------------------------------------------------------------
# What the server is doing right now
# -----------------------------------------------------------------------------


def _work_in_flight() -> bool:
    return bool(
        reddit_jobs.is_running(reddit_jobs.JOB_DISCOVER)
        or reddit_jobs.is_running(reddit_jobs.JOB_SCHEDULE)
        or reddit_jobs.is_running(reddit_jobs.JOB_FETCH_CLIPS)
        or reddit_queue.parts_with_status(reddit_queue.STATUS_RENDERING)
    )


def _render_activity(live: bool) -> None:
    """
    The single place that says what the server is busy with.

    Shown above the tabs because the work outlives whichever tab started it:
    a render kicked off under "Find" finishes while the user is reading
    "All stories".
    """
    discover_job = reddit_jobs.get_job(reddit_jobs.JOB_DISCOVER)
    schedule_job = reddit_jobs.get_job(reddit_jobs.JOB_SCHEDULE)
    rendering = reddit_queue.parts_with_status(reddit_queue.STATUS_RENDERING)

    busy = False

    if discover_job and discover_job["status"] == reddit_jobs.STATUS_RUNNING:
        busy = True
        st.progress(
            max(discover_job["progress"], 5) / 100,
            text=f"Finding stories · {discover_job['message'] or 'working'}",
        )

    if schedule_job and schedule_job["status"] == reddit_jobs.STATUS_RUNNING:
        busy = True
        st.progress(
            max(schedule_job["progress"], 5) / 100,
            text=f"Scheduling uploads · {schedule_job['message'] or 'working'}",
        )

    clips_job = reddit_jobs.get_job(reddit_jobs.JOB_FETCH_CLIPS)
    if clips_job and clips_job["status"] == reddit_jobs.STATUS_RUNNING:
        busy = True
        st.progress(
            max(clips_job["progress"], 5) / 100,
            text=f"Downloading gameplay clips · {clips_job['message'] or 'working'}",
        )

    for part in rendering:
        busy = True
        task = sm.state.get_task(part["task_id"]) or {}
        st.progress(
            min(int(task.get("progress", 0) or 0), 100) / 100,
            text=(
                f"Rendering · {part['title'][:60]} "
                f"part {part['index']}/{part['total']}"
            ),
        )

    if not busy:
        if live:
            # Everything finished since the last poll: refresh the whole page so
            # the results appear and this stops polling.
            st.rerun(scope="app")
        st.caption(
            "The server is idle. Anything you start keeps running here even "
            "if you close this page."
        )


@st.fragment(run_every=_LIVE_REFRESH_SECONDS)
def _render_activity_live() -> None:
    _render_activity(live=True)


# -----------------------------------------------------------------------------
# Find stories
# -----------------------------------------------------------------------------


def _render_last_discovery(job: dict | None) -> None:
    """Why the last run produced what it did, kept visible after it finished."""
    if not job:
        st.caption("No search has run yet.")
        return

    if job["status"] == reddit_jobs.STATUS_RUNNING:
        st.info("Searching on the server. This keeps running if you leave the page.")
        return

    if job["status"] == reddit_jobs.STATUS_FAILED:
        st.error(f"Last search failed {_ago(job['finished_at'])}: {job['error']}")
        return

    result = job["result"]
    fetched = int(result.get("fetched", 0) or 0)
    matched = int(result.get("matched", 0) or 0)
    added = int(result.get("added", 0) or 0)

    st.caption(
        f"Last search {_ago(job['finished_at'])} · {fetched} posts fetched · "
        f"{matched} new and past the filters · {added} added to the backlog"
    )

    if added:
        return

    # An empty result is the confusing case, so say which step emptied it.
    if not fetched:
        st.warning(
            "Nothing came back from Reddit. Check the credentials above, the "
            "subreddit names, and whether the server can reach the network."
        )
    elif not matched:
        st.info(
            "Posts were fetched but none of them were new and past the filters. "
            "Lower the minimum score or word count, widen the time window, or "
            "note that a story is only ever offered once — anything already in "
            "the backlog, the library or the archive is skipped."
        )
    else:
        skipped = int(result.get("skipped_truncated", 0) or 0)
        empty = int(result.get("skipped_empty", 0) or 0)
        st.info(
            f"Every matching story was dropped while splitting "
            f"({skipped} too long for the maximum parts, {empty} with nothing "
            f"narratable). Raise the maximum parts or turn off 'Skip overlong "
            f"stories'."
        )


def _render_find(options: dict, ready: bool) -> None:
    st.subheader("Find stories")

    running = reddit_jobs.is_running(reddit_jobs.JOB_DISCOVER)
    col_button, col_note = st.columns([1, 3])
    with col_button:
        if st.button(
            "Searching…" if running else "Find stories",
            type="primary",
            use_container_width=True,
            disabled=running or not ready,
            key="reddit_find_button",
        ):
            reddit_jobs.start_discovery(options)
            st.rerun()
    with col_note:
        st.caption(
            "Runs as a background task on the server: it fetches the listings, "
            "drops every story it has seen before, splits what is left into "
            "parts and files them in the backlog below. Nothing renders yet, "
            "and a story only ever appears here once."
        )

    _render_last_discovery(reddit_jobs.get_job(reddit_jobs.JOB_DISCOVER))

    st.divider()
    _render_backlog(options)
    _render_archive()


def _story_headline(post: dict) -> str:
    parts = post.get("parts") or []
    seconds = sum(
        part["estimated_seconds"]
        for part in parts
        if isinstance(part.get("estimated_seconds"), (int, float))
    )
    return (
        f"r/{post['subreddit']} · {post['score']:,} · {len(parts)} parts · "
        f"{seconds:.0f}s — {post['title'][:80]}"
    )


def _render_story_scripts(post: dict) -> None:
    for part in post.get("parts") or []:
        seconds = part.get("estimated_seconds")
        length = f" · {seconds:.0f}s" if isinstance(seconds, (int, float)) else ""
        with st.expander(
            f"Part {part['index']}/{part['total']}{length}", expanded=False
        ):
            st.write(part.get("script", ""))


def _promote(post_ids: list[str], options: dict) -> None:
    """Render backlog stories, which is what puts them up for review."""
    def submit(task_id, params, part, split):
        webui_task.submit_generation(
            task_id=task_id,
            params=params,
            capture_logs=not config.ui.get("hide_log", False),
        )

    result = reddit_pipeline.promote_posts(post_ids, options, submit)

    if result["failed"]:
        st.warning(
            f"{result['submitted']} parts queued, "
            f"{result['failed']} could not start."
        )
    else:
        st.success(
            f"{result['posts']} stories promoted, "
            f"{result['submitted']} parts queued for rendering. "
            "They move to Review as each render finishes."
        )
    st.rerun()


def _render_backlog(options: dict) -> None:
    st.markdown("#### Backlog")

    posts = reddit_queue.backlog()
    if not posts:
        st.caption(
            "No story is waiting. Search above — anything found is kept here "
            "until you promote it to review or archive it."
        )
        return

    background_issues = reddit_pipeline.background_issues(options)
    for issue in background_issues:
        st.warning(issue)

    shown = posts[:_BACKLOG_PAGE_SIZE]
    if len(posts) > len(shown):
        st.caption(
            f"{len(posts)} stories waiting; showing the {len(shown)} most "
            "recent. Archive what you do not want to keep the list short."
        )
    else:
        st.caption(
            f"{len(posts)} stories waiting. Promoting one renders its parts and "
            "sends them to Review; archiving keeps the text and never offers "
            "the story again."
        )

    for post in shown:
        post_id = post["post_id"]
        with st.container(border=True):
            st.markdown(f"**{_story_headline(post)}**")
            st.caption(f"Found {_ago(post['created_at'])} · {post['permalink']}")
            _render_story_scripts(post)

            actions = st.columns([1, 1, 3])
            with actions[0]:
                if st.button(
                    "Promote to review",
                    key=f"promote_{post_id}",
                    type="primary",
                    disabled=bool(background_issues),
                    use_container_width=True,
                ):
                    _promote([post_id], options)
            with actions[1]:
                if st.button(
                    "Archive",
                    key=f"archive_{post_id}",
                    use_container_width=True,
                ):
                    reddit_queue.archive_post(post_id)
                    st.rerun()

    st.divider()
    bulk = st.columns([1, 1, 2])
    with bulk[0]:
        if st.button(
            f"Promote all {len(posts)}",
            key="reddit_promote_all",
            disabled=bool(background_issues),
            use_container_width=True,
        ):
            # Every waiting story, not just the page on screen: the button says
            # "all" and rendering is what the user came for.
            _promote([post["post_id"] for post in posts], options)
    with bulk[1]:
        if st.button(
            f"Archive all {len(posts)}",
            key="reddit_archive_all",
            use_container_width=True,
        ):
            for post in posts:
                reddit_queue.archive_post(post["post_id"])
            st.rerun()


def _render_archive() -> None:
    """
    Archived stories, with a way back.

    Archiving is one click on a long list, so it cannot be the kind of thing
    that needs a JSON editor to undo.
    """
    archived = reddit_queue.archived_posts()
    if not archived:
        return

    with st.expander(f"Archived ({len(archived)})", expanded=False):
        st.caption(
            "Set aside and never offered again. The text is kept, so an "
            "archived story can still be read here or put back."
        )
        for post in archived[:_BACKLOG_PAGE_SIZE]:
            row = st.columns([4, 1])
            row[0].caption(_story_headline(post))
            if row[1].button(
                "Restore",
                key=f"restore_{post['post_id']}",
                use_container_width=True,
            ):
                reddit_queue.restore_post(post["post_id"])
                st.rerun()


# -----------------------------------------------------------------------------
# Review
# -----------------------------------------------------------------------------


def _group_by_post(parts: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for part in parts:
        grouped.setdefault(part["post_id"], []).append(part)
    return grouped


def _render_review() -> None:
    rendering = reddit_queue.parts_with_status(reddit_queue.STATUS_RENDERING)
    pending = reddit_queue.pending_review()

    header = st.columns([3, 1])
    with header[0]:
        st.subheader("Review")
    with header[1]:
        if st.button("Refresh", use_container_width=True, key="reddit_review_refresh"):
            reddit_jobs.refresh_now()
            st.rerun()

    discard_on_reject = st.checkbox(
        "Rejecting stops the story and deletes its videos",
        value=bool(config.app.get("reddit_reject_discards_videos", True)),
        help=(
            "The story, its link and the narration of every part stay in All "
            "stories — only the footage goes. Parts still rendering are stopped "
            "from reaching the queue, and their file is deleted once the render "
            "finishes. Turn this off to keep rejected videos on disk."
        ),
        key="reddit_reject_discards_input",
    )
    _set_config("reddit_reject_discards_videos", bool(discard_on_reject))

    if rendering:
        st.info(
            f"{len(rendering)} parts still rendering. They appear here on their "
            "own — the server promotes them as each render finishes."
        )
        for post_id, parts in _group_by_post(rendering).items():
            row = st.columns([4, 1])
            row[0].caption(
                f"{parts[0]['title'][:70]} · {len(parts)} parts rendering"
            )
            if row[1].button("Stop", key=f"stop_{post_id}", use_container_width=True):
                # Stopping is rejecting with the footage discarded: there is no
                # cancel in the task pool, so the render finishes into the bin.
                reddit_queue.reject_post(post_id, discard_video=True)
                st.rerun()

    if not pending:
        if not rendering:
            st.caption("Nothing waiting for review.")
        return

    by_post = _group_by_post(pending)

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
                    reddit_queue.reject_post(
                        post_id, discard_video=discard_on_reject
                    )
                    st.rerun()

    if len(by_post) > 1 and st.button("Approve all", key="reddit_approve_all"):
        reddit_queue.approve_all()
        st.rerun()


# -----------------------------------------------------------------------------
# Schedule
# -----------------------------------------------------------------------------


def _render_schedule() -> None:
    st.subheader("Schedule")

    approved = reddit_queue.approved_parts()
    scheduled = reddit_queue.parts_with_status(reddit_queue.STATUS_SCHEDULED)
    job = reddit_jobs.get_job(reddit_jobs.JOB_SCHEDULE)
    running = bool(job and job["status"] == reddit_jobs.STATUS_RUNNING)

    if job and job["status"] == reddit_jobs.STATUS_FAILED:
        st.error(f"Last scheduling run failed {_ago(job['finished_at'])}: {job['error']}")
    elif job and job["status"] == reddit_jobs.STATUS_COMPLETED and job["message"]:
        st.caption(f"Last scheduling run {_ago(job['finished_at'])}: {job['message']}")
        for error in (job["result"].get("errors") or [])[:10]:
            st.warning(error)

    if not upload_post.upload_post_service.is_configured():
        st.info("Configure Upload-Post in the main settings dialog to schedule publishing.")
        return

    if not approved:
        st.caption("Approve something in Review to schedule it.")
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
                    "Caption": reddit_pipeline.caption_for(part)[:70],
                }
                for part, slot in zip(approved, slots)
            ],
            use_container_width=True,
            hide_index=True,
        )

        platforms = list(upload_post.upload_post_service.platforms)
        st.caption(f"Publishing to {', '.join(platforms) or '—'}")

        if st.button(
            "Scheduling…" if running else "Schedule uploads",
            type="primary",
            disabled=running or not platforms,
            key="reddit_schedule_button",
        ):
            # Uploading a video file takes minutes per part, so this cannot run
            # inside the page script: the browser would sit on a spinner and a
            # closed tab would abandon it half way through the batch.
            reddit_jobs.start_scheduling(
                approved,
                [
                    slot.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                    for slot in slots
                ],
                platforms,
            )
            st.rerun()

    if scheduled:
        st.divider()
        head = st.columns([3, 1])
        with head[0]:
            st.caption(
                f"{len(scheduled)} parts scheduled. The server checks Upload-Post "
                "on its own and marks them published when they go out."
            )
        with head[1]:
            if st.button(
                "Check now", use_container_width=True, key="reddit_check_uploads"
            ):
                reddit_jobs.refresh_now()
                st.rerun()
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


# -----------------------------------------------------------------------------
# All stories
# -----------------------------------------------------------------------------


def _part_row(part: dict) -> dict:
    seconds = part.get("estimated_seconds")
    return {
        "Part": f"{part['index']}/{part['total']}",
        "State": _stage_label(part["status"]),
        "Length": f"{seconds:.0f}s" if isinstance(seconds, (int, float)) else "—",
        "Scheduled for": part.get("scheduled_for") or "—",
        "Upload job": part.get("job_id") or "—",
        "Video": os.path.basename(part.get("video_path") or "") or "—",
        "Problem": part.get("error") or "",
    }


def _render_library() -> None:
    header = st.columns([3, 1])
    with header[0]:
        st.subheader("All stories")
    with header[1]:
        if st.button("Refresh", use_container_width=True, key="reddit_library_refresh"):
            reddit_jobs.refresh_now()
            st.rerun()

    posts = reddit_queue.all_posts()
    if not posts:
        st.caption(
            "Nothing yet. Every story the pipeline picks up shows here with what "
            "happened to it — rendered, reviewed, scheduled and published."
        )
        return

    options = ["all", *reddit_queue.PART_STATUSES]
    chosen = st.selectbox(
        "Show",
        options=options,
        format_func=lambda value: "Everything" if value == "all" else _stage_label(value),
        key="reddit_library_filter",
    )
    if chosen != "all":
        posts = [
            post
            for post in posts
            if any(part["status"] == chosen for part in post["parts"])
        ]
        if not posts:
            st.caption("No story is in that state.")
            return

    st.dataframe(
        [
            {
                "Found": datetime.fromtimestamp(
                    post["created_at"], timezone.utc
                ).strftime("%Y-%m-%d %H:%M"),
                "Story": post["title"][:70],
                "Subreddit": f"r/{post['subreddit']}",
                "Score": post["score"],
                "Parts": len(post["parts"]),
                "State": _stage_label(reddit_queue.post_stage(post)),
                "Published": reddit_queue.post_counts(post)[
                    reddit_queue.STATUS_UPLOADED
                ],
            }
            for post in posts
        ],
        use_container_width=True,
        hide_index=True,
    )

    st.caption("Open a story for its parts, upload jobs and any errors.")
    for post in posts:
        counts = reddit_queue.post_counts(post)
        breakdown = " · ".join(
            f"{count} {_STAGE_LABELS[status].lower()}"
            for status, count in counts.items()
            if count
        )
        with st.expander(
            f"{_stage_label(reddit_queue.post_stage(post))} — {post['title'][:80]} "
            f"({breakdown})",
            expanded=False,
        ):
            st.caption(f"r/{post['subreddit']} · {post['permalink']}")
            st.dataframe(
                [_part_row(part) for part in post["parts"]],
                use_container_width=True,
                hide_index=True,
            )

            scripts = [part for part in post["parts"] if part.get("script")]
            if scripts:
                st.markdown("**Story text**")
                st.caption(
                    "Kept whatever happens to the video, so a rejected story "
                    "can still be read or re-used."
                )
                for part in scripts:
                    st.caption(f"Part {part['index']}/{part['total']}")
                    st.write(part["script"])


# -----------------------------------------------------------------------------


def _render_metrics() -> None:
    counts = reddit_queue.summary()["parts"]
    metrics = st.columns(7)
    for column, (label, status) in zip(
        metrics,
        [
            ("In backlog", reddit_queue.STATUS_DISCOVERED),
            ("Rendering", reddit_queue.STATUS_RENDERING),
            ("To review", reddit_queue.STATUS_RENDERED),
            ("Approved", reddit_queue.STATUS_APPROVED),
            ("Scheduled", reddit_queue.STATUS_SCHEDULED),
            ("Published", reddit_queue.STATUS_UPLOADED),
            ("Failed", reddit_queue.STATUS_FAILED),
        ],
    ):
        column.metric(label, counts.get(status, 0))


def main() -> None:
    # The sidebar that normally carries page navigation is unreachable on a
    # phone, so the way back has to live in the page body too.
    st.page_link("Main.py", label="Back to video generation", icon=":material/arrow_back:")
    st.title("Reddit Recaps")
    st.caption("Find Reddit stories, render them as Shorts-sized parts, review what came out, schedule the approved ones, and watch the whole thing from the library below.")

    # Starts the process-wide worker that promotes finished renders and
    # published uploads. Idempotent, so every page load is free, and the thread
    # outlives the browser session that happened to start it.
    try:
        reddit_jobs.ensure_worker()
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception(f"could not start the reddit recap worker: {exc}")
        st.warning(
            "The background worker could not start, so renders and uploads "
            "will only advance while this page is open."
        )

    ready = _render_setup()
    options = _render_options()
    _render_background_library(options["background"])

    _render_metrics()

    if _work_in_flight():
        _render_activity_live()
    else:
        _render_activity(live=False)

    st.divider()

    tab_find, tab_review, tab_schedule, tab_library = st.tabs(
        ["1 · Find", "2 · Review", "3 · Schedule", "4 · All stories"]
    )
    with tab_find:
        _render_find(options, ready)
    with tab_review:
        _render_review()
    with tab_schedule:
        _render_schedule()
    with tab_library:
        _render_library()


main()
