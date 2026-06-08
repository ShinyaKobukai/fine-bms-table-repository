import json
import sqlite3
import urllib.request
from pathlib import Path

from sheet_sync import lookup_songdata

DB = "stella_songs.db"

def load_env(path="fine.env"):
    env = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env

def normalize_tag(tag):
    tag = tag.strip()
    if tag.startswith("惜敗["):
        return "惜敗"
    if tag == "しょうもないラス殺し":
        return "ラス殺し"
    if tag == "横認識(縦系)":
        return "横認識縦系"
    if tag == "横認識(8分系)":
        return "横認識8分系"
    return tag

def split_tags(tags):
    return [t.strip() for t in (tags or "").split("|") if t.strip()]

def post_json(url, payload):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as res:
        return res.read().decode("utf-8")

env = load_env()
url = env["SHEET_SYNC_URL"]

con = sqlite3.connect(DB)
cur = con.cursor()
cur.execute("""
SELECT level, title, chart_name, url, tags, memo
FROM songs
WHERE tags IS NOT NULL
AND tags != ''
ORDER BY level, title
""")

sent = 0
skipped = 0

for level, title, chart_name, song_url, tags, memo in cur.fetchall():
    found = lookup_songdata(title, chart_name or "")

    if not found:
        display_title = title if not chart_name else f"{title} [{chart_name}]"
        print("[SKIP md5 not found]", display_title)
        skipped += 1
        continue

    table_title = found["title"]
    if found.get("subtitle"):
        subtitle = str(found["subtitle"]).strip()
        if subtitle and subtitle not in table_title:
            table_title = f"{table_title} {subtitle}"

    for raw_tag in split_tags(tags):
        sheet = normalize_tag(raw_tag)

        payload = {
            "action": "upsert",
            "sheet": sheet,
            "level": str(level or ""),
            "title": table_title,
            "artist": found.get("artist", ""),
            "comment": raw_tag,
            "md5": found["md5"],
            "url_diff": song_url or "",
            "tag": sheet,
        }

        try:
            res = post_json(url, payload)
            print("[OK]", sheet, table_title, found["md5"], res)
            sent += 1
        except Exception as e:
            print("[NG]", sheet, table_title, e)
            skipped += 1

con.close()
print(f"done sent={sent} skipped={skipped}")
