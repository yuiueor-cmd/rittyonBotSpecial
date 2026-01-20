import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
import datetime
import pytz
from flask import Flask
from threading import Thread
import google.generativeai as genai
import random

MODES = ["boke", "tsundere"]
app = Flask(__name__)

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_flask)
    t.start()

@app.route('/', methods=['GET', 'HEAD'])
def home():
    return "I'm alive!", 200

TOKEN = os.environ["DISCORD_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

genai.configure(api_key=GEMINI_API_KEY)

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# 保存用
target_channel_id = None

# AI 会話セッション保存
user_sessions = {}
model = genai.GenerativeModel("gemini-pro")
# 性格プロンプト
PERSONALITY = {
    "boke": "あなたは明るくてボケ担当のAIです。ユーザーの発言に対して面白くズレた返答をしてください。",
    "tsundere": "あなたはツンデレAIです。少し冷たくしつつも、内心は優しい返答をしてください。"
}

# 日本時間
JST = pytz.timezone("Asia/Tokyo")

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    keep_alive()
    send_daily_message.start()
    try:
        synced = await bot.tree.sync()  # ← グローバル同期に変更
        print(f"Synced {len(synced)} global commands")
    except Exception as e:
        print(e)



# -----------------------------
# ここから AI 会話機能
# -----------------------------

# /mode コマンド
@bot.tree.command(name="mode", description="AIの性格をランダムで決めます")
async def mode(interaction: discord.Interaction):
    user_id = interaction.user.id

    # ランダムでモードを決定
    selected = random.choice(["boke", "tsundere"])

    # セッションがなければ作成
    if user_id not in user_sessions:
        user_sessions[user_id] = {"history": [], "mode": selected}
    else:
        user_sessions[user_id]["mode"] = selected

    await interaction.response.send_message(
        f"あなたのAIモードは **{selected}** に決定したよ！"
    )

# /reset コマンド
@bot.tree.command(name="reset", description="AIとの会話をリセットします")
async def reset(interaction: discord.Interaction):
    user_id = interaction.user.id

    if user_id in user_sessions:
        user_sessions[user_id]["history"] = []

    await interaction.response.send_message("会話をリセットしたよ！")

# /ai コマンド
import asyncio

@bot.tree.command(name="ai", description="AIと会話します")
async def ai(interaction: discord.Interaction, prompt: str):
    await interaction.response.defer()
    user_id = interaction.user.id

    if user_id not in user_sessions:
        user_sessions[user_id] = {"history": [], "mode": "boke"}

    session = user_sessions[user_id]

    # Gemini モデルは外で1回だけ作るのが理想だが、
    # とりあえず今の構造に合わせてここで使う


    messages = [
        {"role": "system", "content": PERSONALITY[session["mode"]]}
    ] + session["history"] + [
        {"role": "user", "content": prompt}
    ]

    # ★ここが非同期化のポイント
    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(
        None,
        lambda: model.generate_content(messages)
    )

    # 履歴に追加
    session["history"].append({"role": "user", "content": prompt})
    session["history"].append({"role": "assistant", "content": response.text})

    await interaction.followup.send(response.text)

# -----------------------------
# ここまで AI 会話機能
# -----------------------------

# スラッシュコマンド：送信先チャンネルを設定
@bot.tree.command(name="setchannel", description="毎日19時に送信するチャンネルを設定します")
async def setchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    global target_channel_id
    target_channel_id = channel.id
    await interaction.response.send_message(f"送信先チャンネルを **{channel.mention}** に設定しました。")

# 毎日19時にメッセージ送信
@tasks.loop(minutes=1)
async def send_daily_message():
    global target_channel_id
    if target_channel_id is None:
        return

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


bot.run(TOKEN)