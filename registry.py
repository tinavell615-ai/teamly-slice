# registry.py
# Единый источник правды по таблицам, алиасам, связям и нормализации имён.
# Всё выводится из schema_v7. SCHEMA. Никаких дублирующих словарей в app.py.

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

# --- связи: (таблица, имя поля) → целевая таблица ------------------------
RELATION_TARGET: dict[tuple[str, str], str] = {}
for _k, _t in SCHEMA.items():
    for _rel in _t.get("relations", []):
        RELATION_TARGET[(_k, normalize(_rel["name"]))] = _rel["target"]


def relation_target(tkey: str, prop_name: str) -> str | None:
    """None = это не поле связи, а обычное свойство."""
    return RELATION_TARGET.get((tkey, normalize(prop_name)))


def is_relation(tkey: str, prop_name: str) -> bool:
    return relation_target(tkey, prop_name) is not None
