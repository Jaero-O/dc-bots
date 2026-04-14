import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
import sys
from datetime import datetime, date, timezone, time
from aiohttp import web
import asyncio
import aiohttp

# Force unbuffered output so Render shows logs immediately
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

print("🚀 Bot starting up...")
print(f"   TOKEN set: {bool(os.environ.get('TOKEN'))}")
print(f"   SUPABASE_URL set: {bool(os.environ.get('SUPABASE_URL'))}")
print(f"   SUPABASE_KEY set: {bool(os.environ.get('SUPABASE_KEY'))}")

# ── Config ───────────────────────────────────────────────────────────────────
TOKEN        = os.environ.get("TOKEN", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
MIN_STREAK_SECONDS  = 60
AUTO_POST_CHANNEL   = "leaderboard"
AUTO_POST_TIME      = time(hour=16, minute=0, tzinfo=timezone.utc)  # Midnight PHT
WEB_PORT            = int(os.environ.get("PORT", 8080))

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}

# ── Bot setup ────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.voice_states   = True
intents.members        = True
intents.message_content = True

bot  = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ── Session state (in memory only) ──────────────────────────────────────────
# join_times : user_id -> UTC datetime when they joined current session
# saved_secs : user_id -> seconds already flushed to DB in current session
join_times: dict[int, datetime] = {}
saved_secs: dict[int, float]   = {}

# ── Helpers ──────────────────────────────────────────────────────────────────

def format_duration(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:   return f"{h}h {m:02d}m"
    if m:   return f"{m}m {sec:02d}s"
    return f"{sec}s"


def live_seconds(uid: int) -> float:
    """Extra seconds for a user currently in voice (not yet saved)."""
    if uid not in join_times:
        return 0.0
    elapsed = (datetime.now(timezone.utc) - join_times[uid]).total_seconds()
    return max(0.0, elapsed - saved_secs.get(uid, 0.0))


def update_streak(entry: dict, new_seconds: float, today: str):
    """Increment streak counters; only advances streak once per day."""
    if entry.get("streak_date_today") != today:
        entry["streak_date_today"]    = today
        entry["streak_seconds_today"] = 0.0

    entry["streak_seconds_today"] = (entry.get("streak_seconds_today") or 0.0) + new_seconds

    if entry["streak_seconds_today"] >= MIN_STREAK_SECONDS:
        last = entry.get("last_active_date")
        if last is None:
            entry["streak"] = 1
        else:
            delta = (date.fromisoformat(today) - date.fromisoformat(last)).days
            if delta == 0:
                pass          # already counted today
            elif delta == 1:
                entry["streak"] = (entry.get("streak") or 0) + 1
            else:
                entry["streak"] = 1   # streak broken
        entry["last_active_date"] = today

# ── Supabase I/O ──────────────────────────────────────────────────────────────

async def db_get(uid: int) -> dict | None:
    url = f"{SUPABASE_URL}/rest/v1/voice_data?user_id=eq.{uid}"
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers=HEADERS) as r:
            rows = await r.json()
            return rows[0] if rows else None


async def db_upsert(entry: dict):
    url = f"{SUPABASE_URL}/rest/v1/voice_data"
    hdrs = {**HEADERS,
            "Prefer": "resolution=merge-duplicates,return=minimal"}
    async with aiohttp.ClientSession() as s:
        async with s.post(url, headers=hdrs, json=entry) as r:
            if r.status not in (200, 201):
                text = await r.text()
                print(f"⚠️  db_upsert error {r.status}: {text}")


async def db_get_all() -> list[dict]:
    url = f"{SUPABASE_URL}/rest/v1/voice_data?order=total_seconds.desc"
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers=HEADERS) as r:
            return await r.json()


def blank_entry(uid: int) -> dict:
    return {
        "user_id":             str(uid),
        "total_seconds":       0.0,
        "streak":              0,
        "last_active_date":    None,
        "streak_seconds_today": 0.0,
        "streak_date_today":   None,
    }


async def get_or_create(uid: int) -> dict:
    row = await db_get(uid)
    return row if row else blank_entry(uid)

# ── Core save logic ───────────────────────────────────────────────────────────

async def save_new_seconds(uid: int, new_secs: float, today: str):
    """Add new_secs to the user's DB row and update streak."""
    if new_secs <= 0:
        return
    entry = await get_or_create(uid)
    entry["total_seconds"] = (entry.get("total_seconds") or 0.0) + new_secs
    update_streak(entry, new_secs, today)
    await db_upsert(entry)
    print(f"💾 Saved {format_duration(new_secs)} for user {uid} "
          f"(total {format_duration(entry['total_seconds'])})")


