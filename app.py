from flask import Flask, request, Response, jsonify
import requests
import json
import os
import time
import threading
from datetime import datetime
from collections import defaultdict

app = Flask(__name__)

CLIENT_ID = os.environ.get("CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET", "")
INITIAL_REFRESH_TOKEN = os.environ.get("REFRESH_TOKEN", "")
UPSTASH_REDIS_REST_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
UPSTASH_REDIS_REST_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
SLUG = "tina-vell"
DEBUG_KEY = os.environ.get("DEBUG_KEY", "tvell-debug-2026")
CLUSTER = "https://app.teamly.ru"
TOKENS_KEY = "teamly_tokens"

# ===================== LLM PROVIDER (сменный) =====================
# Все поля из env — смена провайдера = правка Variables, не кода.
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com").rstrip("/")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "") or os.environ.get("DEEPSEEK_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-v4-flash")
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "openai_compatible")  # openai_compatible | yandex | gigachat


PROJECTS = {
    "burevestnik": {
        "name": "Буревестник",
        "tables": {
            "characters": "d0f91b04-7924-4fd2-9450-58cf6c12a89f",
            "world": "be3811c2-70cc-4581-9f95-3e512da235d9",
            "locations": "6d9b436c-e213-49a2-8bec-2d109cef7280",
            "events": "bd5891eb-976b-4f7b-8bf0-5cb19d53c302",
            "chapters": "c288e5e4-ae16-44e2-8937-63e0ed8dd748"
        }
    },
    "detective_v7": {
        "name": "Детективный движок v7 (тест)",
        "space_id": "846990cf-487f-4650-9cf1-f396492d2e17",
        "tables": {
            "world": "d024b1b2-f999-437b-affd-0fc259233fa3",
            "locations": "d9ab271c-f7be-43a2-a158-d74ae959e279",
            "characters": "f32e41c6-384b-4af1-8d54-cb5329a57c22",
            "organizations": "d0db18e6-35d9-4b52-bfaa-152e4baeb93a",
            "artifacts": "9259fcdc-288f-4924-b300-22ad61c7117c",
            "lines": "ff412fe6-2a64-4588-bcf9-341a2ab1cdcc",
            "events": "8ea0fdf1-2bec-4775-a571-d90f88ae8361",
            "chapters": "616b179d-22be-4aa1-acdc-ae06b6743c68",
            "hooks": "4d7e944d-19ca-4b01-90b7-2f2d2ff76fea",
            "secrets": "9e9faf75-82e3-429e-8be6-5f07f2173614",
            "references": "fda2d470-9b68-40b8-88dd-5102db9d836a",
            "archive": "d0384707-f300-41ba-b4ea-a515a1b55394",
        }
    },
}

# ===================== SCHEMA CODES (Слой 2) =====================
# Владение картой перенесено в schema_live.py (один источник правды).
from registry import normalize as reg_normalize, table_key as reg_table_key, DISPLAY as REG_DISPLAY, is_relation as reg_is_relation, relation_target as reg_relation_target, EMOJI as REG_EMOJI, choose_visible_binding
from rules import RULES
from names import names_compatible
from schema_live import (
    CODES,
    UnknownPropertyCode,
    load_codes_from_redis,
    prop_code,
    prop_meta,
    resolve_select_value,
    get_codes,
    ensure_codes,
)

# PROPERTY_LABELS удалён (слой 1). Коды читаются из schema:codes через schema_live.

VOLUME_LIMITS = {
    "compact": 45000,
    "working": 110000,
    "full": 999999
}

# ===================== NAME RESOLVER (Task C) =====================
import re
import uuid
# reg_normalize берётся из registry (импорт выше)

_TITLE_CACHE: dict[str, tuple[float, dict]] = {}  # project_key → (ts, data)
_TITLE_CACHE_TTL = 120

def invalidate_title_cache(project_key: str | None = None):
    if project_key:
        _TITLE_CACHE.pop(project_key, None)
    else:
        _TITLE_CACHE.clear()

def build_title_to_ids(project_key: str = "burevestnik", force: bool = False) -> dict:

    """
    Строит словарь: table_key → {normalized_title → [id, ...]}
    Кэш 120 сек, инвалидация после записи.
    """
    now = time.time()
    if not force and project_key in _TITLE_CACHE:
        ts, data = _TITLE_CACHE[project_key]
        if now - ts < _TITLE_CACHE_TTL:
            return data
    project = PROJECTS.get(project_key)
    if not project:
        return {}
    
    title_to_ids = {}          # normalized → [(table_key, id, original_title), ...]
    per_table = {k: {} for k in project["tables"]}
    
    for table_key, table_id in project["tables"].items():
        try:
            data = api("/api/v1/ql/content-database/content", {
                "query": {
                    "__filter": {"contentDatabaseId": table_id},
                    "content": {"article": {"id": True, "title": True}}
                }
            })
            for item in data.get("content", []):
                art = item.get("article", {})
                cid = art.get("id")
                title = art.get("title") or ""
                if not cid:
                    continue
                norm = reg_normalize(title)
                if not norm:
                    continue
                # общий
                title_to_ids.setdefault(norm, []).append((table_key, cid, title))
                # по таблице
                per_table[table_key].setdefault(norm, []).append((cid, title))
        except Exception as e:
            print(f"[resolver] Ошибка загрузки {table_key}: {e}")
    
    result = {
        "global": title_to_ids,
        "per_table": per_table
    }
    _TITLE_CACHE[project_key] = (time.time(), result)
    return result

def resolve_name(name: str, table_key: str, resolver_data: dict) -> dict:
    """
    Резолвит одно имя.
    Возвращает:
    {
        "status": "ok" | "not_found" | "ambiguous",
        "id": "...",           # только если ok
        "original_title": "...",
        "candidates": [...]    # если ambiguous
        "question": "..."      # текст для предпросмотра
    }
    """
    norm = reg_normalize(name)
    if not norm:
        return {"status": "not_found", "question": "Пустое название после нормализации"}
    
    per_table = resolver_data.get("per_table", {})
    table_map = per_table.get(table_key, {})
    
    matches = table_map.get(norm, [])
    
    if len(matches) == 0:
        return {
            "status": "not_found",
            "question": f'«{name}» не найден в таблице «{table_key}». Создать новую карточку / это опечатка?'
        }
    if len(matches) == 1:
        cid, orig = matches[0]
        return {
            "status": "ok",
            "id": cid,
            "original_title": orig
        }
    # ambiguous
    candidates = [{"id": cid, "title": orig} for cid, orig in matches]
    return {
        "status": "ambiguous",
        "candidates": candidates,
        "question": f'Найдено несколько карточек, похожих на «{name}». Какую использовать?'
    }

def make_idempotent(action: str, name: str, table_key: str, resolver_data: dict) -> str:
    """
    Если action == "создать", но карточка уже существует → возвращаем "обновить".
    Иначе возвращаем исходный action.
    """
    if action != "создать":
        return action
    res = resolve_name(name, table_key, resolver_data)
    if res["status"] == "ok":
        return "обновить"
    return action


# ===================== DELTA PARSER + PREVIEW (Task D) =====================

# TABLE_ALIASES / TABLE_DISPLAY удалены — registry.table_key / DISPLAY

def parse_delta(text: str) -> list:
    """
    Парсит текст DELTA в список действий.
    Поддерживает несколько карточек в одном блоке, разделённых ---.
    """
    actions = []
    # Сначала убираем обёртку
    text = re.sub(r'(?m)^=== DELTA ===\s*$', '', text)
    text = re.sub(r'(?m)^=== КОНЕЦ DELTA ===.*', '', text)
    text = text.strip()

    # Разбиваем на карточки по --- (но не внутри тела)
    # Простой подход: сначала найдём все заголовки ТАБЛИЦА: и разрежем по ним
    parts = re.split(r'(?m)(?=^ТАБЛИЦА:\s*)', text)
    for part in parts:
        part = part.strip()
        if not part:
            continue
        lines = part.splitlines()
        action = {
            "table_key": None,
            "table_display": None,
            "action": "создать",
            "title": None,
            "properties": {},
            "body_mode": None,
            "body": None,
            "raw_block": part
        }
        body_lines = []
        in_body = False
        body_mode = None

        for line in lines:
            stripped = line.strip()
            upper = stripped.upper()

            if in_body:
                # Если встретили новый ключ верхнего уровня — заканчиваем тело
                if upper.startswith("ТАБЛИЦА:") or upper.startswith("---"):
                    break
                body_lines.append(line)
                continue

            if upper.startswith("ТАБЛИЦА:"):
                raw = stripped.split(":", 1)[1].strip().lower()
                key = reg_table_key(raw)
                if key:
                    action["table_key"] = key
                    action["table_display"] = REG_DISPLAY.get(key, raw) if key else raw
                else:
                    action["table_key"] = raw
                    action["table_display"] = stripped.split(":", 1)[1].strip()
            elif upper.startswith("ДЕЙСТВИЕ:"):
                act = stripped.split(":", 1)[1].strip().lower()
                action["action"] = "обновить" if "обнов" in act else "создать"
            elif upper.startswith("НАЗВАНИЕ:"):
                action["title"] = stripped.split(":", 1)[1].strip()
            elif upper.startswith("ТЕЛО-ДОПОЛНИТЬ:") or upper == "ТЕЛО-ДОПОЛНИТЬ:":
                in_body = True
                body_mode = "append"
                rest = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
                if rest:
                    body_lines.append(rest)
            elif upper.startswith("ТЕЛО:") or upper == "ТЕЛО:":
                # Исход Б (слой 1): полная замена тела не поддерживается.
                # Отказ виден автору до записи.
                action["_error"] = "полная замена тела пока не поддерживается, используйте ТЕЛО-ДОПОЛНИТЬ"
                body_mode = None
            elif ":" in stripped and not stripped.startswith("---"):
                k, v = stripped.split(":", 1)
                k = k.strip()
                v = v.strip()
                if k and k.upper() not in ("ТАБЛИЦА", "ДЕЙСТВИЕ", "НАЗВАНИЕ"):
                    action["properties"][k] = v

        if body_mode:
            action["body_mode"] = body_mode
            action["body"] = "\n".join(body_lines).strip()

        if action["title"] and action["table_key"]:
            actions.append(action)

    return actions


