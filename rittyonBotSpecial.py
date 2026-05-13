import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
import datetime
import pytz
from flask import Flask
from threading import Thread
import re
import asyncio
from typing import Optional

import apex_helpers as apex
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# 入隊フロー用セッション
apply_sessions = {}

def extract_seasons(text):
    z2h = str.maketrans('０１２３４５６７８９', '0123456789')
    text = text.translate(z2h)
    nums = re.findall(r'\d+', text)
    return [int(n) for n in nums]

def check_master_seasons(seasons):
    if seasons == [17]:
        return True
    if 17 in seasons and len(seasons) == 2:
        return True
    if len(seasons) == 1:
        return True
    return False

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, use_reloader=False)

def keep_alive():
    t = Thread(target=run_flask, daemon=True)
    t.start()

@app.route('/', methods=['GET', 'HEAD'])
def home():
    return "I'm alive!", 200

TOKEN = os.environ["DISCORD_TOKEN"]

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

target_channel_id = None
welcome_enabled = True


async def _maybe_apex_role_sync_on_join(member: discord.Member):
    await asyncio.sleep(5)
    try:
        key = apex.get_api_key()
        if not key or not member.guild:
            return
        entries = await apex.get_roster(member.guild)
        if not apex.find_roster_entry(entries, member.id):
            return
        ok, msg = await apex.sync_rank_roles_from_api(member, key)
        if not ok:
            print(f"[apex join sync] {member.id}: {msg}")
    except Exception as e:
        print(f"[apex join sync] error: {e}")


async def try_post_map_craft_update():
    api_key = apex.get_api_key()
    raw = (os.environ.get("APEX_MAP_CRAFT_CHANNEL_ID") or "").strip()
    if not api_key or not raw:
        return
    try:
        cid = int(raw)
    except ValueError:
        return
    m, c = await apex.fetch_map_and_craft(api_key)
    if not apex.touch_map_craft_fingerprint(m, c):
        return
    ch = bot.get_channel(cid)
    if ch and isinstance(ch, discord.TextChannel):
        try:
            await ch.send(apex.format_map_craft_message(m, c))
        except discord.HTTPException as e:
            print(f"map/craft post failed: {e}")


@tasks.loop(minutes=20)
async def poll_map_craft_loop():
    await try_post_map_craft_update()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    keep_alive()
    send_daily_message.start()
    if not poll_map_craft_loop.is_running():
        poll_map_craft_loop.start()

    async def _delayed_first_map_craft():
        await asyncio.sleep(20)
        await try_post_map_craft_update()

    asyncio.create_task(_delayed_first_map_craft())
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} global commands")
    except Exception as e:
        print(e)

@bot.tree.command(name="setchannel", description="毎日19時に送信するチャンネルを設定します")
async def setchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    global target_channel_id
    target_channel_id = channel.id
    await interaction.response.send_message(f"送信先チャンネルを **{channel.mention}** に設定しました。")

@bot.tree.command(name="welcome_on", description="参加者自動チャンネル作成を有効化します（管理者専用）")
@app_commands.checks.has_permissions(administrator=True)
async def welcome_on(interaction: discord.Interaction):
    global welcome_enabled
    welcome_enabled = True
    await interaction.response.send_message("✅ 自動ウェルカムチャンネル作成を **有効化** しました。", ephemeral=True)

@bot.tree.command(name="welcome_off", description="参加者自動チャンネル作成を無効化します（管理者専用）")
@app_commands.checks.has_permissions(administrator=True)
async def welcome_off(interaction: discord.Interaction):
    global welcome_enabled
    welcome_enabled = False
    await interaction.response.send_message("⛔ 自動ウェルカムチャンネル作成を **無効化** しました。", ephemeral=True)

