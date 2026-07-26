# provision_v7.py
# Провижининг пространств и таблиц по схеме v7
# Порядок: space → таблицы (только обычные колонки) → вторым проходом relations + rollups
# Идемпотентность: по имени таблицы (если уже есть — пропускаем)

import uuid
import json
import time
from datetime import datetime
from typing import Any

from schema_v7 import SCHEMA, CREATION_ORDER, SOURCE_OPTIONS

# Пауза между запросами, чтобы не ловить 429
REQUEST_DELAY = 0.55

# Эти функции должны быть переданы из app (api, get_token и т.д.)
# или импортированы, если модуль в том же процессе.

CLUSTER = "https://app.teamly.ru"
SLUG = "tina-vell"


def _now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _gen_code() -> str:
    """Короткий код свойства в стиле Teamly (4 символа)."""
    return uuid.uuid4().hex[:4]


def _gen_id() -> str:
    return str(uuid.uuid4())


class ProvisionJournal:
    def __init__(self):
        self.entries: list[dict] = []
        self.created_space_id: str | None = None
        self.table_ids: dict[str, str] = {}          # schema_key → contentDatabaseId
        self.property_codes: dict[str, dict[str, str]] = {}  # schema_key → {prop_name → code}

    def log(self, action: str, ok: bool, detail: str = "", data: Any = None):
        self.entries.append({
            "ts": _now(),
            "action": action,
            "ok": ok,
            "detail": detail,
            "data": data,
        })
        status = "OK" if ok else "FAIL"
        print(f"[provision] {status} | {action} | {detail}")

    def summary(self) -> dict:
        return {
            "space_id": self.created_space_id,
            "tables": self.table_ids,
            "properties": self.property_codes,
            "log": self.entries,
        }


def create_space(api_func, title: str, journal: ProvisionJournal) -> str | None:
    """
    POST /api/v1/space
    Возвращает spaceId созданного пространства.
    """
    payload = {
        "title": title,
        "description": None,
        "is_pinned": False,
        "settings": {
            "property": True,
            "glossary": True,
            "fastLinks": True,
            "disk": True,
            "autoPublication": False,
            "editorsAllowedToEditGlossary": True,
            "editorsAllowedToEditTemplates": True,
            "canOnlyAdminDeleteSources": True,
            "allowExportToReaders": True,
        },
        "templateCode": None,
    }
    try:
        result = api_func("/api/v1/space", payload)
        # Ожидаем, что в ответе есть id / spaceId
        space_id = (
            result.get("id")
            or result.get("spaceId")
            or result.get("space", {}).get("id")
        )
        if not space_id:
            # иногда id лежит глубже
            journal.log("create_space", False, f"нет id в ответе: {str(result)[:200]}")
            return None
        journal.created_space_id = space_id
        journal.log("create_space", True, f"title={title}", {"space_id": space_id})
        return space_id
    except Exception as e:
        journal.log("create_space", False, str(e))
        return None


def create_table(
    api_func,
    space_id: str,
    parent_id: str,
    title: str,
    journal: ProvisionJournal,
    schema_key: str,
) -> str | None:
    """
    POST /api/v1/content-database
    По разведке 26.07: containerId = space_id, parentId = внутренний id пространства.
    """
    payload = {
        "title": title,
        "parentId": parent_id,
        "containerId": space_id,
        "displayView": {
            "name": "Таблица",
            "type": "table",
            "settings": {
                "__displayProperties": {
                    "fields": ["title", "executor", "executionDate"]
                }
            },
        },
    }
    try:
        result = api_func("/api/v1/content-database", payload)
        table_id = (
            result.get("id")
            or result.get("contentDatabaseId")
            or result.get("spaceId")
        )
        if not table_id:
            journal.log("create_table", False, f"{schema_key}: нет id в ответе {str(result)[:200]}")
            return None
        journal.table_ids[schema_key] = table_id
        journal.property_codes[schema_key] = {}
        journal.log("create_table", True, f"{schema_key} → {table_id}", {"title": title})
        time.sleep(REQUEST_DELAY)
        return table_id
    except Exception as e:
        journal.log("create_table", False, f"{schema_key}: {e}")
        time.sleep(REQUEST_DELAY)
        return None


