# rules.py
# Правила проверки Библии как данные. build_preview вызывает цикл по RULES.

from __future__ import annotations
from registry import is_relation, EMOJI


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
    for prop_name, prop_val in (action.get("properties") or {}).items():
        if not is_relation(tkey, prop_name):
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
    """Название должно начинаться с эмодзи таблицы. bible 9.0"""
    title = (action.get("title") or "").strip()
    tkey = action.get("table_key") or ""
    if not title:
        return []
    if any(title.startswith(e) for e in EMOJI.values() if e):
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
    props = action.get("properties") or {}
    has_loc = False
    for prop_name, prop_val in props.items():
        low = prop_name.lower()
        if low in ("локации", "локация", "locations", "location") or (
            is_relation("events", prop_name) and "локац" in low
        ):
            if str(prop_val).strip():
                has_loc = True
                break
    if not has_loc:
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
    loc_val = None
    for prop_name, prop_val in (action.get("properties") or {}).items():
        if prop_name.lower() in ("локации", "локация") or (
            is_relation("events", prop_name) and "локац" in prop_name.lower()
        ):
            loc_val = str(prop_val).strip()
            break
    if event_level == "root" and loc_val and loc_val.startswith("🟩"):
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
