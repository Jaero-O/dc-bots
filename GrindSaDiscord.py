import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
from datetime import datetime, date, timezone, time
from aiohttp import web
import asyncio
import aiohttp

# ── Config ──────────────────────────────────────────────────────────────────
TOKEN = os.environ.get("TOKEN", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://vseqeydijcherzsleszg.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
MIN_STREAK_SECONDS = 60
AUTO_POST_CHANNEL = "leaderboard"
AUTO_POST_TIME = time(hour=16, minute=0, tzinfo=timezone.utc)  # Midnight PHT (UTC+8)
WEB_PORT = int(os.environ.get("PORT", 8080))

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}

# ── Bot Setup ────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.voice_states = True
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ── In-memory session tracker ────────────────────────────────────────────────
active_sessions: dict[int, datetime] = {}

# ── Web Server ────────────────────────────────────────────────────────────────

async def handle_ping(request):
    return web.Response(text="✅ Bot is alive!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEB_PORT)
    await site.start()
    print(f"🌐 Web server running on port {WEB_PORT}")

# ── Supabase Helpers ──────────────────────────────────────────────────────────

async def db_get_user(user_id: int) -> dict | None:
    """Fetch a user row from Supabase."""
    url = f"{SUPABASE_URL}/rest/v1/voice_data?user_id=eq.{user_id}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=HEADERS) as resp:
            rows = await resp.json()
            return rows[0] if rows else None


async def db_upsert_user(entry: dict):
    """Insert or update a user row in Supabase."""
    url = f"{SUPABASE_URL}/rest/v1/voice_data"
    upsert_headers = {**HEADERS, "Prefer": "resolution=merge-duplicates,return=representation"}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=upsert_headers, json=entry) as resp:
            return await resp.json()


async def db_get_all() -> list[dict]:
    """Fetch all rows ordered by total_seconds."""
    url = f"{SUPABASE_URL}/rest/v1/voice_data?order=total_seconds.desc"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=HEADERS) as resp:
            return await resp.json()


async def get_or_create_user(user_id: int) -> dict:
    """Get user entry or return a default one."""
    entry = await db_get_user(user_id)
    if entry is None:
        entry = {
            "user_id": str(user_id),
            "total_seconds": 0,
            "streak": 0,
            "last_active_date": None,
            "streak_seconds_today": 0,
            "streak_date_today": None,
            "last_flushed_seconds": 0,
        }
    return entry

# ── Streak & Helpers ──────────────────────────────────────────────────────────

def update_streak(entry: dict, session_seconds: float, today: str):
    if entry.get("streak_date_today") != today:
        entry["streak_date_today"] = today
        entry["streak_seconds_today"] = 0

    entry["streak_seconds_today"] = (entry.get("streak_seconds_today") or 0) + session_seconds

    if entry["streak_seconds_today"] >= MIN_STREAK_SECONDS:
        last = entry.get("last_active_date")
        if last is None:
            entry["streak"] = 1
        else:
            last_date = date.fromisoformat(last)
            today_date = date.fromisoformat(today)
            delta = (today_date - last_date).days
            if delta == 0:
                pass
            elif delta == 1:
                entry["streak"] = (entry.get("streak") or 0) + 1
            else:
                entry["streak"] = 1
        entry["last_active_date"] = today


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h {minutes:02d}m"
    elif minutes > 0:
        return f"{minutes}m {secs:02d}s"
    else:
        return f"{secs}s"


def get_live_total(uid: int, saved_seconds: float) -> float:
    if uid in active_sessions:
        elapsed = (datetime.now(timezone.utc) - active_sessions[uid]).total_seconds()
        return saved_seconds + elapsed
    return saved_seconds


async def flush_active_sessions():
    """Save all active sessions to Supabase (called before daily post)."""
    now = datetime.now(timezone.utc)
    today = date.today().isoformat()
    for uid, join_time in list(active_sessions.items()):
        elapsed = (now - join_time).total_seconds()
        entry = await get_or_create_user(uid)
        last_flushed = entry.get("last_flushed_seconds") or 0
        new_seconds = max(0, elapsed - last_flushed)
        entry["total_seconds"] = (entry.get("total_seconds") or 0) + new_seconds
        entry["last_flushed_seconds"] = elapsed
        update_streak(entry, new_seconds, today)
        await db_upsert_user(entry)

# ── Events ───────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    await tree.sync()
    daily_leaderboard.start()
    print(f"✅ Logged in as {bot.user} ({bot.user.id})")
    print(f"🗄️  Database: Supabase")
    print(f"⏱️  Min streak time: {MIN_STREAK_SECONDS}s")
    print(f"📅 Daily leaderboard → #{AUTO_POST_CHANNEL} at midnight PHT")


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    now = datetime.now(timezone.utc)
    uid = member.id

    # Joined voice
    if before.channel is None and after.channel is not None:
        active_sessions[uid] = now
        print(f"🎙️  {member.display_name} joined voice")

    # Left voice — save to Supabase
    elif before.channel is not None and after.channel is None:
        if uid in active_sessions:
            join_time = active_sessions.pop(uid)
            session_seconds = (now - join_time).total_seconds()

            entry = await get_or_create_user(uid)
            last_flushed = entry.pop("last_flushed_seconds", 0) or 0
            remaining = max(0, session_seconds - last_flushed)
            entry["total_seconds"] = (entry.get("total_seconds") or 0) + remaining
            entry["last_flushed_seconds"] = 0

            today = date.today().isoformat()
            update_streak(entry, session_seconds, today)

            await db_upsert_user(entry)
            print(f"💾 {member.display_name} left voice — saved {format_duration(session_seconds)}")

