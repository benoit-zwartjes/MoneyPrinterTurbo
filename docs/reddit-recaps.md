# Automated Reddit recaps

Fetch story posts from Reddit, split them into Shorts-sized parts, render each
part, hold them for review, then hand the approved ones to Upload-Post with a
publishing schedule.

Two ways to drive it. The **Reddit Recaps page** in the WebUI does the whole
thing — find, render, review, schedule — and is the one to use day to day. The
**CLI scripts** do the same work unattended, for a cron job.

```
                    fetch → filter → split          (app/services/reddit_pipeline.py)
                              ↓
WebUI page → background job ─→ background task pool ──┐
scripts/reddit_recap.py ─────→ cli.py --batch-file────┘
                              ↓
                    review queue              (storage/reddit_recaps.json)
                              ↓
                    approve → schedule        (Upload-Post scheduled_date)
                              ↓
                    recap worker              (promotes renders and uploads)
```

Both entry points share `reddit_pipeline`, so they cannot drift apart. The page
is a new file under `webui/pages/`, not an edit to `Main.py` — that is the file
upstream changes most, and a new file never conflicts on merge.

Nothing slow runs inside the page script. Finding stories and scheduling
uploads are handed to `app/services/reddit_jobs.py`, which runs them in daemon
threads owned by the server process and writes their state to
`storage/reddit_jobs.json`. The page only reads that file, so a result survives
a refresh, a dropped websocket, a locked phone or a second tab — and closing
the browser does not stop the work. The same module runs one **recap worker**
per process, which promotes finished renders into the review queue and asks
Upload-Post what became of scheduled parts, so the queue keeps moving with
nobody watching it.

## What was added

| Path | Role |
| --- | --- |
| `webui/pages/1_Reddit_Recaps.py` | The whole workflow as a page |
| `app/services/reddit_pipeline.py` | Shared orchestration and the backend switch |
| `app/services/reddit_jobs.py` | Background jobs, their persisted state, and the worker |
| `app/services/gameplay_library.py` | The Minecraft parkour clips every render plays over |
| `app/services/reddit_apify.py` | Apify actor backend |
| `app/services/reddit_source.py` | Official Reddit API backend |
| `app/services/reddit_script.py` | Markdown → narration, jargon, part splitting |
| `app/services/reddit_queue.py` | Dedup, review queue, upload job tracking |
| `scripts/reddit_recap.py` | Unattended fetch and render |
| `scripts/reddit_publish.py` | Unattended review and schedule |

`app/services/upload_post.py` gained one optional `scheduled_date` argument.

## 1. Pick a fetch backend

Two backends return identical posts; everything downstream is unaware of which
ran. Set `reddit_provider` in `config.toml`, or pick it on the page.

### Apify (default)

