import html
import json
import logging
import re
import sqlite3
import time
import unicodedata
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    load_dotenv = None

if load_dotenv:
    load_dotenv("fine.env")

DB = "stella_songs.db"
SONGDATA_DB = Path(__file__).with_name("songdata.db")
OUTPUT_ROOT = Path("public") / "tables"
_SONGDATA_ROWS = None
_SONGDATA_STATS = {"read_seconds": 0.0, "lookup_seconds": 0.0, "lookup_calls": 0}

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


def normalize_lookup_text(text):
    text = unicodedata.normalize("NFKC", str(text or ""))
    text = text.lower()
    text = re.sub(r"[\W_]+", "", text, flags=re.UNICODE)
    return text


def slugify(text, fallback="tag"):
    text = unicodedata.normalize("NFKC", str(text or "")).strip().lower()
    text = re.sub(r"[^a-z0-9_-]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text or fallback


def strip_brackets(text):
    text = str(text or "").strip()
    if text.startswith("[") and text.endswith("]"):
        return text[1:-1].strip()
    return text


def add_unique(items, value):
    value = str(value or "").strip()
    if value and value not in items:
        items.append(value)


def fine_display_candidates(title, chart_name):
    title = str(title or "").strip()
    chart_name = str(chart_name or "").strip()
    if not title:
        return []

    candidates = []
    if chart_name:
        chart_plain = strip_brackets(chart_name)
        add_unique(candidates, f"{title}{chart_name}")
        add_unique(candidates, f"{title} {chart_name}")
        if chart_plain:
            add_unique(candidates, f"{title}[{chart_plain}]")
            add_unique(candidates, f"{title} [{chart_plain}]")
    add_unique(candidates, title)
    return candidates


def songdata_display_candidates(title, subtitle):
    title = str(title or "").strip()
    subtitle = str(subtitle or "").strip()
    if not title:
        return []

    candidates = []
    add_unique(candidates, title)
    if subtitle:
        subtitle_plain = strip_brackets(subtitle)
        add_unique(candidates, f"{title}{subtitle}")
        add_unique(candidates, f"{title} {subtitle}")
        if subtitle_plain:
            add_unique(candidates, f"{title}[{subtitle_plain}]")
            add_unique(candidates, f"{title} [{subtitle_plain}]")
    return candidates


def load_songdata_rows(force=False):
    global _SONGDATA_ROWS
    if _SONGDATA_ROWS is not None and not force:
        return _SONGDATA_ROWS

    started = time.perf_counter()
    if not SONGDATA_DB.exists():
        _SONGDATA_ROWS = []
        _SONGDATA_STATS["read_seconds"] = time.perf_counter() - started
        return _SONGDATA_ROWS

    con = sqlite3.connect(SONGDATA_DB)
    try:
        cur = con.cursor()
        select_sql = songdata_select_sql(cur)
        rows = []
        for raw_row in cur.execute(select_sql).fetchall():
            row = songdata_row_to_dict(raw_row)
            candidates = songdata_display_candidates(row["title"], row["subtitle"])
            row["_display_candidates"] = tuple(candidates)
            row["_normalized_candidates"] = tuple(
                sorted({normalize_lookup_text(candidate) for candidate in candidates})
            )
            rows.append(row)
        _SONGDATA_ROWS = rows
        _SONGDATA_STATS["read_seconds"] = time.perf_counter() - started
        logging.info(
            "table_generator songdata.db read rows=%s seconds=%.3f",
            len(rows),
            _SONGDATA_STATS["read_seconds"],
        )
        return _SONGDATA_ROWS
    finally:
        con.close()


def reset_lookup_stats():
    _SONGDATA_STATS["lookup_seconds"] = 0.0
    _SONGDATA_STATS["lookup_calls"] = 0
    _lookup_songdata_cached.cache_clear()


def lookup_songdata(title, chart_name="", url="", level=""):
    started = time.perf_counter()
    try:
        return _lookup_songdata_cached(
            str(title or ""),
            str(chart_name or ""),
            str(url or ""),
            str(level or ""),
        )
    finally:
        _SONGDATA_STATS["lookup_calls"] += 1
        _SONGDATA_STATS["lookup_seconds"] += time.perf_counter() - started


@lru_cache(maxsize=4096)
def _lookup_songdata_cached(title, chart_name="", url="", level=""):
    rows = load_songdata_rows()

    fine_title = str(title or "").strip()
    fine_chart = str(chart_name or "").strip()
    fine_url = str(url or "").strip()
    if not fine_title:
        return None

    fine_candidates = fine_display_candidates(fine_title, fine_chart)

    if fine_url:
        url_matches = [
            row for row in rows
            if fine_url in {row.get("url", ""), row.get("url_diff", "")}
        ]
        found = choose_unique_match(url_matches)
        if found:
            return found

    for fine_candidate in fine_candidates:
        exact_matches = [
            row for row in rows
            if fine_candidate in row.get("_display_candidates", ())
        ]
        found = choose_unique_match(exact_matches)
        if found:
            return found

    for fine_candidate in fine_candidates:
        normalized_fine = normalize_lookup_text(fine_candidate)
        normalized_matches = [
            row for row in rows
            if normalized_fine in row.get("_normalized_candidates", ())
        ]
        found = choose_unique_match(normalized_matches)
        if found:
            return found

    return None


def songdata_select_sql(cur):
    cols = {row[1] for row in cur.execute("PRAGMA table_info(song)").fetchall()}
    sha_expr = "sha256" if "sha256" in cols else "'' AS sha256"
    artist_expr = "artist" if "artist" in cols else "'' AS artist"
    subtitle_expr = "subtitle" if "subtitle" in cols else "'' AS subtitle"
    url_expr = "url" if "url" in cols else "'' AS url"
    url_diff_expr = "url_diff" if "url_diff" in cols else "'' AS url_diff"
    level_expr = "level" if "level" in cols else "'' AS level"
    return (
        "SELECT "
        f"md5, {sha_expr}, title, {subtitle_expr}, {artist_expr}, "
        f"{url_expr}, {url_diff_expr}, {level_expr} "
        "FROM song"
    )


def unique_songdata_rows(rows):
    seen = set()
    unique = []
    for row in rows:
        key = (row.get("md5", ""), row.get("sha256", ""), row.get("title", ""), row.get("subtitle", ""))
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def songdata_row_to_dict(row):
    md5, sha256, title, subtitle, artist, url, url_diff, level = row
    return {
        "md5": md5 or "",
        "sha256": sha256 or "",
        "title": title or "",
        "subtitle": subtitle or "",
        "artist": artist or "",
        "url": url or "",
        "url_diff": url_diff or "",
        "level": level or "",
    }


def choose_unique_match(rows):
    unique = unique_songdata_rows(rows)
    if len(unique) == 1:
        return unique[0]
    return None


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
    found = lookup_songdata(title, chart_name, url=url, level=level)
    return {
        "id": sid,
        "source": source or "",
        "level": level or "",
        "title": title or "",
        "chart_name": chart_name or "",
        "display_title": display_title,
        "md5": found["md5"] if found else "",
        "sha256": found["sha256"] if found else "",
        "artist": found["artist"] if found else "",
        "songdata_title": found["title"] if found else "",
        "songdata_subtitle": found["subtitle"] if found else "",
        "url": url or "",
        "memo": memo or "",
        "tag": canonical_tag_name(tag_name),
        "comment": comment,
    }


def beatoraja_title(record):
    title = str(record.get("songdata_title") or "").strip()
    subtitle = str(record.get("songdata_subtitle") or "").strip()
    if title:
        if subtitle and subtitle not in title:
            return f"{title} {subtitle}"
        return title
    return str(record.get("display_title") or record.get("title") or "").strip()


def score_record(record):
    return {
        "level": str(record["level"]),
        "title": beatoraja_title(record),
        "artist": record["artist"],
        "comment": record["tag"],
        "md5": record["md5"],
        "url_diff": record["url"],
        "tag": record["tag"],
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


def make_score_url(table_base_url, user_id, slug):
    base = str(table_base_url or "").rstrip("/")
    if base:
        return f"{base}/users/{quote(str(user_id))}/tags/{quote(slug)}/score.json"
    return str(OUTPUT_ROOT / "users" / str(user_id) / "tags" / slug / "score.json")


def make_header_url(table_base_url, user_id, slug):
    base = str(table_base_url or "").rstrip("/")
    if base:
        return f"{base}/users/{quote(str(user_id))}/tags/{quote(slug)}/header.json"
    return str(OUTPUT_ROOT / "users" / str(user_id) / "tags" / slug / "header.json")


def generate_user_tables(user_id, table_base_url=None, output_root=OUTPUT_ROOT, progress=None):
    started_total = time.perf_counter()
    user_id = str(user_id)
    output_root = Path(output_root)
    con = db()
    ensure_tables(con)
    con.commit()

    try:
        reset_lookup_stats()
        load_songdata_rows()
        tag_specs = build_tag_specs(con, user_id)
        user_root = output_root / "users" / user_id
        tag_results = []

        total = len(tag_specs)
        json_started = time.perf_counter()
        for index, spec in enumerate(tag_specs, 1):
            if progress:
                progress("generate_tag", index=index, total=total, tag_name=spec["name"])
            rows = load_tag_songs(con, user_id, spec["name"])
            records = [song_to_record(row) for row in rows]
            tag_dir = user_root / "tags" / spec["slug"]
            tag_dir.mkdir(parents=True, exist_ok=True)

            beatoraja_records = [record for record in records if record["md5"]]
            score = [score_record(record) for record in beatoraja_records]
            header = {
                "name": f"Fine {spec['name']}",
                "symbol": "",
                "data_url": "score.json",
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
                    "score_count": len(score),
                    "songs": records,
                },
            )
            (tag_dir / "index.html").write_text(render_html(user_id, spec["name"], records), encoding="utf-8")

            tag_results.append(
                {
                    "tag_name": spec["name"],
                    "slug": spec["slug"],
                    "count": len(records),
                    "score_count": len(score),
                    "path": str(tag_dir),
                    "relative_url": f"tags/{spec['slug']}/",
                    "url": make_public_url(table_base_url, user_id, spec["slug"]),
                    "table_url": make_header_url(table_base_url, user_id, spec["slug"]),
                    "score_url": make_score_url(table_base_url, user_id, spec["slug"]),
                }
            )

        user_root.mkdir(parents=True, exist_ok=True)
        (user_root / "index.html").write_text(render_user_index(user_id, tag_results), encoding="utf-8")
        write_json(user_root / "index.json", {"user_id": user_id, "tags": tag_results})
        json_seconds = time.perf_counter() - json_started
        logging.info(
            "table_generator timings user_id=%s songdata_read=%.3fs lookup_total=%.3fs lookup_calls=%s json_generation=%.3fs total=%.3fs",
            user_id,
            _SONGDATA_STATS["read_seconds"],
            _SONGDATA_STATS["lookup_seconds"],
            _SONGDATA_STATS["lookup_calls"],
            json_seconds,
            time.perf_counter() - started_total,
        )
        return {
            "user_id": user_id,
            "root": str(user_root),
            "tags": tag_results,
            "index_url": str(table_base_url).rstrip("/") + f"/users/{quote(user_id)}/" if table_base_url else str(user_root),
        }
    finally:
        con.close()