# ── Daily Auto-Post ───────────────────────────────────────────────────────────

@tasks.loop(time=AUTO_POST_TIME)
async def daily_leaderboard():
    await flush_active_sessions()
    print("💾 Flushed active sessions for daily post")

    all_rows = await db_get_all()

    for guild in bot.guilds:
        channel = discord.utils.get(guild.text_channels, name=AUTO_POST_CHANNEL)
        if channel is None:
            print(f"⚠️  #{AUTO_POST_CHANNEL} not found in {guild.name}")
            continue

        lines = await build_leaderboard_lines(guild, all_rows)
        if not lines:
            await channel.send("📭 No voice data yet! Jump in a voice channel to get on the board.")
            continue

        today_str = datetime.now(timezone.utc).strftime("%B %d, %Y")
        await channel.send(f"📅 **Daily Leaderboard — {today_str}**\n" + "\n".join(lines))
        print(f"📅 Daily leaderboard posted in #{AUTO_POST_CHANNEL} for {guild.name}")


@daily_leaderboard.before_loop
async def before_daily():
    await bot.wait_until_ready()

# ── Leaderboard Builder ───────────────────────────────────────────────────────

async def build_leaderboard_lines(guild: discord.Guild, rows: list[dict] = None, top: int = 10) -> list[str]:
    if rows is None:
        rows = await db_get_all()

    if not rows:
        return []

    # Merge with live session time
    merged = []
    for row in rows:
        uid = int(row["user_id"])
        live_total = get_live_total(uid, row.get("total_seconds") or 0)
        merged.append({**row, "total_seconds": live_total})

    merged.sort(key=lambda x: x["total_seconds"], reverse=True)
    merged = merged[:top]

    medals = ["🥇", "🥈", "🥉"]
    lines = ["```", "🎙️  Voice Time Leaderboard", "─" * 36]

    for i, entry in enumerate(merged):
        uid = int(entry["user_id"])
        try:
            member = await guild.fetch_member(uid)
            name = member.display_name[:16]
        except Exception:
            name = f"User {str(uid)[:4]}"

        rank = medals[i] if i < 3 else f"{i + 1}."
        duration = format_duration(entry["total_seconds"])
        streak = entry.get("streak") or 0
        streak_str = f"🔥 {streak}d" if streak > 0 else "❌"
        live_tag = " 🔴" if uid in active_sessions else ""

        lines.append(f"{rank:<3} {name:<16} {duration:>8}   {streak_str}{live_tag}")

    lines.append("```")
    lines.append("*🔴 = currently in voice*")
    return lines

# ── Slash Commands ────────────────────────────────────────────────────────────

@tree.command(name="leaderboard", description="Show the voice time leaderboard")
@app_commands.describe(top="How many users to show (default: 10)")
async def leaderboard(interaction: discord.Interaction, top: int = 10):
    lines = await build_leaderboard_lines(interaction.guild, top=top)
    if not lines:
        await interaction.response.send_message("📭 No voice data yet!", ephemeral=True)
        return
    await interaction.response.send_message("\n".join(lines))


@tree.command(name="myvoicetime", description="Check your own voice time and streak")
async def myvoicetime(interaction: discord.Interaction):
    uid = interaction.user.id
    entry = await get_or_create_user(uid)
    live_total = get_live_total(uid, entry.get("total_seconds") or 0)

    if live_total == 0 and uid not in active_sessions:
        await interaction.response.send_message("📭 No voice time yet! Join a voice channel to start.", ephemeral=True)
        return

    duration = format_duration(live_total)
    streak = entry.get("streak") or 0
    last_active = entry.get("last_active_date") or "Never"
    live_tag = " 🔴 *(currently in voice)*" if uid in active_sessions else ""
    streak_line = f"🔥 **{streak}-day streak!**" if streak > 0 else "❌ No active streak"

    msg = (
        f"🎙️ **{interaction.user.display_name}'s Voice Stats**{live_tag}\n"
        f"⏱️  Total time: **{duration}**\n"
        f"{streak_line}\n"
        f"📅 Last active: `{last_active}`"
    )
    await interaction.response.send_message(msg, ephemeral=True)


@tree.command(name="voicetime", description="Check another user's voice time")
@app_commands.describe(member="The member to check")
async def voicetime(interaction: discord.Interaction, member: discord.Member):
    uid = member.id
    entry = await get_or_create_user(uid)
    live_total = get_live_total(uid, entry.get("total_seconds") or 0)

    if live_total == 0 and uid not in active_sessions:
        await interaction.response.send_message(f"📭 **{member.display_name}** has no voice time yet.", ephemeral=True)
        return

    duration = format_duration(live_total)
    streak = entry.get("streak") or 0
    live_tag = " 🔴 *(currently in voice)*" if uid in active_sessions else ""
    streak_line = f"🔥 **{streak}-day streak!**" if streak > 0 else "❌ No active streak"

    msg = (
        f"🎙️ **{member.display_name}'s Voice Stats**{live_tag}\n"
        f"⏱️  Total time: **{duration}**\n"
        f"{streak_line}"
    )
    await interaction.response.send_message(msg)

# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    await start_web_server()
    await bot.start(TOKEN)

asyncio.run(main())