def build_preview(
    delta_text: str,
    project_key: str = "burevestnik",
    *,
    codes: dict | None = None,
    resolver_data: dict | None = None,
) -> dict:
    """
    Полный предпросмотр: creates / updates / warnings / questions.
    codes / resolver_data — опционально для offline-прогона (задача 5).
    Если не переданы — берутся из живой схемы / Redis.
    """
    actions = parse_delta(delta_text)
    if resolver_data is None:
        resolver_data = build_title_to_ids(project_key)
    # codes НЕ пишем в глобальную карту (закон 4 + изоляция процесса).
    # Offline-прогон (run_samples.py) вызывает set_codes один раз в своём процессе до build_preview.

    preview = {
        "creates": [],
        "updates": [],
        "warnings": [],
        "questions": [],
        "raw_actions": actions,
        "ok": True
    }

    for act in actions:
        table_key = act["table_key"]
        title = act["title"]
        original_action = act["action"]

        effective_action = make_idempotent(original_action, title, table_key, resolver_data)
        act["effective_action"] = effective_action

        res = resolve_name(title, table_key, resolver_data)

        item = {
            "table": act["table_display"],
            "table_key": table_key,
            "title": title,
            "action": effective_action,
            "properties": act["properties"],
            "body_mode": act["body_mode"],
            "body_preview": (act["body"][:300] + "…") if act.get("body") and len(act["body"]) > 300 else act.get("body"),
            "resolved_id": res.get("id"),
            "status": res["status"]
        }

        if res["status"] == "not_found":
            if effective_action == "создать":
                preview["creates"].append(item)
            else:
                q = res["question"]
                preview["warnings"].append(q)
                preview["questions"].append(q)
                preview["ok"] = False
        elif res["status"] == "ambiguous":
            q = res["question"]
            preview["questions"].append(q)
            preview["warnings"].append(f"Неоднозначность: {title}")
            item["candidates"] = res.get("candidates")
            preview["ok"] = False
            if effective_action == "создать":
                preview["creates"].append(item)
            else:
                preview["updates"].append(item)
        else:
            if effective_action == "создать":
                preview["creates"].append(item)
            else:
                preview["updates"].append(item)

        # ========== ВАЛИДАЦИЯ ПО ПРАВИЛАМ БИБЛИИ (rules.py) ==========
        ctx = {"resolver": resolver_data, "project_key": project_key}
        for rule_fn in RULES:
            for note in rule_fn(act, ctx):
                text_note = note.get("text") or ""
                if text_note and text_note not in preview["questions"]:
                    preview["questions"].append(text_note)
                    preview["warnings"].append(text_note)
                if note.get("level") == "block":
                    preview["ok"] = False

        # ========== РЕЗОЛВ СВЯЗЕЙ (после валидации) ==========
        for prop_name, prop_val in act["properties"].items():
            if not reg_is_relation(table_key, prop_name, project_key):
                continue
            rel_table = reg_relation_target(table_key, prop_name, project_key)
            if rel_table is None:
                q = f"Связь «{prop_name}»: цель не выведена из имени поля — свойство пропущено"
                if q not in preview["questions"]:
                    preview["questions"].append(q)
                    preview["warnings"].append(q)
                    preview["ok"] = False
                continue
            # видимая колонка (задача 3) — отказ виден в предпросмотре
            route = choose_visible_binding(project_key, table_key, rel_table)
            if not route.get("ok"):
                q = route.get("error") or f"нет видимой колонки связи «{prop_name}»"
                if q not in preview["questions"]:
                    preview["questions"].append(q)
                    preview["warnings"].append(q)
                    preview["ok"] = False
                continue
            names = [n.strip() for n in re.split(r'[,;]', str(prop_val)) if n.strip()]
            for n in names:
                clean_n = re.sub(r'\s*[\(\[\{].*?[\)\]\}]\s*', '', n).strip()
                if not clean_n:
                    continue
                r = resolve_name(clean_n, rel_table, resolver_data)
                if r["status"] != "ok":
                    q = r.get("question", f"Проблема с «{n}»")
                    if q not in preview["questions"]:
                        preview["questions"].append(q)
                        preview["warnings"].append(q)
                        preview["ok"] = False

    return preview


def render_preview_html(preview: dict, delta_text: str) -> str:
    """HTML-предпросмотр для автора."""
    import html as html_mod
    esc = html_mod.escape

    parts = ['<div style="font-family: system-ui, sans-serif; max-width: 900px; margin: 20px auto; padding: 20px;">']
    parts.append('<h2>Предпросмотр изменений</h2>')

    if preview["creates"]:
        parts.append('<h3 style="color:#0a7;">Будет создано</h3><ul>')
        for item in preview["creates"]:
            parts.append(f'<li><b>{esc(item["table"])}</b> → «{esc(item["title"])}»')
            if item.get("properties"):
                props = ", ".join(f"{esc(k)}: {esc(str(v))}" for k, v in list(item["properties"].items())[:6])
                parts.append(f'<br><small>{props}</small>')
            if item.get("body_preview"):
                mode = "дополнить" if item["body_mode"] == "append" else "заменить тело"
                parts.append(f'<br><small>[{mode}] {esc(item["body_preview"])}</small>')
            parts.append('</li>')
        parts.append('</ul>')

    if preview["updates"]:
        parts.append('<h3 style="color:#07a;">Будет обновлено</h3><ul>')
        for item in preview["updates"]:
            rid = (item.get("resolved_id") or "?")[:8]
            parts.append(f'<li><b>{esc(item["table"])}</b> → «{esc(item["title"])}» <small>(id: {rid}…)</small>')
            if item.get("properties"):
                props = ", ".join(f"{esc(k)}: {esc(str(v))}" for k, v in list(item["properties"].items())[:6])
                parts.append(f'<br><small>{props}</small>')
            if item.get("body_preview"):
                mode = "дополнить" if item["body_mode"] == "append" else "заменить тело"
                parts.append(f'<br><small>[{mode}] {esc(item["body_preview"])}</small>')
            parts.append('</li>')
        parts.append('</ul>')

    if preview["questions"]:
        parts.append('<h3 style="color:#c50;">Предупреждения и вопросы</h3><ul>')
        for q in preview["questions"]:
            parts.append(f'<li style="color:#c50;">{esc(q)}</li>')
        parts.append('</ul>')

    if not preview["creates"] and not preview["updates"] and not preview["questions"]:
        parts.append('<p>В DELTA не найдено распознанных действий.</p>')

    parts.append('<hr>')
    if preview["ok"] and (preview["creates"] or preview["updates"]):
        # Для безопасности hidden value не используем длинный текст в HTML — 
        # в реальном сервисе будем хранить в сессии / временном ключе.
        # Здесь для прототипа — form with textarea.
        parts.append('<form method="POST" action="/delta/apply">')
        parts.append(f'<textarea name="delta" style="display:none;">{esc(delta_text)}</textarea>')
        parts.append('<button type="submit" style="background:#0a7;color:white;padding:12px 24px;border:none;border-radius:6px;font-size:16px;cursor:pointer;">Применить изменения</button>')
        parts.append(' &nbsp; <a href="/delta" style="color:#666;">Отмена</a>')
        parts.append('</form>')
    else:
        parts.append('<p style="color:#c50;"><b>Применение заблокировано</b> — закройте вопросы выше или подтвердите создание Крючков/Секретов.</p>')
        parts.append('<p><a href="/delta">← Вернуться к вводу DELTA</a></p>')

    parts.append('</div>')
    return "\n".join(parts)



# ===================== WRITE ENGINE (Task F) =====================

import logging
from datetime import datetime as dt

# Простой лог в память + print (в проде можно писать в файл/Upstash)
def _log_write(action: str, title: str, table: str, success: bool, detail: str = "", project_key: str = "burevestnik"):
    """Кольцевой буфер 500 записей в Redis (переживает передеплой)."""
    from documents import redis_get, redis_set
    entry = {
        "ts": datetime.utcnow().isoformat() + "Z",
        "action": action,
        "title": title,
        "table": table,
        "ok": success,
        "detail": detail,
    }
    key = f"writelog:{project_key}"
    log = redis_get(key) or []
    if not isinstance(log, list):
        log = []
    log.append(entry)
    log = log[-500:]
    redis_set(key, log)

    status = "OK" if success else "FAIL"
    print(f"[write] {status} | {table} | {action} | {title} | {detail[:120]}")

# Обратный словарь label → code (берём первое вхождение)
# LABEL_TO_CODE удалён (слой 1) — используется prop_code / CODES

def _get_table_id(project_key: str, table_key: str) -> str | None:
    project = PROJECTS.get(project_key)
    if not project:
        return None
    return project["tables"].get(table_key)


def _prepare_property_for_write(
    project_key: str,
    table_key: str,
    label: str,
    value,
    resolver_data: dict | None = None,
) -> tuple[str, object] | None:
    """
    Единый помощник записи свойства.
    Возвращает (code, resolved_value) или None (свойство пропустить).
    Raises UnknownPropertyCode при ошибке.
    Для binding: имена → [{"id": uuid}, ...]; пустое значение → None (не стирать).
    """
    # маршрутизация связи
    if reg_is_relation(table_key, label, project_key):
        target = reg_relation_target(table_key, label, project_key)
        if target is None:
            raise UnknownPropertyCode(
                f"Связь «{label}»: цель не выведена из имени поля — свойство пропущено"
            )
        route = choose_visible_binding(project_key, table_key, target)
        if not route.get("ok"):
            err = route.get("error") or "нет видимой колонки связи"
            wf = route.get("write_from")
            if wf:
                err = f"{err} (write_from={wf})"
            raise UnknownPropertyCode(err)
        label = route["prop_name"]

        if resolver_data is None:
            resolver_data = build_title_to_ids(project_key)
        names = [n.strip() for n in re.split(r"[,;]", str(value)) if n.strip()]
        if not names:
            # пустое значение — не стирать существующие связи
            return None
        ids = []
        for n in names:
            clean = re.sub(r"\s*[\(\[\{].*?[\)\]\}]\s*", "", n).strip()
            if not clean:
                continue
            r = resolve_name(clean, target, resolver_data)
            if r["status"] != "ok":
                raise UnknownPropertyCode(
                    r.get("question") or f"Связь «{label}»: «{clean}» не резолвится в id"
                )
            ids.append({"id": r["id"]})
        if not ids:
            return None
        code = prop_code(project_key, table_key, label)
        return code, ids

    # обычное свойство (select / text / ...)
    code = prop_code(project_key, table_key, label)
    resolved = resolve_select_value(project_key, table_key, label, value)
    return code, resolved


def create_article_in_table(project_key: str, table_key: str, title: str, properties: dict, resolver_data: dict | None = None) -> dict:
    """
    Создаёт строку в умной таблице.
    Сигнатура: (project_key, table_key, ...) — table_id берётся из PROJECTS/registry.
    Для select резолвит текст → option id. Неизвестный код/вариант → failed.
    """
    if project_key not in CODES and not load_codes_from_redis(project_key):
        return {"ok": False, "id": None, "error": "карта кодов не загружена, запись запрещена"}
    project = PROJECTS.get(project_key)
    if not project:
        return {"ok": False, "id": None, "error": f"нет проекта {project_key}"}
    table_id = (project.get("tables") or {}).get(table_key)
    if not table_id:
        # try redis tables
        from documents import redis_get
        tables = redis_get(f"schema:tables:{project_key}") or {}
        table_id = tables.get(table_key)
    if not table_id:
        return {"ok": False, "id": None, "error": f"нет table_id для {table_key}"}

    new_id = str(uuid.uuid4())
    prop_list = []
    try:
        for label, value in properties.items():
            prepared = _prepare_property_for_write(
                project_key, table_key, label, value, resolver_data
            )
            if prepared is None:
                continue  # пустое значение связи — не стирать
            code, resolved = prepared
            prop_list.append({
                "method": "add",
                "code": code,
                "value": resolved
            })
    except UnknownPropertyCode as e:
        _log_write("create", title, table_id, False, str(e))
        return {"ok": False, "id": None, "error": str(e)}

    payload = {
        "code": "article_create",
        "payload": {
            "entity": {
                "spaceId": table_id,
                "id": new_id,
                "title": title,
                "properties": prop_list
            }
        }
    }
    try:
        result = api("/api/v1/wiki/properties/command/execute", payload)
        _log_write("create", title, table_id, True, f"id={new_id}")
        return {"ok": True, "id": new_id, "error": None, "raw": result}
    except Exception as e:
        _log_write("create", title, table_id, False, str(e))
        return {"ok": False, "id": None, "error": str(e)}


