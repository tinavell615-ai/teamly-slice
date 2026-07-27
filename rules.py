# rules.py
# Правила проверки Библии как данные. build_preview вызывает цикл по RULES.

from __future__ import annotations
from registry import is_relation, relation_target, EMOJI, LEVEL_MARKERS


def _event_location_value(action: dict, project_key: str | None) -> str | None:
    """
    Единый поиск значения локации у события.
    Поле — связь с целью locations (Библия 2.6). Не подстрока и не список имён.
    """
    props = action.get("properties") or {}
    for prop_name, prop_val in props.items():
        if not is_relation("events", prop_name, project_key):
            continue
        if relation_target("events", prop_name, project_key) == "locations":
            val = str(prop_val).strip()
            if val:
                return val
    return None


def r_hooks_need_approval(action: dict, ctx: dict) -> list[dict]:
    """Крючки и секреты — только с подтверждением автора. bible 2.4"""
    if action.get("table_key") in ("hooks", "secrets") and action.get("effective_action") == "создать":
        return [{
            "level": "block",
            "bible": "2.4",
            "text": f"Создание карточки в таблице «{action.get('table_display')}» требует явного подтверждения автора.",
        }]
    return []


def r_relation_no_brackets(action: dict, ctx: dict) -> list[dict]:
    """В полях связей — только чистые имена. bible 2.3"""
    out = []
    tkey = action.get("table_key") or ""
    project_key = ctx.get("project_key")
    for prop_name, prop_val in (action.get("properties") or {}).items():
        if not is_relation(tkey, prop_name, project_key):
            continue
        for n in [x.strip() for x in str(prop_val).replace(";", ",").split(",") if x.strip()]:
            if "(" in n or ")" in n or "[" in n or "]" in n:
                out.append({
                    "level": "warn",
                    "bible": "2.3",
                    "text": f"В поле связи «{prop_name}» найдено описание в скобках: «{n}». Нужны только чистые имена.",
                })
    return out


def r_emoji_prefix(action: dict, ctx: dict) -> list[dict]:
    """Название должно начинаться с эмодзи таблицы или маркера уровня. bible 9.0 + 5.6 Г"""
    title = (action.get("title") or "").strip()
    tkey = action.get("table_key") or ""
    if not title:
        return []
    # допустимы табличные эмодзи и маркеры уровня 🟥🟧🟩
    if any(title.startswith(e) for e in EMOJI.values() if e):
        return []
    if any(title.startswith(m) for m in LEVEL_MARKERS):
        return []
    suggested = EMOJI.get(tkey) or "•"
    return [{
        "level": "warn",
        "bible": "9.0",
        "text": f"В названии «{title}» нет эмодзи-префикса. Рекомендуется: {suggested} {title}",
    }]


def r_event_needs_location(action: dict, ctx: dict) -> list[dict]:
    """Событие обязано иметь локацию. bible 2.6"""
    if action.get("table_key") != "events":
        return []
    project_key = ctx.get("project_key")
    if _event_location_value(action, project_key) is None:
        return [{
            "level": "warn",
            "bible": "2.6",
            "text": f"Событие «{action.get('title')}» не имеет локации.",
        }]
    return []


def r_event_location_scale(action: dict, ctx: dict) -> list[dict]:
    """Масштаб события ↔ масштаб локации (эмодзи). bible 2.6"""
    if action.get("table_key") != "events":
        return []
    title = (action.get("title") or "").strip()
    event_level = None
    if title.startswith("🟥"):
        event_level = "root"
    elif title.startswith("🟧"):
        event_level = "mid"
    elif title.startswith("🟩"):
        event_level = "leaf"
    if event_level != "root":
        return []
    project_key = ctx.get("project_key")
    loc_val = _event_location_value(action, project_key)
    if loc_val and loc_val.startswith("🟩"):
        return [{
            "level": "warn",
            "bible": "2.6",
            "text": (
                f"Масштаб: корневое событие (🟥) привязано к мелкой локации (🟩 «{loc_val}»). "
                "Крупное событие обычно происходит в крупной локации."
            ),
        }]
    return []


def r_property_too_long(action: dict, ctx: dict) -> list[dict]:
    """Длинный текст — в теле, не в свойствах. bible 2.0"""
    out = []
    for prop_name, prop_val in (action.get("properties") or {}).items():
        if len(str(prop_val)) > 200:
            out.append({
                "level": "warn",
                "bible": "2.0",
                "text": (
                    f"Свойство «{prop_name}» слишком длинное ({len(str(prop_val))} символов). "
                    "Длинный текст должен быть в ТЕЛЕ."
                ),
            })
    return out


RULES = [
    r_hooks_need_approval,
    r_relation_no_brackets,
    r_emoji_prefix,
    r_event_needs_location,
    r_event_location_scale,
    r_property_too_long,
]
