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
import asyncio
import traceback


app = Flask(__name__)

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    # use_reloader=False avoids double-start in some hosts
    app.run(host='0.0.0.0', port=port, use_reloader=False)

def keep_alive():
    t = Thread(target=run_flask, daemon=True)
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

# モデル初期化（ここで一度だけ初期化）
try:
    model = genai.GenerativeModel("gemini-2.5-flash-lite")
except Exception as e:
    print("モデル初期化エラー, フォールバックします:", e)
    model = genai.GenerativeModel("models/chat-bison-001")  # フォールバック

# 性格プロンプト
PERSONALITY = {
    "boke": "あなたは明るくてボケ担当のAIです。ユーザーの発言に対して面白くズレた返答をしてください。",
    "tsundere": "あなたはツンデレAIです。少し冷たくしつつも、内心は優しい返答をしてください。",
    "genki": "あなたは超元気で明るいAIです。テンション高めで楽しく返答してください。",
    "yandere": "あなたはヤンデレAIです。優しいけれど独占欲が強く、少し怖い雰囲気で返答してください。",
    "ojousama": "あなたは上品なお嬢様AIです。おしとやかで優雅な口調で返答してください。",
    "robot": "あなたは無機質で機械的なAIです。感情を排除し、論理的に返答してください。",
    "sarcastic": "あなたは皮肉屋AIです。少し毒のあるユーモアで返答してください。",
    "kansai": "あなたは関西弁AIです。ノリよくツッコミを交えながら返答してください。"
}
MODES = list(PERSONALITY.keys())
# 日本時間
JST = pytz.timezone("Asia/Tokyo")

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

# 管理者用チェックコマンド
@bot.tree.command(name="check_genai", description="genai SDK と利用可能モデルを確認します（管理者用）")
@app_commands.checks.has_permissions(administrator=True)
async def check_genai(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)

    def sync_check():
        import google.generativeai as genai
        out = []
        out.append(f"genai version: {getattr(genai, '__version__', 'unknown')}")
        try:
            models = genai.list_models()
            names = []
            for m in models:
                name = getattr(m, "name", None) or getattr(m, "model", None) or str(m)
                names.append(name)
            out.append("available models: " + ", ".join(names))
        except Exception as e:
            out.append(f"list_models error: {type(e).__name__} {e}")
        return "\n".join(out)

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, sync_check)
    # 長い場合は分割して送る
    for i in range(0, len(result), 1900):
        await interaction.followup.send(result[i:i+1900], ephemeral=True)

# -----------------------------
# ここから AI 会話機能
# -----------------------------

# /mode コマンド
@bot.tree.command(name="mode", description="AIの性格をランダムで決めます")
async def mode(interaction: discord.Interaction):
    user_id = interaction.user.id
    selected = random.choice(MODES)

    if user_id not in user_sessions:
        user_sessions[user_id] = {
            "history": [],
            "mode": selected,
            "chat": model.start_chat(history=[])
        }
    else:
        user_sessions[user_id]["mode"] = selected
        user_sessions[user_id]["chat"] = model.start_chat(history=[])

    await interaction.response.send_message(f"あなたのAIモードは **{selected}** に決定したよ！")

# /reset コマンド
@bot.tree.command(name="reset", description="AIとの会話をリセットします")
async def reset(interaction: discord.Interaction):
    user_id = interaction.user.id
    if user_id in user_sessions:
        user_sessions[user_id]["history"] = []
    await interaction.response.send_message("会話をリセットしたよ！")

# /ai コマンド
@bot.tree.command(name="ai", description="AIと会話します")
async def ai(interaction: discord.Interaction, prompt: str):
    await interaction.response.defer(thinking=True)

    user_id = interaction.user.id

    if user_id not in user_sessions:
        user_sessions[user_id] = {
            "mode": "boke",
            "history": []
        }

    session = user_sessions[user_id]
    mode = session["mode"]

    chat = model.start_chat(history=[])

    # personality（1回目）
    try:
        await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: chat.send_message(
                PERSONALITY[mode],
                request_options={"timeout": 60}
            )
        )
    except Exception as e:
        print("personality send error:", e)
        await interaction.followup.send("⚠️ AI の初期化に失敗しました。時間をおいて再試行してください。")
        return

    # personality（2回目）← ここがクラッシュしてた
    try:
        await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: chat.send_message(
                PERSONALITY[mode],
                request_options={"timeout": 60}
            )
        )
    except Exception as e:
        print("quota error:", e)
        await interaction.followup.send("⚠️ 現在AIの利用上限に達しています。しばらくしてからもう一度試してください。")
        return

    # prompt を送る
    try:
        response = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: chat.send_message(
                prompt,
                request_options={"timeout": 60}
            )
        )
    except Exception as e:
        print("chat send error:", e)
        await interaction.followup.send("⚠️ AI 応答の取得に失敗しました。時間をおいて再試行してください。")
        return

    # テキスト抽出
    text = getattr(response, "text", None)
    if not text:
        try:
            candidates = getattr(response, "candidates", None)
            if candidates and len(candidates) > 0:
                text = getattr(candidates[0], "content", None) or str(candidates[0])
        except Exception:
            text = None
    if not text:
        text = str(response)

    reply = (
        f"👤 **{interaction.user.display_name}**: {prompt}\n"
        f"🤖 **AI（{mode}）**: {text}"
    )

    await interaction.followup.send(reply)

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