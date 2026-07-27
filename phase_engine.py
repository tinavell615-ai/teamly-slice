# -*- coding: utf-8 -*-
"""
C. Движок фаз — минимальный контур.
Сначала один кусок (боевой тест), потом цикл по всем.
"""
from __future__ import annotations

import time
from typing import Any

from documents import get_chunk, list_chunks, redis_get, redis_set, _meta_key
from llm import chat_completion, parse_json_content, llm_configured, LLM_MODEL
from prompts import build_messages, PHASES
from names import names_compatible as _names_compatible


def _known_key(project: str, doc_id: str) -> str:
    return f"docs:{project}:{doc_id}:known"


def _phase_result_key(project: str, doc_id: str, phase: int) -> str:
    return f"docs:{project}:{doc_id}:phase:{phase}"


def get_known_entities(project: str, doc_id: str) -> dict[str, list[str]]:
    data = redis_get(_known_key(project, doc_id))
    return data if isinstance(data, dict) else {}


def merge_known_from_delta(known: dict[str, list[str]], delta_items: list[dict]) -> dict[str, list[str]]:
    out = {k: list(v) for k, v in known.items()}
    for item in delta_items or []:
        table = item.get("table")
        title = (item.get("title") or "").strip()
        if not table or not title:
            continue
        bucket = out.setdefault(table, [])
        title_cf = title.casefold().strip()

        # точный дубль
        if any(b.casefold().strip() == title_cf for b in bucket):
            continue

        # новое полное имя поглощает короткое («Том Реддл» вместо «Том»)
        absorbed = False
        for i, b in enumerate(list(bucket)):
            if _names_compatible(b, title) and len(title) > len(b):
                bucket[i] = title
                absorbed = True
                break
            if _names_compatible(title, b) and len(b) >= len(title):
                # короткое при уже полном — не добавляем
                absorbed = True
                break
        if not absorbed:
            bucket.append(title)
    return out


def run_chunk(
    project: str,
    doc_id: str,
    chunk_id: str,
    phase: int,
    *,
    author_answers: list[dict] | None = None,
) -> dict[str, Any]:
    if phase not in PHASES:
        return {"ok": False, "error": f"unknown phase {phase}"}
    if not llm_configured():
        return {"ok": False, "error": "LLM_API_KEY не задан в Variables"}

    chunk = get_chunk(project, doc_id, chunk_id)
    if not chunk:
        return {"ok": False, "error": f"chunk not found: {chunk_id}"}

    known = get_known_entities(project, doc_id)
    messages_pkg = build_messages(
        phase, chunk, known_entities=known, author_answers=author_answers or []
    )

    llm_result = chat_completion(messages_pkg["messages"], json_mode=True)
    if not llm_result.get("ok"):
        return {
            "ok": False,
            "error": llm_result.get("error"),
            "llm": llm_result,
            "chunk_id": chunk_id,
            "phase": phase,
        }

    parsed = parse_json_content(llm_result.get("content"))
    if not parsed.get("ok"):
        return {
            "ok": False,
            "error": parsed.get("error"),
            "raw_content": llm_result.get("content"),
            "llm": {"usage": llm_result.get("usage"), "latency_ms": llm_result.get("latency_ms")},
            "chunk_id": chunk_id,
            "phase": phase,
        }

    data = parsed["data"] or {}
    delta = data.get("delta") or []
    questions = data.get("questions") or []
    candidates = data.get("candidates") or []
    not_taken = data.get("not_taken") or []

    # Жёсткий фильтр: только таблицы текущей фазы
    phase_tables = PHASES.get(phase, {}).get("tables") or []
    if phase_tables and phase_tables != ["*"]:
        allowed = set(phase_tables)
        kept, rejected = [], []
        for item in delta:
            tbl = item.get("table")
            if tbl in allowed:
                kept.append(item)
            else:
                rejected.append(item)
                not_taken.append({
                    "mention": item.get("title") or tbl,
                    "why": f"не таблица фазы {phase} (отклонено сервером)",
                    "table_attempted": tbl,
                })
        delta = kept
        data["delta_rejected"] = rejected

    # обновить known
    new_known = merge_known_from_delta(known, delta)
    redis_set(_known_key(project, doc_id), new_known)

    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "chunk_id": chunk_id,
        "phase": phase,
        "delta": delta,
        "questions": questions,
        "candidates": candidates,
        "not_taken": not_taken,
        "usage": llm_result.get("usage"),
        "latency_ms": llm_result.get("latency_ms"),
        "model": llm_result.get("model") or LLM_MODEL,
    }

    # журнал фазы: append
    journal = redis_get(_phase_result_key(project, doc_id, phase)) or {"chunks": []}
    if not isinstance(journal, dict):
        journal = {"chunks": []}
    chunks_log = journal.get("chunks") or []
    chunks_log = [c for c in chunks_log if c.get("chunk_id") != chunk_id]
    chunks_log.append(record)
    journal["chunks"] = chunks_log
    journal["updated_at"] = record["ts"]
    redis_set(_phase_result_key(project, doc_id, phase), journal)

    # meta status
    meta = redis_get(_meta_key(project, doc_id)) or {}
    status = meta.get("status") or {}
    phases = status.get("phases") or {}
    phases[str(phase)] = {"state": "partial", "updated_at": record["ts"], "chunks_done": len(chunks_log)}
    status["phases"] = phases
    status["last_phase"] = phase
    meta["status"] = status
    redis_set(_meta_key(project, doc_id), meta)

    return {
        "ok": True,
        "chunk_id": chunk_id,
        "phase": phase,
        "phase_name": PHASES[phase]["name"],
        "delta_count": len(delta),
        "questions_count": len(questions),
        "candidates_count": len(candidates),
        "not_taken_count": len(not_taken),
        "delta": delta,
        "questions": questions,
        "candidates": candidates,
        "not_taken": not_taken,
        "known_entities": new_known,
        "usage": llm_result.get("usage"),
        "latency_ms": llm_result.get("latency_ms"),
        "model": llm_result.get("model"),
    }


