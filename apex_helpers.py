"""Apex Legends Status / Mozambique API helpers, roster parsing, rank roles, map & craft."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from typing import Any

import aiohttp
import discord

ALS_SITE = "https://apexlegendsstatus.com/"
APEX_BASE = "https://api.mozambiquehe.re"
APEX_BRIDGE_URL = f"{APEX_BASE}/bridge"
ROSTER_CHANNEL_NAME = os.environ.get("APEX_ROSTER_CHANNEL_NAME", "apexid")

# guild_id -> (entries, monotonic_ts)
_roster_cache: dict[int, tuple[list[dict[str, Any]], float]] = {}
ROSTER_CACHE_TTL_SEC = 300.0

_last_map_craft_fingerprint: str | None = None

# API rankName (English) -> Discord role name on your server (edit or override via env)
DEFAULT_RANK_ROLE_MAP: dict[str, str] = {
    "Rookie": "ルーキー",
    "Bronze": "ブロンズ",
    "Silver": "シルバー",
    "Gold": "ゴールド",
    "Platinum": "プラチナ",
    "Diamond": "ダイヤモンド",
    "Master": "マスター",
    "Apex Predator": "プレデター",
}


def _rank_role_map() -> dict[str, str]:
    raw = (os.environ.get("APEX_RANK_ROLE_MAP") or "").strip()
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                out = {str(k): str(v) for k, v in data.items()}
                return {**DEFAULT_RANK_ROLE_MAP, **out}
        except json.JSONDecodeError:
            pass
    return dict(DEFAULT_RANK_ROLE_MAP)


def _all_managed_rank_role_names() -> set[str]:
    return set(_rank_role_map().values())


def get_api_key() -> str | None:
    k = (os.environ.get("APEX_LEGENDS_API_KEY") or "").strip()
    return k or None


async def fetch_apex_bridge(player: str, platform: str, api_key: str) -> tuple[dict | None, str | None]:
    params = {"player": player.strip(), "platform": platform, "version": "5"}
    headers = {"Authorization": api_key}
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(APEX_BRIDGE_URL, params=params, headers=headers) as resp:
            try:
                data = await resp.json(content_type=None)
            except Exception:
                return None, f"APIの応答を解析できませんでした (HTTP {resp.status})。"

            if not isinstance(data, dict):
                return None, "想定外のAPI応答です。"

            err = data.get("Error") or data.get("error")
            if err:
                return None, str(err)

            if resp.status == 404:
                return None, "プレイヤーが見つかりません。PCの場合は **EAアカウント名** を確認してください。"
            if resp.status == 403:
                return None, "APIキーが無効です。`APEX_LEGENDS_API_KEY` を確認してください。"
            if resp.status == 429:
                return None, "APIのレート制限です。しばらく待ってから再度お試しください。"
            if resp.status >= 400:
                return None, f"APIエラー (HTTP {resp.status})。"

            return data, None


async def _get_json(session: aiohttp.ClientSession, path: str, api_key: str) -> dict[str, Any] | None:
    url = f"{APEX_BASE}{path}"
    headers = {"Authorization": api_key}
    async with session.get(url, headers=headers) as resp:
        try:
            data = await resp.json(content_type=None)
        except Exception:
            return None
        if isinstance(data, dict) and (data.get("Error") or data.get("error")):
            return None
        if resp.status >= 400:
            return None
        return data if isinstance(data, dict) else None


async def fetch_map_and_craft(api_key: str) -> tuple[dict | None, dict | None]:
    timeout = aiohttp.ClientTimeout(total=25)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        map_task = asyncio.create_task(
            _get_json(session, "/maprotation?version=2", api_key)
        )
        craft_task = asyncio.create_task(
            _get_json(session, "/crafting", api_key)
        )
        m, c = await asyncio.gather(map_task, craft_task)
        return m, c


def fingerprint_map_craft(map_data: dict | None, craft_data: dict | None) -> str:
    blob = json.dumps({"m": map_data, "c": craft_data}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def format_map_craft_message(map_data: dict | None, craft_data: dict | None) -> str:
    lines: list[str] = ["**Apex マップ / クラフト更新**"]

    if map_data:
        ranked = map_data.get("ranked") or {}
        cur = ranked.get("current") or {}
        nxt = ranked.get("next") or {}
        lines.append(
            f"**ランクマ** 現在: **{cur.get('map', '?')}** （残り ~{cur.get('remainingMins', '?')} 分）"
        )
        lines.append(f"　次: **{nxt.get('map', '?')}**")
        br = map_data.get("battle_royale") or {}
        bcur = br.get("current") or {}
        lines.append(
            f"**パブ** 現在: **{bcur.get('map', '?')}** （残り ~{bcur.get('remainingMins', '?')} 分）"
        )
    else:
        lines.append("（マップローテの取得に失敗）")

    lines.append("")

    if craft_data:
        daily = craft_data.get("daily") or craft_data.get("dailyBundles") or []
        weekly = craft_data.get("weekly") or craft_data.get("weeklyBundles") or []
        if isinstance(daily, list) and daily:
            parts = []
            for item in daily[:4]:
                if isinstance(item, dict):
                    parts.append(item.get("itemType", {}).get("name") or item.get("name") or str(item))
                else:
                    parts.append(str(item))
            lines.append("**今日のクラフト（日替わり）**: " + " / ".join(parts))
        if isinstance(weekly, list) and weekly:
            parts = []
            for item in weekly[:4]:
                if isinstance(item, dict):
                    parts.append(item.get("itemType", {}).get("name") or item.get("name") or str(item))
                else:
                    parts.append(str(item))
            lines.append("**週替わり**: " + " / ".join(parts))
        if len(lines) <= 3:
            lines.append(f"```json\n{json.dumps(craft_data, ensure_ascii=False)[:1500]}\n```")
    else:
        lines.append("（クラフトの取得に失敗）")

    lines.append("")
    lines.append("Data from [apexlegendsstatus.com](https://apexlegendsstatus.com/)")
    return "\n".join(lines)


_ROSTER_LINE = re.compile(
    r"^\s*(\d{17,20})\s*[\|｜,]\s*(PC|PS4|X1)\s*[\|｜,]\s*(.+?)\s*$",
    re.IGNORECASE,
)
_ROSTER_MENTION = re.compile(
    r"^\s*<@!?(\d{17,20})>\s+(PC|PS4|X1)\s+(.+?)\s*$",
    re.IGNORECASE,
)


def parse_roster_line(line: str) -> dict[str, Any] | None:
    s = line.strip()
    if not s or s.startswith("#") or s.startswith("//"):
        return None
    m = _ROSTER_LINE.match(s)
    if m:
        plat = m.group(2).upper()
        if plat not in ("PC", "PS4", "X1"):
            return None
        return {
            "discord_id": int(m.group(1)),
            "platform": plat,
            "apex_name": m.group(3).strip(),
        }
    m = _ROSTER_MENTION.match(s)
    if m:
        plat = m.group(2).upper()
        if plat not in ("PC", "PS4", "X1"):
            return None
        return {
            "discord_id": int(m.group(1)),
            "platform": plat,
            "apex_name": m.group(3).strip(),
        }
    return None


def parse_roster_text(text: str) -> dict[int, dict[str, Any]]:
    """Multiple lines; later lines overwrite same discord_id."""
    out: dict[int, dict[str, Any]] = {}
    for line in text.splitlines():
        row = parse_roster_line(line)
        if row:
            out[row["discord_id"]] = row
    return out


async def load_roster_from_channel(guild: discord.Guild) -> list[dict[str, Any]]:
    ch = discord.utils.get(guild.text_channels, name=ROSTER_CHANNEL_NAME)
    if ch is None:
        return []

    merged: dict[int, dict[str, Any]] = {}
    async for msg in ch.history(limit=200, oldest_first=True):
        if msg.author.bot:
            continue
        for line in msg.content.splitlines():
            row = parse_roster_line(line)
            if row:
                merged[row["discord_id"]] = row
    return list(merged.values())


async def get_roster(guild: discord.Guild, *, force_reload: bool = False) -> list[dict[str, Any]]:
    gid = guild.id
    now = time.monotonic()
    if not force_reload and gid in _roster_cache:
        entries, ts = _roster_cache[gid]
        if now - ts < ROSTER_CACHE_TTL_SEC:
            return entries
    entries = await load_roster_from_channel(guild)
    _roster_cache[gid] = (entries, now)
    return entries


def invalidate_roster_cache(guild_id: int) -> None:
    _roster_cache.pop(guild_id, None)


def find_roster_entry(entries: list[dict[str, Any]], discord_user_id: int) -> dict[str, Any] | None:
    for e in entries:
        if e.get("discord_id") == discord_user_id:
            return e
    return None


def br_rank_name_from_bridge(data: dict[str, Any]) -> str | None:
    glob = data.get("global") or {}
    rank = glob.get("rank") or {}
    name = rank.get("rankName")
    return str(name) if name else None


def extract_total_kills_damage(data: dict[str, Any]) -> tuple[int | None, int | None]:
    total = data.get("total") or {}
    kills = total.get("kills")
    dmg = total.get("damage")
    if isinstance(kills, dict):
        kills = kills.get("value")
    if isinstance(dmg, dict):
        dmg = dmg.get("value")
    try:
        ki = int(float(kills)) if kills is not None else None
    except (TypeError, ValueError):
        ki = None
    try:
        di = int(float(dmg)) if dmg is not None else None
    except (TypeError, ValueError):
        di = None
    return ki, di


def extract_selected_legend_trackers(data: dict[str, Any], limit: int = 8) -> list[tuple[str, str]]:
    legends = data.get("legends") or {}
    selected = legends.get("selected")
    rows: list[tuple[str, str]] = []
    if not isinstance(selected, dict):
        return rows

    data_list = selected.get("data")
    if isinstance(data_list, list) and data_list:
        leg = (
            selected.get("LegendName")
            or selected.get("legendName")
            or selected.get("Legend")
            or ""
        )
        prefix = f"{leg} · " if leg else ""
        for tr in data_list:
            if not isinstance(tr, dict):
                continue
            label = tr.get("name") or tr.get("key") or "?"
            val = tr.get("value")
            if val is None:
                continue
            rows.append((f"{prefix}{label}", str(val)))
            if len(rows) >= limit:
                return rows
        return rows

    for legend_name, blob in selected.items():
        if not isinstance(blob, dict) or legend_name in ("Data", "ImgAssets", "gameInfo"):
            continue
        for tr in blob.get("data") or []:
            if not isinstance(tr, dict):
                continue
            label = tr.get("name") or tr.get("key") or "?"
            val = tr.get("value")
            if val is None:
                continue
            rows.append((f"{legend_name} · {label}", str(val)))
            if len(rows) >= limit:
                return rows
    return rows


def build_stats_embed(data: dict[str, Any], *, title_prefix: str = "") -> discord.Embed:
    glob = data.get("global") or {}
    name = glob.get("name") or "Player"
    uid = glob.get("uid")
    api_platform = glob.get("platform") or "?"
    level = glob.get("level")
    rank = glob.get("rank") or {}
    rscore = rank.get("rankScore")
    rname = rank.get("rankName") or "—"
    rdiv = rank.get("rankDiv")
    ladder = rank.get("ladderPosPlatform")

    div_str = f" {rdiv}" if rdiv not in (None, "") else ""
    profile_url = f"https://apexlegendsstatus.com/profile/uid/{api_platform}/{uid}" if uid else ALS_SITE

    if rscore is None:
        rp_display = "—"
    else:
        try:
            rp_display = f"**{int(float(rscore)):,}**"
        except (TypeError, ValueError):
            rp_display = str(rscore)

    title = f"{title_prefix}{name}" if title_prefix else f"📊 {name}"
    embed = discord.Embed(title=title, url=profile_url, color=0xDA292A)
    embed.add_field(name="RP", value=rp_display, inline=True)
    embed.add_field(name="ランク", value=f"{rname}{div_str}", inline=True)
    embed.add_field(name="レベル", value=str(level) if level is not None else "—", inline=True)
    if ladder is not None:
        embed.add_field(name="プラットフォーム内順位", value=str(ladder), inline=True)

    kills, dmg = extract_total_kills_damage(data)
    if kills is not None:
        embed.add_field(name="累計キル", value=f"{kills:,}", inline=True)
    if dmg is not None:
        embed.add_field(name="累計ダメージ", value=f"{dmg:,}", inline=True)

    trs = extract_selected_legend_trackers(data, limit=10)
    if trs:
        chunk = "\n".join(f"・{k}: **{v}**" for k, v in trs[:10])
        embed.add_field(name="選択レジェンド（トラッカー抜粋）", value=chunk[:1024], inline=False)

    embed.description = (
        f"プラットフォーム: **{api_platform}**\n"
        f"[Apex Legends Status で開く]({profile_url})"
    )
    embed.set_footer(text="Data provided by Apex Legends Status")
    return embed


async def apply_br_rank_roles(
    member: discord.Member,
    bridge_data: dict[str, Any],
) -> tuple[bool, str]:
    """Remove other rank roles in the map, add role for current BR rankName."""
    rank_en = br_rank_name_from_bridge(bridge_data)
    if not rank_en:
        return False, "ランク情報がありません。"

    role_map = _rank_role_map()
    target_role_name = role_map.get(rank_en)
    if not target_role_name:
        return False, f"APIランク「{rank_en}」に対応するロール名が設定にありません。`APEX_RANK_ROLE_MAP` で追加してください。"

    target_role = discord.utils.get(member.guild.roles, name=target_role_name)
    if target_role is None:
        return False, f"ロール **{target_role_name}** がサーバーに存在しません。"

    managed_names = _all_managed_rank_role_names()
    to_remove = [r for r in member.roles if r.name in managed_names and r != target_role]

    bot_me = member.guild.me
    if bot_me is None:
        return False, "Bot情報を取得できません。"

    if target_role >= bot_me.top_role:
        return False, f"Botのロールより上に **{target_role_name}** があるため付与できません。"

    try:
        if to_remove:
            await member.remove_roles(*to_remove, reason="Apex BR rank sync")
        if target_role not in member.roles:
            await member.add_roles(target_role, reason="Apex BR rank sync")
    except discord.Forbidden:
        return False, "ロール変更の権限がありません（Botに「ロールの管理」と階層を確認）。"
    except discord.HTTPException as e:
        return False, f"Discord API エラー: {e}"

    return True, f"BRランク **{rank_en}** → ロール **{target_role_name}** に同期しました。"


async def sync_rank_roles_from_api(member: discord.Member, api_key: str) -> tuple[bool, str]:
    entries = await get_roster(member.guild)
    row = find_roster_entry(entries, member.id)
    if not row:
        return False, f"`#{ROSTER_CHANNEL_NAME}` にこのユーザーの行がありません（形式: `DiscordID|PC|Apex名`）。"
    data, err = await fetch_apex_bridge(row["apex_name"], row["platform"], api_key)
    if err or not data:
        return False, err or "APIエラー"
    return await apply_br_rank_roles(member, data)


def touch_map_craft_fingerprint(map_data: dict | None, craft_data: dict | None) -> bool:
    """Return True if changed from last call (updates global fingerprint)."""
    global _last_map_craft_fingerprint
    fp = fingerprint_map_craft(map_data, craft_data)
    if fp != _last_map_craft_fingerprint:
        _last_map_craft_fingerprint = fp
        return True
    return False


async def build_clan_rank_rows(
    guild: discord.Guild,
    api_key: str,
    metric: str,
) -> tuple[list[dict[str, Any]], str | None]:
    """metric: ``rp`` | ``kills``. Respects Mozambique rate limits between calls."""
    entries = await get_roster(guild)
    if not entries:
        return [], f"`#{ROSTER_CHANNEL_NAME}` に有効な行がありません（例: `123456789012345678|PC|YourEAName`）。"

    rows: list[dict[str, Any]] = []
    for e in entries:
        data, err = await fetch_apex_bridge(e["apex_name"], e["platform"], api_key)
        did = int(e["discord_id"])
        member = guild.get_member(did)
        mention = member.mention if member else f"<@{did}>"
        display = member.display_name if member else str(did)
        if err or not data:
            rows.append(
                {
                    "discord_id": did,
                    "mention": mention,
                    "display": display,
                    "error": err or "?",
                    "rp": None,
                    "kills": None,
                    "rank_name": None,
                }
            )
        else:
            glob = data.get("global") or {}
            rank = glob.get("rank") or {}
            rs = rank.get("rankScore")
            try:
                rp = int(float(rs)) if rs is not None else None
            except (TypeError, ValueError):
                rp = None
            kills, _ = extract_total_kills_damage(data)
            rows.append(
                {
                    "discord_id": did,
                    "mention": mention,
                    "display": display,
                    "error": None,
                    "rp": rp,
                    "kills": kills,
                    "rank_name": br_rank_name_from_bridge(data),
                }
            )
        await asyncio.sleep(2.2)

    def sort_key(r: dict[str, Any]):
        if metric == "kills":
            v = r.get("kills")
            return (r.get("error") is not None, v is None, -(v or 0))
        v = r.get("rp")
        return (r.get("error") is not None, v is None, -(v or 0))

    rows.sort(key=sort_key)
    return rows, None
