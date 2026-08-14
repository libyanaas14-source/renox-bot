import asyncio

import json
import logging
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from threading import Thread

import discord
from discord import app_commands
from discord.ext import commands, tasks
import sqlite3 
from flask import Flask, jsonify


BALANCES_FILE = Path(__file__).with_name("balances.json")
balances_lock = asyncio.Lock()
AUTHORIZED_BALANCE_USERNAME = "shddowdr7"
AUTHORIZED_BALANCE_USER_ID = "1476270096296050730"
TRANSFER_TAX_PERCENT = 5
SPAM_REPEAT_LIMIT = 3
SPAM_WINDOW_SECONDS = 10
SPAM_TIMEOUT_MINUTES = 1
KEEP_ALIVE_PORT = 3000
PROTECTED_BALANCE_USER_IDS = {
    1489281825942667355,
    1476270096296050730,
}


@dataclass(slots=True)
class RecentMessage:
    content: str
    message: discord.Message
    timestamp: float


spam_history: defaultdict[int, deque[RecentMessage]] = defaultdict(deque)
keep_alive_app = Flask(__name__)


@keep_alive_app.get("/")
def keep_alive_home() -> tuple[object, int]:
    return jsonify(status="online", service="discord-bot"), 200


@keep_alive_app.get("/health")
def keep_alive_health() -> tuple[object, int]:
    return jsonify(status="ok"), 200


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)


class DiscordBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True

        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
        )

    async def setup_hook(self) -> None:
        """Sync slash commands when the bot connects."""
       synced_commands = await self.tree.sync()
        logging.info("Synced %d command(s) globally", len(synced_commands)) 
    

    async def on_ready(self) -> None:
        if self.user is not None:
            logging.info("Logged in as %s (ID: %s)", self.user, self.user.id)


bot = DiscordBot()


async def send_spam_warning(message: discord.Message, timed_out: bool) -> None:
    if timed_out:
        warning = (
            f"{message.author.mention}, your repeated messages were removed and "
            "you have been timed out for 1 minute due to spamming. "
            "Please slow down and try again afterward."
        )
    else:
        warning = (
            f"{message.author.mention}, your repeated messages were removed. "
            "I couldn't apply the 1-minute timeout because I don't have permission "
            "to moderate this member."
        )

    try:
        await message.channel.send(warning, delete_after=10)
    except (discord.Forbidden, discord.HTTPException):
        try:
            await message.author.send(warning)
        except (discord.Forbidden, discord.HTTPException):
            logging.warning("Unable to send anti-spam warning to user %s", message.author.id)


@bot.event
async def on_message(message: discord.Message) -> None:
    """Delete the third identical message sent within ten seconds and timeout its author."""
    if message.author.bot or message.guild is None or not message.content:
        return

    if "رصيد" in message.content and any(
        mentioned_user.id in PROTECTED_BALANCE_USER_IDS
        for mentioned_user in message.mentions
    ):
        try:
            await message.reply("لا يمكنك رؤية رصيده", mention_author=False)
        except (discord.Forbidden, discord.HTTPException):
            try:
                await message.author.send("لا يمكنك رؤية رصيده")
            except (discord.Forbidden, discord.HTTPException):
                logging.warning(
                    "Unable to send balance-protection reply to user %s",
                    message.author.id,
                )

    now = time.monotonic()
    history = spam_history[message.author.id]

    while history and now - history[0].timestamp > SPAM_WINDOW_SECONDS:
        history.popleft()

    matching_messages = [
        recent for recent in history if recent.content == message.content
    ]
    history.append(RecentMessage(message.content, message, now))

    if len(matching_messages) + 1 < SPAM_REPEAT_LIMIT:
        await bot.process_commands(message)
        return

    spam_messages = [recent.message for recent in matching_messages] + [message]
    history.clear()

    for spam_message in spam_messages:
        try:
            await spam_message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            logging.warning("Unable to delete spam message %s", spam_message.id)

    timed_out = False
    if isinstance(message.author, discord.Member):
        try:
            await message.author.timeout(
                timedelta(minutes=SPAM_TIMEOUT_MINUTES),
                reason="Anti-spam: repeated identical messages",
            )
            timed_out = True
        except (discord.Forbidden, discord.HTTPException):
            logging.warning("Unable to timeout spammer %s", message.author.id)

    await send_spam_warning(message, timed_out)


