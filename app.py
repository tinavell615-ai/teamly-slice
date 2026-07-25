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
CLUSTER = "https://app.teamly.ru"
TOKENS_KEY = "teamly_tokens"

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
    }
}


# Маппинг внутренних ID свойств Teamly → читаемые названия
# Зафиксировано по реальному срезу 25.07.2026. Стабильно, как ID таблиц.
PROPERTY_LABELS = {
    # === События (актуально по schema 25.07.2026) ===
    "lcVz": "Узловой?",
    "K714": "Статус",
    "B4zM": "Хронопорядок",
    "4LZq": "Эпоха / Слой",
    "uHqz": "Источник",
    "Ik4p": "Персонажи",
    "K3b5": "Связанные персонажи",
    "nNmi": "Локации",
    "Vfxy": "Связанные локации",
    "poqo": "Родительское событие",
    "GVsw": "Главы / Части",
    # === Персонажи (оставляем прежние, уточним отдельно) ===
    "QWXk": "Связи",
    "Y7ne": "Связи",
    "eXYm": "События",
    "yB3V": "События",
    "vLi4": "Локации",
    "x919": "Локации",
    "VX3t": "Артефакты/Силы",
    "8LHF": "ID",
    "LC5w": "Год/Возраст",
    "MNDf": "Статус",
    "Mtec": "Слой",
    "pAOs": "Тип",
    # === Локации ===
    "747P": "Связанные персонажи",
    "8iC3": "Связанные события",
    "ZOKQ": "Связанные персонажи",
    "xe2X": "Связанные события",
    "hVwM": "Дочерние локации",
    "y5Br": "Родительская локация",
    "0sAM": "Арки/Главы",
    "i8cY": "Связанные сущности",
    "2chV": "Тип",
    "NcYD": "Слой",
    "ghta": "Родитель",
    "re5V": "Статус",
}

# Опции select-полей: label → {text_lower → option_id}
# Актуально для таблицы События
SELECT_OPTIONS = {
    "Узловой?": {
        "да": "065699cb-1056-4a2a-97de-d76dac77ec87",
        "нет": "d40b53f8-4462-4dce-b10f-90161dc8ea3d",
    },
    "Статус": {
        "закрыто": "b7d66dea-38dd-4aac-bc28-cdbc7fd1f0d0",
        "зафиксировано": "c462d63c-052d-4f1e-83f7-3b5e84a2c681",
    },
    "Эпоха / Слой": {
        "1948": "f72382d9-e0eb-47ab-9a53-e8df5ebbc0ad",
        "1954": "1edfd476-a35d-4fe3-94ef-8f1d8a83cd6e",
        "1955": "ac18f047-a2c6-4fe0-b908-c91b8f5d5609",
        "1961": "6f518aad-a6f8-404b-a9a9-c224d77c48dd",
        "1961 (24.12)": "2e46e1de-3e6f-4d49-a8b0-70f6f376ae4e",
    },
    "Источник": {
        "автор": "0c2e45d4-ad1f-4f07-a39f-860e65d5ee38",
    },
}

VOLUME_LIMITS = {
    "compact": 45000,
    "working": 110000,
    "full": 999999
}


def nav_html(active: str = "") -> str:
    """Простая навигация между срезом и DELTA."""
    items = [
        ("/", "Срез", "slice"),
        ("/delta", "Запись (DELTA)", "delta"),
        ("/delta/bulk", "Массовая", "bulk"),
        ("/status", "Статус", "status"),
    ]
    parts = ['<nav style="font-family:system-ui;padding:12px 20px;background:#1e1e2e;margin-bottom:24px;">']
    for href, label, key in items:
        if key == active:
            parts.append(f'<a href="{href}" style="color:#fff;font-weight:600;margin-right:20px;text-decoration:none;border-bottom:2px solid #7c3aed;padding-bottom:4px;">{label}</a>')
        else:
            parts.append(f'<a href="{href}" style="color:#a1a1aa;margin-right:20px;text-decoration:none;">{label}</a>')
    parts.append('</nav>')
    return "".join(parts)


# ===================== NAME RESOLVER (Task C) =====================
import re
import uuid

# Эмодзи-префиксы, которые нужно игнорировать при сопоставлении
EMOJI_PREFIX_RE = re.compile(
    r'^[\U0001F300-\U0001F9FF\U00002600-\U000027BF\U0001F600-\U0001F64F'
    r'\U0001F680-\U0001F6FF\U0001F1E0-\U0001F1FF\U00002702-\U000027B0'
    r'\U000024C2-\U0001F251\U0001F900-\U0001F9FF\U0001FA00-\U0001FA6F'
    r'\U0001FA70-\U0001FAFF\U00002600-\U000026FF\s]*'
)

def normalize_title(title: str) -> str:
    """Убирает эмодзи-префиксы, лишние пробелы, приводит к нижнему регистру."""
    if not title:
        return ""
    t = EMOJI_PREFIX_RE.sub("", title)
    t = re.sub(r'\s+', ' ', t).strip().lower()
    return t

def build_title_to_ids(project_key: str = "burevestnik") -> dict:
    """
    Строит словарь: table_key → {normalized_title → [id, ...]}
    Один title может иметь несколько id (дубли) — тогда вопрос автору.
    """
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
                norm = normalize_title(title)
                if not norm:
                    continue
                # общий
                title_to_ids.setdefault(norm, []).append((table_key, cid, title))
                # по таблице
                per_table[table_key].setdefault(norm, []).append((cid, title))
        except Exception as e:
            print(f"[resolver] Ошибка загрузки {table_key}: {e}")
    
    return {
        "global": title_to_ids,
        "per_table": per_table
    }

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
    norm = normalize_title(name)
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

TABLE_ALIASES = {
    "мир": "world",
    "локации": "locations",
    "персонажи": "characters",
    "крючки": "hooks",
    "секреты": "secrets",
    "события": "events",
    "главы": "chapters",
    "архив": "archive",
}

