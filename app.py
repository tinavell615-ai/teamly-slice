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
RAILWAY_API_TOKEN = os.environ.get("RAILWAY_API_TOKEN", "")
TOKENS_FILE = os.environ.get("TOKENS_FILE", "/tmp/teamly_tokens.json")
SLUG = "tina-vell"
CLUSTER = "https://app.teamly.ru"
RAILWAY_PROJECT_ID = "d12688c9-9438-4622-ad96-fb9c916aa597"
RAILWAY_GQL = "https://backboard.railway.app/graphql/v2"

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

VOLUME_LIMITS = {
    "compact": 45000,
    "working": 110000,
    "full": 999999
}

# ===================== TOKEN SYSTEM (P0) =====================
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

def _load_from_file():
    try:
        if not os.path.exists(TOKENS_FILE):
            return False
        with open(TOKENS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not data.get("access_token") or not data.get("refresh_token"):
            return False
        _state["access_token"] = data["access_token"]
        _state["refresh_token"] = data["refresh_token"]
        _state["access_token_expires_at"] = int(data.get("access_token_expires_at", 0))
        _state["refresh_token_expires_at"] = int(data.get("refresh_token_expires_at", 0))
        _state["last_refresh_at"] = data.get("last_refresh_at")
        _state["last_refresh_ok"] = data.get("last_refresh_ok")
        _state["source"] = "file"
        print(f"[tokens] Загружено из {TOKENS_FILE}")
        return True
    except Exception as e:
        print(f"[tokens] Не удалось прочитать {TOKENS_FILE}: {e}")
        return False

def _save_to_file():
    try:
        payload = {
            "access_token": _state["access_token"],
            "refresh_token": _state["refresh_token"],
            "access_token_expires_at": _state["access_token_expires_at"],
            "refresh_token_expires_at": _state["refresh_token_expires_at"],
            "last_refresh_at": _state["last_refresh_at"],
            "last_refresh_ok": _state["last_refresh_ok"],
            "saved_at": _now(),
        }
        with open(TOKENS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"[tokens] Сохранено в {TOKENS_FILE}")
        return True
    except Exception as e:
        print(f"[tokens] НЕ удалось сохранить в {TOKENS_FILE}: {e}")
        return False


_railway_env_id = None

def _get_railway_env_id():
    global _railway_env_id
    if _railway_env_id:
        return _railway_env_id
    if not RAILWAY_API_TOKEN:
        return None
    try:
        q = """
        query($id: String!) {
          project(id: $id) {
            environments {
              edges { node { id name } }
            }
          }
        }
        """
        r = requests.post(
            RAILWAY_GQL,
            headers={"Authorization": f"Bearer {RAILWAY_API_TOKEN}", "Content-Type": "application/json"},
            json={"query": q, "variables": {"id": RAILWAY_PROJECT_ID}},
            timeout=15
        )
        if r.status_code != 200:
            print(f"[railway] Не удалось получить environments: {r.status_code} {r.text[:200]}")
            return None
        data = r.json()
        edges = data.get("data", {}).get("project", {}).get("environments", {}).get("edges", [])
        for edge in edges:
            node = edge.get("node", {})
            if node.get("name") == "production":
                _railway_env_id = node["id"]
                print(f"[railway] environmentId production = {_railway_env_id}")
                return _railway_env_id
        print("[railway] production environment не найден")
        return None
    except Exception as e:
        print(f"[railway] Ошибка получения env_id: {e}")
        return None

def _update_railway_refresh_token(new_refresh: str):
    """Обновляет Variable REFRESH_TOKEN в Railway, чтобы пережить рестарт."""
    if not RAILWAY_API_TOKEN or not new_refresh:
        return False
    env_id = _get_railway_env_id()
    if not env_id:
        print("[railway] Не могу обновить Variable — нет environmentId")
        return False
    try:
        mutation = """
        mutation($input: VariableUpsertInput!) {
          variableUpsert(input: $input)
        }
        """
        variables = {
            "input": {
                "projectId": RAILWAY_PROJECT_ID,
                "environmentId": env_id,
                "name": "REFRESH_TOKEN",
                "value": new_refresh
            }
        }
        r = requests.post(
            RAILWAY_GQL,
            headers={"Authorization": f"Bearer {RAILWAY_API_TOKEN}", "Content-Type": "application/json"},
            json={"query": mutation, "variables": variables},
            timeout=15
        )
        if r.status_code != 200:
            print(f"[railway] Ошибка upsert Variable: {r.status_code} {r.text[:300]}")
            return False
        data = r.json()
        if "errors" in data:
            print(f"[railway] GraphQL errors: {data['errors']}")
            return False
        print("[railway] REFRESH_TOKEN успешно обновлён в Railway Variables")
        return True
    except Exception as e:
        print(f"[railway] EXCEPTION при обновлении Variable: {e}")
        return False


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
            _update_railway_refresh_token(new_refresh)
        _state["access_token_expires_at"] = exp
        _state["refresh_token_expires_at"] = rexp
        _state["last_refresh_at"] = _now()
        _state["last_refresh_ok"] = True
        _state["last_error"] = None
        _state["source"] = "refresh"
        _save_to_file()

        print("=" * 60)
        print("[tokens] УСПЕШНЫЙ REFRESH")
        print(f"expires_at = {exp} ({datetime.fromtimestamp(exp)})")
        if new_refresh:
            print("НОВЫЙ refresh_token (при рестарте контейнера обнови Variable):")
            print(new_refresh)
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
            if not _load_from_file():
                if INITIAL_REFRESH_TOKEN:
                    _state["refresh_token"] = INITIAL_REFRESH_TOKEN
                    _state["source"] = "env"
                    print("[tokens] Стартуем с REFRESH_TOKEN из env")
                else:
                    raise Exception("Нет токенов: ни файла, ни REFRESH_TOKEN в env")
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
            "tokens_file": TOKENS_FILE,
            "file_exists": os.path.exists(TOKENS_FILE),
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
    print("[tokens] Проактивный refresh-поток запущен")

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
        timeout=60
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
            if depth_mode == "scenes" or (depth_mode == "chapters" and level < 1):
                walk(child_id, level + 1)
    if depth_mode != "arcs":
        walk(event_id, 0)
    return result

@app.route("/", methods=["GET"])
def index():
    # Получаем список событий для чекбоксов
    try:
        events = get_all_events(PROJECTS["burevestnik"]["tables"]["events"])
        # Показываем в основном корневые и средние
        by_id, children, roots = build_tree(events)
        # Берём корни + прямых детей корней для выбора
        selectable = []
        for rid in roots:
            selectable.append(by_id[rid])
            for cid in children.get(rid, [])[:8]:
                selectable.append(by_id[cid])
    except Exception as e:
        selectable = []
        error_msg = str(e)
    else:
        error_msg = None

    checkboxes = ""
    for ev in selectable:
        checkboxes += f'<label><input type="checkbox" name="events" value="{ev["id"]}"> {ev["title"]}</label>\n'

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
                {checkboxes if checkboxes else "<p>События не загрузились</p>"}
            </div>
            <p class="hint">Если ничего не выбрать — будут взяты все корневые события</p>
        </div>

        <div class="section">
            <strong>Глубина внутри выбранного</strong>
            <select name="depth">
                <option value="arcs">Только выбранные (без детей)</option>
                <option value="chapters" selected>Выбранные + прямые дети</option>
                <option value="scenes">Выбранные + вся глубина</option>
            </select>
        </div>

        <div class="section">
            <strong>Таблицы</strong>
            <label><input type="checkbox" name="tables" value="characters" checked> Персонажи</label>
            <label><input type="checkbox" name="tables" value="events" checked> События</label>
            <label><input type="checkbox" name="tables" value="locations" checked> Локации</label>
            <label><input type="checkbox" name="tables" value="chapters"> Главы / Части</label>
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
    depth = request.args.get("depth", "chapters")
    volume = request.args.get("volume", "working")
    selected_tables = request.args.getlist("tables") or ["characters", "events", "locations"]

    limit = VOLUME_LIMITS.get(volume, 110000)
    project = PROJECTS[project_key]
    events_table_id = project["tables"]["events"]

    # Загружаем все события и строим дерево
    all_events = get_all_events(events_table_id)
    by_id, children, roots = build_tree(all_events)

    # Определяем, какие события включать
    to_include = set()

    if not selected_event_ids:
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
                block = f"### {card['title']}\n{card['body']}\n\n"
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
                block = f"### {card['title']}\n{card['body']}\n\n"
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