@bot.tree.command(name="help", description="このボットの使い方を表示します")
@app_commands.describe(ephemeral="はいにすると自分にだけ表示")
async def help_command(interaction: discord.Interaction, ephemeral: bool = False):
    ch = apex.ROSTER_CHANNEL_NAME
    embed = discord.Embed(
        title="📖 Rittyon Bot 使い方",
        color=0x5865F2,
        description=(
            f"**ロスター登録（`#{ch}`）**\n"
            "管理者が **1行1人** で入力します。Discord と Apex を結びつけます。\n"
            f"• `Discordの数値ID,PC,EAアカウント名` 例: `123456789012345678,PC,YourEAName`\n"
            "• `|` 区切りでも可\n"
            "• メンションでも可: `<@123456789012345678> PC EA名`（ユーザーをメンションしてから続きを書く）\n"
            "• PC の名前は **EA アカウント名**（Steam 表示名と違うことがあります）\n"
            "• 編集後は `/apex_roster_reload`（管理者）か、しばらく待つと再読込されます\n\n"
            "**Apex（要 API キー）**\n"
            "• `/apex_stats` … ロスター登録済みの **今の戦績**（省略時は自分）\n"
            "• `/apex_rp` … 名前と PF を毎回指定して戦績（未登録でも可）\n"
            "• `/apex_clan_rank` … ロスター全員の **RP か累計キル** ランキング（時間がかかります）\n"
            "• `/apex_sync_roles` … **今の BR ランク**に合わせてランクロールを付け替え（省略時は自分）\n"
            "• `/apex_sync_all_roles` … ロスター全員を一括同期（**管理者**・所要時間大）\n"
            f"• `/apex_roster_reload` … `#{ch}` を強制再読込（**管理者**）\n\n"
            "**クラン・集合**\n"
            "• `/setchannel` … 毎日 **19時 JST** に出欠案内を送るチャンネルを設定\n"
            "• `/welcome_on` / `/welcome_off` … 新規向けウェルカム自動作成（**管理者**）\n\n"
            "**自動で動くこと**\n"
            "• メンバー参加時: ロスターにいれば **ランクロール同期**（数秒後）\n"
            "• マップ／クラフト: 環境変数 `APEX_MAP_CRAFT_CHANNEL_ID` があるとき、**変化時**に投稿（約20分ごと）\n\n"
            "データ: [Apex Legends Status](https://apexlegendsstatus.com/) 系 API\n"
            "このメッセージ: オプション **ephemeral** をオンにすると自分にだけ表示"
        ),
    )
    embed.set_footer(text="困ったら管理者に聞いてね")
    await interaction.response.send_message(embed=embed, ephemeral=ephemeral)