TABLE_DISPLAY = {
    "world": "Мир",
    "locations": "Локации",
    "characters": "Персонажи",
    "hooks": "Крючки",
    "secrets": "Секреты",
    "events": "События",
    "chapters": "Главы",
    "archive": "Архив",
}

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
                key = TABLE_ALIASES.get(raw)
                if key:
                    action["table_key"] = key
                    action["table_display"] = TABLE_DISPLAY.get(key, raw)
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
                in_body = True
                body_mode = "replace"
                rest = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
                if rest:
                    body_lines.append(rest)
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


def build_preview(delta_text: str, project_key: str = "burevestnik") -> dict:
    """
    Полный предпросмотр: creates / updates / warnings / questions.
    """
    actions = parse_delta(delta_text)
    resolver_data = build_title_to_ids(project_key)

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

        # ========== ВАЛИДАЦИЯ ПО ПРАВИЛАМ БИБЛИИ (Task E) ==========

        # 1. Крючки и Секреты — только по явному подтверждению
        if table_key in ("hooks", "secrets") and effective_action == "создать":
            q = f"⚠ Создание карточки в таблице «{act['table_display']}» требует явного подтверждения автора."
            if q not in preview["questions"]:
                preview["questions"].append(q)
                preview["warnings"].append(q)
            # блокируем автоматическое применение
            preview["ok"] = False

        # 2. Один персонаж = одна карточка (запрет «Тень Влада» при существующем «Влад»)
        if table_key == "characters" and effective_action == "создать":
            norm_new = normalize_title(title)
            # проверяем, не является ли новое имя «расширением» уже существующего
            per_table = resolver_data.get("per_table", {}).get("characters", {})
            for existing_norm, matches in per_table.items():
                if existing_norm and existing_norm != norm_new:
                    # если существующее имя целиком входит в новое (и длиннее 3 символов)
                    if len(existing_norm) > 3 and existing_norm in norm_new:
                        existing_title = matches[0][1] if matches else existing_norm
                        q = (f"⚠ Возможный дубль персонажа: «{title}» содержит существующее имя «{existing_title}». "
                             f"По правилу «один персонаж = одна карточка» это, скорее всего, статус/форма, а не новая сущность.")
                        if q not in preview["questions"]:
                            preview["questions"].append(q)
                            preview["warnings"].append(q)

        # 3. В полях связей — только чистые имена (без скобок и описаний)
        dirty_rel_props = []
        for prop_name, prop_val in act["properties"].items():
            low = prop_name.lower()
            if low in ("участники", "pov", "локации", "родительское событие",
                       "родительская локация", "связанные персонажи", "главы"):
                names = [n.strip() for n in re.split(r'[,;]', prop_val) if n.strip()]
                for n in names:
                    if "(" in n or ")" in n or "[" in n or "]" in n:
                        dirty_rel_props.append(f"«{n}» в поле «{prop_name}»")
        if dirty_rel_props:
            q = ("⚠ В полях связей найдены описания в скобках (нарушение правила 2.3). "
                 "Нужны только чистые имена. Проблемные значения: " + "; ".join(dirty_rel_props[:5]))
            if q not in preview["questions"]:
                preview["questions"].append(q)
                preview["warnings"].append(q)

        # 4. Эмодзи-маркировка обязательна в названиях
        EMOJI_START = re.compile(r'^[\U0001F300-\U0001F9FF\U00002600-\U000027BF\U0001F600-\U0001F64F'
                                 r'\U0001F680-\U0001F6FF\U0001F1E0-\U0001F1FF\U00002702-\U000027B0'
                                 r'\U000024C2-\U0001F251\U0001F900-\U0001F9FF\U0001FA00-\U0001FA6F'
                                 r'\U0001FA70-\U0001FAFF\U00002600-\U000026FF]')
        if title and not EMOJI_START.match(title.strip()):
            # предлагаем префикс по таблице
            suggested = {
                "events": "🟧",
                "characters": "👤",
                "locations": "📍",
                "hooks": "⚡",
                "secrets": "🔒",
                "chapters": "📖",
                "world": "🌍",
            }.get(table_key, "•")
            q = (f"⚠ В названии «{title}» нет эмодзи-префикса (раздел 9). "
                 f"Рекомендуется: {suggested} {title}")
            if q not in preview["questions"]:
                preview["questions"].append(q)
                preview["warnings"].append(q)

        # 5. Событие обязано иметь Локацию
        if table_key == "events":
            has_loc = False
            for prop_name in act["properties"]:
                if prop_name.lower() in ("локации", "локация", "locations", "location"):
                    if act["properties"][prop_name].strip():
                        has_loc = True
                        break
            if not has_loc:
                q = f"⚠ Событие «{title}» не имеет локации (правило 2.6). Событие без места — нарушение."
                if q not in preview["questions"]:
                    preview["questions"].append(q)
                    preview["warnings"].append(q)

        # 6. Масштаб события ↔ масштаб локации (грубая проверка по эмодзи)
        if table_key == "events":
            event_level = None
            if title.strip().startswith("🟥"):
                event_level = "root"
            elif title.strip().startswith("🟧"):
                event_level = "mid"
            elif title.strip().startswith("🟩"):
                event_level = "leaf"
            loc_val = None
            for prop_name, prop_val in act["properties"].items():
                if prop_name.lower() in ("локации", "локация"):
                    loc_val = prop_val.strip()
                    break
            if event_level == "root" and loc_val and loc_val.startswith("🟩"):
                q = (f"⚠ Масштаб: корневое событие (🟥) привязано к мелкой локации (🟩 «{loc_val}»). "
                     "Крупное событие обычно происходит в крупной локации.")
                if q not in preview["questions"]:
                    preview["questions"].append(q)
                    preview["warnings"].append(q)

        # 7. Описательный текст не должен попадать в свойства (>200 символов)
        for prop_name, prop_val in act["properties"].items():
            if len(str(prop_val)) > 200:
                q = (f"⚠ Свойство «{prop_name}» слишком длинное ({len(str(prop_val))} символов). "
                     "Длинный текст должен быть в ТЕЛЕ, а не в свойствах (правило 2.0).")
                if q not in preview["questions"]:
                    preview["questions"].append(q)
                    preview["warnings"].append(q)

        # ========== РЕЗОЛВ СВЯЗЕЙ (после валидации) ==========
        for prop_name, prop_val in act["properties"].items():
            low = prop_name.lower()
            if low in ("участники", "pov", "локации", "родительское событие",
                       "родительская локация", "связанные персонажи", "главы"):
                names = [n.strip() for n in re.split(r'[,;]', prop_val) if n.strip()]
                for n in names:
                    # очищаем от возможного мусора в скобках для резолва
                    clean_n = re.sub(r'\s*[\(\[\{].*?[\)\]\}]\s*', '', n).strip()
                    if not clean_n:
                        continue
                    if low in ("участники", "pov", "связанные персонажи"):
                        rel_table = "characters"
                    elif low in ("локации", "родительская локация"):
                        rel_table = "locations"
                    elif low == "родительское событие":
                        rel_table = "events"
                    elif low == "главы":
                        rel_table = "chapters"
                    else:
                        rel_table = table_key
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

    parts = [nav_html("delta")]
    parts.append('<div style="font-family: system-ui, sans-serif; max-width: 900px; margin: 20px auto; padding: 20px;">')
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
WRITE_LOG = []