def create_property(
    api_func,
    table_id: str,
    prop: dict,
    journal: ProvisionJournal,
    schema_key: str,
) -> str | None:
    """
    Создаёт обычную колонку (text / select / number) через schema_property_create.
    Возвращает propertyCode.
    """
    prop_id = _gen_id()
    code = _gen_code()
    entity = {
        "spaceId": table_id,
        "propertyId": prop_id,
        "type": prop["type"],
        "name": prop["name"],
        "code": code,
        "format": prop.get("format", prop["type"]),
        "options": {},
        "protected": False,
        "hiddenType": "never",
        "sort": None,
    }
    # Для select можно сразу передать options, но по разведке UI оставляет пустым.
    # Пока оставляем {}.

    payload = {
        "code": "group",
        "payload": {
            "commands": [
                {
                    "code": "schema_property_create",
                    "payload": {"entity": entity},
                    "internal": False,
                }
            ]
        },
    }
    try:
        result = api_func("/api/v1/wiki/properties/command/execute", payload)
        journal.property_codes.setdefault(schema_key, {})[prop["name"]] = code
        journal.log(
            "create_property",
            True,
            f"{schema_key}.{prop['name']} code={code} type={prop['type']}",
        )
        time.sleep(REQUEST_DELAY)
        return code
    except Exception as e:
        journal.log("create_property", False, f"{schema_key}.{prop['name']}: {e}")
        time.sleep(REQUEST_DELAY)
        return None


def create_relation(
    api_func,
    table_id: str,
    rel: dict,
    target_table_id: str,
    journal: ProvisionJournal,
    schema_key: str,
) -> str | None:
    """
    Создаёт колонку-связь (type=binding).
    """
    prop_id = _gen_id()
    code = _gen_code()
    entity = {
        "spaceId": table_id,
        "propertyId": prop_id,
        "type": "binding",
        "name": rel["name"],
        "code": code,
        "format": "binding",
        "options": {
            "bind_entity": {
                "id": target_table_id,
                "type": "database",
            },
            "name": rel["name"],
            "propertyCode": code,
        },
        "protected": False,
        "hiddenType": "never",
        "sort": None,
    }
    payload = {
        "code": "group",
        "payload": {
            "commands": [
                {
                    "code": "schema_property_create",
                    "payload": {"entity": entity},
                    "internal": False,
                }
            ]
        },
    }
    try:
        result = api_func("/api/v1/wiki/properties/command/execute", payload)
        journal.property_codes.setdefault(schema_key, {})[rel["name"]] = code
        journal.log(
            "create_relation",
            True,
            f"{schema_key}.{rel['name']} → {rel['target']} code={code}",
        )
        time.sleep(REQUEST_DELAY)
        return code
    except Exception as e:
        journal.log("create_relation", False, f"{schema_key}.{rel['name']}: {e}")
        time.sleep(REQUEST_DELAY)
        return None


def configure_rollup(
    api_func,
    table_id: str,
    property_code: str,
    binding_code: str,
    target_property_code: str,
    formula_code: str,
    journal: ProvisionJournal,
    schema_key: str,
    name: str,
):
    """
    Настраивает уже созданную колонку как роллап.
    """
    # propertyId нам неизвестен после create, но Teamly принимает propertyCode
    payload = {
        "code": "schema_property_update",
        "payload": {
            "entity": {
                "spaceId": table_id,
                "propertyCode": property_code,
            },
            "options": [
                {"code": "bindingCode", "method": "update", "value": binding_code},
                {"code": "propertyCode", "method": "update", "value": target_property_code},
                {"code": "formulaCode", "method": "update", "value": formula_code},
            ],
        },
        "internal": False,
    }
    try:
        api_func("/api/v1/wiki/properties/command/execute", payload)
        journal.log(
            "configure_rollup",
            True,
            f"{schema_key}.{name} formula={formula_code}",
        )
    except Exception as e:
        journal.log("configure_rollup", False, f"{schema_key}.{name}: {e}")


