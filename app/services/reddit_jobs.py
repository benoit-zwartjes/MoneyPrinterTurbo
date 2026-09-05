"""
Server-side background jobs for the Reddit recap workflow.

Every long step of the workflow — finding stories, scheduling uploads — runs in
a daemon thread owned by the server process, and its state is written to
``storage/reddit_jobs.json``. The page only ever reads that file, so a job
survives a browser refresh, a lost websocket, a phone locking itself, or a
second tab opening the page. Nothing is held in ``st.session_state``, which is
per browser session and is exactly what made "Find stories" look like it never
finished: the fetch ran inside the page script, and any interruption threw the
result away with no trace that it had ever run.

    start_discovery()   fetch → filter → split, results persisted for the page
    start_scheduling()  hand approved parts to Upload-Post on a calendar
    ensure_worker()     the loop that advances renders and uploads unattended

The worker is what makes the queue move without anybody watching it: renders
finish in the task pool and uploads finish on Upload-Post's side, and both need
somebody to notice. Before this, only a page render did that.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from uuid import uuid4

from loguru import logger

from app.services import reddit_pipeline, state as sm, upload_post
from app.utils import utils


JOBS_FILE_NAME = "reddit_jobs.json"

JOB_DISCOVER = "discover"
JOB_SCHEDULE = "schedule"
JOB_KINDS = (JOB_DISCOVER, JOB_SCHEDULE)

STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
JOB_STATUSES = (STATUS_RUNNING, STATUS_COMPLETED, STATUS_FAILED)

# How often the worker promotes finished renders. Renders take minutes, so a
# tighter loop would only add file writes.
WORKER_INTERVAL_SECONDS = 10
# Upload-Post is a paid third-party API and a scheduled slot is hours away;
# polling every render tick would burn quota for nothing.
UPLOAD_SYNC_INTERVAL_SECONDS = 300

# Guards the job file, the thread registry and the worker handle together: a
# read that reconciles a stale record must not race a start that is registering
# its thread.
_jobs_lock = threading.RLock()
_job_threads: dict[str, threading.Thread] = {}
_worker: threading.Thread | None = None
_worker_stop = threading.Event()


def _jobs_path() -> str:
    return os.path.join(utils.storage_dir("", create=True), JOBS_FILE_NAME)


def _empty_store() -> dict:
    return {"version": 1, "jobs": {}}


def _normalize_job(kind: str, raw) -> dict | None:
    if not isinstance(raw, dict):
        return None

    status = raw.get("status")
    if status not in JOB_STATUSES:
        status = STATUS_FAILED

    def _float_or_none(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    return {
        "kind": kind,
        "job_id": str(raw.get("job_id", "") or ""),
        "status": status,
        "message": str(raw.get("message", "") or ""),
        "progress": max(0, min(100, int(_float_or_none(raw.get("progress")) or 0))),
        "started_at": _float_or_none(raw.get("started_at")),
        "finished_at": _float_or_none(raw.get("finished_at")),
        "error": str(raw.get("error")) if raw.get("error") else None,
        "result": raw.get("result") if isinstance(raw.get("result"), dict) else {},
        "pid": raw.get("pid"),
    }


def load_jobs() -> dict:
    """Read the job store; a missing or corrupt file yields an empty one."""
    try:
        with open(_jobs_path(), "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return _empty_store()
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        logger.warning(f"failed to read the reddit job store, starting empty: {exc}")
        return _empty_store()

    raw_jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(raw_jobs, dict):
        return _empty_store()

    jobs = {}
    for kind, raw in raw_jobs.items():
        if kind not in JOB_KINDS:
            continue
        normalized = _normalize_job(kind, raw)
        if normalized:
            jobs[kind] = normalized
    return {"version": 1, "jobs": jobs}


def _save_jobs(store: dict) -> bool:
    path = _jobs_path()
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=os.path.dirname(path),
            prefix=".reddit_jobs-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = handle.name
            json.dump(store, handle, ensure_ascii=False, indent=2)
        os.replace(temp_path, path)
        temp_path = None
        return True
    except OSError as exc:
        logger.warning(f"failed to write the reddit job store: {exc}")
        return False
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def _write_job(kind: str, **fields) -> dict:
    """Merge ``fields`` into the stored record for ``kind`` and return it."""
    with _jobs_lock:
        store = load_jobs()
        record = store["jobs"].get(kind) or _normalize_job(kind, {"status": STATUS_FAILED})
        record.update(fields)
        record["kind"] = kind
        store["jobs"][kind] = record
        _save_jobs(store)
        return dict(record)


def _reconcile(kind: str, record: dict | None) -> dict | None:
    """
    Turn a job left ``running`` by a dead process into a terminal record.

    A restart mid-fetch is the one way a job can stop without writing its own
    outcome, and a record stuck on ``running`` would leave the page waiting on
    a thread that no longer exists, with the button disabled forever.
    """
    if not record or record["status"] != STATUS_RUNNING:
        return record

    thread = _job_threads.get(kind)
    if record.get("pid") == os.getpid() and thread is not None and thread.is_alive():
        return record

    logger.warning(f"reddit job '{kind}' did not survive; marking it failed")
    return _write_job(
        kind,
        status=STATUS_FAILED,
        error="the server stopped while this job was running",
        finished_at=time.time(),
    )


def get_job(kind: str) -> dict | None:
    """The latest record for one job kind, reconciled against reality."""
    with _jobs_lock:
        return _reconcile(kind, load_jobs()["jobs"].get(kind))


def all_jobs() -> dict:
    with _jobs_lock:
        return {kind: _reconcile(kind, job) for kind, job in load_jobs()["jobs"].items()}


def is_running(kind: str) -> bool:
    job = get_job(kind)
    return bool(job and job["status"] == STATUS_RUNNING)


def _start(kind: str, target, message: str) -> dict:
    """
    Run ``target(report)`` in a daemon thread, recording its outcome.

    ``report(message=..., progress=...)`` updates the record while the job
    runs; whatever ``target`` returns is stored as the result. The thread is
    registered before it starts so a concurrent read cannot see a running
    record with no thread behind it and declare it dead.
    """
    with _jobs_lock:
        if is_running(kind):
            return get_job(kind)

        job_id = str(uuid4())
        record = _write_job(
            kind,
            job_id=job_id,
            status=STATUS_RUNNING,
            message=message,
            progress=0,
            started_at=time.time(),
            finished_at=None,
            error=None,
            result={},
            pid=os.getpid(),
        )

        def report(message: str | None = None, progress: int | None = None) -> None:
            fields = {}
            if message is not None:
                fields["message"] = message
            if progress is not None:
                fields["progress"] = max(0, min(100, int(progress)))
            if fields:
                _write_job(kind, **fields)

        def run() -> None:
            try:
                result = target(report) or {}
                _write_job(
                    kind,
                    status=STATUS_COMPLETED,
                    progress=100,
                    message=str(result.get("message", "") or ""),
                    result={k: v for k, v in result.items() if k != "message"},
                    finished_at=time.time(),
                    error=None,
                )
            except Exception as exc:
                # A job must always reach a terminal state. A page that shows
                # "running" forever is exactly the failure being fixed here, so
                # even an unexpected crash has to leave something readable.
                logger.exception(f"reddit job '{kind}' failed: {exc}")
                _write_job(
                    kind,
                    status=STATUS_FAILED,
                    error=f"{type(exc).__name__}: {exc}",
                    finished_at=time.time(),
                )

        thread = threading.Thread(target=run, name=f"reddit-job-{kind}", daemon=True)
        _job_threads[kind] = thread
        thread.start()
        return record


# -----------------------------------------------------------------------------
# Discovery
# -----------------------------------------------------------------------------


def start_discovery(options: dict) -> dict:
    """
    Find stories in the background, keeping the candidates on disk.

    ``options`` is snapshotted at call time: the job must not read settings the
    user changes while it runs.
    """
    snapshot = dict(options)

    def job(report) -> dict:
        report(message="Fetching listings…", progress=10)
        outcome = reddit_pipeline.discover_report(snapshot)
        report(message="Splitting stories into parts…", progress=80)

        candidates = outcome["candidates"]
        return {
            "message": (
                f"{len(candidates)} stories ready"
                if candidates
                else "No story matched the filters"
            ),
            "candidates": candidates,
            "fetched": outcome["fetched"],
            "matched": outcome["matched"],
            "skipped_empty": outcome["skipped_empty"],
            "skipped_truncated": outcome["skipped_truncated"],
            "options": snapshot,
        }

    return _start(JOB_DISCOVER, job, "Starting…")


def discovered_candidates() -> list[dict]:
    """Stories from the last completed discovery run, or an empty list."""
    job = get_job(JOB_DISCOVER)
    if not job or job["status"] != STATUS_COMPLETED:
        return []
    candidates = job["result"].get("candidates")
    return candidates if isinstance(candidates, list) else []


def clear_candidates() -> None:
    """
    Drop the candidate list once its stories have been handed to rendering.

    The run is marked consumed rather than deleted, so the page can say "those
    went to render" instead of re-reading the counts and concluding the search
    found nothing.
    """
    with _jobs_lock:
        job = get_job(JOB_DISCOVER)
        if not job:
            return
        result = dict(job["result"])
        result["candidates"] = []
        result["consumed"] = True
        _write_job(JOB_DISCOVER, result=result)


# -----------------------------------------------------------------------------
# Scheduling
# -----------------------------------------------------------------------------


def start_scheduling(parts: list[dict], slots: list[str], platforms: list[str]) -> dict:
    """
    Upload approved parts to Upload-Post in the background.

    Each part uploads a whole video file, which can take minutes; done inside
    the page script it blocks the browser and dies with the websocket.
    """
    payload = [dict(part) for part in parts]
    slot_list = [str(slot) for slot in slots]
    platform_list = [str(platform) for platform in platforms]

    def job(report) -> dict:
        total = len(payload)

        def progress(position: int, part: dict) -> None:
            report(
                message=(
                    f"Uploading {position}/{total}: "
                    f"{str(part.get('title', ''))[:60]} "
                    f"part {part.get('index')}/{part.get('total')}"
                ),
                progress=int(position * 100 / max(total, 1)),
            )

        outcome = reddit_pipeline.schedule_parts(
            payload, slot_list, platform_list, on_part=progress
        )
        return {
            "message": (
                f"{outcome['scheduled']} scheduled, {outcome['failed']} failed"
                if outcome["failed"]
                else f"{outcome['scheduled']} parts scheduled"
            ),
            **outcome,
        }

    return _start(JOB_SCHEDULE, job, "Starting…")


# -----------------------------------------------------------------------------
# The unattended worker
# -----------------------------------------------------------------------------


_last_upload_sync = 0.0


def sync_once(poll_uploads: bool | None = None) -> dict:
    """
    One pass of the work that has to happen whether or not anyone is looking.

    Renders finish in the task pool, uploads finish on Upload-Post's side, and
    a render whose story was rejected still writes a file; all three leave
    something stale until a pass reads them. Promoting a
    render is a local file read, so it happens every pass; polling uploads is a
    paid API call per scheduled part, so by default it only happens once the
    interval is due. ``poll_uploads`` forces or suppresses that decision.
    """
    global _last_upload_sync

    outcome = {
        "rendered": 0,
        "render_failed": 0,
        "uploaded": 0,
        "upload_failed": 0,
        "purged": 0,
    }

    rendering = reddit_pipeline.sync_rendering(sm.state.get_task)
    outcome["rendered"] = rendering["rendered"]
    outcome["render_failed"] = rendering["failed"]

    # A story rejected mid-render leaves its video behind when the task
    # finishes; this is what clears it.
    outcome["purged"] = reddit_pipeline.purge_discarded(sm.state.get_task)["purged"]

    due = (
        poll_uploads
        if poll_uploads is not None
        else (time.time() - _last_upload_sync) >= UPLOAD_SYNC_INTERVAL_SECONDS
    )
    if due and upload_post.upload_post_service.is_configured():
        _last_upload_sync = time.time()
        uploads = reddit_pipeline.sync_uploads()
        outcome["uploaded"] = uploads["uploaded"]
        outcome["upload_failed"] = uploads["failed"]

    return outcome


def refresh_now() -> dict:
    """
    What a Refresh button in the page should call.

    Finished renders are promoted inline, because that is a local read and the
    user is waiting on the answer. The upload poll is a network call per
    scheduled part, so it is handed to the worker — which picks it up within
    one pass — and only runs inline when no worker is alive to do it.
    """
    global _last_upload_sync

    with _jobs_lock:
        worker_alive = _worker is not None and _worker.is_alive()
        if worker_alive:
            _last_upload_sync = 0.0

    return sync_once(poll_uploads=not worker_alive)


def _worker_loop() -> None:
    logger.info("reddit recap worker started")
    while not _worker_stop.is_set():
        try:
            result = sync_once()
            if any(result.values()):
                logger.info(f"reddit recap worker: {result}")
        except Exception as exc:
            # The worker outlives the process's other threads; one bad pass
            # must never end it, or the queue silently stops moving.
            logger.exception(f"reddit recap worker pass failed: {exc}")
        _worker_stop.wait(WORKER_INTERVAL_SECONDS)
    logger.info("reddit recap worker stopped")


def ensure_worker() -> bool:
    """Start the worker once per process. Returns True when it is running."""
    global _worker
    with _jobs_lock:
        if _worker is not None and _worker.is_alive():
            return True
        _worker_stop.clear()
        _worker = threading.Thread(
            target=_worker_loop, name="reddit-recap-worker", daemon=True
        )
        _worker.start()
        return True


def stop_worker(timeout: float = 5.0) -> None:
    """Stop the worker. Only tests need this; the daemon dies with the process."""
    global _worker
    with _jobs_lock:
        worker = _worker
        _worker = None
    _worker_stop.set()
    if worker is not None and worker.is_alive():
        worker.join(timeout)