def _log_write(action: str, title: str, table: str, success: bool, detail: str = ""):
    entry = {
        "ts": dt.now().isoformat(timespec="seconds"),
        "action": action,
        "title": title,
        "table": table,
        "success": success,
        "detail": detail[:500]
    }
    WRITE_LOG.append(entry)
    status = "OK" if success else "FAIL"
    print(f"[write] {status} | {table} | {action} | {title} | {detail[:120]}")

# Обратный словарь label → code (берём первое вхождение)
LABEL_TO_CODE = {}
for code, label in PROPERTY_LABELS.items():
    if label not in LABEL_TO_CODE:
        LABEL_TO_CODE[label] = code

def _get_table_id(project_key: str, table_key: str) -> str | None:
    project = PROJECTS.get(project_key)
    if not project:
        return None
    return project["tables"].get(table_key)

def create_article_in_table(table_id: str, title: str, properties: dict, project_key: str = "burevestnik", resolver_data: dict | None = None, table_key: str = "") -> dict:
    """
    Создаёт строку (статью) в умной таблице.
    Возвращает {"ok": bool, "id": str|None, "error": str|None}
    """
    new_id = str(uuid.uuid4())
    prop_list = build_properties_payload(properties, resolver_data, table_key)

    # Пробуем несколько вариантов payload — Teamly требует title
    payloads_to_try = [
        # вариант 1: title внутри entity + properties
        {
            "code": "article_create",
            "payload": {
                "entity": {
                    "spaceId": table_id,
                    "id": new_id,
                    "title": title,
                    "properties": prop_list
                }
            }
        },
        # вариант 2: title как отдельное свойство (некоторые схемы)
        {
            "code": "article_create",
            "payload": {
                "entity": {
                    "spaceId": table_id,
                    "id": new_id,
                    "properties": [{"method": "add", "code": "title", "value": title}] + prop_list
                }
            }
        },
        # вариант 3: оригинальный (без title) — на случай если предыдущие упадут
        {
            "code": "article_create",
            "payload": {
                "entity": {
                    "spaceId": table_id,
                    "id": new_id,
                    "properties": prop_list
                }
            }
        },
    ]

    last_err = None
    for i, payload in enumerate(payloads_to_try):
        try:
            print(f"[write] create attempt {i+1}: title={title!r}, props={len(prop_list)}")
            result = api("/api/v1/wiki/properties/command/execute", payload)
            print(f"[write] create response: {str(result)[:300]}")
            _log_write("create", title, table_id, True, f"id={new_id} attempt={i+1}")
            return {"ok": True, "id": new_id, "error": None, "raw": result}
        except Exception as e:
            last_err = str(e)
            print(f"[write] create attempt {i+1} failed: {e}")
            continue

    _log_write("create", title, table_id, False, last_err or "all attempts failed")
    return {"ok": False, "id": None, "error": last_err or "all attempts failed"}

PROP_ALIASES = {
    "локация": "Связанные локации",
    "локации": "Связанные локации",
    "связанные локации": "Связанные локации",
    # в таблице События видимая колонка = Связанные персонажи (K3b5)
    "участники": "Связанные персонажи",
    "персонаж": "Связанные персонажи",
    "персонажи": "Связанные персонажи",
    "связанные персонажи": "Связанные персонажи",
    "родитель": "Родительское событие",
    "родительское событие": "Родительское событие",
    "узловой": "Узловой?",
    "эпоха": "Эпоха / Слой",
    "слой": "Эпоха / Слой",
}

def _resolve_prop_code(label: str) -> str | None:
    canon = PROP_ALIASES.get(label.lower().rstrip("?").strip(), label)
    code = LABEL_TO_CODE.get(canon)
    if code:
        return code
    code = LABEL_TO_CODE.get(label)
    if code:
        return code
    norm = label.lower().rstrip("?").strip()
    for lab, c in LABEL_TO_CODE.items():
        if lab.lower().rstrip("?").strip() == norm:
            return c
    return None

def _is_relation_label(label: str) -> bool:
    low = label.lower()
    return any(x in low for x in (
        "участник", "pov", "локац", "родител", "связан", "глав", "событи", "персонаж"
    ))

