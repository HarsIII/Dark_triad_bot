import discord
from discord.ext import commands, tasks
from discord.ui import Button, View
import re
import calendar
import json
import os
import io
from datetime import datetime, timedelta

# ─── CONFIG ───────────────────────────────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ALERT_MINUTES = 2
import os
DATA_DIR = os.getenv("DATA_DIR", ".")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
DATA_FILE = os.path.join(DATA_DIR, "data.json")
# ──────────────────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# ─── RUNTIME DATA ─────────────────────────────────────────────────────────────
boss_list = {}
tracker_message = None
alerted = set()

# ─── CONFIG LOAD/SAVE ─────────────────────────────────────────────────────────
def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    return {"channels": {}}

def save_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)

def get_channel_id(module: str):
    return load_config()["channels"].get(module)

# ─── DATA LOAD/SAVE ───────────────────────────────────────────────────────────
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {"hackers": {}, "builds": {"traits": {}, "races": {}, "spirits": {}, "titles": {}, "grimorios": {}}}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)

def ensure_builds(data):
    if "builds" not in data:
        data["builds"] = {}
    for cat in ["traits", "races", "spirits", "titles", "grimorios"]:
        if cat not in data["builds"]:
            data["builds"][cat] = {}
    return data

# ─── UTILS ────────────────────────────────────────────────────────────────────
def parse_time(text):
    text = text.strip().lower()
    minutes = 0
    hours = 0
    h_match = re.search(r'(\d+)\s*h', text)
    m_match = re.search(r'(\d+)\s*m', text)
    if h_match:
        hours = int(h_match.group(1))
    if m_match:
        minutes = int(m_match.group(1))
    if not h_match and not m_match:
        bare = re.search(r'(\d+)', text)
        if bare:
            minutes = int(bare.group(1))
    total = hours * 60 + minutes
    return total if total > 0 else None

def to_unix(dt_utc):
    return calendar.timegm(dt_utc.timetuple())

def parse_value(val_str):
    val_str = val_str.strip().lower()
    if val_str.endswith("m"):
        return float(val_str[:-1]) * 1_000_000
    elif val_str.endswith("k"):
        return float(val_str[:-1]) * 1_000
    return float(val_str)

def format_value(val):
    if val >= 1_000_000:
        return f"{val/1_000_000:.2f}M"
    elif val >= 1_000:
        return f"{val/1_000:.1f}k"
    return f"{val:.0f}"

def parse_buff(buff_str):
    """
    Supported formats:
    15%dmg, -10%cd, 20%crit, 35%critdmg
    15%magicdmg, 10%magicres, 10%dmgres
    10%manaregen, 10%hpregen, 5%lifesteal
    30%hprestore@20s, 15%manarestore@30s
    -50%hp  (conditional below X% hp)
    fire=y / fire=n  (standalone grimoire condition)
    """
    buff_str = buff_str.strip().lower()

    # Periodic: 30%hprestore@20s
    m = re.match(r'(-?)(\d+(?:\.\d+)?)%(\w+)@(\d+)s$', buff_str)
    if m:
        return {
            "type": m.group(3),
            "value": float(m.group(2)),
            "negative": m.group(1) == "-",
            "interval": int(m.group(4)),
            "condition": "periodic"
        }

    # HP condition: -50%hp
    m = re.match(r'-(\d+)%hp$', buff_str)
    if m:
        return {"type": "hp_threshold", "value": int(m.group(1)), "negative": False, "condition": f"hp<{m.group(1)}"}

    # Standalone grimoire condition: fire=y
    m = re.match(r'(\w+)=(y|n)$', buff_str)
    if m:
        return {"type": "grimoire_req", "grimoire": m.group(1), "required": m.group(2) == "y"}

    # Standard: [-]number%type
    m = re.match(r'(-?)(\d+(?:\.\d+)?)%(\w+)$', buff_str)
    if m:
        return {
            "type": m.group(3),
            "value": float(m.group(2)),
            "negative": m.group(1) == "-",
            "condition": None
        }
    return None

def parse_spirit_args(args):
    """
    Supports: 5%dmg 5%maxhp fire=y:12%dmg 15%burndmg 10%dmgburning
    Everything after fire=y: is tagged as grimoire-conditional
    """
    buffs = []
    current_grimoire = None
    current_required = None

    for arg in args:
        arg = arg.strip().lower()
        # Inline grimoire trigger: fire=y:12%dmg
        m = re.match(r'(\w+)=(y|n):(.+)$', arg)
        if m:
            current_grimoire = m.group(1)
            current_required = m.group(2) == "y"
            b = parse_buff(m.group(3))
            if b:
                b["grimoire_cond"] = current_grimoire
                b["grimoire_required"] = current_required
                buffs.append(b)
            continue

        b = parse_buff(arg)
        if b:
            if current_grimoire and b.get("type") not in ("grimoire_req", "hp_threshold"):
                b["grimoire_cond"] = current_grimoire
                b["grimoire_required"] = current_required
            buffs.append(b)
    return buffs

def get_total_dmg_bonus(trait=None, race=None, spirit=None, title=None, grimoire_name=None):
    """
    Returns: (total_dmg%, crit_chance%, crit_bonus%, burning_bonus%, notes[])
    Tracks burndmg and dmgburning separately for vs-burning calculation
    """
    data = load_data()
    builds = data.get("builds", {})
    total = 0.0
    crit_chance = 0.0
    crit_bonus = 0.0
    burning_bonus = 0.0   # extra % damage against burning enemies
    burn_dmg_bonus = 0.0  # extra % to the burn tick itself
    notes = []

    def process_buffs(buffs, source_name, grimoire=None):
        nonlocal total, crit_chance, crit_bonus, burning_bonus, burn_dmg_bonus

        # Old-style standalone grimoire_req check
        grimoire_reqs = [b for b in buffs if b.get("type") == "grimoire_req"]
        if grimoire_reqs:
            for req in grimoire_reqs:
                grim_needed = req["grimoire"]
                required = req["required"]
                matches = grimoire and grimoire.lower() == grim_needed.lower()
                if required and not matches:
                    notes.append(f"⚠️ {source_name}: bonus only applies with `{grim_needed}` grimoire")
                    return

        for b in buffs:
            btype = b.get("type", "")
            if btype in ("grimoire_req", "hp_threshold"):
                continue

            val = b.get("value", 0)
            neg = b.get("negative", False)
            sign = -1 if neg else 1

            # Inline grimoire condition check
            grim_cond = b.get("grimoire_cond")
            if grim_cond:
                grim_req = b.get("grimoire_required", True)
                matches = grimoire and grimoire.lower() == grim_cond.lower()
                if grim_req and not matches:
                    notes.append(f"⚠️ {source_name}: `{btype}` only applies with `{grim_cond}` grimoire")
                    continue
                elif not grim_req and matches:
                    notes.append(f"⚠️ {source_name}: `{btype}` does not apply with `{grim_cond}` grimoire")
                    continue

            # HP condition note
            if b.get("condition") and b["condition"].startswith("hp<"):
                notes.append(f"⚠️ {source_name}: `+{val}%{btype}` only active below {b['condition'][3:]}% HP")

            if btype == "critdmg":
                crit_bonus += val
            elif btype == "crit":
                crit_chance += val
            elif btype == "dmgburning":
                burning_bonus += sign * val
            elif btype == "burndmg":
                burn_dmg_bonus += sign * val
            elif btype in ("dmg", "magicdmg", "damage"):
                total += sign * val
            elif btype == "cd":
                notes.append(f"⚡ {source_name}: {'-' if neg else '+'}{val}% Cooldown")
            elif btype == "movespeed":
                notes.append(f"🏃 {source_name}: +{val}% Move Speed")
            elif btype == "magicres":
                notes.append(f"🛡️ {source_name}: +{val}% Magic Resistance")
            elif btype == "dmgres":
                notes.append(f"🛡️ {source_name}: +{val}% Damage Resistance")
            elif btype == "hpregen":
                notes.append(f"💚 {source_name}: +{val}% HP Regen")
            elif btype == "manaregen":
                notes.append(f"💙 {source_name}: +{val}% Mana Regen")
            elif btype == "lifesteal":
                notes.append(f"🩸 {source_name}: +{val}% Lifesteal")
            elif btype == "hprestore":
                interval = b.get("interval", 0)
                notes.append(f"💚 {source_name}: +{val}% HP restore every {interval}s")
            elif btype == "manarestore":
                interval = b.get("interval", 0)
                notes.append(f"💙 {source_name}: +{val}% Mana restore every {interval}s")
            elif btype == "maxhp":
                notes.append(f"❤️ {source_name}: +{val}% Max HP")
            elif btype == "maxmana":
                notes.append(f"💎 {source_name}: +{val}% Max Mana")

    for category, label, key in [
        ("traits", "Trait", trait),
        ("races", "Race", race),
        ("spirits", "Spirit", spirit),
        ("titles", "Title", title)
    ]:
        if key:
            entry = builds.get(category, {}).get(key.lower())
            if entry:
                process_buffs(entry.get("buffs", []), f"{label}:{key}", grimoire=grimoire_name)

    return total, crit_chance, crit_bonus, burning_bonus, burn_dmg_bonus, notes

# ══════════════════════════════════════════════════════════════════════════════
#  MODULE 1 — BOSS TRACKER
# ══════════════════════════════════════════════════════════════════════════════

def build_tracker_embed():
    now = datetime.utcnow()
    embed = discord.Embed(title="🔥 BOSS TRACKER", color=discord.Color.orange())
    if not boss_list:
        embed.description = (
            "*No bosses registered yet.*\n\n"
            "Format: `username time` or `username time bossname`\n"
            "Example: `Hars 24min Vermillion`"
        )
        return embed
    sorted_bosses = sorted(boss_list.items(), key=lambda x: x[1]["spawn"])
    lines = []
    for user, data in sorted_bosses:
        spawn_time = data["spawn"]
        boss_name = data["boss"]
        remaining = (spawn_time - now).total_seconds()
        boss_label = f" `{boss_name}`" if boss_name else ""
        unix_ts = to_unix(spawn_time)
        if remaining <= 0:
            lines.append(f"✅ **{user}**{boss_label} — **SPAWNING NOW!**")
        elif remaining <= ALERT_MINUTES * 60:
            lines.append(f"⚠️ **{user}**{boss_label} — <t:{unix_ts}:R> ← SOON!")
        else:
            lines.append(f"🔹 **{user}**{boss_label} — <t:{unix_ts}:R>")
    embed.description = "\n\n".join(lines)
    embed.set_footer(text=f"Live countdown ✦ {len(boss_list)} boss(es) tracked")
    return embed

async def refresh_tracker(channel):
    global tracker_message
    embed = build_tracker_embed()
    if tracker_message:
        try:
            await tracker_message.edit(embed=embed)
            return
        except discord.NotFound:
            tracker_message = None
    tracker_message = await channel.send(embed=embed)

@tasks.loop(seconds=15)
async def update_tracker():
    global tracker_message, boss_list
    if not tracker_message:
        return
    now = datetime.utcnow()
    channel = tracker_message.channel
    for user, data in list(boss_list.items()):
        spawn_time = data["spawn"]
        boss_name = data["boss"]
        remaining = (spawn_time - now).total_seconds()
        boss_label = f" **{boss_name}**" if boss_name else ""
        if 0 < remaining <= ALERT_MINUTES * 60 and user not in alerted:
            alerted.add(user)
            discord_author = data.get("discord_author")
            mention = discord_author.mention if discord_author else f"`{user}`"
            await channel.send(
                f"⚠️ {mention} your boss{boss_label} spawns in **{ALERT_MINUTES} minutes!**",
                delete_after=120
            )
        if remaining < -60:
            del boss_list[user]
            alerted.discard(user)
    embed = build_tracker_embed()
    try:
        await tracker_message.edit(embed=embed)
    except discord.NotFound:
        tracker_message = None

@bot.command(name="list")
async def list_bosses(ctx):
    if get_channel_id("boss") != ctx.channel.id:
        return
    await ctx.message.delete(delay=2)
    await refresh_tracker(ctx.channel)

@bot.command(name="remove")
async def remove_boss(ctx, username: str = None):
    if get_channel_id("boss") != ctx.channel.id:
        return
    if not username:
        await ctx.send("❌ Usage: `!remove username`", delete_after=5)
        await ctx.message.delete(delay=5)
        return
    if username not in boss_list:
        await ctx.send(f"❌ `{username}` not found in tracker.", delete_after=5)
        await ctx.message.delete(delay=2)
        return
    confirm_msg = await ctx.send(f"⚠️ Remove `{username}` from tracker? React ✅ to confirm or ❌ to cancel.")
    await confirm_msg.add_reaction("✅")
    await confirm_msg.add_reaction("❌")
    def check(reaction, user):
        return user == ctx.author and str(reaction.emoji) in ["✅", "❌"] and reaction.message.id == confirm_msg.id
    try:
        reaction, _ = await bot.wait_for("reaction_add", timeout=15.0, check=check)
        if str(reaction.emoji) == "✅":
            del boss_list[username]
            alerted.discard(username)
            await confirm_msg.delete()
            await ctx.message.delete()
            await refresh_tracker(ctx.channel)
        else:
            await confirm_msg.delete()
            await ctx.message.delete()
            await ctx.send("❌ Cancelled.", delete_after=3)
    except:
        await confirm_msg.delete()
        await ctx.message.delete()
        await ctx.send("⏱️ Timed out.", delete_after=3)

@bot.command(name="clear")
async def clear_tracker(ctx):
    if get_channel_id("boss") != ctx.channel.id:
        return
    boss_list.clear()
    alerted.clear()
    await ctx.message.delete(delay=2)
    await refresh_tracker(ctx.channel)

# ══════════════════════════════════════════════════════════════════════════════
#  MODULE 2 — HACKER LIST
# ══════════════════════════════════════════════════════════════════════════════

HACKER_STATUSES = {
    "reported": "🔴 Reported",
    "ticket":   "🎫 In Ticket",
    "banned":   "✅ Banned",
    "reviewed": "👁️ Reviewed"
}
ENTRIES_PER_PAGE = 20

