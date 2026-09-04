"""
Reddit ingestion via the Apify actor ``trudax/reddit-scraper-lite``.

An alternative to the official API in ``reddit_source``. It needs no Reddit app
registration and is not subject to Reddit's per-client rate limit, which makes
it the easier path from a server. It is pay-per-result, so ``max_items`` is a
cost control, not just a size limit.

The actor is driven with ``startUrls`` pointing at listing pages rather than its
``searches`` input: a listing URL carries its own sort and time window, so what
comes back matches what the same URL shows in a browser.

Posts are normalised into the exact shape ``reddit_source`` produces, so
filtering, splitting, the queue and the UI neither know nor care which backend
fetched them.

Actor: https://apify.com/trudax/reddit-scraper-lite
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import requests
from loguru import logger

from app.config import config


API_BASE = "https://api.apify.com/v2"
DEFAULT_ACTOR_ID = "oAuCIx3ItNrs2okjQ"  # trudax/reddit-scraper-lite

DEFAULT_TIMEOUT_SECONDS = 300
_REQUEST_TIMEOUT = (5, 30)
_POLL_INTERVAL_SECONDS = 3
_TERMINAL_STATUSES = ("SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED")

# The actor emits posts, comments, communities and users into one dataset.
_POST_DATA_TYPE = "post"


def _setting(key: str, default=None):
    return config.app.get(f"reddit_{key}", default)


def _get_tls_verify() -> bool:
    tls_verify = config.app.get("tls_verify", True)
    if isinstance(tls_verify, str):
        tls_verify = tls_verify.strip().lower() not in ("0", "false", "no", "off")
    return bool(tls_verify)


def _token() -> str:
    return str(_setting("apify_token", "") or "").strip()


def _actor_id() -> str:
    return str(_setting("apify_actor_id", "") or "").strip() or DEFAULT_ACTOR_ID


def is_configured() -> bool:
    return bool(_token())


def _headers() -> dict:
    # Sent as a header rather than a query parameter so the token cannot leak
    # into a logged URL.
    return {
        "Authorization": f"Bearer {_token()}",
        "Content-Type": "application/json",
    }


def _listing_url(subreddit: str, listing: str, time_filter: str) -> str:
    subreddit = subreddit.strip().lstrip("r/").strip("/")
    url = f"https://www.reddit.com/r/{subreddit}/{listing}/"
    # Reddit only honours the time window on listings that rank by it.
    if listing == "top":
        url += f"?t={time_filter}"
    return url


def build_input(
    subreddits: list[str],
    listing: str,
    time_filter: str,
    limit: int,
    allow_nsfw: bool,
    max_items: int | None = None,
) -> dict:
    """
    Actor input for a listing scrape.

    Comments are switched off twice — ``skipComments`` and ``maxComments`` — as
    a story recap only ever narrates the post, and on a pay-per-result actor
    every comment returned is billable.

    ``includeMediaLinks`` looks cosmetic but is not optional: the actor only
    populates ``upVotes`` when it is set, and without a score every post fails
    the minimum-score filter.
    """
    return {
        "startUrls": [
            {"url": _listing_url(subreddit, listing, time_filter)}
            for subreddit in subreddits
        ],
        "skipComments": True,
        "maxComments": 0,
        "maxPostCount": limit,
        "maxItems": max_items or limit * max(len(subreddits), 1),
        "includeNSFW": bool(allow_nsfw),
        "includeMediaLinks": True,
        "searchPosts": True,
        "searchComments": False,
        "searchCommunities": False,
        "searchUsers": False,
        "proxy": {"useApifyProxy": True},
    }


def _post(path: str, payload: dict) -> dict | None:
    try:
        response = requests.post(
            f"{API_BASE}{path}",
            json=payload,
            headers=_headers(),
            proxies=config.proxy,
            verify=_get_tls_verify(),
            timeout=_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        logger.warning(
            f"apify request failed: path={path}, "
            f"error_type={type(exc).__name__}, error={exc}"
        )
        return None
    except ValueError as exc:
        logger.warning(f"apify response for {path} was not JSON: {exc}")
        return None


def _get(path: str, params: dict | None = None):
    try:
        response = requests.get(
            f"{API_BASE}{path}",
            params=params or {},
            headers=_headers(),
            proxies=config.proxy,
            verify=_get_tls_verify(),
            timeout=_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        logger.warning(
            f"apify request failed: path={path}, "
            f"error_type={type(exc).__name__}, error={exc}"
        )
        return None
    except ValueError as exc:
        logger.warning(f"apify response for {path} was not JSON: {exc}")
        return None


def _parse_created(value) -> float:
    """``createdAt`` is ISO-8601; the rest of the pipeline expects epoch seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    if not value:
        return 0.0
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def normalize_item(item: dict) -> dict | None:
    """
    One dataset item into the shape ``reddit_source`` produces.

    Three fields the official API supplies have no equivalent here. ``is_self``
    is inferred from the body — a link post scrapes with an empty one — while
    ``stickied`` and ``locked`` default to False, so a pinned mod post is caught
    by the score and word filters rather than by its flag.
    """
    if not isinstance(item, dict):
        return None
    if item.get("dataType") not in (None, _POST_DATA_TYPE):
        return None

    post_id = str(item.get("parsedId") or item.get("id") or "").strip()
    title = " ".join(str(item.get("title", "") or "").split())
    if not post_id or not title:
        return None

    community = str(
        item.get("parsedCommunityName")
        or str(item.get("communityName", "") or "").lstrip("r/")
        or ""
    )
    body = str(item.get("body", "") or "")

    def _int(value) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    return {
        "id": post_id,
        "subreddit": community,
        "title": title,
        "selftext": body,
        "author": str(item.get("username", "") or ""),
        "score": _int(item.get("upVotes")),
        "num_comments": _int(item.get("numberOfComments")),
        "created_utc": _parse_created(item.get("createdAt")),
        "permalink": str(item.get("url", "") or ""),
        "over_18": bool(item.get("over18")),
        "spoiler": bool(item.get("spoiler")),
        "stickied": False,
        "locked": False,
        "is_self": bool(body.strip()),
    }