def provision_space(
    api_func,
    title: str,
    parent_id: str | None = None,
    existing_space_id: str | None = None,
    tables_filter: list[str] | None = None,
) -> dict:
    """
    Полный провижининг одного пространства по схеме v7.

    api_func — функция api(endpoint, payload) из app.
    parent_id — внутренний parentId пространства (из разведки браузера).
    existing_space_id — если задан, не создаём новое пространство, а используем это.
    tables_filter — если задан, создаём только эти ключи (иначе все).

    Возвращает journal.summary().
    """
    journal = ProvisionJournal()
    keys = tables_filter or CREATION_ORDER

    # 1. Пространство
    if existing_space_id:
        space_id = existing_space_id
        journal.created_space_id = space_id
        journal.log("create_space", True, f"используем существующее {space_id}")
    else:
        space_id = create_space(api_func, title, journal)
        if not space_id:
            return journal.summary()

    if not parent_id:
        journal.log("create_table", False, "parent_id не передан — таблицы создать нельзя")
        return journal.summary()

    # 2. Таблицы + обычные колонки
    for key in keys:
        if key not in SCHEMA:
            journal.log("skip", False, f"неизвестная таблица {key}")
            continue
        tbl = SCHEMA[key]
        table_title = f"{tbl['emoji']} {tbl['title']}"
        table_id = create_table(api_func, space_id, parent_id, table_title, journal, key)
        if not table_id:
            continue

        for prop in tbl.get("properties", []):
            # title уже есть как системное, пропускаем если name == "Название"
            if prop["name"] == "Название":
                continue
            create_property(api_func, table_id, prop, journal, key)

    # 3. Второй проход — relations
    for key in keys:
        if key not in journal.table_ids:
            continue
        tbl = SCHEMA[key]
        table_id = journal.table_ids[key]
        for rel in tbl.get("relations", []):
            target_key = rel["target"]
            target_id = journal.table_ids.get(target_key)
            if not target_id:
                journal.log(
                    "create_relation",
                    False,
                    f"{key}.{rel['name']}: целевая таблица {target_key} ещё не создана",
                )
                continue
            create_relation(api_func, table_id, rel, target_id, journal, key)

    # 4. Роллапы (пока только заготовка — нужны реальные propertyId/codes)
    # Для Крючков «Дистанция» оставляем на ручную настройку или следующий шаг.

    journal.log("done", True, f"пространство «{title}» готово")
    return journal.summary()


# ---------------------------------------------------------------------------
# Известные ID из успешного провижининга 26.07.2026
# ---------------------------------------------------------------------------