def build_hacker_embed(page=0, filter_status=None):
    data = load_data()
    hackers = data.get("hackers", {})
    entries = {k: v for k, v in hackers.items() if v["status"] == filter_status} if filter_status else hackers
    if not entries:
        embed = discord.Embed(title="🚨 HACKER LIST", description="*No entries found.*", color=discord.Color.red())
        return embed, 0, 0
    sorted_entries = sorted(entries.items(), key=lambda x: x[1]["date"], reverse=True)
    total = len(sorted_entries)
    total_pages = max(1, (total + ENTRIES_PER_PAGE - 1) // ENTRIES_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    page_entries = sorted_entries[page * ENTRIES_PER_PAGE:(page + 1) * ENTRIES_PER_PAGE]
    title = "🚨 HACKER LIST"
    if filter_status:
        title += f" — {HACKER_STATUSES.get(filter_status, filter_status)}"
    embed = discord.Embed(title=title, color=discord.Color.red())
    lines = []
    for username, info in page_entries:
        status_label = HACKER_STATUSES.get(info["status"], info["status"])
        world = f" `{info['world']}`" if info.get("world") else ""
        evidence = info.get("evidence", "")
        line = f"{status_label} **{username}**{world}\n"
        line += f"　Reported by: `{info.get('reported_by', 'unknown')}` • {info.get('date', '')[:10]}"
        if evidence:
            line += f"\n　📎 [Evidence]({evidence})" if evidence.startswith("http") else f"\n　📎 `{evidence}`"
        lines.append(line)
    embed.description = "\n\n".join(lines)
    embed.set_footer(text=f"Page {page + 1}/{total_pages} ✦ {total} entries")
    return embed, page, total_pages

class HackerListView(View):
    def __init__(self, page, total_pages, filter_status=None):
        super().__init__(timeout=120)
        self.page = page
        self.total_pages = total_pages
        self.filter_status = filter_status
        self.prev_btn.disabled = page == 0
        self.next_btn.disabled = page >= total_pages - 1

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction, button):
        self.page -= 1
        embed, self.page, self.total_pages = build_hacker_embed(self.page, self.filter_status)
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= self.total_pages - 1
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction, button):
        self.page += 1
        embed, self.page, self.total_pages = build_hacker_embed(self.page, self.filter_status)
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= self.total_pages - 1
        await interaction.response.edit_message(embed=embed, view=self)

@bot.command(name="report")
async def report_hacker(ctx, username: str = None, world: str = None):
    if get_channel_id("hackers") != ctx.channel.id:
        return
    if not username:
        await ctx.send("❌ Usage: `!report username world`", delete_after=5)
        await ctx.message.delete(delay=5)
        return
    evidence = ""
    if ctx.message.attachments:
        evidence = ctx.message.attachments[0].url
    else:
        url_match = re.search(r'https?://\S+', ctx.message.content)
        if url_match:
            evidence = url_match.group(0)
    data = load_data()
    data["hackers"][username] = {
        "status": "reported",
        "world": world or "",
        "reported_by": str(ctx.author),
        "date": str(datetime.utcnow()),
        "evidence": evidence,
        "discord_id": ""
    }
    save_data(data)
    await ctx.message.delete(delay=3)
    embed = discord.Embed(title="🚨 New Report", color=discord.Color.red())
    embed.add_field(name="User", value=f"`{username}`", inline=True)
    embed.add_field(name="World", value=f"`{world}`" if world else "N/A", inline=True)
    embed.add_field(name="Reported by", value=f"`{ctx.author}`", inline=True)
    if evidence:
        embed.add_field(name="Evidence", value=f"[Link]({evidence})" if evidence.startswith("http") else f"`{evidence}`", inline=False)
    await ctx.send(embed=embed, delete_after=30)

async def _update_status(ctx, username, new_status):
    if not username:
        await ctx.send(f"❌ Usage: `!{new_status} username`", delete_after=5)
        await ctx.message.delete(delay=5)
        return
    data = load_data()
    if username not in data["hackers"]:
        await ctx.send(f"❌ `{username}` not found in hacker list.", delete_after=5)
        await ctx.message.delete(delay=5)
        return
    data["hackers"][username]["status"] = new_status
    save_data(data)
    await ctx.message.delete(delay=2)
    await ctx.send(f"✅ `{username}` updated to {HACKER_STATUSES.get(new_status, new_status)}", delete_after=10)

@bot.command(name="ticket")
async def ticket_hacker(ctx, username: str = None):
    if get_channel_id("hackers") != ctx.channel.id: return
    await _update_status(ctx, username, "ticket")

@bot.command(name="banned")
async def banned_hacker(ctx, username: str = None):
    if get_channel_id("hackers") != ctx.channel.id: return
    await _update_status(ctx, username, "banned")

@bot.command(name="reviewed")
async def reviewed_hacker(ctx, username: str = None):
    if get_channel_id("hackers") != ctx.channel.id: return
    await _update_status(ctx, username, "reviewed")

@bot.command(name="deletehacker")
async def delete_hacker(ctx, username: str = None):
    if get_channel_id("hackers") != ctx.channel.id: return
    if not username:
        await ctx.send("❌ Usage: `!deletehacker username`", delete_after=5)
        await ctx.message.delete(delay=5)
        return
    data = load_data()
    if username not in data["hackers"]:
        await ctx.send(f"❌ `{username}` not found.", delete_after=5)
        await ctx.message.delete(delay=5)
        return
    del data["hackers"][username]
    save_data(data)
    await ctx.message.delete(delay=2)
    await ctx.send(f"🗑️ `{username}` removed from hacker list.", delete_after=10)

@bot.command(name="hackers")
async def show_hackers(ctx, filter_status: str = None):
    if get_channel_id("hackers") != ctx.channel.id: return
    if filter_status and filter_status.lower() not in HACKER_STATUSES:
        await ctx.send("❌ Invalid filter. Options: `reported`, `ticket`, `banned`, `reviewed`", delete_after=5)
        await ctx.message.delete(delay=5)
        return
    fs = filter_status.lower() if filter_status else None
    embed, page, total_pages = build_hacker_embed(0, fs)
    view = HackerListView(page, total_pages, fs)
    await ctx.message.delete(delay=2)
    await ctx.send(embed=embed, view=view)

@bot.command(name="archive")
async def archive_hackers(ctx):
    if get_channel_id("hackers") != ctx.channel.id: return
    data = load_data()
    banned = {k: v for k, v in data["hackers"].items() if v["status"] == "banned"}
    if not banned:
        await ctx.send("❌ No banned users to archive.", delete_after=5)
        await ctx.message.delete(delay=5)
        return
    now_str = datetime.utcnow().strftime("%Y-%m-%d_%H-%M")
    lines = [f"BANNED USERS ARCHIVE — {now_str}\n{'='*40}\n"]
    for username, info in banned.items():
        lines += [
            f"User: {username}",
            f"World: {info.get('world', 'N/A')}",
            f"Reported by: {info.get('reported_by', 'N/A')}",
            f"Date: {info.get('date', 'N/A')[:10]}",
            f"Evidence: {info.get('evidence', 'N/A')}",
            "-" * 30
        ]
    for username in banned:
        del data["hackers"][username]
    save_data(data)
    file = discord.File(io.BytesIO("\n".join(lines).encode()), filename=f"archive_banned_{now_str}.txt")
    await ctx.message.delete(delay=2)
    await ctx.send(f"📦 Archived **{len(banned)}** banned user(s). Removed from active list.", file=file)

# ══════════════════════════════════════════════════════════════════════════════
#  MODULE 3 — BUILD CALCULATOR
# ══════════════════════════════════════════════════════════════════════════════

def _save_entry(ctx, category, name, data_dict):
    data = load_data()
    data = ensure_builds(data)
    data["builds"][category][name.lower()] = data_dict
    save_data(data)

def _remove_entry(data, category, name):
    del data["builds"][category][name.lower()]
    save_data(data)

# ── GENERIC CRUD HELPERS ──────────────────────────────────────────────────────
async def _add_entry(ctx, category, name, buffs):
    if get_channel_id("build") != ctx.channel.id: return
    _save_entry(ctx, category, name, {"name": name, "buffs": buffs})
    await ctx.message.delete(delay=2)
    await ctx.send(f"✅ {category[:-1].capitalize()} `{name}` saved with {len(buffs)} buff(s).", delete_after=8)

async def _edit_entry(ctx, category, name, buffs):
    if get_channel_id("build") != ctx.channel.id: return
    data = load_data()
    data = ensure_builds(data)
    if name.lower() not in data["builds"][category]:
        await ctx.send(f"❌ `{name}` not found in {category}.", delete_after=5)
        await ctx.message.delete(delay=5)
        return
    _save_entry(ctx, category, name, {"name": name, "buffs": buffs})
    await ctx.message.delete(delay=2)
    await ctx.send(f"✅ {category[:-1].capitalize()} `{name}` updated.", delete_after=8)

async def _delete_entry(ctx, category, name):
    if get_channel_id("build") != ctx.channel.id: return
    data = load_data()
    data = ensure_builds(data)
    if not name or name.lower() not in data["builds"][category]:
        await ctx.send(f"❌ `{name}` not found in {category}.", delete_after=5)
        await ctx.message.delete(delay=5)
        return
    _remove_entry(data, category, name)
    await ctx.message.delete(delay=2)
    await ctx.send(f"🗑️ `{name}` removed from {category}.", delete_after=8)

# ── TRAITS ────────────────────────────────────────────────────────────────────
@bot.command(name="trait")
async def add_trait(ctx, name: str = None, *args):
    """!trait CritGenius 20%crit 35%critdmg"""
    if not name or not args:
        await ctx.send("❌ Usage: `!trait Name buff1 buff2 ...`", delete_after=10)
        await ctx.message.delete(delay=5)
        return
    await _add_entry(ctx, "traits", name, [b for a in args if (b := parse_buff(a))])

@bot.command(name="edittrait")
async def edit_trait(ctx, name: str = None, *args):
    if not name or not args:
        await ctx.send("❌ Usage: `!edittrait Name buff1 buff2 ...`", delete_after=10)
        return
    await _edit_entry(ctx, "traits", name, [b for a in args if (b := parse_buff(a))])

@bot.command(name="removetrait")
async def remove_trait(ctx, name: str = None):
    await _delete_entry(ctx, "traits", name)

# ── RACES ─────────────────────────────────────────────────────────────────────
@bot.command(name="race")
async def add_race(ctx, name: str = None, *args):
    """!race SpiritRace 15%magicdmg -10%cd 15%movespeed"""
    if not name or not args:
        await ctx.send("❌ Usage: `!race Name buff1 buff2 ...`", delete_after=10)
        await ctx.message.delete(delay=5)
        return
    await _add_entry(ctx, "races", name, [b for a in args if (b := parse_buff(a))])

@bot.command(name="editrace")
async def edit_race(ctx, name: str = None, *args):
    if not name or not args:
        await ctx.send("❌ Usage: `!editrace Name buff1 buff2 ...`", delete_after=10)
        return
    await _edit_entry(ctx, "races", name, [b for a in args if (b := parse_buff(a))])

@bot.command(name="removerace")
async def remove_race(ctx, name: str = None):
    await _delete_entry(ctx, "races", name)

# ── SPIRITS ───────────────────────────────────────────────────────────────────
@bot.command(name="spirit")
async def add_spirit(ctx, name: str = None, *args):
    """!spirit Salamander 5%dmg 5%maxhp fire=y:12%dmg 15%burndmg 10%dmgburning"""
    if not name or not args:
        await ctx.send("❌ Usage: `!spirit Name buffs... [grimoire=y:buffs...]`", delete_after=10)
        await ctx.message.delete(delay=5)
        return
    await _add_entry(ctx, "spirits", name, parse_spirit_args(args))

@bot.command(name="editspirit")
async def edit_spirit(ctx, name: str = None, *args):
    if not name or not args:
        await ctx.send("❌ Usage: `!editspirit Name buffs...`", delete_after=10)
        return
    await _edit_entry(ctx, "spirits", name, parse_spirit_args(args))

@bot.command(name="removespirit")
async def remove_spirit(ctx, name: str = None):
    await _delete_entry(ctx, "spirits", name)

# ── TITLES ────────────────────────────────────────────────────────────────────
@bot.command(name="title")
async def add_title(ctx, name: str = None, *args):
    """!title MythicWarrior 18%dmg 6%magicres"""
    if not name or not args:
        await ctx.send("❌ Usage: `!title Name buff1 buff2 ...`", delete_after=10)
        await ctx.message.delete(delay=5)
        return
    await _add_entry(ctx, "titles", name, [b for a in args if (b := parse_buff(a))])

@bot.command(name="edittitle")
async def edit_title(ctx, name: str = None, *args):
    if not name or not args:
        await ctx.send("❌ Usage: `!edittitle Name buff1 buff2 ...`", delete_after=10)
        return
    await _edit_entry(ctx, "titles", name, [b for a in args if (b := parse_buff(a))])

@bot.command(name="removetitle")
async def remove_title(ctx, name: str = None):
    await _delete_entry(ctx, "titles", name)

# ── GRIMORIOS ─────────────────────────────────────────────────────────────────
def parse_grimoire_args(args):
    skills = {}
    current_skill = None
    for arg in args:
        arg = arg.strip()
        hits_m = re.match(r'^hits:(\d+)$', arg, re.IGNORECASE)
        stage_m = re.match(r'^stage(\d+)=(-?\d+(?:\.\d+)?)%(\w+)$', arg, re.IGNORECASE)
        skill_m = re.match(r'^([A-Za-z0-9]+):(.+)$', arg)

        if hits_m and current_skill:
            skills[current_skill]["hits"] = int(hits_m.group(1))
        elif stage_m and current_skill:
            snum = f"stage{stage_m.group(1)}"
            if "stages" not in skills[current_skill]:
                skills[current_skill]["stages"] = {}
            skills[current_skill]["stages"][snum] = {
                "value": float(stage_m.group(2)),
                "type": stage_m.group(3)
            }
        elif skill_m:
            sname = skill_m.group(1).upper()
            rest = skill_m.group(2)
            if rest.lower().startswith("stage"):
                skills[sname] = {"damage": 0, "hits": 0, "stages": {}, "is_passive": True}
                current_skill = sname
                sm = re.match(r'stage(\d+)=(-?\d+(?:\.\d+)?)%(\w+)', rest, re.IGNORECASE)
                if sm:
                    skills[sname]["stages"][f"stage{sm.group(1)}"] = {
                        "value": float(sm.group(2)), "type": sm.group(3)
                    }
            else:
                try:
                    is_burn = rest.lower().endswith("~tick")
                    clean = rest[:-5] if is_burn else rest
                    skills[sname] = {"damage": parse_value(clean), "hits": 1, "is_burn": is_burn}
                    current_skill = sname
                except:
                    pass
    return skills

@bot.command(name="grimoire")
async def add_grimoire(ctx, name: str = None, *args):
    """
    !grimoire Mereo M1:8.9k Z:47.5k hits:3 V:264.3k E:313k G:stage1=5%stats stage2=10%stats stage3=25%stats
    """
    if get_channel_id("build") != ctx.channel.id: return
    if not name or not args:
        await ctx.send("❌ Usage: `!grimoire Name Skill:dmg hits:N ...`", delete_after=10)
        await ctx.message.delete(delay=5)
        return
    skills = parse_grimoire_args(args)
    data = load_data()
    data = ensure_builds(data)
    data["builds"]["grimorios"][name.lower()] = {"name": name, "skills": skills}
    save_data(data)
    await ctx.message.delete(delay=2)
    summary = ", ".join([
        k if v.get("is_passive") else f"{k}:{format_value(v['damage'])}x{v['hits']}"
        for k, v in skills.items()
    ])
    await ctx.send(f"✅ Grimoire `{name}` saved. Skills: `{summary}`", delete_after=10)

