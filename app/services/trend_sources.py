"""
从公开热榜来源拉取话题标签。

只接入官方公开发布、供程序读取的接口：Wikipedia 的 pageviews REST API 和
Google Trends 的 RSS 订阅。两者都不需要 API Key。这里刻意不抓取 TikTok
Creative Center——该页面的内部接口不是公开 API，抓取违反平台条款；需要
TikTok 榜单时应使用取得授权的第三方数据商，或手动粘贴。

任何来源失败都返回空列表并记录日志：热榜拉取是选题流程的辅助步骤，
不能因为外部服务波动就阻断用户手动粘贴标签。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree

import requests
from loguru import logger

from app.config import config


SOURCE_WIKIPEDIA = "wikipedia"
SOURCE_GOOGLE_TRENDS = "google_trends"
TREND_SOURCES = (SOURCE_WIKIPEDIA, SOURCE_GOOGLE_TRENDS)

DEFAULT_REGION = "US"
DEFAULT_LIMIT = 25
MAX_LIMIT = 100
_REQUEST_TIMEOUT = (5, 15)
_USER_AGENT = "MoneyPrinterTurbo/1.0 (trending topic discovery)"

WIKIPEDIA_TOP_URL = (
    "https://wikimedia.org/api/rest_v1/metrics/pageviews/top/"
    "{project}/all-access/{year}/{month}/{day}"
)
GOOGLE_TRENDS_RSS_URL = "https://trends.google.com/trending/rss?geo={geo}"

# 地区代码到 Wikipedia 语言项目的映射。未列出的地区回退到英文站，
# 这样新增地区不会直接报错。
_REGION_TO_WIKI_LANGUAGE = {
    "US": "en", "GB": "en", "AU": "en", "CA": "en", "IN": "en",
    "DE": "de", "AT": "de", "CH": "de",
    "FR": "fr", "ES": "es", "MX": "es", "AR": "es",
    "IT": "it", "PT": "pt", "BR": "pt",
    "NL": "nl", "RU": "ru", "JP": "ja", "KR": "ko",
    "ID": "id", "VN": "vi", "TR": "tr", "PL": "pl", "SE": "sv",
}

# Wikipedia 榜单里常年占据前列的导航页和维护页不是选题。
_WIKI_TITLE_BLOCKLIST = frozenset(
    {"main_page", "special:search", "wikipedia:hauptseite", "-"}
)
_WIKI_NAMESPACE_RE = re.compile(
    r"^(special|wikipedia|portal|help|category|file|template|talk):",
    re.IGNORECASE,
)


def _normalize_region(region: str | None) -> str:
    value = str(region or "").strip().upper()
    return value if re.fullmatch(r"[A-Z]{2}", value or "") else DEFAULT_REGION


def _normalize_limit(limit: int | None) -> int:
    try:
        value = int(limit or DEFAULT_LIMIT)
    except (TypeError, ValueError):
        value = DEFAULT_LIMIT
    return max(1, min(value, MAX_LIMIT))


def _get_tls_verify() -> bool:
    tls_verify = config.app.get("tls_verify", True)
    if isinstance(tls_verify, str):
        tls_verify = tls_verify.strip().lower() not in ("0", "false", "no", "off")
    return bool(tls_verify)


def _request(url: str) -> requests.Response | None:
    try:
        response = requests.get(
            url,
            headers={"User-Agent": _USER_AGENT},
            proxies=config.proxy,
            verify=_get_tls_verify(),
            timeout=_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return response
    except requests.RequestException as exc:
        # 只记录异常类型和信息，不回显 URL，避免代理配置里的凭据进入日志。
        logger.warning(
            f"failed to fetch trending topics: "
            f"url_host={url.split('/')[2]}, error_type={type(exc).__name__}, error={exc}"
        )
        return None


def _fetch_wikipedia(region: str, limit: int) -> list[str]:
    """
    读取 Wikipedia 前一日最高浏览量条目。

    使用前一日而不是当天：当天数据在 UTC 日切前并不完整，直接查询经常
    返回 404。条目标题天然是"人们正在查的东西"，很适合作为科普类选题。
    """
    language = _REGION_TO_WIKI_LANGUAGE.get(region, "en")
    day = datetime.now(timezone.utc).date() - timedelta(days=1)
    url = WIKIPEDIA_TOP_URL.format(
        project=f"{language}.wikipedia",
        year=day.strftime("%Y"),
        month=day.strftime("%m"),
        day=day.strftime("%d"),
    )

    response = _request(url)
    if response is None:
        return []

    try:
        payload = response.json()
    except ValueError as exc:
        logger.warning(f"wikipedia trending payload is not valid JSON: {exc}")
        return []

    items = (payload.get("items") or [{}])[0].get("articles") or []
    titles: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        raw_title = str(item.get("article") or "").strip()
        if not raw_title:
            continue
        if raw_title.casefold() in _WIKI_TITLE_BLOCKLIST:
            continue
        if _WIKI_NAMESPACE_RE.match(raw_title):
            continue
        titles.append(raw_title.replace("_", " "))
        if len(titles) >= limit:
            break
    return titles


def _fetch_google_trends(region: str, limit: int) -> list[str]:
    """读取 Google Trends 官方发布的每日热搜 RSS。"""
    response = _request(GOOGLE_TRENDS_RSS_URL.format(geo=region))
    if response is None:
        return []

    try:
        root = ElementTree.fromstring(response.content)
    except ElementTree.ParseError as exc:
        logger.warning(f"google trends RSS is not valid XML: {exc}")
        return []

    titles: list[str] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        titles.append(title)
        if len(titles) >= limit:
            break
    return titles


def fetch_trending_tags(
    source: str = SOURCE_WIKIPEDIA,
    region: str = DEFAULT_REGION,
    limit: int = DEFAULT_LIMIT,
) -> list[str]:
    """
    拉取指定来源的热门话题，去重后返回。

    返回空列表表示"这次没取到"，调用方应保留用户手动粘贴的入口，
    而不是把它当成致命错误。
    """
    region = _normalize_region(region)
    limit = _normalize_limit(limit)

    if source == SOURCE_WIKIPEDIA:
        titles = _fetch_wikipedia(region, limit)
    elif source == SOURCE_GOOGLE_TRENDS:
        titles = _fetch_google_trends(region, limit)
    else:
        logger.warning(f"unsupported trend source: {source}")
        return []

    deduped: list[str] = []
    seen: set[str] = set()
    for title in titles:
        normalized = " ".join(title.split())
        key = normalized.casefold()
        if not normalized or key in seen:
            continue
        seen.add(key)
        deduped.append(normalized)

    logger.info(f"fetched {len(deduped)} trending topics: source={source}, region={region}")
    return deduped
