import os
import re
import json
import sqlite3
import unicodedata
import asyncio
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import discord
from bs4 import BeautifulSoup
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv("fine.env")

TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))

DB = "stella_songs.db"
JST = ZoneInfo("Asia/Tokyo")

TABLES = {
    "st": "https://stellabms.xyz/st/table_rec.html",
    "sl": "https://stellabms.xyz/sl/table_rec.html",
}

TAG_ALIASES = {
    "y": "横認識",
    "r": "乱打",
    "8": "横認識(8分系)",
    "yt": "横認識(縦系)",
    "tt": "縦連",
    "sr": "ラス殺し",
    "ep": "地力上げ",
    "ni": "良譜面",
    "g": "ゴミ",
    "sh": "惜敗",
}

OLD_LAST_KILL_TAG = "しょうもないラス殺し"
NEW_LAST_KILL_TAG = "\u30e9\u30b9\u6bba\u3057"
TAG_ALIASES["sr"] = NEW_LAST_KILL_TAG


TAG_INPUT_ALIASES = {
    "ん": "y",
    "す": "r",
    "ゆ": "8",
    "はち": "8",
    "んか": "yt",
    "んt": "yt",
    "かか": "tt",
    "っt": "tt",
    "とす": "sr",
    "えp": "ep",
    "えぴ": "ep",
    "いせ": "ep",
    "dy": "日課",
    "しん": "ep",
    "に": "ni",
    "みに": "ni",
    "あdd": "add",
    "あd": "add",
    "き": "g",
}


def normalize_tag_input(token):
    token = normalize_text(token)
    return TAG_INPUT_ALIASES.get(token, token)


CUSTOM_EMOJI_PATTERN = re.compile(r"^<a?:([A-Za-z0-9_]+):([0-9]+)>$")


def normalize_reaction_emoji(emoji):
    emoji = str(emoji or "").strip()
    match = CUSTOM_EMOJI_PATTERN.match(emoji)
    if match:
        return f"{match.group(1)}:{match.group(2)}"
    return emoji


last_search = {}
reaction_song_messages = {}

CUSTOM_TAG_REACTION_EMOJIS = [
    "\U0001f1e6", "\U0001f1e7", "\U0001f1e8", "\U0001f1e9", "\U0001f1ea", "\U0001f1eb",
    "\U0001f1ec", "\U0001f1ed", "\U0001f1ee", "\U0001f1ef", "\U0001f1f0", "\U0001f1f1",
    "\U0001f1f2", "\U0001f1f3", "\U0001f1f4", "\U0001f1f5", "\U0001f1f6", "\U0001f1f7",
    "\U0001f1f8", "\U0001f1f9", "\U0001f1fa", "\U0001f1fb", "\U0001f1fc", "\U0001f1fd",
    "\U0001f1fe", "\U0001f1ff",
]


def now_text():
    return datetime.now(JST).isoformat(timespec="seconds")


def normalize_text(text):
    text = unicodedata.normalize("NFKC", str(text))
    text = text.replace("　", " ")
    text = text.replace("、", " ")
    text = text.replace(",", " ")
    return text.strip().lower()




def db():
    return sqlite3.connect(DB)






def canonical_tag_name(tag):
    if tag == OLD_LAST_KILL_TAG:
        return NEW_LAST_KILL_TAG
    return tag


def ensure_user_custom_tags_table(con):
    con.execute("""
    CREATE TABLE IF NOT EXISTS user_custom_tags (
        user_id TEXT NOT NULL,
        short_name TEXT NOT NULL,
        full_name TEXT NOT NULL,
        emoji TEXT DEFAULT '',
        created_at TEXT DEFAULT '',
        updated_at TEXT DEFAULT '',
        PRIMARY KEY(user_id, short_name)
    )
    """)
    try:
        con.execute("ALTER TABLE user_custom_tags ADD COLUMN emoji TEXT DEFAULT ''")
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e).lower():
            raise
    con.execute(
        "UPDATE user_tags SET tag_name=? WHERE tag_name=?",
        (NEW_LAST_KILL_TAG, OLD_LAST_KILL_TAG),
    )
    con.execute(
        "UPDATE user_custom_tags SET full_name=? WHERE full_name=?",
        (NEW_LAST_KILL_TAG, OLD_LAST_KILL_TAG),
    )


def add_custom_tag(full_name, short_name, user_id, emoji=""):
    con = db()
    ensure_user_custom_tags_table(con)
    now = now_text()
    user_id = str(user_id)
    emoji = (emoji or "").strip()
    normalized_emoji = normalize_reaction_emoji(emoji)

    if normalized_emoji:
        for fixed_emoji, fixed_tag in TAG_REACTION_EMOJIS:
            if normalized_emoji == normalize_reaction_emoji(fixed_emoji):
                con.close()
                return False, f"`{emoji}` は固定タグ「{fixed_tag}」で使っている絵文字だよ！"

        for existing_short, existing_name, existing_emoji in con.execute(
            """
            SELECT short_name, full_name, emoji
            FROM user_custom_tags
            WHERE user_id=? AND short_name<>? AND COALESCE(emoji, '')<>''
            """,
            (user_id, short_name),
        ):
            if normalized_emoji == normalize_reaction_emoji(existing_emoji):
                con.close()
                return False, f"`{emoji}` は既に「{existing_name}」で使っている絵文字だよ！"

    con.execute(
        """
        INSERT OR REPLACE INTO user_custom_tags
        (user_id, short_name, full_name, emoji, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (user_id, short_name, canonical_tag_name(full_name), emoji, now, now)
    )

    con.commit()
    con.close()
    schedule_auto_table_publish(user_id, reason="custom_tag_added")
    return True, ""


def delete_custom_tag(short_name, user_id):
    con = db()
    ensure_user_custom_tags_table(con)

    cur = con.execute(
        "DELETE FROM user_custom_tags WHERE user_id=? AND short_name=?",
        (str(user_id), short_name)
    )

    con.commit()
    con.close()
    if cur.rowcount > 0:
        schedule_auto_table_publish(user_id, reason="custom_tag_deleted")
        return True
    return False


def get_user_custom_tag_rows(user_id):
    if user_id is None:
        return []

    con = db()
    try:
        ensure_user_custom_tags_table(con)
        rows = con.execute(
            """
            SELECT short_name, full_name, COALESCE(emoji, '')
            FROM user_custom_tags
            WHERE user_id=?
            ORDER BY short_name
            """,
            (str(user_id),),
        ).fetchall()
    finally:
        con.close()

    return [
        {
            "short_name": short_name,
            "full_name": canonical_tag_name(full_name),
            "emoji": emoji or "",
        }
        for short_name, full_name, emoji in rows
    ]



def tag_matches(song_tag, wanted_tag):
    song_tag = canonical_tag_name(song_tag)
    wanted_tag = canonical_tag_name(wanted_tag)
    if wanted_tag == "惜敗":
        return song_tag == "惜敗" or song_tag.startswith("惜敗[")
    return song_tag == wanted_tag

def ensure_user_tags_table(con):
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


def get_user_song_tags(user_id, song_id):
    if user_id is None:
        return []

    con = db()
    ensure_user_tags_table(con)
    rows = con.execute(
        """
        SELECT tag_name
        FROM user_tags
        WHERE user_id=? AND song_id=?
        ORDER BY id
        """,
        (str(user_id), song_id),
    ).fetchall()
    con.close()
    return [canonical_tag_name(row[0]) for row in rows]


def get_user_song_tags_map(user_id, song_ids=None):
    if user_id is None:
        return {}

    con = db()
    ensure_user_tags_table(con)
    params = [str(user_id)]
    where = "WHERE user_id=?"

    if song_ids is not None:
        song_ids = [int(song_id) for song_id in song_ids]
        if not song_ids:
            con.close()
            return {}
        placeholders = ",".join("?" for _ in song_ids)
        where += f" AND song_id IN ({placeholders})"
        params.extend(song_ids)

    rows = con.execute(
        f"""
        SELECT song_id, tag_name
        FROM user_tags
        {where}
        ORDER BY id
        """,
        params,
    ).fetchall()
    con.close()

    tags_by_song = {}
    for song_id, tag_name in rows:
        tags_by_song.setdefault(song_id, []).append(canonical_tag_name(tag_name))
    return tags_by_song


def get_song_tags_for_display(row, user_id=None):
    song_id = row[0]
    return join_tags(get_user_song_tags(user_id, song_id))


def split_tags(text):
    if not text:
        return []
    return [x for x in text.split("|") if x]




def resolve_tag(token, user_id=None):
    token = normalize_tag_input(token)

    tags = get_all_tags(user_id=user_id)

    if token in tags:
        return canonical_tag_name(tags[token])

    for formal in tags.values():
        if normalize_text(formal) == token:
            return canonical_tag_name(formal)

    if normalize_text(OLD_LAST_KILL_TAG) == token:
        return NEW_LAST_KILL_TAG

    return None


def parse_edit_args(args, user_id=None):
    tags = []
    memo_parts = []
    memo_is_null = False

    for token in args:
        normalized = normalize_tag_input(token)

        if normalized == "/null":
            memo_is_null = True
            continue

        if normalized == "sh":
            tags.append(f"惜敗[{datetime.now(JST).strftime('%Y-%m-%d')}]")
            continue

        tag = resolve_tag(token, user_id=user_id)
        if tag:
            tags.append(tag)
        else:
            memo_parts.append(token)

    if memo_is_null:
        memo = ""
    else:
        memo = " ".join(memo_parts).strip()

    if len(memo) > 150:
        memo = memo[:150]

    return join_tags(tags), memo


def ensure_md5_overrides_table_bot(con):
    con.execute("""
    CREATE TABLE IF NOT EXISTS md5_overrides (
        song_id INTEGER PRIMARY KEY,
        url_diff TEXT,
        title TEXT,
        chart_name TEXT,
        level TEXT,
        md5 TEXT NOT NULL,
        source TEXT DEFAULT 'manual',
        created_by_user_id TEXT,
        created_at TEXT,
        updated_at TEXT
    )
    """)
    con.execute("""
    CREATE TABLE IF NOT EXISTS md5_override_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        song_id INTEGER NOT NULL,
        url_diff TEXT,
        title TEXT,
        chart_name TEXT,
        level TEXT,
        old_md5 TEXT DEFAULT '',
        new_md5 TEXT DEFAULT '',
        action TEXT NOT NULL,
        source TEXT DEFAULT 'abmd5',
        created_by_user_id TEXT,
        created_at TEXT
    )
    """)


def record_md5_override_history(con, row, old_md5, new_md5, action, user_id, source="abmd5"):
    con.execute(
        """
        INSERT INTO md5_override_history
            (song_id, url_diff, title, chart_name, level, old_md5, new_md5, action, source, created_by_user_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row[0],
            row[5] or "",
            row[3] or "",
            row[4] or "",
            row[2] or "",
            old_md5 or "",
            new_md5 or "",
            action,
            source,
            str(user_id),
            now_text(),
        ),
    )


def valid_md5(value):
    return bool(re.fullmatch(r"[0-9a-fA-F]{32}", str(value or "").strip()))


def resolve_song_for_abmd5(ctx, target):
    target = str(target or "").strip()
    if not target:
        return None, "曲指定が空みたいだよ！"

    rows = last_search.get(ctx.author.id, [])
    if target.isdigit():
        number = int(target)
        if 1 <= number <= len(rows):
            return rows[number - 1], ""
        row = get_song_by_id(number)
        if row:
            return row, ""

    normalized_target = normalize_text(target)
    exact_matches = []
    partial_matches = []
    con = db()
    try:
        for row in con.execute("SELECT id, source, level, title, chart_name, url, tags, memo FROM songs"):
            display_name = song_display_name(row)
            haystacks = [
                normalize_text(row[3] or ""),
                normalize_text(row[4] or ""),
                normalize_text(display_name),
            ]
            if normalized_target in haystacks:
                exact_matches.append(row)
            elif any(normalized_target in hay for hay in haystacks):
                partial_matches.append(row)
    finally:
        con.close()

    matches = exact_matches or partial_matches
    if len(matches) == 1:
        return matches[0], ""
    if not matches:
        return None, f"`{target}` に合う曲が見つからなかったよ！ `!s` 後に番号指定もできるよ。"
    examples = "\n".join(f"{row[0]}: {row[2]} {song_display_name(row)}" for row in matches[:5])
    return None, f"`{target}` は候補が複数あるみたいだよ。song_idか `!s` 後の番号で指定してね。\n```text\n{examples}\n```"


