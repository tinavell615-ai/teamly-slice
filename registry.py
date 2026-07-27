# registry.py
# Единый источник правды по таблицам, алиасам, связям и нормализации имён.
# Всё выводится из schema_v7. SCHEMA. Никаких дублирующих словарей в app.py.
# is_relation / relation_target умеют смотреть живую схему (schema_live).

from __future__ import annotations
import re
from schema_v7 import SCHEMA, CREATION_ORDER

_EMOJI_RE = re.compile(
    r'^[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U000024C2-\U0001F251\s]*'
)


def normalize(s: str) -> str:
    """Единственная нормализация имён в проекте. Другой быть не должно.
    Срезает концевую пунктуацию (**: и т.п.), эмодзи-префикс, лишние пробелы.
    """
    if not s:
        return ""
    s = _EMOJI_RE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = s.rstrip("*:;,.!?)»\"']")
    return s.casefold()


# --- таблицы -------------------------------------------------------------
TABLE_KEYS: list[str] = list(CREATION_ORDER)
DISPLAY: dict[str, str] = {k: SCHEMA[k]["title"] for k in SCHEMA}
EMOJI: dict[str, str] = {k: SCHEMA[k].get("emoji", "") for k in SCHEMA}

# Маркеры уровня событий/локаций (Библия 5.6 Г). Допустимый префикс названия.
LEVEL_MARKERS = ("🟥", "🟧", "🟩")

# --- алиасы: русское написание → ключ ------------------------------------
EXTRA_ALIASES = {
    "главы / части": "chapters",
    "части": "chapters",
    "крючки": "hooks",
    "ружья": "hooks",
}
ALIASES: dict[str, str] = {}
for _k, _t in SCHEMA.items():
    ALIASES[normalize(_t["title"])] = _k
    ALIASES[normalize(_k)] = _k
for _a, _k in EXTRA_ALIASES.items():
    ALIASES[normalize(_a)] = _k


def table_key(raw: str) -> str | None:
    return ALIASES.get(normalize(raw))


# --- свойства ------------------------------------------------------------
PROP_NAMES: dict[str, list[str]] = {
    k: [p["name"] for p in SCHEMA[k].get("properties", [])] for k in SCHEMA
}

# --- связи: (таблица, имя поля) → целевая таблица (из schema_v7) ----------
RELATION_TARGET: dict[tuple[str, str], str] = {}
for _k, _t in SCHEMA.items():
    for _rel in _t.get("relations", []):
        RELATION_TARGET[(_k, normalize(_rel["name"]))] = _rel["target"]


# Слова-уточнители для вывода цели из имени поля (задача 2).
# Единственный допустимый литеральный список в пакете. Морфология, не имена таблиц.
# Разделено: слова родителя и слова уточнения — используются только эти константы.
_PARENT_WORDS = (
    "родительская", "родительское", "родительский", "родительские",
)
_QUALIFIER_WORDS = (
    "связанные", "связанный", "связанная",
    "ключевые", "ключевой", "ключевая",
)


def relation_target(tkey: str, prop_name: str, project_key: str | None = None) -> str | None:
    """
    Целевая таблица связи.
    Порядок (до первого результата):
    1. schema_v7.RELATION_TARGET
    2. Вывод из имени поля (родительская / связанные / ключевые + остаток в ALIASES)
    3. None = отказ, не догадка.
    project_key зарезервирован (живая схема цель не отдаёт).
    """
    norm = normalize(prop_name)
    # 1. жёсткая карта v7
    hit = RELATION_TARGET.get((tkey, norm))
    if hit is not None:
        return hit

    # 2. вывод из имени
    words = norm.split()
    if not words:
        return None

    first = words[0]
    rest_words = words[1:]

    if first in _PARENT_WORDS:
        # слово родителя + любой (в т.ч. неопознанный) остаток → та же таблица
        if not rest_words:
            return tkey
        rest = " ".join(rest_words)
        target = ALIASES.get(rest) or ALIASES.get(normalize(rest))
        if target:
            return target
        return tkey

    if first in _QUALIFIER_WORDS:
        rest = " ".join(rest_words)
        if not rest:
            return None
        target = ALIASES.get(rest) or ALIASES.get(normalize(rest))
        return target

    # 3. остаток целиком
    target = ALIASES.get(norm)
    if target:
        return target
    return None


def is_relation(tkey: str, prop_name: str, project_key: str | None = None) -> bool:
    """
    Является ли поле связью.
    1. Если есть живая схема проекта — смотрим type == "binding". Точка.
    2. Иначе строгий откат на schema_v7.RELATION_TARGET (без name-derivation).
    Вызов без project_key работает по-старому.
    """
    if project_key:
        # late import: schema_live импортирует normalize из registry
        from schema_live import get_prop_type
        live_type = get_prop_type(project_key, tkey, prop_name)
        if live_type is not None:
            return live_type == "binding"
    # строгий откат: только карта v7, без вывода из имени
    return RELATION_TARGET.get((tkey, normalize(prop_name))) is not None


# --- видимые колонки связей (данные, samples/binding_visible.json) -----
import json
from pathlib import Path as _Path

_BINDING_VISIBLE_CACHE: dict | None = None

def _load_binding_visible() -> dict:
    global _BINDING_VISIBLE_CACHE
    if _BINDING_VISIBLE_CACHE is not None:
        return _BINDING_VISIBLE_CACHE
    path = _Path(__file__).resolve().parent / "samples" / "binding_visible.json"
    if not path.exists():
        _BINDING_VISIBLE_CACHE = {}
        return _BINDING_VISIBLE_CACHE
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    _BINDING_VISIBLE_CACHE = data
    return data


def choose_visible_binding(
    project_key: str,
    source_tkey: str,
    target_tkey: str,
) -> dict:
    """
    Выбирает видимое поле связи для записи source → target.
    Возвращает:
      {"ok": True, "prop_name": "..."}
      {"ok": False, "error": "...", "write_from": "tkey"|None}
    Алгоритм: среди видимых полей source, чья цель == target_tkey.
    0 → проверяем только target_tkey: есть ли у неё видимое поле с целью source.
         Есть — write_from = target_tkey. Нет — отказ без подсказки.
    1 → ok.
    >1 → отказ с перечислением.
    """
    data = _load_binding_visible()
    proj = data.get(project_key) or {}
    visible = proj.get(source_tkey) or []
    matches = []
    for raw_name in visible:
        tgt = relation_target(source_tkey, raw_name, project_key)
        if tgt == target_tkey:
            matches.append(raw_name)
    if len(matches) == 1:
        return {"ok": True, "prop_name": matches[0]}
    if len(matches) > 1:
        return {
            "ok": False,
            "error": (
                f"В таблице «{source_tkey}» несколько видимых полей связи "
                f"с целью «{target_tkey}»: {matches}. Уточните колонку."
            ),
            "write_from": None,
        }
    # ни одного у source — проверяем только целевую таблицу (без догадок)
    target_visible = proj.get(target_tkey) or []
    for raw_name in target_visible:
        if relation_target(target_tkey, raw_name, project_key) == source_tkey:
            return {
                "ok": False,
                "error": (
                    f"В таблице «{source_tkey}» нет видимой колонки связи "
                    f"с целью «{target_tkey}». Пишите со стороны таблицы «{target_tkey}»."
                ),
                "write_from": target_tkey,
            }
    return {
        "ok": False,
        "error": (
            f"В таблице «{source_tkey}» нет видимой колонки связи с целью «{target_tkey}», "
            f"и у целевой таблицы тоже нет обратного видимого поля."
        ),
        "write_from": None,
    }