Runs [`trudax/reddit-scraper-lite`](https://apify.com/trudax/reddit-scraper-lite).
No Reddit app to register and no per-client rate limit. Paste the token from
console.apify.com → Settings → Integrations:

```toml
reddit_provider = "apify"
reddit_apify_token = "apify_api_..."
```

It is **pay-per-result**, so `reddit_fetch_limit` is a cost control as much as a
size one — it is passed as `maxPostCount`, and `maxItems` is that times the
number of subreddits. Comments are switched off twice (`skipComments` and
`maxComments: 0`) since a recap never narrates them and each one would be
billable.

Two details are load-bearing. `includeMediaLinks` is sent as true because the
actor only populates `upVotes` when it is — without it every post scores zero
and fails the minimum-score filter. And subreddits are driven through
`startUrls` pointing at listing pages rather than the actor's `searches` input,
so what comes back matches what that URL shows in a browser.

The actor gives no `stickied` or `locked` flag, so a pinned mod post is caught
by the score and word filters rather than by its flag. `is_self` is inferred
from an empty body, which is what a link post scrapes as.

### Official Reddit API

Register a **script** app at <https://www.reddit.com/prefs/apps>:

```toml
reddit_provider = "official"
reddit_client_id = "..."
reddit_client_secret = "..."
reddit_username = "your-reddit-username"
```

Free, and uses the application-only (`client_credentials`) flow — no account
password. Capped at ~100 queries a minute per client. The username only builds
the User-Agent Reddit requires, in the form
`<platform>:<app id>:<version> (by /u/<username>)`; generic agents are throttled
hard. The unauthenticated `.json` endpoints are not a fallback: roughly 10
queries a minute, and datacenter IP ranges are blocked outright.

## 2. Turn off auto-upload

`app/services/task.py` cross-posts at the end of the video stage whenever
Upload-Post auto-upload is on. That would publish every part the moment it
renders and make the review queue pointless, so `scripts/reddit_recap.py`
refuses to run while it is enabled:

```toml
upload_post_auto_upload = false
```

Pass `--allow-auto-upload` to override, once you trust the output enough to
skip review entirely.

## 3. Use the page

Open **Reddit Recaps** in the WebUI sidebar. It carries the whole workflow, and
every step of it runs on the server:

1. **Connection** — pick the backend and paste its credentials.
2. **Story filters** and **Parts and video** — subreddits, thresholds, part
   length, voice, aspect ratio, material terms, publish caption. Changes save as
   you make them.
3. **1 · Find** — starts a background search. The button reads *Searching…*
   while it runs, the progress bar above the tabs shows what the server is
   doing, and the result is written to disk: leave the page, come back tomorrow,
   the candidates are still there. Each candidate shows its parts and the full
   narration script. Nothing renders yet; read the scripts before spending
   render time.

   A search that finds nothing says which step emptied it — nothing fetched
   (credentials, subreddit names, network), nothing past the filters (score and
   word thresholds, or the story is already in the library), or everything
   dropped while splitting (too long for the maximum parts).
4. Untick what you do not want and **Render** — parts go to the same background
   task pool the main page uses, and the worker moves them into the review queue
   as each render finishes, whether or not the page is open.
5. **2 · Review** — watch each part, then approve or reject the story. A story
   still rendering can be **stopped** from here. See *Rejecting* below for what
   rejecting keeps and what it deletes.
6. **3 · Schedule** — pick a first slot and an interval; approved parts are
   handed to Upload-Post in story order, so part 1 always lands before part 2.
   The uploads themselves run as a background job, so a batch of large files
   does not tie the browser to the tab. The worker then polls Upload-Post and
   marks each part **published** when it goes out.
7. **4 · All stories** — every story the pipeline has ever picked up, newest
   first, with what became of it: how many parts rendered, are waiting for
   review, are approved, scheduled or published, each part's upload job, its
   slot, and any error. Filter by state to answer "what is stuck" or "what went
   out".

The counters above the tabs are the same picture in one line: rendering, to
review, approved, scheduled, published, failed.

Everything below is the same workflow without a browser, for a cron job.

## 4. Dry run

```bash
uv run python scripts/reddit_recap.py --dry-run
```

Fetches, filters and splits without rendering anything, and prints the parts as
JSON. Read the scripts before spending render time — the normalizer is where
surprises live.

## 5. Render

```bash
uv run python scripts/reddit_recap.py --max-posts 3
```

Every option defaults to a `reddit_*` key under `[app]`; see
`config.example.toml`. Arguments after `--` are forwarded verbatim to `cli.py`:

```bash
uv run python scripts/reddit_recap.py --max-posts 2 -- --voice-name en-US-AriaNeural
```

Rendered parts land in the queue as `rendered`.

## 6. Review and schedule

```bash
uv run python scripts/reddit_publish.py list
uv run python scripts/reddit_publish.py approve 1a2b3c
uv run python scripts/reddit_publish.py schedule --interval-hours 8 --dry-run
uv run python scripts/reddit_publish.py schedule --interval-hours 8
```

Parts are scheduled in story order, so part 1 always lands before part 2.
Upload-Post keeps the calendar — `scheduled_date` accepts ISO-8601 up to 365
days ahead — so nothing here has to stay running to publish on time.

Check on jobs later:

```bash
uv run python scripts/reddit_publish.py status --refresh
```

## 7. Schedule it in Coolify

**Resource → Scheduled Tasks**, running inside the existing container:

| Field | Value |
| --- | --- |
| Command | `python scripts/reddit_recap.py --max-posts 3` |
| Frequency | `0 6 * * *` |

Rendering is ffmpeg-bound, so pick an hour when you are not using the WebUI —
the two will fight for CPU otherwise.

Leave the publish step manual, or add a second task once you trust the output.

## Background footage

Recaps play over gameplay footage rather than stock b-roll, because that is what
the format is watched with. `reddit_background = "gameplay"` uses the clip
library; `pexels`, `pixabay` and `coverr` still search stock video with
`reddit_video_terms` instead.

The library is `storage/local_videos/gameplay`. That parent directory is not
cosmetic: `video.preprocess_video` refuses to read a local material from
anywhere else, so a library outside it would look fine in the page and be
dropped at render time.

Two ways to fill it:

* **Upload** on the page, under *Gameplay clips*. Validation and the 200 MB
  limit are the same ones the main page's local materials use.
* **Drop files into the folder** over a mounted volume or SFTP. A ten-minute
  parkour clip is usually past what a browser upload is worth; anything in the
  folder is listed whether or not the page ever saw it.

One clip backs one part, picked by hashing the part's subject: parts of a story
spread across the library, and re-rendering a part picks the same clip again
rather than quietly changing the look of one video in a published set. The
render then cuts that clip into segments the usual way, so a single long
recording gives every part different footage.

Rendering is blocked while the library is empty, with the reason on the page —
otherwise every part fails separately at the materials stage, minutes after the
click that caused it.

## Subtitles

Defaults are thick yellow on a black outline: white disappears into bright
gameplay footage, and a thin outline disappears into everything.

| Setting | Default | Meaning |
| --- | --- | --- |
| `reddit_text_color` | `#FFFF00` | Fill colour of the caption |
| `reddit_text_thickness` | `thick` | Outline weight: thin, medium, thick, extra thick |
| `reddit_stroke_color` | `#000000` | Outline colour |
| `reddit_font_name` | `MicrosoftYaHeiBold.ttc` | Bold, so "thick" is thick letters too |
| `reddit_font_size` | `60` | Caption size in the 1080-wide frame |

Colour, thickness and size are on the page under *Parts and video*, with a
preview strip. They apply to renders started from the page and to
`scripts/reddit_recap.py`, which writes them into the cli.py manifest.

## Rejecting a story

Rejecting is where a story stops. With `reddit_reject_discards_videos = true`
(the default, and a checkbox in *Review*):

* every part still rendering is rejected too, so nothing promotes it into the
  review queue later;
* every video already written is deleted, and the file a still-running render
  produces afterwards is deleted by the worker when that task finishes — the
  task pool has no cancel, so the render finishes into the bin;
* the story itself stays: title, subreddit, permalink, and the narration of
  every part, readable under *All stories*.

That is the point of the setting — a rejected story is worth keeping as text
even when its footage is not worth the disk. Turn it off to keep the videos.

Post IDs of rejected stories stay in the dedup set either way, so a rejected
thread is not offered again on the next search.

## Part shape

`reddit_part_seconds` is a narration target, not a hard cut. The title (part 1)
and the follow-on line (every part but the last) are charged against the budget,
so parts land under the limit rather than over it. Splits only ever happen on
sentence boundaries; a single sentence longer than the budget becomes its own
part rather than being cut in half.

Duration is estimated from word count at `reddit_words_per_minute` (150 by
default). If you raise `voice_rate` above 1.0, raise this too or parts will
render longer than the target.

Stories needing more than `reddit_max_parts` are skipped by default, rather than
published stopping mid-plot. Set `reddit_skip_truncated = false` to keep them.

## What the normalizer does

Reddit self-text is markdown with subreddit shorthand in it. Both are handled in
`app/services/reddit_script.py`:

- markdown stripped — links keep their text, bare URLs are dropped, along with
  headings, quotes, bullets, emphasis, code and spoiler tags;
- HTML entities unescaped twice, because Reddit double-escapes some bodies and
  one pass leaves `&#x200B;` in the text for the voice engine to read out;
- unterminated lines get a full stop, so bullet lists split into sentences
  instead of running together;
- a trailing `EDIT:` or `TL;DR` block is dropped — it usually gives away the
  ending — but only when it sits in the last 40% of the post, so a story that
  opens with `UPDATE:` survives;
- shorthand expanded: `AITA`, `NTA`, `MIL`, `IMO` and friends, plus age and
  gender tokens, so `(28F)` is read as "28 female" rather than spelled out.

Only the uppercase form is matched, so ordinary words like "so", "op" and "info"
are left alone. Add your own with `[app.reddit_jargon]` in `config.toml`.

## Tests

```bash
uv run python -m pytest test/services/test_reddit_*.py \
  test/services/test_upload_post.py
```

## Worth knowing

**Reddit's terms** cover how fetched content may be used regardless of which
backend fetched it, and both YouTube and TikTok have policies on repetitive
mass-produced uploads. Volume and template-sameness are the practical risk
factors.

**Attribution.** The permalink is stored on every queued post and goes into the
YouTube description by default via `reddit_caption_template` and the scheduler.

**Gameplay footage is somebody's recording.** A Minecraft parkour clip pulled
from YouTube belongs to whoever made it, and both YouTube and TikTok act on
reused content. Record your own, or use footage licensed for reuse.

**Dedup is by post ID**, kept in `storage/reddit_recaps.json` and capped at 1000
posts, oldest dropped first. Delete that file and the next run will happily
recap everything again — it is also what the *All stories* tab lists, so
deleting it wipes the history along with the dedup.

**Job state lives in `storage/reddit_jobs.json`**, one record per job kind. A
job left `running` by a server restart is marked failed the next time it is
read, rather than leaving the page waiting on a thread that no longer exists.
