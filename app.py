from flask import Flask, Response
import requests
import json
import os
from datetime import datetime

app = Flask(__name__)

# Эти значения потом поставим в Variables на Railway
CLIENT_ID = os.environ.get("CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET", "")
REFRESH_TOKEN = os.environ.get("REFRESH_TOKEN", "")
SLUG = "tina-vell"
CLUSTER = "https://app.teamly.ru"

TABLES = {
    "characters": "d0f91b04-7924-4fd2-9450-58cf6c12a89f",
    "events": "bd5891eb-976b-4f7b-8bf0-5cb19d53c302",
    "locations": "6d9b436c-e213-49a2-8bec-2d109cef7280",
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
        }
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
        json=payload
    )
    if r.status_code != 200:
        raise Exception(f"API error {r.status_code}: {r.text[:200]}")
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
    return [{"id": i["article"]["id"], "title": i["article"]["title"]} for i in data.get("content", [])]

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
        "title": data.get("title", ""),
        "body": extract_text(data.get("editorContent"))
    }

@app.route("/")
def index():
    return """
    <html>
    <head><title>Teamly Slice</title></head>
    <body style="font-family: sans-serif; max-width: 600px; margin: 50px auto; text-align: center;">
        <h1>Срез базы Teamly</h1>
        <p>Нажми кнопку, чтобы собрать актуальный срез</p>
        <a href="/slice" style="display:inline-block; padding: 15px 30px; background:#4f46e5; color:white; text-decoration:none; border-radius:8px; font-size:18px;">
            Собрать срез
        </a>
    </body>
    </html>
    """

@app.route("/slice")
def slice():
    result = [f"# Срез базы Teamly\nСобран: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"]
    
    for name, tid in TABLES.items():
        rows = get_rows(tid)[:8]  # берём по 8 карточек
        result.append(f"\n## {name.upper()} ({len(rows)} карточек)\n")
        for row in rows:
            card = get_card(row["id"])
            result.append(f"### {card['title']}\n{card['body']}\n")
    
    text = "\n".join(result)
    return Response(
        text,
        mimetype="text/markdown",headers={"Content-Disposition": "attachment; filename=slice.md"}
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

