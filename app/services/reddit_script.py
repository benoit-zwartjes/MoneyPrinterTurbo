"""
Turn a Reddit self-post into narration scripts, split into Shorts-sized parts.

Two problems sit between a raw ``selftext`` field and a usable TTS script.

The body is markdown, so it carries link syntax, block quotes, spoiler tags and
zero-width characters that a voice engine reads aloud or chokes on. And these
subreddits have their own shorthand — ``AITA``, ``NTA``, ``(28F)``, ``MIL`` —
which TTS spells out letter by letter unless it is expanded first.

The output is a list of parts, each short enough for a Shorts slot, split only
on sentence boundaries so a part never ends mid-thought.
"""

from __future__ import annotations

import html
import re

from app.config import config


DEFAULT_WORDS_PER_MINUTE = 150
DEFAULT_PART_SECONDS = 55
DEFAULT_MAX_PARTS = 4
MIN_PART_SECONDS = 10
MAX_SUBJECT_LENGTH = 180

# Matched case-sensitively in their uppercase form. Lower-cased matching would
# rewrite ordinary words — "so", "op" and "info" all appear in normal prose.
DEFAULT_JARGON = {
    "AITA": "Am I the asshole",
    "WIBTA": "Would I be the asshole",
    "AITAH": "Am I the asshole",
    "NTA": "Not the asshole",
    "YTA": "You're the asshole",
    "ESH": "Everyone sucks here",
    "NAH": "No assholes here",
    "TIFU": "Today I messed up",
    "OP": "the original poster",
    "SO": "significant other",
    "MIL": "mother-in-law",
    "FIL": "father-in-law",
    "BIL": "brother-in-law",
    "SIL": "sister-in-law",
    "DH": "my husband",
    "DW": "my wife",
    "DS": "my son",
    "DD": "my daughter",
    "STBX": "soon to be ex",
    "LDR": "long distance relationship",
    "IMO": "in my opinion",
    "IMHO": "in my honest opinion",
    "IIRC": "if I remember correctly",
    "AFAIK": "as far as I know",
    "FWIW": "for what it's worth",
    "NGL": "not gonna lie",
    "IRL": "in real life",
    "TBH": "to be honest",
    "ETA": "edited to add",
    "YSK": "you should know",
    "LPT": "life pro tip",
}

# Sections that give away the ending or address commenters rather than the
# reader. Dropped only when they sit near the end — a post that opens with
# "UPDATE:" is a story in its own right.
_TRAILER_MARKERS = ("EDIT", "ETA", "TL;DR", "TLDR", "TL DR", "UPDATE")
_TRAILER_MIN_POSITION = 0.6

_ZERO_WIDTH = re.compile(r"[​-‏﻿⁠]")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_BARE_URL = re.compile(r"https?://\S+|www\.\S+")
_SPOILER = re.compile(r">!(.+?)!<", re.DOTALL)
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_QUOTE = re.compile(r"^\s{0,3}>\s?", re.MULTILINE)
_RULE = re.compile(r"^\s{0,3}([-*_])\s*(\1\s*){2,}$", re.MULTILINE)
_BULLET = re.compile(r"^\s{0,3}[-*+]\s+", re.MULTILINE)
_ORDERED = re.compile(r"^\s{0,3}\d{1,2}[.)]\s+", re.MULTILINE)
_EMPHASIS = re.compile(r"(\*{1,3}|_{1,3}|~{2})(?=\S)(.+?)(?<=\S)\1", re.DOTALL)
_CODE = re.compile(r"`+([^`]+)`+")
_ESCAPED = re.compile(r"\\([\\`*_{}\[\]()#+\-.!~>])")
_SUPERSCRIPT = re.compile(r"\^+")

_AGE_GENDER = re.compile(r"[(\[]\s*(\d{1,2})\s*([MmFf])\s*[)\]]")
_GENDER_AGE = re.compile(r"[(\[]\s*([MmFf])\s*(\d{1,2})\s*[)\]]")
_BARE_AGE_GENDER = re.compile(r"\b(\d{1,2})([MF])\b")

# Sentence boundary: terminator, closing quote or bracket, then whitespace.
# The lookbehind keeps common abbreviations from splitting a sentence in half.
_ABBREVIATIONS = (
    "Mr", "Mrs", "Ms", "Dr", "Prof", "Sr", "Jr", "St", "vs", "etc",
    "e.g", "i.e", "approx", "Inc", "Ltd", "No",
)
_SENTENCE_SPLIT = re.compile(r'(?<=[.!?])["\')\]]*\s+')

_WHITESPACE = re.compile(r"[ \t]+")
_BLANK_LINES = re.compile(r"\n{2,}")


def _setting(key: str, default=None):
    return config.app.get(f"reddit_{key}", default)


def words_per_minute() -> int:
    try:
        value = int(_setting("words_per_minute", DEFAULT_WORDS_PER_MINUTE))
    except (TypeError, ValueError):
        return DEFAULT_WORDS_PER_MINUTE
    return value if value > 0 else DEFAULT_WORDS_PER_MINUTE


