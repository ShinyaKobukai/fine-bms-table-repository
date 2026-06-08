import html
import json
import re
import sqlite3
import unicodedata
from pathlib import Path
from urllib.parse import quote

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    load_dotenv = None

if load_dotenv:
    load_dotenv("fine.env")

DB = "stella_songs.db"
OUTPUT_ROOT = Path("public") / "tables"

FIXED_TAGS = [
    ("y", "\u6a2a\u8a8d\u8b58"),
    ("8", "\u6a2a\u8a8d\u8b58(8\u5206\u7cfb)"),
    ("yt", "\u6a2a\u8a8d\u8b58(\u7e26\u7cfb)"),
    ("tt", "\u7e26\u9023"),
    ("go", "\u30ac\u30c1\u62bc\u3057\u7cfb"),
    ("r", "\u4e71\u6253"),
    ("ep", "\u5730\u529b\u4e0a\u3052"),
    ("ni", "\u826f\u8b5c\u9762"),
    ("sr", "\u30e9\u30b9\u6bba\u3057"),
    ("g", "\u30b4\u30df"),
    ("sh", "\u60dc\u6557"),
    ("dy", "\u65e5\u8ab2"),
]

OLD_LAST_KILL_TAG = "\u3057\u3087\u3046\u3082\u306a\u3044\u30e9\u30b9\u6bba\u3057"
NEW_LAST_KILL_TAG = "\u30e9\u30b9\u6bba\u3057"


def db():
    return sqlite3.connect(DB)


def canonical_tag_name(tag):
    tag = str(tag or "")
    if tag == OLD_LAST_KILL_TAG:
        return NEW_LAST_KILL_TAG
    return tag


def tag_matches(song_tag, wanted_tag):
    song_tag = canonical_tag_name(song_tag)
    wanted_tag = canonical_tag_name(wanted_tag)
    if wanted_tag == "\u60dc\u6557":
        return song_tag == "\u60dc\u6557" or song_tag.startswith("\u60dc\u6557[")
    return song_tag == wanted_tag


def normalize_text(text):
    text = unicodedata.normalize("NFKC", str(text or ""))
    return text.strip().lower()


