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
    # События
    "Ik4p": "Участники",
    "K3b5": "Участники",
    "Vfxy": "Локация",
    "nNmi": "Локация",
    "4LZq": "ID",
    "B4zM": "Хронопорядок",
    "K714": "Эпоха/Слой",
    "lcVz": "Статус",
    "uHqz": "Узловой",
    # Персонажи
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
    # Локации
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

VOLUME_LIMITS = {
    "compact": 45000,
    "working": 110000,
    "full": 999999
}

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
    """Список глав/арок для чекбоксов."""
    try:
        data = api("/api/v1/ql/content-database/content", {
            "query": {
                "__filter": {"contentDatabaseId": table_id},
                "content": {"article": {"id": True, "title": True}, "hasNested": True}
            }
        })
        return [{"id": i["article"]["id"], "title": i["article"].get("title", "")} for i in data.get("content", [])]
    except Exception as e:
        print(f"[index] chapters error: {e}")
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

    # Если выбраны главы — резолвим их «Связанные события»
    if selected_chapter_ids:
        for chid in selected_chapter_ids:
            try:
                card = get_card_full(chid)
                props = card.get("properties") or {}
                for k, v in props.items():
                    label = PROPERTY_LABELS.get(k, k)
                    if "событ" in label.lower() or k in ("xe2X", "8iC3", "eXYm", "yB3V"):
                        resolved_ids = []
                        if isinstance(v, list):
                            for item in v:
                                if isinstance(item, dict) and "id" in item:
                                    resolved_ids.append(item["id"])
                        for eid in resolved_ids:
                            selected_event_ids.append(eid)
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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
