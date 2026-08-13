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
from discord.ext import commands
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
        guild_id = os.getenv("DISCORD_GUILD_ID")

        if guild_id:
            try:
                guild = discord.Object(id=int(guild_id))
            except ValueError as error:
                raise ValueError("DISCORD_GUILD_ID must be a numeric Discord server ID") from error

            self.tree.copy_global_to(guild=guild)
            synced_commands = await self.tree.sync(guild=guild)
            logging.info("Synced %d command(s) to Discord server %s", len(synced_commands), guild_id)
            return

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