def slugify(text, fallback="tag"):
    text = unicodedata.normalize("NFKC", str(text or "")).strip().lower()
    text = re.sub(r"[^a-z0-9_-]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text or fallback


def ensure_tables(con):
    con.execute("""
    CREATE TABLE IF NOT EXISTS songs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT NOT NULL,
        level TEXT NOT NULL,
        title TEXT NOT NULL,
        chart_name TEXT,
        url TEXT,
        tags TEXT,
        memo TEXT,
        created_at TEXT,
        updated_at TEXT
    )
    """)
    con.execute("""
    CREATE TABLE IF NOT EXISTS user_tags (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        song_id INTEGER NOT NULL,
        tag_name TEXT NOT NULL,
        memo TEXT DEFAULT '',
        created_at TEXT DEFAULT '',
        updated_at TEXT DEFAULT '',
        UNIQUE(user_id, song_id, tag_name)
    )
    """)
    con.execute("""
    CREATE TABLE IF NOT EXISTS user_custom_tags (
        user_id TEXT NOT NULL,
        short_name TEXT NOT NULL,
        full_name TEXT NOT NULL,
        created_at TEXT DEFAULT '',
        updated_at TEXT DEFAULT '',
        PRIMARY KEY(user_id, short_name)
    )
    """)
    con.execute(
        "UPDATE user_tags SET tag_name=? WHERE tag_name=?",
        (NEW_LAST_KILL_TAG, OLD_LAST_KILL_TAG),
    )
    con.execute(
        "UPDATE user_custom_tags SET full_name=? WHERE full_name=?",
        (NEW_LAST_KILL_TAG, OLD_LAST_KILL_TAG),
    )


def level_sort_key(level):
    n = normalize_text(level)
    match = re.search(r"\d+", n)
    number = int(match.group(0)) if match else 999
    if n.startswith("sl"):
        group = 0
    elif n.startswith("st"):
        group = 1
    elif n.startswith("lv"):
        group = 2
    else:
        group = 9
    return (group, number, n)


def load_custom_tag_slugs(con, user_id):
    rows = con.execute(
        """
        SELECT short_name, full_name
        FROM user_custom_tags
        WHERE user_id=?
        """,
        (str(user_id),),
    ).fetchall()
    return {canonical_tag_name(full_name): slugify(short_name, fallback="custom") for short_name, full_name in rows}


def build_tag_specs(con, user_id):
    fixed = [
        {"slug": slug, "name": name, "fixed": True}
        for slug, name in FIXED_TAGS
    ]
    fixed_names = {spec["name"] for spec in fixed}
    custom_slug_by_name = load_custom_tag_slugs(con, user_id)

    rows = con.execute(
        """
        SELECT DISTINCT tag_name
        FROM user_tags
        WHERE user_id=?
        ORDER BY tag_name
        """,
        (str(user_id),),
    ).fetchall()

    custom = []
    used_slugs = {spec["slug"] for spec in fixed}
    for (raw_tag,) in rows:
        tag = canonical_tag_name(raw_tag)
        if any(tag_matches(tag, fixed_name) for fixed_name in fixed_names):
            continue

        base_slug = custom_slug_by_name.get(tag) or slugify(tag, fallback="custom")
        slug = base_slug
        i = 2
        while slug in used_slugs:
            slug = f"{base_slug}-{i}"
            i += 1
        used_slugs.add(slug)
        custom.append({"slug": f"custom/{slug}", "name": tag, "fixed": False})

    custom.sort(key=lambda spec: normalize_text(spec["name"]))
    return fixed + custom


def load_tag_songs(con, user_id, tag_name):
    rows = con.execute(
        """
        SELECT
            s.id,
            s.source,
            s.level,
            s.title,
            s.chart_name,
            s.url,
            s.memo,
            ut.tag_name,
            COALESCE(ut.memo, '')
        FROM user_tags ut
        JOIN songs s ON s.id = ut.song_id
        WHERE ut.user_id=?
        ORDER BY s.level, s.title, s.chart_name, ut.id
        """,
        (str(user_id),),
    ).fetchall()

    matched = []
    for row in rows:
        if tag_matches(row[7], tag_name):
            matched.append(row)

    matched.sort(key=lambda row: (level_sort_key(row[2]), normalize_text(song_display_name(row))))
    return matched


def song_display_name(row):
    title = row[3] or ""
    chart_name = row[4] or ""
    return f"{title} [{chart_name}]" if chart_name else title


def song_to_record(row):
    sid, source, level, title, chart_name, url, memo, tag_name, user_tag_memo = row
    display_title = song_display_name(row)
    comment = user_tag_memo or memo or ""
    return {
        "id": sid,
        "source": source or "",
        "level": level or "",
        "title": title or "",
        "chart_name": chart_name or "",
        "display_title": display_title,
        "url": url or "",
        "memo": memo or "",
        "tag": canonical_tag_name(tag_name),
        "comment": comment,
    }


def score_record(record):
    return {
        "md5": "",
        "sha256": "",
        "level": str(record["level"]),
        "title": record["display_title"],
        "artist": "",
        "url": record["url"],
        "url_diff": record["url"],
        "comment": record["comment"],
    }


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def render_html(user_id, tag_name, records):
    rows = []
    for record in records:
        title = html.escape(record["display_title"])
        level = html.escape(str(record["level"]))
        comment = html.escape(record["comment"])
        url = html.escape(record["url"])
        link = f'<a href="{url}">差分</a>' if url else ""
        rows.append(
            f"<tr><td>{level}</td><td>{title}</td><td>{comment}</td><td>{link}</td></tr>"
        )

    body = "\n".join(rows) or '<tr><td colspan="4">このタグの曲はまだありません。</td></tr>'
    return f"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(tag_name)} - Fine BMS Table</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 24px; color: #1f2937; }}
    h1 {{ font-size: 24px; margin-bottom: 4px; }}
    .meta {{ color: #6b7280; margin-bottom: 20px; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border-bottom: 1px solid #e5e7eb; padding: 8px 10px; text-align: left; }}
    th {{ background: #f9fafb; }}
    a {{ color: #2563eb; }}
  </style>
</head>
<body>
  <h1>{html.escape(tag_name)}</h1>
  <div class="meta">user_id: {html.escape(str(user_id))} / {len(records)} songs</div>
  <p><a href="header.json">header.json</a> / <a href="score.json">score.json</a> / <a href="data.json">data.json</a></p>
  <table>
    <thead><tr><th>Level</th><th>Title</th><th>Comment</th><th>URL</th></tr></thead>
    <tbody>{body}</tbody>
  </table>
</body>
</html>
"""


def render_user_index(user_id, tag_results):
    items = []
    for result in tag_results:
        url = quote(result["relative_url"].replace("\\", "/"))
        items.append(
            f'<li><a href="{url}">{html.escape(result["tag_name"])}</a> ({result["count"]})</li>'
        )
    body = "\n".join(items)
    return f"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Fine BMS Tables</title>
</head>
<body>
  <h1>Fine BMS Tables</h1>
  <p>user_id: {html.escape(str(user_id))}</p>
  <ul>{body}</ul>
</body>
</html>
"""


def make_public_url(table_base_url, user_id, slug):
    base = str(table_base_url or "").rstrip("/")
    path = f"users/{quote(str(user_id))}/tags/{quote(slug)}/"
    if base:
        return f"{base}/{path}"
    return str(OUTPUT_ROOT / path)


def generate_user_tables(user_id, table_base_url=None, output_root=OUTPUT_ROOT):
    user_id = str(user_id)
    output_root = Path(output_root)
    con = db()
    ensure_tables(con)
    con.commit()

    try:
        tag_specs = build_tag_specs(con, user_id)
        user_root = output_root / "users" / user_id
        tag_results = []

        for spec in tag_specs:
            rows = load_tag_songs(con, user_id, spec["name"])
            records = [song_to_record(row) for row in rows]
            tag_dir = user_root / "tags" / spec["slug"]
            tag_dir.mkdir(parents=True, exist_ok=True)

            score = [score_record(record) for record in records]
            header = {
                "name": f"Fine {spec['name']}",
                "symbol": spec["slug"].replace("/", "-"),
                "data_url": "score.json",
                "level_order": sorted({str(record["level"]) for record in records}, key=level_sort_key),
            }

            write_json(tag_dir / "header.json", header)
            write_json(tag_dir / "score.json", score)
            write_json(
                tag_dir / "data.json",
                {
                    "user_id": user_id,
                    "tag": spec["name"],
                    "slug": spec["slug"],
                    "fixed": spec["fixed"],
                    "count": len(records),
                    "songs": records,
                },
            )
            (tag_dir / "index.html").write_text(render_html(user_id, spec["name"], records), encoding="utf-8")

            tag_results.append(
                {
                    "tag_name": spec["name"],
                    "slug": spec["slug"],
                    "count": len(records),
                    "path": str(tag_dir),
                    "relative_url": f"tags/{spec['slug']}/",
                    "url": make_public_url(table_base_url, user_id, spec["slug"]),
                }
            )

        user_root.mkdir(parents=True, exist_ok=True)
        (user_root / "index.html").write_text(render_user_index(user_id, tag_results), encoding="utf-8")
        write_json(user_root / "index.json", {"user_id": user_id, "tags": tag_results})
        return {
            "user_id": user_id,
            "root": str(user_root),
            "tags": tag_results,
            "index_url": str(table_base_url).rstrip("/") + f"/users/{quote(user_id)}/" if table_base_url else str(user_root),
        }
    finally:
        con.close()