@bot.command(name="editgrimoire")
async def edit_grimoire(ctx, name: str = None, *args):
    if get_channel_id("build") != ctx.channel.id: return
    if not name or not args:
        await ctx.send("❌ Usage: `!editgrimoire Name Skill:dmg hits:N ...`", delete_after=10)
        return
    data = load_data()
    data = ensure_builds(data)
    if name.lower() not in data["builds"]["grimorios"]:
        await ctx.send(f"❌ Grimoire `{name}` not found.", delete_after=5)
        return
    skills = parse_grimoire_args(args)
    data["builds"]["grimorios"][name.lower()] = {"name": name, "skills": skills}
    save_data(data)
    await ctx.message.delete(delay=2)
    await ctx.send(f"✅ Grimoire `{name}` updated.", delete_after=8)

@bot.command(name="removegrimoire")
async def remove_grimoire(ctx, name: str = None):
    await _delete_entry(ctx, "grimorios", name)

# ── BUILD CALCULATOR ──────────────────────────────────────────────────────────
@bot.command(name="build")
async def calculate_build(ctx, grimoire_name: str = None, race_name: str = None,
                           trait_name: str = None, title_name: str = None, spirit_name: str = None):
    """
    !build Grimoire [Race] [Trait] [Title] [Spirit]
    Example: !build Mereo SpiritRace CritGenius MythicWarrior Salamander
    """
    if get_channel_id("build") != ctx.channel.id: return
    if not grimoire_name:
        await ctx.send("❌ Usage: `!build Grimoire [Race] [Trait] [Title] [Spirit]`", delete_after=10)
        await ctx.message.delete(delay=5)
        return

    data = load_data()
    grimoire = data.get("builds", {}).get("grimorios", {}).get(grimoire_name.lower())
    if not grimoire:
        await ctx.send(f"❌ Grimoire `{grimoire_name}` not found.", delete_after=5)
        await ctx.message.delete(delay=5)
        return

    total_dmg, crit_chance, crit_bonus, burning_bonus, burn_dmg_bonus, notes = get_total_dmg_bonus(
        trait=trait_name, race=race_name, spirit=spirit_name,
        title=title_name, grimoire_name=grimoire_name
    )

    multiplier      = 1 + (total_dmg / 100)
    crit_multiplier = multiplier * (1 + (crit_bonus / 100))
    burn_multiplier = 1 + (burn_dmg_bonus / 100)
    burning_multiplier = multiplier * (1 + (burning_bonus / 100))
    burning_crit_multiplier = crit_multiplier * (1 + (burning_bonus / 100))

    has_burning = burning_bonus > 0 or burn_dmg_bonus > 0

    embed = discord.Embed(
        title=f"⚔️ Build Calculator — {grimoire['name']}",
        color=discord.Color.gold()
    )

    parts = []
    if race_name:   parts.append(f"Race: `{race_name}`")
    if trait_name:  parts.append(f"Trait: `{trait_name}`")
    if title_name:  parts.append(f"Title: `{title_name}`")
    if spirit_name: parts.append(f"Spirit: `{spirit_name}`")
    embed.description = " • ".join(parts) if parts else "*No buffs applied*"

    # Buff summary
    buff_lines = [
        f"+{total_dmg:.1f}% Damage",
        f"+{crit_chance:.0f}% Crit Chance",
        f"+{crit_bonus:.0f}% Crit Bonus",
    ]
    if burning_bonus > 0:
        buff_lines.append(f"+{burning_bonus:.1f}% vs Burning enemies")
    if burn_dmg_bonus > 0:
        buff_lines.append(f"+{burn_dmg_bonus:.1f}% Burn tick damage")
    embed.add_field(name="📊 Total Buffs", value="\n".join(buff_lines), inline=False)

    # Skill damage
    skill_lines = []
    for skill_name, skill_data in grimoire["skills"].items():
        is_passive = skill_data.get("is_passive", False)
        stages = skill_data.get("stages", {})

        if is_passive and stages:
            stage_lines = [
                f"　{sk}: +{sv['value']}% {sv['type']}"
                for sk, sv in sorted(stages.items())
            ]
            skill_lines.append(f"**{skill_name}** *(passive/stages)*\n" + "\n".join(stage_lines))
            continue

        base = skill_data["damage"]
        hits = skill_data.get("hits", 1)
        is_burn_skill = skill_data.get("is_burn", False)

        if is_burn_skill:
            # Burn tick: apply burn_dmg_bonus
            dmg_buffed = base * burn_multiplier
            line = f"**{skill_name}** *(burn tick)*\n　Per tick: `{format_value(dmg_buffed)}`"
        elif hits > 1:
            dmg_buffed = base * multiplier
            dmg_crit   = base * crit_multiplier
            line = (
                f"**{skill_name}** ({hits} hits)\n"
                f"　No crit: `{format_value(dmg_buffed)}` total\n"
                f"　All crit: `{format_value(dmg_crit)}` total"
            )
            if has_burning:
                dmg_burn      = base * burning_multiplier
                dmg_burn_crit = base * burning_crit_multiplier
                line += (
                    f"\n　vs Burning no crit: `{format_value(dmg_burn)}` total"
                    f"\n　vs Burning all crit: `{format_value(dmg_burn_crit)}` total"
                )
        else:
            dmg_buffed = base * multiplier
            dmg_crit   = base * crit_multiplier
            line = (
                f"**{skill_name}**\n"
                f"　No crit: `{format_value(dmg_buffed)}`\n"
                f"　Crit: `{format_value(dmg_crit)}`"
            )
            if has_burning:
                dmg_burn      = base * burning_multiplier
                dmg_burn_crit = base * burning_crit_multiplier
                line += (
                    f"\n　vs Burning no crit: `{format_value(dmg_burn)}`"
                    f"\n　vs Burning crit: `{format_value(dmg_burn_crit)}`"
                )

        skill_lines.append(line)

    embed.add_field(
        name="💥 Skill Damage",
        value="\n\n".join(skill_lines) if skill_lines else "No skills found",
        inline=False
    )

    if notes:
        embed.add_field(name="📝 Notes", value="\n".join(notes), inline=False)

    embed.set_footer(text=f"Base x{multiplier:.3f} • Crit x{crit_multiplier:.3f}")
    await ctx.message.delete(delay=2)
    await ctx.send(embed=embed)

# ── LIST BUILDS ───────────────────────────────────────────────────────────────
BUILD_CATEGORIES = ["traits", "races", "spirits", "titles", "grimorios"]

@bot.command(name="builds")
async def list_builds(ctx, category: str = None):
    if get_channel_id("build") != ctx.channel.id: return
    data = load_data()
    builds = data.get("builds", {})
    if not category:
        embed = discord.Embed(title="📚 Build Database", color=discord.Color.gold())
        for cat in BUILD_CATEGORIES:
            items = builds.get(cat, {})
            names = ", ".join([v.get("name", k) for k, v in items.items()]) if items else "*empty*"
            embed.add_field(name=f"{cat.capitalize()} ({len(items)})", value=names, inline=False)
        await ctx.message.delete(delay=2)
        await ctx.send(embed=embed)
        return

    category = category.lower()
    if category not in BUILD_CATEGORIES:
        await ctx.send(f"❌ Invalid category. Options: `{'`, `'.join(BUILD_CATEGORIES)}`", delete_after=5)
        await ctx.message.delete(delay=5)
        return

    items = builds.get(category, {})
    if not items:
        await ctx.send(f"❌ No entries in `{category}` yet.", delete_after=5)
        await ctx.message.delete(delay=5)
        return

    embed = discord.Embed(title=f"📚 {category.capitalize()}", color=discord.Color.gold())
    for key, val in items.items():
        if category == "grimorios":
            skills_summary = ", ".join([
                k if v.get("is_passive") else f"{k}:{format_value(v['damage'])}x{v['hits']}"
                for k, v in val.get("skills", {}).items()
            ])
            embed.add_field(name=val.get("name", key), value=skills_summary or "*no skills*", inline=False)
        else:
            buffs = val.get("buffs", [])
            buff_str = " ".join([
                f"{'-' if b.get('negative') else '+'}{b['value']}%{b['type']}"
                for b in buffs if b.get("type") not in ("grimoire_req", "hp_threshold")
            ])
            embed.add_field(name=val.get("name", key), value=buff_str or "*no buffs*", inline=False)

    await ctx.message.delete(delay=2)
    await ctx.send(embed=embed)

# ══════════════════════════════════════════════════════════════════════════════
#  GLOBAL — SETCANAL / REMOVECANAL
# ══════════════════════════════════════════════════════════════════════════════

VALID_MODULES = ["boss", "hackers", "build", "trading", "aw3"]

@bot.command(name="setcanal")
async def set_canal(ctx, module: str = None, channel: str = None):
    """!setcanal boss #channel  OR  !setcanal boss 123456789"""
    if not module or module.lower() not in VALID_MODULES:
        await ctx.send(f"❌ Invalid module. Options: `boss`, `hackers`, `build`, `trading`, `aw3`", delete_after=10)
        return
    module = module.lower()
    channel_id = None
    if ctx.message.channel_mentions:
        channel_id = ctx.message.channel_mentions[0].id
    elif channel and channel.isdigit():
        channel_id = int(channel)
    else:
        channel_id = ctx.channel.id
    resolved = ctx.guild.get_channel(channel_id)
    if not resolved:
        await ctx.send("❌ Channel not found.", delete_after=10)
        return
    config = load_config()
    config["channels"][module] = channel_id
    save_config(config)
    await ctx.message.delete(delay=2)
    await ctx.send(f"✅ Module `{module}` configured to {resolved.mention}", delete_after=10)

@bot.command(name="removecanal")
async def remove_canal(ctx, module: str = None):
    """!removecanal boss / hackers / build"""
    if not module or module.lower() not in VALID_MODULES:
        await ctx.send(f"❌ Invalid module. Options: `boss`, `hackers`, `build`, `trading`, `aw3`", delete_after=10)
        await ctx.message.delete(delay=5)
        return
    module = module.lower()
    config = load_config()
    if module not in config.get("channels", {}):
        await ctx.send(f"❌ Module `{module}` has no channel configured.", delete_after=5)
        await ctx.message.delete(delay=5)
        return
    del config["channels"][module]
    save_config(config)
    await ctx.message.delete(delay=2)
    await ctx.send(f"🗑️ Channel for module `{module}` removed.", delete_after=8)

# ══════════════════════════════════════════════════════════════════════════════
#  EVENTS
# ══════════════════════════════════════════════════════════════════════════════

@bot.event
async def on_ready():
    print(f"✅ Bot connected as {bot.user}")
    if not os.path.exists(DATA_FILE):
        save_data({"hackers": {}, "builds": {"traits": {}, "races": {}, "spirits": {}, "titles": {}, "grimorios": {}}})
    update_tracker.start()
    update_timers.start()

@bot.event
async def on_message(message):
    global tracker_message
    if message.author.bot:
        return
    boss_channel_id = get_channel_id("boss")
    if boss_channel_id and message.channel.id == boss_channel_id:
        content = message.content.strip()
        if not content.startswith("!"):
            parts = content.split()
            if len(parts) >= 2:
                username = parts[0]
                time_str = parts[1]
                boss_name = parts[2] if len(parts) >= 3 else ""
                minutes = parse_time(time_str)
                if minutes:
                    spawn_time = datetime.utcnow() + timedelta(minutes=minutes)
                    boss_list[username] = {
                        "spawn": spawn_time,
                        "boss": boss_name,
                        "discord_author": message.author
                    }
                    alerted.discard(username)
                    await message.add_reaction("✅")
                    await message.delete(delay=3)
                    await refresh_tracker(message.channel)
                    return
            await message.add_reaction("❓")
            await message.delete(delay=5)
    await bot.process_commands(message)

# ══════════════════════════════════════════════════════════════════════════════
#  MODULE 3 — COMPARE & BESTBUILD
# ══════════════════════════════════════════════════════════════════════════════

def calculate_build_stats(grimoire_name, trait=None, race=None, title=None, spirit=None):
    data = load_data()
    grimoire = data.get("builds", {}).get("grimorios", {}).get(grimoire_name.lower())
    if not grimoire:
        return None

    total_dmg, crit_chance, crit_bonus, burning_bonus, burn_dmg_bonus, notes = get_total_dmg_bonus(
        trait=trait, race=race, spirit=spirit, title=title, grimoire_name=grimoire_name
    )

    multiplier        = 1 + (total_dmg / 100)
    crit_multiplier   = multiplier * (1 + (crit_bonus / 100))
    burn_multiplier   = 1 + (burn_dmg_bonus / 100)
    burning_mult      = multiplier * (1 + (burning_bonus / 100))
    burning_crit_mult = crit_multiplier * (1 + (burning_bonus / 100))

    builds = data.get("builds", {})
    lifesteal = 0.0
    for category, key in [("traits", trait), ("races", race), ("spirits", spirit), ("titles", title)]:
        if key:
            entry = builds.get(category, {}).get(key.lower())
            if entry:
                for b in entry.get("buffs", []):
                    if b.get("type") == "lifesteal":
                        lifesteal += b.get("value", 0)

    top_skill = None
    top_dmg = 0
    skill_results = {}

    for sname, sdata in grimoire["skills"].items():
        if sdata.get("is_passive"):
            continue
        base = sdata["damage"]
        hits = sdata.get("hits", 1)
        skill_type = sdata.get("skill_type", "hit")

        if skill_type == "tick":
            # Tick: no crit, affected by burn bonus
            dmg = base * (1 + burn_dmg_bonus / 100) * hits
            skill_results[sname] = {"no_crit": dmg, "crit": dmg, "burn": dmg, "is_burn": True}
        elif skill_type == "total":
            # Total: 1 hit, full buffs + crit
            dmg_nc = base * multiplier
            dmg_c  = base * crit_multiplier
            dmg_b  = base * burning_mult
            dmg_bc = base * burning_crit_mult
            skill_results[sname] = {"no_crit": dmg_nc, "crit": dmg_c, "burn": dmg_b, "burn_crit": dmg_bc}
            if dmg_nc > top_dmg:
                top_dmg = dmg_nc
                top_skill = sname
        else:
            # Hit: independent hits, each buffed
            dmg_nc = base * multiplier * hits
            dmg_c  = base * crit_multiplier * hits
            dmg_b  = base * burning_mult * hits
            dmg_bc = base * burning_crit_mult * hits
            skill_results[sname] = {"no_crit": dmg_nc, "crit": dmg_c, "burn": dmg_b, "burn_crit": dmg_bc}
            if dmg_nc > top_dmg:
                top_dmg = dmg_nc
                top_skill = sname

    total_burst      = sum(v["no_crit"] for v in skill_results.values())
    total_burst_crit = sum(v["crit"] for v in skill_results.values())

    return {
        "grimoire": grimoire, "grimoire_name": grimoire_name,
        "total_dmg": total_dmg, "crit_chance": crit_chance, "crit_bonus": crit_bonus,
        "burning_bonus": burning_bonus, "burn_dmg_bonus": burn_dmg_bonus,
        "lifesteal": lifesteal, "multiplier": multiplier, "crit_multiplier": crit_multiplier,
        "has_burning": burning_bonus > 0 or burn_dmg_bonus > 0,
        "top_skill": top_skill, "top_dmg": top_dmg,
        "skill_results": skill_results,
        "total_burst": total_burst, "total_burst_crit": total_burst_crit,
        "notes": notes,
        "trait": trait, "race": race, "title": title, "spirit": spirit
    }