async def flush_all():
    """
    Flush every active session.
    Only saves seconds accumulated SINCE the last flush for each user.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    for uid in list(join_times):
        elapsed   = (datetime.now(timezone.utc) - join_times[uid]).total_seconds()
        new_secs  = max(0.0, elapsed - saved_secs.get(uid, 0.0))
        saved_secs[uid] = elapsed          # remember we've now saved up to `elapsed`
        await save_new_seconds(uid, new_secs, today)

# ── Web server (keeps Render alive) ──────────────────────────────────────────

async def handle_ping(request):
    return web.Response(text="✅ Bot is alive!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", WEB_PORT).start()
    print(f"🌐 Web server running on port {WEB_PORT}")

# ── DB connection check ───────────────────────────────────────────────────────

async def check_db():
    print("🔌 Checking Supabase connection...")
    try:
        url = f"{SUPABASE_URL}/rest/v1/voice_data?limit=1"
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=HEADERS) as r:
                if r.status == 200:
                    print("✅ Supabase connected!")
                elif r.status == 401:
                    print("❌ Supabase: Invalid API key (401) — check SUPABASE_KEY in Render")
                else:
                    print(f"❌ Supabase: HTTP {r.status} — {await r.text()}")
    except Exception as e:
        print(f"❌ Supabase error: {e}")

# ── Discord events ────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    await tree.sync()
    await check_db()
    daily_leaderboard.start()
    print(f"✅ Logged in as {bot.user}")

@bot.event
async def on_voice_state_update(member: discord.Member,
                                before: discord.VoiceState,
                                after:  discord.VoiceState):
    uid = member.id
    now = datetime.now(timezone.utc)

    joined = before.channel is None and after.channel is not None
    left   = before.channel is not None and after.channel is None

    if joined:
        join_times[uid] = now
        saved_secs[uid] = 0.0          # reset saved counter for new session
        print(f"🎙️  {member.display_name} joined voice")

    elif left and uid in join_times:
        elapsed  = (now - join_times.pop(uid)).total_seconds()
        new_secs = max(0.0, elapsed - saved_secs.pop(uid, 0.0))
        today    = now.date().isoformat()
        await save_new_seconds(uid, new_secs, today)
        print(f"👋 {member.display_name} left voice")

# ── Daily leaderboard task ────────────────────────────────────────────────────

@tasks.loop(time=AUTO_POST_TIME)
async def daily_leaderboard():
    await flush_all()
    rows = await db_get_all()
    for guild in bot.guilds:
        ch = discord.utils.get(guild.text_channels, name=AUTO_POST_CHANNEL)
        if not ch:
            print(f"⚠️  #{AUTO_POST_CHANNEL} not found in {guild.name}")
            continue
        lines = await build_lines(guild, rows)
        label = datetime.now(timezone.utc).strftime("%B %d, %Y")
        await ch.send(f"📅 **Daily Leaderboard — {label}**\n" + "\n".join(lines)
                      if lines else "📭 No data yet!")

@daily_leaderboard.before_loop
async def _before():
    await bot.wait_until_ready()

# ── Leaderboard builder ───────────────────────────────────────────────────────

async def build_lines(guild: discord.Guild,
                      rows: list[dict] | None = None,
                      top: int = 10) -> list[str]:
    if rows is None:
        rows = await db_get_all()
    if not rows:
        return []

    # Add live unsaved seconds on top of DB value
    enriched = []
    for row in rows:
        uid   = int(row["user_id"])
        total = (row.get("total_seconds") or 0.0) + live_seconds(uid)
        enriched.append((uid, total, row.get("streak") or 0))

    enriched.sort(key=lambda x: x[1], reverse=True)
    enriched = enriched[:top]

    medals = ["🥇", "🥈", "🥉"]
    lines  = ["```", "🎙️  Voice Time Leaderboard", "─" * 36]

    for i, (uid, total, streak) in enumerate(enriched):
        try:
            m    = await guild.fetch_member(uid)
            name = m.display_name[:16]
        except Exception:
            name = f"User {str(uid)[:6]}"

        rank       = medals[i] if i < 3 else f"{i+1}."
        streak_str = f"🔥 {streak}d" if streak else "❌"
        live_tag   = " 🔴" if uid in join_times else ""
        lines.append(f"{rank:<3} {name:<16} {format_duration(total):>8}   {streak_str}{live_tag}")

    lines += ["```", "*🔴 = currently in voice*"]
    return lines

# ── Slash commands ────────────────────────────────────────────────────────────

@tree.command(name="leaderboard", description="Show the voice time leaderboard")
@app_commands.describe(top="How many users to show (default: 10)")
async def cmd_leaderboard(interaction: discord.Interaction, top: int = 10):
    await interaction.response.defer()
    await flush_all()
    lines = await build_lines(interaction.guild, top=top)
    if not lines:
        await interaction.followup.send("📭 No voice data yet!", ephemeral=True)
        return
    await interaction.followup.send("\n".join(lines))


@tree.command(name="myvoicetime", description="Check your own voice time and streak")
async def cmd_myvoicetime(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await flush_all()
    uid   = interaction.user.id
    entry = await get_or_create(uid)
    total = (entry.get("total_seconds") or 0.0) + live_seconds(uid)

    if total == 0:
        await interaction.followup.send("📭 No voice time yet!", ephemeral=True)
        return

    streak   = entry.get("streak") or 0
    last     = entry.get("last_active_date") or "Never"
    live_tag = " 🔴 *(currently in voice)*" if uid in join_times else ""
    streak_line = f"🔥 **{streak}-day streak!**" if streak else "❌ No active streak"

    await interaction.followup.send(
        f"🎙️ **{interaction.user.display_name}'s Voice Stats**{live_tag}\n"
        f"⏱️  Total time: **{format_duration(total)}**\n"
        f"{streak_line}\n"
        f"📅 Last active: `{last}`",
        ephemeral=True
    )


@tree.command(name="voicetime", description="Check another user's voice time")
@app_commands.describe(member="The member to check")
async def cmd_voicetime(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.defer()
    await flush_all()
    uid   = member.id
    entry = await get_or_create(uid)
    total = (entry.get("total_seconds") or 0.0) + live_seconds(uid)

    if total == 0:
        await interaction.followup.send(
            f"📭 **{member.display_name}** has no voice time yet.", ephemeral=True)
        return

    streak   = entry.get("streak") or 0
    live_tag = " 🔴 *(currently in voice)*" if uid in join_times else ""
    streak_line = f"🔥 **{streak}-day streak!**" if streak else "❌ No active streak"

    await interaction.followup.send(
        f"🎙️ **{member.display_name}'s Voice Stats**{live_tag}\n"
        f"⏱️  Total time: **{format_duration(total)}**\n"
        f"{streak_line}"
    )

# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    await start_web_server()
    await bot.start(TOKEN)

asyncio.run(main())