def set_md5_override(row, md5, user_id, source="manual_abmd5", action="set", schedule_publish=True):
    con = db()
    ensure_md5_overrides_table_bot(con)
    now = now_text()
    old = con.execute("SELECT md5 FROM md5_overrides WHERE song_id=?", (row[0],)).fetchone()
    old_md5 = old[0] if old else ""
    record_md5_override_history(con, row, old_md5, md5.lower(), action, user_id, source=source)
    con.execute(
        """
        INSERT OR REPLACE INTO md5_overrides
            (song_id, url_diff, title, chart_name, level, md5, source, created_by_user_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (row[0], row[5] or "", row[3] or "", row[4] or "", row[2] or "", md5.lower(), source, str(user_id), now, now),
    )
    con.commit()
    con.close()
    if schedule_publish:
        schedule_auto_table_publish(user_id, reason="md5_override_updated")


def delete_md5_override(song_id, user_id):
    con = db()
    ensure_md5_overrides_table_bot(con)
    row = get_song_by_id(song_id)
    old = con.execute("SELECT md5 FROM md5_overrides WHERE song_id=?", (song_id,)).fetchone()
    if row and old:
        record_md5_override_history(con, row, old[0] or "", "", "delete", user_id)
    cur = con.execute("DELETE FROM md5_overrides WHERE song_id=?", (song_id,))
    con.commit()
    con.close()
    if cur.rowcount > 0:
        schedule_auto_table_publish(user_id, reason="md5_override_deleted")
        return True
    return False


def clear_all_md5_overrides(user_id):
    con = db()
    ensure_md5_overrides_table_bot(con)
    rows = con.execute(
        """
        SELECT s.id, s.source, s.level, s.title, s.chart_name, s.url, s.tags, s.memo, mo.md5
        FROM md5_overrides mo
        JOIN songs s ON s.id = mo.song_id
        WHERE COALESCE(mo.source, '') IN ('manual_abmd5', 'auto_abmd5', 'abmd5')
        """
    ).fetchall()
    for row in rows:
        song_row = row[:8]
        record_md5_override_history(con, song_row, row[8] or "", "", "clear_all", user_id)
    cur = con.execute("DELETE FROM md5_overrides WHERE COALESCE(source, '') IN ('manual_abmd5', 'auto_abmd5', 'abmd5')")
    con.commit()
    con.close()
    if cur.rowcount > 0:
        schedule_auto_table_publish(user_id, reason="md5_override_clear_all")
    return cur.rowcount


def score_abmd5_candidate(song_row, songdata_row):
    from table_generator import fine_display_candidates, normalize_lookup_text

    fine_title = song_row[3] or ""
    fine_chart = song_row[4] or ""
    fine_url = song_row[5] or ""
    fine_level = normalize_text(song_row[2] or "")
    fine_candidates = fine_display_candidates(fine_title, fine_chart)
    normalized_fine = {normalize_lookup_text(candidate) for candidate in fine_candidates}
    normalized_fine_title = normalize_lookup_text(fine_title)
    normalized_fine_chart = normalize_lookup_text(fine_chart)

    score = 0
    reasons = []

    if fine_url and fine_url in {songdata_row.get("url", ""), songdata_row.get("url_diff", "")}:
        score += 120
        reasons.append("url")

    db_candidates = set(songdata_row.get("_display_candidates", ()))
    db_normalized = set(songdata_row.get("_normalized_candidates", ()))
    if db_candidates.intersection(fine_candidates):
        score += 95
        reasons.append("display")
    elif db_normalized.intersection(normalized_fine):
        score += 82
        reasons.append("normalized")

    normalized_db_title = normalize_lookup_text(songdata_row.get("title", ""))
    normalized_db_subtitle = normalize_lookup_text(songdata_row.get("subtitle", ""))
    if normalized_fine_title and normalized_fine_title == normalized_db_title:
        score += 22
        reasons.append("title")
    elif normalized_fine_title and (
        normalized_fine_title in normalized_db_title or normalized_db_title in normalized_fine_title
    ):
        score += 12
        reasons.append("title-like")

    if normalized_fine_chart and normalized_db_subtitle and normalized_fine_chart == normalized_db_subtitle:
        score += 14
        reasons.append("chart")

    db_level = normalize_text(songdata_row.get("level", ""))
    if fine_level and db_level and fine_level == db_level:
        score += 4
        reasons.append("level")

    return score, "+".join(reasons) or "text"


def infer_abmd5_match(song_row):
    from table_generator import load_songdata_rows

    rows = load_songdata_rows()
    scored = []
    for songdata_row in rows:
        md5 = songdata_row.get("md5", "")
        if not valid_md5(md5):
            continue
        score, reason = score_abmd5_candidate(song_row, songdata_row)
        if score >= 70:
            scored.append((score, reason, songdata_row))

    scored.sort(key=lambda item: item[0], reverse=True)
    if not scored:
        return None, "no_candidate", 0, "", None

    top_score, top_reason, top = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0
    if top_score < 82 and top_score - second_score < 15:
        return None, "ambiguous", top_score, top_reason, top
    if top_score >= 82 and second_score and top_score - second_score < 4:
        return None, "ambiguous", top_score, top_reason, top

    return top, top_reason, top_score, "", None


def auto_abmd5_candidates_for_user(user_id, tag_name=None, limit=50):
    from table_generator import missing_md5_records

    missing = missing_md5_records(str(user_id), tag_name=tag_name)
    seen = set()
    candidates = []
    skipped = []

    for item in missing:
        song_id = item["song_id"]
        if song_id in seen:
            continue
        seen.add(song_id)
        if len(candidates) >= limit:
            skipped_item = dict(item)
            skipped_item["reason"] = "limit"
            skipped_item["score"] = 0
            skipped_item["detail"] = ""
            skipped.append(skipped_item)
            continue

        song_row = get_song_by_id(song_id)
        if not song_row:
            skipped_item = dict(item)
            skipped_item["reason"] = "missing_song"
            skipped_item["score"] = 0
            skipped_item["detail"] = ""
            skipped.append(skipped_item)
            continue

        found, reason, score, detail, rejected = infer_abmd5_match(song_row)
        if not found and rejected:
            found = rejected
            reason = f"nearest-{reason}"
            if detail:
                reason = f"{reason}+{detail}"
        if not found:
            skipped_item = dict(item)
            skipped_item["reason"] = reason
            skipped_item["score"] = score
            skipped_item["detail"] = detail or ""
            if rejected:
                skipped_item["songdata_title"] = rejected.get("title", "")
                skipped_item["songdata_subtitle"] = rejected.get("subtitle", "")
                skipped_item["md5"] = rejected.get("md5", "")
            skipped.append(skipped_item)
            continue

        set_md5_override(
            song_row,
            found["md5"],
            user_id,
            source="auto_abmd5",
            action="auto_set",
            schedule_publish=False,
        )
        candidates.append(
            {
                "song_id": song_id,
                "level": song_row[2] or "",
                "title": song_display_name(song_row),
                "md5": found["md5"],
                "score": score,
                "reason": reason,
                "songdata_title": found.get("title", ""),
                "songdata_subtitle": found.get("subtitle", ""),
            }
        )

    return candidates, skipped, len(seen)


def discover_and_import():
    imported = []
    errors = []

    sources = {
        "st": "https://stellabms.xyz/st/",
        "sl": "https://stellabms.xyz/sl/",
    }

    pages = [
        ("header.json", "通常"),
        ("header_rec.json", "rec"),
    ]

    for source, base in sources.items():
        for header_name, label in pages:
            try:
                header_url = base + header_name
                header = requests.get(header_url, timeout=20).json()

                data_url = header.get("data_url", "score.json")

                if data_url.startswith("http"):
                    url = data_url
                else:
                    url = base + data_url

                data = requests.get(url, timeout=30).json()

                rows = []

                for item in data:
                    level = str(item.get("level", "")).strip()
                    title = str(item.get("title", "")).strip()

                    if not level or not title:
                        continue

                    rows.append({
                        "source": source,
                        "level": f"{source}{level}",
                        "title": title,
                        "url": item.get("url_diff") or item.get("url") or "",
                    })

                imported += upsert_songs(rows)

            except Exception:
                errors.append(f"{source} {label}: 取得または更新に失敗しました")

    return imported, errors


def upsert_songs(rows):
    added = []
    con = db()

    for r in rows:
        source = r["source"]
        level = normalize_text(r["level"])
        title = r["title"].strip()
        url = r.get("url", "")

        if not title or not level:
            continue

        cur = con.execute(
            "SELECT id FROM songs WHERE source=? AND level=? AND title=?",
            (source, level, title)
        )
        exists = cur.fetchone()

        if exists:
            con.execute(
                "UPDATE songs SET url=?, updated_at=? WHERE source=? AND level=? AND title=?",
                (url, now_text(), source, level, title)
            )
        else:
            con.execute(
                """
                INSERT INTO songs(source, level, title, url, tags, memo, created_at, updated_at)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (source, level, title, url, "", "", now_text(), now_text())
            )
            added.append(f"{level} {title}")

    con.commit()
    con.close()
    return added






def song_display_name(row):
    title = row[3]
    chart_name = row[4] if len(row) >= 8 else ""
    name = title
    if chart_name:
        name += f" [{chart_name}]"
    return name


def add_tags_to_song(row, add_tags, memo=None, user_id=None):
    current_tags = split_tags(get_song_tags_for_display(row, user_id=user_id))
    merged_tags = current_tags + split_tags(add_tags)

    current_memo = row[7] if len(row) >= 8 else ""

    if memo is None:
        memo = current_memo or ""

    update_song(row, join_tags(merged_tags), memo, user_id=user_id)


def append_tags_to_song(row, tags_to_add, user_id=None):
    sid = row[0]

    con = db()
    cur = con.execute(
        "SELECT id, source, level, title, chart_name, url, tags, memo FROM songs WHERE id=?",
        (sid,)
    )
    fresh = cur.fetchone()
    con.close()

    if not fresh:
        return

    current_tags = get_song_tags_for_display(fresh, user_id=user_id)
    current_memo = fresh[7] or ""

    merged = split_tags(current_tags) + split_tags(tags_to_add)
    update_song(fresh, join_tags(merged), current_memo, user_id=user_id)


def remove_tags_from_song(row, remove_tokens, user_id=None):
    fresh = get_song_by_id(row[0])
    if not fresh:
        return

    current_tags = split_tags(get_song_tags_for_display(fresh, user_id=user_id))
    current_memo = fresh[7] or ""

    remove_tags = []
    for token in remove_tokens:
        token = token[1:] if token.startswith("-") else token
        tag = resolve_tag_fuzzy(token, user_id=user_id) or resolve_tag(token, user_id=user_id)
        if tag:
            remove_tags.append(tag)

    kept = []
    for song_tag in current_tags:
        remove = False
        for tag in remove_tags:
            if tag_matches(song_tag, tag):
                remove = True
                break
        if not remove:
            kept.append(song_tag)

    update_song(fresh, join_tags(kept), current_memo, user_id=user_id)


def update_song(row, tags, memo, user_id=None):
    started = time.perf_counter()
    if user_id is None:
        raise ValueError("user_id is required when updating song tags")

    sid = row[0]
    con = db()
    now = now_text()

    con.execute(
        "UPDATE songs SET memo=?, updated_at=? WHERE id=?",
        (memo, now, sid)
    )

    try:
        ensure_user_tags_table(con)

        user_id = str(user_id)

        con.execute(
            "DELETE FROM user_tags WHERE user_id=? AND song_id=?",
            (user_id, sid)
        )

        for tag in split_tags(tags):
            tag = canonical_tag_name(tag)
            con.execute("""
            INSERT OR IGNORE INTO user_tags
                (user_id, song_id, tag_name, memo, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """, (user_id, sid, tag, memo or "", now, now))

    except Exception as e:
        with open("/tmp/fine_user_tags.log", "a", encoding="utf-8") as f:
            f.write(f"[user_tags] mirror failed song_id={sid} error={e}\\n")

    con.commit()
    con.close()
    schedule_auto_table_publish(user_id, reason="song_tags_updated")
    logging.info(
        "timing update_song=%.3fs user_id=%s song_id=%s tag_count=%s",
        time.perf_counter() - started,
        user_id,
        sid,
        len(split_tags(tags)),
    )



def sync_removed_tags_to_sheet(row, tags, user_id=None):
    with open("/tmp/fine_sheet_sync.log", "a", encoding="utf-8") as f:
        f.write(f"[sheet_sync] reset/delete called row_id={row[0]} tags={tags}\n")
    try:
        from sheet_sync import sync_song_to_sheet
        for tag in tags:
            ok = sync_song_to_sheet(row, tag_name=tag, enabled=False, user_id=user_id)
            with open("/tmp/fine_sheet_sync.log", "a", encoding="utf-8") as f:
                f.write(f"[sheet_sync] reset/delete tag={tag} ok={ok}\n")
    except Exception as e:
        with open("/tmp/fine_sheet_sync.log", "a", encoding="utf-8") as f:
            f.write(f"[sheet_sync] reset/delete skipped: {e}\n")

def reset_song(row, user_id=None):
    started = time.perf_counter()
    if user_id is None:
        raise ValueError("user_id is required when resetting song tags")

    sid = row[0]
    con = db()
    ensure_user_tags_table(con)
    con.execute(
        "DELETE FROM user_tags WHERE user_id=? AND song_id=?",
        (str(user_id), sid)
    )
    con.execute(
        "UPDATE songs SET memo='', updated_at=? WHERE id=?",
        (now_text(), sid)
    )
    con.commit()
    con.close()
    schedule_auto_table_publish(user_id, reason="song_tags_reset")
    logging.info(
        "timing reset_song=%.3fs user_id=%s song_id=%s",
        time.perf_counter() - started,
        user_id,
        sid,
    )


def resolve_tag_fuzzy(token, user_id=None):
    token = normalize_tag_input(token)

    tags = get_all_tags(user_id=user_id)

    if token in tags:
        return canonical_tag_name(tags[token])

    matches = []

    for short, formal in tags.items():
        if token in normalize_text(short) or token in normalize_text(formal):
            matches.append(canonical_tag_name(formal))

    if token in normalize_text(OLD_LAST_KILL_TAG):
        matches.append(NEW_LAST_KILL_TAG)

    matches = list(dict.fromkeys(matches))

    if len(matches) == 1:
        return canonical_tag_name(matches[0])

    return None




intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.guilds = True

bot = commands.Bot(command_prefix=["!", "！"], intents=intents, help_command=None)
auto_table_publish_tasks = {}
auto_table_publish_lock = asyncio.Lock()


