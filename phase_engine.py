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
        # нормализация для антидубля
        titles_norm = {t.casefold().strip() for t in bucket}
        if title.casefold().strip() not in titles_norm:
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