def run_actor(actor_input: dict, wait_seconds: int | None = None) -> list[dict]:
    """
    Start the actor, wait for it to finish, and return its dataset items.

    Run asynchronously and poll rather than using the run-sync endpoint: that
    one caps at five minutes, and a scrape that overruns it would be billed and
    then thrown away. Polling means a run that outlives our patience still has
    its results waiting in the dataset.
    """
    if not is_configured():
        logger.warning("apify token is not configured; skipping fetch")
        return []

    wait_seconds = int(
        wait_seconds or _setting("apify_timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    )

    started = _post(f"/acts/{_actor_id()}/runs", actor_input)
    run = (started or {}).get("data") or {}
    run_id = run.get("id")
    dataset_id = run.get("defaultDatasetId")
    if not run_id or not dataset_id:
        logger.error("apify did not return a run id; check the token and actor id")
        return []

    logger.info(f"apify run {run_id} started")

    deadline = time.time() + wait_seconds
    status = run.get("status")
    while status not in _TERMINAL_STATUSES:
        if time.time() > deadline:
            logger.error(
                f"apify run {run_id} still {status} after {wait_seconds}s; "
                "results stay in the dataset and the next run will start fresh"
            )
            return []
        time.sleep(_POLL_INTERVAL_SECONDS)
        polled = _get(f"/actor-runs/{run_id}")
        if polled is None:
            continue
        status = (polled.get("data") or {}).get("status")

    if status != "SUCCEEDED":
        logger.error(f"apify run {run_id} finished as {status}")
        return []

    items = _get(f"/datasets/{dataset_id}/items", {"clean": "true", "format": "json"})
    if not isinstance(items, list):
        logger.warning(f"apify dataset {dataset_id} did not return a list")
        return []

    logger.info(f"apify run {run_id} returned {len(items)} items")
    return items


def fetch_story_posts(
    subreddits: list[str] | tuple[str, ...] | None = None,
    listing: str = "top",
    time_filter: str = "day",
    limit: int = 50,
    allow_nsfw: bool = False,
) -> list[dict]:
    """
    Fetch listings for every subreddit in one actor run, best-scoring first.

    Unlike the official API this is a single call for all subreddits, so one bad
    subreddit name costs the whole run rather than just its own results.
    """
    if not subreddits:
        configured = _setting("subreddits", None)
        if isinstance(configured, str):
            subreddits = [s.strip() for s in configured.split(",") if s.strip()]
        elif isinstance(configured, (list, tuple)):
            subreddits = [str(s).strip() for s in configured if str(s).strip()]
        else:
            subreddits = []

    subreddits = [s for s in (subreddits or []) if s]
    if not subreddits:
        logger.warning("no subreddits configured for the apify fetch")
        return []

    actor_input = build_input(
        subreddits=list(subreddits),
        listing=listing,
        time_filter=time_filter,
        limit=limit,
        allow_nsfw=allow_nsfw,
    )
    items = run_actor(actor_input)

    posts = [post for post in (normalize_item(item) for item in items) if post]
    posts.sort(key=lambda post: post["score"], reverse=True)
    logger.info(f"apify returned {len(posts)} usable posts")
    return posts