def auto_table_publish_enabled():
    return os.getenv("AUTO_TABLE_PUBLISH_ON_TAG_CHANGE", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def schedule_auto_table_publish(user_id, reason="tag_change"):
    if not auto_table_publish_enabled() or user_id is None:
        return

    user_id = str(user_id)
    old_task = auto_table_publish_tasks.get(user_id)
    if old_task and not old_task.done():
        old_task.cancel()

    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = bot.loop
        task = loop.create_task(auto_publish_user_tables(user_id, reason))
        auto_table_publish_tasks[user_id] = task
        logging.info("auto_table_publish scheduled user_id=%s reason=%s", user_id, reason)
    except Exception:
        logging.exception("auto_table_publish schedule failed user_id=%s reason=%s", user_id, reason)


async def auto_publish_user_tables(user_id, reason="tag_change"):
    total_started = time.perf_counter()
    delay = float(os.getenv("AUTO_TABLE_PUBLISH_DELAY_SECONDS", "8"))
    try:
        await asyncio.sleep(max(0.0, delay))
        table_base_url = os.getenv("TABLE_BASE_URL", "").strip()
        loop = asyncio.get_running_loop()
        logging.info("auto_table_publish start user_id=%s reason=%s", user_id, reason)

        lock_wait_started = time.perf_counter()
        async with auto_table_publish_lock:
            lock_wait_seconds = time.perf_counter() - lock_wait_started
            logging.info(
                "timing auto_publish_lock_wait=%.3fs user_id=%s reason=%s",
                lock_wait_seconds,
                user_id,
                reason,
            )
            from table_generator import generate_user_tables
            from pages_deploy import deploy_user_tables

            result = await loop.run_in_executor(
                None,
                lambda: generate_user_tables(user_id, table_base_url=table_base_url),
            )
            deploy_result = await loop.run_in_executor(
                None,
                lambda: deploy_user_tables(user_id),
            )

        logging.info(
            "auto_table_publish done user_id=%s reason=%s tags=%s deploy=%s commit=%s total_seconds=%.3f",
            user_id,
            reason,
            len(result.get("tags", [])),
            deploy_result.get("message", ""),
            deploy_result.get("commit", ""),
            time.perf_counter() - total_started,
        )
        logging.info(
            "timing auto_publish_total=%.3fs user_id=%s reason=%s",
            time.perf_counter() - total_started,
            user_id,
            reason,
        )
    except asyncio.CancelledError:
        logging.info("auto_table_publish cancelled user_id=%s reason=%s", user_id, reason)
        raise
    except Exception:
        logging.exception("auto_table_publish failed user_id=%s reason=%s", user_id, reason)


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    content = unicodedata.normalize("NFKC", message.content)
    content = content.replace("　", " ")
    content = content.replace("、", " ")
    content = content.replace(",", " ")
    message.content = content

    await bot.process_commands(message)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    if not check_update_loop.is_running():
        check_update_loop.start()



def compact_text(text, limit=40):
    text = str(text or "")
    if len(text) > limit:
        return text[:limit] + "…"
    return text



@bot.command(name="help", aliases=["h", "ヘルプ", "へるぷ"])
async def help_cmd(ctx):
    if ctx.channel.id != CHANNEL_ID:
        return

    def command_block(command, description):
        return f"```text\n{command}\n```\n{description}"

    fields = [
        ("検索", "!s レシュ", "曲名・差分名・レベル・タグ・備考から検索するよ。1件だけならリアクション編集もできるよ。"),
        ("タグ検索", "!ts 日課", "指定タグが付いた曲を検索するよ。例: `!ts sl12 日課`"),
        ("タグ一覧", "!t", "自分が使える固定タグ・追加タグを表示するよ。"),
        ("タグ数", "!tagcount", "自分のタグ件数を表示するよ。固定タグは0件でも出るよ。"),
        ("タグ別レベル", "!tl 日課", "指定タグの曲をレベル別に見るよ。"),
        ("タグ追加", "!addtag ハネリズム hn 🪽", "自分用のタグを追加するよ。3つ目はリアクション絵文字だよ。"),
        ("タグ削除", "!deltag hn", "自分用の追加タグを削除するよ。"),
        ("編集", "!e 1 y ep", "`!s` の検索結果番号を指定してタグや備考を編集するよ。"),
        ("件数", "!count レシュ", "検索条件に合う曲数を数えるよ。"),
        ("md5未取得", "!missingmd5", "自分のタグ付き曲のうち、難易度表に出ないmd5未取得曲を確認するよ。"),
        ("md5未取得 タグ指定", "!missingmd5 日課", "指定タグだけのmd5未取得曲を確認するよ。"),
        ("md5補正登録", "!abmd5 1 0123456789abcdef0123456789abcdef", "`!s` 後の番号、song_id、曲名でmd5補正を登録するよ。"),
        ("md5補正削除", "!abmd5 -1", "登録したmd5補正を削除するよ。例: `!abmd5 -曲名`"),
        ("md5補正 全削除", "!abmd5 --clear-all", "abmd5で登録したmd5補正を一度空にするよ。履歴は残るよ。"),
        ("md5直接登録", "ins md5 0123456789abcdef0123456789abcdef 曲名 [差分名]", "`!s` で1曲に絞った表示名と完全一致した時だけmd5登録するよ。"),
        ("md5最近接登録", "!abmd5 auto", "未取得曲の最近接md5候補を一括登録するよ。候補が取れない曲は見送るよ。"),
        ("難易度表生成", "!maketables", "自分専用のタグ別難易度表を生成して、登録URLを返すよ。"),
        ("難易度表生成 タグ指定", "!maketables 日課", "生成後、指定タグの登録URLだけ返すよ。"),
        ("テーブル一覧", "!tables", "登録済みの取得元テーブルを見るよ。"),
        ("テーブル追加", "!addtable テーブル名", "手動曲追加用のテーブルを作るよ。"),
        ("曲追加", '!addsong テーブル名 lv12 "曲名" "差分名" y 備考', "手動で曲を追加するよ。"),
        ("タグ順", "!tagorder dy 1", "固定タグや短縮名の表示順を変更するよ。"),
        ("テーブル削除", "!deltable テーブル名", "手動追加用テーブルを削除するよ。"),
        ("曲削除", "!delsong 1", "`!s` の検索結果番号を指定して曲を削除するよ。"),
        ("取り込み", "!import", "登録テーブルから曲を取り込むよ。"),
    ]

    embed = discord.Embed(
        title="📖 Fine Bot ヘルプだよ！",
        description="コピペしやすいように、1コマンドずつ分けてあるよ。",
        color=EMBED_BLUE,
    )

    for name, command, description in fields:
        embed.add_field(name=name, value=command_block(command, description), inline=False)

    embed.add_field(
        name="1件検索後の直接入力",
        value=(
            "```text\ny ep dy\n```\n"
            "```text\nadd ni\n```\n"
            "```text\n-dy\n```\n"
            "```text\nreset\n```\n"
            "```text\n/null\n```\n"
            "`!s` の結果が1件だけなら、そのままタグ編集できるよ。"
        ),
        inline=False,
    )

    embed.add_field(
        name="主な短縮タグ",
        value=(
            "```text\ny 横認識\n```\n"
            "```text\n8 横認識(8分系)\n```\n"
            "```text\nyt 横認識(縦系)\n```\n"
            "```text\ntt 縦連\n```\n"
            "```text\ngo ガチ押し系\n```\n"
            "```text\nr 乱打\n```\n"
            "```text\nep 地力上げ\n```\n"
            "```text\nsr ラス殺し\n```\n"
            "```text\ng ゴミ\n```\n"
            "```text\nni 良譜面\n```\n"
            "```text\ndy 日課\n```\n"
            "```text\nsh 惜敗\n```"
        ),
        inline=False,
    )

    embed.set_footer(text="迷ったら !s で検索してから編集してね。")
    await ctx.send(embed=embed)



# FINE_EMBED_UI_BLOCK_V2

EMBED_BLUE = 0x8EC5FF
EMBED_GREEN = 0x9BE28F
EMBED_RED = 0xFF9999


def compact_text(text, limit=40):
    text = str(text or "")
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def normalize_tag_for_count(tag):
    if tag.startswith("惜敗["):
        return "惜敗"
    return tag


def make_song_field(row, index=None, user_id=None):
    if len(row) >= 8:
        sid, source, level, title, chart_name, url, tags, memo = row
    else:
        sid, source, level, title, url, tags, memo = row
        chart_name = ""

    name = compact_text(title, 32)
    field_name = f"{level} {name}"
    if index is not None:
        field_name = f"{index}. {field_name}"

    value = []
    if chart_name:
        value.append(f"差分: {compact_text(chart_name, 36)}")
    value.append(f"🏷️ {display_tags(get_song_tags_for_display(row, user_id=user_id))}")
    value.append(f"💬 {memo if memo else '備考なし'}")

    return field_name, "\n".join(value)


def make_song_list_embed(title, rows, start_index=1, footer_text=None, user_id=None):
    embed = discord.Embed(title=title, color=EMBED_BLUE)

    for i, row in enumerate(rows, start_index):
        field_name, value = make_song_field(row, i, user_id=user_id)
        embed.add_field(name=field_name, value=value, inline=False)

    if footer_text:
        embed.set_footer(text=footer_text)

    return embed


async def send_song_list_embeds(ctx, title, rows, per_page=10, first_message=None):
    total = len(rows)

    for page_start in range(0, total, per_page):
        page_rows = rows[page_start:page_start + per_page]
        start_index = page_start + 1
        end_index = page_start + len(page_rows)

        embed = make_song_list_embed(
            title if page_start == 0 else f"{title} 続き",
            page_rows,
            start_index=start_index,
            footer_text=f"{total}件中 {start_index}-{end_index}件",
            user_id=ctx.author.id,
        )

        if page_start == 0 and first_message is not None:
            try:
                await first_message.edit(content=None, embed=embed)
            except Exception:
                logging.exception("search result message edit failed user_id=%s", ctx.author.id)
                await ctx.send(embed=embed)
        else:
            await ctx.send(embed=embed)



def make_single_song_embed(row, title="✅ 更新したよ！", color=EMBED_GREEN, user_id=None):
    embed = make_single_song_detail_embed(row, title, color, user_id=user_id)
    return embed


async def send_single_song_embed(target, row, title="✅ 更新したよ！", color=EMBED_GREEN, user_id=None):
    if user_id is None:
        user_id = getattr(getattr(target, "author", None), "id", None)
    msg = await target.send(embed=make_single_song_embed(row, title, color, user_id=user_id))
    register_reaction_song_message(msg.id, row[0], user_id)
    await add_all_tag_reactions(msg, user_id=user_id)



def make_tag_count_embed_legacy(title, counter):
    embed = discord.Embed(title=title, color=EMBED_BLUE)

    if not counter:
        embed.description = "まだタグが登録されていないよ。"
        return embed

    items = sorted(
        ((tag, count) for tag, count in counter.items() if count > 0),
        key=lambda item: (-item[1], tag_priority(item[0]), normalize_text(item[0])),
    )
    lines = [f"{tag} ({count})" for tag, count in items]

    for i, chunk in enumerate(split_embed_lines(lines), 1):
        embed.add_field(
            name="📊 集計" if i == 1 else f"📊 集計 続き{i}",
            value="```text\n" + chunk + "\n```",
            inline=False,
        )
    return embed


def split_embed_lines(lines, limit=950):
    chunks = []
    current = ""

    for line in lines:
        add = line if not current else "\n" + line
        if len(current) + len(add) > limit:
            if current:
                chunks.append(current)
            current = line
        else:
            current += add

    if current:
        chunks.append(current)

    return chunks




# FINE_SINGLE_RESULT_EMOJI_UI_V1

TAG_REACTION_EMOJIS = [
    ("↔️", "横認識"),
    ("🎵", "横認識(8分系)"),
    ("↕️", "横認識(縦系)"),
    ("🧱", "縦連"),
    ("👊", "ガチ押し系"),
    ("🔥", "乱打"),
    ("💪", "地力上げ"),
    ("💀", "ラス殺し"),
    ("🗑️", "ゴミ"),
    ("⭐", "良譜面"),
    ("📅", "日課"),
    ("😭", "惜敗"),
]
TAG_REACTION_EMOJIS = [
    (emoji, canonical_tag_name(tag))
    for emoji, tag in TAG_REACTION_EMOJIS
]


def tag_base_name(tag):
    tag = canonical_tag_name(tag)
    if tag.startswith("惜敗["):
        return "惜敗"
    return tag


def reaction_tag_options(user_id=None):
    options = []
    seen_tags = set()

    for emoji, tag in TAG_REACTION_EMOJIS:
        tag = canonical_tag_name(tag)
        options.append((emoji, tag))
        seen_tags.add(tag)

    if user_id is None:
        return options

    custom_index = 0
    used_emojis = {normalize_reaction_emoji(emoji) for emoji, _tag in options}
    for row in get_user_custom_tag_rows(user_id):
        short_name = row["short_name"]
        tag = row["full_name"]
        tag = canonical_tag_name(tag)
        if tag in seen_tags:
            continue
        emoji = row["emoji"].strip()
        normalized_emoji = normalize_reaction_emoji(emoji)
        if not normalized_emoji or normalized_emoji in used_emojis:
            while custom_index < len(CUSTOM_TAG_REACTION_EMOJIS):
                candidate = CUSTOM_TAG_REACTION_EMOJIS[custom_index]
                custom_index += 1
                if normalize_reaction_emoji(candidate) not in used_emojis:
                    emoji = candidate
                    normalized_emoji = normalize_reaction_emoji(candidate)
                    break
            else:
                logging.warning(
                    "custom reaction emoji limit reached user_id=%s short_name=%s tag=%s",
                    user_id,
                    short_name,
                    tag,
                )
                break
        if normalized_emoji in used_emojis:
            logging.warning(
                "custom reaction emoji duplicate skipped user_id=%s short_name=%s tag=%s emoji=%s",
                user_id,
                short_name,
                tag,
                emoji,
            )
            continue
        options.append((emoji, tag))
        used_emojis.add(normalized_emoji)
        seen_tags.add(tag)

    return options


def reaction_emoji_map(user_id=None):
    mapping = {}
    for emoji, tag in reaction_tag_options(user_id=user_id):
        mapping[emoji] = tag
        mapping[normalize_reaction_emoji(emoji)] = tag
    return mapping


def register_reaction_song_message(message_id, song_id, user_id):
    reaction_song_messages[message_id] = {
        "song_id": song_id,
        "owner_user_id": str(user_id) if user_id is not None else None,
        "emoji_to_tag": reaction_emoji_map(user_id=user_id),
    }


def tag_emoji(tag, user_id=None):
    base = tag_base_name(tag)
    for emoji, name in reaction_tag_options(user_id=user_id):
        if name == base:
            return emoji
    return "🏷️"


def emoji_tag_lines_from_tags(tags, user_id=None):
    tag_list = split_tags(tags)

    if not tag_list:
        return "タグなし"

    return "\n".join(
        f"{tag_emoji(tag, user_id=user_id)} {tag}"
        for tag in sort_tags(tag_list)
    )


def reaction_guide_text(user_id=None):
    parts = [f"{emoji} {tag}" for emoji, tag in reaction_tag_options(user_id=user_id)]
    lines = []
    for index in range(0, len(parts), 3):
        lines.append(" / ".join(parts[index:index + 3]))
    return compact_text("\n".join(lines), 1000)


def quick_tag_input_text(user_id=None):
    tags = get_all_tags(user_id=user_id)
    preferred = ["y", "8", "yt", "tt", "go", "r", "ep", "sr", "g", "ni", "dy", "sh"]
    keys = [key for key in preferred if key in tags]
    keys += [key for key in tags.keys() if key not in keys]

    lines = []
    for index in range(0, len(keys), 3):
        parts = [f"`{key}`（{tags[key]}）" for key in keys[index:index + 3]]
        lines.append(" / ".join(parts))

    lines.append("")
    lines.append("`add ep` / `add dy` / `add ni`")
    custom_keys = [key for key in keys if key not in preferred]
    if custom_keys:
        lines.append(" / ".join(f"`add {key}`" for key in custom_keys[:6]))
    lines.append("`-dy` / `reset` / `/null`")
    return compact_text("\n".join(lines), 1000)


def make_single_song_detail_embed(row, title="🎵 楽曲情報", color=0x8EC5FF, user_id=None):
    embed = discord.Embed(
        title=title,
        color=color,
    )

    if len(row) >= 8:
        sid, source, level, song_title, chart_name, url, tags, memo = row
    else:
        sid, source, level, song_title, url, tags, memo = row
        chart_name = ""

    name = song_title
    if chart_name:
        name += f" [{chart_name}]"

    embed.add_field(
        name=f"{level} {name}",
        value="\n".join([
            "🏷️ 現在のタグ",
            emoji_tag_lines_from_tags(get_song_tags_for_display(row, user_id=user_id), user_id=user_id),
            "",
            f"💬 {memo if memo else '備考なし'}",
            "",
        ]),
        inline=False,
    )

    embed.add_field(
        name="🎛️ 対応絵文字",
        value=reaction_guide_text(user_id=user_id),
        inline=False,
    )

    embed.add_field(
        name="✏️ このまま入力できるよ！",
        value=quick_tag_input_text(user_id=user_id),
        inline=False,
    )

    embed.set_footer(text="リアクションで直接編集できるよ！")
    return embed



@bot.command(name="s", aliases=["search", "\u691c\u7d22", "\u3055", "\u3057"])
async def search_cmd(ctx, *args):
    if ctx.channel.id != CHANNEL_ID:
        return

    if not args:
        await ctx.send("\u691c\u7d22\u8a9e\u3092\u5165\u308c\u3066\u306d\uff01\u4f8b: `!s sl1 ceu`")
        return

    progress_message = await ctx.send("\U0001f50e \u691c\u7d22\u4e2d\u3060\u3088\u3001\u3061\u3087\u3063\u3068\u5f85\u3063\u3066\u306d\uff01")

    rows = search_songs(args, user_id=ctx.author.id)
    last_search[ctx.author.id] = rows

    if not rows:
        try:
            await progress_message.edit(content="\u898b\u3064\u304b\u3089\u306a\u304b\u3063\u305f\u307f\u305f\u3044\u3060\u306d\uff01\u691c\u7d22\u8a9e\u3092\u5c11\u3057\u5909\u3048\u3066\u307f\u3066\u306d\u3002")
        except Exception:
            logging.exception("search no-result message edit failed user_id=%s", ctx.author.id)
            await ctx.send("\u898b\u3064\u304b\u3089\u306a\u304b\u3063\u305f\u307f\u305f\u3044\u3060\u306d\uff01\u691c\u7d22\u8a9e\u3092\u5c11\u3057\u5909\u3048\u3066\u307f\u3066\u306d\u3002")
        return

    if len(rows) == 1:
        embed = make_single_song_detail_embed(rows[0], "\U0001f3b5 \u697d\u66f2\u60c5\u5831\u3060\u3088\uff01", user_id=ctx.author.id)
        try:
            await progress_message.edit(content=None, embed=embed)
            msg = progress_message
        except Exception:
            logging.exception("search single-result message edit failed user_id=%s", ctx.author.id)
            msg = await ctx.send(embed=embed)

        register_reaction_song_message(msg.id, rows[0][0], ctx.author.id)
        await add_all_tag_reactions(msg, user_id=ctx.author.id)
        return

    await send_song_list_embeds(ctx, "\U0001f50e \u691c\u7d22\u7d50\u679c\u3060\u3088\uff01", rows, first_message=progress_message)



@bot.command(name="ts", aliases=["タグ検索", "たぐけんさく"])
async def tag_search_cmd(ctx, *args):
    if ctx.channel.id != CHANNEL_ID:
        return

    if not args:
        await ctx.send("使い方ですわ: `!ts sl12 go` または `!ts ガチ`")
        return

    tags, rows = search_by_tag(args, user_id=ctx.author.id)

    if not tags:
        await ctx.send("タグが見つかりませんでしたわ。`!t` で一覧を確認してくださいませ。")
        return

    title = "・".join(tags)

    if not rows:
        await ctx.send(f"{title} の検索結果はありませんでしたわ。")
        return

    last_search[ctx.author.id] = rows

    await send_song_list_embeds(ctx, f"🏷️ {title} の検索結果だよ！", rows)

    if len(rows) >= 20:
        await ctx.send("※20件まで表示しているよ！")




@bot.command(name="t", aliases=["tag", "tags", "タグ", "た"])
async def tag_cmd(ctx):
    if ctx.channel.id != CHANNEL_ID:
        return

    embed = discord.Embed(
        title="🏷️ タグ一覧だよ！",
        color=0x8EC5FF,
    )

    tags = get_all_tags(user_id=ctx.author.id)

    lines = []
    for short, formal in tags.items():
        lines.append(f"・`{short}`　{formal}")

    chunks = []
    current = ""

    for line in lines:
        add = line if not current else "\n" + line
        if len(current) + len(add) > 950:
            chunks.append(current)
            current = line
        else:
            current += add

    if current:
        chunks.append(current)

    for i, chunk in enumerate(chunks, 1):
        embed.add_field(
            name="登録タグ" if i == 1 else f"登録タグ 続き{i}",
            value=chunk,
            inline=False,
        )

    embed.set_footer(text=f"{len(tags)}件")
    await ctx.send(embed=embed)


@bot.command(name="missingmd5", aliases=["md5missing", "md5\u672a\u53d6\u5f97"])
async def missing_md5_cmd(ctx, *args):
    if ctx.channel.id != CHANNEL_ID:
        return

    started = time.perf_counter()
    progress_message = await ctx.send("\U0001f50d md5\u672a\u53d6\u5f97\u66f2\u3092\u691c\u7d22\u4e2d\u3060\u3088\uff01")

    async def edit_missing_md5_message(content):
        try:
            await progress_message.edit(content=content[:1900])
        except Exception:
            logging.exception("missingmd5 progress edit failed user_id=%s", ctx.author.id)
            await ctx.send(content[:1900])

    tag_name = None
    if args:
        token = " ".join(args)
        tag_name = resolve_tag_fuzzy(token, user_id=ctx.author.id) or resolve_tag(token, user_id=ctx.author.id)
        if not tag_name:
            await edit_missing_md5_message("\u30bf\u30b0\u304c\u898b\u3064\u304b\u3089\u306a\u304b\u3063\u305f\u307f\u305f\u3044\u3060\u306d\uff01 `!t` \u3067\u4e00\u89a7\u3092\u78ba\u8a8d\u3057\u3066\u307f\u3066\u306d\u3002")
            return

    try:
        async with ctx.typing():
            from table_generator import missing_md5_records

            rows = missing_md5_records(str(ctx.author.id), tag_name=tag_name)
    except Exception:
        logging.exception("missingmd5 failed user_id=%s tag=%s", ctx.author.id, tag_name or "")
        await edit_missing_md5_message("md5\u672a\u53d6\u5f97\u66f2\u306e\u78ba\u8a8d\u306b\u5931\u6557\u3057\u305f\u307f\u305f\u3044\u3060\u306d\u3002\u30b5\u30fc\u30d0\u30fc\u30ed\u30b0\u3092\u78ba\u8a8d\u3057\u3066\u306d\uff01")
        return

    title = f"md5\u672a\u53d6\u5f97\u66f2: {tag_name}" if tag_name else "md5\u672a\u53d6\u5f97\u66f2"
    elapsed = time.perf_counter() - started

    grouped = {}
    for item in rows:
        song_id = item["song_id"]
        if song_id not in grouped:
            grouped[song_id] = {
                "song_id": song_id,
                "level": item["level"],
                "title": item["title"],
                "chart_name": item["chart_name"],
                "url": item["url"],
                "tags": [],
            }
        tag = item["tag"]
        if tag and tag not in grouped[song_id]["tags"]:
            grouped[song_id]["tags"].append(tag)

    songs = list(grouped.values())
    if not songs:
        await edit_missing_md5_message(f"\u2705 {title}\u306f\u898b\u3064\u304b\u3089\u306a\u304b\u3063\u305f\u3088\uff01\n\u691c\u7d22\u6642\u9593: {elapsed:.1f}\u79d2")
        return

    lines = [
        f"\U0001f50d {title}",
        f"\u4ef6\u6570: {len(songs)}\u66f2",
        f"\u691c\u7d22\u6642\u9593: {elapsed:.1f}\u79d2",
        "",
    ]

    for item in songs[:15]:
        name = item["title"]
        if item["chart_name"]:
            name = f"{name} [{item['chart_name']}]"
        lines.append(f"{item['song_id']}: {item['level']} {compact_text(name, 70)}")
        lines.append(f"tags: {', '.join(item['tags'])}")
        if item["url"]:
            lines.append(f"url: {compact_text(item['url'], 120)}")
        lines.append("")

    if len(songs) > 15:
        lines.append(f"...\u307b\u304b {len(songs) - 15} \u66f2\u3042\u308b\u3088\uff01")

    await edit_missing_md5_message("\n".join(lines))


@bot.command(name="abmd5", aliases=["md5override"])
async def abmd5_cmd(ctx, *args):
    if ctx.channel.id != CHANNEL_ID:
        return

    if not args:
        await ctx.send(
            "使い方だよ！\n"
            "```text\n!abmd5 1 0123456789abcdef0123456789abcdef\n```\n"
            "```text\n!abmd5 -1\n```\n"
            "```text\n!abmd5 --clear-all\n```\n"
            "```text\n!abmd5 auto\n```"
        )
        return

    delete_mode = False
    args = list(args)
    if args[0] in {"auto", "guess", "infer"}:
        tag_name = None
        if len(args) > 1:
            tag_token = " ".join(args[1:]).strip()
            tag_name = resolve_tag_fuzzy(tag_token, user_id=ctx.author.id) or resolve_tag(tag_token, user_id=ctx.author.id)
            if not tag_name:
                await ctx.send(f"⚠️ `{tag_token}` はタグに見つからなかったみたいだよ！")
                return

        started = time.perf_counter()
        progress = await ctx.send(
            "🔎 md5候補を推定しているよ！\n"
            "最近接の候補が取れた曲はまとめて登録するよ。少し待ってね！"
        )
        try:
            await progress.edit(
                content=(
                    "🔎 md5候補を推定しているよ！\n"
                    "[1/3] md5未取得曲を確認中..."
                )
            )
            await progress.edit(
                content=(
                    "🔎 md5候補を推定しているよ！\n"
                    "[2/3] songdata.db と照合中..."
                )
            )
            loop = asyncio.get_running_loop()
            candidates, skipped, total = await loop.run_in_executor(
                None,
                lambda: auto_abmd5_candidates_for_user(ctx.author.id, tag_name=tag_name),
            )
            await progress.edit(
                content=(
                    "🔎 md5候補を登録しているよ！\n"
                    "[3/3] md5_overridesへ反映中..."
                )
            )
            if candidates:
                schedule_auto_table_publish(ctx.author.id, reason="auto_abmd5_updated")
        except Exception as e:
            logging.exception("abmd5 auto failed user_id=%s tag=%s", ctx.author.id, tag_name or "")
            await progress.edit(content=f"❌ md5推定に失敗したみたいだよ。\n{concise_error(e)}")
            return

        lines = [
            "✅ md5候補を一括登録したよ！",
            f"対象: {total}曲",
            f"登録: {len(candidates)}曲",
            f"見送り: {len(skipped)}曲",
            f"処理時間: {time.perf_counter() - started:.1f}秒",
            "※最近接候補が取れた曲は `md5_overrides` に登録済みだよ。",
            "",
        ]
        if candidates:
            lines.append("登録した曲:")
        for item in candidates[:10]:
            lines.append(f"{item['song_id']}: {item['level']} {compact_text(item['title'], 45)}")
            matched_name = item["songdata_title"]
            if item["songdata_subtitle"]:
                matched_name = f"{matched_name} {item['songdata_subtitle']}"
            lines.append(f"根拠: score={item['score']} reason={item['reason']}")
            lines.append(f"照合先: {compact_text(matched_name, 55)}")
            lines.append(f"md5: {item['md5']}")
            lines.append("source: auto_abmd5")
        if len(candidates) > 10:
            lines.append(f"...ほか {len(candidates) - 10} 曲も登録したよ！")
        if skipped:
            lines.append("")
            lines.append("見送り例（最近接候補なし/処理対象外）:")
            for item in skipped[:5]:
                lines.append(f"{item['song_id']}: {compact_text(item['title'], 35)}")
                reason_text = item.get("reason", "")
                detail_text = item.get("detail", "")
                score_text = item.get("score", 0)
                if detail_text:
                    lines.append(f"見送り理由: {reason_text} ({detail_text}) score={score_text}")
                else:
                    lines.append(f"見送り理由: {reason_text} score={score_text}")
                matched_name = item.get("songdata_title", "")
                if item.get("songdata_subtitle"):
                    matched_name = f"{matched_name} {item['songdata_subtitle']}"
                if matched_name:
                    lines.append(f"最接近: {compact_text(matched_name, 45)}")
                    if item.get("md5"):
                        lines.append(f"md5: {item['md5']}")

        await progress.edit(content="\n".join(lines)[:1900])
        return

    if args[0] in {"--clear-all", "clear-all", "all-clear"}:
        count = clear_all_md5_overrides(ctx.author.id)
        await ctx.send(f"✅ md5補正を全削除したよ！\n```text\n削除数: {count}\n```")
        return

    if args[0].startswith("-"):
        delete_mode = True
        if args[0] == "-":
            target = " ".join(args[1:]).strip()
        else:
            target = args[0][1:]
            if len(args) > 1:
                target = " ".join([target] + args[1:]).strip()
    else:
        if len(args) < 2:
            await ctx.send("md5を入れてね。例: `!abmd5 1 0123456789abcdef0123456789abcdef`")
            return
        md5 = args[-1].strip().lower()
        target = " ".join(args[:-1]).strip()
        if not valid_md5(md5):
            await ctx.send("md5は32文字の16進数で入れてね。")
            return

    row, error = resolve_song_for_abmd5(ctx, target)
    if not row:
        await ctx.send(error[:1900])
        return

    if delete_mode:
        deleted = delete_md5_override(row[0], ctx.author.id)
        if deleted:
            await ctx.send(f"✅ md5補正を削除したよ！\n```text\n{row[0]}: {row[2]} {song_display_name(row)}\n```")
        else:
            await ctx.send(f"⚠️ この曲のmd5補正はまだ登録されていなかったみたいだよ。\n```text\n{row[0]}: {row[2]} {song_display_name(row)}\n```")
        return

    set_md5_override(row, md5, ctx.author.id)
    await ctx.send(
        "✅ md5補正を登録したよ！\n"
        f"```text\n{row[0]}: {row[2]} {song_display_name(row)}\nmd5: {md5}\n```"
    )


@bot.command(name="import", aliases=["取り込み", "更新"])
async def import_cmd(ctx):
    if ctx.channel.id != CHANNEL_ID:
        return

    async with ctx.typing():
        added, errors = discover_and_import()

    msg = [f"取り込み完了ですわ。追加 {len(added)} 件ですわ。"]
    if errors:
        msg.append("")
        msg.append("エラーですわ。")
        msg += errors

    await ctx.send("\n".join(msg)[:1900])




@bot.command(name="addtag", aliases=["tagadd","タグ追加","たぐついか"])
async def addtag(ctx, full_name=None, short_name=None, emoji=None):

    if ctx.channel.id != CHANNEL_ID:
        return

    if not full_name or not short_name:
        await ctx.send(
            "使い方だよ: `!addtag ハネリズム hn 🪽`"
        )
        return

    ok, message = add_custom_tag(full_name, short_name, user_id=ctx.author.id, emoji=emoji or "")
    if not ok:
        await ctx.send(f"⚠️ {message}")
        return

    emoji_line = f"\n{emoji}" if emoji else ""
    await ctx.send(
        f"✅ タグ追加したよ！\n・{full_name}\n{short_name}{emoji_line}"
    )


@bot.command(name="deltag", aliases=["tagdel","タグ削除","たぐさくじょ"])
async def deltag(ctx, short_name=None):

    if ctx.channel.id != CHANNEL_ID:
        return

    if not short_name:
        await ctx.send(
            "使い方ですわ: !deltag go"
        )
        return

    deleted = delete_custom_tag(short_name, user_id=ctx.author.id)
    if not deleted:
        await ctx.send(f"⚠️ `{short_name}` は追加タグに見つからなかったみたいだよ！")
        return

    await ctx.send(
        f"🗑️ タグ削除ですわ\n{short_name}"
    )




def compact_song_display_name(row, limit=20):
    name = song_display_name(row)
    if len(name) > limit:
        return name[:limit] + "…"
    return name


def display_width(text):
    import unicodedata
    width = 0
    for ch in str(text):
        width += 2 if unicodedata.east_asian_width(ch) in ("F", "W", "A") else 1
    return width


def pad_display(text, width):
    pad = max(1, width - display_width(text))
    return text + (" " * pad)


def normalize_tag_for_count(tag):
    if tag.startswith("惜敗["):
        return "惜敗"
    return tag


def tag_count_lines(rows, user_id=None):
    from collections import Counter

    counter = Counter()
    for row in rows:
        for tag in split_tags(get_song_tags_for_display(row, user_id=user_id)):
            counter[normalize_tag_for_count(tag)] += 1

    if not counter:
        return []

    ordered = sort_tags(list(counter.keys()))
    return [f"{tag}　{counter[tag]}" for tag in ordered]



def compact_song_title(row, limit=20):
    title = row[3] or ""
    chart_name = row[4] if len(row) >= 8 else ""

    if len(title) > limit:
        title = title[:limit] + "…"

    if chart_name:
        return f"{title} [{chart_name}]"

    return title


def normalize_tag_for_count(tag):
    if tag.startswith("惜敗["):
        return "惜敗"
    return tag



@bot.command(name="tl", aliases=["taglevel", "タグレベル"])
async def tag_level_cmd(ctx, *args):
    if ctx.channel.id != CHANNEL_ID:
        return

    if not args:
        await ctx.send("使い方だよ: `!tl y`")
        return

    wanted_tags = []
    for token in args:
        tag = resolve_tag_fuzzy(token, user_id=ctx.author.id) or resolve_tag(token, user_id=ctx.author.id)
        if tag:
            wanted_tags.append(tag)

    wanted_tags = list(dict.fromkeys(wanted_tags))

    if not wanted_tags:
        await ctx.send("タグが見つからなかったよ。`!t` で確認してみてね。")
        return

    con = db()
    rows = con.execute(
        "SELECT id, source, level, title, chart_name, url, tags, memo FROM songs"
    ).fetchall()
    con.close()

    matched = []
    for row in rows:
        song_tags = split_tags(get_song_tags_for_display(row, user_id=ctx.author.id))
        if all(any(tag_matches(song_tag, tag) for song_tag in song_tags) for tag in wanted_tags):
            matched.append(row)

    if not matched:
        await ctx.send("そのタグの曲は見つからなかったよ。")
        return

    def level_key(level):
        n = normalize_text(level)

        for prefix, order in [("sl", 0), ("st", 1), ("lv", 2)]:
            if n.startswith(prefix):
                try:
                    return (order, int(n[len(prefix):]))
                except Exception:
                    return (order, 999)

        return (9, 999, n)

    levels = sorted(set(row[2] for row in matched), key=level_key)
    title = " | ".join(wanted_tags)

    await ctx.send(f"🏷️ {title} のレベル別リストだよ！")

    from collections import Counter

    for level in levels:
        level_rows = [row for row in matched if row[2] == level]
        level_rows.sort(key=lambda row: normalize_text(song_display_name(row)))

        embed = discord.Embed(title=f"📘 {level}", color=EMBED_BLUE)

        song_lines = []
        for row in level_rows:
            song_lines.append(f"・**{compact_text(song_display_name(row), 42)}**")
            song_lines.append(f"　🏷️ {display_tags(get_song_tags_for_display(row, user_id=ctx.author.id))}")
            if row[7]:
                song_lines.append(f"　💬 {compact_text(row[7], 80)}")
            song_lines.append("")

        for i, chunk in enumerate(split_embed_lines(song_lines), 1):
            embed.add_field(
                name="曲リスト" if i == 1 else f"曲リスト 続き{i}",
                value=chunk,
                inline=False,
            )

        counter = Counter()
        for row in level_rows:
            for tag in split_tags(get_song_tags_for_display(row, user_id=ctx.author.id)):
                counter[normalize_tag_for_count(tag)] += 1

        count_lines = [f"{tag}　{counter[tag]}" for tag in sort_tags(list(counter.keys()))]
        embed.add_field(
            name="📊 タグ集計",
            value="```text\n" + "\n".join(count_lines) + "\n```",
            inline=False,
        )

        embed.set_footer(text=f"{len(level_rows)}件")
        await ctx.send(embed=embed)



@bot.command(name="tagcount", aliases=["タグ数", "タグ統計"])
async def tagcount_cmd(ctx):
    if ctx.channel.id != CHANNEL_ID:
        return

    from collections import Counter

    con = db()
    ensure_user_tags_table(con)
    rows = con.execute(
        """
        SELECT tag_name
        FROM user_tags
        WHERE user_id=?
        """,
        (str(ctx.author.id),),
    ).fetchall()
    con.close()

    counter = Counter()
    for (tag,) in rows:
        counter[normalize_tag_for_count(tag)] += 1

    await ctx.send(embed=make_tag_count_embed("🏷️ タグ統計だよ！", counter))


@bot.command(name="count", aliases=["数", "件数", "か"])
async def count_cmd(ctx):
    if ctx.channel.id != CHANNEL_ID:
        return

    con = db()
    total = con.execute("SELECT COUNT(*) FROM songs").fetchone()[0]
    st = con.execute("SELECT COUNT(*) FROM songs WHERE source='st'").fetchone()[0]
    sl = con.execute("SELECT COUNT(*) FROM songs WHERE source='sl'").fetchone()[0]
    con.close()

    await ctx.send(f"登録数ですわ\nST: {st}\nSL: {sl}\n合計: {total}")



@bot.command(name="e", aliases=["え", "edit", "編集"])
async def edit_cmd(ctx, index: int = None, *args):
    if ctx.channel.id != CHANNEL_ID:
        return

    if index is None:
        await ctx.send("使い方ですわ: `!e 1 y ni 備考` または `!え 1 reset`")
        return

    rows = last_search.get(ctx.author.id, [])

    if index < 1 or index > len(rows):
        await ctx.send("その番号は直前の検索結果にありませんわ。")
        return

    row = rows[index - 1]
    body = " ".join(args).strip()

    if not body:
        fresh_row = get_song_by_id(row[0]) or row
        last_search[ctx.author.id] = [fresh_row]
        await send_single_song_embed(ctx, fresh_row, "🎵 楽曲情報だよ！", user_id=ctx.author.id)
        return

    if normalize_text(body) == "reset":
        fresh_before = get_song_by_id(row[0]) or row
        sync_removed_tags_to_sheet(
            fresh_before,
            split_tags(get_song_tags_for_display(fresh_before, user_id=ctx.author.id)),
            user_id=ctx.author.id,
        )
        reset_song(row, user_id=ctx.author.id)
        fresh_row = get_song_by_id(row[0]) or row
        await send_single_song_embed(ctx, fresh_row, "🗑️ 初期化したよ！", user_id=ctx.author.id)
        return

    parts = body.split()

    add_mode = False
    if parts and normalize_tag_input(parts[0]) == "add":
        add_mode = True
        parts = parts[1:]

    remove_mode = any(part.startswith("-") for part in parts)

    if remove_mode:
        remove_tags_from_song(row, [part for part in parts if part.startswith("-")], user_id=ctx.author.id)
    else:
        tags, memo = parse_edit_args(parts, user_id=ctx.author.id)

        if add_mode:
            append_tags_to_song(row, tags, user_id=ctx.author.id)
        else:
            update_song(row, tags, memo, user_id=ctx.author.id)

    fresh_row = get_song_by_id(row[0])
    if fresh_row:
        row = fresh_row
        last_search[ctx.author.id] = [fresh_row]

    await send_single_song_embed(ctx, row, "✅ 更新したよ！", user_id=ctx.author.id)


@bot.event
async def on_message_edit(before, after):
    pass


@bot.listen("on_message")
async def edit_after_search(message):
    if message.author.bot:
        return
    if message.channel.id != CHANNEL_ID:
        return

    text = unicodedata.normalize("NFKC", message.content).strip()
    m = re.match(r"^(\d+)e(?:\s+(.+))?$", text, re.I)
    if not m:
        return

    index = int(m.group(1))
    body = (m.group(2) or "").strip()

    rows = last_search.get(message.author.id, [])

    if index < 1 or index > len(rows):
        await message.channel.send("その番号は直前の検索結果にありませんわ。")
        return

    row = rows[index - 1]

    if normalize_text(body) == "reset":
        fresh_before = get_song_by_id(row[0]) or row
        sync_removed_tags_to_sheet(
            fresh_before,
            split_tags(get_song_tags_for_display(fresh_before, user_id=message.author.id)),
            user_id=message.author.id,
        )
        reset_song(row, user_id=message.author.id)
        fresh_row = get_song_by_id(row[0]) or row
        await send_single_song_embed(message.channel, fresh_row, "🗑️ 初期化したよ！", user_id=message.author.id)
        return

    if not body:
        await message.channel.send("タグか備考を入力してくださいませ。例: `1e y r 良い`")
        return

    parts = body.split()

    if any(part.startswith("-") for part in parts):
        remove_tags_from_song(row, [part for part in parts if part.startswith("-")], user_id=message.author.id)
        fresh_row = get_song_by_id(row[0])
        if fresh_row:
            row = fresh_row
            last_search[message.author.id] = [fresh_row]

        await send_single_song_embed(message.channel, row, "✅ 更新したよ！", user_id=message.author.id)
        return

    add_mode = False
    if parts and normalize_tag_input(parts[0]) == "add":
        add_mode = True
        parts = parts[1:]

    tags, memo = parse_edit_args(parts, user_id=message.author.id)

    if add_mode:
        append_tags_to_song(row, tags, user_id=message.author.id)
    else:
        update_song(row, tags, memo, user_id=message.author.id)

    fresh_row = get_song_by_id(row[0])
    if fresh_row:
        row = fresh_row
        last_search[message.author.id] = [fresh_row]

    await send_single_song_embed(message.channel, row, "✅ 更新したよ！", user_id=message.author.id)


@bot.listen("on_message")
async def insert_md5_after_search(message):
    if message.author.bot:
        return
    if message.channel.id != CHANNEL_ID:
        return

    text = unicodedata.normalize("NFKC", message.content).strip()
    if not text.lower().startswith("ins md5 "):
        return

    rows = last_search.get(message.author.id, [])
    if len(rows) != 1:
        await message.channel.send("`ins md5` は `!s` で1曲だけに絞ってから使ってね。")
        return

    md5_match = re.search(r"\b[0-9a-fA-F]{32}\b", text)
    if not md5_match:
        await message.channel.send("32文字のmd5が見つからなかったよ。")
        return

    md5 = md5_match.group(0).lower()
    name_text = (text[:md5_match.start()] + text[md5_match.end():]).strip()
    name_text = re.sub(r"^ins\s+md5\s+", "", name_text, flags=re.I).strip()
    row = rows[0]
    display_name = song_display_name(row)
    if name_text != display_name:
        await message.channel.send(
            "曲名が直前の検索結果と完全一致しなかったよ。\n"
            f"```text\n入力: {name_text}\n期待: {display_name}\n```"
        )
        return

    set_md5_override(row, md5, message.author.id)
    await message.channel.send(
        "✅ md5を登録したよ！\n"
        f"```text\n{row[0]}: {row[2]} {display_name}\nmd5: {md5}\n```"
    )


@bot.listen("on_message")
async def quick_edit_single_search(message):
    if message.author.bot:
        return
    if message.channel.id != CHANNEL_ID:
        return

    text = unicodedata.normalize("NFKC", message.content).strip()
    text = text.replace("　", " ")
    text = text.replace("、", " ")
    text = text.replace(",", " ")

    if not text:
        return
    if text.startswith("!") or text.startswith("！"):
        return
    if re.match(r"^\d+e(?:\s+.*)?$", text, re.I):
        return

    rows = last_search.get(message.author.id, [])
    if len(rows) != 1:
        return

    parts = text.split()
    row = rows[0]

    if normalize_text(text) == "reset":
        fresh_before = get_song_by_id(row[0]) or row
        sync_removed_tags_to_sheet(
            fresh_before,
            split_tags(get_song_tags_for_display(fresh_before, user_id=message.author.id)),
            user_id=message.author.id,
        )
        reset_song(row, user_id=message.author.id)
        fresh = get_song_by_id(row[0]) or row
        last_search[message.author.id] = [fresh]
        await send_single_song_embed(message.channel, fresh, "🗑️ 初期化したよ！", user_id=message.author.id)
        return

    # 削除モード: -sh / -惜敗 / -ni / -良譜面
    if any(part.startswith("-") for part in parts):
        fresh = get_song_by_id(row[0]) or row
        current_tags = split_tags(get_song_tags_for_display(fresh, user_id=message.author.id))
        current_memo = fresh[7] or ""

        remove_tags = []
        for part in parts:
            if not part.startswith("-"):
                continue
            token = part[1:]
            tag = resolve_tag_fuzzy(token, user_id=message.author.id) or resolve_tag(token, user_id=message.author.id)
            if tag:
                remove_tags.append(tag)

        kept = []
        for song_tag in current_tags:
            should_remove = False
            for tag in remove_tags:
                if tag_matches(song_tag, tag):
                    should_remove = True
                    break
            if not should_remove:
                kept.append(song_tag)

        update_song(fresh, join_tags(kept), current_memo, user_id=message.author.id)
        fresh = get_song_by_id(row[0]) or fresh
        last_search[message.author.id] = [fresh]

        await send_single_song_embed(message.channel, fresh, "✅ 更新したよ！", user_id=message.author.id)
        return

    # 通常編集として扱うか判定
    check_parts = parts[:]
    if check_parts and normalize_tag_input(check_parts[0]) == "add":
        check_parts = check_parts[1:]

    has_tag_or_null = False
    for part in check_parts:
        if normalize_text(part) == "/null" or resolve_tag(part, user_id=message.author.id):
            has_tag_or_null = True
            break

    if not has_tag_or_null:
        return

    add_mode = False
    if parts and normalize_tag_input(parts[0]) == "add":
        add_mode = True
        parts = parts[1:]

    tags, memo = parse_edit_args(parts, user_id=message.author.id)

    if add_mode:
        fresh = get_song_by_id(row[0]) or row
        merged = split_tags(get_song_tags_for_display(fresh, user_id=message.author.id)) + split_tags(tags)
        update_song(fresh, join_tags(merged), fresh[7] or "", user_id=message.author.id)
    else:
        update_song(row, tags, memo, user_id=message.author.id)

    fresh = get_song_by_id(row[0]) or row
    last_search[message.author.id] = [fresh]

    await send_single_song_embed(message.channel, fresh, "✅ 更新したよ！", user_id=message.author.id)


@tasks.loop(hours=24)
async def check_update_loop():
    now = datetime.now(JST)

    # 2か月に1回、1日の0時に確認
    if not (now.day == 1 and now.hour == 0 and now.month in [1, 3, 5, 7, 9, 11]):
        return

    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        return

    added, errors = discover_and_import()

    if added:
        lines = [
            "📢 Stella/Satelliteに追加分がありましたわ。",
            f"追加 {len(added)} 件ですわ。",
            "",
        ]
        lines += added[:20]
        if len(added) > 20:
            lines.append(f"...ほか {len(added)-20} 件")
        await channel.send("\n".join(lines)[:1900])
    elif errors:
        await channel.send("更新確認でエラーが出ましたわ。\n" + "\n".join(errors)[:1500])



def init_db():
    con = sqlite3.connect(DB)

    con.execute("""
    CREATE TABLE IF NOT EXISTS songs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT NOT NULL,
        level TEXT NOT NULL,
        title TEXT NOT NULL,
        url TEXT,
        tags TEXT,
        memo TEXT,
        created_at TEXT,
        updated_at TEXT,
        UNIQUE(source, level, title)
    )
    """)

    con.execute("""
    CREATE TABLE IF NOT EXISTS meta(
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)

    con.execute("""
    CREATE TABLE IF NOT EXISTS custom_tags(
        short_name TEXT PRIMARY KEY,
        full_name TEXT NOT NULL
    )
    """)

    ensure_user_tags_table(con)
    ensure_user_custom_tags_table(con)

    con.commit()
    con.close()


# FINE_MANUAL_TABLE_BLOCK_V1

import shlex


def migrate_manual_tables():
    con = db()

    con.execute("""
    CREATE TABLE IF NOT EXISTS custom_tables(
        name TEXT PRIMARY KEY,
        created_at TEXT
    )
    """)

    cols = [r[1] for r in con.execute("PRAGMA table_info(songs)").fetchall()]

    if "chart_name" not in cols:
        con.execute("ALTER TABLE songs ADD COLUMN chart_name TEXT")

    con.execute(
        "INSERT OR IGNORE INTO custom_tables(name, created_at) VALUES(?, ?)",
        ("st", now_text())
    )
    con.execute(
        "INSERT OR IGNORE INTO custom_tables(name, created_at) VALUES(?, ?)",
        ("sl", now_text())
    )

    con.commit()
    con.close()


_old_init_db_for_manual_tables = init_db


def init_db():
    _old_init_db_for_manual_tables()
    migrate_manual_tables()


def get_tables():
    con = db()
    rows = con.execute(
        "SELECT name FROM custom_tables ORDER BY name"
    ).fetchall()
    con.close()
    return [r[0] for r in rows]


def add_table(name):
    name = name.strip()
    con = db()
    con.execute(
        "INSERT OR IGNORE INTO custom_tables(name, created_at) VALUES(?, ?)",
        (name, now_text())
    )
    con.commit()
    con.close()


def table_exists(name):
    return normalize_text(name) in [normalize_text(x) for x in get_tables()]


def resolve_table_name(name):
    n = normalize_text(name)
    for table in get_tables():
        if normalize_text(table) == n:
            return table
    return None


def parse_tag_list(text, user_id=None):
    if not text or normalize_text(text) == "/null":
        return ""

    parts = []
    for x in text.replace("、", " ").replace(",", " ").replace("|", " ").split():
        tag = resolve_tag(x, user_id=user_id)
        if tag:
            parts.append(tag)

    return join_tags(parts)


def parse_song_line(line, user_id=None):
    parts = shlex.split(line)

    if len(parts) < 5:
        return None, "形式が足りませんわ。例: `lv12 \"曲名\" \"差分名\" go 備考`"

    level = parts[0]
    title = parts[1]
    chart_name = parts[2]
    tag_text = parts[3]
    memo = " ".join(parts[4:]).strip()

    if normalize_text(title) == "/null":
        title = ""

    if normalize_text(chart_name) == "/null":
        chart_name = ""

    if normalize_text(memo) == "/null":
        memo = ""

    tags = parse_tag_list(tag_text, user_id=user_id)

    if len(memo) > 150:
        memo = memo[:150]

    if not level or not title:
        return None, "難易度と曲名は必須ですわ。"

    return {
        "level": level,
        "title": title,
        "chart_name": chart_name,
        "tags": tags,
        "memo": memo,
    }, None


def insert_manual_song(table_name, data, user_id=None):
    if user_id is None:
        raise ValueError("user_id is required when inserting manual song tags")

    con = db()
    now = now_text()

    cur = con.execute(
        """
        INSERT INTO songs(source, level, title, chart_name, url, tags, memo, created_at, updated_at)
        VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            table_name,
            data["level"],
            data["title"],
            data["chart_name"],
            "",
            "",
            data["memo"],
            now,
            now,
        )
    )
    song_id = cur.lastrowid

    ensure_user_tags_table(con)
    for tag in split_tags(data["tags"]):
        tag = canonical_tag_name(tag)
        con.execute("""
        INSERT OR IGNORE INTO user_tags
            (user_id, song_id, tag_name, memo, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """, (str(user_id), song_id, tag, data["memo"] or "", now, now))

    con.commit()
    con.close()


