"""
Reddit story-post ingestion for the recap pipeline.

Talks to Reddit's official OAuth2 API using the application-only
(``grant_type=client_credentials``) flow. That needs a registered "script" app
but no account password, and it is the only supported way to read listings from
a server: the unauthenticated ``.json`` endpoints are throttled to roughly
10 queries per minute and datacenter ranges are blocked outright, so a Coolify
deployment collects 429s within minutes.

Credentials live under ``[app]`` in config.toml, following the same convention
as the Upload-Post integration. Any failure returns an empty list and logs —
a listing outage must never break a scheduled run half way through.

Docs: https://github.com/reddit-archive/reddit/wiki/OAuth2
"""

from __future__ import annotations

import threading
import time

import requests
from loguru import logger

from app import __version__
from app.config import config


TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API_BASE = "https://oauth.reddit.com"

LISTINGS = ("top", "hot", "new", "rising")
TIME_FILTERS = ("hour", "day", "week", "month", "year", "all")

DEFAULT_SUBREDDITS = ("AmItheAsshole", "tifu", "relationship_advice")
DEFAULT_LISTING = "top"
DEFAULT_TIME_FILTER = "day"
DEFAULT_FETCH_LIMIT = 50
MAX_FETCH_LIMIT = 100

_REQUEST_TIMEOUT = (5, 20)
# Reddit expires application-only tokens after an hour. Renew early so a long
# batch run never fails on a token that lapsed mid-flight.
_TOKEN_EARLY_RENEWAL_SECONDS = 300

_token_lock = threading.Lock()
_token_cache: dict = {"access_token": "", "expires_at": 0.0}


def _setting(key: str, default=None):
    return config.app.get(f"reddit_{key}", default)


def _get_tls_verify() -> bool:
    tls_verify = config.app.get("tls_verify", True)
    if isinstance(tls_verify, str):
        tls_verify = tls_verify.strip().lower() not in ("0", "false", "no", "off")
    return bool(tls_verify)


def user_agent() -> str:
    """
    Reddit requires ``<platform>:<app id>:<version> (by /u/<username>)`` and
    heavily throttles generic or browser-style agents, so this is built rather
    than left to the caller.
    """
    configured = str(_setting("user_agent", "") or "").strip()
    if configured:
        return configured
    username = str(_setting("username", "") or "").strip().lstrip("/u/").lstrip("u/")
    suffix = f" (by /u/{username})" if username else ""
    return f"server:MoneyPrinterTurbo:{__version__}{suffix}"


def is_configured() -> bool:
    return bool(
        str(_setting("client_id", "") or "").strip()
        and str(_setting("client_secret", "") or "").strip()
    )


