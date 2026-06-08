import os
import re
import sqlite3
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv("fine.env")

SONGDATA_DB = Path(__file__).with_name("songdata.db")
SHEET_SYNC_URL = os.getenv("SHEET_SYNC_URL", "").strip()

TAG_TO_SHEET = {
    "横認識": "横認識",
    "横認識(8分系)": "横認識8分系",
    "横認識(縦系)": "横認識縦系",
    "縦連": "縦連",
    "ガチ押し系": "ガチ押し系",
    "乱打": "乱打",
    "地力上げ": "地力上げ",
    "しょうもないラス殺し": "ラス殺し",
    "ゴミ": "ゴミ",
    "良譜面": "良譜面",
    "日課": "日課",
    "惜敗": "惜敗",
}


def split_title_chart(title, chart_name=""):
    title = str(title or "").strip()
    chart_name = str(chart_name or "").strip()

    if chart_name:
        c = chart_name.strip()
        if not (c.startswith("[") and c.endswith("]")):
            c = f"[{c}]"
        return title, c

    if title.endswith("]") and "[" in title:
        base, bracket = title.rsplit("[", 1)
        base = base.strip()
        bracket = "[" + bracket.strip()
        if base and bracket:
            return base, bracket

    return title, chart_name


def fine_row_to_dict(row):
    if len(row) >= 8:
        sid, source, level, title, chart_name, url, tags, memo = row
    else:
        sid, source, level, title, url, tags, memo = row
        chart_name = ""

    title, chart_name = split_title_chart(title, chart_name)

    return {
        "id": sid,
        "source": source,
        "level": level,
        "title": title or "",
        "chart_name": chart_name or "",
        "url": url or "",
        "tags": tags or "",
        "memo": memo or "",
    }


def level_to_number(level):
    m = re.search(r"\d+", str(level or ""))
    return m.group(0) if m else str(level or "")


def normalize(text):
    return str(text or "").strip()



def strip_brackets(text):
    text = normalize(text)
    if text.startswith("[") and text.endswith("]"):
        return text[1:-1].strip()
    return text


def subtitle_candidates(chart_name):
    c = normalize(chart_name)
    if not c:
        return [""]

    candidates = [
        c,
        f"[{strip_brackets(c)}]",
        strip_brackets(c),
    ]

    seen = []
    for x in candidates:
        if x not in seen:
            seen.append(x)
    return seen



def join_title_subtitle_candidates(title, chart_name):
    title = normalize(title)
    cands = []

    for sub in subtitle_candidates(chart_name):
        sub_clean = normalize(sub)
        if not sub_clean:
            continue

        # title + [ANOTHER]
        cands.append(f"{title}{sub_clean}")

        # title + space + [ANOTHER]
        cands.append(f"{title} {sub_clean}")

        # title + ANOTHER
        cands.append(f"{title}{strip_brackets(sub_clean)}")

    seen = []
    for x in cands:
        if x and x not in seen:
            seen.append(x)
    return seen