@bot.command(name="compare")
async def compare_builds(ctx, grimoire1: str = None, grimoire2: str = None,
                          race: str = None, trait: str = None,
                          title: str = None, spirit: str = None):
    """
    !compare Grimoire1 Grimoire2 [Race] [Trait] [Title] [Spirit]
    Example: !compare Mereo Light SpiritRace CritGenius MythicWarrior Salamander
    """
    if get_channel_id("build") != ctx.channel.id: return
    if not grimoire1 or not grimoire2:
        await ctx.send("❌ Usage: `!compare Grimoire1 Grimoire2 [Race] [Trait] [Title] [Spirit]`", delete_after=10)
        await ctx.message.delete(delay=5)
        return

    s1 = calculate_build_stats(grimoire1, trait=trait, race=race, title=title, spirit=spirit)
    s2 = calculate_build_stats(grimoire2, trait=trait, race=race, title=title, spirit=spirit)

    if not s1:
        await ctx.send(f"❌ Grimoire `{grimoire1}` not found.", delete_after=5)
        await ctx.message.delete(delay=5)
        return
    if not s2:
        await ctx.send(f"❌ Grimoire `{grimoire2}` not found.", delete_after=5)
        await ctx.message.delete(delay=5)
        return

    embed = discord.Embed(
        title=f"⚔️ {s1['grimoire']['name']} vs {s2['grimoire']['name']}",
        color=discord.Color.purple()
    )

    parts = []
    if race:   parts.append(f"Race: `{race}`")
    if trait:  parts.append(f"Trait: `{trait}`")
    if title:  parts.append(f"Title: `{title}`")
    if spirit: parts.append(f"Spirit: `{spirit}`")
    embed.description = " • ".join(parts) if parts else "*No buffs applied*"

    buff_line = (
        f"`{s1['grimoire']['name']}` → +{s1['total_dmg']:.1f}% dmg | "
        f"+{s1['crit_chance']:.0f}% crit | +{s1['crit_bonus']:.0f}% critdmg\n"
        f"`{s2['grimoire']['name']}` → +{s2['total_dmg']:.1f}% dmg | "
        f"+{s2['crit_chance']:.0f}% crit | +{s2['crit_bonus']:.0f}% critdmg"
    )
    embed.add_field(name="📊 Buffs", value=buff_line, inline=False)

    all_skills = sorted(set(list(s1["skill_results"].keys()) + list(s2["skill_results"].keys())))
    skill_lines = []
    for sname in all_skills:
        d1 = s1["skill_results"].get(sname)
        d2 = s2["skill_results"].get(sname)
        if not d1 or not d2:
            continue
        v1, v2 = d1["no_crit"], d2["no_crit"]
        if v1 == 0 and v2 == 0:
            continue
        if v1 > v2:
            diff = ((v1 - v2) / v2 * 100) if v2 > 0 else 0
            winner = f"← {s1['grimoire']['name']} +{diff:.1f}%"
        elif v2 > v1:
            diff = ((v2 - v1) / v1 * 100) if v1 > 0 else 0
            winner = f"← {s2['grimoire']['name']} +{diff:.1f}%"
        else:
            winner = "← Tie"
        skill_lines.append(
            f"**{sname}** {winner}\n"
            f"　{s1['grimoire']['name']}: `{format_value(v1)}`  |  "
            f"{s2['grimoire']['name']}: `{format_value(v2)}`"
        )

    embed.add_field(
        name="💥 Skill Comparison (no crit)",
        value="\n\n".join(skill_lines) if skill_lines else "No comparable skills",
        inline=False
    )

    if s1["total_burst"] > s2["total_burst"]:
        diff = (s1["total_burst"] - s2["total_burst"]) / s2["total_burst"] * 100
        overall = f"🏆 **{s1['grimoire']['name']}** wins overall by **{diff:.1f}%**"
        loser_name, loser_stats = grimoire2, s2
    elif s2["total_burst"] > s1["total_burst"]:
        diff = (s2["total_burst"] - s1["total_burst"]) / s1["total_burst"] * 100
        overall = f"🏆 **{s2['grimoire']['name']}** wins overall by **{diff:.1f}%**"
        loser_name, loser_stats = grimoire1, s1
    else:
        overall = "🤝 Both grimoires are equal in total burst"
        loser_name, loser_stats = None, None

    embed.add_field(name="🏆 Overall", value=overall, inline=False)

    if loser_name and loser_stats:
        data = load_data()
        builds = data.get("builds", {})
        recs = []

        for category, cur_key, label in [
            ("traits", trait, "Trait"),
            ("races", race, "Race"),
            ("titles", title, "Title"),
            ("spirits", spirit, "Spirit")
        ]:
            best_boost = 0
            best_name = None
            for key, val in builds.get(category, {}).items():
                kw = {
                    "trait": val["name"] if category == "traits" else trait,
                    "race":  val["name"] if category == "races"  else race,
                    "title": val["name"] if category == "titles" else title,
                    "spirit":val["name"] if category == "spirits" else spirit,
                }
                test = calculate_build_stats(loser_name, **kw)
                if test and test["total_burst"] > loser_stats["total_burst"]:
                    boost = (test["total_burst"] - loser_stats["total_burst"]) / loser_stats["total_burst"] * 100
                    if boost > best_boost:
                        best_boost = boost
                        best_name = val["name"]
            if best_name:
                recs.append(f"💡 {label} → `{best_name}` (+{best_boost:.1f}% burst)")

        embed.add_field(
            name=f"📝 Recommendations for {loser_stats['grimoire']['name']}",
            value="\n".join(recs) if recs else "No better options found in the database.",
            inline=False
        )

    await ctx.message.delete(delay=2)
    await ctx.send(embed=embed)


@bot.command(name="bestbuild")
async def best_build(ctx, grimoire_name: str = None):
    """
    !bestbuild GrimoireName
    Returns 3 optimized builds: max damage, max sustain, max burning
    """
    if get_channel_id("build") != ctx.channel.id: return
    if not grimoire_name:
        await ctx.send("❌ Usage: `!bestbuild GrimoireName`", delete_after=10)
        await ctx.message.delete(delay=5)
        return

    data = load_data()
    builds = data.get("builds", {})

    if grimoire_name.lower() not in builds.get("grimorios", {}):
        await ctx.send(f"❌ Grimoire `{grimoire_name}` not found.", delete_after=5)
        await ctx.message.delete(delay=5)
        return

    traits  = [None] + list(builds.get("traits",  {}).values())
    races   = [None] + list(builds.get("races",   {}).values())
    titles  = [None] + list(builds.get("titles",  {}).values())
    spirits = [None] + list(builds.get("spirits", {}).values())

    best_dmg     = None
    best_sustain = None
    best_burning = None

    total_combos = len(traits) * len(races) * len(titles) * len(spirits)

    for t in traits:
        for r in races:
            for ti in titles:
                for sp in spirits:
                    tname  = t["name"]  if t  else None
                    rname  = r["name"]  if r  else None
                    tiname = ti["name"] if ti else None
                    spname = sp["name"] if sp else None

                    stats = calculate_build_stats(
                        grimoire_name,
                        trait=tname, race=rname,
                        title=tiname, spirit=spname
                    )
                    if not stats:
                        continue

                    combo = (tname, rname, tiname, spname)

                    # Max damage
                    if best_dmg is None or stats["total_burst"] > best_dmg["total_burst"]:
                        best_dmg = {**stats, "combo": combo}

                    # Max sustain
                    if best_sustain is None or stats["lifesteal"] > best_sustain["lifesteal"]:
                        best_sustain = {**stats, "combo": combo}
                    elif stats["lifesteal"] == best_sustain["lifesteal"] and stats["total_burst"] > best_sustain["total_burst"]:
                        best_sustain = {**stats, "combo": combo}

                    # Max burning
                    burn_score = stats["total_burst"] * (1 + stats["burning_bonus"] / 100)
                    if best_burning is None or burn_score > best_burning.get("burn_score", 0):
                        best_burning = {**stats, "combo": combo, "burn_score": burn_score}

    def combo_str(combo):
        t, r, ti, sp = combo
        parts = []
        if r:  parts.append(f"Race: `{r}`")
        if t:  parts.append(f"Trait: `{t}`")
        if ti: parts.append(f"Title: `{ti}`")
        if sp: parts.append(f"Spirit: `{sp}`")
        return " • ".join(parts) if parts else "*No buffs*"

    grimoire_data = builds["grimorios"][grimoire_name.lower()]
    embed = discord.Embed(
        title=f"⚔️ Best Builds — {grimoire_data['name']}",
        color=discord.Color.gold()
    )

    if best_dmg:
        top = best_dmg["top_skill"]
        top_val = best_dmg["skill_results"].get(top, {}).get("no_crit", 0) if top else 0
        embed.add_field(
            name="🥇 Max Damage",
            value=(
                f"{combo_str(best_dmg['combo'])}\n"
                f"Total burst: `{format_value(best_dmg['total_burst'])}` | "
                f"Crit: `{format_value(best_dmg['total_burst_crit'])}`\n"
                f"Top skill: **{top}** → `{format_value(top_val)}`"
            ),
            inline=False
        )

    if best_sustain:
        embed.add_field(
            name="🛡️ Max Sustain (Lifesteal)",
            value=(
                f"{combo_str(best_sustain['combo'])}\n"
                f"Lifesteal: `{best_sustain['lifesteal']:.1f}%` | "
                f"Total burst: `{format_value(best_sustain['total_burst'])}`"
            ),
            inline=False
        )

    if best_burning:
        embed.add_field(
            name="🔥 Max Burning Damage",
            value=(
                f"{combo_str(best_burning['combo'])}\n"
                f"Burning bonus: `+{best_burning['burning_bonus']:.1f}%` | "
                f"Burn tick: `+{best_burning['burn_dmg_bonus']:.1f}%`\n"
                f"Total burst vs burning: `{format_value(best_burning['burn_score'])}`"
            ),
            inline=False
        )

    embed.set_footer(text=f"Analyzed {total_combos} combinations")
    await ctx.message.delete(delay=2)
    await ctx.send(embed=embed)

@bot.command(name="addskill")
async def add_skill(ctx, grimoire_name: str = None, *args):
    """
    Add skills to an existing grimoire without overwriting it.
    !addskill GrimoireName Skill:damage [hits:N] [Skill2:damage~tick] ...
    Examples:
      !addskill Light E:61.7k~tick
      !addskill Mereo BURN:1.6k~tick
      !addskill Light V:14.98k hits:18
    """
    build_channel_id = get_channel_id("build")
    if not build_channel_id or ctx.channel.id != build_channel_id:
        return
    if not grimoire_name or not args:
        await ctx.send("❌ Usage: `!addskill GrimoireName Skill:damage [hits:N]`", delete_after=10)
        await ctx.message.delete(delay=5)
        return

    data = load_data()
    data = ensure_builds(data)

    if grimoire_name.lower() not in data["builds"]["grimorios"]:
        await ctx.send(f"❌ Grimoire `{grimoire_name}` not found. Use `!grimoire` to create it first.", delete_after=8)
        await ctx.message.delete(delay=5)
        return

    existing_skills = data["builds"]["grimorios"][grimoire_name.lower()]["skills"]

    # Parse new skills
    new_skills = {}
    current_skill = None
    for arg in args:
        arg = arg.strip()
        hits_match = re.match(r'^hits:(\d+)$', arg, re.IGNORECASE)
        stage_match = re.match(r'^stage(\d+)=(-?\d+(?:\.\d+)?)%(\w+)$', arg, re.IGNORECASE)
        skill_match = re.match(r'^([A-Za-z]+):(.+)$', arg)

        if hits_match and current_skill:
            new_skills[current_skill]["hits"] = int(hits_match.group(1))
        elif stage_match and current_skill:
            stage_num = int(stage_match.group(1))
            val = float(stage_match.group(2))
            btype = stage_match.group(3)
            if "stages" not in new_skills[current_skill]:
                new_skills[current_skill]["stages"] = {}
            new_skills[current_skill]["stages"][f"stage{stage_num}"] = {"value": val, "type": btype}
        elif skill_match:
            skill_name = skill_match.group(1).upper()
            rest = skill_match.group(2)
            if rest.lower().startswith("stage"):
                new_skills[skill_name] = {"damage": 0, "hits": 0, "stages": {}, "is_passive": True}
                current_skill = skill_name
                sm = re.match(r'stage(\d+)=(-?\d+(?:\.\d+)?)%(\w+)', rest, re.IGNORECASE)
                if sm:
                    new_skills[skill_name]["stages"][f"stage{sm.group(1)}"] = {"value": float(sm.group(2)), "type": sm.group(3)}
            else:
                try:
                    if rest.lower().endswith("~tick"):
                        skill_type = "tick"
                        clean = rest[:-5]
                    elif rest.lower().endswith("~total"):
                        skill_type = "total"
                        clean = rest[:-6]
                    else:
                        skill_type = "hit"
                        clean = rest
                    new_skills[skill_name] = {
                        "damage": parse_value(clean),
                        "hits": 1,
                        "skill_type": skill_type,
                        "is_burn": skill_type == "tick"
                    }
                    current_skill = skill_name
                except:
                    pass

    if not new_skills:
        await ctx.send("❌ No valid skills found in input.", delete_after=8)
        await ctx.message.delete(delay=5)
        return

    # Merge with existing skills
    existing_skills.update(new_skills)
    data["builds"]["grimorios"][grimoire_name.lower()]["skills"] = existing_skills
    save_data(data)

    skill_list = ", ".join([
        f"{k}:{format_value(v['damage'])}x{v.get('hits',1)}({'tick' if v.get('skill_type')=='tick' else 'hit'})"
        for k, v in new_skills.items() if not v.get("is_passive") or not v.get("stages")
    ])
    await ctx.message.delete(delay=2)
    await ctx.send(f"✅ Added to `{grimoire_name}`: `{skill_list}`", delete_after=10)


# ══════════════════════════════════════════════════════════════════════════════
#  MODULE 4 — TRADING
# ══════════════════════════════════════════════════════════════════════════════

VALID_CURRENCIES = ["yens", "robux", "offer"]
VALID_TYPES = ["key", "fragment", "bundle", "item", "limited"]
TRADE_ENTRIES_PER_PAGE = 10

def get_rate():
    config = load_config()
    return config.get("yen_rate", 799)  # Default: 100k yens = 799 RBX

def get_farm_rate():
    config = load_config()
    return config.get("farm_rate", 222000)  # Default: 222k yens/hour

def get_grind_index_label(hours):
    """Returns a label and bar based on expected grind hours"""
    if hours <= 1:
        return "EASY", "██░░░░░░░░"
    elif hours <= 5:
        return "MODERATE", "████░░░░░░"
    elif hours <= 20:
        return "HARD", "██████░░░░"
    elif hours <= 60:
        return "VERY HARD", "████████░░"
    else:
        return "EXTREME", "██████████"

