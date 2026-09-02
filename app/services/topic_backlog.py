"""
视频选题待办列表的磁盘存储。

热榜生成的选题需要跨会话保留：Streamlit 的 session_state 会在刷新或重启后
清空，而选题列表的价值恰恰在于"上次生成的还没拍完"。这里用一个 JSON 文件
保存列表，并记录每条选题是否已经产出过视频，避免重复选题。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from typing import Iterable

from loguru import logger

from app.utils import utils


BACKLOG_FILE_NAME = "topic_backlog.json"
# 单条选题来自 LLM，长度可控；上限只用于防止磁盘文件无限增长。
MAX_BACKLOG_ENTRIES = 500
MAX_SUBJECT_LENGTH = 300

STATUS_PENDING = "pending"
STATUS_MADE = "made"

# 读改写序列必须整体串行，否则两个并发的"标记已完成"会互相覆盖。跨进程
# 写入由临时文件加 os.replace 保证读取方只看到完整的旧文件或新文件。
_backlog_lock = threading.RLock()


def _backlog_path() -> str:
    return os.path.join(utils.storage_dir("", create=True), BACKLOG_FILE_NAME)


def _normalize_subject(subject: str) -> str:
    return " ".join(str(subject or "").split())[:MAX_SUBJECT_LENGTH]


def _normalize_entry(raw) -> dict | None:
    """把磁盘上的一条记录整理成受支持的结构，无法识别时丢弃。"""
    if not isinstance(raw, dict):
        return None
    subject = _normalize_subject(raw.get("subject"))
    if not subject:
        return None

    status = raw.get("status")
    if status not in {STATUS_PENDING, STATUS_MADE}:
        status = STATUS_PENDING

    def _float_or_none(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    task_id = raw.get("task_id")
    return {
        "subject": subject,
        "status": status,
        "created_at": _float_or_none(raw.get("created_at")) or time.time(),
        "made_at": _float_or_none(raw.get("made_at")),
        "task_id": str(task_id) if task_id else None,
    }


def load_backlog() -> list[dict]:
    """读取选题列表；文件缺失或损坏时返回空列表而不是抛出异常。"""
    path = _backlog_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        # 选题列表属于辅助数据。损坏时不能让整个 WebUI 无法打开，
        # 但必须留下日志，否则用户只会看到列表莫名其妙变空。
        logger.warning(f"failed to read topic backlog, treating as empty: {exc}")
        return []

    entries = payload.get("entries") if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        return []

    normalized = []
    seen: set[str] = set()
    for raw in entries:
        entry = _normalize_entry(raw)
        if entry is None:
            continue
        key = entry["subject"].casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(entry)
    return normalized


def _save_backlog(entries: list[dict]) -> bool:
    path = _backlog_path()
    temp_path = None
    try:
        payload = {"version": 1, "entries": entries[:MAX_BACKLOG_ENTRIES]}
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=os.path.dirname(path),
            prefix=".topic_backlog-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = handle.name
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(temp_path, path)
        temp_path = None
        return True
    except OSError as exc:
        logger.warning(f"failed to write topic backlog: {exc}")
        return False
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def add_subjects(subjects: Iterable[str]) -> int:
    """
    追加选题并返回真正新增的条数。

    与已有选题（含已完成的）按大小写不敏感去重，这样重复运行热榜生成不会
    把上次已经拍过的选题重新排进待办。
    """
    with _backlog_lock:
        entries = load_backlog()
        existing = {entry["subject"].casefold() for entry in entries}

        added = 0
        now = time.time()
        for subject in subjects:
            normalized = _normalize_subject(subject)
            if not normalized:
                continue
            key = normalized.casefold()
            if key in existing:
                continue
            if len(entries) >= MAX_BACKLOG_ENTRIES:
                logger.warning(
                    f"topic backlog reached {MAX_BACKLOG_ENTRIES} entries; "
                    "skipping the remaining subjects"
                )
                break
            existing.add(key)
            entries.append(
                {
                    "subject": normalized,
                    "status": STATUS_PENDING,
                    "created_at": now,
                    "made_at": None,
                    "task_id": None,
                }
            )
            added += 1

        if added:
            _save_backlog(entries)
        return added


def _set_status(subject: str, status: str, task_id: str | None) -> bool:
    normalized = _normalize_subject(subject)
    if not normalized:
        return False

    with _backlog_lock:
        entries = load_backlog()
        target = normalized.casefold()
        for entry in entries:
            if entry["subject"].casefold() != target:
                continue
            if entry["status"] == status and status == STATUS_PENDING:
                return False
            entry["status"] = status
            if status == STATUS_MADE:
                entry["made_at"] = time.time()
                entry["task_id"] = str(task_id) if task_id else entry.get("task_id")
            else:
                entry["made_at"] = None
                entry["task_id"] = None
            return _save_backlog(entries)
        return False


def mark_made(subject: str, task_id: str | None = None) -> bool:
    """把选题标记为已产出；选题不在列表中时返回 False。"""
    return _set_status(subject, STATUS_MADE, task_id)


def mark_pending(subject: str) -> bool:
    """撤销"已产出"标记，便于重新生成同一选题。"""
    return _set_status(subject, STATUS_PENDING, None)


def remove_subject(subject: str) -> bool:
    normalized = _normalize_subject(subject)
    if not normalized:
        return False

    with _backlog_lock:
        entries = load_backlog()
        target = normalized.casefold()
        remaining = [
            entry for entry in entries if entry["subject"].casefold() != target
        ]
        if len(remaining) == len(entries):
            return False
        return _save_backlog(remaining)


def clear_made() -> int:
    """移除全部已产出的选题，返回清理条数。"""
    with _backlog_lock:
        entries = load_backlog()
        remaining = [entry for entry in entries if entry["status"] != STATUS_MADE]
        removed = len(entries) - len(remaining)
        if removed:
            _save_backlog(remaining)
        return removed


def pending_subjects() -> list[str]:
    return [
        entry["subject"]
        for entry in load_backlog()
        if entry["status"] == STATUS_PENDING
    ]