def search_songs(words, user_id=None):
    started = time.perf_counter()
    con = db()

    rows = con.execute(
        "SELECT id, source, level, title, chart_name, url, tags, memo FROM songs"
    ).fetchall()

    con.close()

    norm_words = [normalize_text(w) for w in words if w.strip()]
    result = []
    user_tags_by_song = get_user_song_tags_map(user_id)

    for row in rows:
        sid, source, level, title, chart_name, url, tags, memo = row
        user_tags = join_tags(user_tags_by_song.get(sid, []))

        hay = normalize_text(" ".join([
            source or "",
            level or "",
            title or "",
            chart_name or "",
            url or "",
            user_tags,
            memo or "",
        ]))

        if all(w in hay for w in norm_words):
            result.append(row)
            if len(result) >= 20:
                break

    logging.info(
        "search_songs user_id=%s words=%s rows=%s results=%s seconds=%.3f",
        user_id,
        list(words),
        len(rows),
        len(result),
        time.perf_counter() - started,
    )
    return result


def search_by_tag(args, user_id=None):
    started = time.perf_counter()
    level_filter = None
    tag_tokens = []

    for arg in args:
        n = normalize_text(arg)

        if n.startswith("sl") or n.startswith("st") or n.startswith("lv"):
            level_filter = n
        else:
            tag_tokens.append(arg)

    resolved_tags = []

    for token in tag_tokens:
        tag = resolve_tag_fuzzy(token, user_id=user_id) or resolve_tag(token, user_id=user_id)
        if tag:
            resolved_tags.append(tag)

    resolved_tags = list(dict.fromkeys(resolved_tags))

    if not resolved_tags:
        return [], []

    con = db()

    rows = con.execute(
        "SELECT id, source, level, title, chart_name, url, tags, memo FROM songs"
    ).fetchall()

    con.close()

    results = []
    user_tags_by_song = get_user_song_tags_map(user_id)

    for row in rows:
        sid, source, level, title, chart_name, url, tags, memo = row

        if level_filter and normalize_text(level) != level_filter:
            continue

        song_tags = user_tags_by_song.get(sid, [])

        if all(any(tag_matches(song_tag, tag) for song_tag in song_tags) for tag in resolved_tags):
            results.append(row)
            if len(results) >= 20:
                break

    logging.info(
        "search_by_tag user_id=%s args=%s rows=%s results=%s seconds=%.3f",
        user_id,
        list(args),
        len(rows),
        len(results),
        time.perf_counter() - started,
    )
    return resolved_tags, results


