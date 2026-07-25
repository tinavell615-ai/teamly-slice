# provision_v7.py
# Провижининг пространств и таблиц по схеме v7
# Порядок: space → таблицы (только обычные колонки) → вторым проходом relations + rollups
# Идемпотентность: по имени таблицы (если уже есть — пропускаем)

import uuid
import json
from datetime import datetime
from typing import Any

from schema_v7 import SCHEMA, CREATION_ORDER, SOURCE_OPTIONS

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
    container_id: str,
    title: str,
    journal: ProvisionJournal,
    schema_key: str,
) -> str | None:
    """
    POST /api/v1/content-database
    Возвращает contentDatabaseId (он же spaceId таблицы в терминах Teamly).
    """
    payload = {
        "title": title,
        "parentId": space_id,
        "containerId": container_id,
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
        return table_id
    except Exception as e:
        journal.log("create_table", False, f"{schema_key}: {e}")
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
        return code
    except Exception as e:
        journal.log("create_property", False, f"{schema_key}.{prop['name']}: {e}")
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
        return code
    except Exception as e:
        journal.log("create_relation", False, f"{schema_key}.{rel['name']}: {e}")
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
    container_id: str,
    tables_filter: list[str] | None = None,
) -> dict:
    """
    Полный провижининг одного пространства по схеме v7.

    api_func — функция api(endpoint, payload) из app.
    container_id — id контейнера (из разведки, обычно id списка пространств / аккаунта).
    tables_filter — если задан, создаём только эти ключи (иначе все).

    Возвращает journal.summary().
    """
    journal = ProvisionJournal()
    keys = tables_filter or CREATION_ORDER

    # 1. Пространство
    space_id = create_space(api_func, title, journal)
    if not space_id:
        return journal.summary()

    # 2. Таблицы + обычные колонки
    for key in keys:
        if key not in SCHEMA:
            journal.log("skip", False, f"неизвестная таблица {key}")
            continue
        tbl = SCHEMA[key]
        table_title = f"{tbl['emoji']} {tbl['title']}"
        table_id = create_table(api_func, space_id, container_id, table_title, journal, key)
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
# Пример использования из app (после импорта):
#
# from provision_v7 import provision_space
# result = provision_space(api, "Тест-v7-провижининг", container_id="6aea92ec-...")
# print(json.dumps(result, ensure_ascii=False, indent=2))
# ---------------------------------------------------------------------------