KNOWN_TABLES = {
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

# Связи, которые упали с 429 при первом прогоне
MISSING_RELATIONS = [
    ("locations", "Контролирующая организация"),
    ("characters", "Связанные персонажи"),
    ("characters", "Организации"),
    ("characters", "Артефакты"),
    ("characters", "Ключевые события"),
    ("characters", "Ключевые локации"),
    ("characters", "Линии"),
    ("organizations", "Родительская организация"),
    ("organizations", "Руководство"),
    ("organizations", "Члены"),
    ("organizations", "Базовые локации"),
    ("organizations", "Противники"),
    ("organizations", "Артефакты"),
]


def resume_missing_relations(api_func) -> dict:
    """
    Досоздаёт только те связи, которые упали с 429.
    """
    journal = ProvisionJournal()
    journal.table_ids = dict(KNOWN_TABLES)
    journal.created_space_id = "846990cf-487f-4650-9cf1-f396492d2e17"

    for schema_key, rel_name in MISSING_RELATIONS:
        tbl = SCHEMA.get(schema_key)
        if not tbl:
            continue
        table_id = KNOWN_TABLES.get(schema_key)
        if not table_id:
            continue

        rel = next((r for r in tbl.get("relations", []) if r["name"] == rel_name), None)
        if not rel:
            journal.log("create_relation", False, f"{schema_key}.{rel_name}: нет в схеме")
            continue

        target_id = KNOWN_TABLES.get(rel["target"])
        if not target_id:
            journal.log("create_relation", False, f"{schema_key}.{rel_name}: нет target {rel['target']}")
            continue

        create_relation(api_func, table_id, rel, target_id, journal, schema_key)

    journal.log("done", True, "недостающие связи досозданы")
    return journal.summary()


def get_view_id(api_func, table_id: str, journal=None) -> str | None:
    """
    POST /api/v1/wiki/views
    Возвращает id первого (основного) представления таблицы.
    """
    try:
        result = api_func("/api/v1/wiki/views", {
            "sourceId": table_id,
            "sourceType": "space",
        })
        raw = str(result)[:600]
        if journal is not None:
            journal.log("views_raw", True, f"{table_id}: {raw}")

        if isinstance(result, list) and result:
            return result[0].get("id") or result[0].get("viewId")
        if isinstance(result, dict):
            items = (
                result.get("items")
                or result.get("data")
                or result.get("views")
                or result.get("list")
                or []
            )
            if items:
                return items[0].get("id") or items[0].get("viewId")
            return result.get("id") or result.get("viewId")
        return None
    except Exception as e:
        if journal is not None:
            journal.log("get_view_id", False, f"{table_id}: {e}")
        return None


def show_columns(
    api_func,
    table_id: str,
    view_id: str,
    codes: list[str],
    journal: ProvisionJournal,
    schema_key: str,
) -> bool:
    """
    Делает перечисленные property codes видимыми в представлении.
    """
    # title всегда первый
    fields = ["title"] + [c for c in codes if c and c != "title"]
    payload = {
        "code": "group",
        "payload": {
            "commands": [
                {
                    "code": "display_view_update",
                    "payload": {
                        "entity": {
                            "spaceId": table_id,
                            "viewId": view_id,
                        },
                        "settingsOperations": [
                            {
                                "path": "__displayProperties",
                                "method": "update",
                                "code": "fields",
                                "value": fields,
                            },
                            {
                                "path": "__layout",
                                "method": "update",
                                "code": "propertySort",
                                "value": fields + ["author"],
                            },
                        ],
                    },
                    "internal": False,
                }
            ]
        },
    }
    try:
        api_func("/api/v1/wiki/properties/command/execute", payload)
        journal.log("show_columns", True, f"{schema_key}: {len(fields)} колонок")
        time.sleep(REQUEST_DELAY)
        return True
    except Exception as e:
        journal.log("show_columns", False, f"{schema_key}: {e}")
        time.sleep(REQUEST_DELAY)
        return False


# Коды свойств из успешных прогонов (обычные + связи)
KNOWN_CODES: dict[str, list[str]] = {
    "world": ["a6df", "9f57", "c25d"],
    "locations": ["931b", "ce47", "aa15", "eabc", "310a", "6907"],
    "characters": ["e0a5", "f56d", "b36f", "d65e", "232e", "0ec3", "a725", "13db", "652d", "aa6c", "19cd"],
    "organizations": ["3ae1", "c0bc", "b039", "f469", "46fe", "86d3", "797d", "6f21", "179e", "b03b", "3926"],
    "artifacts": ["c98c", "02da", "2f5c", "cccf", "b390", "63cb", "aa7f", "9e2a", "d0a0", "6e4d"],
    "lines": ["c8e9", "6791", "9cc6", "15f3", "d8dc", "5714"],
    "events": ["0fd0", "572b", "c376", "de4c", "a8e4", "8e07", "61ce", "95be", "98bc", "d432", "661c", "f8c1"],
    "chapters": ["b37e", "114c", "618a", "648e", "a688", "11ad", "ebd2", "d828", "a10b", "924b", "ac67"],
    "hooks": ["d879", "c318", "5278", "3ee7", "19b5", "46f7", "d5a6", "cf8d", "7369", "2a19", "a246"],
    "secrets": ["ffea", "fdef", "2e03", "d266", "6fb7", "a0d4", "63ea", "7c9c", "d424", "7cfc"],
    "references": ["6117", "8cbd", "ab73", "fc11", "7d94", "542a", "bb62", "0fcc"],
    "archive": ["6a40", "253a", "b0d5"],
}


def show_all_columns(api_func) -> dict:
    """
    Для каждой таблицы: получает viewId и делает все известные колонки видимыми.
    """
    journal = ProvisionJournal()
    journal.table_ids = dict(KNOWN_TABLES)
    journal.created_space_id = "846990cf-487f-4650-9cf1-f396492d2e17"

    for key, table_id in KNOWN_TABLES.items():
        codes = KNOWN_CODES.get(key, [])
        if not codes:
            journal.log("show_columns", False, f"{key}: нет известных кодов")
            continue

        view_id = get_view_id(api_func, table_id, journal)
        if not view_id:
            journal.log("show_columns", False, f"{key}: не удалось получить viewId")
            continue

        journal.log("get_view_id", True, f"{key} → {view_id}")
        show_columns(api_func, table_id, view_id, codes, journal, key)

    journal.log("done", True, "колонки сделаны видимыми")
    return journal.summary()
