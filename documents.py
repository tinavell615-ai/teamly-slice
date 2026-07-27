# -*- coding: utf-8 -*-
"""
A. Загрузка и хранение документов рукописей.
Хранение: Upstash Redis (переживает редеплой).
Форматы: .docx, .txt, .md
"""
from __future__ import annotations

import io
import json
import os
import re
import time
import uuid
from typing import Any

import requests

UPSTASH_REDIS_REST_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
UPSTASH_REDIS_REST_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")

# ~1800 знаков ≈ 1 страница рукописи (грубо для RU)
CHARS_PER_PAGE = 1800
# ~1.5 символа на token для русского (оценка)
CHARS_PER_TOKEN = 1.5

CHAPTER_TITLE_RE = re.compile(
    r"(?m)^(?:"
    r"глава\s+\d+"
    r"|глава\s+[ivxlcdm]+"
    r"|chapter\s+\d+"
    r"|часть\s+\d+"
    r"|§\s*\d+"
    r"|\d+\.\s+\S+"
    r")",
    re.IGNORECASE,
)


# ---------- Redis ----------

def _redis_headers() -> dict:
    return {"Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"}


def redis_command(*args, timeout: int | None = None) -> Any:
    """
    Универсальная команда Upstash REST: POST body = ["CMD", arg1, ...].
    Единственная точка транспорта. redis_get / redis_set и SCAN идут через неё.
    Возвращает result или raises RuntimeError с телом ответа.
    timeout: для SET — 60 с (большие тексты рукописей), остальное — 30 с.
    """
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        raise RuntimeError("Redis не настроен (UPSTASH_*)")
    if timeout is None:
        timeout = 60 if (args and str(args[0]).upper() == "SET") else 30
    r = requests.post(
        UPSTASH_REDIS_REST_URL,
        headers={**_redis_headers(), "Content-Type": "application/json"},
        json=list(args),
        timeout=timeout,
    )
    if r.status_code != 200:
        raise RuntimeError(f"redis_command {args[0] if args else '?'}: HTTP {r.status_code} {r.text[:300]}")
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"redis_command {args[0] if args else '?'}: {data['error']}")
    return data.get("result")


def redis_get(key: str) -> Any | None:
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        return None
    try:
        result = redis_command("GET", key)
        if result is None:
            return None
        if isinstance(result, str):
            try:
                return json.loads(result)
            except json.JSONDecodeError:
                return result
        return result
    except Exception as e:
        print(f"[docs] redis_get {key}: {e}")
        return None


def redis_set(key: str, value: Any) -> bool:
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        print("[docs] Redis не настроен")
        return False
    try:
        payload = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        redis_command("SET", key, payload)  # timeout=60 по умолчанию для SET
        return True
    except Exception as e:
        print(f"[docs] redis_set {key}: {e}")
        return False


def redis_scan(match: str, count: int = 100, max_iterations: int = 50) -> list[str]:
    """
    SCAN курсором. Возвращает список уникальных ключей, подходящих под match.
    Бросает RuntimeError, если команда недоступна или обход не завершился
    за max_iterations (защита от зацикливания).
    """
    seen: set[str] = set()
    keys: list[str] = []
    cursor = "0"
    for _ in range(max_iterations):
        # Upstash: SCAN cursor MATCH pattern COUNT n → [next_cursor, [key, ...]]
        result = redis_command("SCAN", cursor, "MATCH", match, "COUNT", str(count))
        if not isinstance(result, (list, tuple)) or len(result) < 2:
            raise RuntimeError(f"SCAN unexpected result: {result!r}")
        cursor = str(result[0])
        batch = result[1] or []
        for k in batch:
            sk = str(k)
            if sk not in seen:
                seen.add(sk)
                keys.append(sk)
        if cursor == "0":
            return keys
    raise RuntimeError(
        f"SCAN не завершился за {max_iterations} итераций (match={match!r}, last_cursor={cursor!r})"
    )


def _index_key(project: str) -> str:
    return f"docs:{project}:index"


def _meta_key(project: str, doc_id: str) -> str:
    return f"docs:{project}:{doc_id}:meta"


def _text_key(project: str, doc_id: str) -> str:
    return f"docs:{project}:{doc_id}:text"