def lookup_songdata(title, chart_name=""):
    if not SONGDATA_DB.exists():
        print("[sheet_sync] songdata.db not found")
        return None

    title = normalize(title)
    chart_name = normalize(chart_name)

    base_title = title
    embedded_chart = ""

    if title.endswith("]") and "[" in title:
        base_title, bracket = title.rsplit("[", 1)
        base_title = normalize(base_title)
        embedded_chart = "[" + bracket.strip()

    chart_plain = strip_brackets(chart_name or embedded_chart)

    title_candidates = []

    def add(x):
        x = normalize(x)
        if x and x not in title_candidates:
            title_candidates.append(x)

    add(title)
    add(title.replace(" [", "["))

    if chart_plain:
        add(f"{base_title}[{chart_plain}]")
        add(f"{base_title} [{chart_plain}]")
        add(f"{base_title}{chart_plain}")

    con = sqlite3.connect(SONGDATA_DB)
    cur = con.cursor()

    try:
        # 1. title完全一致
        for cand in title_candidates:
            row = cur.execute(
                """
                SELECT md5, title, subtitle, artist
                FROM song
                WHERE title = ?
                LIMIT 1
                """,
                (cand,),
            ).fetchone()

            if row:
                md5, db_title, db_subtitle, artist = row
                return {
                    "md5": md5,
                    "title": db_title or cand,
                    "subtitle": "",
                    "artist": artist or "",
                }

        # 2. title + chart_name を songdata.db の subtitle として照合
        subtitle_lookups = []

        if chart_plain:
            subtitle_lookups.append((base_title, f"[{chart_plain}]"))
            subtitle_lookups.append((base_title, chart_plain))

        for lookup_title, sub in subtitle_lookups:
            row = cur.execute(
                """
                SELECT md5, title, subtitle, artist
                FROM song
                WHERE title = ? AND subtitle = ?
                LIMIT 1
                """,
                (lookup_title, sub),
            ).fetchone()

            if row:
                md5, db_title, db_subtitle, artist = row
                return {
                    "md5": md5,
                    "title": db_title or lookup_title,
                    "subtitle": "",
                    "artist": artist or "",
                }

        # 3. DB基準の逆引き
        #    Fine Bot側 title が、songdata.db の
        #    「title + subtitle」「title + space + subtitle」「title + [subtitle]」
        #    のどれかと一致する場合だけ拾う。
        rows = cur.execute(
            """
            SELECT md5, title, subtitle, artist
            FROM song
            WHERE subtitle IS NOT NULL
              AND subtitle != ''
            """
        ).fetchall()

        for md5, db_title, db_subtitle, artist in rows:
            db_title = normalize(db_title)
            db_subtitle = normalize(db_subtitle)
            sub_plain = strip_brackets(db_subtitle)

            forms = []
            for form in [
                f"{db_title}{db_subtitle}",
                f"{db_title} {db_subtitle}",
                f"{db_title}{sub_plain}",
                f"{db_title} {sub_plain}",
            ]:
                form = normalize(form)
                if form and form not in forms:
                    forms.append(form)

            if title in forms:
                return {
                    "md5": md5,
                    "title": db_title,
                    "subtitle": "",
                    "artist": artist or "",
                }

        print(f"[sheet_sync] title not found: title={title} chart={chart_name} candidates={title_candidates}")

    finally:
        con.close()

    return None

def sync_song_to_sheet(fine_row, tag_name=None, emoji=None, enabled=True, user_id=None):
    if not SHEET_SYNC_URL:
        print("[sheet_sync] SHEET_SYNC_URL is empty")
        return False

    base_tag_name = re.sub(r"\[.*?\]$", "", str(tag_name or ""))
    sheet_name = TAG_TO_SHEET.get(tag_name) or TAG_TO_SHEET.get(base_tag_name)
    if not sheet_name:
        print(f"[sheet_sync] no sheet mapping for tag={tag_name}")
        return False

    song = fine_row_to_dict(fine_row)
    found = lookup_songdata(song["title"], song["chart_name"])

    if not found:
        print(f"[sheet_sync] md5 not found: {song['title']} / {song['chart_name']}")
        return False

    payload = {
        "action": "upsert" if enabled else "delete",
        "sheet": sheet_name,
        "level": str(song["level"] or ""),
        "title": found["title"],
        "artist": found["artist"],
        "comment": tag_name or "",
        "md5": found["md5"],
        "url_diff": song["url"],
    }
    if user_id is not None:
        payload["user_id"] = str(user_id)

    try:
        r = requests.post(SHEET_SYNC_URL, json=payload, timeout=10)
        log_line = (
            f"[sheet_sync] action={payload.get('action')} "
            f"sheet={sheet_name} title={payload.get('title')} "
            f"md5={payload.get('md5')} status={r.status_code} "
            f"body={r.text[:500]}\\n"
        )
        print(log_line.strip())
        with open("/tmp/fine_sheet_sync.log", "a", encoding="utf-8") as f:
            f.write(log_line)
        return 200 <= r.status_code < 300
    except Exception as e:
        print(f"[sheet_sync] post failed: {e}")
        return False