def format_song(row, index, user_id=None):
    if len(row) >= 8:
        sid, source, level, title, chart_name, url, tags, memo = row
    else:
        sid, source, level, title, url, tags, memo = row
        chart_name = ""

    name = title
    if chart_name:
        name += f" [{chart_name}]"

    tag_text = display_tags(get_song_tags_for_display(row, user_id=user_id))
    memo_text = memo if memo else "備考なし"

    return chr(10).join([
        f"{index}. {level} {name}",
        f"🏷️ {tag_text}",
        f"💬 {memo_text}",
    ])



@bot.command(name="tables", aliases=["tablelist", "テーブル一覧", "てーぶる"])
async def tables_cmd(ctx):
    if ctx.channel.id != CHANNEL_ID:
        return

    rows = get_tables()

    embed = discord.Embed(
        title="📚 テーブル一覧だよ！",
        color=0x8EC5FF,
    )

    if not rows:
        embed.description = "まだテーブルがありませんわ。"
    else:
        lines = []
        for i, name in enumerate(rows, 1):
            lines.append(f"{i}. {name}")

        embed.description = "\n".join(
            f"{i}. {name}"
            for i, name in enumerate(rows, 1)
        )

        embed.set_footer(text=f"{len(rows)}件")

    await ctx.send(embed=embed)