def build_properties_payload(properties: dict, resolver_data: dict | None = None, table_key: str = "") -> list:
    """
    Формирует список properties для command/execute.
    - select → option id
    - binding/relation → list of article ids
    - number/text → as-is
    """
    prop_list = []
    for label, value in properties.items():
        code = _resolve_prop_code(label)
        if not code:
            print(f"[write] Нет code для «{label}» — пропускаю")
            continue

        # 1. Select: текст → option id
        sel_key = None
        if label in SELECT_OPTIONS:
            sel_key = label
        else:
            for k in SELECT_OPTIONS:
                if k.lower().rstrip("?").strip() == label.lower().rstrip("?").strip():
                    sel_key = k
                    break
        if sel_key:
            opts = SELECT_OPTIONS[sel_key]
            key = str(value).strip().lower()
            if key in opts:
                value = opts[key]
                print(f"[write] select «{label}»={key} → {value}")
            else:
                print(f"[write] select «{label}»: неизвестная опция «{value}», варианты: {list(opts.keys())}")

        # 2. Связи / binding: имена → id
        elif _is_relation_label(label) and resolver_data is not None:
            names = [n.strip() for n in re.split(r'[,;]', str(value)) if n.strip()]
            ids = []
            low = label.lower()
            if any(x in low for x in ("участник", "pov", "персонаж")):
                rel_table = "characters"
            elif any(x in low for x in ("локац",)):
                rel_table = "locations"
            elif "родител" in low:
                rel_table = "events"
            elif "глав" in low:
                rel_table = "chapters"
            else:
                rel_table = table_key or "events"
            for n in names:
                clean = re.sub(r'\s*[\(\[\{].*?[\)\]\}]\s*', '', n).strip()
                r = resolve_name(clean, rel_table, resolver_data)
                if r["status"] == "ok":
                    ids.append(r["id"])
                    print(f"[write] связь «{n}» → {r['id']}")
                else:
                    print(f"[write] связь «{n}» НЕ резолвнута: {r.get('question')}")
            if not ids:
                print(f"[write] binding «{label}»: нет id — пропускаю свойство")
                continue  # не добавляем в prop_list
            # Teamly binding: список объектов {id: ...}
            value = [{"id": i} for i in ids]
            print(f"[write] binding «{label}» value={value!r}")

        # 3. Number
        elif label in ("Хронопорядок",):
            try:
                value = int(str(value).replace(" ", "").replace(",", ""))
            except ValueError:
                pass

        prop_list.append({
            "method": "add",
            "code": code,
            "value": value
        })
    return prop_list

def update_article_properties(table_id: str, article_id: str, properties: dict, title: str = "",
                               resolver_data: dict | None = None, table_key: str = "") -> dict:
    """
    Обновляет свойства существующей карточки.
    Живой формат Teamly (перехват UI 25.07.2026):
    code: property_update
    entity: {spaceId, articleId}
    operation: {method: update, code, value}
    Каждое свойство — отдельный вызов.
    """
    prop_list = build_properties_payload(properties, resolver_data, table_key)
    if not prop_list:
        return {"ok": True, "error": None}

    errors = []
    ok_count = 0
    for p in prop_list:
        payload = {
            "code": "property_update",
            "internal": False,
            "payload": {
                "entity": {
                    "spaceId": table_id,
                    "articleId": article_id
                },
                "operation": {
                    "method": "update",
                    "code": p["code"],
                    "value": p["value"]
                }
            }
        }
        try:
            result = api("/api/v1/wiki/properties/command/execute", payload)
            print(f"[write] property_update {p['code']} OK: {str(result)[:150]}")
            ok_count += 1
        except Exception as e:
            err = str(e)
            print(f"[write] property_update {p['code']} FAIL: {err[:200]}")
            errors.append(f"{p['code']}: {err[:120]}")

    if ok_count == len(prop_list):
        _log_write("update_props", title or article_id, table_id, True, f"ok={ok_count}")
        return {"ok": True, "error": None}
    if ok_count > 0:
        _log_write("update_props", title or article_id, table_id, True, f"partial ok={ok_count} err={errors}")
        return {"ok": True, "error": f"частично: {'; '.join(errors)}"}
    _log_write("update_props", title or article_id, table_id, False, str(errors))
    return {"ok": False, "error": "; ".join(errors)}



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
        # пустой ответ при 200/204 считаем успехом
        if isinstance(result, dict) and result.get("_empty"):
            _log_write("append_body", title or article_id, space_id, True, f"len={len(text)} empty_ok")
            return {"ok": True, "error": None}
        _log_write("append_body", title or article_id, space_id, True, f"len={len(text)}")
        return {"ok": True, "error": None, "raw": result}
    except Exception as e:
        _log_write("append_body", title or article_id, space_id, False, str(e))
        return {"ok": False, "error": str(e)}

