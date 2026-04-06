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
import re  # ★ 追加：extract_seasons で使う

app = Flask(__name__)

# 入隊フロー用セッション
apply_sessions = {}

def extract_seasons(text):
    # 全角 → 半角
    z2h = str.maketrans('０１２３４５６７８９', '0123456789')
    text = text.translate(z2h)

    # 数字を全部抽出
    nums = re.findall(r'\d+', text)
    return [int(n) for n in nums]

def check_master_seasons(seasons):
    # 17だけ → OK
    if seasons == [17]:
        return True

    # 17を含む & 2つまで → OK
    if 17 in seasons and len(seasons) == 2:
        return True

    # 1つだけ → OK
    if len(seasons) == 1:
        return True

    # それ以外は NG
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
    "robot": "あなたは無機質で機械的なAIです。感情を排除し、論理的に返答してください。",
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
    for i in range(0, len(result), 1900):
        await interaction.followup.send(result[i:i+1900], ephemeral=True)

# -----------------------------
# ここから AI 会話機能
# -----------------------------

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

@bot.tree.command(name="reset", description="AIとの会話をリセットします")
async def reset(interaction: discord.Interaction):
    user_id = interaction.user.id
    if user_id in user_sessions:
        user_sessions[user_id]["history"] = []
    await interaction.response.send_message("会話をリセットしたよ！")

@bot.tree.command(name="ai", description="AIと会話します")
async def ai(interaction: discord.Interaction, prompt: str):
    await interaction.response.defer(thinking=True)

    user_id = interaction.user.id

    if user_id not in user_sessions:
        user_sessions[user_id] = {
            "mode": "robot",
            "history": []
        }

    session = user_sessions[user_id]
    mode = session["mode"]

    chat = model.start_chat(history=[])

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
    MAX_LEN = 2000

    if len(reply) <= MAX_LEN:
        await interaction.followup.send(reply)
    else:
        for i in range(0, len(reply), MAX_LEN):
            await interaction.followup.send(reply[i:i+MAX_LEN])

# -----------------------------
# ここまで AI 会話機能
# -----------------------------

welcome_enabled = True

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

    # ★ 入隊フローセッション開始
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
    # Bot自身は無視
    if message.author.bot:
        return

    user_id = message.author.id

    # 入隊フロー中か？
    if user_id in apply_sessions:
        session = apply_sessions[user_id]

        # 他チャンネルのメッセージは無視（welcome-チャンネルだけで進行）
        if message.channel.id != session.get("channel_id"):
            await bot.process_commands(message)
            return

        step = session["step"]

        # STEP 1：最初の5つの質問（まとめて回答）
        if step == 1:
            session["answers"]["basic"] = message.content
            session["step"] = 2
            await message.channel.send(
                "ありがとうございます！\n"
                "次に、ランクバッジのスクショを貼ってください。\n"
                "複数枚ある場合はすべて貼ったあとに **「完了」** と送ってください。"
            )
            return

        # STEP 2：画像受付
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

        # STEP 3：参加条件（YES/NO）
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
                await message.channel.send(
                    "申し訳ありませんが、参加条件を満たさないため入隊をお断りさせていただきます。"
                )
                del apply_sessions[user_id]
                return
            else:
                await message.channel.send("「はい」または「いいえ」で回答してください。")
                return

        # STEP 4：マスター経験の有無
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

        # STEP 4-1：マスターシーズン入力
        if step == 41:
            seasons = extract_seasons(message.content)

            if not seasons:
                await message.channel.send("数字で入力してください。（例：17）")
                return

            if not check_master_seasons(seasons):
                await message.channel.send(
                    "申し訳ありませんが、当クランの基準に満たないため入隊をお断りさせていただきます。"
                )
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

        # STEP 5：説明会の日程
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

    # 他のコマンドも動かす
    await bot.process_commands(message)

bot.run(TOKEN)