def update_article_properties(project_key: str, table_key: str, article_id: str, properties: dict, title: str = "", resolver_data: dict | None = None) -> dict:
    """
    Обновляет свойства. Форма подтверждена перехватом 26.07:
    code=group → commands[] с property_update, одно свойство на команду.
    select → option id.
    """
    if project_key not in CODES and not load_codes_from_redis(project_key):
        return {"ok": False, "error": "карта кодов не загружена, запись запрещена"}
    project = PROJECTS.get(project_key)
    table_id = None
    if project:
        table_id = (project.get("tables") or {}).get(table_key)
    if not table_id:
        from documents import redis_get
        tables = redis_get(f"schema:tables:{project_key}") or {}
        table_id = tables.get(table_key)
    if not table_id:
        return {"ok": False, "error": f"нет table_id для {table_key}"}

    commands = []
    try:
        for label, value in properties.items():
            prepared = _prepare_property_for_write(
                project_key, table_key, label, value, resolver_data
            )
            if prepared is None:
                continue  # пустое значение связи — не стирать
            code, resolved = prepared
            commands.append({
                "code": "property_update",
                "payload": {
                    "entity": {
                        "spaceId": table_id,
                        "articleId": article_id
                    },
                    "operation": {
                        "method": "update",
                        "code": code,
                        "value": resolved
                    }
                },
                "internal": False
            })
    except UnknownPropertyCode as e:
        _log_write("update_props", title or article_id, table_id, False, str(e))
        return {"ok": False, "error": str(e)}

    if not commands:
        return {"ok": True, "error": None}

    payload = {
        "code": "group",
        "payload": {"commands": commands}
    }
    try:
        result = api("/api/v1/wiki/properties/command/execute", payload)
        _log_write("update_props", title or article_id, table_id, True, f"props={len(commands)}")
        return {"ok": True, "error": None, "raw": result}
    except Exception as e:
        _log_write("update_props", title or article_id, table_id, False, str(e))
        return {"ok": False, "error": str(e)}


def append_body(space_id: str, article_id: str, text: str, title: str = "") -> dict:
    """
    Добавляет текст в конец тела через merge.
    """
    payload = {
        "document": [
            {
                "type": "text",
                "text": text,
                "marks": []
            }
        ]
    }
    try:
        result = api(f"/api/v1/collaboration/space/{space_id}/article/{article_id}/merge", payload)
        _log_write("append_body", title or article_id, space_id, True, f"len={len(text)}")
        return {"ok": True, "error": None}
    except Exception as e:
        _log_write("append_body", title or article_id, space_id, False, str(e))
        return {"ok": False, "error": str(e)}


# replace_body удалён (исход Б): полная замена тела не поддерживается.
# parse_delta должен отклонять ТЕЛО:

def apply_delta(delta_text: str, project_key: str = "burevestnik") -> dict:
    """
    Применяет DELTA по одной карточке.
    Возвращает структурированный отчёт:
    {
        "applied": [...],
        "failed": [...],
        "skipped": [...],
        "log": [...]
    }
    Никогда не бросает общее исключение — все ошибки собираются.
    """
    preview = build_preview(delta_text, project_key)
    actions = preview.get("raw_actions", [])
    resolver_data = build_title_to_ids(project_key)

    report = {
        "applied": [],
        "failed": [],
        "skipped": [],
        "questions_remaining": preview.get("questions", []),
        "log": []
    }

    # Если есть блокирующие вопросы (Крючки/Секреты без подтверждения) — не применяем ничего
    blocking = [q for q in preview.get("questions", []) if "требует явного подтверждения" in q]
    if blocking and not preview.get("ok", True):
        # В текущей версии UI уже блокирует кнопку, но на всякий случай
        report["skipped"] = [{"title": a["title"], "reason": "блокирующее предупреждение"} for a in actions]
        return report

    for act in actions:
        table_key = act["table_key"]
        title = act["title"]
        table_id = _get_table_id(project_key, table_key)
        if not table_id:
            report["failed"].append({
                "title": title,
                "table": act.get("table_display"),
                "error": f"Неизвестный table_key: {table_key}"
            })
            continue

        effective = make_idempotent(act["action"], title, table_key, resolver_data)
        res_name = resolve_name(title, table_key, resolver_data)

        try:
            if effective == "создать":
                result = create_article_in_table(project_key, table_key, title, act["properties"], resolver_data)
                if not result["ok"]:
                    report["failed"].append({
                        "title": title,
                        "table": act.get("table_display"),
                        "error": result["error"]
                    })
                    continue
                article_id = result["id"]
                # тело
                if act.get("body"):
                    if act.get("body_mode") == "append":
                        br = append_body(table_id, article_id, act["body"], title)
                        if not br["ok"]:
                            report["failed"].append({
                                "title": title,
                                "table": act.get("table_display"),
                                "error": f"Карточка создана, но тело не записалось: {br['error']}"
                            })
                            continue
                    else:
                        report["failed"].append({
                            "title": title,
                            "table": act.get("table_display"),
                            "error": "полная замена тела пока не поддерживается, используйте ТЕЛО-ДОПОЛНИТЬ"
                        })
                        continue
                report["applied"].append({
                    "title": title,
                    "table": act.get("table_display"),
                    "action": "создать",
                    "id": article_id
                })

            else:  # обновить
                if res_name["status"] != "ok":
                    report["failed"].append({
                        "title": title,
                        "table": act.get("table_display"),
                        "error": res_name.get("question", "карточка не найдена")
                    })
                    continue
                article_id = res_name["id"]
                # свойства
                if act["properties"]:
                    ur = update_article_properties(project_key, table_key, article_id, act["properties"], title, resolver_data)
                    if not ur["ok"]:
                        report["failed"].append({
                            "title": title,
                            "table": act.get("table_display"),
                            "error": f"Свойства: {ur['error']}"
                        })
                        continue
                # тело
                if act.get("body"):
                    if act.get("body_mode") == "append":
                        br = append_body(table_id, article_id, act["body"], title)
                        if not br["ok"]:
                            report["failed"].append({
                                "title": title,
                                "table": act.get("table_display"),
                                "error": f"Тело: {br['error']}"
                            })
                            continue
                    else:
                        report["failed"].append({
                            "title": title,
                            "table": act.get("table_display"),
                            "error": "полная замена тела пока не поддерживается, используйте ТЕЛО-ДОПОЛНИТЬ"
                        })
                        continue
                report["applied"].append({
                    "title": title,
                    "table": act.get("table_display"),
                    "action": "обновить",
                    "id": article_id
                })

        except Exception as e:
            # Гарантированно ловим всё, ничего не проглатываем
            _log_write("exception", title, table_key, False, str(e))
            report["failed"].append({
                "title": title,
                "table": act.get("table_display"),
                "error": f"Неожиданная ошибка: {e}"
            })

    from documents import redis_get
    report["log"] = (redis_get(f"writelog:{project_key}") or [])[-50:]
    if report.get("applied"):
        invalidate_title_cache(project_key)

    return report


# ===================== TOKEN SYSTEM (P0) — Upstash Redis =====================
REFRESH_MARGIN_SEC = 15 * 60
PROACTIVE_INTERVAL_SEC = 25 * 60

_state = {
    "access_token": None,
    "refresh_token": None,
    "access_token_expires_at": 0,
    "refresh_token_expires_at": 0,
    "last_refresh_at": None,
    "last_refresh_ok": None,
    "last_error": None,
    "source": None,
}
_lock = threading.Lock()

def _now():
    return int(time.time())

def _upstash_get():
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        return None
    try:
        r = requests.get(
            f"{UPSTASH_REDIS_REST_URL}/get/{TOKENS_KEY}",
            headers={"Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"},
            timeout=10
        )
        if r.status_code != 200:
            print(f"[upstash] GET failed: {r.status_code} {r.text[:200]}")
            return None
        data = r.json()
        result = data.get("result")
        if not result:
            return None
        return json.loads(result)
    except Exception as e:
        print(f"[upstash] GET exception: {e}")
        return None

def _upstash_set(payload: dict):
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        print("[upstash] Нет URL/TOKEN — пропускаю сохранение")
        return False
    try:
        import urllib.parse
        value = json.dumps(payload, ensure_ascii=False)
        encoded = urllib.parse.quote(value, safe="")
        r = requests.post(
            f"{UPSTASH_REDIS_REST_URL}/set/{TOKENS_KEY}/{encoded}",
            headers={"Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"},
            timeout=10
        )
        if r.status_code != 200:
            print(f"[upstash] SET failed: {r.status_code} {r.text[:300]}")
            return False
        print("[upstash] Токены сохранены в Redis")
        return True
    except Exception as e:
        print(f"[upstash] SET exception: {e}")
        return False

def _load_tokens():
    data = _upstash_get()
    if data and data.get("access_token") and data.get("refresh_token"):
        _state["access_token"] = data["access_token"]
        _state["refresh_token"] = data["refresh_token"]
        _state["access_token_expires_at"] = int(data.get("access_token_expires_at", 0))
        _state["refresh_token_expires_at"] = int(data.get("refresh_token_expires_at", 0))
        _state["last_refresh_at"] = data.get("last_refresh_at")
        _state["last_refresh_ok"] = data.get("last_refresh_ok")
        _state["source"] = "upstash"
        print("[tokens] Загружено из Upstash")
        return True
    if INITIAL_REFRESH_TOKEN:
        _state["refresh_token"] = INITIAL_REFRESH_TOKEN
        _state["source"] = "env"
        print("[tokens] Стартуем с REFRESH_TOKEN из env")
        return True
    return False

def _save_tokens():
    payload = {
        "access_token": _state["access_token"],
        "refresh_token": _state["refresh_token"],
        "access_token_expires_at": _state["access_token_expires_at"],
        "refresh_token_expires_at": _state["refresh_token_expires_at"],
        "last_refresh_at": _state["last_refresh_at"],
        "last_refresh_ok": _state["last_refresh_ok"],
        "saved_at": _now(),
    }
    return _upstash_set(payload)

def _do_refresh():
    refresh_token = _state["refresh_token"] or INITIAL_REFRESH_TOKEN
    if not refresh_token:
        _state["last_error"] = "Нет refresh_token"
        _state["last_refresh_ok"] = False
        print("[tokens] ОШИБКА: нет refresh_token")
        return False
    try:
        r = requests.post(
            f"{CLUSTER}/api/v1/auth/integration/refresh",
            json={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "refresh_token": refresh_token,
            },
            timeout=30,
        )
        if r.status_code != 200:
            _state["last_error"] = f"HTTP {r.status_code}: {r.text[:400]}"
            _state["last_refresh_ok"] = False
            print(f"[tokens] ОШИБКА refresh: {_state['last_error']}")
            return False
        data = r.json()
        new_access = data.get("access_token")
        new_refresh = data.get("refresh_token")
        exp = int(data.get("access_token_expires_at", _now() + 3600))
        rexp = int(data.get("refresh_token_expires_at", 0))
        if not new_access:
            _state["last_error"] = "В ответе нет access_token"
            _state["last_refresh_ok"] = False
            return False
        _state["access_token"] = new_access
        if new_refresh:
            _state["refresh_token"] = new_refresh
            print("[tokens] Получен НОВЫЙ refresh_token (ротация)")
        _state["access_token_expires_at"] = exp
        _state["refresh_token_expires_at"] = rexp
        _state["last_refresh_at"] = _now()
        _state["last_refresh_ok"] = True
        _state["last_error"] = None
        _state["source"] = "refresh"
        _save_tokens()
        print("=" * 60)
        print("[tokens] УСПЕШНЫЙ REFRESH")
        print(f"expires_at = {exp} ({datetime.fromtimestamp(exp)})")
        print("=" * 60)
        return True
    except Exception as e:
        _state["last_error"] = str(e)
        _state["last_refresh_ok"] = False
        print(f"[tokens] EXCEPTION при refresh: {e}")
        return False

