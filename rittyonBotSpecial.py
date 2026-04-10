import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
import datetime
import pytz
from flask import Flask
from threading import Thread
import random
import asyncio
import traceback
import re

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

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    keep_alive()
    send_daily_message.start()
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
