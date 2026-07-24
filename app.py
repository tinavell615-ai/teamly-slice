from flask import Flask, request, Response
import requests
import json
import os
from datetime import datetime

app = Flask(__name__)

CLIENT_ID = os.environ.get("CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET", "")
REFRESH_TOKEN = os.environ.get("REFRESH_TOKEN", "")
SLUG = "tina-vell"
CLUSTER = "https://app.teamly.ru"

# Пока один проект. Позже сделаем список.
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

access_token = None
access_token_expires = 0

def get_token():
    global access_token, access_token_expires
    now = int(datetime.now().timestamp())
    if access_token and now < access_token_expires - 60:
        return access_token

    r = requests.post(
        f"https://{SLUG}.teamly.ru/api/v1/auth/integration/refresh",
        json={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "refresh_token": REFRESH_TOKEN
        },
        timeout=30
    )
    if r.status_code != 200:
        raise Exception(f"Token error: {r.text}")
    data = r.json()
    access_token = data["access_token"]
    access_token_expires = data.get("access_token_expires_at", now + 3600)
    return access_token

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
        raise Exception(f"API {r.status_code}: {r.text[:300]}")
    return r.json()

def get_rows(table_id):
    data = api("/api/v1/ql/content-database/content", {
        "query": {
            "__filter": {"contentDatabaseId": table_id},
            "content": {
                "article": {"id": True, "title": True},
                "hasNested": True
            }
        }
    })
    return [{"id": i["article"]["id"], "title": i["article"].get("title", "")} 
            for i in data.get("content", [])]

def extract_text(editor):
    if not editor:
        return ""
    try:
        doc = json.loads(editor)
    except:
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

def get_card(cid):
    data = api("/api/v1/wiki/ql/article", {
        "query": {
            "__filter": {"id": cid},
            "id": True,
            "title": True,
            "editorContent": True
        }
    })
    return {
        "id": data.get("id"),
        "title": data.get("title", ""),
        "body": extract_text(data.get("editorContent"))
    }

@app.route("/", methods=["GET"])
def index():
    return """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Срез Teamly</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 640px; margin: 40px auto; padding: 0 20px; color: #1a1a1a; }
        h1 { font-size: 1.6rem; margin-bottom: 8px; }
        .desc { color: #555; margin-bottom: 28px; }
        label { display: block; margin: 14px 0 6px; font-weight: 600; }
        select, input[type=number] { width: 100%; padding: 10px; font-size: 1rem; border: 1px solid #ccc; border-radius: 8px; }
        .checkboxes label { font-weight: 400; margin: 6px 0; }
        .checkboxes input { margin-right: 8px; }
        button { margin-top: 28px; width: 100%; padding: 14px; font-size: 1.1rem; background: #4f46e5; color: white; border: none; border-radius: 10px; cursor: pointer; }
        button:hover { background: #4338ca; }
        .hint { font-size: 0.85rem; color: #777; margin-top: 4px; }
    </style>
</head>
<body>
    <h1>Срез базы Teamly</h1>
    <p class="desc">Собери курируемый срез для работы в чате</p>

    <form action="/slice" method="get">
        <label>Проект</label>
        <select name="project">
            <option value="burevestnik">Буревестник</option>
        </select>

        <label>Таблицы</label>
        <div class="checkboxes">
            <label><input type="checkbox" name="tables" value="characters" checked> Персонажи</label>
            <label><input type="checkbox" name="tables" value="events" checked> События</label>
            <label><input type="checkbox" name="tables" value="locations" checked> Локации</label>
            <label><input type="checkbox" name="tables" value="chapters"> Главы / Части</label>
            <label><input type="checkbox" name="tables" value="world"> Мир</label>
        </div>

        <label>Уровень дробления событий</label>
        <select name="depth">
            <option value="arcs">Только арки (крупные блоки)</option>
            <option value="chapters" selected>Арки + главы</option>
            <option value="scenes">Арки + главы + сцены (максимум)</option>
        </select>
        <div class="hint">Пока влияет на количество и порядок. Полная иерархия будет в следующей версии.</div>

        <label>Объём среза</label>
        <select name="volume">
            <option value="compact">Компактный (~45 тыс.)</option>
            <option value="working" selected>Рабочий (~110 тыс.)</option>
            <option value="full">Полный (без лимита)</option>
        </select>

        <button type="submit">Собрать срез</button>
    </form>
</body>
</html>
"""

@app.route("/slice")
def slice():
    project_key = request.args.get("project", "burevestnik")
    selected_tables = request.args.getlist("tables")
    depth = request.args.get("depth", "chapters")
    volume = request.args.get("volume", "working")

    if not selected_tables:
        selected_tables = ["characters", "events", "locations"]

    limit = VOLUME_LIMITS.get(volume, 110000)
    project = PROJECTS.get(project_key)
    if not project:
        return "Проект не найден", 404

    result = []
    result.append(f"# Срез: {project['name']}")
    result.append(f"Собран: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    result.append(f"Режим: {volume} | Глубина: {depth}")
    result.append("")

    current_len = 0
    cards_added = 0

    # Порядок приоритета
    priority = ["characters", "events", "locations", "chapters", "world"]

    for table_key in priority:
        if table_key not in selected_tables:
            continue
        table_id = project["tables"].get(table_key)
        if not table_id:
            continue

        rows = get_rows(table_id)

        # Простое ограничение по глубине (пока количественное)
        if table_key == "events":
            if depth == "arcs":
                rows = rows[:6]
            elif depth == "chapters":
                rows = rows[:14]
            else:
                rows = rows[:30]
        else:
            rows = rows[:20]

        section = [f"\n## {table_key.upper()} ({len(rows)} карточек)\n"]
        section_len = sum(len(s) for s in section)

        for row in rows:
            if current_len + section_len > limit:
                break
            try:
                card = get_card(row["id"])
                block = f"### {card['title']}\n{card['body']}\n\n"
                if current_len + len(block) > limit:
                    result.append("\n--- Обрезано по лимиту объёма ---\n")
                    break
                section.append(block)
                current_len += len(block)
                cards_added += 1
            except Exception as e:
                section.append(f"### {row['title']}\n[ошибка загрузки: {e}]\n\n")

        result.extend(section)
        if current_len >= limit:
            break

    text = "\n".join(result)
    filename = f"slice_{project_key}_{datetime.now().strftime('%Y%m%d_%H%M')}.md"

    return Response(
        text,
        mimetype="text/markdown",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