@bot.command(name="addtable", aliases=["tableadd", "テーブル追加", "てーぶるついか"])
async def tableadd_cmd(ctx, *, name=None):
    if ctx.channel.id != CHANNEL_ID:
        return

    if not name:
        await ctx.send("① テーブル名を入力してくださいませ。")

        def check(message):
            return message.author == ctx.author and message.channel == ctx.channel

        try:
            message = await bot.wait_for("message", timeout=120, check=check)
            name = message.content.strip()
        except Exception:
            await ctx.send("時間切れですわ。もう一度 `!tableadd テーブル名` で試してくださいませ。")
            return

    name = name.strip()

    if not name:
        await ctx.send("テーブル名が空ですわ。")
        return

    add_table(name)

    await ctx.send(f"✅ テーブルを追加しましたわ\n・{name}")


@bot.command(name="addsong", aliases=["songadd", "曲追加", "曲登録"])
async def addsong_cmd(ctx, *, body=None):
    if ctx.channel.id != CHANNEL_ID:
        return

    if body:
        parts = shlex.split(body)
        if len(parts) < 6:
            await ctx.send(
                "使い方ですわ: `!addsong テーブル lv12 \"曲名\" \"差分名\" go 備考`\n"
                "不要な項目は `/null` ですわ。"
            )
            return

        table_input = parts[0]
        table_name = resolve_table_name(table_input)

        if not table_name:
            await ctx.send("そのテーブルはありませんわ。先に `!tableadd テーブル名` してくださいませ。")
            return

        data, error = parse_song_line(" ".join(shlex.quote(x) for x in parts[1:]), user_id=ctx.author.id)

        if error:
            await ctx.send(error)
            return

        insert_manual_song(table_name, data, user_id=ctx.author.id)

        await ctx.send(
            f"✅ 曲を追加しましたわ\n"
            f"テーブル: {table_name}\n"
            f"難易度: {data['level']}\n"
            f"曲名: {data['title']}\n"
            f"差分名: {data['chart_name'] or 'なし'}\n"
            f"タグ: {data['tags'] or 'なし'}\n"
            f"備考: {data['memo'] or 'なし'}"
        )
        return

    await ctx.send("① 保存テーブルを選んでくださいませ。\n`!tables` で一覧を見られますわ。")

    def check(message):
        return message.author == ctx.author and message.channel == ctx.channel

    try:
        table_message = await bot.wait_for("message", timeout=120, check=check)
    except Exception:
        await ctx.send("時間切れですわ。")
        return

    table_name = resolve_table_name(table_message.content.strip())

    if not table_name:
        await ctx.send("そのテーブルはありませんわ。先に `!tableadd テーブル名` してくださいませ。")
        return

    await ctx.send(
        "② 曲のデータを入力してくださいませ。\n"
        "`lv○○ \"曲名\" \"差分名\" tag 備考`\n"
        "不要な項目は `/null` ですわ。"
    )

    try:
        data_message = await bot.wait_for("message", timeout=180, check=check)
    except Exception:
        await ctx.send("時間切れですわ。")
        return

    data, error = parse_song_line(data_message.content.strip(), user_id=ctx.author.id)

    if error:
        await ctx.send(error)
        return

    insert_manual_song(table_name, data, user_id=ctx.author.id)

    await ctx.send(
        f"✅ 曲を追加しましたわ\n"
        f"テーブル: {table_name}\n"
        f"難易度: {data['level']}\n"
        f"曲名: {data['title']}\n"
        f"差分名: {data['chart_name'] or 'なし'}\n"
        f"タグ: {data['tags'] or 'なし'}\n"
        f"備考: {data['memo'] or 'なし'}"
    )