def run_phase_all_chunks(
    project: str,
    doc_id: str,
    phase: int,
    *,
    max_chunks: int | None = None,
) -> dict[str, Any]:
    chunks = list_chunks(project, doc_id, include_text=False)
    if not chunks:
        return {"ok": False, "error": "нет кусков — сначала «Куски»"}

    results = []
    errors = []
    for i, ch in enumerate(chunks):
        if max_chunks is not None and i >= max_chunks:
            break
        r = run_chunk(project, doc_id, ch["id"], phase)
        results.append({
            "chunk_id": ch["id"],
            "ok": r.get("ok"),
            "delta_count": r.get("delta_count"),
            "error": r.get("error"),
            "latency_ms": r.get("latency_ms"),
        })
        if not r.get("ok"):
            errors.append(r)
            # стоп на первой ошибке LLM — не жечь баланс
            break
        # пауза между запросами
        time.sleep(0.5)

    return {
        "ok": len(errors) == 0 and len(results) > 0,
        "phase": phase,
        "processed": len(results),
        "results": results,
        "stopped_on_error": errors[0] if errors else None,
        "known_entities": get_known_entities(project, doc_id),
    }


# ---------- Фоновые задания ----------
import threading
import uuid

# _jobs / _jobs_lock удалены (слой 1)


def _job_key(job_id: str) -> str:
    return f"jobs:phase:{job_id}"