def get_token():
    with _lock:
        if _state["access_token"] is None:
            if not _load_tokens():
                raise Exception("Нет токенов: ни Upstash, ни REFRESH_TOKEN в env")
        now = _now()
        expires = _state["access_token_expires_at"]
        if _state["access_token"] and expires > now + REFRESH_MARGIN_SEC:
            return _state["access_token"]
        print(f"[tokens] Токен истекает (осталось {max(0, expires - now)} сек) → refresh")
        ok = _do_refresh()
        if not ok:
            raise Exception(f"Не удалось обновить токен: {_state['last_error']}")
        return _state["access_token"]

def get_status():
    with _lock:
        now = _now()
        expires = _state["access_token_expires_at"]
        remaining = max(0, expires - now) if expires else 0
        return {
            "ok": bool(_state["last_refresh_ok"]),
            "source": _state["source"],
            "access_token_expires_at": expires,
            "access_token_expires_in_sec": remaining,
            "access_token_expires_in_min": round(remaining / 60, 1),
            "last_refresh_at": _state["last_refresh_at"],
            "last_refresh_ok": _state["last_refresh_ok"],
            "last_error": _state["last_error"],
            "has_refresh_token": bool(_state["refresh_token"] or INITIAL_REFRESH_TOKEN),
            "storage": "upstash",
        }

def _proactive_loop():
    while True:
        time.sleep(PROACTIVE_INTERVAL_SEC)
        try:
            with _lock:
                now = _now()
                expires = _state["access_token_expires_at"]
                if expires and expires < now + 40 * 60:
                    print("[tokens] Проактивный refresh по таймеру")
                    _do_refresh()
        except Exception as e:
            print(f"[tokens] Ошибка в proactive loop: {e}")

# ===================== DELTA UI (Task D) =====================

@app.route("/delta", methods=["GET", "POST"])
def delta_page():
    """Страница ввода DELTA и показа предпросмотра."""
    if request.method == "GET":
        return """
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>DELTA to Teamly</title>
<style>
body { font-family: system-ui, sans-serif; max-width: 900px; margin: 40px auto; padding: 0 20px; }
textarea { width: 100%; height: 420px; font-family: ui-monospace, monospace; font-size: 13px; padding: 12px; border: 1px solid #ccc; border-radius: 8px; }
button { margin-top: 16px; padding: 12px 28px; font-size: 16px; background: #4f46e5; color: white; border: none; border-radius: 8px; cursor: pointer; }
h1 { margin-bottom: 8px; }
.hint { color: #666; font-size: 0.9rem; margin-bottom: 20px; }
</style>
</head>
<body>
<h1>Обратный канал — DELTA</h1>
<p class="hint">Вставь блок === DELTA === ... === КОНЕЦ DELTA === и нажми «Показать предпросмотр».<br>
Ничего не записывается в Teamly до явного подтверждения.</p>
<form method="POST" action="/delta/preview">
<textarea name="delta" placeholder="=== DELTA ===\nТАБЛИЦА: События\n..."></textarea>
<br>
<button type="submit">Показать предпросмотр</button>
</form>
<p style="margin-top:30px;"><a href="/">← К срезу</a></p>
</body>
</html>
"""
    return "Use /delta/preview", 400


@app.route("/delta/preview", methods=["POST"])
def delta_preview():
    delta_text = request.form.get("delta", "").strip()
    if not delta_text:
        return "Пустой DELTA", 400
    try:
        preview = build_preview(delta_text)
        html = render_preview_html(preview, delta_text)
        return html
    except Exception as e:
        import traceback
        return f"<pre>Ошибка предпросмотра:\n{e}\n\n{traceback.format_exc()}</pre>", 500


@app.route("/delta/apply", methods=["POST"])
def delta_apply():
    """
    Реальное применение DELTA с частичным успехом и отчётом (Task F).
    """
    delta_text = request.form.get("delta", "").strip()
    if not delta_text:
        return "Пустой DELTA", 400

    try:
        report = apply_delta(delta_text)
    except Exception as e:
        import traceback
        return f"<pre>Критическая ошибка apply_delta:\n{e}\n\n{traceback.format_exc()}</pre>", 500

    # HTML-отчёт
    import html as html_mod
    esc = html_mod.escape
    parts = ['<div style="font-family:system-ui;max-width:800px;margin:40px auto;padding:20px;">']
    parts.append('<h2>Результат применения</h2>')

    if report["applied"]:
        parts.append(f'<h3 style="color:#0a7;">Успешно применено ({len(report["applied"])})</h3><ul>')
        for item in report["applied"]:
            parts.append(f'<li><b>{esc(item["table"])}</b> → «{esc(item["title"])}» <small>({esc(item["action"])}, id: {esc(str(item.get("id","?"))[:8])}…)</small></li>')
        parts.append('</ul>')

    if report["failed"]:
        parts.append(f'<h3 style="color:#c50;">Не применено ({len(report["failed"])})</h3><ul>')
        for item in report["failed"]:
            parts.append(f'<li><b>{esc(item.get("table","?"))}</b> → «{esc(item["title"])}»<br><small style="color:#c50;">{esc(item["error"])}</small></li>')
        parts.append('</ul>')
        parts.append('<p style="color:#666;">Можно исправить DELTA и применить повторно — идемпотентность защищает от дублей.</p>')

    if report["skipped"]:
        parts.append(f'<h3 style="color:#a60;">Пропущено ({len(report["skipped"])})</h3><ul>')
        for item in report["skipped"]:
            parts.append(f'<li>«{esc(item["title"])}» — {esc(item["reason"])}</li>')
        parts.append('</ul>')

    if not report["applied"] and not report["failed"] and not report["skipped"]:
        parts.append('<p>Нечего применять.</p>')

    parts.append('<hr><p><a href="/delta">← Новый DELTA</a> &nbsp; <a href="/">К срезу</a></p>')
    parts.append('</div>')
    return "\n".join(parts)


def start_proactive_refresh():
    t = threading.Thread(target=_proactive_loop, daemon=True)
    t.start()
    print("[tokens] Проактивный refresh-поток запущен (Upstash)")

# ===================== END TOKEN SYSTEM =====================

def api(endpoint, payload):
    token = get_token()
    r = requests.post(
        f"{CLUSTER}{endpoint}",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Account-Slug": SLUG,
            "Content-Type": "application/json"
        },
        json=payload,
        timeout=90
    )
    if r.status_code != 200:
        if r.status_code == 401:
            print("[API] 401 Unauthorized — токен истёк или refresh не удался")
        raise Exception(f"API {r.status_code}: {r.text[:300]}")
    return r.json()

def extract_text(editor):
    if not editor:
        return ""
    try:
        doc = json.loads(editor)
    except Exception:
        return ""
    def walk(n):
        if isinstance(n, dict):
            if n.get("type") == "text":
                return n.get("text", "")
            return "".join(walk(c) for c in n.get("content", []))
        if isinstance(n, list):
            return "".join(walk(i) for i in n)
        return ""
    return walk(doc).strip()

def get_card_full(cid):
    data = api("/api/v1/wiki/ql/article", {
        "query": {
            "__filter": {"id": cid},
            "id": True,
            "title": True,
            "editorContent": True,
            "properties": {"properties": True}
        }
    })
    props = data.get("properties", {}).get("properties", {})
    parent_id = None
    # Пытаемся найти родительское событие в свойствах
    for k, v in props.items():
        if isinstance(v, list) and v and isinstance(v[0], dict) and "id" in v[0]:
            # Это похоже на relation. Для простоты берём первое, если ключ похож на parent
            if "parent" in k.lower() or "родител" in str(k).lower():
                parent_id = v[0]["id"]
                break
        # Иногда parent хранится иначе — пока оставляем None
    return {
        "id": data.get("id"),
        "title": data.get("title", ""),
        "body": extract_text(data.get("editorContent")),
        "parent_id": parent_id,
        "properties": props
    }


def fetch_article_schema(article_id: str) -> dict:
    """
    Читает schemaProperties карточки (и space) по форме из перехвата 26.07.
    Возвращает сырой ответ. При 403/ошибке — Exception.
    """
    payload = {
        "query": {
            "__filter": {"id": article_id},
            "id": True,
            "title": True,
            "schemaProperties": {
                "propertyId": True, "name": True, "type": True,
                "code": True, "format": True, "options": True
            },
            "space": {
                "id": True, "title": True, "main_article_id": True,
                "schemaProperties": {
                    "propertyId": True, "name": True, "type": True,
                    "code": True, "format": True, "options": True
                }
            }
        }
    }
    return api("/api/v1/wiki/ql/article", payload)


def _parse_schema_properties(raw_props) -> dict:
    """
    Превращает schemaProperties (list или dict) в
    {name: {"code": str, "type": str, "options": {text: option_id}}}.
    """
    result = {}
    if not raw_props:
        return result
    items = raw_props
    if isinstance(raw_props, dict):
        items = list(raw_props.values()) if not any(k in raw_props for k in ("name", "code")) else [raw_props]
    if not isinstance(items, list):
        return result
    for p in items:
        if not isinstance(p, dict):
            continue
        name = p.get("name") or ""
        code = p.get("code") or ""
        ptype = p.get("type") or p.get("format") or "text"
        if not name or not code:
            continue
        options_map = {}
        opts = p.get("options")
        if isinstance(opts, dict):
            # иногда options = {"items": [...]} или прямой dict
            opts = opts.get("items") or opts.get("options") or opts.get("values") or list(opts.values()) if opts else []
        if isinstance(opts, list):
            for o in opts:
                if isinstance(o, dict):
                    oid = o.get("id") or o.get("optionId") or o.get("value")
                    otext = (o.get("name") or o.get("title") or o.get("text") or o.get("label") or str(o.get("value", ""))).strip()
                    if oid and otext:
                        # один ключ на вариант (оригинал). Сравнение — через casefold в resolve.
                        options_map[otext] = oid
        result[name] = {"code": code, "type": ptype, "options": options_map}
    return result