# FINE_TAG_ORDER_BLOCK_V1

DEFAULT_TAG_ORDER = [
    "y",
    "8",
    "yt",
    "tt",
    "go",
    "r",
    "ep",
    "sr",
    "g",
    "ni",
    "dy",
    "sh",
]


def ensure_tag_order_table():
    con = db()
    con.execute("""
    CREATE TABLE IF NOT EXISTS tag_order(
        short_name TEXT PRIMARY KEY,
        priority INTEGER NOT NULL
    )
    """)

    for i, short in enumerate(DEFAULT_TAG_ORDER, 1):
        con.execute(
            "INSERT OR IGNORE INTO tag_order(short_name, priority) VALUES(?, ?)",
            (short, i)
        )

    con.commit()
    con.close()


def tag_to_short_map():
    tags = get_all_tags()
    result = {}
    for short, formal in tags.items():
        result[formal] = short
        if formal.startswith("惜敗["):
            result[formal] = "sh"
    return result


def short_to_tag_map():
    return get_all_tags()


def get_tag_order_map():
    ensure_tag_order_table()

    con = db()
    rows = con.execute(
        "SELECT short_name, priority FROM tag_order"
    ).fetchall()
    con.close()

    return {short: priority for short, priority in rows}


def tag_priority(tag):
    if tag.startswith("惜敗["):
        return get_tag_order_map().get("sh", 999)

    short = tag_to_short_map().get(tag)
    if not short:
        return 999

    return get_tag_order_map().get(short, 999)


def sort_tags(tags):
    return sorted(tags, key=lambda tag: (tag_priority(tag), normalize_text(tag)))


def join_tags(tags):
    seen = []
    for t in tags:
        if t and t not in seen:
            seen.append(t)

    seen = sort_tags(seen)
    return "|".join(seen)


def display_tags(tags):
    tag_list = split_tags(tags)
    tag_list = sort_tags(tag_list)
    return " | ".join(tag_list) if tag_list else "タグなし"


def get_all_tags(user_id=None):
    tags = {}

    for k, v in TAG_ALIASES.items():
        tags[k] = canonical_tag_name(v)

    con = db()

    try:
        ensure_user_custom_tags_table(con)
        if user_id is not None:
            for short_name, full_name in con.execute(
                """
                SELECT short_name, full_name
                FROM user_custom_tags
                WHERE user_id=?
                """,
                (str(user_id),),
            ):
                tags[short_name] = canonical_tag_name(full_name)
    finally:
        con.close()

    order = get_tag_order_map()

    return dict(
        sorted(
            tags.items(),
            key=lambda item: (order.get(item[0], 999), normalize_text(item[1]))
        )
    )


def normalize_tag_for_count(tag):
    tag = canonical_tag_name(tag)
    if tag.startswith("諠懈風["):
        return "諠懈風"
    return tag


def make_tag_count_embed(title, counter):
    embed = discord.Embed(title=title, color=EMBED_BLUE)

    all_tags = get_all_tags()
    fixed_tags = []
    for short_name in DEFAULT_TAG_ORDER:
        tag = all_tags.get(short_name)
        if tag and tag not in fixed_tags:
            fixed_tags.append(tag)

    fixed_tag_set = set(fixed_tags)
    custom_tags = sorted(
        (
            tag
            for tag, count in counter.items()
            if count > 0 and tag not in fixed_tag_set
        ),
        key=normalize_text,
    )

    lines = [f"{tag} ({counter.get(tag, 0)})" for tag in fixed_tags]
    lines += [f"{tag} ({counter[tag]})" for tag in custom_tags]

    for i, chunk in enumerate(split_embed_lines(lines, limit=900), 1):
        embed.add_field(
            name="集計" if i == 1 else f"集計 続き{i}",
            value="```text\n" + chunk + "\n```",
            inline=False,
        )

    return embed


def set_tag_order(short_name, new_priority):
    ensure_tag_order_table()

    tags = get_all_tags()

    if short_name not in tags:
        return False, "そのタグは見つからなかったよ。`!t` で確認してみてね。"

    items = [short for short in tags.keys()]
    items = [short for short in items if short != short_name]

    if new_priority < 1:
        new_priority = 1

    if new_priority > len(items) + 1:
        new_priority = len(items) + 1

    items.insert(new_priority - 1, short_name)

    con = db()
    for i, short in enumerate(items, 1):
        con.execute(
            "INSERT OR REPLACE INTO tag_order(short_name, priority) VALUES(?, ?)",
            (short, i)
        )

    con.commit()
    con.close()

    return True, short_name


@bot.command(name="tagorder", aliases=["タグ順", "tagprio"])
async def tagorder_cmd(ctx, short_name=None, priority: int = None):
    if ctx.channel.id != CHANNEL_ID:
        return

    ensure_tag_order_table()

    if not short_name:
        tags = get_all_tags()
        lines = ["🏷️ タグ優先順だよ！", ""]

        for i, (short, formal) in enumerate(tags.items(), 1):
            lines.append(f"{i}. {formal} → {short}")

        await ctx.send(chr(10).join(lines)[:1900])
        return

    short_name = normalize_tag_input(short_name)

    if priority is None:
        await ctx.send("使い方だよ: `!tagorder go 5`")
        return

    ok, msg = set_tag_order(short_name, priority)

    if not ok:
        await ctx.send(msg)
        return

    tags = get_all_tags()
    lines = [f"✅ {short_name} の優先順を {priority} 番にしたよ！", ""]

    for i, (short, formal) in enumerate(tags.items(), 1):
        lines.append(f"{i}. {formal} → {short}")

    await ctx.send(chr(10).join(lines)[:1900])



# FINE_DELETE_COMMANDS_V1

def delete_table(name):
    real_name = resolve_table_name(name)

    if not real_name:
        return False, "そのテーブルは見つかりませんでしたわ。"

    if normalize_text(real_name) in ["st", "sl"]:
        return False, "st / sl は基本テーブルなので削除できませんわ。"

    con = db()
    ensure_user_tags_table(con)
    song_ids = [
        row[0]
        for row in con.execute("SELECT id FROM songs WHERE source=?", (real_name,)).fetchall()
    ]
    for song_id in song_ids:
        con.execute("DELETE FROM user_tags WHERE song_id=?", (song_id,))
    con.execute("DELETE FROM songs WHERE source=?", (real_name,))
    con.execute("DELETE FROM custom_tables WHERE name=?", (real_name,))
    con.commit()
    con.close()

    return True, real_name


def get_song_by_id(song_id):
    con = db()
    row = con.execute(
        "SELECT id, source, level, title, chart_name, url, tags, memo FROM songs WHERE id=?",
        (song_id,)
    ).fetchone()
    con.close()
    return row


def delete_song_by_id(song_id):
    con = db()
    ensure_user_tags_table(con)
    con.execute("DELETE FROM user_tags WHERE song_id=?", (song_id,))
    cur = con.execute("DELETE FROM songs WHERE id=?", (song_id,))
    con.commit()
    ok = cur.rowcount > 0
    con.close()
    return ok


@bot.command(name="deltable", aliases=["テーブル削除"])
async def deltable_cmd(ctx, *, name=None):
    if ctx.channel.id != CHANNEL_ID:
        return

    if not name:
        await ctx.send("使い方ですわ: `!deltable テーブル名`")
        return

    ok, msg = delete_table(name.strip())

    if ok:
        await ctx.send(f"🗑️ テーブルを削除しましたわ\n・{msg}")
    else:
        await ctx.send(msg)


@bot.command(name="delsong", aliases=["曲削除"])
async def delsong_cmd(ctx, *, target=None):
    if ctx.channel.id != CHANNEL_ID:
        return

    if not target:
        await ctx.send("使い方ですわ: `!delsong 1`")
        return

    rows = last_search.get(ctx.author.id, [])

    if not target.strip().isdigit():
        await ctx.send("曲削除は、先に `!s` で検索してから `!delsong 番号` で指定してくださいませ。")
        return

    index = int(target.strip())

    if index < 1 or index > len(rows):
        await ctx.send("その番号は直前の検索結果にありませんわ。")
        return

    row = rows[index - 1]
    song_id = row[0]
    title = row[3]

    if delete_song_by_id(song_id):
        await ctx.send(f"🗑️ 曲を削除しましたわ\n・{title}")
    else:
        await ctx.send("削除に失敗しましたわ。")



# FINE_REACTION_TOGGLE_V1

def reaction_emoji_to_tag(emoji, user_id=None, emoji_to_tag=None):
    emoji = str(emoji)
    if emoji_to_tag and emoji in emoji_to_tag:
        return canonical_tag_name(emoji_to_tag[emoji])
    normalized_emoji = normalize_reaction_emoji(emoji)
    if emoji_to_tag and normalized_emoji in emoji_to_tag:
        return canonical_tag_name(emoji_to_tag[normalized_emoji])
    for e, tag in reaction_tag_options(user_id=user_id):
        if normalized_emoji == normalize_reaction_emoji(e):
            return tag
    return None


def make_reaction_tag(tag):
    if tag == "惜敗":
        return f"惜敗[{datetime.now(JST).strftime('%Y-%m-%d')}]"
    return tag


def set_song_tag_by_reaction(song_id, emoji, enabled, user_id=None, emoji_to_tag=None):
    started = time.perf_counter()
    result = None
    tag = None
    try:
        tag = reaction_emoji_to_tag(emoji, user_id=user_id, emoji_to_tag=emoji_to_tag)
        if not tag:
            return None

        row = get_song_by_id(song_id)
        if not row:
            return None

        current_tags = split_tags(get_song_tags_for_display(row, user_id=user_id))
        current_memo = row[7] or ""

        exists = any(tag_matches(song_tag, tag) for song_tag in current_tags)

        if enabled:
            if exists:
                result = row
                return result
            new_tags = current_tags + [make_reaction_tag(tag)]
        else:
            if not exists:
                result = row
                return result
            new_tags = [
                song_tag
                for song_tag in current_tags
                if not tag_matches(song_tag, tag)
            ]

        update_song(row, join_tags(new_tags), current_memo, user_id=user_id)
        result = get_song_by_id(song_id)
        return result
    finally:
        logging.info(
            "timing set_song_tag_by_reaction=%.3fs user_id=%s song_id=%s emoji=%s tag=%s enabled=%s changed=%s",
            time.perf_counter() - started,
            user_id,
            song_id,
            emoji,
            tag or "",
            enabled,
            result is not None,
        )


async def add_all_tag_reactions(message, user_id=None):
    for emoji, tag in reaction_tag_options(user_id=user_id):
        try:
            await message.add_reaction(normalize_reaction_emoji(emoji))
        except Exception:
            logging.exception("add reaction failed message_id=%s user_id=%s emoji=%s tag=%s", message.id, user_id, emoji, tag)