def calc_item_stats(item):
    """Dynamically calculate grind index and equivalences using current rate"""
    if not item or "cost_yens" not in item:
        return {
            "cost_yens": 0, "cost_rbx": 0,
            "grind_index_rbx": 0, "grind_hours": 0,
            "grind_label": "UNKNOWN", "grind_bar": "░░░░░░░░░░",
            "julius_equiv": 0, "wizard_equiv": 0,
            "market_price": 0, "market_currency": "",
            "worth_trading": False, "hours_saved": 0
        }
    rate = get_rate()
    farm_rate = get_farm_rate()
    cost_yens = item["cost_yens"]
    cost_rbx = yens_to_rbx(cost_yens, rate)
    probability = item["probability"]
    
    # Grind Index (formerly EV)
    grind_index_rbx = calc_ev(cost_rbx, probability)
    
    # Farmeo time
    cost_hours = cost_yens / farm_rate if farm_rate > 0 else 0
    grind_hours = cost_hours / probability if probability > 0 else 0
    
    grind_label, grind_bar = get_grind_index_label(grind_hours)
    
    # Market price
    market_price = item.get("market_price_rbx", 0)
    market_currency = item.get("market_currency", "")
    
    # Worth trading?
    worth_trading = market_price > 0 and grind_index_rbx > market_price
    hours_saved = grind_hours - (market_price / rate * 100000 / farm_rate) if worth_trading and farm_rate > 0 else 0
    
    return {
        "cost_yens": cost_yens,
        "cost_rbx": cost_rbx,
        "grind_index_rbx": grind_index_rbx,
        "grind_hours": grind_hours,
        "grind_label": grind_label,
        "grind_bar": grind_bar,
        "julius_equiv": grind_index_rbx / 799,
        "wizard_equiv": grind_index_rbx / 699,
        "market_price": market_price,
        "market_currency": market_currency,
        "worth_trading": worth_trading,
        "hours_saved": hours_saved
    }

def yens_to_rbx(yens, rate=None):
    if rate is None:
        rate = get_rate()
    return yens * (rate / 100000)

def calc_ev(cost_rbx, probability):
    if probability <= 0:
        return 0
    return cost_rbx / probability

def load_trading():
    data = load_data()
    if "trading" not in data:
        data["trading"] = {"items": {}, "listings": {}}
        save_data(data)
    return data["trading"]

def save_trading(trading):
    data = load_data()
    data["trading"] = trading
    save_data(data)

# ── ADMIN: Set exchange rate ───────────────────────────────────────────────────
@bot.command(name="setrate")
async def set_rate(ctx, rate: float = None):
    """
    !setrate 799
    Sets how many RBX = 100k Yens
    """
    trading_channel_id = get_channel_id("trading")
    if not trading_channel_id or ctx.channel.id != trading_channel_id:
        return
    if not rate:
        await ctx.send("❌ Usage: `!setrate amount` (e.g: `!setrate 799`)", delete_after=8)
        await ctx.message.delete(delay=5)
        return
    config = load_config()
    config["yen_rate"] = rate
    save_config(config)
    await ctx.message.delete(delay=2)
    await ctx.send(f"✅ Exchange rate updated: `100k Yens = {rate} RBX`", delete_after=10)

@bot.command(name="setfarmrate")
async def set_farm_rate(ctx, yens: float = None, period: str = "1h"):
    """
    !setfarmrate 222000 1h
    Sets how many Yens can be farmed per hour
    Supports: 1h, 30m, 10m
    """
    trading_channel_id = get_channel_id("trading")
    if not trading_channel_id or ctx.channel.id != trading_channel_id:
        return
    if not yens:
        await ctx.send("❌ Usage: `!setfarmrate 222000 1h`", delete_after=8)
        await ctx.message.delete(delay=5)
        return

    # Convert to yens per hour
    period = period.lower()
    if period.endswith("m"):
        minutes = int(period[:-1])
        yens_per_hour = yens * (60 / minutes)
    else:
        yens_per_hour = yens

    config = load_config()
    config["farm_rate"] = yens_per_hour
    save_config(config)
    await ctx.message.delete(delay=2)
    await ctx.send(
        f"✅ Farm rate updated: `{yens:,.0f} Yens per {period}` → `{yens_per_hour:,.0f} Yens/hour`",
        delete_after=10
    )

@bot.command(name="setprice")
async def set_market_price(ctx, name: str = None, price: str = None, currency: str = "robux"):
    """
    !setprice FireFragment 799 robux
    Sets the real market price for an item
    """
    trading_channel_id = get_channel_id("trading")
    if not trading_channel_id or ctx.channel.id != trading_channel_id:
        return
    if not name or not price:
        await ctx.send("❌ Usage: `!setprice ItemName price robux/yens`", delete_after=8)
        await ctx.message.delete(delay=5)
        return

    try:
        price_val = float(price)
    except:
        await ctx.send("❌ Price must be a number.", delete_after=5)
        await ctx.message.delete(delay=5)
        return

    trading = load_trading()
    if name.lower() not in trading["items"]:
        await ctx.send(f"❌ Item `{name}` not found.", delete_after=5)
        await ctx.message.delete(delay=5)
        return

    rate = get_rate()
    if currency.lower() == "yens":
        price_rbx = yens_to_rbx(price_val, rate)
    else:
        price_rbx = price_val

    trading["items"][name.lower()]["market_price_rbx"] = price_rbx
    trading["items"][name.lower()]["market_currency"] = currency.lower()
    save_trading(trading)

    await ctx.message.delete(delay=2)
    await ctx.send(
        f"✅ Market price for `{name}` set to `{price_val:,.0f} {currency.upper()}` (~{price_rbx:.1f} RBX)",
        delete_after=10
    )

# ── ADMIN: Register item in database ──────────────────────────────────────────
@bot.command(name="additem")
async def add_item(ctx, name: str = None, cost: str = None, prob: str = None, item_type: str = None):
    """
    !additem FireKey 250000yens 10% type:key
    !additem FireFragment 250000yens 1% type:fragment
    !additem JuliusBundle 0yens 100% type:bundle
    """
    trading_channel_id = get_channel_id("trading")
    if not trading_channel_id or ctx.channel.id != trading_channel_id:
        return
    if not name or not cost or not prob:
        await ctx.send(
            "❌ Usage: `!additem Name 250000yens 10% type:key`\n"
            "Types: `key`, `fragment`, `bundle`, `item`",
            delete_after=10
        )
        await ctx.message.delete(delay=5)
        return

    # Parse cost
    cost_match = re.match(r'(\d+(?:\.\d+)?)(yens|robux)', cost.lower())
    if not cost_match:
        await ctx.send("❌ Cost format: `250000yens` or `799robux`", delete_after=8)
        await ctx.message.delete(delay=5)
        return

    cost_val = float(cost_match.group(1))
    cost_currency = cost_match.group(2)

    # Parse probability
    prob_match = re.match(r'(\d+(?:\.\d+)?)%', prob)
    if not prob_match:
        await ctx.send("❌ Probability format: `10%` or `0.5%`", delete_after=8)
        await ctx.message.delete(delay=5)
        return
    probability = float(prob_match.group(1)) / 100

    # Parse type
    parsed_type = "item"
    if item_type:
        type_match = re.match(r'type:(\w+)', item_type.lower())
        if type_match and type_match.group(1) in VALID_TYPES:
            parsed_type = type_match.group(1)

    # Only save base data - EV calculated dynamically
    if cost_currency == "yens":
        cost_yens = cost_val
    else:
        # Convert RBX to Yens for storage
        rate = get_rate()
        cost_yens = cost_val * (100000 / rate)

    trading = load_trading()
    trading["items"][name.lower()] = {
        "name": name,
        "cost_yens": cost_yens,
        "cost_currency": cost_currency,
        "probability": probability,
        "type": parsed_type
    }
    save_trading(trading)

    # Calculate dynamically for display
    stats = calc_item_stats(trading["items"][name.lower()])

    await ctx.message.delete(delay=2)
    embed = discord.Embed(title="✅ Item Registered", color=discord.Color.green())
    embed.add_field(name="Name", value=f"`{name}`", inline=True)
    embed.add_field(name="Type", value=f"`{parsed_type}`", inline=True)
    embed.add_field(name="Drop Rate", value=f"`{probability*100:.2f}%`", inline=True)
    embed.add_field(name="Cost (Yens)", value=f"`{stats['cost_yens']:,.0f}`", inline=True)
    embed.add_field(name="Cost (RBX)", value=f"`{stats['cost_rbx']:.2f}`", inline=True)
    embed.add_field(name="Grind Index (RBX)", value=f"`{stats['grind_index_rbx']:,.2f}`", inline=True)
    embed.add_field(name="Julius Bundle Equiv", value=f"`{stats['julius_equiv']:.1f}x`", inline=True)
    embed.add_field(name="Wizard Bundle Equiv", value=f"`{stats['wizard_equiv']:.1f}x`", inline=True)
    await ctx.send(embed=embed, delete_after=20)

@bot.command(name="removeitem")
async def remove_item(ctx, name: str = None):
    """!removeitem FireKey"""
    trading_channel_id = get_channel_id("trading")
    if not trading_channel_id or ctx.channel.id != trading_channel_id:
        return
    if not name:
        await ctx.send("❌ Usage: `!removeitem Name`", delete_after=5)
        await ctx.message.delete(delay=5)
        return
    trading = load_trading()
    if name.lower() not in trading["items"]:
        await ctx.send(f"❌ Item `{name}` not found.", delete_after=5)
        await ctx.message.delete(delay=5)
        return
    del trading["items"][name.lower()]
    save_trading(trading)
    await ctx.message.delete(delay=2)
    await ctx.send(f"🗑️ Item `{name}` removed from database.", delete_after=8)

# ── USER: List item for sale ───────────────────────────────────────────────────
@bot.command(name="sell")
async def sell_item(ctx, name: str = None, quantity: str = None, price: str = None, currency: str = None, *, comment: str = None):
    """
    !sell FireKey 2 250000 yens
    !sell FireKey 1 799 robux
    !sell FireKey 3 offer
    !sell FireFragment 1 offer 1 julius bundle + 2 wind frags
    """
    trading_channel_id = get_channel_id("trading")
    if not trading_channel_id or ctx.channel.id != trading_channel_id:
        return

    if not name:
        await ctx.send(
            "❌ Usage:\n"
            "`!sell ItemName quantity price yens`\n"
            "`!sell ItemName quantity price robux`\n"
            "`!sell ItemName quantity offer`",
            delete_after=10
        )
        await ctx.message.delete(delay=5)
        return

    trading = load_trading()

    # Check item exists
    if name.lower() not in trading["items"]:
        await ctx.send(
            f"❌ Item `{name}` not in database. Ask an admin to register it with `!additem`.",
            delete_after=8
        )
        await ctx.message.delete(delay=5)
        return

    # Parse quantity
    try:
        qty = int(quantity)
        if qty <= 0:
            raise ValueError
    except:
        await ctx.send("❌ Quantity must be a positive number.", delete_after=5)
        await ctx.message.delete(delay=5)
        return

    # Parse price + currency
    # Handle: !sell FireKey 3 offer  (quantity=3, price="offer", currency=None)
    if price and price.lower() == "offer":
        listing_price = 0
        listing_currency = "offer"
    elif price and currency and currency.lower() in ["yens", "robux"]:
        try:
            listing_price = float(price)
            listing_currency = currency.lower()
        except:
            await ctx.send("❌ Price must be a number.", delete_after=5)
            await ctx.message.delete(delay=5)
            return
    else:
        await ctx.send(
            "❌ Specify currency: `yens`, `robux`, or `offer`\n"
            "Example: `!sell FireKey 2 250000 yens`",
            delete_after=8
        )
        await ctx.message.delete(delay=5)
        return

    # Calculate RBX equivalent
    rate = get_rate()
    item_data = trading["items"][name.lower()]
    if listing_currency == "yens":
        price_rbx = yens_to_rbx(listing_price, rate)
        price_display = f"{listing_price:,.0f} Yens (~{price_rbx:.1f} RBX)"
    elif listing_currency == "robux":
        price_rbx = listing_price
        price_display = f"{listing_price:.0f} RBX"
    else:
        price_rbx = 0
        price_display = "Open to offers"

    # Create listing
    stats = calc_item_stats(item_data)
    listing_id = f"{ctx.author.id}_{name.lower()}_{int(datetime.utcnow().timestamp())}"
    trading["listings"][listing_id] = {
        "item_name": name,
        "item_key": name.lower(),
        "quantity": qty,
        "price": listing_price,
        "currency": listing_currency,
        "price_rbx": price_rbx,
        "price_display": price_display,
        "comment": comment or "",
        "seller": str(ctx.author),
        "seller_id": ctx.author.id,
        "seller_mention": ctx.author.mention,
        "date": str(datetime.utcnow()),
        "item_type": item_data["type"],
        "item_key_ref": name.lower()
    }
    save_trading(trading)

    await ctx.message.delete(delay=3)
    embed = discord.Embed(title="🛒 New Listing", color=discord.Color.blue())
    embed.add_field(name="Item", value=f"`{name}`", inline=True)
    embed.add_field(name="Type", value=f"`{item_data['type']}`", inline=True)
    embed.add_field(name="Quantity", value=f"`{qty}`", inline=True)
    embed.add_field(name="Price", value=price_display, inline=True)
    embed.add_field(name="Grind Index", value=f"`{calc_item_stats(item_data)['grind_index_rbx']:,.1f} RBX`", inline=True)
    embed.add_field(name="Seller", value=ctx.author.mention, inline=True)
    if comment:
        embed.add_field(name="💬 Comment", value=f"`{comment}`", inline=False)
    await ctx.send(embed=embed, delete_after=30)

# ── USER: Remove own listing ───────────────────────────────────────────────────
@bot.command(name="unsell")
async def unsell_item(ctx, name: str = None):
    """
    !unsell FireKey  → removes YOUR listing for that item
    """
    trading_channel_id = get_channel_id("trading")
    if not trading_channel_id or ctx.channel.id != trading_channel_id:
        return
    if not name:
        await ctx.send("❌ Usage: `!unsell ItemName`", delete_after=5)
        await ctx.message.delete(delay=5)
        return

    trading = load_trading()
    removed = []
    for lid, listing in list(trading["listings"].items()):
        if listing["item_key"] == name.lower() and listing["seller_id"] == ctx.author.id:
            del trading["listings"][lid]
            removed.append(lid)

    if not removed:
        await ctx.send(f"❌ No listing found for `{name}` from you.", delete_after=5)
        await ctx.message.delete(delay=5)
        return

    save_trading(trading)
    await ctx.message.delete(delay=2)
    await ctx.send(f"🗑️ Your listing for `{name}` has been removed.", delete_after=8)