def build_project_schema_codes(project_key: str) -> dict:
    """
    Для каждой таблицы проекта: берёт первую карточку, читает schemaProperties,
    собирает карту кодов + options для select.
    Пустые таблицы — помечаются missing (техстрока не создаётся: endpoint удаления неизвестен).
    Сохраняет в Redis schema:codes, schema:tables, schema:main_article.
    """
    from documents import redis_set, redis_get
    project = PROJECTS.get(project_key)
    if not project:
        return {"ok": False, "error": f"нет проекта {project_key}"}

    codes = {}
    tables = dict(project.get("tables") or {})
    main_article_id = None
    missing = []
    raw_samples = []
    errors = []
    
    for tkey, table_id in tables.items():
        article_id = None
        # 1. Попытка найти существующую карточку
        try:
            data = api("/api/v1/ql/content-database/content", {
                "query": {
                    "__filter": {"contentDatabaseId": table_id},
                    "content": {"article": {"id": True, "title": True}}
                }
            })
            content = data.get("content") or []
            if content:
                article_id = content[0].get("article", {}).get("id")
        except Exception as e:
            errors.append(f"{tkey}: list content failed: {e}")

        # 2. Пустая таблица — не создаём техстроку (endpoint удаления неизвестен).
        # Помечаем missing. Класс уходит в карантин «коды свойств».
        if not article_id:
            missing.append({"table": tkey, "reason": "empty or no rows — техстрока не создаётся (удаление неизвестно)"})
            codes[tkey] = {}
            continue

        # 3. Читаем схему
        try:
            raw = fetch_article_schema(article_id)
            raw_samples.append({
                "table": tkey,
                "article_id": article_id,
                "keys": list(raw.keys()) if isinstance(raw, dict) else type(raw).__name__
            })
            props = raw.get("schemaProperties")
            if not props and isinstance(raw.get("space"), dict):
                props = raw["space"].get("schemaProperties")
            parsed = _parse_schema_properties(props)
            codes[tkey] = parsed
            if not main_article_id and isinstance(raw.get("space"), dict):
                main_article_id = (
                    raw["space"].get("main_article_id")
                    or raw["space"].get("mainArticleId")
                )
            if not parsed:
                missing.append({
                    "table": tkey,
                    "reason": "schemaProperties empty or unparsed",
                    "sample_keys": list((props or {}).keys()) if isinstance(props, dict) else str(type(props))
                })
        except Exception as e:
            errors.append(f"{tkey}: fetch schema failed: {e}")
            codes[tkey] = {}
            missing.append({"table": tkey, "reason": str(e)})

    # Защита карты: не сохранять полностью пустую, не затирать непустую пустой
    total_codes = sum(len(v) for v in codes.values())
    from documents import redis_get
    existing = redis_get(f"schema:codes:{project_key}")
    existing_count = sum(len(v) for v in (existing or {}).values()) if isinstance(existing, dict) else 0

    if total_codes == 0:
        return {
            "ok": False,
            "error": "карта кодов пуста (все таблицы без строк или schemaProperties не отдались)",
            "project_key": project_key,
            "codes": codes,
            "tables": tables,
            "main_article_id": main_article_id,
            "missing": missing,
            "errors": errors,
            "raw_samples": raw_samples,
            "counts": {k: len(v) for k, v in codes.items()},
        }

    if existing_count > 0 and total_codes < existing_count:
        # не затираем более полную карту
        return {
            "ok": False,
            "error": f"отказ перезаписать карту: существующая имеет {existing_count} кодов, новая — {total_codes}",
            "project_key": project_key,
            "codes": codes,
            "tables": tables,
            "missing": missing,
            "errors": errors,
            "counts": {k: len(v) for k, v in codes.items()},
        }

    redis_set(f"schema:codes:{project_key}", codes)
    redis_set(f"schema:tables:{project_key}", tables)
    if main_article_id:
        redis_set(f"schema:main_article:{project_key}", main_article_id)

    return {
        "ok": True,
        "project_key": project_key,
        "codes": codes,
        "tables": tables,
        "main_article_id": main_article_id,
        "missing": missing,
        "errors": errors,
        "raw_samples": raw_samples,
        "counts": {k: len(v) for k, v in codes.items()},
    }

def build_id_to_title(project):
    """Один раз собираем id → title по всем таблицам проекта."""
    id_to_title = {}
    for table_key, table_id in project["tables"].items():
        try:
            data = api("/api/v1/ql/content-database/content", {
                "query": {
                    "__filter": {"contentDatabaseId": table_id},
                    "content": {"article": {"id": True, "title": True}, "hasNested": True}
                }
            })
            for item in data.get("content", []):
                art = item.get("article", {})
                cid = art.get("id")
                title = art.get("title") or ""
                if cid:
                    id_to_title[cid] = title
        except Exception as e:
            print(f"[slice] Не удалось загрузить таблицу {table_key} для словаря: {e}")
    print(f"[slice] Словарь id→title: {len(id_to_title)} записей")
    return id_to_title

def resolve_relation(val, id_to_title):
    """Превращает relation (list of {id:..}) в строку имён."""
    if not isinstance(val, list):
        return None
    names = []
    for item in val:
        if isinstance(item, dict) and "id" in item:
            cid = item["id"]
            names.append(id_to_title.get(cid, f"[не найдено: {cid}]"))
        elif isinstance(item, str):
            names.append(item)
    return ", ".join(names) if names else None

def _code_to_label(project_key: str, table_key: str, code: str) -> str:
    """Обратная карта code → имя. Только внутри одной таблицы."""
    tmap = (get_codes(project_key) or {}).get(table_key) or {}
    for name, meta in tmap.items():
        c = meta.get("code") if isinstance(meta, dict) else meta
        if c == code:
            return name
    return code

def _option_id_to_text(project_key: str, table_key: str, code: str, opt_id: str) -> str:
    """option id → текст. Только внутри одной таблицы."""
    tmap = (get_codes(project_key) or {}).get(table_key) or {}
    for name, meta in tmap.items():
        if not isinstance(meta, dict):
            continue
        if meta.get("code") != code:
            continue
        for otext, oid in (meta.get("options") or {}).items():
            if oid == opt_id:
                return otext
    return opt_id

def format_card(card, id_to_title, project_key: str, table_key: str):
    """Формирует блок карточки с резолвнутыми связями и подписями из CODES."""
    lines = [f"### {card['title']}"]
    props = card.get("properties") or {}
    
    meta = {}
    relations = {}
    
    for k, v in props.items():
        label = _code_to_label(project_key, table_key, k)
        # Простые значения или одиночные UUID
        if not isinstance(v, (list, dict)):
            if v is None or str(v).strip() in ("", "None", "null"):
                continue
            val = str(v).strip()
            if len(val) == 36 and val.count("-") == 4:
                # сначала option id → текст, потом relation
                as_opt = _option_id_to_text(project_key, table_key, k, val)
                if as_opt != val:
                    val = as_opt
                else:
                    resolved = id_to_title.get(val)
                    if resolved:
                        val = resolved
            meta[label] = val
            continue
        
        # Relation (список)
        resolved = resolve_relation(v, id_to_title)
        if resolved:
            if label not in relations:
                relations[label] = set()
            for name in resolved.split(", "):
                relations[label].add(name.strip())
    
    # Мета-строка
    if meta:
        meta_parts = [f"{k}: {v}" for k, v in meta.items()]
        lines.append(" | ".join(meta_parts[:8]))
    
    # Связи без дублей
    for label, names in relations.items():
        lines.append(f"{label}: {', '.join(sorted(names))}")
    
    body = card.get("body") or ""
    if body:
        lines.append("")
        lines.append(body)
    lines.append("")
    return "\n".join(lines) + "\n"



def get_chapters_for_select(table_id):
    """Список глав/арок для чекбоксов. 3 попытки."""
    for attempt in range(3):
        try:
            data = api("/api/v1/ql/content-database/content", {
                "query": {
                    "__filter": {"contentDatabaseId": table_id},
                    "content": {"article": {"id": True, "title": True}, "hasNested": True}
                }
            })
            rows = [{"id": i["article"]["id"], "title": i["article"].get("title", "")} for i in data.get("content", [])]
            print(f"[index] chapters loaded: {len(rows)}")
            return rows
        except Exception as e:
            print(f"[index] chapters attempt {attempt+1}: {e}")
            time.sleep(2)
    return []

def get_all_events(table_id):
    """Получаем все события + пытаемся вытащить родителя"""
    data = api("/api/v1/ql/content-database/content", {
        "query": {
            "__filter": {"contentDatabaseId": table_id},
            "content": {
                "article": {
                    "id": True,
                    "title": True,
                    "properties": {"properties": True}
                },
                "hasNested": True
            }
        }
    })
    events = []
    for item in data.get("content", []):
        art = item.get("article", {})
        props = art.get("properties", {}).get("properties", {})
        parent_id = None
        # Ищем relation на родителя
        for k, v in props.items():
            if isinstance(v, list) and len(v) > 0:
                if isinstance(v[0], dict) and "id" in v[0]:
                    # Грубая эвристика: если в названии свойства есть parent/родител
                    if "parent" in k.lower() or "родител" in k.lower() or k in ("parent", "parentId"):
                        parent_id = v[0]["id"]
                        break
        events.append({
            "id": art.get("id"),
            "title": art.get("title", ""),
            "parent_id": parent_id
        })
    return events

def build_tree(events):
    by_id = {e["id"]: e for e in events}
    children = defaultdict(list)
    roots = []
    for e in events:
        pid = e.get("parent_id")
        if pid and pid in by_id:
            children[pid].append(e["id"])
        else:
            roots.append(e["id"])
    return by_id, children, roots

def get_ancestors(event_id, by_id):
    """Собирает цепочку предков (то, что было до)"""
    ancestors = []
    current = by_id.get(event_id)
    visited = set()
    while current and current.get("parent_id") and current["parent_id"] not in visited:
        pid = current["parent_id"]
        visited.add(pid)
        parent = by_id.get(pid)
        if parent:
            ancestors.append(parent)
            current = parent
        else:
            break
    return list(reversed(ancestors))  # от корня к выбранному

def get_descendants(event_id, children, depth_mode):
    """Собирает потомков в зависимости от глубины"""
    result = []
    def walk(eid, level):
        for child_id in children.get(eid, []):
            result.append(child_id)
            if depth_mode == "scenes" or (depth_mode == "direct_children" and level < 1):
                walk(child_id, level + 1)
    if depth_mode != "arcs":
        walk(event_id, 0)
    return result

@app.route("/", methods=["GET"])
def index():
    projects_options = "".join(
        f'<option value="{k}">{v.get("name", k)}</option>'
        for k, v in PROJECTS.items()
    )
    return f"""
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Detective Engine</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 720px; margin: 40px auto; padding: 0 20px; color: #1a1a1a; line-height: 1.45; }}
  h1 {{ font-size: 1.6rem; margin-bottom: 4px; }}
  .sub {{ color: #666; margin-bottom: 28px; }}
  nav {{ display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 32px; }}
  nav a {{ display: inline-block; padding: 10px 16px; background: #f3f4f6; border-radius: 8px; text-decoration: none; color: #111; font-weight: 500; }}
  nav a:hover {{ background: #e5e7eb; }}
  nav a.primary {{ background: #4f46e5; color: white; }}
  .card {{ border: 1px solid #e5e7eb; border-radius: 12px; padding: 20px; margin-bottom: 20px; }}
  .card h2 {{ font-size: 1.1rem; margin: 0 0 8px; }}
  .card p {{ margin: 0; color: #555; font-size: 0.95rem; }}
  select, button {{ font-size: 1rem; padding: 10px 14px; border-radius: 8px; border: 1px solid #ccc; }}
  button {{ background: #4f46e5; color: white; border: none; cursor: pointer; margin-top: 12px; }}
</style>
</head>
<body>
  <h1>Detective Engine</h1>
  <p class="sub">Срезы · DELTA · Провижининг v7</p>

  <nav>
    <a class="primary" href="/">Срез</a>
    <a href="/delta">DELTA</a>
    <a href="/provision">Провижининг</a>
    <a href="/status">Статус токена</a>
    <a href="/documents">Документы</a>
    <a href="/spaces">Пространства</a>
  </nav>

  <div class="card">
    <h2>Собрать срез</h2>
    <p>Выбери проект и параметры. Сейчас доступны Буревестник и тестовое пространство v7.</p>
    <form action="/slice" method="get" style="margin-top:16px;">
      <label><strong>Проект</strong></label><br>
      <select name="project" style="width:100%; margin-top:6px;">
        {projects_options}
      </select>
      <button type="submit">Перейти к параметрам среза →</button>
    </form>
  </div>

  <div class="card">
    <h2>Быстрые ссылки</h2>
    <p>
      <a href="/delta">Обратный канал (DELTA)</a><br>
      <a href="/provision">Провижининг пространства v7</a><br>
      <a href="/status">Состояние OAuth-токена</a>
    </p>
  </div>
</body>
</html>
"""