async def refresh_reaction_song_message(payload, enabled):
    if payload.user_id == bot.user.id:
        return

    if payload.channel_id != CHANNEL_ID:
        return

    context = reaction_song_messages.get(payload.message_id)
    if not context:
        return

    if isinstance(context, dict):
        song_id = context.get("song_id")
        owner_user_id = context.get("owner_user_id")
        emoji_to_tag = context.get("emoji_to_tag") or {}
    else:
        song_id = context
        owner_user_id = None
        emoji_to_tag = {}

    if not song_id:
        return

    if owner_user_id and str(payload.user_id) != str(owner_user_id):
        logging.info(
            "reaction ignored non-owner message_id=%s owner_user_id=%s payload_user_id=%s emoji=%s",
            payload.message_id,
            owner_user_id,
            payload.user_id,
            payload.emoji,
        )
        return

    user_id = owner_user_id or str(payload.user_id)
    fresh = set_song_tag_by_reaction(
        song_id,
        str(payload.emoji),
        enabled,
        user_id=user_id,
        emoji_to_tag=emoji_to_tag,
    )
    if not fresh:
        return

    channel = bot.get_channel(payload.channel_id)
    if not channel:
        return

    try:
        message = await channel.fetch_message(payload.message_id)
    except Exception:
        return

    try:
        await message.edit(
            embed=make_single_song_detail_embed(
                fresh,
                "🎵 楽曲情報",
                user_id=user_id,
            )
        )
    except Exception:
        pass

    if os.getenv("ENABLE_SHEET_SYNC_ON_REACTION", "").strip() != "1":
        logging.info(
            "sheet sync skipped on reaction user_id=%s song_id=%s enabled=%s reason=disabled",
            user_id,
            song_id,
            enabled,
        )
        try:
            with open("/tmp/fine_sheet_sync.log", "a", encoding="utf-8") as f:
                f.write(
                    f"[sheet_sync] sheet sync skipped on reaction "
                    f"enabled={enabled} song_id={song_id} user={user_id}\n"
                )
        except Exception:
            logging.exception("failed to write sheet sync skipped log")
        return

    try:
        from sheet_sync import sync_song_to_sheet
        tag_name = reaction_emoji_to_tag(str(payload.emoji), user_id=user_id, emoji_to_tag=emoji_to_tag)

        with open("/tmp/fine_sheet_sync.log", "a", encoding="utf-8") as f:
            f.write(
                f"[sheet_sync] start enabled={enabled} "
                f"emoji={payload.emoji} tag={tag_name} song_id={song_id} "
                f"user={user_id} bot={bot.user.id if bot.user else None}\n"
            )

        ok = sync_song_to_sheet(
            fresh,
            tag_name=tag_name,
            emoji=str(payload.emoji),
            enabled=enabled,
            user_id=user_id,
        )

        with open("/tmp/fine_sheet_sync.log", "a", encoding="utf-8") as f:
            f.write(f"[sheet_sync] done ok={ok}\n")

    except Exception as e:
        with open("/tmp/fine_sheet_sync.log", "a", encoding="utf-8") as f:
            f.write(f"[sheet_sync] skipped: {e}\n")


@bot.event
async def on_raw_reaction_add(payload):
    started = time.perf_counter()
    try:
        with open("/tmp/fine_reaction.log", "a", encoding="utf-8") as f:
            f.write(f"[REACTION ADD] message={payload.message_id} emoji={payload.emoji} user={payload.user_id}\n")
        await refresh_reaction_song_message(payload, True)
    finally:
        logging.info(
            "timing reaction_add=%.3fs user_id=%s message_id=%s emoji=%s",
            time.perf_counter() - started,
            payload.user_id,
            payload.message_id,
            payload.emoji,
        )


@bot.event
async def on_raw_reaction_remove(payload):
    started = time.perf_counter()
    try:
        await refresh_reaction_song_message(payload, False)
    finally:
        logging.info(
            "timing reaction_remove=%.3fs user_id=%s message_id=%s emoji=%s",
            time.perf_counter() - started,
            payload.user_id,
            payload.message_id,
            payload.emoji,
        )


@bot.command(name="maketables", aliases=["make_tables", "tablesgen"])
async def maketables_cmd(ctx):
    if ctx.channel.id != CHANNEL_ID:
        return

    user_id = str(ctx.author.id)
    table_base_url = os.getenv("TABLE_BASE_URL", "").strip()

    try:
        async with ctx.typing():
            from table_generator import generate_user_tables

            result = generate_user_tables(user_id, table_base_url=table_base_url)
    except Exception:
        await ctx.send("難易度表データの生成に失敗しました。DBと出力先を確認してください。")
        return

    tags = result["tags"]
    non_empty = [tag for tag in tags if tag["count"] > 0]
    lines = [
        "難易度表データを生成しました。",
        f"user_id: {user_id}",
        f"生成タグ数: {len(tags)}",
        f"曲ありタグ数: {len(non_empty)}",
        "",
        result.get("index_url") or result.get("root", ""),
    ]

    for tag in non_empty[:10]:
        lines.append(f"{tag['tag_name']} ({tag['count']}): {tag['url']}")

    if len(non_empty) > 10:
        lines.append(f"...ほか {len(non_empty) - 10} タグ")

    await ctx.send("\n".join(lines)[:1900])


bot.remove_command("maketables")


@bot.command(name="maketables", aliases=["make_tables", "tablesgen"])
async def maketables_cmd_pages(ctx):
    if ctx.channel.id != CHANNEL_ID:
        return

    user_id = str(ctx.author.id)
    table_base_url = os.getenv("TABLE_BASE_URL", "").strip()

    try:
        async with ctx.typing():
            from table_generator import generate_user_tables

            result = generate_user_tables(user_id, table_base_url=table_base_url)
    except Exception:
        await ctx.send("難易度表データの生成に失敗しました。DBと出力先を確認してください。")
        return

    try:
        async with ctx.typing():
            from pages_deploy import deploy_user_tables

            deploy_result = deploy_user_tables(user_id)
    except Exception:
        await ctx.send(
            "難易度表データは生成しましたが、GitHub Pagesへの公開に失敗しました。"
            "GITHUB_PAGES_DIR / GITHUB_TOKEN / git push権限を確認してください。"
        )
        return

    tags = result["tags"]
    non_empty = [tag for tag in tags if tag["count"] > 0]
    lines = [
        "難易度表データを生成して公開しました。",
        f"user_id: {user_id}",
        f"生成タグ数: {len(tags)}",
        f"曲ありタグ数: {len(non_empty)}",
        f"deploy: {deploy_result.get('message', '')}",
        "",
        result.get("index_url") or result.get("root", ""),
    ]

    for tag in non_empty[:10]:
        lines.append(f"{tag['tag_name']} ({tag['count']}): {tag['url']}")

    if len(non_empty) > 10:
        lines.append(f"...ほか {len(non_empty) - 10} タグ")

    await ctx.send("\n".join(lines)[:1900])


bot.remove_command("maketables")


@bot.command(name="maketables", aliases=["make_tables", "tablesgen"])
async def maketables_cmd_progress(ctx, *tag_args):
    if ctx.channel.id != CHANNEL_ID:
        return

    user_id = str(ctx.author.id)
    target_tag_name = None
    if tag_args:
        tag_token = " ".join(tag_args).strip()
        target_tag_name = resolve_tag_fuzzy(tag_token, user_id=ctx.author.id) or resolve_tag(tag_token, user_id=ctx.author.id)
        if not target_tag_name:
            await ctx.send(f"⚠️ `{tag_token}` はタグに見つからなかったみたいだよ！ `!t` で確認してね。")
            return

    table_base_url = os.getenv("TABLE_BASE_URL", "").strip()
    progress_message = await ctx.send("📋 難易度表生成を開始したよ！")
    logging.info(
        "maketables progress sent user_id=%s message_id=%s sent_at=%s discord_error=False label=start",
        user_id,
        getattr(progress_message, "id", ""),
        datetime.now(JST).isoformat(),
    )
    loop = asyncio.get_running_loop()
    pending_progress = []

    async def update_progress(text, label="progress", queued_at=None):
        edit_started = time.perf_counter()
        edited_at = datetime.now(JST).isoformat()
        try:
            await progress_message.edit(content=text[:1900])
            logging.info(
                "maketables progress edited user_id=%s message_id=%s label=%s queued_at=%s edited_at=%s duration=%.3fs discord_error=False",
                user_id,
                getattr(progress_message, "id", ""),
                label,
                queued_at or "",
                edited_at,
                time.perf_counter() - edit_started,
            )
        except Exception:
            logging.exception(
                "maketables progress edit failed user_id=%s message_id=%s label=%s queued_at=%s edited_at=%s duration=%.3fs discord_error=True",
                user_id,
                getattr(progress_message, "id", ""),
                label,
                queued_at or "",
                edited_at,
                time.perf_counter() - edit_started,
            )

    def schedule_progress(text, label="progress"):
        queued_at = datetime.now(JST).isoformat()
        logging.info(
            "maketables progress queued user_id=%s message_id=%s label=%s queued_at=%s discord_error=False",
            user_id,
            getattr(progress_message, "id", ""),
            label,
            queued_at,
        )
        try:
            future = asyncio.run_coroutine_threadsafe(
                update_progress(text, label=label, queued_at=queued_at),
                loop,
            )
        except Exception:
            logging.exception(
                "maketables progress callback scheduling failed user_id=%s label=%s queued_at=%s",
                user_id,
                label,
                queued_at,
            )
            return

        pending_progress.append(future)

        def log_progress_result(done):
            try:
                done.result()
            except Exception:
                logging.exception(
                    "maketables progress callback failed user_id=%s label=%s queued_at=%s",
                    user_id,
                    label,
                    queued_at,
                )

        future.add_done_callback(log_progress_result)
        return future

    async def flush_progress(label):
        if not pending_progress:
            return
        futures = [asyncio.wrap_future(future) for future in pending_progress if not future.done()]
        pending_progress.clear()
        if not futures:
            return
        logging.info(
            "maketables progress flush start user_id=%s label=%s pending=%s flushed_at=%s",
            user_id,
            label,
            len(futures),
            datetime.now(JST).isoformat(),
        )
        await asyncio.gather(*futures, return_exceptions=True)
        logging.info(
            "maketables progress flush done user_id=%s label=%s flushed_at=%s",
            user_id,
            label,
            datetime.now(JST).isoformat(),
        )

    def percent(index, total):
        if not total:
            return 0
        return round(index * 100 / total)

    def progress_text(title, index, total, current):
        return f"{title}\n[{index}/{total}] {percent(index, total)}%\n現在: {current}"

    def concise_error(exc):
        text = compact_text(str(exc), 500)
        lower = text.lower()
        if "fetch first" in lower or "rejected" in lower or "non-fast-forward" in lower:
            return "git push rejected\nremote contains work that you do not have locally"
        if "github_pages_dir" in lower:
            return "GITHUB_PAGES_DIR が設定されていないみたいだね！"
        return text or exc.__class__.__name__

    def generation_error_message(exc):
        text = str(exc)
        if "stella_songs.db" in text or "no such table" in text:
            return "曲データベースの確認中に問題が起きたみたいだね！"
        return concise_error(exc)

    try:
        async with ctx.typing():
            from table_generator import generate_user_tables

            def generate_progress(event, **data):
                if event != "generate_tag":
                    return
                index = data.get("index")
                total = data.get("total")
                tag_name = data.get("tag_name", "")
                schedule_progress(
                    progress_text("📋 難易度表生成中だよ！", index, total, tag_name),
                    label=f"generate:{index}/{total}",
                )

            result = await loop.run_in_executor(
                None,
                lambda: generate_user_tables(
                    user_id,
                    table_base_url=table_base_url,
                    progress=generate_progress,
                ),
            )
            await flush_progress("generation")
    except Exception as e:
        logging.exception("maketables generation failed for user_id=%s", user_id)
        await update_progress(
            "❌ 難易度表生成失敗\n"
            f"{generation_error_message(e)}\n\n"
            "詳細はサーバーログを確認してね！"
        )
        return

    try:
        async with ctx.typing():
            from pages_deploy import deploy_user_tables

            def deploy_progress(event, **data):
                steps = {
                    "pull": (1, "git pull"),
                    "copy": (2, "copy"),
                    "commit": (3, "commit"),
                    "push": (4, "push"),
                    "complete": (4, "push完了" if data.get("status") == "pushed" else "変更なし"),
                }
                index, current = steps.get(event, (1, "準備中"))
                schedule_progress(
                    progress_text("📋 GitHub Pagesへ公開中だよ！", index, 4, current),
                    label=f"deploy:{event}",
                )

            async with auto_table_publish_lock:
                deploy_result = await loop.run_in_executor(
                    None,
                    lambda: deploy_user_tables(user_id, progress=deploy_progress),
                )
            await flush_progress("deploy")
            deploy_done_text = "push完了" if deploy_result.get("pushed") else "変更なし"
            await update_progress(
                progress_text("📋 GitHub Pagesへ公開中だよ！", 4, 4, deploy_done_text),
                label="deploy:complete",
            )
    except Exception as e:
        logging.exception("maketables deploy failed for user_id=%s", user_id)
        await update_progress(
            "❌ GitHub Pages公開失敗\n"
            f"{concise_error(e)}\n\n"
            "詳細はサーバーログを確認してね！"
        )
        return

    tags = result["tags"]
    non_empty = [tag for tag in tags if tag["count"] > 0]
    table_links = [tag for tag in tags if tag.get("table_url")]
    if target_tag_name:
        table_links = [
            tag for tag in table_links
            if tag_matches(tag.get("tag_name", ""), target_tag_name)
        ]
    header_lines = [
        "✅ 難易度表を公開できたよ！",
        f"user_id: {user_id}",
        f"生成タグ数: {len(tags)}",
        f"曲ありタグ数: {len(non_empty)}",
        "deploy:",
        f"status: {deploy_result.get('message', '')}",
        f"commit: {deploy_result.get('commit', '')}",
        "",
        f"beatoraja登録URL: {target_tag_name}" if target_tag_name else "beatoraja登録URL:",
    ]

    def table_link_lines(link_tags, page=None, total_pages=None):
        lines = []
        if page is not None and total_pages is not None:
            lines.append(f"beatoraja登録URL 続き {page}/{total_pages}:")
            lines.append("")
        for tag in link_tags:
            lines.append(f"{tag['tag_name']} ({tag['count']})")
            lines.append(f"```text\n{tag['table_url']}\n```")
        return lines

    def build_table_link_chunks(link_tags, first_prefix_len=0, limit=1750):
        chunks = []
        current = []
        current_len = first_prefix_len
        for tag in link_tags:
            item_lines = table_link_lines([tag])
            item_text = "\n".join(item_lines)
            item_len = len(item_text) + 1
            if current and current_len + item_len > limit:
                chunks.append(current)
                current = []
                current_len = 0
            current.append(tag)
            current_len += item_len
        if current:
            chunks.append(current)
        return chunks

    chunks = build_table_link_chunks(
        table_links,
        first_prefix_len=len("\n".join(header_lines)) + 2,
    )
    if not chunks:
        await update_progress("\n".join(header_lines), label="complete")
        return

    total_pages = len(chunks)
    first_lines = header_lines + table_link_lines(chunks[0])
    if total_pages > 1:
        first_lines.append("")
        first_lines.append(f"続きのURLがあと {total_pages - 1} 件あるよ！")
    await update_progress("\n".join(first_lines)[:1900], label="complete")

    for page, chunk in enumerate(chunks[1:], 2):
        await ctx.send("\n".join(table_link_lines(chunk, page=page, total_pages=total_pages))[:1900])


init_db()
bot.run(TOKEN)