def _fetch_token() -> str:
    """Return a cached bearer token, refreshing it when it is close to expiry."""
    with _token_lock:
        now = time.time()
        if (
            _token_cache["access_token"]
            and now < _token_cache["expires_at"] - _TOKEN_EARLY_RENEWAL_SECONDS
        ):
            return _token_cache["access_token"]

        client_id = str(_setting("client_id", "") or "").strip()
        client_secret = str(_setting("client_secret", "") or "").strip()
        if not client_id or not client_secret:
            logger.warning(
                "reddit client_id/client_secret are not configured; skipping fetch"
            )
            return ""

        try:
            response = requests.post(
                TOKEN_URL,
                auth=(client_id, client_secret),
                data={"grant_type": "client_credentials"},
                headers={"User-Agent": user_agent()},
                proxies=config.proxy,
                verify=_get_tls_verify(),
                timeout=_REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            # Never echo the URL or body: both can carry credentials.
            logger.warning(
                f"reddit token request failed: "
                f"error_type={type(exc).__name__}, error={exc}"
            )
            return ""
        except ValueError as exc:
            logger.warning(f"reddit token response was not JSON: {exc}")
            return ""

        token = str(payload.get("access_token", "") or "")
        if not token:
            logger.warning("reddit token response did not contain an access_token")
            return ""

        try:
            expires_in = float(payload.get("expires_in", 3600))
        except (TypeError, ValueError):
            expires_in = 3600.0

        _token_cache["access_token"] = token
        _token_cache["expires_at"] = now + expires_in
        logger.info(f"reddit access token acquired, valid for {int(expires_in)}s")
        return token


def reset_token_cache() -> None:
    """Drop the cached token. Used by tests and after a credential change."""
    with _token_lock:
        _token_cache["access_token"] = ""
        _token_cache["expires_at"] = 0.0


def _api_get(path: str, params: dict) -> dict | None:
    token = _fetch_token()
    if not token:
        return None

    try:
        response = requests.get(
            f"{API_BASE}{path}",
            params=params,
            headers={
                "Authorization": f"bearer {token}",
                "User-Agent": user_agent(),
            },
            proxies=config.proxy,
            verify=_get_tls_verify(),
            timeout=_REQUEST_TIMEOUT,
        )
        if response.status_code == 401:
            # The cached token was rejected — force a refresh on the next call
            # rather than failing every remaining subreddit in this run.
            reset_token_cache()
            logger.warning(f"reddit rejected the access token for {path}")
            return None
        if response.status_code == 429:
            logger.warning(
                f"reddit rate limited {path}; "
                "reduce the schedule frequency or the subreddit count"
            )
            return None
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        logger.warning(
            f"reddit request failed: path={path}, "
            f"error_type={type(exc).__name__}, error={exc}"
        )
        return None
    except ValueError as exc:
        logger.warning(f"reddit response for {path} was not JSON: {exc}")
        return None


def _normalize_post(child: dict) -> dict | None:
    """Flatten one listing child into the fields the recap pipeline needs."""
    if not isinstance(child, dict):
        return None
    data = child.get("data")
    if not isinstance(data, dict):
        return None

    post_id = str(data.get("id", "") or "").strip()
    title = " ".join(str(data.get("title", "") or "").split())
    if not post_id or not title:
        return None

    def _int(value) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    return {
        "id": post_id,
        "subreddit": str(data.get("subreddit", "") or ""),
        "title": title,
        "selftext": str(data.get("selftext", "") or ""),
        "author": str(data.get("author", "") or ""),
        "score": _int(data.get("score")),
        "num_comments": _int(data.get("num_comments")),
        "created_utc": float(data.get("created_utc") or 0.0),
        "permalink": f"https://www.reddit.com{data.get('permalink', '')}",
        "over_18": bool(data.get("over_18")),
        "spoiler": bool(data.get("spoiler")),
        "stickied": bool(data.get("stickied")),
        "locked": bool(data.get("locked")),
        "is_self": bool(data.get("is_self")),
    }


def fetch_subreddit_posts(
    subreddit: str,
    listing: str = DEFAULT_LISTING,
    time_filter: str = DEFAULT_TIME_FILTER,
    limit: int = DEFAULT_FETCH_LIMIT,
) -> list[dict]:
    """Fetch one listing page. Returns [] on any failure."""
    subreddit = str(subreddit or "").strip().lstrip("r/").strip("/")
    if not subreddit:
        return []

    listing = listing if listing in LISTINGS else DEFAULT_LISTING
    limit = max(1, min(int(limit or DEFAULT_FETCH_LIMIT), MAX_FETCH_LIMIT))

    params: dict = {"limit": limit, "raw_json": 1}
    if listing == "top":
        params["t"] = time_filter if time_filter in TIME_FILTERS else DEFAULT_TIME_FILTER

    payload = _api_get(f"/r/{subreddit}/{listing}", params)
    if not payload:
        return []

    children = (payload.get("data") or {}).get("children")
    if not isinstance(children, list):
        logger.warning(f"unexpected reddit listing shape for r/{subreddit}")
        return []

    posts = [post for post in (_normalize_post(c) for c in children) if post]
    logger.info(f"fetched {len(posts)} posts from r/{subreddit}/{listing}")
    return posts


def fetch_story_posts(
    subreddits: list[str] | tuple[str, ...] | None = None,
    listing: str = DEFAULT_LISTING,
    time_filter: str = DEFAULT_TIME_FILTER,
    limit: int = DEFAULT_FETCH_LIMIT,
) -> list[dict]:
    """
    Fetch every configured subreddit and return the combined list, best first.

    One failing subreddit does not sink the run: it contributes nothing and the
    remaining subreddits are still fetched.
    """
    if not subreddits:
        configured = _setting("subreddits", None)
        if isinstance(configured, str):
            subreddits = [s.strip() for s in configured.split(",") if s.strip()]
        elif isinstance(configured, (list, tuple)):
            subreddits = [str(s).strip() for s in configured if str(s).strip()]
        else:
            subreddits = list(DEFAULT_SUBREDDITS)

    collected: list[dict] = []
    for subreddit in subreddits:
        collected.extend(
            fetch_subreddit_posts(
                subreddit, listing=listing, time_filter=time_filter, limit=limit
            )
        )

    collected.sort(key=lambda post: post["score"], reverse=True)
    return collected


def filter_posts(
    posts: list[dict],
    min_score: int = 0,
    min_words: int = 0,
    max_words: int = 0,
    allow_nsfw: bool = False,
    exclude_ids: set[str] | None = None,
) -> list[dict]:
    """
    Keep only self-posts that will actually narrate well.

    Link posts, stickied announcements and removed bodies all reach the listing
    but have no story in them, so they are dropped before anything is rendered.
    """
    exclude_ids = exclude_ids or set()
    kept: list[dict] = []

    for post in posts:
        if post["id"] in exclude_ids:
            continue
        if not post["is_self"]:
            continue
        if post["stickied"]:
            continue
        if not allow_nsfw and post["over_18"]:
            continue

        body = post["selftext"].strip()
        # Reddit keeps the listing entry after a removal and replaces the body
        # with one of these markers.
        if not body or body in ("[removed]", "[deleted]"):
            continue
        if post["score"] < min_score:
            continue

        word_count = len(body.split())
        if min_words and word_count < min_words:
            continue
        if max_words and word_count > max_words:
            continue

        kept.append(post)

    return kept