def start_phase_job(
    project: str,
    doc_id: str,
    phase: int,
    *,
    max_chunks: int | None = 1,
    chunk_id: str | None = None,
) -> dict:
    job_id = str(uuid.uuid4())
    job = {
        "id": job_id,
        "status": "queued",
        "project": project,
        "doc_id": doc_id,
        "phase": phase,
        "max_chunks": max_chunks,
        "chunk_id": chunk_id,
        "result": None,
        "error": None,
        "started_at": None,
        "finished_at": None,
    }
    redis_set(_job_key(job_id), job)

    def worker():
        job["status"] = "running"
        job["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        job["progress"] = {"done": 0, "total": None, "last_chunk": None}
        redis_set(_job_key(job_id), job)
        try:
            if chunk_id:
                result = run_chunk(project, doc_id, chunk_id, phase)
                job["progress"] = {"done": 1, "total": 1, "last_chunk": chunk_id}
            else:
                # пошагово, с записью прогресса
                from documents import list_chunks
                chunks = list_chunks(project, doc_id, include_text=False)
                if max_chunks is not None:
                    chunks = chunks[: int(max_chunks)]
                job["progress"]["total"] = len(chunks)
                redis_set(_job_key(job_id), job)
                results = []
                errors = []
                for i, ch in enumerate(chunks):
                    job["progress"]["last_chunk"] = ch["id"]
                    job["progress"]["done"] = i  # текущий в работе
                    redis_set(_job_key(job_id), job)
                    r = run_chunk(project, doc_id, ch["id"], phase)
                    results.append({
                        "chunk_id": ch["id"],
                        "ok": r.get("ok"),
                        "delta_count": r.get("delta_count"),
                        "error": r.get("error"),
                        "latency_ms": r.get("latency_ms"),
                    })
                    job["progress"]["done"] = i + 1
                    redis_set(_job_key(job_id), job)
                    if not r.get("ok"):
                        errors.append(r)
                        break
                    time.sleep(0.5)
                result = {
                    "ok": len(errors) == 0 and len(results) > 0,
                    "phase": phase,
                    "processed": len(results),
                    "results": results,
                    "stopped_on_error": errors[0] if errors else None,
                    "known_entities": get_known_entities(project, doc_id),
                }
            job["result"] = result
            job["status"] = "done" if result.get("ok") else "error"
            if not result.get("ok"):
                job["error"] = result.get("error") or (result.get("stopped_on_error") or {}).get("error")
        except Exception as e:
            job["status"] = "error"
            job["error"] = str(e)
            job["result"] = {"ok": False, "error": str(e)}
        job["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        redis_set(_job_key(job_id), job)

    threading.Thread(target=worker, daemon=True).start()
    return {"ok": True, "job_id": job_id, "status": "queued"}


STALE_AFTER_SECONDS = 600


def _is_job_stale(job: dict) -> tuple[bool, int]:
    """
    Единая проверка: зависло ли задание.
    Возвращает (is_stale, age_seconds). Порог — STALE_AFTER_SECONDS.
    """
    if not job or job.get("status") != "running":
        return False, 0
    started = job.get("started_at")
    if not started:
        return False, 0
    try:
        from datetime import datetime, timezone
        t0 = datetime.strptime(started, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - t0).total_seconds()
        return age > STALE_AFTER_SECONDS, int(age)
    except Exception:
        return False, 0


def _mark_job_stale(job: dict, age: int, reason: str = "") -> dict:
    """Записывает status=stale и текст ошибки. Возвращает обновлённый job."""
    job = dict(job)
    job["status"] = "stale"
    suffix = f", {reason}" if reason else ""
    job["error"] = job.get("error") or (
        f"зависшее задание (running {age}s > {STALE_AFTER_SECONDS}s{suffix})"
    )
    job["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return job


def get_phase_job(job_id: str) -> dict | None:
    job = redis_get(_job_key(job_id))
    if not job or not isinstance(job, dict):
        return job
    is_stale, age = _is_job_stale(job)
    if is_stale:
        job = _mark_job_stale(job, age)
        redis_set(_job_key(job_id), job)
    return job


# Результат обхода при старте (для /selfcheck).
# None = ещё не запускали; {"status": "running"} = идёт; иначе итоговый dict.
_STALE_STARTUP_RESULT: dict | None = None


def mark_stale_jobs_on_startup() -> dict:
    """
    SCAN jobs:phase:*, пометить running старше STALE_AFTER_SECONDS как stale.
    Возвращает {"ok": True, "scanned": N, "marked": M} или {"ok": False, "error": "..."}.
    Полное возобновление заданий в пакет не входит.
    """
    global _STALE_STARTUP_RESULT
    _STALE_STARTUP_RESULT = {"status": "running"}
    try:
        from documents import redis_scan, redis_get, redis_set
        keys = redis_scan("jobs:phase:*", count=50, max_iterations=30)
        marked = 0
        for key in keys:
            job = redis_get(key)
            if not job or not isinstance(job, dict):
                continue
            is_stale, age = _is_job_stale(job)
            if is_stale:
                job = _mark_job_stale(job, age, reason="startup")
                redis_set(key, job)
                marked += 1
        result = {"ok": True, "scanned": len(keys), "marked": marked}
    except Exception as e:
        result = {"ok": False, "error": str(e)}
    _STALE_STARTUP_RESULT = result
    return result


def get_stale_startup_status() -> dict | None:
    """Для /selfcheck: результат mark_stale_jobs_on_startup (или status=running)."""
    return _STALE_STARTUP_RESULT


def _run_stale_scan_in_background() -> None:
    """Фоновый поток: обход не блокирует импорт и старт gunicorn."""
    import threading
    def worker():
        try:
            mark_stale_jobs_on_startup()
        except Exception as e:
            global _STALE_STARTUP_RESULT
            _STALE_STARTUP_RESULT = {"ok": False, "error": str(e)}
    t = threading.Thread(target=worker, daemon=True, name="stale-jobs-scan")
    t.start()


# При импорте — только запуск фона. Старт процесса не ждёт Upstash.
_run_stale_scan_in_background()


def reset_known(project: str, doc_id: str) -> dict:
    """Сброс накопленных known_entities и журналов фаз для чистого прогона."""
    redis_set(_known_key(project, doc_id), {})
    # журналы фаз 0–11
    for ph in range(0, 12):
        redis_set(_phase_result_key(project, doc_id, ph), {"chunks": []})
    meta = redis_get(_meta_key(project, doc_id)) or {}
    if "status" in meta:
        meta["status"] = {"phases": {}, "last_phase": None}
        redis_set(_meta_key(project, doc_id), meta)
    return {"ok": True, "doc_id": doc_id, "known_entities": {}, "phases_cleared": list(range(0, 12))}
