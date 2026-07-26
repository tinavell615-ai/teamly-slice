# provision_v7.py
# Провижининг пространств и таблиц по схеме v7
# Порядок: space → таблицы (только обычные колонки) → вторым проходом relations + rollups
# Идемпотентность: по имени таблицы (если уже есть — пропускаем)
# Слой 1: журнал сохраняется в Redis; коды уникальны внутри таблицы; main_article_id из ответа.

import uuid
import json
import time
from datetime import datetime
from typing import Any

from schema_v7 import SCHEMA, CREATION_ORDER, SOURCE_OPTIONS

# Пауза между запросами, чтобы не ловить 429
REQUEST_DELAY = 0.55

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
        self.main_article_id: str | None = None
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
            "main_article_id": self.main_article_id,
            "tables": self.table_ids,
            "properties": self.property_codes,
            "log": self.entries,
        }


def create_space(api_func, title: str, journal: ProvisionJournal) -> str | None:
    """
    POST /api/v1/space
    Всегда логирует полный сырой ответ.
    Извлекает space_id и main_article_id. Если main_article_id нет — стоп (закон 2).
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
        # Закон 2: полный сырой ответ всегда
        journal.log("create_space_raw", True, "полный ответ POST /api/v1/space", data=result)

        space_id = (
            result.get("id")
            or result.get("spaceId")
            or (result.get("space") or {}).get("id")
        )
        # Возможные поля main_article_id (не угадываем дальше — если нет, стоп)
        main_article_id = (
            result.get("mainArticleId")
            or result.get("main_article_id")
            or (result.get("mainArticle") or {}).get("id")
            or (result.get("main_article") or {}).get("id")
            or result.get("articleId")
            or (result.get("space") or {}).get("mainArticleId")
            or (result.get("data") or {}).get("mainArticleId")
            or (result.get("data") or {}).get("main_article_id")
        )

        if not space_id:
            journal.log("create_space", False, "нет space_id в ответе. Полный ответ в create_space_raw")
            return None
        if not main_article_id:
            journal.created_space_id = space_id
            journal.log(
                "create_space",
                False,
                "нет main_article_id в ответе — остановка. Полный сырой ответ залогирован. Не угадываем поле.",
            )
            return None

        journal.created_space_id = space_id
        journal.main_article_id = main_article_id
        journal.log(
            "create_space",
            True,
            f"title={title} space={space_id} main_article={main_article_id}",
            {"space_id": space_id, "main_article_id": main_article_id},
        )
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
    По разведке 26.07: containerId = space_id, parentId = внутренний id пространства (main_article_id).
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


def _unique_code(journal: ProvisionJournal, schema_key: str) -> str:
    """Генерирует код, уникальный внутри таблицы (защита от коллизии 4-hex)."""
    existing = set((journal.property_codes.get(schema_key) or {}).values())
    for _ in range(20):
        code = _gen_code()
        if code not in existing:
            return code
    return _gen_code()


def create_property(
    api_func,
    table_id: str,
    prop: dict,
    journal: ProvisionJournal,
    schema_key: str,
) -> str | None:
    """
    Создаёт обычную колонку (text / select / number) через schema_property_create.
    Возвращает propertyCode. Код уникален внутри таблицы.
    """
    prop_id = _gen_id()
    code = _unique_code(journal, schema_key)
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
    Создаёт колонку-связь (type=binding). Код уникален внутри таблицы.
    """
    prop_id = _gen_id()
    code = _unique_code(journal, schema_key)
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
    project_key: str,
    parent_id: str | None = None,
    existing_space_id: str | None = None,
    tables_filter: list[str] | None = None,
) -> dict:
    """
    Полный провижининг одного пространства по схеме v7.

    api_func — функция api(endpoint, payload) из app.
    project_key — ключ проекта (detective_v7), под которым сохраняется карта в Redis.
    parent_id — только если existing_space_id (для legacy); для нового берётся из journal.main_article_id.
    existing_space_id — если задан, не создаём новое пространство.
    tables_filter — если задан, создаём только эти ключи (иначе все).

    Возвращает journal.summary(). После успеха пишет в Redis:
      schema:tables:{project_key}, schema:codes:{project_key}, provision:journal:{space_id}
    """
    journal = ProvisionJournal()
    keys = tables_filter or CREATION_ORDER

    # 1. Пространство
    if existing_space_id:
        space_id = existing_space_id
        journal.created_space_id = space_id
        journal.log("create_space", True, f"используем существующее {space_id}")
        if not parent_id:
            journal.log("create_table", False, "для existing_space_id обязателен parent_id")
            return journal.summary()
    else:
        space_id = create_space(api_func, title, journal)
        if not space_id or not journal.main_article_id:
            return journal.summary()
        parent_id = journal.main_article_id

    if not parent_id:
        journal.log("create_table", False, "parent_id отсутствует — таблицы создать нельзя")
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

    # 4. Роллапы (пока только заготовка)
    journal.log("done", True, f"пространство «{title}» готово")

    # Сохранение журнала — обязательно до return (слой 1)
    try:
        from documents import redis_set
        summary = journal.summary()
        redis_set(f"provision:journal:{journal.created_space_id}", summary)
        redis_set(f"schema:tables:{project_key}", summary["tables"])
        redis_set(f"schema:codes:{project_key}", summary["properties"])
        journal.log("redis_save", True, f"project_key={project_key} tables={len(summary['tables'])} props_tables={len(summary['properties'])}")
    except Exception as e:
        journal.log("redis_save", False, str(e))

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


def list_spaces(api_func) -> dict:
    """
    POST /api/v1/wiki/ql/spaces
    Возвращает список пространств аккаунта.
    """
    payload = {
        "query": {
            "__filter": {
                "keeping_types": ["default"],
                "__text": {},
                "__nested": {
                    "keeping_types": ["default", "inlineContentDatabase"],
                    "__text": {"query": ""},
                },
            },
            "__sort": [{"user_pinned_at": "desc"}, {"created_at": "desc"}],
            "__pagination": {"page": 1, "per_page": 50},
            "id": True,
            "title": True,
            "description": True,
            "keeping_type": True,
            "pinned_at": True,
            "nested_count": {"article": True},
        }
    }
    try:
        result = api_func("/api/v1/wiki/ql/spaces", payload)
        return {"ok": True, "raw": result}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def resume_missing_relations(api_func) -> dict:
    """
    Устарело. Старое пространство удаляется. Для нового пространства
    используйте полный provision_space.
    """
    return {
        "ok": False,
        "error": "resume_missing_relations устарело: KNOWN_TABLES удалены. Используйте полный провижининг нового пространства.",
    }


def show_all_columns(api_func) -> dict:
    """
    Устарело. Коды теперь в schema:codes:{project_key}.
    """
    return {
        "ok": False,
        "error": "show_all_columns устарело: KNOWN_CODES/KNOWN_TABLES удалены. После провижининга колонки создаются видимыми по умолчанию или через view API с кодами из Redis.",
    }