def run_keep_alive_server() -> None:
    keep_alive_app.run(
        host="0.0.0.0",
        port=KEEP_ALIVE_PORT,
        debug=False,
        threaded=True,
        use_reloader=False,
    )


def start_keep_alive_server() -> None:
    server_thread = Thread(
        target=run_keep_alive_server,
        name="keep-alive-server",
        daemon=True,
    )
    server_thread.start()
    logging.info("Keep-alive server starting on port %d", KEEP_ALIVE_PORT)


def load_balances() -> dict[str, int]:
    """Load balances keyed by Discord user ID from the JSON data file."""
    if not BALANCES_FILE.exists():
        return {}

    try:
        with BALANCES_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Could not read {BALANCES_FILE.name}") from error

    if not isinstance(data, dict):
        raise RuntimeError(f"{BALANCES_FILE.name} must contain a JSON object")

    balances: dict[str, int] = {}
    for user_id, amount in data.items():
        if (
            not isinstance(user_id, str)
            or isinstance(amount, bool)
            or not isinstance(amount, int)
            or amount < 0
        ):
            raise RuntimeError(
                f"Invalid balance entry for {user_id!r}; balances must be non-negative integers"
            )
        balances[user_id] = amount

    return balances


def save_balances(balances: dict[str, int]) -> None:
    """Persist balances atomically so a transfer cannot leave partial JSON."""
    temporary_file = BALANCES_FILE.with_suffix(".json.tmp")

    try:
        with temporary_file.open("w", encoding="utf-8") as file:
            json.dump(balances, file, ensure_ascii=False, indent=2)
            file.write("\n")
        temporary_file.replace(BALANCES_FILE)
    except OSError as error:
        raise RuntimeError(f"Could not write {BALANCES_FILE.name}") from error


@bot.tree.command(name="ping", description="Check whether the bot is online.")
async def ping(interaction: discord.Interaction) -> None:
    latency_ms = round(bot.latency * 1000)
    await interaction.response.send_message(f"Pong! {latency_ms} ms")


@bot.tree.command(name="رصيد", description="Display your رينو balance.")
@app_commands.describe(member="The member whose رينو balance to display")
async def balance(
    interaction: discord.Interaction,
    member: discord.Member | None = None,
) -> None:
    target = member or interaction.user

    try:
        async with balances_lock:
            amount = load_balances().get(str(target.id), 0)
    except RuntimeError:
        logging.exception("Unable to load balances for /رصيد")
        await interaction.response.send_message(
            "تعذر قراءة ملف الأرصدة حالياً. يرجى المحاولة لاحقاً.",
            ephemeral=True,
        )
        return

    if member is None:
        message = f"رصيدك من رينو: **{amount:,} رينو**"
    else:
        message = f"رصيد {member.mention} من رينو: **{amount:,} رينو**"

    await interaction.response.send_message(message)


@bot.tree.command(name="تحويل", description="Transfer رينو to another member.")
@app_commands.describe(member="The member receiving the رينو", amount="The amount of رينو to transfer")
async def transfer(
    interaction: discord.Interaction,
    member: discord.Member,
    amount: app_commands.Range[int, 1],
) -> None:
    if member.id == interaction.user.id:
        await interaction.response.send_message(
            "لا يمكنك تحويل رينو إلى نفسك.",
            ephemeral=True,
        )
        return

    if member.bot:
        await interaction.response.send_message(
            "لا يمكنك تحويل رينو إلى حساب آلي.",
            ephemeral=True,
        )
        return

    sender_id = str(interaction.user.id)
    recipient_id = str(member.id)

    try:
        async with balances_lock:
            balances = load_balances()
            sender_balance = balances.get(sender_id, 0)

            if sender_balance < amount:
                await interaction.response.send_message(
                    f"رصيدك غير كافٍ. رصيدك الحالي: **{sender_balance:,} رينو**.",
                    ephemeral=True,
                )
                return

            tax_amount = amount * TRANSFER_TAX_PERCENT // 100
            recipient_amount = amount - tax_amount
            balances[sender_id] = sender_balance - amount
            balances[recipient_id] = balances.get(recipient_id, 0) + recipient_amount
            save_balances(balances)
            remaining_balance = balances[sender_id]
    except RuntimeError:
        logging.exception("Unable to complete /تحويل")
        await interaction.response.send_message(
            "تعذر إتمام التحويل حالياً. يرجى المحاولة لاحقاً.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"تم تحويل **{recipient_amount:,} رينو** إلى {member.mention} "
        f"بعد خصم ضريبة **{tax_amount:,} رينو** (5%). "
        f"رصيدك المتبقي: **{remaining_balance:,} رينو**."
    )


