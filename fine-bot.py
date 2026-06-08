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
    "sr": "しょうもないラス殺し",
    "ep": "地力上げ",
    "ni": "良譜面",
    "g": "ゴミ",
    "sh": "惜敗",
}

OLD_LAST_KILL_TAG = TAG_ALIASES["sr"]
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

last_search = {}
reaction_song_messages = {}


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


def add_custom_tag(full_name, short_name, user_id):
    con = db()
    ensure_user_custom_tags_table(con)
    now = now_text()

    con.execute(
        """
        INSERT OR REPLACE INTO user_custom_tags
        (user_id, short_name, full_name, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (str(user_id), short_name, canonical_tag_name(full_name), now, now)
    )

    con.commit()
    con.close()


def delete_custom_tag(short_name, user_id):
    con = db()
    ensure_user_custom_tags_table(con)

    con.execute(
        "DELETE FROM user_custom_tags WHERE user_id=? AND short_name=?",
        (str(user_id), short_name)
    )

    con.commit()
    con.close()



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

    embed = discord.Embed(
        title="📖 Fine Bot ヘルプだよ！",
        description=(
            "BMS楽曲のタグ付け・検索・復習をするためのBotだよ。\n"
            "まず検索して、出てきた番号を使って編集してね。"
        ),
        color=EMBED_BLUE,
    )

    embed.add_field(
        name="🔎 検索",
        value="`!s ceu`\n`!s sl12 ceu`\n曲名・差分名・タグ・備考をまとめて検索するよ。",
        inline=False,
    )

    embed.add_field(
        name="✏️ 編集",
        value="`!e 1 y ep`\n`1e y ep`\n`reset`\n検索結果の番号を指定して更新するよ。",
        inline=False,
    )

    embed.add_field(
        name="⚡ 1件検索後の簡易編集",
        value="`y ep dy`\n`add ni`\n`-dy`\n`/null`\n検索結果が1件だけなら、そのまま編集できるよ。",
        inline=False,
    )

    embed.add_field(
        name="🏷️ タグ系",
        value=(
            "`!t`\n`!ts y`\n`!ts sl12 日課`\n`!tl dy`\n`!tagcount`\n"
            "`!addtag テスト01 te01`\n"
            "タグ一覧に新しいタグを追加するよ。例: `!addtag テスト01 te01`"
        ),
        inline=False,
    )

    embed.add_field(
        name="📚 テーブル・曲追加",
        value="`!tables`\n`!addtable テーブル名`\n`!addsong テーブル lv12 \"曲名\" \"差分名\" y 備考`",
        inline=False,
    )

    embed.add_field(
        name="🏷️ 主なタグ",
        value=(
            "`y` 横認識 / `8` 横認識(8分系) / `yt` 横認識(縦系)\n"
            "`tt` 縦連 / `go` ガチ押し系 / `r` 乱打\n"
            "`ep` 地力上げ / `ni` 良譜面 / `dy` 日課 / `sh` 惜敗"
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


async def send_song_list_embeds(ctx, title, rows, per_page=10):
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

        await ctx.send(embed=embed)



def make_single_song_embed(row, title="✅ 更新したよ！", color=EMBED_GREEN, user_id=None):
    embed = make_single_song_detail_embed(row, title, color, user_id=user_id)
    return embed


async def send_single_song_embed(target, row, title="✅ 更新したよ！", color=EMBED_GREEN, user_id=None):
    if user_id is None:
        user_id = getattr(getattr(target, "author", None), "id", None)
    msg = await target.send(embed=make_single_song_embed(row, title, color, user_id=user_id))
    reaction_song_messages[msg.id] = row[0]
    await add_all_tag_reactions(msg)



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
    ("💀", "しょうもないラス殺し"),
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


def tag_emoji(tag):
    base = tag_base_name(tag)
    for emoji, name in TAG_REACTION_EMOJIS:
        if name == base:
            return emoji
    return "🏷️"


def emoji_tag_lines_from_tags(tags):
    tag_list = split_tags(tags)

    if not tag_list:
        return "タグなし"

    return "\n".join(
        f"{tag_emoji(tag)} {tag}"
        for tag in sort_tags(tag_list)
    )


def reaction_guide_text():
    return "\n".join([
        "↔️ 横認識 / 🎵 横認識(8分系) / ↕️ 横認識(縦系)",
        "🧱 縦連 / 👊 ガチ押し系 / 🔥 乱打 / 💪 地力上げ",
        "💀 しょうもないラス殺し / 🗑️ ゴミ / ⭐ 良譜面",
        "📅 日課 / 😭 惜敗",
    ])


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
            emoji_tag_lines_from_tags(get_song_tags_for_display(row, user_id=user_id)),
            "",
            f"💬 {memo if memo else '備考なし'}",
            "",
        ]),
        inline=False,
    )

    embed.add_field(
        name="🎛️ 対応絵文字",
        value=reaction_guide_text(),
        inline=False,
    )

    embed.add_field(
        name="✏️ このまま入力できるよ！",
        value="\n".join([
            "`y`（横認識） / `8`（横認識8分系） / `yt`（横認識縦系）",
            "`tt`（縦連） / `go`（ガチ押し系） / `r`（乱打）",
            "`ep`（地力上げ） / `sr`（ラス殺し） / `g`（ゴミ）",
            "`ni`（良譜面） / `dy`（日課） / `sh`（惜敗）",
            "",
            "`add ep` / `add dy` / `add ni`",
            "`-dy` / `reset` / `/null`",
        ]),
        inline=False,
    )

    embed.set_footer(text="リアクションで直接編集できるよ！")
    return embed



@bot.command(name="s", aliases=["search", "検索", "さ", "す"])
async def search_cmd(ctx, *args):
    if ctx.channel.id != CHANNEL_ID:
        return

    if not args:
        await ctx.send("検索語を入れてくださいませ。例: `!s sl1 ceu`")
        return

    rows = search_songs(args, user_id=ctx.author.id)
    last_search[ctx.author.id] = rows

    if not rows:
        await ctx.send("一致する曲が見つかりませんでしたわ。")
        return

    if len(rows) == 1:
        msg = await ctx.send(
            embed=make_single_song_detail_embed(
                rows[0],
                "🎵 楽曲情報",
                user_id=ctx.author.id,
            )
        )

        reaction_song_messages[msg.id] = rows[0][0]
        await add_all_tag_reactions(msg)

        return

    await send_song_list_embeds(ctx, "🔎 検索結果だよ！", rows)



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
async def addtag(ctx, full_name=None, short_name=None):

    if ctx.channel.id != CHANNEL_ID:
        return

    if not full_name or not short_name:
        await ctx.send(
            "使い方ですわ: !addtag ガチ押し系 go"
        )
        return

    add_custom_tag(full_name, short_name, user_id=ctx.author.id)

    await ctx.send(
        f"✅ タグ追加ですわ\n・{full_name}\n{short_name}"
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

    delete_custom_tag(short_name, user_id=ctx.author.id)

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

    if not body:
        await ctx.send("タグか備考を入力してくださいませ。例: `!e 1 y r 良い`")
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
    con = db()

    rows = con.execute(
        "SELECT id, source, level, title, chart_name, url, tags, memo FROM songs"
    ).fetchall()

    con.close()

    norm_words = [normalize_text(w) for w in words if w.strip()]
    result = []

    for row in rows:
        sid, source, level, title, chart_name, url, tags, memo = row

        hay = normalize_text(" ".join([
            source or "",
            level or "",
            title or "",
            chart_name or "",
            url or "",
            get_song_tags_for_display(row, user_id=user_id),
            memo or "",
        ]))

        if all(w in hay for w in norm_words):
            result.append(row)

    return result[:20]


def search_by_tag(args, user_id=None):
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
        tag = resolve_tag_fuzzy(token, user_id=user_id)
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

    for row in rows:
        sid, source, level, title, chart_name, url, tags, memo = row

        if level_filter and normalize_text(level) != level_filter:
            continue

        song_tags = split_tags(get_song_tags_for_display(row, user_id=user_id))

        if all(any(tag_matches(song_tag, tag) for song_tag in song_tags) for tag in resolved_tags):
            results.append(row)

    return resolved_tags, results[:20]


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

def reaction_emoji_to_tag(emoji):
    emoji = str(emoji)
    for e, tag in TAG_REACTION_EMOJIS:
        if emoji == e:
            return tag
    return None


def make_reaction_tag(tag):
    if tag == "惜敗":
        return f"惜敗[{datetime.now(JST).strftime('%Y-%m-%d')}]"
    return tag


def set_song_tag_by_reaction(song_id, emoji, enabled, user_id=None):
    tag = reaction_emoji_to_tag(emoji)
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
            return row
        new_tags = current_tags + [make_reaction_tag(tag)]
    else:
        if not exists:
            return row
        new_tags = [
            song_tag
            for song_tag in current_tags
            if not tag_matches(song_tag, tag)
        ]

    update_song(row, join_tags(new_tags), current_memo, user_id=user_id)
    return get_song_by_id(song_id)


async def add_all_tag_reactions(message):
    for emoji, tag in TAG_REACTION_EMOJIS:
        try:
            await message.add_reaction(emoji)
        except Exception:
            pass


async def refresh_reaction_song_message(payload, enabled):
    if payload.user_id == bot.user.id:
        return

    if payload.channel_id != CHANNEL_ID:
        return

    song_id = reaction_song_messages.get(payload.message_id)
    if not song_id:
        return

    fresh = set_song_tag_by_reaction(song_id, str(payload.emoji), enabled, user_id=payload.user_id)
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
                user_id=payload.user_id,
            )
        )
    except Exception:
        pass

    try:
        from sheet_sync import sync_song_to_sheet
        tag_name = reaction_emoji_to_tag(str(payload.emoji))

        with open("/tmp/fine_sheet_sync.log", "a", encoding="utf-8") as f:
            f.write(
                f"[sheet_sync] start enabled={enabled} "
                f"emoji={payload.emoji} tag={tag_name} song_id={song_id} "
                f"user={payload.user_id} bot={bot.user.id if bot.user else None}\n"
            )

        ok = sync_song_to_sheet(
            fresh,
            tag_name=tag_name,
            emoji=str(payload.emoji),
            enabled=enabled,
            user_id=payload.user_id,
        )

        with open("/tmp/fine_sheet_sync.log", "a", encoding="utf-8") as f:
            f.write(f"[sheet_sync] done ok={ok}\n")

    except Exception as e:
        with open("/tmp/fine_sheet_sync.log", "a", encoding="utf-8") as f:
            f.write(f"[sheet_sync] skipped: {e}\n")


@bot.event
async def on_raw_reaction_add(payload):
    with open("/tmp/fine_reaction.log", "a", encoding="utf-8") as f:
        f.write(f"[REACTION ADD] message={payload.message_id} emoji={payload.emoji} user={payload.user_id}\n")
    await refresh_reaction_song_message(payload, True)


@bot.event
async def on_raw_reaction_remove(payload):
    await refresh_reaction_song_message(payload, False)


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
async def maketables_cmd_progress(ctx):
    if ctx.channel.id != CHANNEL_ID:
        return

    user_id = str(ctx.author.id)
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
    lines = [
        "✅ 難易度表を公開できたよ！",
        f"user_id: {user_id}",
        f"生成タグ数: {len(tags)}",
        f"曲ありタグ数: {len(non_empty)}",
        "deploy:",
        f"status: {deploy_result.get('message', '')}",
        f"commit: {deploy_result.get('commit', '')}",
        "",
        "beatoraja登録URL:",
    ]

    for tag in table_links[:12]:
        lines.append(f"{tag['tag_name']} ({tag['count']})")
        lines.append(f"```text\n{tag['table_url']}\n```")

    if len(table_links) > 12:
        lines.append(f"...ほか {len(table_links) - 12} タグあるよ！")

    await update_progress("\n".join(lines), label="complete")


init_db()
bot.run(TOKEN)