@app.route("/slice")
def slice():
    project_key = request.args.get("project", "burevestnik")
    selected_event_ids = request.args.getlist("events")
    selected_chapter_ids = request.args.getlist("chapters")
    depth = request.args.get("depth", "direct_children")
    volume = request.args.get("volume", "working")
    selected_tables = request.args.getlist("tables") or ["characters", "events", "locations"]

    limit = VOLUME_LIMITS.get(volume, 110000)
    project = PROJECTS[project_key]
    events_table_id = project["tables"]["events"]

    # Словарь id → title для резолва связей
    id_to_title = build_id_to_title(project)

    # Загружаем все события и строим дерево
    all_events = get_all_events(events_table_id)
    by_id, children, roots = build_tree(all_events)

    # Определяем, какие события включать
    to_include = set()

    # Если выбраны главы — резолвим ВСЕ их relation, которые указывают на события
    if selected_chapter_ids:
        print(f"[slice] Резолв {len(selected_chapter_ids)} глав...")
        for chid in selected_chapter_ids:
            try:
                card = get_card_full(chid)
                props = card.get("properties") or {}
                print(f"[slice] Глава «{card.get('title', chid)}» keys: {list(props.keys())}")
                found = 0
                for k, v in props.items():
                    if not isinstance(v, list):
                        # maybe single id
                        if isinstance(v, str) and v in by_id:
                            selected_event_ids.append(v)
                            found += 1
                        continue
                    for item in v:
                        if isinstance(item, dict) and "id" in item:
                            eid = item["id"]
                            if eid in by_id:
                                selected_event_ids.append(eid)
                                found += 1
                        elif isinstance(item, str) and item in by_id:
                            selected_event_ids.append(item)
                            found += 1
                print(f"[slice] Глава «{card.get('title', chid)}» → {found} событий")
            except Exception as e:
                print(f"[slice] Ошибка резолва главы {chid}: {e}")

    if not selected_event_ids and not selected_chapter_ids:
        # Если ничего не выбрано — берём корни
        selected_event_ids = roots

    for eid in selected_event_ids:
        if eid not in by_id:
            continue
        # Предки (то, что было до)
        for anc in get_ancestors(eid, by_id):
            to_include.add(anc["id"])
        # Само событие
        to_include.add(eid)
        # Потомки по глубине
        for desc in get_descendants(eid, children, depth):
            to_include.add(desc)

    # Порядок: сначала корни, потом по дереву
    ordered_ids = []
    def add_with_children(eid):
        if eid in to_include and eid not in ordered_ids:
            ordered_ids.append(eid)
            for cid in children.get(eid, []):
                add_with_children(cid)
    for rid in roots:
        add_with_children(rid)
    # На всякий случай добавляем оставшиеся
    for eid in to_include:
        if eid not in ordered_ids:
            ordered_ids.append(eid)

    result = []
    result.append(f"# Срез: {project['name']}")
    result.append(f"Собран: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    result.append(f"Режим: {volume} | Глубина: {depth}")
    result.append(f"Выбрано событий: {len(selected_event_ids)} → итого в срезе: {len(ordered_ids)}")
    result.append("")

    current_len = 0

    # События
    if "events" in selected_tables:
        result.append("\n## СОБЫТИЯ\n")
        for eid in ordered_ids:
            if current_len > limit:
                result.append("\n--- Обрезано по лимиту ---\n")
                break
            try:
                card = get_card_full(eid)
                block = format_card(card, id_to_title, project_key, "events")
                if current_len + len(block) > limit:
                    result.append("\n--- Обрезано по лимиту ---\n")
                    break
                result.append(block)
                current_len += len(block)
            except Exception as e:
                result.append(f"### [ошибка {eid}]: {e}\n\n")

    # Остальные таблицы (персонажи, локации...)
    priority = ["characters", "locations", "chapters", "world"]
    for table_key in priority:
        if table_key not in selected_tables or current_len > limit:
            continue
        table_id = project["tables"].get(table_key)
        if not table_id:
            continue
        data = api("/api/v1/ql/content-database/content", {
            "query": {
                "__filter": {"contentDatabaseId": table_id},
                "content": {"article": {"id": True, "title": True}, "hasNested": True}
            }
        })
        rows = [{"id": i["article"]["id"], "title": i["article"].get("title", "")} for i in data.get("content", [])]
        result.append(f"\n## {table_key.upper()} ({len(rows)})\n")
        for row in rows[:15]:
            if current_len > limit:
                break
            try:
                card = get_card_full(row["id"])
                block = format_card(card, id_to_title, project_key, table_key)
                if current_len + len(block) > limit:
                    break
                result.append(block)
                current_len += len(block)
            except Exception as e:
                print(f"[slice] Ошибка загрузки карточки {row.get('id')}: {e}")
                result.append(f"### [ошибка {row.get('id')}]: {e}\n\n")

    text = "\n".join(result)
    filename = f"slice_{project_key}_{datetime.now().strftime('%Y%m%d_%H%M')}.md"
    return Response(text, mimetype="text/markdown",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})



@app.route("/debug/refresh")
def debug_refresh():
    key = request.args.get("key", "")
    if key != DEBUG_KEY:
        return jsonify({"error": "forbidden"}), 403
    try:
        with _lock:
            ok = _do_refresh()
            st = get_status()
            st["force_refresh_ok"] = ok
            return jsonify(st)
    except Exception as e:
        return jsonify({"error": str(e), "force_refresh_ok": False}), 500