# ── VIEW: Market listings ──────────────────────────────────────────────────────
def build_market_embed(page=0, filter_type=None, filter_currency=None,
                        filter_seller=None, sort_by="price"):
    trading = load_trading()
    listings = list(trading["listings"].values())

    # Filters
    if filter_type:
        listings = [l for l in listings if l["item_type"] == filter_type]
    if filter_currency:
        listings = [l for l in listings if l["currency"] == filter_currency]
    if filter_seller:
        listings = [l for l in listings if filter_seller.lower() in l["seller"].lower()]

    if not listings:
        embed = discord.Embed(
            title="🏪 MARKET",
            description="*No listings found.*",
            color=discord.Color.blue()
        )
        return embed, 0, 0

    # Sort
    if sort_by == "price":
        listings.sort(key=lambda x: x["price_rbx"])
    elif sort_by == "ev":
        listings.sort(key=lambda x: calc_item_stats(load_trading()["items"].get(x.get("item_key_ref",""), {})).get("grind_index_rbx", 0))

    total = len(listings)
    total_pages = max(1, (total + TRADE_ENTRIES_PER_PAGE - 1) // TRADE_ENTRIES_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * TRADE_ENTRIES_PER_PAGE
    page_listings = listings[start:start + TRADE_ENTRIES_PER_PAGE]

    title = "🏪 MARKET"
    if filter_type:   title += f" — {filter_type.capitalize()}s"
    if filter_currency: title += f" — {filter_currency.upper()}"

    embed = discord.Embed(title=title, color=discord.Color.blue())
    lines = []
    rate = get_rate()
    items_db = trading.get("items", {})
    for l in page_listings:
        qty_label = f"x{l['quantity']}"
        item_ref = items_db.get(l.get("item_key_ref", l.get("item_key", "")))
        item_stats = calc_item_stats(item_ref) if item_ref is not None else calc_item_stats({})
        grind_label = item_stats.get("grind_label", "UNKNOWN")
        grind_hours = item_stats.get("grind_hours", 0)
        market_price = item_stats.get("market_price", 0)
        worth_trading = item_stats.get("worth_trading", False)
        trade_tag = " ✅ Trade!" if worth_trading else ""
        comment_line = f"\n　💬 `{l['comment']}`" if l.get("comment") else ""
        lines.append(
            f"🔹 **{l['item_name']}** {qty_label} — {l['price_display']}{trade_tag}\n"
            f"　Grind: **{grind_label}** (~{grind_hours:.0f}h) • Seller: {l['seller_mention']}\n"
            f"　Type: `{l['item_type']}`{comment_line}"
        )

    embed.description = "\n\n".join(lines)
    embed.set_footer(
        text=f"Page {page+1}/{total_pages} ✦ {total} listing(s) • Rate: 100k Yens = {rate} RBX"
    )
    return embed, page, total_pages


class MarketView(View):
    def __init__(self, page, total_pages, filter_type=None,
                 filter_currency=None, filter_seller=None, sort_by="price"):
        super().__init__(timeout=120)
        self.page = page
        self.total_pages = total_pages
        self.filter_type = filter_type
        self.filter_currency = filter_currency
        self.filter_seller = filter_seller
        self.sort_by = sort_by
        self.prev_btn.disabled = page == 0
        self.next_btn.disabled = page >= total_pages - 1

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction, button):
        self.page -= 1
        embed, self.page, self.total_pages = build_market_embed(
            self.page, self.filter_type, self.filter_currency,
            self.filter_seller, self.sort_by
        )
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= self.total_pages - 1
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction, button):
        self.page += 1
        embed, self.page, self.total_pages = build_market_embed(
            self.page, self.filter_type, self.filter_currency,
            self.filter_seller, self.sort_by
        )
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= self.total_pages - 1
        await interaction.response.edit_message(embed=embed, view=self)


@bot.command(name="market")
async def show_market(ctx, *args):
    """
    !market                          → all listings, sorted by price
    !market type:key                 → filter by type
    !market currency:robux           → filter by currency
    !market seller:Hars              → filter by seller
    !market sort:ev                  → sort by expected value
    Combine: !market type:key currency:robux sort:ev
    """
    trading_channel_id = get_channel_id("trading")
    if not trading_channel_id or ctx.channel.id != trading_channel_id:
        return

    filter_type = None
    filter_currency = None
    filter_seller = None
    sort_by = "price"

    for arg in args:
        arg = arg.lower()
        if arg.startswith("type:"):
            filter_type = arg[5:]
        elif arg.startswith("currency:"):
            filter_currency = arg[9:]
        elif arg.startswith("seller:"):
            filter_seller = arg[7:]
        elif arg.startswith("sort:"):
            sort_by = arg[5:]

    embed, page, total_pages = build_market_embed(
        0, filter_type, filter_currency, filter_seller, sort_by
    )
    view = MarketView(page, total_pages, filter_type, filter_currency, filter_seller, sort_by)
    await ctx.message.delete(delay=2)
    await ctx.send(embed=embed, view=view)


@bot.command(name="iteminfo")
async def item_info(ctx, name: str = None):
    """!iteminfo FireKey → shows full item data + EV calculation"""
    trading_channel_id = get_channel_id("trading")
    if not trading_channel_id or ctx.channel.id != trading_channel_id:
        return
    if not name:
        await ctx.send("❌ Usage: `!iteminfo ItemName`", delete_after=5)
        await ctx.message.delete(delay=5)
        return

    trading = load_trading()
    item = trading["items"].get(name.lower())
    if not item:
        await ctx.send(f"❌ Item `{name}` not found.", delete_after=5)
        await ctx.message.delete(delay=5)
        return

    rate = get_rate()
    farm_rate = get_farm_rate()
    stats = calc_item_stats(item)
    embed = discord.Embed(title=f"📦 {item['name']}", color=discord.Color.gold())
    embed.add_field(name="Type", value=f"`{item['type']}`", inline=True)
    embed.add_field(name="Drop Rate", value=f"`{item['probability']*100:.2f}%`", inline=True)
    embed.add_field(name="Cost (Yens)", value=f"`{stats['cost_yens']:,.0f}`", inline=True)
    embed.add_field(name="Cost (RBX)", value=f"`{stats['cost_rbx']:.2f}`", inline=True)

    # Grind Index
    embed.add_field(
        name="⚔️ Grind Index",
        value=f"{stats['grind_bar']} **{stats['grind_label']}**\n`~{stats['grind_hours']:.1f} hours` to obtain",
        inline=False
    )

    # Market price + worth trading
    if stats['market_price'] > 0:
        if stats['worth_trading']:
            trade_verdict = f"✅ **Cheaper to trade!** Saves ~`{stats['hours_saved']:.1f}h` of grinding"
        else:
            trade_verdict = f"⚠️ Grinding might be worth it over trading"
        embed.add_field(
            name="💰 Market Price",
            value=f"`{stats['market_price']:.0f} RBX` ({stats['market_currency'].upper()})\n{trade_verdict}",
            inline=False
        )
    else:
        embed.add_field(name="💰 Market Price", value="*Not set — use `!setprice`*", inline=False)

    embed.add_field(name="Julius Bundle Equiv", value=f"`{stats['julius_equiv']:.1f}x`", inline=True)
    embed.add_field(name="Wizard Bundle Equiv", value=f"`{stats['wizard_equiv']:.1f}x`", inline=True)
    embed.set_footer(text=f"Rate: 100k Yens = {rate} RBX • Farm rate: {farm_rate:,.0f} Yens/hr")

    # Active listings for this item
    listings = [l for l in trading["listings"].values() if l["item_key"] == name.lower()]
    if listings:
        listing_lines = []
        for l in listings[:5]:
            listing_lines.append(f"• {l['seller_mention']} — x{l['quantity']} — {l['price_display']}")
        embed.add_field(
            name=f"🛒 Active Listings ({len(listings)})",
            value="\n".join(listing_lines),
            inline=False
        )

    await ctx.message.delete(delay=2)
    await ctx.send(embed=embed)


@bot.command(name="mylistings")
async def my_listings(ctx):
    """!mylistings → shows your active listings"""
    trading_channel_id = get_channel_id("trading")
    if not trading_channel_id or ctx.channel.id != trading_channel_id:
        return

    trading = load_trading()
    my = [l for l in trading["listings"].values() if l["seller_id"] == ctx.author.id]

    if not my:
        await ctx.send("❌ You have no active listings.", delete_after=5)
        await ctx.message.delete(delay=5)
        return

    embed = discord.Embed(
        title=f"🛒 Your Listings ({len(my)})",
        color=discord.Color.blue()
    )
    lines = []
    items_db = trading.get("items", {})
    for l in my:
        key = l.get("item_key_ref") or l.get("item_key") or ""
        item_ref = items_db.get(key)
        item_stats = calc_item_stats(item_ref) if item_ref is not None else calc_item_stats({})
        grind_label = item_stats.get("grind_label", "UNKNOWN")
        grind_hours = item_stats.get("grind_hours", 0)
        comment_line = f"\n　💬 `{l['comment']}`" if l.get("comment") else ""
        lines.append(
            f"🔹 **{l['item_name']}** x{l['quantity']} — {l['price_display']}\n"
            f"　Type: `{l['item_type']}` • Grind: {grind_label} (~{grind_hours:.0f}h){comment_line}"
        )
    embed.description = "\n\n".join(lines)
    await ctx.message.delete(delay=2)
    await ctx.send(embed=embed)

# ══════════════════════════════════════════════════════════════════════════════
#  HELP COMMAND
# ══════════════════════════════════════════════════════════════════════════════

HELP_DATA = {
    "global": {
        "title": "🌐 Global Commands",
        "color": discord.Color.blurple(),
        "fields": [
            ("Channel Setup", (
                "`!setcanal boss #channel` → Set boss tracker channel\n"
                "`!setcanal hackers #channel` → Set hacker list channel\n"
                "`!setcanal build #channel` → Set build calculator channel\n"
                "`!setcanal trading #channel` → Set trading market channel\n"
                "`!removecanal [module]` → Remove channel config"
            )),
        ]
    },
    "boss": {
        "title": "🔥 Module 1 — Boss Tracker",
        "color": discord.Color.orange(),
        "fields": [
            ("Adding a Boss", (
                "`username time` → Add boss\n"
                "`username time bossname` → Add boss with name\n"
                "Example: `Hars 24min Vermillion`"
            )),
            ("Commands", (
                "`!list` → Show tracker list\n"
                "`!remove username` → Remove entry (asks confirmation ✅❌)\n"
                "`!clear` → Clear entire list"
            )),
        ]
    },
    "hackers": {
        "title": "🚨 Module 2 — Hacker List",
        "color": discord.Color.red(),
        "fields": [
            ("Reporting", (
                "`!report username world` → Report a user\n"
                "Attach a file or include a link as evidence\n"
                "Example: `!report pepito123 world2`"
            )),
            ("Status Commands", (
                "`!ticket username` → 🎫 Mark as In Ticket\n"
                "`!banned username` → ✅ Mark as Banned\n"
                "`!reviewed username` → 👁️ Mark as Reviewed\n"
                "`!deletehacker username` → Delete from list"
            )),
            ("Viewing", (
                "`!hackers` → Full list (◀ ▶, 20 per page)\n"
                "`!hackers reported` → Filter: reported\n"
                "`!hackers ticket` → Filter: in ticket\n"
                "`!hackers banned` → Filter: banned\n"
                "`!hackers reviewed` → Filter: reviewed"
            )),
            ("Archive", (
                "`!archive` → Generate TXT with banned users\n"
                "Removes them from active list after export"
            )),
        ]
    },
    "build": {
        "title": "⚔️ Module 3 — Build Calculator",
        "color": discord.Color.gold(),
        "fields": [
            ("Registering Data", (
                "`!trait Name buffs` → Add trait\n"
                "`!race Name buffs` → Add race\n"
                "`!spirit Name buffs` → Add spirit\n"
                "`!title Name buffs` → Add title\n"
                "`!grimoire Name skills` → Add grimoire\n"
                "`!addskill Name skills` → Add skills to existing grimoire\n"
                "Use `!edit[category]` and `!remove[category]` to modify"
            )),
            ("Skill Format", (
                "`Skill:damage` → 1 hit, can crit\n"
                "`Skill:damage hits:N` → N independent hits, each can crit\n"
                "`Skill:damage~tick` → Tick damage, no crit\n"
                "`G:stage1=5%stats stage2=10%stats` → Passive stages"
            )),
            ("Buff Format", (
                "`15%dmg` → +15% Damage\n"
                "`15%magicdmg` → +15% Magic Damage\n"
                "`20%crit` → +20% Crit Chance\n"
                "`35%critdmg` → +35% Crit Bonus\n"
                "`15%burndmg` → +15% Burn tick damage\n"
                "`10%dmgburning` → +10% vs burning enemies\n"
                "`-10%cd` → -10% Cooldown\n"
                "`5%lifesteal` → +5% Lifesteal\n"
                "`30%hprestore@20s` → 30% HP restore every 20s\n"
                "`-50%hp` → Active below 50% HP only\n"
                "`fire=y:12%dmg` → Only with Fire grimoire"
            )),
            ("Calculating", (
                "`!build Grimoire Race Trait Title Spirit`\n"
                "Example: `!build Mereo SpiritRace CritGenius MythicWarrior Salamander`"
            )),
            ("Comparing", (
                "`!compare Grimoire1 Grimoire2 [Race] [Trait] [Title] [Spirit]`\n"
                "Example: `!compare Mereo Light SpiritRace CritGenius MythicWarrior Salamander`\n"
                "Shows skill by skill comparison + recommendations"
            )),
            ("Best Build", (
                "`!bestbuild GrimoireName`\n"
                "Analyzes all combinations → returns:\n"
                "🥇 Max Damage | 🛡️ Max Sustain | 🔥 Max Burning"
            )),
            ("Viewing Data", (
                "`!builds` → Summary of all categories\n"
                "`!builds traits` → List traits\n"
                "`!builds races` → List races\n"
                "`!builds spirits` → List spirits\n"
                "`!builds titles` → List titles\n"
                "`!builds grimorios` → List grimorios"
            )),
        ]
    },
    "aw3": {
        "title": "🕐 Module 5 — AW3 (Timers + Drop Calculator)",
        "color": discord.Color.dark_purple(),
        "fields": [
            ("Setup (first time)", (
                "`!setcanal timers #channel` → Set timers channel\n"
                "`!gauntletanchor HH:MM` → Anchor gauntlet (UTC, future)\n"
                "`!shopanchor HH:MM` → Anchor shop restock (UTC, future)\n"
                "`!pathanchor HH:MM` → Anchor paths (UTC, future)\n"
                "`!showtimers` → Post/refresh live timer embed"
            )),
            ("Reset (after game update)", (
                "`!resetgauntletanchor HH:MM` → Reset gauntlet to today UTC\n"
                "`!resetshopanchor HH:MM` → Reset shop to today UTC\n"
                "`!resetpathanchor HH:MM` → Reset path to today UTC"
            )),
            ("Ping Roles", (
                "`!gauntletping @role` → Role for gauntlet alerts\n"
                "`!shoppping @role` → Role for shop restock alerts\n"
                "`!pathping @role` → Role for path alerts\n"
                "Alerts fire **2 minutes** before each event"
            )),
            ("Stop Timers", (
                "`!gauntletstop` → Stop gauntlet timer\n"
                "`!shopstop` → Stop shop timer\n"
                "`!pathstop` → Stop path timer\n"
                "`!timerstop all` → Stop all timers"
            )),
            ("Drop Calculator", (
                "`!dropcalc drop% luckX boost_min ttk_min`\n"
                "Example: `!dropcalc 0.2% 3.161x 180min 4min`\n"
                "Parameters:\n"
                "• `drop%` → base drop rate of the item\n"
                "• `luckX` → your current luck multiplier\n"
                "• `boost_min` → total boost duration in minutes\n"
                "• `ttk_min` → time per kill in minutes\n"
                "Returns: confidence table (50/90/99%) + checkpoint table"
            )),
            ("Timer Cycles", (
                "🏪 Shop Restock → every **1h 30min**\n"
                "⚔️ Gauntlet → every **1h**, open **5 min** (at :01)\n"
                "🛤️ Path II → every **1h**, open **20 min** (from anchor)\n"
                "🛤️ Path III → right after Path II, **20 min**\n"
                "🛤️ Path I → always open\n"
                "⏰ All times in **UTC** (24h) — Bogota is UTC-5"
            )),
        ]
    },
    "trading": {
        "title": "🏪 Module 4 — Trading Market",
        "color": discord.Color.green(),
        "fields": [
            ("Admin: Register Items", (
                "`!additem Name cost prob type`\n"
                "Example: `!additem FireKey 250000yens 10% type:key`\n"
                "Example: `!additem FireFragment 250000yens 1% type:fragment`\n"
                "Example: `!additem JuliusNecklace 799robux 100% type:limited`\n"
                "Types: `key`, `fragment`, `bundle`, `item`, `limited`"
            )),
            ("Admin: Exchange Rate", (
                "`!setrate 799` → Set rate (100k Yens = X RBX)\n"
                "EV is recalculated dynamically on every query"
            )),
            ("Selling", (
                "`!sell Name qty price yens` → Sell for Yens\n"
                "`!sell Name qty price robux` → Sell for Robux\n"
                "`!sell Name qty offer` → Open to offers\n"
                "`!sell Name qty offer comment` → Offer with description\n"
                "Example: `!sell FireKey 2 250000 yens`\n"
                "Example: `!sell FireFragment 1 offer 1 julius bundle + 2 wind frags`"
            )),
            ("Managing Listings", (
                "`!unsell Name` → Remove your listing\n"
                "`!mylistings` → View your active listings"
            )),
            ("Admin: Rates & Prices", (
                "`!setrate 799` → Set exchange rate (100k Yens = X RBX)\n"
                "`!setfarmrate 222000 1h` → Set farm rate per hour\n"
                "`!setfarmrate 37000 10m` → Set farm rate per 10 minutes\n"
                "`!setprice Name 799 robux` → Set real market price for item"
            )),
            ("Viewing Market", (
                "`!market` → All listings sorted by price (◀ ▶)\n"
                "`!market type:key` → Filter by type\n"
                "`!market currency:robux` → Filter by currency\n"
                "`!market seller:Hars` → Filter by seller\n"
                "`!market sort:ev` → Sort by Expected Value\n"
                "`!iteminfo Name` → Full item info + EV + listings"
            )),
            ("EV Formula", (
                "`cost_rbx = cost_yens × (rate / 100000)`\n"
                "grind_index = cost_rbx / probability\n"
                "equiv = grind_index / bundle_cost"
            )),
        ]
    }
}