@bot.tree.command(name="apex_roster_reload", description="`#apexid` チャンネルからロスターを再読込します（管理者）")
@app_commands.checks.has_permissions(administrator=True)
async def apex_roster_reload(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("サーバー内でのみ使えます。", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    apex.invalidate_roster_cache(interaction.guild.id)
    entries = await apex.get_roster(interaction.guild, force_reload=True)
    await interaction.followup.send(
        f"✅ 読み込み完了: **{len(entries)}** 件（`#{apex.ROSTER_CHANNEL_NAME}`）\n"
        "1行形式: `DiscordユーザーID|PC|Apex名` または `<@ID> PC Apex名`",
        ephemeral=True,
    )

@bot.tree.command(name="apex_stats", description="登録済みメンバーの今の戦績（RP・ランク・累計キル等）を表示")
@app_commands.describe(target="省略時は自分")
async def apex_stats(interaction: discord.Interaction, target: Optional[discord.Member] = None):
    api_key = apex.get_api_key()
    if not api_key:
        await interaction.response.send_message(
            "`APEX_LEGENDS_API_KEY` が未設定です。",
            ephemeral=True,
        )
        return
    if not interaction.guild:
        await interaction.response.send_message("サーバー内でのみ使えます。", ephemeral=True)
        return

    member = target or interaction.user
    if not isinstance(member, discord.Member):
        member = interaction.guild.get_member(member.id) or interaction.guild.get_member(interaction.user.id)
    if member is None:
        await interaction.response.send_message("メンバーを取得できませんでした。", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    entries = await apex.get_roster(interaction.guild)
    row = apex.find_roster_entry(entries, member.id)
    if not row:
        await interaction.followup.send(
            f"{member.mention} は `#{apex.ROSTER_CHANNEL_NAME}` に未登録です。\n"
            "管理者が `DiscordID|PC|EA名` 形式で1行追加してください。",
            ephemeral=True,
        )
        return

    data, err = await apex.fetch_apex_bridge(row["apex_name"], row["platform"], api_key)
    if err or not data:
        await interaction.followup.send(f"❌ {err or 'APIエラー'}", ephemeral=True)
        return

    embed = apex.build_stats_embed(data)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="apex_clan_rank", description="`#apexid` の全員をAPIで取り、RPまたは累計キルでランキング表示")
@app_commands.describe(metric="並び順の指標")
@app_commands.choices(metric=[
    app_commands.Choice(name="BRのRP", value="rp"),
    app_commands.Choice(name="累計キル", value="kills"),
])
async def apex_clan_rank(interaction: discord.Interaction, metric: app_commands.Choice[str]):
    api_key = apex.get_api_key()
    if not api_key:
        await interaction.response.send_message("`APEX_LEGENDS_API_KEY` が未設定です。", ephemeral=True)
        return
    if not interaction.guild:
        await interaction.response.send_message("サーバー内でのみ使えます。", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    rows, err = await apex.build_clan_rank_rows(interaction.guild, api_key, metric.value)
    if err:
        await interaction.followup.send(f"❌ {err}", ephemeral=True)
        return

    lines = []
    medals = ["🥇", "🥈", "🥉"]
    for i, r in enumerate(rows[:20]):
        med = medals[i] if i < 3 else f"{i + 1}."
        if r.get("error"):
            lines.append(f"{med} {r['mention']} — ⚠️ {r['error']}")
        elif metric.value == "kills":
            kv = r.get("kills")
            rk = r.get("rank_name") or "?"
            if kv is not None:
                lines.append(f"{med} {r['mention']} **キル {kv:,}** （{rk}）")
            else:
                lines.append(f"{med} {r['mention']} キル — （{rk}）")
        else:
            rv = r.get("rp")
            rk = r.get("rank_name") or "?"
            if rv is not None:
                lines.append(f"{med} {r['mention']} **RP {rv:,}** （{rk}）")
            else:
                lines.append(f"{med} {r['mention']} RP — （{rk}）")

    body = "\n".join(lines) if lines else "（データなし）"
    embed = discord.Embed(
        title=f"クランランキング（{'RP' if metric.value == 'rp' else '累計キル'}）",
        description=body[:4000],
        color=0xDA292A,
    )
    embed.set_footer(text="Data provided by Apex Legends Status")
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="apex_sync_roles", description="APIのBRランクに合わせてランクロールを付け替え（`#apexid` 登録者）")
@app_commands.describe(target="省略時は自分。他人を指定する場合はロール管理権限が必要です")
async def apex_sync_roles(interaction: discord.Interaction, target: Optional[discord.Member] = None):
    api_key = apex.get_api_key()
    if not api_key:
        await interaction.response.send_message("`APEX_LEGENDS_API_KEY` が未設定です。", ephemeral=True)
        return
    if not interaction.guild:
        await interaction.response.send_message("サーバー内でのみ使えます。", ephemeral=True)
        return

    subject = target or interaction.user
    if not isinstance(subject, discord.Member):
        subject = interaction.guild.get_member(subject.id)
    if subject is None:
        await interaction.response.send_message("メンバーを取得できませんでした。", ephemeral=True)
        return

    if target is not None and target.id != interaction.user.id:
        if not interaction.user.guild_permissions.manage_roles:
            await interaction.response.send_message("他人の同期には **ロールの管理** 権限が必要です。", ephemeral=True)
            return

    await interaction.response.defer(ephemeral=True, thinking=True)
    ok, msg = await apex.sync_rank_roles_from_api(subject, api_key)
    await interaction.followup.send(("✅ " if ok else "❌ ") + msg, ephemeral=True)

@bot.tree.command(name="apex_sync_all_roles", description="ロスター全員のランクロールをAPIで一括同期（管理者・時間がかかります）")
@app_commands.checks.has_permissions(administrator=True)
async def apex_sync_all_roles(interaction: discord.Interaction):
    api_key = apex.get_api_key()
    if not api_key:
        await interaction.response.send_message("`APEX_LEGENDS_API_KEY` が未設定です。", ephemeral=True)
        return
    if not interaction.guild:
        await interaction.response.send_message("サーバー内でのみ使えます。", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    entries = await apex.get_roster(interaction.guild, force_reload=True)
    ok_n = err_n = 0
    err_samples: list[str] = []
    for e in entries:
        m = interaction.guild.get_member(int(e["discord_id"]))
        if not m:
            err_n += 1
            if len(err_samples) < 3:
                err_samples.append(f"ID {e['discord_id']}: サーバーに不在")
            await asyncio.sleep(0.2)
            continue
        ok, msg = await apex.sync_rank_roles_from_api(m, api_key)
        if ok:
            ok_n += 1
        else:
            err_n += 1
            if len(err_samples) < 5:
                err_samples.append(f"{m.display_name}: {msg}")
        await asyncio.sleep(2.2)

    extra = "\n".join(err_samples) if err_samples else "なし"
    await interaction.followup.send(
        f"完了: 成功 **{ok_n}** / 失敗 **{err_n}**\n失敗例:\n{extra[:1800]}",
        ephemeral=True,
    )

@bot.tree.command(name="apex_rp", description="Apex BRランクのRPを表示（Apex Legends Status と同系の Mozambique API）")
@app_commands.describe(
    ingame_name="ゲーム内のユーザー名（PCは EA アカウント名。Steam連携時はEA側の名前）",
    platform="プラットフォーム",
)
@app_commands.choices(platform=[
    app_commands.Choice(name="PC", value="PC"),
    app_commands.Choice(name="PlayStation", value="PS4"),
    app_commands.Choice(name="Xbox", value="X1"),
])
async def apex_rp(interaction: discord.Interaction, ingame_name: str, platform: app_commands.Choice[str]):
    api_key = apex.get_api_key()
    if not api_key:
        await interaction.response.send_message(
            "環境変数 **`APEX_LEGENDS_API_KEY`** が未設定です。"
            "[portal.apexlegendsapi.com](https://portal.apexlegendsapi.com/) で無料キーを発行し、`.env` に追記してください。",
            ephemeral=True,
        )
        return

    await interaction.response.defer(thinking=True)
    data, err = await apex.fetch_apex_bridge(ingame_name, platform.value, api_key)
    if err:
        await interaction.followup.send(f"❌ {err}", ephemeral=True)
        return

    embed = apex.build_stats_embed(data, title_prefix="🏅 ")
    await interaction.followup.send(embed=embed)

@tasks.loop(minutes=1)
async def send_daily_message():
    global target_channel_id
    if target_channel_id is None:
        return

    JST = pytz.timezone("Asia/Tokyo")
    now = datetime.datetime.now(JST)
    if now.hour == 19 and now.minute == 0:
        channel = bot.get_channel(target_channel_id)
        if channel:
            await channel.send(
                "@everyone\n"
                "20時👍\n"
                "21時⭕\n"
                "22時😎\n"
                "観戦👀\n"
                "参加不可❌"
            )

@bot.event
async def on_member_join(member):
    asyncio.create_task(_maybe_apex_role_sync_on_join(member))

    global welcome_enabled
    if not welcome_enabled:
        return

    guild = member.guild
    admin_role = discord.utils.get(guild.roles, name="管理者")
    bot_member = guild.me

    safe_name = re.sub(r'[^a-zA-Z0-9\-]', '-', member.name)
    channel_name = f"welcome-{safe_name}"

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        member: discord.PermissionOverwrite(view_channel=True),
        admin_role: discord.PermissionOverwrite(view_channel=True) if admin_role else discord.PermissionOverwrite(view_channel=True),
        bot_member: discord.PermissionOverwrite(view_channel=True, send_messages=True)
    }

    try:
        channel = await guild.create_text_channel(name=channel_name, overwrites=overwrites)
    except Exception as e:
        print(f"チャンネル作成エラー: {e}")
        return

    apply_sessions[member.id] = {
        "step": 1,
        "answers": {},
        "images": [],
        "channel_id": channel.id
    }

    await channel.send(
        f"""{member.mention} さん、参加ありがとうございます！🎉

以下の項目を教えてください：

・年齢  
・プラットフォーム  
・最高ランク帯（シーズンまで記載ください）  
・現在のランク帯  
・参加率  

まずはこちら教えてください！"""
    )

    general_channel = discord.utils.get(guild.text_channels, name="一般")
    if general_channel:
        await general_channel.send(
            f"{member.mention} さん、ようこそ！🎉\nこちらのチャンネルで自己紹介をお願いします：\n{channel.mention}"
        )

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if message.guild and message.channel.name == apex.ROSTER_CHANNEL_NAME:
        apex.invalidate_roster_cache(message.guild.id)

    user_id = message.author.id

    if user_id in apply_sessions:
        session = apply_sessions[user_id]

        if message.channel.id != session.get("channel_id"):
            await bot.process_commands(message)
            return

        step = session["step"]

        if step == 1:
            session["answers"]["basic"] = message.content
            session["step"] = 2
            await message.channel.send(
                "ありがとうございます！\n"
                "次に、ランクバッジのスクショを貼ってください。\n"
                "複数枚ある場合はすべて貼ったあとに **「完了」** と送ってください。"
            )
            return

        if step == 2:
            if message.content.lower() == "完了":
                session["step"] = 3
                await message.channel.send(
                    "ありがとうございます！\n"
                    "次の質問です。\n\n"
                    "当クランでは **週3日以上・1日2時間以上** の参加をお願いしています。\n"
                    "この条件で問題ありませんか？（はい / いいえ）"
                )
                return

            if message.attachments:
                for att in message.attachments:
                    session["images"].append(att.url)
                await message.channel.send("画像を受け取りました。他にもあれば続けて送ってください。完了したら「完了」と送ってください。")
                return

            await message.channel.send("画像を送るか、完了と入力してください。")
            return

        if step == 3:
            text = message.content.strip().lower()
            if any(k in text for k in ["はい", "ok", "大丈夫", "問題ない"]):
                session["step"] = 4
                await message.channel.send(
                    "ありがとうございます！\n"
                    "次の質問です。\n\n"
                    "マスター以上を経験していますか？（はい / いいえ）"
                )
                return
            elif any(k in text for k in ["いいえ", "無理", "できない"]):
                await message.channel.send("申し訳ありませんが、参加条件を満たさないため入隊をお断りさせていただきます。")
                del apply_sessions[user_id]
                return
            else:
                await message.channel.send("「はい」または「いいえ」で回答してください。")
                return

        if step == 4:
            text = message.content.strip().lower()
            if any(k in text for k in ["いいえ", "no", "ない", "未経験"]):
                session["step"] = 5
                await message.channel.send(
                    "ありがとうございます！\n"
                    "この後、説明会を実施します。\n"
                    "対応可能な日時を教えてください。（例：今日の21時、明日の20〜22時 など）"
                )
                return
            elif any(k in text for k in ["はい", "ある", "経験あり", "ok"]):
                session["step"] = 41
                await message.channel.send(
                    "どのシーズンでマスターを取りましたか？\n"
                    "数字で答えてください。（例：17）\n"
                    "複数ある場合はスペース区切りで入力してください。（例：17 12）"
                )
                return
            else:
                await message.channel.send("「はい」または「いいえ」で回答してください。")
                return

        if step == 41:
            seasons = extract_seasons(message.content)

            if not seasons:
                await message.channel.send("数字で入力してください。（例：17）")
                return

            if not check_master_seasons(seasons):
                await message.channel.send("申し訳ありませんが、当クランの基準に満たないため入隊をお断りさせていただきます。")
                del apply_sessions[user_id]
                return

            session["answers"]["master_seasons"] = seasons
            session["step"] = 5
            await message.channel.send(
                "ありがとうございます！\n"
                "この後、説明会を実施します。\n"
                "対応可能な日時を教えてください。（例：今日の21時、明日の20〜22時 など）"
            )
            return

        if step == 5:
            session["answers"]["meeting"] = message.content

            admin_channel = discord.utils.get(message.guild.text_channels, name="管理者")
            if admin_channel:
                await admin_channel.send(
                    f"【新規入隊希望者】\n"
                    f"ユーザー: {message.author.mention}\n\n"
                    f"--- 基本情報 ---\n"
                    f"{session['answers'].get('basic', '')}\n\n"
                    f"--- ランクバッジ画像 ---\n"
                    + ("\n".join(session["images"]) if session["images"] else "なし") +
                    "\n\n--- マスター経験 ---\n"
                    f"{session['answers'].get('master_seasons', 'なし')}\n\n"
                    f"--- 説明会希望日時 ---\n"
                    f"{session['answers']['meeting']}"
                )

            await message.channel.send(
                "ありがとうございます！\n"
                "管理者に情報を送信しましたので、説明会の日程調整をお待ちください。"
            )

            del apply_sessions[user_id]
            return

    await bot.process_commands(message)

bot.run(TOKEN)