def jargon_map() -> dict[str, str]:
    """Defaults merged with any ``reddit_jargon`` table from config.toml."""
    merged = dict(DEFAULT_JARGON)
    extra = _setting("jargon", None)
    if isinstance(extra, dict):
        for key, value in extra.items():
            key = str(key).strip()
            value = str(value).strip()
            if key and value:
                merged[key] = value
    return merged


def expand_jargon(text: str, mapping: dict[str, str] | None = None) -> str:
    """Expand subreddit shorthand so TTS reads words instead of letters."""
    mapping = mapping if mapping is not None else jargon_map()
    if not text:
        return ""

    # Longest first, so WIBTA is not consumed by a shorter overlapping key.
    for acronym in sorted(mapping, key=len, reverse=True):
        replacement = mapping[acronym]
        pattern = re.compile(rf"(?<![A-Za-z]){re.escape(acronym)}(?![A-Za-z])")
        text = pattern.sub(replacement.replace("\\", r"\\"), text)

    text = _AGE_GENDER.sub(
        lambda m: f" {m.group(1)} {'female' if m.group(2).lower() == 'f' else 'male'} ",
        text,
    )
    text = _GENDER_AGE.sub(
        lambda m: f" {m.group(2)} {'female' if m.group(1).lower() == 'f' else 'male'} ",
        text,
    )
    text = _BARE_AGE_GENDER.sub(
        lambda m: f"{m.group(1)} {'female' if m.group(2) == 'F' else 'male'}",
        text,
    )
    return text


def _strip_trailer(text: str) -> str:
    """Drop a trailing EDIT / TL;DR block, but only when it really is trailing."""
    if not text:
        return ""

    lines = text.split("\n")
    offsets: list[int] = []
    cursor = 0
    for line in lines:
        offsets.append(cursor)
        cursor += len(line) + 1

    total = max(len(text), 1)
    for index, line in enumerate(lines):
        stripped = line.strip().upper()
        if not stripped:
            continue
        for marker in _TRAILER_MARKERS:
            if not stripped.startswith(marker):
                continue
            remainder = stripped[len(marker):].lstrip()
            # Require punctuation or nothing after the marker so a sentence
            # opening with "Update the spreadsheet" survives.
            if remainder and remainder[0] not in ":-—–,. ":
                continue
            if offsets[index] / total >= _TRAILER_MIN_POSITION:
                return "\n".join(lines[:index])
    return text


def normalize_selftext(text: str, expand: bool = True) -> str:
    """Markdown self-post body to plain narration text."""
    if not text:
        return ""

    text = html.unescape(text)
    # Reddit double-escapes some bodies, so one pass leaves "&#x200B;" sitting
    # in the text as literal characters the voice engine would read out.
    if "&#" in text or "&amp;" in text:
        text = html.unescape(text)
    text = _ZERO_WIDTH.sub("", text)
    text = _strip_trailer(text)

    text = _IMAGE.sub(" ", text)
    text = _LINK.sub(r"\1", text)
    text = _BARE_URL.sub(" ", text)
    text = _SPOILER.sub(r"\1", text)
    text = _RULE.sub(" ", text)
    text = _HEADING.sub("", text)
    text = _QUOTE.sub("", text)
    text = _BULLET.sub("", text)
    text = _ORDERED.sub("", text)
    text = _CODE.sub(r"\1", text)
    text = _EMPHASIS.sub(r"\2", text)
    text = _SUPERSCRIPT.sub("", text)
    text = _ESCAPED.sub(r"\1", text)

    if expand:
        text = expand_jargon(text)

    text = _WHITESPACE.sub(" ", text)
    text = _BLANK_LINES.sub("\n", text)
    return _terminate_lines(text)


_TERMINATED = re.compile(r"""[.!?…]["')\]]*$""")


def _terminate_lines(text: str) -> str:
    """
    Give every line a sentence terminator.

    These posts lean on bullet lists and single-line paragraphs that carry no
    full stop. Without one the sentence splitter cannot see a boundary, so a
    whole list ends up inside one "sentence" and is read as a run-on.
    """
    lines = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if not _TERMINATED.search(line):
            line += "."
        lines.append(line)
    return "\n".join(lines).strip()


def normalize_title(title: str, expand: bool = True) -> str:
    title = html.unescape(str(title or ""))
    title = _ZERO_WIDTH.sub("", title)
    title = _LINK.sub(r"\1", title)
    if expand:
        title = expand_jargon(title)
    return " ".join(title.split()).strip()


def estimate_seconds(text: str, wpm: int | None = None) -> float:
    wpm = wpm or words_per_minute()
    return len(text.split()) / max(wpm, 1) * 60.0