def replace_body(space_id: str, article_id: str, text: str, title: str = "") -> dict:
    """
    Полная замена тела. Документация merge только добавляет.
    Пока делаем append с пометкой; при живом тесте найдём endpoint replace.
    """
    # Временное решение: append с разделителем
    marked = f"\n\n---\n[ПОЛНАЯ ЗАМЕНА ТЕЛА]\n{text}"
    return append_body(space_id, article_id, marked, title)

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
                # Связи идут вместе с create (формат [{id}] уже выставлен в build_properties_payload)
                result = create_article_in_table(table_id, title, act["properties"], project_key, resolver_data, table_key)
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
                    space_id = table_id  # в умной таблице spaceId = tableId
                    if act["body_mode"] == "append":
                        br = append_body(space_id, article_id, act["body"], title)
                    else:
                        br = replace_body(space_id, article_id, act["body"], title)
                    if not br["ok"]:
                        report["failed"].append({
                            "title": title,
                            "table": act.get("table_display"),
                            "error": f"Карточка создана, но тело не записалось: {br['error']}"
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
                    ur = update_article_properties(table_id, article_id, act["properties"], title, resolver_data, table_key)
                    if not ur["ok"]:
                        report["failed"].append({
                            "title": title,
                            "table": act.get("table_display"),
                            "error": f"Свойства: {ur['error']}"
                        })
                        continue
                # тело
                if act.get("body"):
                    if act["body_mode"] == "append":
                        br = append_body(table_id, article_id, act["body"], title)
                    else:
                        br = replace_body(table_id, article_id, act["body"], title)
                    if not br["ok"]:
                        report["failed"].append({
                            "title": title,
                            "table": act.get("table_display"),
                            "error": f"Тело: {br['error']}"
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

    report["log"] = list(WRITE_LOG[-50:])  # последние 50 записей
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


# ===================== BULK / TWO-PASS (Task H) =====================
import threading
from datetime import datetime, timezone

JOBS = {}
JOBS_LOCK = threading.Lock()
THROTTLE_SEC = 0.08  # ~12 req/s, устойчивость без долбёжки

def _job_log(job, msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    job["log"].append(line)
    print(f"[bulk {job['id'][:8]}] {msg}")

def split_two_pass(actions: list) -> tuple:
    """
    Проход 1: create/update без relation-полей.
    Проход 2: только relation-поля (для create — update после появления id;
              для update — сразу property_update).
    """
    pass1, pass2 = [], []
    for act in actions:
        props = act.get("properties") or {}
        plain = {k: v for k, v in props.items() if not _is_relation_label(k)}
        rels  = {k: v for k, v in props.items() if _is_relation_label(k)}
        a1 = dict(act)
        a1["properties"] = plain
        a1["_rels"] = rels
        pass1.append(a1)
        if rels:
            a2 = dict(act)
            a2["properties"] = rels
            a2["_plain_done"] = True
            pass2.append(a2)
    return pass1, pass2

def estimate_requests(pass1, pass2) -> dict:
    creates = sum(1 for a in pass1 if a.get("action") in ("создать", "create") or True)
    # грубо: create+body на каждую pass1, + property_update на каждое rel-поле
    n1 = len(pass1) * 2  # create + body (если есть)
    n2 = sum(len(a.get("properties") or {}) for a in pass2)
    return {
        "pass1_est": n1,
        "pass2_est": n2,
        "total_est": n1 + n2,
        "cards": len(pass1),
        "rel_ops": n2,
    }

def build_bulk_summary(actions: list) -> dict:
    by_table = {}
    for a in actions:
        t = a.get("table_display") or a.get("table") or "?"
        by_table.setdefault(t, {"create": 0, "update": 0})
        act = (a.get("action") or "создать").lower()
        if act in ("обновить", "update"):
            by_table[t]["update"] += 1
        else:
            by_table[t]["create"] += 1
    return by_table

def run_bulk_job(job_id: str):
    """Фоновый runner: pass1 → pass2, с троттлингом и журналом."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return
    try:
        project_key = job.get("project_key", "burevestnik")
        project = PROJECTS[project_key]
        resolver_data = build_title_to_ids(project_key)
        title_to_id = {}  # (table_key, normalize_title(title)) → article_id

        # ----- PASS 1 -----
        job["status"] = "pass1"
        _job_log(job, f"Проход 1: {len(job['pass1'])} карточек")
        for i, act in enumerate(job["pass1"]):
            if job.get("cancel"):
                job["status"] = "cancelled"
                _job_log(job, "Отменено пользователем")
                return
            title = act.get("title") or ""
            table_key = act.get("table_key") or act.get("table") or ""
            table_id = project["tables"].get(table_key)
            if not table_id:
                # resolve table from display name
                for k, disp in TABLE_DISPLAY.items():
                    if disp == act.get("table_display") or k == table_key:
                        table_key = k
                        table_id = project["tables"].get(k)
                        break
            if not table_id:
                job["errors"].append({"title": title, "error": f"нет table_id для {table_key}"})
                job["done"] += 1
                continue

            # идемпотентность
            effective = act.get("action", "создать")
            existing = None
            try:
                from_resolver = resolve_name(title, table_key, resolver_data)
                if from_resolver.get("status") == "ok":
                    existing = from_resolver["id"]
                    effective = "обновить"
            except Exception:
                pass

            try:
                if effective in ("создать", "create") and not existing:
                    result = create_article_in_table(
                        table_id, title, act.get("properties") or {},
                        project_key, resolver_data, table_key
                    )
                    if not result["ok"]:
                        job["errors"].append({"title": title, "error": result["error"]})
                    else:
                        aid = result["id"]
                        title_to_id[(table_key, normalize_title(title))] = aid
                        if act.get("body"):
                            time.sleep(THROTTLE_SEC)
                            append_body(table_id, aid, act["body"], title)
                        _job_log(job, f"create OK «{title}»")
                else:
                    aid = existing
                    title_to_id[(table_key, normalize_title(title))] = aid
                    if act.get("properties"):
                        ur = update_article_properties(
                            table_id, aid, act["properties"], title, resolver_data, table_key
                        )
                        if not ur["ok"]:
                            job["errors"].append({"title": title, "error": f"props: {ur['error']}"})
                    if act.get("body"):
                        time.sleep(THROTTLE_SEC)
                        if act.get("body_mode") == "replace":
                            try:
                                replace_body(table_id, aid, act["body"], title)
                            except NameError:
                                append_body(table_id, aid, act["body"], title)
                        else:
                            append_body(table_id, aid, act["body"], title)
                    _job_log(job, f"update OK «{title}»")
            except Exception as e:
                job["errors"].append({"title": title, "error": str(e)[:200]})
                _job_log(job, f"FAIL «{title}»: {e}")

            job["done"] += 1
            time.sleep(THROTTLE_SEC)

        # ----- PASS 2: relations -----
        job["status"] = "pass2"
        job["pass2_done"] = 0
        _job_log(job, f"Проход 2: {len(job['pass2'])} карточек со связями")
        # обновить resolver — новые id
        resolver_data = build_title_to_ids(project_key)

        for act in job["pass2"]:
            if job.get("cancel"):
                job["status"] = "cancelled"
                return
            title = act.get("title") or ""
            table_key = act.get("table_key") or ""
            table_id = project["tables"].get(table_key)
            if not table_id:
                for k, disp in TABLE_DISPLAY.items():
                    if disp == act.get("table_display"):
                        table_key = k
                        table_id = project["tables"].get(k)
                        break
            aid = title_to_id.get((table_key, normalize_title(title)))
            if not aid:
                r = resolve_name(title, table_key, resolver_data)
                if r.get("status") == "ok":
                    aid = r["id"]
            if not aid:
                job["errors"].append({"title": title, "error": "нет id для прохода 2"})
                job["pass2_done"] += 1
                job["done"] += 1
                continue
            try:
                ur = update_article_properties(
                    table_id, aid, act.get("properties") or {}, title, resolver_data, table_key
                )
                if not ur["ok"]:
                    job["errors"].append({"title": title, "error": f"rels: {ur['error']}"})
                else:
                    _job_log(job, f"rels OK «{title}»")
            except Exception as e:
                job["errors"].append({"title": title, "error": str(e)[:200]})
            job["pass2_done"] += 1
            job["done"] += 1
            time.sleep(THROTTLE_SEC)

        job["status"] = "done"
        _job_log(job, f"Готово. ошибок: {len(job['errors'])}")
    except Exception as e:
        job["status"] = "error"
        job["fatal"] = str(e)
        _job_log(job, f"FATAL: {e}")
        import traceback
        traceback.print_exc()


def start_bulk_job(delta_text: str, tables_filter: list | None = None,
                   project_key: str = "burevestnik") -> dict:
    actions = parse_delta(delta_text)
    # filter tables
    if tables_filter:
        allowed = set(tables_filter)
        filtered = []
        for a in actions:
            tk = a.get("table_key") or ""
            td = a.get("table_display") or ""
            if tk in allowed or td in allowed:
                filtered.append(a)
            else:
                # try map display
                for k, disp in TABLE_DISPLAY.items():
                    if disp == td and k in allowed:
                        filtered.append(a)
                        break
        actions = filtered

    pass1, pass2 = split_two_pass(actions)
    est = estimate_requests(pass1, pass2)
    summary = build_bulk_summary(actions)
    job_id = str(uuid.uuid4())
    job = {
        "id": job_id,
        "status": "queued",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "total": len(pass1) + len(pass2),
        "done": 0,
        "pass2_done": 0,
        "errors": [],
        "log": [],
        "summary": summary,
        "estimate": est,
        "pass1": pass1,
        "pass2": pass2,
        "project_key": project_key,
        "cancel": False,
    }
    with JOBS_LOCK:
        JOBS[job_id] = job
    t = threading.Thread(target=run_bulk_job, args=(job_id,), daemon=True)
    t.start()
    return job

# TABLE_DISPLAY helper if missing
try:
    TABLE_DISPLAY
except NameError:
    TABLE_DISPLAY = {
        "characters": "Персонажи",
        "world": "Мир",
        "locations": "Локации",
        "events": "События",
        "chapters": "Главы / Части",
    }


# ===================== DELTA UI (Task D) =====================

@app.route("/delta", methods=["GET", "POST"])
def delta_page():
    """Страница ввода DELTA и показа предпросмотра."""
    if request.method == "GET":
        return f"""
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>DELTA to Teamly</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 900px; margin: 0 auto; padding: 0 20px 40px; }}
textarea {{ width: 100%; height: 420px; font-family: ui-monospace, monospace; font-size: 13px; padding: 12px; border: 1px solid #ccc; border-radius: 8px; }}
button {{ margin-top: 16px; padding: 12px 28px; font-size: 16px; background: #4f46e5; color: white; border: none; border-radius: 8px; cursor: pointer; }}
h1 {{ margin-bottom: 8px; }}
.hint {{ color: #666; font-size: 0.9rem; margin-bottom: 20px; }}
</style>
</head>
<body>
{nav_html("delta")}
<h1>Обратный канал — DELTA</h1>
<p class="hint">Вставь блок === DELTA === ... === КОНЕЦ DELTA === и нажми «Показать предпросмотр».<br>
Ничего не записывается в Teamly до явного подтверждения.</p>
<form method="POST" action="/delta/preview">
<textarea name="delta" placeholder="=== DELTA ===\nТАБЛИЦА: События\n..."></textarea>
<br>
<button type="submit">Показать предпросмотр</button>
</form>
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
    parts = [nav_html("delta")]
    parts.append('<div style="font-family:system-ui;max-width:800px;margin:40px auto;padding:20px;">')
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
    print(f"[API] {r.status_code} {endpoint} body_len={len(r.text)}")
    if r.status_code not in (200, 201, 204):
        if r.status_code == 401:
            print("[API] 401 Unauthorized — токен истёк или refresh не удался")
        raise Exception(f"API {r.status_code}: {r.text[:400]}")
    if not r.text or not r.text.strip():
        return {"_empty": True, "status": r.status_code}
    try:
        return r.json()
    except Exception as e:
        print(f"[API] JSON parse failed: {e}, raw[:200]={r.text[:200]!r}")
        return {"_raw": r.text[:500], "status": r.status_code}

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

def format_card(card, id_to_title):
    """Формирует блок карточки с резолвнутыми связями, без дублей и с резолвом статусов."""
    lines = [f"### {card['title']}"]
    props = card.get("properties") or {}
    
    meta = {}          # label → value (последний побеждает)
    relations = {}     # label → set of names (дедуп)
    
    for k, v in props.items():
        label = PROPERTY_LABELS.get(k, k)
        if label in ("ID",) or k in ("4LZq", "8LHF"):
            continue
        
        # Простые значения или одиночные UUID (статусы, типы и т.п.)
        if not isinstance(v, (list, dict)):
            if v is None or str(v).strip() in ("", "None", "null"):
                continue
            val = str(v).strip()
            # Если это UUID — пробуем резолвить как relation
            if len(val) == 36 and val.count("-") == 4:
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
    # Главы / Арки как основной выбор
    error_msg = None
    chapter_boxes = ""
    event_boxes = ""
    try:
        chapters = get_chapters_for_select(PROJECTS["burevestnik"]["tables"]["chapters"])
        for ch in chapters:
            chapter_boxes += f'<label><input type="checkbox" name="chapters" value="{ch["id"]}"> {ch["title"]}</label>\n'
        
        # События оставляем как дополнительный режим
        events = get_all_events(PROJECTS["burevestnik"]["tables"]["events"])
        by_id, children, roots = build_tree(events)
        selectable = []
        for rid in roots:
            selectable.append(by_id[rid])
            for cid in children.get(rid, [])[:6]:
                selectable.append(by_id[cid])
        for ev in selectable:
            event_boxes += f'<label><input type="checkbox" name="events" value="{ev["id"]}"> {ev["title"]}</label>\n'
    except Exception as e:
        error_msg = str(e)
        chapter_boxes = ""
        event_boxes = ""

    return f"""
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Срез Teamly</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 680px; margin: 40px auto; padding: 0 20px; color: #1a1a1a; }}
        h1 {{ font-size: 1.5rem; }}
        label {{ display: block; margin: 8px 0; }}
        .section {{ margin: 22px 0; }}
        .checkboxes {{ max-height: 280px; overflow-y: auto; border: 1px solid #ddd; padding: 12px; border-radius: 8px; }}
        select {{ width: 100%; padding: 10px; font-size: 1rem; border-radius: 8px; border: 1px solid #ccc; }}
        button {{ margin-top: 24px; width: 100%; padding: 14px; font-size: 1.1rem; background: #4f46e5; color: white; border: none; border-radius: 10px; cursor: pointer; }}
        .hint {{ font-size: 0.85rem; color: #666; }}
        .error {{ color: #b91c1c; background: #fef2f2; padding: 10px; border-radius: 8px; }}
    </style>
</head>
<body>
""" + nav_html("slice") + """
    <h1>Срез базы Teamly</h1>
    <p class="hint">Выбери арки/главы — система подтянет то, что было до них</p>

    {"<div class='error'>Не удалось загрузить список событий: " + error_msg + "</div>" if error_msg else ""}

    <form action="/slice" method="get">
        <div class="section">
            <strong>Проект</strong>
            <select name="project">
                <option value="burevestnik">Буревестник</option>
            </select>
        </div>

        <div class="section">
            <strong>Арки / Главы (можно несколько)</strong>
            <div class="checkboxes">
                {chapter_boxes if chapter_boxes else "<p>Главы не загрузились</p>"}
            </div>
            <p class="hint">Если ничего не выбрать — будут взяты все корневые события</p>
        </div>

        <div class="section">
            <strong>Глубина внутри выбранного</strong>
            <select name="depth">
                <option value="arcs">Только выбранные (без детей)</option>
                <option value="direct_children" selected>Выбранные + прямые дети</option>
                <option value="scenes">Выбранные + вся глубина</option>
            </select>
        </div>

        <div class="section">
            <strong>Таблицы</strong>
            <label><input type="checkbox" name="tables" value="characters" checked> Персонажи</label>
            <label><input type="checkbox" name="tables" value="events" checked> События</label>
            <label><input type="checkbox" name="tables" value="locations" checked> Локации</label>
            <label><input type="checkbox" name="tables" value="direct_children"> Главы / Части</label>
            <label><input type="checkbox" name="tables" value="world"> Мир</label>
        </div>

        <div class="section">
            <strong>Объём</strong>
            <select name="volume">
                <option value="compact">Компактный (~45 тыс.)</option>
                <option value="working" selected>Рабочий (~110 тыс.)</option>
                <option value="full">Полный</option>
            </select>
        </div>

        <button type="submit">Собрать срез</button>
    </form>
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
                block = format_card(card, id_to_title)
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
                block = format_card(card, id_to_title)
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
    if key != "tvell-debug-2026":
        return jsonify({"error": "forbidden"}), 403
    try:
        with _lock:
            ok = _do_refresh()
            st = get_status()
            st["force_refresh_ok"] = ok
            return jsonify(st)
    except Exception as e:
        return jsonify({"error": str(e), "force_refresh_ok": False}), 500


@app.route("/status")
def status():
    return jsonify(get_status())



start_proactive_refresh()


@app.route("/debug/props/<article_id>")
def debug_props(article_id):
    """Сырые свойства карточки + schema таблицы События."""
    try:
        card = api("/api/v1/wiki/ql/article", {
            "query": {
                "__filter": {"id": article_id},
                "id": True,
                "title": True,
                "properties": {"properties": True},
                "schemaProperties": True
            }
        })
        # schema таблицы
        table_id = PROJECTS["burevestnik"]["tables"]["events"]
        schema = api("/api/v1/ql/content-database/content", {
            "query": {
                "__filter": {"contentDatabaseId": table_id},
                "schemaProperties": {
                    "id": True, "name": True, "type": True, "code": True,
                    "format": True, "options": True, "propertyId": True
                }
            }
        })
        import json
        return f"<pre>{json.dumps({'card': card, 'schema_sample': schema}, ensure_ascii=False, indent=2)[:15000]}</pre>"
    except Exception as e:
        import traceback
        return f"<pre>{e}\n{traceback.format_exc()}</pre>", 500



@app.route("/debug/schema/<table_key>")
def debug_schema(table_key):
    """Схема свойств таблицы."""
    table_id = PROJECTS["burevestnik"]["tables"].get(table_key)
    if not table_id:
        return f"Unknown table: {table_key}", 404
    try:
        data = api("/api/v1/ql/content-database/content", {
            "query": {
                "__filter": {"contentDatabaseId": table_id},
                "schemaProperties": {
                    "id": True, "name": True, "type": True, "code": True,
                    "format": True, "options": True, "propertyId": True,
                    "protected": True, "hide": True
                }
            }
        })
        import json
        props = data.get("schemaProperties", [])
        # compact view
        compact = []
        for p in props:
            compact.append({
                "name": p.get("name"),
                "code": p.get("code"),
                "type": p.get("type"),
                "format": p.get("format"),
                "options": p.get("options"),
                "propertyId": p.get("propertyId")
            })
        return f"<pre>{json.dumps(compact, ensure_ascii=False, indent=2)}</pre>"
    except Exception as e:
        import traceback
        return f"<pre>{e}\n{traceback.format_exc()}</pre>", 500



@app.route("/delta/bulk", methods=["GET", "POST"])
def delta_bulk():
    """Массовая заливка: предпросмотр сводки → запуск фонового job."""
    if request.method == "GET":
        return f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><title>Массовая запись</title>
<style>
body{{font-family:system-ui;max-width:900px;margin:0 auto;padding:0 20px 40px}}
textarea{{width:100%;height:280px;font-family:ui-monospace,monospace;font-size:13px;padding:12px;border:1px solid #ccc;border-radius:8px}}
button{{margin-top:12px;padding:12px 24px;font-size:15px;background:#4f46e5;color:#fff;border:none;border-radius:8px;cursor:pointer}}
.checks label{{display:inline-block;margin-right:16px;margin-top:8px}}
</style></head><body>
{nav_html("delta")}
<h1>Массовая запись (два прохода)</h1>
<p style="color:#666">Проход 1 — карточки без связей. Проход 2 — все связи. Работает в фоне.</p>
<form method="POST" action="/delta/bulk/preview">
<textarea name="delta" placeholder="=== DELTA ===&#10;..."></textarea>
<div class="checks">
<p><b>Таблицы:</b></p>
<label><input type="checkbox" name="tables" value="characters" checked> Персонажи</label>
<label><input type="checkbox" name="tables" value="locations" checked> Локации</label>
<label><input type="checkbox" name="tables" value="events" checked> События</label>
<label><input type="checkbox" name="tables" value="chapters" checked> Главы</label>
<label><input type="checkbox" name="tables" value="world" checked> Мир</label>
</div>
<br><button type="submit">Сводка и оценка</button>
</form>
</body></html>"""

    return "POST /delta/bulk/preview", 400


@app.route("/delta/bulk/preview", methods=["POST"])
def delta_bulk_preview():
    delta_text = request.form.get("delta", "").strip()
    tables = request.form.getlist("tables")
    if not delta_text:
        return "Пустой DELTA", 400
    try:
        actions = parse_delta(delta_text)
        if tables:
            allowed = set(tables)
            actions = [a for a in actions if a.get("table_key") in allowed]
        pass1, pass2 = split_two_pass(actions)
        est = estimate_requests(pass1, pass2)
        summary = build_bulk_summary(actions)
        # warnings from validate if available
        warnings = []
        try:
            prev = build_preview(delta_text)
            warnings = prev.get("warnings") or prev.get("questions") or []
        except Exception:
            pass

        def esc(s):
            return str(s).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;")

        rows = "".join(
            f"<tr><td>{esc(t)}</td><td>{v['create']}</td><td>{v['update']}</td></tr>"
            for t, v in summary.items()
        )
        warn_html = ""
        if warnings:
            warn_html = "<h3 style='color:#b45309'>Предупреждения</h3><ul>" + \
                "".join(f"<li>{esc(w)}</li>" for w in warnings[:50]) + "</ul>"
        tables_hidden = "".join(f'<input type="hidden" name="tables" value="{esc(t)}">' for t in tables)

        secs = est["total_est"] * (THROTTLE_SEC + 0.15)
        return f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><title>Сводка</title>
<style>
body{{font-family:system-ui;max-width:900px;margin:0 auto;padding:0 20px 40px}}
table{{border-collapse:collapse;width:100%}} th,td{{border:1px solid #ddd;padding:8px;text-align:left}}
button{{padding:12px 24px;background:#059669;color:#fff;border:none;border-radius:8px;font-size:15px;cursor:pointer}}
</style></head><body>
{nav_html("delta")}
<h1>Сводка массовой записи</h1>
<table><tr><th>Таблица</th><th>Создать</th><th>Обновить</th></tr>{rows}</table>
<p>Карточек: <b>{est['cards']}</b> · операций связей: <b>{est['rel_ops']}</b> ·
оценка запросов: ~{est['total_est']} · оценка времени: ~{int(secs)} сек ({secs/60:.1f} мин)</p>
{warn_html}
<form method="POST" action="/delta/bulk/start">
<input type="hidden" name="delta" value="{esc(delta_text)}">
{tables_hidden}
<button type="submit">Запустить в фоне</button>
<a href="/delta/bulk" style="margin-left:16px">← Назад</a>
</form>
</body></html>"""
    except Exception as e:
        import traceback
        return f"<pre>{e}\n{traceback.format_exc()}</pre>", 500


@app.route("/delta/bulk/start", methods=["POST"])
def delta_bulk_start():
    delta_text = request.form.get("delta", "").strip()
    tables = request.form.getlist("tables")
    if not delta_text:
        return "Пустой DELTA", 400
    job = start_bulk_job(delta_text, tables_filter=tables or None)
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="2;url=/delta/job/{job['id']}">
<title>Запуск</title></head><body style="font-family:system-ui;padding:40px">
{nav_html("delta")}
<p>Job {job['id'][:8]}… запущен. Переход к прогрессу…</p>
<a href="/delta/job/{job['id']}">Открыть прогресс</a>
</body></html>"""


@app.route("/delta/job/<job_id>")
def delta_job_page(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return "Job не найден (сервер мог перезапуститься — журнал in-memory)", 404
    total = max(job["total"], 1)
    pct = int(100 * job["done"] / total)
    errs = "".join(f"<li>{e.get('title','?')}: {e.get('error','')}</li>" for e in job["errors"][:30])
    log_tail = "<br>".join(job["log"][-25:])
    refresh = "" if job["status"] in ("done", "error", "cancelled") else '<meta http-equiv="refresh" content="3">'
    return f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">{refresh}
<title>Прогресс {job_id[:8]}</title>
<style>
body{{font-family:system-ui;max-width:900px;margin:0 auto;padding:0 20px 40px}}
.bar{{height:22px;background:#e5e7eb;border-radius:8px;overflow:hidden}}
.fill{{height:100%;background:#4f46e5;width:{pct}%}}
.log{{font-family:ui-monospace,monospace;font-size:12px;background:#f9fafb;padding:12px;border-radius:8px;max-height:320px;overflow:auto}}
</style></head><body>
{nav_html("delta")}
<h1>Прогресс · {job["status"]}</h1>
<p>{job["done"]} / {job["total"]} ({pct}%) · ошибок: {len(job["errors"])}</p>
<div class="bar"><div class="fill"></div></div>
<p style="color:#666;font-size:13px">pass2: {job.get("pass2_done",0)}</p>
{"<h3 style=color:#b91c1c>Ошибки</h3><ul>"+errs+"</ul>" if job["errors"] else ""}
<h3>Журнал</h3>
<div class="log">{log_tail}</div>
<p style="margin-top:20px"><a href="/delta/bulk">← Массовая запись</a> · <a href="/delta">DELTA</a></p>
</body></html>"""


@app.route("/api/job/<job_id>")
def api_job(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return {"error": "not found"}, 404
    return {
        "id": job["id"],
        "status": job["status"],
        "done": job["done"],
        "total": job["total"],
        "errors": len(job["errors"]),
        "log_tail": job["log"][-10:],
    }


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