@bot.tree.command(name="اضافة", description="Add رينو to a member's balance.")
@app_commands.describe(member="The member receiving the رينو", amount="The amount of رينو to add")
async def add_balance(
    interaction: discord.Interaction,
    member: discord.Member,
    amount: app_commands.Range[int, 1],
) -> None:
    is_authorized_user = (
        str(interaction.user.id) == AUTHORIZED_BALANCE_USER_ID
        or interaction.user.name == AUTHORIZED_BALANCE_USERNAME
    )
    if not is_authorized_user:
        await interaction.response.send_message(
            "ليس لديك صلاحية استخدام هذا الأمر.",
            ephemeral=True,
        )
        return

    recipient_id = str(member.id)

    try:
        async with balances_lock:
            balances = load_balances()
            balances[recipient_id] = balances.get(recipient_id, 0) + amount
            save_balances(balances)
            new_balance = balances[recipient_id]
    except RuntimeError:
        logging.exception("Unable to complete /اضافة")
        await interaction.response.send_message(
            "تعذر إضافة رينو حالياً. يرجى المحاولة لاحقاً.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"تمت إضافة **{amount:,} رينو** إلى {member.mention}. "
        f"رصيده الجديد: **{new_balance:,} رينو**."
    )


# ==========================================
# نظام الترقيات والـ TOP اليومي للإدارة
# =========================================

ADMIN_ROLE_ID = 1537274972597260379  # ضع هنا ID رتبة الإدارة

# ترتيب الـ 41 رتبة بالترتيب الفعلي (من الأقل للأعلى)
STAFF_ROLES_ORDER = [1537274972597260379,
    1537274242909868085,
    1537275238885359636,
    1537275451209158656,
    1537275663503855696,
    1537276083907465337,
    1537276300404985896,
    1537276853658718229,
    1537277669492920460,
    1537278347120214138,
    1537279054724595794,
    1537282990672187522,
    1537283287318274188,
    1537283780497113181,
    1537287444943339560,
    1537287340131614791,
    1537287147567194132,
    1537287030571147264,
    1537286917098573855,
    1537286787746111518,
    1537286692820619274,
    1537286574994362398,
    1537286410065678428,
    1537286276146004090,
    1537275451209158656,
    1537286049108205728,
    1537285923971010670,
    1537285722887954512,
    1537285604570824815,
    1537285461666431077,
    1537285297220485230,
    1537285183953440900,
    1537285070883258379,
    1537284815727099985,
    1537284679127015504,
    1537284582611882075,
    1537468075379523654,
    1537284292189626458,
    1537283268796354590,
    1537283064965627995,
    1537282925115088906,
    1537282688560533654,
    1537282379293663373,
    1537277669492920460,
    1537281951705211010,
    1537281806586478652,
    1537281536498606150,
    1537281235951419434,
    1537280175509872640,
    1537279895011459153,
    1537279625707782265,
    1537279087389843467,
    1537278719553568768,
    1537278304338321528,
    1537277782487203940


    # ضع الـ IDs للـ 41 رتبة هنا مفصولة بفواصل
]

def get_max_rank_index(total_messages):
    rank = 0
    messages = total_messages
    tiers = [(7, 150), (9, 200), (8, 250), (14, 300), (3, 500)]
    for count, cost in tiers:
        for _ in range(count):
            if messages >= cost:
                messages -= cost
                rank += 1
            else:
                return rank
    return rank