@bot.command(name="help")
async def help_cmd(ctx, category: str = None):
    """
    !help                → shows available categories
    !help global         → global commands
    !help boss           → boss tracker commands
    !help hackers        → hacker list commands
    !help build          → build calculator commands
    !help trading        → trading market commands
    """
    valid = list(HELP_DATA.keys())

    if not category:
        embed = discord.Embed(
            title="📖 Dark Triad Bot — Help",
            description=(
                "Use `!help [category]` to see commands for each module.\n\n"
                "`!help global` → Channel setup\n"
                "`!help boss` → Boss Tracker\n"
                "`!help hackers` → Hacker List\n"
                "`!help build` → Build Calculator\n"
                "`!help trading` → Trading Market\n"
                "`!help aw3` → AW3 (Timers + Drop Calc)"
            ),
            color=discord.Color.blurple()
        )
        embed.set_footer(text="Dark Triad Bot v1.0")
        await ctx.message.delete(delay=2)
        await ctx.send(embed=embed)
        return

    category = category.lower()
    if category not in HELP_DATA:
        await ctx.send(
            f"❌ Unknown category `{category}`.\n"
            f"Available: `{'`, `'.join(valid)}`",
            delete_after=8
        )
        await ctx.message.delete(delay=5)
        return

    data = HELP_DATA[category]
    embed = discord.Embed(title=data["title"], color=data["color"])
    for field_name, field_value in data["fields"]:
        embed.add_field(name=field_name, value=field_value, inline=False)
    embed.set_footer(text="Dark Triad Bot v1.0 • !help for categories")

    await ctx.message.delete(delay=2)
    await ctx.send(embed=embed)

# ══════════════════════════════════════════════════════════════════════════════
#  MODULE 5 — EVENT TIMERS
# ══════════════════════════════════════════════════════════════════════════════

import math

def load_timers():
    config = load_config()
    if "timers" not in config:
        config["timers"] = {
            "anchors": {},
            "pings": {},
            "active": {},
            "message_id": None
        }
        save_config(config)
    return config["timers"]

def save_timers(timers):
    config = load_config()
    config["timers"] = timers
    save_config(config)

def parse_utc_time(time_str):
    """Parse HH:MM string and return next UTC datetime matching that time"""
    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc)
    h, m = map(int, time_str.strip().split(":"))
    candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if candidate <= now:
        candidate += dt.timedelta(days=1)
    return candidate

def get_next_event(anchor_unix, interval_seconds, now_unix=None):
    """Get next and previous event times from anchor"""
    import time
    if now_unix is None:
        now_unix = time.time()
    elapsed = now_unix - anchor_unix
    cycles = math.floor(elapsed / interval_seconds)
    prev_unix = anchor_unix + cycles * interval_seconds
    next_unix = prev_unix + interval_seconds
    return int(prev_unix), int(next_unix)

def get_gauntlet_status(anchor_unix, now_unix=None):
    """Returns gauntlet open/close status and next event"""
    import time
    if now_unix is None:
        now_unix = time.time()
    cycle = 3600  # 1 hour
    open_duration = 300  # 5 min
    prev, next_open = get_next_event(anchor_unix, cycle, now_unix)
    elapsed_in_cycle = now_unix - prev
    if elapsed_in_cycle <= open_duration:
        # Currently OPEN
        closes_at = int(prev + open_duration)
        return True, closes_at, int(next_open + open_duration)
    else:
        # Currently CLOSED
        return False, int(next_open), int(next_open + open_duration)

def get_path_status(anchor_unix, now_unix=None):
    """Returns current path status"""
    import time
    if now_unix is None:
        now_unix = time.time()
    cycle = 3600  # 1 hour
    path2_duration = 1200  # 20 min
    path3_duration = 1200  # 20 min
    prev, next_cycle = get_next_event(anchor_unix, cycle, now_unix)
    elapsed = now_unix - prev

    if elapsed < path2_duration:
        # PATH II OPEN
        path2_closes = int(prev + path2_duration)
        path3_opens = path2_closes
        path3_closes = int(path3_opens + path3_duration)
        return "II", path2_closes, path3_opens, path3_closes, int(next_cycle)
    elif elapsed < path2_duration + path3_duration:
        # PATH III OPEN
        path3_closes = int(prev + path2_duration + path3_duration)
        next_path2 = int(next_cycle)
        return "III", path3_closes, None, None, next_path2
    else:
        # Both closed
        next_path2 = int(next_cycle)
        return "CLOSED", None, None, None, next_path2

def get_shop_next(anchor_unix, now_unix=None):
    """Returns next shop restock time"""
    import time
    if now_unix is None:
        now_unix = time.time()
    cycle = 5400  # 1h 30min
    _, next_restock = get_next_event(anchor_unix, cycle, now_unix)
    return next_restock

def build_timers_embed():
    import time as t
    import datetime as dt
    timers = load_timers()
    anchors = timers.get("anchors", {})
    now_unix = t.time()
    now_ts = int(now_unix)

    embed = discord.Embed(
        title="🎮 DARK TRIAD — EVENT TIMERS",
        color=discord.Color.dark_purple()
    )

    sep = "━━━━━━━━━━━━━━━━━━━━━━"

    # ── SHOP RESTOCK ──
    if "shop" in anchors:
        next_restock = get_shop_next(anchors["shop"], now_unix)
        shop_val = f"**Next restock:** <t:{next_restock}:R>\n<t:{next_restock}:T>"
    else:
        shop_val = "*Not configured — use `!shopanchor HH:MM`*"
    embed.add_field(name=f"{sep}\n🏪 SHOP RESTOCK", value=shop_val, inline=False)

    # ── GAUNTLET ──
    if "gauntlet" in anchors:
        is_open, time1, time2 = get_gauntlet_status(anchors["gauntlet"], now_unix)
        if is_open:
            gauntlet_val = f"🟢 **OPEN**\nCloses: <t:{time1}:R> | <t:{time1}:T>"
        else:
            gauntlet_val = f"🔴 **CLOSED**\nOpens: <t:{time1}:R> | <t:{time1}:T>"
    else:
        gauntlet_val = "*Not configured — use `!gauntletanchor HH:MM`*"
    embed.add_field(name=f"{sep}\n⚔️ GAUNTLET", value=gauntlet_val, inline=False)

    # ── PATH ──
    if "path" in anchors:
        status, t1, t2, t3, t4 = get_path_status(anchors["path"], now_unix)
        if status == "II":
            path_val = (
                f"🟢 **PATH II OPEN**\nCloses: <t:{t1}:R>\n\n"
                f"⏳ **PATH III** opens: <t:{t2}:R>"
            )
        elif status == "III":
            path_val = (
                f"🟢 **PATH III OPEN**\nCloses: <t:{t1}:R>\n\n"
                f"⏳ **PATH II** next: <t:{t4}:R>"
            )
        else:
            path_val = (
                f"🔴 **PATHS CLOSED**\nNext PATH II: <t:{t4}:R> | <t:{t4}:T>"
            )
    else:
        path_val = "*Not configured — use `!pathanchor HH:MM`*"
    embed.add_field(name=f"{sep}\n🛤️ PATH", value=path_val, inline=False)

    embed.set_footer(text="━━━━━━━━━━━━━━━━━━━━━━\nLast updated")
    embed.timestamp = dt.datetime.now(dt.timezone.utc)
    return embed


# ── ANCHOR COMMANDS ────────────────────────────────────────────────────────────

@bot.command(name="gauntletanchor")
async def gauntlet_anchor(ctx, time_str: str = None):
    """!gauntletanchor 10:01 — anchors gauntlet timer (UTC)"""
    timers_channel_id = get_channel_id("aw3")
    if not timers_channel_id or ctx.channel.id != timers_channel_id:
        return
    if not time_str:
        await ctx.send("❌ Usage: `!gauntletanchor HH:MM` (UTC)", delete_after=8)
        await ctx.message.delete(delay=5)
        return
    try:
        anchor_dt = parse_utc_time(time_str)
        timers = load_timers()
        timers["anchors"]["gauntlet"] = anchor_dt.timestamp()
        timers["active"]["gauntlet"] = True
        save_timers(timers)
        await ctx.message.delete(delay=2)
        await ctx.send(f"✅ Gauntlet anchored at `{time_str} UTC` → next: <t:{int(anchor_dt.timestamp())}:R>", delete_after=10)
    except:
        await ctx.send("❌ Invalid time format. Use `HH:MM`", delete_after=5)
        await ctx.message.delete(delay=5)

@bot.command(name="shopanchor")
async def shop_anchor(ctx, time_str: str = None):
    """!shopanchor 10:20 — anchors shop restock timer (UTC)"""
    timers_channel_id = get_channel_id("aw3")
    if not timers_channel_id or ctx.channel.id != timers_channel_id:
        return
    if not time_str:
        await ctx.send("❌ Usage: `!shopanchor HH:MM` (UTC)", delete_after=8)
        await ctx.message.delete(delay=5)
        return
    try:
        anchor_dt = parse_utc_time(time_str)
        timers = load_timers()
        timers["anchors"]["shop"] = anchor_dt.timestamp()
        timers["active"]["shop"] = True
        save_timers(timers)
        await ctx.message.delete(delay=2)
        await ctx.send(f"✅ Shop anchored at `{time_str} UTC` → next restock: <t:{int(anchor_dt.timestamp())}:R>", delete_after=10)
    except:
        await ctx.send("❌ Invalid time format. Use `HH:MM`", delete_after=5)
        await ctx.message.delete(delay=5)

@bot.command(name="pathanchor")
async def path_anchor(ctx, time_str: str = None):
    """!pathanchor 17:20 — anchors path timer (UTC)"""
    timers_channel_id = get_channel_id("aw3")
    if not timers_channel_id or ctx.channel.id != timers_channel_id:
        return
    if not time_str:
        await ctx.send("❌ Usage: `!pathanchor HH:MM` (UTC)", delete_after=8)
        await ctx.message.delete(delay=5)
        return
    try:
        anchor_dt = parse_utc_time(time_str)
        timers = load_timers()
        timers["anchors"]["path"] = anchor_dt.timestamp()
        timers["active"]["path"] = True
        save_timers(timers)
        await ctx.message.delete(delay=2)
        await ctx.send(f"✅ Path anchored at `{time_str} UTC` → next Path II: <t:{int(anchor_dt.timestamp())}:R>", delete_after=10)
    except:
        await ctx.send("❌ Invalid time format. Use `HH:MM`", delete_after=5)
        await ctx.message.delete(delay=5)

# ── STOP COMMANDS ──────────────────────────────────────────────────────────────

@bot.command(name="gauntletstop")
async def gauntlet_stop(ctx):
    """!gauntletstop — stops gauntlet timer"""
    timers_channel_id = get_channel_id("aw3")
    if not timers_channel_id or ctx.channel.id != timers_channel_id:
        return
    timers = load_timers()
    timers["active"]["gauntlet"] = False
    timers["anchors"].pop("gauntlet", None)
    save_timers(timers)
    await ctx.message.delete(delay=2)
    await ctx.send("🛑 Gauntlet timer stopped.", delete_after=8)

@bot.command(name="shopstop")
async def shop_stop(ctx):
    """!shopstop — stops shop restock timer"""
    timers_channel_id = get_channel_id("aw3")
    if not timers_channel_id or ctx.channel.id != timers_channel_id:
        return
    timers = load_timers()
    timers["active"]["shop"] = False
    timers["anchors"].pop("shop", None)
    save_timers(timers)
    await ctx.message.delete(delay=2)
    await ctx.send("🛑 Shop timer stopped.", delete_after=8)

@bot.command(name="pathstop")
async def path_stop(ctx):
    """!pathstop — stops path timer"""
    timers_channel_id = get_channel_id("aw3")
    if not timers_channel_id or ctx.channel.id != timers_channel_id:
        return
    timers = load_timers()
    timers["active"]["path"] = False
    timers["anchors"].pop("path", None)
    save_timers(timers)
    await ctx.message.delete(delay=2)
    await ctx.send("🛑 Path timer stopped.", delete_after=8)