def _chapters_key(project: str, doc_id: str) -> str:
    return f"docs:{project}:{doc_id}:chapters"


def _chunks_key(project: str, doc_id: str) -> str:
    return f"docs:{project}:{doc_id}:chunks"


# ---------- Parse ----------

def extract_text_txt(raw: bytes) -> str:
    for enc in ("utf-8", "utf-8-sig", "cp1251", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def extract_text_docx(raw: bytes) -> tuple[str, list[dict]]:
    """
    Возвращает (полный_текст, главы[{title, start_char, end_char, text}]).
    Без python-docx: zipfile + XML. Heading по pStyle, иначе regex «Глава N».
    """
    import zipfile
    import xml.etree.ElementTree as ET

    NS = {
        "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    }

    paragraphs: list[tuple[str, bool]] = []  # (text, is_heading)
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            xml_bytes = zf.read("word/document.xml")
    except Exception as e:
        raise ValueError(f"не удалось прочитать docx: {e}") from e

    root = ET.fromstring(xml_bytes)
    for p_el in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"):
        texts = []
        for t_el in p_el.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"):
            if t_el.text:
                texts.append(t_el.text)
            if t_el.tail:
                texts.append(t_el.tail)
        text = "".join(texts).strip()
        style_name = ""
        pPr = p_el.find("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}pPr")
        if pPr is not None:
            pStyle = pPr.find("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}pStyle")
            if pStyle is not None:
                style_name = pStyle.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val") or ""
        is_heading = style_name.lower().startswith("heading") or style_name.startswith("Заголовок")
        if not is_heading and text and CHAPTER_TITLE_RE.match(text) and len(text) < 120:
            is_heading = True
        paragraphs.append((text, is_heading))

    # Собрать полный текст и границы
    full_parts: list[str] = []
    chapter_starts: list[tuple[int, str]] = []  # (char_offset, title)

    offset = 0
    for text, is_heading in paragraphs:
        if not text:
            full_parts.append("")
            offset += 1  # \n
            continue
        if is_heading:
            chapter_starts.append((offset, text))
        full_parts.append(text)
        offset += len(text) + 1  # + newline

    full_text = "\n".join(full_parts)

    if not chapter_starts:
        # одна «глава» = весь документ
        return full_text, [{
            "index": 1,
            "title": "Документ",
            "start_char": 0,
            "end_char": len(full_text),
            "text": full_text,
            "chars": len(full_text),
            "pages_est": max(1, round(len(full_text) / CHARS_PER_PAGE)),
        }]

    chapters = []
    for i, (start, title) in enumerate(chapter_starts):
        end = chapter_starts[i + 1][0] if i + 1 < len(chapter_starts) else len(full_text)
        chunk = full_text[start:end].strip()
        chapters.append({
            "index": i + 1,
            "title": title,
            "start_char": start,
            "end_char": end,
            "text": chunk,
            "chars": len(chunk),
            "pages_est": max(1, round(len(chunk) / CHARS_PER_PAGE)),
        })
    return full_text, chapters


def extract_chapters_from_plain(text: str) -> list[dict]:
    lines = text.splitlines()
    starts: list[tuple[int, str]] = []  # (line_index, title)
    for i, line in enumerate(lines):
        s = line.strip()
        if s and CHAPTER_TITLE_RE.match(s) and len(s) < 120:
            starts.append((i, s))

    if not starts:
        return [{
            "index": 1,
            "title": "Документ",
            "start_char": 0,
            "end_char": len(text),
            "text": text,
            "chars": len(text),
            "pages_est": max(1, round(len(text) / CHARS_PER_PAGE)),
        }]

    # char offsets
    line_starts = [0]
    for line in lines:
        line_starts.append(line_starts[-1] + len(line) + 1)

    chapters = []
    for i, (line_i, title) in enumerate(starts):
        start_char = line_starts[line_i]
        if i + 1 < len(starts):
            end_char = line_starts[starts[i + 1][0]]
        else:
            end_char = len(text)
        chunk = text[start_char:end_char].strip()
        chapters.append({
            "index": i + 1,
            "title": title,
            "start_char": start_char,
            "end_char": end_char,
            "text": chunk,
            "chars": len(chunk),
            "pages_est": max(1, round(len(chunk) / CHARS_PER_PAGE)),
        })
    return chapters


def parse_upload(filename: str, raw: bytes) -> dict:
    name = (filename or "document").lower()
    if name.endswith(".docx"):
        full_text, chapters = extract_text_docx(raw)
        fmt = "docx"
    elif name.endswith(".md"):
        full_text = extract_text_txt(raw)
        chapters = extract_chapters_from_plain(full_text)
        fmt = "md"
    else:
        full_text = extract_text_txt(raw)
        chapters = extract_chapters_from_plain(full_text)
        fmt = "txt"

    chars = len(full_text)
    return {
        "format": fmt,
        "text": full_text,
        "chapters": chapters,
        "chars": chars,
        "pages_est": max(1, round(chars / CHARS_PER_PAGE)),
        "tokens_est": max(1, round(chars / CHARS_PER_TOKEN)),
        "chapters_count": len(chapters),
    }


# ---------- CRUD ----------

def list_documents(project: str) -> list[dict]:
    index = redis_get(_index_key(project)) or []
    result = []
    for doc_id in index:
        meta = redis_get(_meta_key(project, doc_id))
        if meta:
            result.append(meta)
    return result


def get_document(project: str, doc_id: str, include_text: bool = False) -> dict | None:
    meta = redis_get(_meta_key(project, doc_id))
    if not meta:
        return None
    out = dict(meta)
    chapters = redis_get(_chapters_key(project, doc_id))
    if chapters:
        # без полных текстов глав в списке — только мета
        out["chapters"] = [
            {k: v for k, v in ch.items() if k != "text"}
            for ch in chapters
        ]
        out["chapters_full_available"] = True
    if include_text:
        out["text"] = redis_get(_text_key(project, doc_id)) or ""
        out["chapters"] = chapters or []
    return out


def get_chapter(project: str, doc_id: str, chapter_index: int) -> dict | None:
    chapters = redis_get(_chapters_key(project, doc_id)) or []
    for ch in chapters:
        if ch.get("index") == chapter_index:
            return ch
    return None


def save_document(project: str, filename: str, raw: bytes) -> dict:
    parsed = parse_upload(filename, raw)
    doc_id = str(uuid.uuid4())
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    meta = {
        "id": doc_id,
        "project": project,
        "filename": filename,
        "format": parsed["format"],
        "chars": parsed["chars"],
        "pages_est": parsed["pages_est"],
        "tokens_est": parsed["tokens_est"],
        "chapters_count": parsed["chapters_count"],
        "created_at": now,
        "status": {
            "phases": {},  # phase_id -> {state, updated_at}
            "last_phase": None,
        },
    }

    # главы без гигантского дублирования в meta
    chapters_store = parsed["chapters"]

    ok_meta = redis_set(_meta_key(project, doc_id), meta)
    ok_text = redis_set(_text_key(project, doc_id), parsed["text"])
    ok_ch = redis_set(_chapters_key(project, doc_id), chapters_store)

    index = redis_get(_index_key(project)) or []
    if doc_id not in index:
        index.insert(0, doc_id)
        redis_set(_index_key(project), index)

    if not (ok_meta and ok_text and ok_ch):
        meta["warning"] = "частичная запись в Redis — проверьте UPSTASH_*"

    return {
        "ok": True,
        "document": meta,
        "chapters_preview": [
            {"index": c["index"], "title": c["title"], "pages_est": c["pages_est"], "chars": c["chars"]}
            for c in chapters_store[:20]
        ],
    }


def delete_document(project: str, doc_id: str) -> bool:
    index = redis_get(_index_key(project)) or []
    index = [i for i in index if i != doc_id]
    redis_set(_index_key(project), index)
    # Upstash DEL
    if UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN:
        for key in (_meta_key(project, doc_id), _text_key(project, doc_id), _chapters_key(project, doc_id)):
            try:
                redis_command("DEL", key)
            except Exception:
                pass
    return True


# ---------- B. Нарезка на куски ----------
# Цель: ~12–20 стр. на кусок. Крупные главы режем с перекрытием ~2 стр.
# Мелкие (< 4 стр.) при желании склеиваем с соседней — пока только split.

TARGET_CHARS = 18 * CHARS_PER_PAGE   # ~18 стр.
MAX_CHARS = 25 * CHARS_PER_PAGE      # выше — режем
OVERLAP_CHARS = 2 * CHARS_PER_PAGE   # ~2 стр. перекрытия
MIN_CHARS = 4 * CHARS_PER_PAGE       # мельче — кандидат на склейку


def _split_text_by_paragraphs(text: str, max_chars: int, overlap_chars: int) -> list[str]:
    """Режет текст по границам абзацев, с перекрытием хвоста предыдущего куска."""
    if len(text) <= max_chars:
        return [text]

    paras = text.split("\n")
    chunks: list[str] = []
    buf: list[str] = []
    buf_len = 0

    def flush(with_overlap_from: str | None = None):
        nonlocal buf, buf_len
        body = "\n".join(buf).strip()
        if not body:
            buf, buf_len = [], 0
            return
        chunks.append(body)
        # подготовка overlap для следующего
        if overlap_chars > 0 and len(body) > overlap_chars:
            tail = body[-overlap_chars:]
            # выровнять по началу абзаца в хвосте
            nl = tail.find("\n")
            if nl != -1:
                tail = tail[nl + 1:]
            buf = [tail] if tail.strip() else []
            buf_len = len(tail)
        else:
            buf, buf_len = [], 0

    for para in paras:
        plen = len(para) + 1
        if buf_len + plen > max_chars and buf:
            flush()
        buf.append(para)
        buf_len += plen
    if buf:
        body = "\n".join(buf).strip()
        if body:
            chunks.append(body)
    return chunks or [text]


def build_chunks(project: str, doc_id: str) -> dict:
    """
    Строит куски по главам документа.
    id куска устойчивый: {doc_id}:c{chapter_index}:p{part}
    """
    chapters = redis_get(_chapters_key(project, doc_id))
    if not chapters:
        return {"ok": False, "error": "нет глав — документ не найден или не разобран"}

    chunks: list[dict] = []
    for ch in chapters:
        ch_idx = ch.get("index")
        title = ch.get("title") or f"Глава {ch_idx}"
        text = ch.get("text") or ""
        chars = len(text)

        if chars <= MAX_CHARS:
            parts = [text]
        else:
            parts = _split_text_by_paragraphs(text, TARGET_CHARS, OVERLAP_CHARS)

        for part_i, part_text in enumerate(parts, start=1):
            chunk_id = f"{doc_id}:c{ch_idx}:p{part_i}"
            chunks.append({
                "id": chunk_id,
                "doc_id": doc_id,
                "chapter_index": ch_idx,
                "chapter_title": title,
                "part": part_i,
                "parts_total": len(parts),
                "chars": len(part_text),
                "pages_est": max(1, round(len(part_text) / CHARS_PER_PAGE)),
                "tokens_est": max(1, round(len(part_text) / CHARS_PER_TOKEN)),
                "text": part_text,
                "status": "pending",  # pending | done | error | skipped
            })

    # мета без текста для списка
    meta_list = [{k: v for k, v in c.items() if k != "text"} for c in chunks]
    redis_set(_chunks_key(project, doc_id), chunks)

    # обновить meta документа
    doc_meta = redis_get(_meta_key(project, doc_id)) or {}
    doc_meta["chunks_count"] = len(chunks)
    doc_meta["chunks_built_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    redis_set(_meta_key(project, doc_id), doc_meta)

    return {
        "ok": True,
        "doc_id": doc_id,
        "chunks_count": len(chunks),
        "chunks": meta_list,
        "params": {
            "target_pages": TARGET_CHARS // CHARS_PER_PAGE,
            "max_pages": MAX_CHARS // CHARS_PER_PAGE,
            "overlap_pages": OVERLAP_CHARS // CHARS_PER_PAGE,
        },
    }


def list_chunks(project: str, doc_id: str, include_text: bool = False) -> list[dict]:
    chunks = redis_get(_chunks_key(project, doc_id)) or []
    if include_text:
        return chunks
    return [{k: v for k, v in c.items() if k != "text"} for c in chunks]


def get_chunk(project: str, doc_id: str, chunk_id: str) -> dict | None:
    chunks = redis_get(_chunks_key(project, doc_id)) or []
    for c in chunks:
        if c.get("id") == chunk_id:
            return c
    return None