def split_sentences(text: str) -> list[str]:
    """Split on sentence boundaries, keeping common abbreviations intact."""
    if not text:
        return []

    # Protect abbreviation periods, split, then restore.
    guarded = text
    for abbreviation in _ABBREVIATIONS:
        guarded = re.sub(
            rf"(?<![A-Za-z]){re.escape(abbreviation)}\.",
            f"{abbreviation}\x00",
            guarded,
        )

    pieces = [p.strip() for p in _SENTENCE_SPLIT.split(guarded)]
    return [p.replace("\x00", ".") for p in pieces if p.strip()]


def _pack_sentences(sentences: list[str], budget_for_part) -> list[list[str]]:
    """
    Greedily fill parts up to a per-part word budget, never splitting a sentence.

    ``budget_for_part(n)`` returns the words available for part ``n``'s body.
    The budget varies because part one also has to narrate the title, and every
    part but the last carries a follow-on line — spend that overhead here and a
    55-second target does not quietly render at 70 seconds.

    A single sentence longer than the budget becomes its own part: cutting one
    in half reads worse than a part that runs slightly long.
    """
    chunks: list[list[str]] = []
    current: list[str] = []
    current_words = 0
    budget = budget_for_part(1)

    for sentence in sentences:
        sentence_words = len(sentence.split())
        if current and current_words + sentence_words > budget:
            chunks.append(current)
            current = [sentence]
            current_words = sentence_words
            budget = budget_for_part(len(chunks) + 1)
        else:
            current.append(sentence)
            current_words += sentence_words

    if current:
        chunks.append(current)
    return chunks


def _part_subject(subreddit: str, title: str, index: int, total: int) -> str:
    base = f"r/{subreddit} — {title}" if subreddit else title
    if total > 1:
        base = f"{base} (Part {index}/{total})"
    return base[:MAX_SUBJECT_LENGTH]


def split_post(
    post: dict,
    part_seconds: int | None = None,
    max_parts: int | None = None,
    wpm: int | None = None,
) -> dict:
    """
    Build the per-part narration scripts for one Reddit post.

    Returns ``parts: []`` when there is nothing narratable left after cleaning,
    which the caller should treat as "skip this post", not as an error.
    """
    wpm = wpm or words_per_minute()
    part_seconds = max(
        int(part_seconds or _setting("part_seconds", DEFAULT_PART_SECONDS)),
        MIN_PART_SECONDS,
    )
    max_parts = max(int(max_parts or _setting("max_parts", DEFAULT_MAX_PARTS)), 1)

    title = normalize_title(post.get("title", ""))
    body = normalize_selftext(post.get("selftext", ""))
    subreddit = str(post.get("subreddit", "") or "")

    result = {
        "post_id": str(post.get("id", "") or ""),
        "subreddit": subreddit,
        "title": title,
        "permalink": str(post.get("permalink", "") or ""),
        "score": int(post.get("score", 0) or 0),
        "total_words": len(body.split()),
        "truncated": False,
        "parts": [],
    }
    if not title or not body:
        return result

    budget_words = max(int(part_seconds / 60.0 * wpm), 20)
    sentences = split_sentences(body)
    if not sentences:
        return result

    intro_template = str(_setting("part_intro", "Part {index}.") or "")
    outro_template = str(
        _setting("part_outro", "Part {next_index} is up next — follow so you catch it.")
        or ""
    )
    final_outro = str(_setting("final_outro", "") or "")

    # Reserve the words the framing will spend. The outro is charged to every
    # part: which one is last is not known until packing is done, and coming in
    # slightly under the target is the harmless direction to be wrong in.
    title_words = len(title.split())
    intro_words = len(intro_template.format(index=2, total=9).split())
    outro_words = len(
        outro_template.format(index=1, next_index=2, total=9).split()
    )

    def budget_for_part(index: int) -> int:
        overhead = (title_words if index == 1 else intro_words) + outro_words
        return max(budget_words - overhead, 20)

    chunks = _pack_sentences(sentences, budget_for_part)

    if len(chunks) > max_parts:
        chunks = chunks[:max_parts]
        result["truncated"] = True

    total = len(chunks)

    for index, chunk in enumerate(chunks, start=1):
        segments: list[str] = []
        if index == 1:
            segments.append(title if title.endswith((".", "!", "?")) else f"{title}.")
        elif intro_template:
            segments.append(intro_template.format(index=index, total=total))

        # Expanding an acronym that opened a sentence leaves it lower-cased
        # ("IMO that's too long" -> "in my opinion..."), which shows up in the
        # caption even though TTS cannot hear it.
        body_segment = " ".join(chunk)
        if body_segment[:1].islower():
            body_segment = body_segment[0].upper() + body_segment[1:]
        segments.append(body_segment)

        if index < total and outro_template:
            segments.append(
                outro_template.format(
                    index=index, next_index=index + 1, total=total
                )
            )
        elif index == total and final_outro:
            segments.append(final_outro.format(index=index, total=total))

        script = " ".join(segment.strip() for segment in segments if segment.strip())
        result["parts"].append(
            {
                "index": index,
                "total": total,
                "script": script,
                "subject": _part_subject(subreddit, title, index, total),
                "estimated_seconds": round(estimate_seconds(script, wpm), 1),
            }
        )

    return result