@bot.command(name="timerstop")
async def timer_stop_all(ctx, target: str = "all"):
    """!timerstop all — stops all timers"""
    timers_channel_id = get_channel_id("aw3")
    if not timers_channel_id or ctx.channel.id != timers_channel_id:
        return
    timers = load_timers()
    if target.lower() == "all":
        timers["anchors"] = {}
        timers["active"] = {}
        timers["message_id"] = None
    save_timers(timers)
    await ctx.message.delete(delay=2)
    await ctx.send("🛑 All timers stopped.", delete_after=8)

# ── PING ROLE COMMANDS ─────────────────────────────────────────────────────────

@bot.command(name="gauntletping")
async def gauntlet_ping(ctx, role: discord.Role = None):
    """!gauntletping @role — sets ping role for gauntlet"""
    timers_channel_id = get_channel_id("aw3")
    if not timers_channel_id or ctx.channel.id != timers_channel_id:
        return
    if not role:
        await ctx.send("❌ Usage: `!gauntletping @role`", delete_after=5)
        await ctx.message.delete(delay=5)
        return
    timers = load_timers()
    timers["pings"]["gauntlet"] = role.id
    save_timers(timers)
    await ctx.message.delete(delay=2)
    await ctx.send(f"✅ Gauntlet ping set to {role.mention}", delete_after=8)

@bot.command(name="shoppping")
async def shop_ping(ctx, role: discord.Role = None):
    """!shoppping @role — sets ping role for shop restock"""
    timers_channel_id = get_channel_id("aw3")
    if not timers_channel_id or ctx.channel.id != timers_channel_id:
        return
    if not role:
        await ctx.send("❌ Usage: `!shoppping @role`", delete_after=5)
        await ctx.message.delete(delay=5)
        return
    timers = load_timers()
    timers["pings"]["shop"] = role.id
    save_timers(timers)
    await ctx.message.delete(delay=2)
    await ctx.send(f"✅ Shop ping set to {role.mention}", delete_after=8)

@bot.command(name="pathping")
async def path_ping(ctx, role: discord.Role = None):
    """!pathping @role — sets ping role for path events"""
    timers_channel_id = get_channel_id("aw3")
    if not timers_channel_id or ctx.channel.id != timers_channel_id:
        return
    if not role:
        await ctx.send("❌ Usage: `!pathping @role`", delete_after=5)
        await ctx.message.delete(delay=5)
        return
    timers = load_timers()
    timers["pings"]["path"] = role.id
    save_timers(timers)
    await ctx.message.delete(delay=2)
    await ctx.send(f"✅ Path ping set to {role.mention}", delete_after=8)

# ── SHOWTIMERS ─────────────────────────────────────────────────────────────────

timer_display_message = None
alerted_timers = set()

@bot.command(name="showtimers")
async def show_timers(ctx):
    """!showtimers — posts a new live timer embed and deletes the old one"""
    global timer_display_message
    timers_channel_id = get_channel_id("aw3")
    if not timers_channel_id or ctx.channel.id != timers_channel_id:
        return
    await ctx.message.delete(delay=2)

    # Delete old message if exists
    if timer_display_message:
        try:
            await timer_display_message.delete()
        except discord.NotFound:
            pass
        timer_display_message = None

    # Always send a new message
    embed = build_timers_embed()
    timer_display_message = await ctx.channel.send(embed=embed)
    timers = load_timers()
    timers["message_id"] = timer_display_message.id
    save_timers(timers)

# ── TIMER UPDATE LOOP ──────────────────────────────────────────────────────────

@tasks.loop(seconds=30)
async def update_timers():
    global timer_display_message, alerted_timers
    import time as t

    timers_channel_id = get_channel_id("aw3")
    if not timers_channel_id or not timer_display_message:
        return

    timers = load_timers()
    anchors = timers.get("anchors", {})
    pings = timers.get("pings", {})
    now_unix = t.time()
    channel = timer_display_message.channel

    # ── ALERTS (2 min before) ──
    async def maybe_ping(event_key, event_unix, label, role_id):
        alert_key = f"{event_key}_{int(event_unix)}"
        remaining = event_unix - now_unix
        if 0 < remaining <= 120 and alert_key not in alerted_timers:
            alerted_timers.add(alert_key)
            role = channel.guild.get_role(role_id) if role_id else None
            mention = role.mention if role else ""
            await channel.send(
                f"⚠️ {mention} **{label}** starts in 2 minutes! <t:{int(event_unix)}:T>",
                delete_after=180
            )

    if "gauntlet" in anchors:
        is_open, t1, _ = get_gauntlet_status(anchors["gauntlet"], now_unix)
        if not is_open:
            await maybe_ping("gauntlet", t1, "GAUNTLET", pings.get("gauntlet"))

    if "shop" in anchors:
        next_restock = get_shop_next(anchors["shop"], now_unix)
        await maybe_ping("shop", next_restock, "SHOP RESTOCK", pings.get("shop"))

    if "path" in anchors:
        status, t1, t2, t3, t4 = get_path_status(anchors["path"], now_unix)
        if status == "CLOSED":
            await maybe_ping("pathII", t4, "PATH II", pings.get("path"))
        elif status == "II" and t2:
            await maybe_ping("pathIII", t2, "PATH III", pings.get("path"))
        elif status == "III" and t1:
            pass  # PATH III already open

    # ── UPDATE EMBED ──
    embed = build_timers_embed()
    try:
        await timer_display_message.edit(embed=embed)
    except discord.NotFound:
        timer_display_message = None

@bot.command(name="resetpathanchor")
async def reset_path_anchor(ctx, time_str: str = None):
    """!resetpathanchor 23:20 — resets path anchor to a new UTC time"""
    timers_channel_id = get_channel_id("aw3")
    if not timers_channel_id or ctx.channel.id != timers_channel_id:
        return
    if not time_str:
        await ctx.send("❌ Usage: `!resetpathanchor HH:MM` (UTC)", delete_after=8)
        await ctx.message.delete(delay=5)
        return
    try:
        import datetime as dt
        now = dt.datetime.now(dt.timezone.utc)
        h, m = map(int, time_str.strip().split(":"))
        anchor_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
        timers = load_timers()
        timers["anchors"]["path"] = anchor_dt.timestamp()
        timers["active"]["path"] = True
        save_timers(timers)
        await ctx.message.delete(delay=2)
        await ctx.send(f"✅ Path anchor reset to `{time_str} UTC` today → <t:{int(anchor_dt.timestamp())}:T>", delete_after=10)
    except:
        await ctx.send("❌ Invalid time format. Use `HH:MM`", delete_after=5)
        await ctx.message.delete(delay=5)

@bot.command(name="resetgauntletanchor")
async def reset_gauntlet_anchor(ctx, time_str: str = None):
    """!resetgauntletanchor 10:01 — resets gauntlet anchor to today"""
    timers_channel_id = get_channel_id("aw3")
    if not timers_channel_id or ctx.channel.id != timers_channel_id:
        return
    if not time_str:
        await ctx.send("❌ Usage: `!resetgauntletanchor HH:MM` (UTC)", delete_after=8)
        await ctx.message.delete(delay=5)
        return
    try:
        import datetime as dt
        now = dt.datetime.now(dt.timezone.utc)
        h, m = map(int, time_str.strip().split(":"))
        anchor_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
        timers = load_timers()
        timers["anchors"]["gauntlet"] = anchor_dt.timestamp()
        timers["active"]["gauntlet"] = True
        save_timers(timers)
        await ctx.message.delete(delay=2)
        await ctx.send(f"✅ Gauntlet anchor reset to `{time_str} UTC` today → <t:{int(anchor_dt.timestamp())}:T>", delete_after=10)
    except:
        await ctx.send("❌ Invalid time format. Use `HH:MM`", delete_after=5)
        await ctx.message.delete(delay=5)

@bot.command(name="resetshopanchor")
async def reset_shop_anchor(ctx, time_str: str = None):
    """!resetshopanchor 10:20 — resets shop anchor to today"""
    timers_channel_id = get_channel_id("aw3")
    if not timers_channel_id or ctx.channel.id != timers_channel_id:
        return
    if not time_str:
        await ctx.send("❌ Usage: `!resetshopanchor HH:MM` (UTC)", delete_after=8)
        await ctx.message.delete(delay=5)
        return
    try:
        import datetime as dt
        now = dt.datetime.now(dt.timezone.utc)
        h, m = map(int, time_str.strip().split(":"))
        anchor_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
        timers = load_timers()
        timers["anchors"]["shop"] = anchor_dt.timestamp()
        timers["active"]["shop"] = True
        save_timers(timers)
        await ctx.message.delete(delay=2)
        await ctx.send(f"✅ Shop anchor reset to `{time_str} UTC` today → <t:{int(anchor_dt.timestamp())}:T>", delete_after=10)
    except:
        await ctx.send("❌ Invalid time format. Use `HH:MM`", delete_after=5)
        await ctx.message.delete(delay=5)

# ── DROP CALCULATOR ───────────────────────────────────────────────────────────

import math as _math

def _drop_bar(value, max_val, width=20, fill="█", empty="░"):
    filled = round((value / max_val) * width) if max_val > 0 else 0
    filled = min(filled, width)
    return fill * filled + empty * (width - filled)

def _drop_tier(hours):
    if hours <= 2:   return "OPTIMAL",     "★★★★★"
    if hours <= 5:   return "GOOD",        "★★★★☆"
    if hours <= 15:  return "ACCEPTABLE",  "★★★☆☆"
    if hours <= 40:  return "SLOW",        "★★☆☆☆"
    return           "INEFFICIENT",        "★☆☆☆☆"

def _drop_calc(drop_base, luck_mult, boost_dur, ttk):
    p = min((drop_base * luck_mult) / 100.0, 1.0)
    kills_per_boost = max(1, int(boost_dur / ttk))
    p_per_boost = 1 - (1 - p) ** kills_per_boost
    if p_per_boost <= 0:
        return None
    boosts_50  = _math.ceil(_math.log(0.50) / _math.log(1 - p_per_boost))
    boosts_90  = _math.ceil(_math.log(0.10) / _math.log(1 - p_per_boost))
    boosts_99  = _math.ceil(_math.log(0.01) / _math.log(1 - p_per_boost))
    avg_boosts = _math.ceil(1 / p_per_boost)
    return {
        "p_efectiva":      p * 100,
        "kills_per_boost": kills_per_boost,
        "p_per_boost":     p_per_boost * 100,
        "avg_boosts":      avg_boosts,
        "avg_kills":       avg_boosts * kills_per_boost,
        "avg_hours":       (avg_boosts * boost_dur) / 60,
        "boosts_50":       boosts_50,
        "boosts_90":       boosts_90,
        "boosts_99":       boosts_99,
        "hours_50":        (boosts_50 * boost_dur) / 60,
        "hours_90":        (boosts_90 * boost_dur) / 60,
        "hours_99":        (boosts_99 * boost_dur) / 60,
        "boost_dur":       boost_dur,
    }

@bot.command(name="dropcalc")
async def drop_calc(ctx, drop_str: str = None, luck_str: str = None,
                    boost_str: str = None, ttk_str: str = None):
    """
    !dropcalc 0.2% 3.161x 180min 4min
    drop_base% luck_multiplierx boost_duration_min ttk_min
    """
    aw3_channel_id = get_channel_id("aw3")
    if not aw3_channel_id or ctx.channel.id != aw3_channel_id:
        return

    if not all([drop_str, luck_str, boost_str, ttk_str]):
        await ctx.send(
            "❌ Usage: `!dropcalc 0.2% 3.161x 180min 4min`\n"
            "Parameters: `drop%` `luckX` `boost_duration_min` `ttk_min`",
            delete_after=10
        )
        await ctx.message.delete(delay=5)
        return

    try:
        drop_base = float(drop_str.replace("%", ""))
        luck_mult = float(luck_str.replace("x", "").replace("X", ""))
        boost_dur = float(boost_str.replace("min", "").replace("m", ""))
        ttk       = float(ttk_str.replace("min", "").replace("m", ""))
    except:
        await ctx.send("❌ Invalid format. Example: `!dropcalc 0.2% 3.161x 180min 4min`", delete_after=8)
        await ctx.message.delete(delay=5)
        return

    r = _drop_calc(drop_base, luck_mult, boost_dur, ttk)
    if not r:
        await ctx.send("❌ Effective probability = 0. Check your parameters.", delete_after=8)
        await ctx.message.delete(delay=5)
        return

    await ctx.message.delete(delay=2)

    # ── EMBED: Summary ──
    embed = discord.Embed(
        title="📊 DROP CALCULATOR",
        color=discord.Color.teal()
    )

    embed.add_field(
        name="⚙️ Parameters",
        value=(
            f"Drop base: `{drop_base}%`\n"
            f"Luck: `{luck_mult}x` → Effective: `{r['p_efectiva']:.4f}%`\n"
            f"Boost: `{boost_dur:.0f} min` | TTK: `{ttk:.1f} min`\n"
            f"Kills per boost: `{r['kills_per_boost']}`\n"
            f"Chance per boost: `{r['p_per_boost']:.3f}%`"
        ),
        inline=False
    )

    conf_lines = []
    max_h = max(r['hours_50'], r['hours_90'], r['hours_99'])
    for label, boosts, hours in [
        ("50%", r['boosts_50'], r['hours_50']),
        ("90%", r['boosts_90'], r['hours_90']),
        ("99%", r['boosts_99'], r['hours_99']),
    ]:
        t, stars = _drop_tier(hours)
        conf_lines.append(f"`{label}` → `{boosts}` boosts | `{hours:.1f}h` {stars} {t}")

    embed.add_field(
        name="🎯 Cumulative Confidence",
        value="\n".join(conf_lines),
        inline=False
    )

    t, stars = _drop_tier(r['avg_hours'])
    embed.add_field(
        name="📈 Average Scenario",
        value=(
            f"Boosts: `{r['avg_boosts']}` | Kills: `{r['avg_kills']}`\n"
            f"Time: `{r['avg_hours']:.1f}h` {stars} {t}"
        ),
        inline=False
    )

    embed.set_footer(text="See checkpoint table below ↓")
    await ctx.send(embed=embed)

    # ── MESSAGE 2: Checkpoint table as code block ──
    checkpoints = [10, 25, 50, 100, 200, 500, 1000]
    header = f"{'Boosts':>8}  {'Kills':>7}  {'Hours':>6}  {'Cum. Prob':>10}  Bar"
    sep    = "─" * 55
    rows   = [header, sep]
    for b in checkpoints:
        kills = b * r['kills_per_boost']
        hours = (b * boost_dur) / 60
        prob  = (1 - (1 - r['p_per_boost'] / 100) ** b) * 100
        prob  = min(prob, 99.99)
        bar   = _drop_bar(prob, 100, 20)
        rows.append(f"{b:>8}  {kills:>7}  {hours:>5.1f}h  {prob:>9.2f}%  {bar}")

    table = "\n".join(rows)
    await ctx.send(f"```\n{table}\n```")

bot.run(BOT_TOKEN)