def init_staff_db():
    conn = sqlite3.connect("admin_system.db")
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS staff_stats 
                 (user_id INTEGER PRIMARY KEY, total_messages INTEGER, promotions_given INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS daily_leaderboard 
                 (user_id INTEGER PRIMARY KEY, daily_messages INTEGER)''')
    conn.commit()
    conn.close()

init_staff_db()

@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return
    
    admin_role = message.guild.get_role(ADMIN_ROLE_ID)
    if admin_role and admin_role in message.author.roles:
        conn = sqlite3.connect("admin_system.db")
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO staff_stats (user_id, total_messages, promotions_given) VALUES (?, COALESCE((SELECT total_messages+1 FROM staff_stats WHERE user_id=?), 1), COALESCE((SELECT promotions_given FROM staff_stats WHERE user_id=?), 0))", (message.author.id, message.author.id, message.author.id))
        c.execute("INSERT OR REPLACE INTO daily_leaderboard (user_id, daily_messages) VALUES (?, COALESCE((SELECT daily_messages+1 FROM daily_leaderboard WHERE user_id=?), 1))", (message.author.id, message.author.id))
        conn.commit()
        conn.close()
    
    await bot.process_commands(message)

@tasks.loop(time=datetime.time(hour=4, minute=0))
async def reset_daily_top():
    conn = sqlite3.connect("admin_system.db")
    conn.execute("DELETE FROM daily_leaderboard")
    conn.commit()
    conn.close()

reset_daily_top.start()

# أمر /roll
@bot.tree.command(name="roll", description="ترقية إداري بناءً على عدد رسائله")
@app_commands.describe(promotions="عدد الترقيات المطلوب إعطاؤها", member="الإداري المراد ترقيته")
async def roll_cmd(interaction: discord.Interaction, promotions: int, member: discord.Member):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("ليس لديك صلاحية استخدام هذا الأمر.", ephemeral=True)
    
    conn = sqlite3.connect("admin_system.db")
    c = conn.cursor()
    c.execute("SELECT total_messages, promotions_given FROM staff_stats WHERE user_id=?", (member.id,))
    row = c.fetchone()
    
    if not row:
        return await interaction.response.send_message(f"{member.mention} ليس لديه أي رسائل مسجلة بعد.", ephemeral=True)
    
    total_messages, given = row
    max_eligible = get_max_rank_index(total_messages)
    can_give = max_eligible - given
    to_give = min(promotions, can_give)
    
    if to_give <= 0:
        return await interaction.response.send_message(f"{member.mention} لا يستحق ترقيات جديدة حالياً.", ephemeral=True)

    current_rank_index = -1
    for i, rid in enumerate(STAFF_ROLES_ORDER):
        if interaction.guild.get_role(rid) in member.roles:
            current_rank_index = i
        
    added = []
    for i in range(current_rank_index + 1, current_rank_index + 1 + to_give):
        if i < len(STAFF_ROLES_ORDER):
            role = interaction.guild.get_role(STAFF_ROLES_ORDER[i])
            if role:
                await member.add_roles(role)
                added.append(role.mention)
            
    c.execute("UPDATE staff_stats SET promotions_given = promotions_given + ? WHERE user_id=?", (to_give, member.id))
    conn.commit()
    conn.close()
    
    await interaction.response.send_message(f"🎉 تمت ترقية {member.mention} بـ `{to_give}` رتب بنجاح!")

# أمر /top
@bot.tree.command(name="top", description="عرض قائمة أفضل 10 إداريين اليوم")
async def top_cmd(interaction: discord.Interaction):
    conn = sqlite3.connect("admin_system.db")
    c = conn.cursor()
    c.execute("SELECT user_id, daily_messages FROM daily_leaderboard ORDER BY daily_messages DESC LIMIT 10")
    data = c.fetchall()
    conn.close()
    
    if not data:
        return await interaction.response.send_message("لا يوجد نشاط للإداريين اليوم حتى الآن.", ephemeral=True)
    
    embed = discord.Embed(title="🏆 TOP الإداريين اليومي", color=discord.Color.gold())
    desc = ""
    for i, (uid, msg) in enumerate(data, 1):
        mem = interaction.guild.get_member(uid)
        name = mem.display_name if mem else f"عضو ({uid})"
        desc += f"**{i}.** {name} — `{msg}` رسالة\n"
    
    embed.description = desc
    await interaction.response.send_message(embed=embed)



def main() -> None:
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "DISCORD_BOT_TOKEN is not set. Add it as a Replit Secret before running the bot."
        )

    start_keep_alive_server()
    bot.run(token)


if __name__ == "__main__":
    main()