@app.route("/debug/token")
def debug_token():
    """Отдаёт текущий access_token без refresh. Только для зонда."""
    key = request.args.get("key", "")
    if key != DEBUG_KEY:
        return jsonify({"error": "forbidden"}), 403
    try:
        token = get_token()  # может обновить, если уже истёк — это штатно для сервиса
        if not token:
            return jsonify({"error": "no access_token"}), 503
        return jsonify({
            "access_token": token,
            "token_type": "Bearer",
            "source": "service",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/debug/schema")
def debug_schema():
    """
    Читает schemaProperties всех таблиц проекта и сохраняет карту кодов в Redis.
    ?project=detective_v7|burevestnik
    ?key=<DEBUG_KEY>
    """
    key = request.args.get("key", "")
    if key != DEBUG_KEY:
        return jsonify({"error": "forbidden"}), 403
    project_key = request.args.get("project", "detective_v7")
    try:
        result = build_project_schema_codes(project_key)
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/status")
def status():
    return jsonify(get_status())


start_proactive_refresh()

# ===================== PROVISION v7 (Слой 1) =====================
try:
    from provision_v7 import provision_space, resume_missing_relations, show_all_columns, list_spaces
except ImportError:
    provision_space = None
    resume_missing_relations = None
    show_all_columns = None
    list_spaces = None

@app.route("/provision", methods=["GET"])
def provision_endpoint():
    """
    ?confirm=1 — создать НОВОЕ пространство «Детективный движок v7 · 26.07.2026»
                с project_key=detective_v7, сохранить карту кодов в Redis.
    Старые KNOWN_* удалены. parent_id берётся из main_article_id ответа create_space.
    """
    confirm = request.args.get("confirm") == "1"

    if not confirm:
        return """
        <h2>Провижининг v7 (Слой 1)</h2>
        <p>Создаёт <b>новое</b> пространство «Детективный движок v7 · 26.07.2026».</p>
        <p>Карта таблиц и кодов свойств сохраняется в Redis (schema:tables:detective_v7, schema:codes:detective_v7).</p>
        <p>Старое тестовое пространство после успешной проверки автор удаляет руками.</p>
        <ul>
          <li><a href="/provision?confirm=1"><b>Создать новое пространство v7</b></a></li>
        </ul>
        <p><a href="/">← Назад</a></p>
        """, 200, {"Content-Type": "text/html; charset=utf-8"}

    if provision_space is None:
        return jsonify({"error": "provision_v7.py не найден"}), 500

    try:
        result = provision_space(
            api,
            title="Детективный движок v7 · 26.07.2026",
            project_key="detective_v7",
        )
        return jsonify(result), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@app.route("/spaces", methods=["GET"])
def spaces_endpoint():
    """Список пространств аккаунта через API."""
    try:
        from provision_v7 import list_spaces as _list
    except ImportError:
        return jsonify({"error": "list_spaces не найден"}), 500
    result = _list(api)
    return jsonify(result), 200 if result.get("ok") else 502



# ===================== DOCUMENTS (A) =====================

@app.route("/documents")
def documents_page():
    project = request.args.get("project", "detective_v7")
    proj_js = json.dumps(project)
    js_parts = [
        "const project = " + proj_js + ";",
        "async function loadList(){",
        "const el=document.getElementById('list');",
        "try{",
        "const r=await fetch('/api/documents?project='+encodeURIComponent(project));",
        "const text=await r.text();",
        "let data;",
        "try{data=JSON.parse(text);}catch(e){",
        "el.innerHTML='<pre class=meta>HTTP '+r.status+' '+text.slice(0,800)+'</pre>';return;}",
        "if(!r.ok){el.innerHTML='<pre class=meta>HTTP '+r.status+' '+JSON.stringify(data)+'</pre>';return;}",
        "if(!data.documents||!data.documents.length){el.innerHTML='<p class=meta>Пока нет документов.</p>';return;}",
        "el.innerHTML=data.documents.map(function(d){",
        "return '<div class=card><strong>'+(d.filename||'')+'</strong>'",
        "+'<div class=meta>'+(d.format||'')+' · ~'+(d.pages_est||'?')+' стр. · ~'+(d.tokens_est||'?')",
        "+' tok · глав: '+(d.chapters_count||0)",
        "+(d.chunks_count?(' · кусков: '+d.chunks_count):'')",
        "+' · '+(d.created_at||'')+'</div>'",
        "+'<div class=meta>id: '+d.id+'</div>'",
        "+'<button type=button data-act=chapters data-id=\"'+d.id+'\">Главы</button> '",
        "+'<button type=button data-act=chunks data-id=\"'+d.id+'\">Куски</button> '",
        "+'<button type=button data-act=prompt data-id=\"'+d.id+'\">Промт ф.1</button> '",
        "+'<button type=button data-act=run3 data-id=\"'+d.id+'\">Фаза 1 · 3 куска</button> '",
        "+'<button type=button data-act=run2 data-id=\"'+d.id+'\">Фаза 2 · 3 куска</button> '",
        "+'<button type=button data-act=reset data-id=\"'+d.id+'\">Сброс known</button> '",
        "+'<button type=button data-act=del data-id=\"'+d.id+'\">Удалить</button>'",
        "+'<div id=ch-'+d.id+'></div></div>';",
        "}).join('');",
        "}catch(e){el.innerHTML='<pre class=meta>JS: '+e+'</pre>';}",
        "}",
        "async function showChapters(id){",
        "const box=document.getElementById('ch-'+id);box.innerHTML='…';",
        "const r=await fetch('/api/documents/'+id+'?project='+encodeURIComponent(project));",
        "const d=await r.json();",
        "if(!d.chapters){box.textContent=d.error||'нет глав';return;}",
        "box.innerHTML='<ul class=chapters>'+d.chapters.map(function(c){",
        "return '<li>#'+c.index+' '+c.title+' — '+c.pages_est+' стр. ('+c.chars+' зн.)</li>';",
        "}).join('')+'</ul>';",
        "}",
        "async function buildChunks(id){",
        "const box=document.getElementById('ch-'+id);box.innerHTML='Нарезка…';",
        "const r=await fetch('/api/documents/'+id+'/chunks/build?project='+encodeURIComponent(project),{method:'POST'});",
        "const data=await r.json();",
        "if(!data.ok){box.textContent=data.error||'ошибка';return;}",
        "const p=data.params||{};",
        "box.innerHTML='<p class=meta>кусков: '+data.chunks_count",
        "+' (цель ~'+(p.target_pages||'?')+', max '+(p.max_pages||'?')+', overlap '+(p.overlap_pages||'?')+')</p>'",
        "+'<ul class=chapters>'+(data.chunks||[]).map(function(c){",
        "return '<li>'+c.id+' · '+c.chapter_title+' ч.'+c.part+'/'+c.parts_total+' — '+c.pages_est+' стр.</li>';",
        "}).join('')+'</ul>';",
        "}",
        "async function showPrompt(id){",
        "const box=document.getElementById('ch-'+id);box.innerHTML='Сборка промта…';",
        "const r=await fetch('/api/documents/'+id+'/prompt?project='+encodeURIComponent(project)+'&phase=1&preview=short');",
        "const data=await r.json();",
        "if(data.error){box.textContent=data.error;return;}",
        "box.innerHTML='<p class=meta>фаза '+data.phase+': '+data.phase_name",
        "+' · chunk '+data.chunk_id",
        "+' · sys ~'+data.meta.system_tokens_est+' tok, user ~'+data.meta.user_tokens_est+' tok</p>'",
        "+'<pre class=box></pre>';",
        "box.querySelector('pre').textContent='--- SYSTEM ---'+String.fromCharCode(10)+(data.system_preview||'')+String.fromCharCode(10,10)+'--- USER ---'+String.fromCharCode(10)+(data.user_preview||'');",
        "}",
        "async function runPhase2(id){",
        "const box=document.getElementById('ch-'+id);",
        "box.innerHTML='Старт фазы 2 (3 куска)…';",
        "try{",
        "const r=await fetch('/api/documents/'+id+'/phase/run?project='+encodeURIComponent(project)+'&phase=2',{",
        "method:'POST',headers:{'Content-Type':'application/json'},",
        "body:JSON.stringify({max_chunks:3})});",
        "const start=await r.json();",
        "if(!start.job_id){box.textContent=JSON.stringify(start);return;}",
        "const jid=start.job_id;",
        "box.innerHTML='job '+jid+' · queued…';",
        "for(let i=0;i<90;i++){",
        "await new Promise(function(res){setTimeout(res,10000);});",
        "const s=await fetch('/api/phase/job/'+jid).then(function(x){return x.json();});",
        "const job=s.job||{};",
        "var extra='';",
        "if(job.progress){extra=' · '+((job.progress.done||0))+'/'+(job.progress.total||'?');",
        "if(job.progress.last_chunk)extra+=' · '+job.progress.last_chunk;}",
        "if(job.result&&job.result.processed)extra+=' · processed '+job.result.processed;",
        "box.innerHTML='job '+jid+' · '+job.status+extra;",
        "if(job.status==='done'||job.status==='error'){",
        "box.innerHTML='<pre class=box>'+JSON.stringify(s,null,2)+'</pre>';",
        "return;}",
        "}",
        "box.innerHTML=box.innerHTML+' · timeout опроса';",
        "}catch(e){box.textContent='err '+e;}",
        "}",

"async function runPhase3(id){",
        "const box=document.getElementById('ch-'+id);",
        "box.innerHTML='Старт фазы 1 (3 куска)…';",
        "try{",
        "const r=await fetch('/api/documents/'+id+'/phase/run?project='+encodeURIComponent(project)+'&phase=1',{",
        "method:'POST',headers:{'Content-Type':'application/json'},",
        "body:JSON.stringify({max_chunks:3})});",
        "const start=await r.json();",
        "if(!start.job_id){box.textContent=JSON.stringify(start);return;}",
        "const jid=start.job_id;",
        "box.innerHTML='job '+jid+' · queued…';",
        "for(let i=0;i<90;i++){",
        "await new Promise(function(res){setTimeout(res,10000);});",
        "const s=await fetch('/api/phase/job/'+jid).then(function(x){return x.json();});",
        "const job=s.job||{};",
        "var extra='';",
        "if(job.progress){extra=' · '+((job.progress.done||0))+'/'+(job.progress.total||'?');",
        "if(job.progress.last_chunk)extra+=' · '+job.progress.last_chunk;}",
        "if(job.result&&job.result.processed)extra+=' · processed '+job.result.processed;",
        "box.innerHTML='job '+jid+' · '+job.status+extra;",
        "if(job.status==='done'||job.status==='error'){",
        "box.innerHTML='<pre class=box>'+JSON.stringify(s,null,2)+'</pre>';",
        "return;}",
        "}",
        "box.innerHTML=box.innerHTML+' · timeout опроса';",
        "}catch(e){box.textContent='err '+e;}",
        "}",
        "async function resetKnown(id){",
        "if(!confirm('Сбросить known и журналы фаз?'))return;",
        "const box=document.getElementById('ch-'+id);box.innerHTML='Сброс…';",
        "const r=await fetch('/api/documents/'+id+'/known/reset?project='+encodeURIComponent(project),{method:'POST'});",
        "const data=await r.json();",
        "box.innerHTML='<pre class=box>'+JSON.stringify(data,null,2)+'</pre>';",
        "}",
        "async function delDoc(id){",
        "if(!confirm('Удалить документ?'))return;",
        "await fetch('/api/documents/'+id+'?project='+encodeURIComponent(project),{method:'DELETE'});",
        "loadList();",
        "}",
        "document.getElementById('list').addEventListener('click',function(ev){",
        "const btn=ev.target.closest('button[data-act]');if(!btn)return;",
        "const id=btn.getAttribute('data-id');const act=btn.getAttribute('data-act');",
        "if(act==='chapters')showChapters(id);",
        "else if(act==='chunks')buildChunks(id);",
        "else if(act==='prompt')showPrompt(id);",
        "else if(act==='run3')runPhase3(id);else if(act==='run2')runPhase2(id);",
        "else if(act==='reset')resetKnown(id);",
        "else if(act==='del')delDoc(id);",
        "});",
        "document.getElementById('up').onsubmit=async function(e){",
        "e.preventDefault();const fd=new FormData(e.target);",
        "const msg=document.getElementById('msg');msg.textContent='Загрузка…';",
        "const r=await fetch('/api/documents/upload',{method:'POST',body:fd});",
        "const data=await r.json();msg.textContent=JSON.stringify(data,null,2);loadList();",
        "};",
        "loadList();",
    ]
    js = "".join(js_parts)
    html = (
        "<!DOCTYPE html><html lang=ru><head><meta charset=utf-8><title>Документы</title>"
        "<style>"
        "body{font-family:system-ui,sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem;background:#f7f7f8;color:#111}"
        "h1{font-size:1.4rem}a{color:#334}"
        ".card{background:#fff;border:1px solid #e5e5e5;border-radius:10px;padding:1rem 1.2rem;margin:0.8rem 0}"
        ".meta{color:#666;font-size:0.9rem}"
        "button{background:#111;color:#fff;border:0;border-radius:8px;padding:0.5rem 1rem;cursor:pointer;margin:0.2rem 0.3rem 0.2rem 0}"
        ".nav a{margin-right:1rem}"
        "ul.chapters{max-height:280px;overflow:auto;font-size:0.9rem}"
        "pre.box{white-space:pre-wrap;font-size:12px;max-height:420px;overflow:auto;background:#f0f0f0;padding:8px;border-radius:6px}"
        "</style></head><body>"
        "<div class=nav><a href=/>← Главная</a> "
        "<a href=/documents?project=detective_v7>detective_v7</a> "
        "<a href=/documents?project=burevestnik>burevestnik</a></div>"
        "<h1>Документы · " + project + "</h1>"
        "<div class=card>"
        "<form id=up enctype=multipart/form-data>"
        '<input type=hidden name=project value="' + project + '">'
        "<label>Загрузить .docx / .txt / .md</label><br>"
        "<input type=file name=files multiple accept=.docx,.txt,.md,.markdown>"
        "<button type=submit>Загрузить</button></form>"
        "<pre id=msg class=meta></pre></div>"
        "<div id=list>Загрузка списка…</div>"
        "<script>" + js + "</script></body></html>"
    )
    return Response(html, mimetype="text/html; charset=utf-8")


@app.route("/api/documents", methods=["GET"])
def api_documents_list():
    try:
        from documents import list_documents
    except ImportError as e:
        return jsonify({"error": "documents module missing", "detail": str(e)}), 500
    project = request.args.get("project", "detective_v7")
    try:
        docs = list_documents(project)
        return jsonify({"project": project, "documents": docs})
    except Exception as e:
        return jsonify({"error": "list_documents failed", "detail": str(e)}), 500


@app.route("/api/documents/upload", methods=["POST"])
def api_documents_upload():
    try:
        from documents import save_document
    except ImportError:
        return jsonify({"error": "documents module missing"}), 500
    project = request.form.get("project") or request.args.get("project") or "detective_v7"
    files = request.files.getlist("files") or []
    if not files and "file" in request.files:
        files = [request.files["file"]]
    if not files:
        return jsonify({"error": "нет файлов"}), 400
    results = []
    for f in files:
        try:
            raw = f.read()
            if not raw:
                continue
            results.append(save_document(project, f.filename or "document.txt", raw))
        except Exception as e:
            results.append({"ok": False, "filename": f.filename, "error": str(e)})
    ok = any(r.get("ok") for r in results) if results else False
    return jsonify({"ok": ok, "uploaded": results}), (200 if ok else 400)


@app.route("/api/documents/<doc_id>", methods=["GET"])
def api_document_get(doc_id):
    try:
        from documents import get_document
    except ImportError:
        return jsonify({"error": "documents module missing"}), 500
    project = request.args.get("project", "detective_v7")
    include_text = request.args.get("text") == "1"
    doc = get_document(project, doc_id, include_text=include_text)
    if not doc:
        return jsonify({"error": "not found"}), 404
    return jsonify(doc)


@app.route("/api/documents/<doc_id>", methods=["DELETE"])
def api_document_delete(doc_id):
    try:
        from documents import delete_document
    except ImportError:
        return jsonify({"error": "documents module missing"}), 500
    project = request.args.get("project", "detective_v7")
    delete_document(project, doc_id)
    return jsonify({"ok": True})


@app.route("/api/documents/<doc_id>/chapters/<int:chapter_index>", methods=["GET"])
def api_document_chapter(doc_id, chapter_index):
    try:
        from documents import get_chapter
    except ImportError:
        return jsonify({"error": "documents module missing"}), 500
    project = request.args.get("project", "detective_v7")
    ch = get_chapter(project, doc_id, chapter_index)
    if not ch:
        return jsonify({"error": "not found"}), 404
    return jsonify(ch)



@app.route("/api/documents/<doc_id>/chunks", methods=["GET"])
def api_document_chunks(doc_id):
    try:
        from documents import list_chunks, build_chunks
    except ImportError:
        return jsonify({"error": "documents module missing"}), 500
    project = request.args.get("project", "detective_v7")
    rebuild = request.args.get("rebuild") == "1"
    if rebuild:
        result = build_chunks(project, doc_id)
        return jsonify(result), (200 if result.get("ok") else 400)
    chunks = list_chunks(project, doc_id)
    return jsonify({"doc_id": doc_id, "chunks_count": len(chunks), "chunks": chunks})


@app.route("/api/documents/<doc_id>/chunks/build", methods=["POST"])
def api_document_chunks_build(doc_id):
    try:
        from documents import build_chunks
    except ImportError:
        return jsonify({"error": "documents module missing"}), 500
    project = request.args.get("project") or (request.json or {}).get("project") or "detective_v7"
    result = build_chunks(project, doc_id)
    return jsonify(result), (200 if result.get("ok") else 400)


@app.route("/api/documents/<doc_id>/chunks/<path:chunk_id>", methods=["GET"])
def api_document_chunk_one(doc_id, chunk_id):
    try:
        from documents import get_chunk
    except ImportError:
        return jsonify({"error": "documents module missing"}), 500
    project = request.args.get("project", "detective_v7")
    ch = get_chunk(project, doc_id, chunk_id)
    if not ch:
        return jsonify({"error": "not found"}), 404
    return jsonify(ch)



@app.route("/api/documents/<doc_id>/prompt", methods=["GET", "POST"])
def api_document_prompt(doc_id):
    """Сборка промта (D) без вызова модели. phase + chunk_id обязательны."""
    try:
        from documents import get_chunk, list_chunks
        from prompts import build_messages, PHASES
    except ImportError as e:
        return jsonify({"error": str(e)}), 500

    project = request.args.get("project") or (request.json or {}).get("project") or "detective_v7"
    body = request.json if request.is_json else {}
    try:
        phase = int(request.args.get("phase") or body.get("phase") or 1)
    except (TypeError, ValueError):
        return jsonify({"error": "phase must be int"}), 400
    if phase not in PHASES:
        return jsonify({"error": f"unknown phase {phase}", "known": list(PHASES.keys())}), 400

    chunk_id = request.args.get("chunk_id") or body.get("chunk_id")
    if not chunk_id:
        # первый кусок по умолчанию
        chunks = list_chunks(project, doc_id, include_text=False)
        if not chunks:
            return jsonify({"error": "нет кусков — сначала нажмите «Куски»"}), 400
        chunk_id = chunks[0]["id"]

    chunk = get_chunk(project, doc_id, chunk_id)
    if not chunk:
        return jsonify({"error": f"chunk not found: {chunk_id}"}), 404

    known = body.get("known_entities") or {}
    answers = body.get("author_answers") or []
    result = build_messages(phase, chunk, known_entities=known, author_answers=answers)
    # по умолчанию не тащим полный user text в UI если preview=short
    if request.args.get("preview") == "short":
        result = {
            "phase": result["phase"],
            "phase_name": result["phase_name"],
            "chunk_id": result["chunk_id"],
            "meta": result["meta"],
            "system_preview": result["messages"][0]["content"][:1200] + "…",
            "user_preview": result["messages"][1]["content"][:800] + "…",
        }
    return jsonify(result)



@app.route("/api/llm/status", methods=["GET"])
def api_llm_status():
    try:
        from llm import llm_configured, LLM_BASE_URL, LLM_MODEL, llm_ping
    except ImportError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    key_set = llm_configured()
    ping = request.args.get("ping") == "1"
    out = {
        "ok": True,
        "configured": key_set,
        "base_url": LLM_BASE_URL,
        "model": LLM_MODEL,
        "key_present": key_set,
    }
    if ping and key_set:
        out["ping"] = llm_ping()
    return jsonify(out)



@app.route("/api/documents/<doc_id>/phase/run", methods=["POST"])
def api_phase_run(doc_id):
    """Стартует фоновую задачу. Сразу возвращает job_id."""
    try:
        from phase_engine import start_phase_job
        from prompts import PHASES
    except Exception as e:
        return jsonify({"ok": False, "error": "import", "detail": repr(e)}), 500

    try:
        body = request.get_json(silent=True) or {}
        project = request.args.get("project") or body.get("project") or "detective_v7"
        try:
            phase = int(request.args.get("phase") or body.get("phase") or 1)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "phase must be int"}), 400
        if phase not in PHASES:
            return jsonify({"ok": False, "error": "unknown phase", "known": list(PHASES.keys())}), 400

        chunk_id = request.args.get("chunk_id") or body.get("chunk_id")
        max_chunks = body.get("max_chunks", 1)
        try:
            max_chunks = int(max_chunks) if max_chunks is not None else 1
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "max_chunks must be int"}), 400

        job = start_phase_job(
            project, doc_id, phase, max_chunks=max_chunks, chunk_id=chunk_id
        )
        return jsonify(job)
    except Exception as e:
        import traceback
        return jsonify({
            "ok": False,
            "error": "phase_run exception",
            "detail": str(e),
            "trace": traceback.format_exc()[-2000:],
        }), 500


