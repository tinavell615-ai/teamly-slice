# schema_live.py
# Живая карта кодов свойств проекта (из Upstash / schema:codes:*).
# Единственный владелец CODES. registry и app импортируют отсюда.
# Не дублировать карту нигде.

from __future__ import annotations

from registry import normalize as reg_normalize

# project_key → {tkey → {prop_name → {code, type, options}}}
CODES: dict[str, dict] = {}


class UnknownPropertyCode(Exception):
    pass


def load_codes_from_redis(project_key: str) -> bool:
    """Загружает schema:codes:{project_key} в CODES. Возвращает True если карта есть."""
    from documents import redis_get
    data = redis_get(f"schema:codes:{project_key}")
    if not data or not isinstance(data, dict):
        CODES.pop(project_key, None)
        return False
    CODES[project_key] = data
    return True


def set_codes(project_key: str, data: dict) -> None:
    """
    Явная подстановка карты в память (для run_samples.py и тестов).
    Единственная точка записи в CODES извне модуля.
    """
    if not isinstance(data, dict):
        raise TypeError("set_codes: data must be dict")
    CODES[project_key] = data


def ensure_codes(project_key: str) -> bool:
    """Гарантирует, что карта в памяти. True если есть."""
    if project_key in CODES and CODES[project_key]:
        return True
    return load_codes_from_redis(project_key)


def _find_meta(project_key: str, tkey: str, prop_name: str) -> dict | None:
    """
    Единый поиск свойства по нормализованному имени.
    Возвращает meta-dict или None. Все публичные функции зовут только его.
    """
    table = CODES.get(project_key, {}).get(tkey)
    if not table:
        return None
    for name, meta in table.items():
        if reg_normalize(name) == reg_normalize(prop_name):
            if isinstance(meta, dict):
                return meta
            return {"code": str(meta), "type": "text", "options": {}}
    return None


def prop_code(project_key: str, tkey: str, prop_name: str) -> str:
    meta = _find_meta(project_key, tkey, prop_name)
    if meta is None:
        if tkey not in (CODES.get(project_key) or {}):
            raise UnknownPropertyCode(
                f"нет карты кодов для таблицы «{tkey}» проекта «{project_key}»"
            )
        raise UnknownPropertyCode(f"{tkey}.{prop_name}: код неизвестен")
    code = meta.get("code")
    if not code:
        raise UnknownPropertyCode(f"{tkey}.{prop_name}: код пустой в карте")
    return code


def prop_meta(project_key: str, tkey: str, prop_name: str) -> dict:
    """Возвращает {code, type, options} или raises."""
    meta = _find_meta(project_key, tkey, prop_name)
    if meta is None:
        if tkey not in (CODES.get(project_key) or {}):
            raise UnknownPropertyCode(f"нет карты кодов для таблицы «{tkey}»")
        raise UnknownPropertyCode(f"{tkey}.{prop_name}: код неизвестен")
    return meta


def get_prop_type(project_key: str | None, tkey: str, prop_name: str) -> str | None:
    """
    Тип свойства из живой схемы или None, если схемы нет / свойства нет.
    Используется registry.is_relation для опознавания binding.
    """
    if not project_key:
        return None
    if not ensure_codes(project_key):
        return None
    meta = _find_meta(project_key, tkey, prop_name)
    if meta is None:
        return None
    return meta.get("type")


def resolve_select_value(project_key: str, tkey: str, prop_name: str, text_value: str) -> str:
    """
    Для select / multi-select возвращает option id.
    Если текст не найден — raises UnknownPropertyCode (не добавляем вариант).
    Тип нормализуется: multi_select и multi-select считаются одним.
    Несколько значений в multi-select — отказ: формат value не подтверждён разведкой.
    """
    meta = prop_meta(project_key, tkey, prop_name)
    raw_type = (meta.get("type") or "").replace("-", "_").lower()
    if raw_type not in ("select", "multi_select", "status"):
        return text_value  # plain text / number / binding handled elsewhere

    options = meta.get("options") or {}
    if not options:
        raise UnknownPropertyCode(
            f"{tkey}.{prop_name}: карта вариантов селекта пуста (схема прочитана неполно)"
        )

    key = text_value.strip()
    key_cf = key.casefold()

    # точное совпадение проверяется ДО разбиения на части
    if key in options:
        return options[key]

    # multi-select + несколько значений через разделитель → отказ (закон 2)
    if raw_type == "multi_select":
        parts = [p.strip() for p in str(text_value).replace(";", ",").split(",") if p.strip()]
        if len(parts) > 1:
            raise UnknownPropertyCode(
                f"{tkey}.{prop_name}: несколько значений в multi-select "
                f"(«{text_value}»). Формат value для множественного выбора "
                f"не подтверждён разведкой — поле пропущено. Одно значение работает."
            )

    # без учёта регистра: ровно одно совпадение — берём; несколько — отказ
    matches = [(otext, oid) for otext, oid in options.items() if otext.casefold() == key_cf]
    if len(matches) == 1:
        return matches[0][1]
    if len(matches) > 1:
        variants = [m[0] for m in matches]
        raise UnknownPropertyCode(
            f"{tkey}.{prop_name}: значение «{text_value}» неоднозначно по регистру. "
            f"Варианты: {variants}"
        )
    raise UnknownPropertyCode(
        f"{tkey}.{prop_name}: значение «{text_value}» отсутствует среди вариантов селекта. "
        f"Доступные: {list(options.keys())[:12]}"
    )


def get_codes(project_key: str) -> dict:
    """Прямой доступ к карте (для selfcheck, format helpers)."""
    ensure_codes(project_key)
    return CODES.get(project_key) or {}