@app.route("/api/phase/job/<job_id>", methods=["GET"])
def api_phase_job(job_id):
    try:
        from phase_engine import get_phase_job
    except Exception as e:
        return jsonify({"ok": False, "error": repr(e)}), 500
    job = get_phase_job(job_id)
    if not job:
        return jsonify({"ok": False, "error": "job not found"}), 404
    return jsonify({"ok": True, "job": job})



@app.route("/api/debug/phase", methods=["GET"])
def api_debug_phase():
    info = {}
    try:
        import phase_engine
        info["phase_engine"] = "ok"
        info["has_start"] = hasattr(phase_engine, "start_phase_job")
        info["has_run_chunk"] = hasattr(phase_engine, "run_chunk")
    except Exception as e:
        import traceback
        info["phase_engine"] = "fail"
        info["error"] = str(e)
        info["trace"] = traceback.format_exc()[-2000:]
    try:
        import prompts
        info["prompts"] = "ok"
        info["phases"] = list(prompts.PHASES.keys())
    except Exception as e:
        info["prompts"] = str(e)
    try:
        from documents import list_chunks, get_chunk
        project = request.args.get("project", "detective_v7")
        doc_id = request.args.get("doc_id", "8de824d3-a5b9-44fc-bfc9-739cf6835e7c")
        chunks = list_chunks(project, doc_id)
        info["chunks_count"] = len(chunks)
        if chunks:
            ch = get_chunk(project, doc_id, chunks[0]["id"])
            info["first_chunk_id"] = chunks[0]["id"]
            info["first_chunk_chars"] = len((ch or {}).get("text") or "")
    except Exception as e:
        info["chunks_error"] = str(e)
    return jsonify(info)



@app.route("/api/documents/<doc_id>/known/reset", methods=["POST"])
def api_known_reset(doc_id):
    try:
        from phase_engine import reset_known
    except Exception as e:
        return jsonify({"ok": False, "error": repr(e)}), 500
    body = request.get_json(silent=True) or {}
    project = request.args.get("project") or body.get("project") or "detective_v7"
    return jsonify(reset_known(project, doc_id))



@app.route("/selfcheck")
def selfcheck():
    """
    Задача 7: ok по живой схеме проекта, не по schema_v7.
    schema_v7_diff — отдельный раздел, на ok не влияет.
    """
    from schema_v7 import SCHEMA
    from documents import redis_get
    from pathlib import Path as _P
    import json as _json

    result = {
        "projects": list(PROJECTS.keys()),
        "codes_loaded": {},
        "missing_props": [],
        "binding_visible_ok": [],
        "binding_visible_fail": [],
        "schema_v7_diff": [],
        "stale_jobs": None,
    }

    # binding_visible data
    bv_path = _P(__file__).resolve().parent / "samples" / "binding_visible.json"
    bv = {}
    if bv_path.exists():
        with open(bv_path, encoding="utf-8") as f:
            bv = _json.load(f)

    for pk in PROJECTS:
        codes = get_codes(pk) or redis_get(f"schema:codes:{pk}") or {}
        if not isinstance(codes, dict):
            codes = {}
        result["codes_loaded"][pk] = {t: len(v) for t, v in codes.items()}

        # живые таблицы проекта
        proj_tables = set((PROJECTS[pk].get("tables") or {}).keys()) | set(codes.keys())

        # missing: таблица проекта без свойств в карте
        for tkey in proj_tables:
            if not codes.get(tkey):
                result["missing_props"].append({"project": pk, "table": tkey, "got": 0})

        # binding_visible: каждое видимое поле есть в живой схеме и type=binding, цель выводится
        for tkey, fields in (bv.get(pk) or {}).items():
            for fname in fields or []:
                meta = None
                table_codes = codes.get(tkey) or {}
                for name, m in table_codes.items():
                    if reg_normalize(name) == reg_normalize(fname):
                        meta = m if isinstance(m, dict) else {"type": "text"}
                        break
                target = reg_relation_target(tkey, fname, pk)
                if meta and meta.get("type") == "binding" and target:
                    result["binding_visible_ok"].append(f"{pk}.{tkey}.{fname}->{target}")
                else:
                    result["binding_visible_fail"].append({
                        "project": pk, "table": tkey, "field": fname,
                        "type": (meta or {}).get("type"),
                        "target": target,
                    })

        # schema_v7_diff (информативно, на ok не влияет)
        schema_tables = set(SCHEMA.keys())
        only_proj = sorted(proj_tables - schema_tables)
        only_schema = sorted(schema_tables - proj_tables)
        if only_proj or only_schema:
            result["schema_v7_diff"].append({
                "project": pk,
                "only_in_project": only_proj,
                "only_in_schema_v7": only_schema,
            })

    # stale jobs
    try:
        from phase_engine import get_stale_startup_status
        st = get_stale_startup_status()
        if st is None:
            result["stale_jobs"] = {"status": "not_started", "note": "обход не запускался"}
        elif isinstance(st, dict) and st.get("status") == "running":
            result["stale_jobs"] = {"status": "running", "note": "обход ещё идёт"}
        else:
            result["stale_jobs"] = st
    except Exception as e:
        result["stale_jobs"] = {"error": str(e)}

    # ok_by_project + schema_not_read
    # карта не прочитана = ни в одной таблице нет свойств (не «словарь пуст»)
    ok_by_project = {}
    schema_not_read = []
    for pk, counts in result["codes_loaded"].items():
        if not any(counts.values()):
            schema_not_read.append(pk)
            continue
        miss = [m for m in result["missing_props"] if m["project"] == pk]
        fail = [f for f in result["binding_visible_fail"] if f.get("project") == pk]
        ok_by_project[pk] = (not miss and not fail)
    # missing_props только по проектам с картой
    result["missing_props"] = [
        m for m in result["missing_props"] if m["project"] not in schema_not_read
    ]
    result["ok_by_project"] = ok_by_project
    result["schema_not_read"] = schema_not_read
    # общий ok: все проекты с картой здоровы
    result["ok"] = bool(ok_by_project) and all(ok_by_project.values())
    return jsonify(result)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
