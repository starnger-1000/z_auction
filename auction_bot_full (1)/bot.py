# bot with duelist register, duelist auction, salary deduction, club balance adjust
pip install discord.py fastapi uvicorn jinja2

# bot.py
# Full Club Auction Bot (single-file)
# Dependencies: discord.py, fastapi, uvicorn, jinja2
# Install: pip install discord.py fastapi uvicorn jinja2

import os
import sqlite3
import asyncio
import random
import threading
from datetime import datetime, timedelta

# ---------- CONFIG ----------
# Add your Discord token here OR set environment variable DISCORD_TOKEN
# Option A (recommended): export DISCORD_TOKEN in your environment
# Option B: paste token directly below (NOT recommended for shared code)
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN") or "PASTE_YOUR_TOKEN_HERE"

# Optional: owner id (int) for owner-only checks
BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID")) if os.getenv("BOT_OWNER_ID") else None

# Optional: report channel id for weekly auto report
REPORT_CHANNEL_ID = int(os.getenv("REPORT_CHANNEL_ID")) if os.getenv("REPORT_CHANNEL_ID") else None

# Enable a small web dashboard (FastAPI). Set to False if you don't want it.
START_DASHBOARD = False
DASHBOARD_HOST = "0.0.0.0"
DASHBOARD_PORT = 8000

# Auction config
TIME_LIMIT = 30                 # seconds after last bid until finalize
MIN_INCREMENT_PERCENT = 5      # minimum percent increase per new bid
LEAVE_PENALTY_PERCENT = 10     # if member leaves group mid-auction (applies to group funds)
DUELIST_MISS_PENALTY_PERCENT = 15  # salary deduction percent when a duelist misses a match

DB_FILE = "auction.db"
SCHEMA_FILE = "shared_schema.sql"

# ---------- DATABASE HELPER ----------
class DB:
    def __init__(self, path=DB_FILE):
        self.path = path
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._ensure_schema()

    def _ensure_schema(self):
        # If schema file exists in same folder, use that; otherwise create minimal schema
        if os.path.exists(SCHEMA_FILE):
            with open(SCHEMA_FILE, "r", encoding="utf-8") as f:
                schema = f.read()
        else:
            schema = """
BEGIN TRANSACTION;
CREATE TABLE IF NOT EXISTS investor_groups (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, funds INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS groups_members (id INTEGER PRIMARY KEY AUTOINCREMENT, group_name TEXT, user_id TEXT);
CREATE TABLE IF NOT EXISTS personal_wallets (user_id TEXT PRIMARY KEY, balance INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS user_profiles (user_id TEXT PRIMARY KEY, bio TEXT, banner TEXT, color TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS club (id INTEGER PRIMARY KEY, name TEXT UNIQUE, base_price INTEGER, slogan TEXT, logo TEXT, banner TEXT, value INTEGER, manager_id TEXT);
CREATE TABLE IF NOT EXISTS club_market_history (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, value INTEGER);
CREATE TABLE IF NOT EXISTS bids (id INTEGER PRIMARY KEY AUTOINCREMENT, bidder TEXT, amount INTEGER, item_type TEXT, item_id TEXT, timestamp TEXT DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS club_history (id INTEGER PRIMARY KEY AUTOINCREMENT, winner TEXT, amount INTEGER, timestamp TEXT, market_value_at_sale INTEGER);
CREATE TABLE IF NOT EXISTS audit_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, entry TEXT, timestamp TEXT DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS duelists (id INTEGER PRIMARY KEY AUTOINCREMENT, discord_user_id TEXT, username TEXT, avatar_url TEXT, base_price INTEGER, expected_salary INTEGER, registered_at TEXT, owned_by TEXT);
CREATE TABLE IF NOT EXISTS duelist_contracts (id INTEGER PRIMARY KEY AUTOINCREMENT, duelist_id INTEGER, club_owner TEXT, purchase_price INTEGER, salary INTEGER, signed_at TEXT);
CREATE TABLE IF NOT EXISTS wallet_transactions (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, amount INTEGER, type TEXT, timestamp TEXT DEFAULT (datetime('now')));
COMMIT;
"""
        self.conn.executescript(schema)
        self.conn.commit()

    def query(self, sql, params=()):
        cur = self.conn.cursor()
        cur.execute(sql, params)
        self.conn.commit()
        return cur

    def fetchone(self, sql, params=()):
        cur = self.conn.cursor()
        cur.execute(sql, params)
        return cur.fetchone()

    def fetchall(self, sql, params=()):
        cur = self.conn.cursor()
        cur.execute(sql, params)
        return cur.fetchall()

# ---------- SETUP ----------
db = DB(DB_FILE)

# ---------- DISCORD BOT ----------
import discord
from discord.ext import commands

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# in-memory timer tracking (single timer simplified; supports multiple auctions if you extend)
active_timers = {}  # key: (item_type,item_id) -> asyncio.Task
bidding_frozen = False

# ---------- UTIL FUNCTIONS ----------
def log_audit(entry: str):
    db.query("INSERT INTO audit_logs (entry) VALUES (?)", (entry,))

def get_current_bid(item_type=None, item_id=None):
    if item_type and item_id is not None:
        row = db.fetchone("SELECT amount FROM bids WHERE item_type=? AND item_id=? ORDER BY id DESC LIMIT 1", (item_type, str(item_id)))
    else:
        row = db.fetchone("SELECT amount FROM bids ORDER BY id DESC LIMIT 1")
    if row:
        return int(row["amount"])
    # fallback values
    if item_type == "club" and item_id is not None:
        row2 = db.fetchone("SELECT base_price FROM club WHERE id=?", (item_id,))
        return int(row2["base_price"]) if row2 else 0
    if item_type == "duelist" and item_id is not None:
        row2 = db.fetchone("SELECT base_price FROM duelists WHERE id=?", (item_id,))
        return int(row2["base_price"]) if row2 else 0
    row2 = db.fetchone("SELECT base_price FROM club WHERE id=1")
    return int(row2["base_price"]) if row2 else 0

def min_required_bid(current):
    # integer-safe: round up to nearest integer
    add = current * MIN_INCREMENT_PERCENT / 100
    return int(current + max(1, round(add)))  # require at least +1 if percent too small

# ---------- BACKGROUND: MARKET SIMULATION & WEEKLY REPORT ----------
async def market_simulation_task():
    while True:
        await asyncio.sleep(3600)  # hourly
        club = db.fetchone("SELECT * FROM club WHERE id=1")
        if not club:
            continue
        base = int(club["value"] or club["base_price"])
        bid_count = len(db.fetchall('SELECT id FROM bids WHERE item_type="club"'))
        bid_factor = max(0, bid_count - 1) * 0.001
        change = random.uniform(-0.03, 0.03) + bid_factor
        new_value = int(max(100, base * (1 + change)))
        db.query("UPDATE club SET value=? WHERE id=1", (new_value,))
        db.query("INSERT INTO club_market_history (timestamp, value) VALUES (?,?)", (datetime.now().isoformat(), new_value))
        log_audit(f"Market updated to {new_value}")

async def weekly_report_scheduler():
    while True:
        await asyncio.sleep(7 * 24 * 3600)
        report = generate_weekly_report()
        log_audit("Weekly report generated")
        if REPORT_CHANNEL_ID:
            ch = bot.get_channel(REPORT_CHANNEL_ID)
            if ch:
                await ch.send(report)

def generate_weekly_report():
    now = datetime.now()
    weekago = now - timedelta(days=7)
    rows = db.fetchall("SELECT * FROM club_history WHERE timestamp>?", (weekago.isoformat(),))
    total_sales = len(rows)
    total_volume = sum([r["amount"] for r in rows]) if rows else 0
    group_profits = {}
    for r in rows:
        w = r["winner"]
        if "(group)" in str(w):
            g = str(w).replace(" (group)", "")
            group_profits[g] = group_profits.get(g, 0) + r["amount"]
    top = sorted(group_profits.items(), key=lambda x: x[1], reverse=True)[:5]
    report = f"📈 Weekly Report\nTotal Sales: {total_sales}\nVolume: {total_volume}\nTop Groups: {top}\nGenerated: {now}"
    return report

# ---------- TIMER / AUCTION FINALIZER ----------
async def finalize_auction(item_type: str, item_id: str, channel_id: int):
    # This runs after TIME_LIMIT seconds with no new bids
    winner = db.fetchone("SELECT bidder, amount FROM bids WHERE item_type=? AND item_id=? ORDER BY id DESC LIMIT 1", (item_type, str(item_id)))
    channel = bot.get_channel(channel_id)
    if winner:
        bidder_str = winner["bidder"]
        amount = int(winner["amount"])
        if item_type == "club":
            db.query("INSERT INTO club_history (winner, amount, timestamp, market_value_at_sale) VALUES (?,?,datetime('now'),?)",
                     (bidder_str, amount, (db.fetchone("SELECT value FROM club WHERE id=1")["value"] if db.fetchone("SELECT value FROM club WHERE id=1") else None)))
            # if group, deduct funds
            if "(group)" in bidder_str:
                gname = bidder_str.replace(" (group)", "").lower()
                g = db.fetchone("SELECT funds FROM investor_groups WHERE name=?", (gname,))
                if g:
                    newfunds = max(0, g["funds"] - amount)
                    db.query("UPDATE investor_groups SET funds=? WHERE name=?", (newfunds, gname))
                    log_audit(f"Deducted {amount} from group {gname} after winning club")
            if channel:
                await channel.send(f"🏁 Auction ended for club {item_id}. Winner: **{bidder_str}** for **{amount}**.")
            log_audit(f"Auction ended for club {item_id}. Winner: {bidder_str} for {amount}")
        else:  # duelist
            duelist = db.fetchone("SELECT * FROM duelists WHERE id=?", (item_id,))
            if duelist:
                # sign contract: purchase_price=amount, salary = expected_salary (negotiation not implemented in this version)
                salary = duelist["expected_salary"]
                db.query("INSERT INTO duelist_contracts (duelist_id, club_owner, purchase_price, salary, signed_at) VALUES (?,?,?,?,datetime('now'))",
                         (item_id, bidder_str, amount, salary))
                db.query("UPDATE duelists SET owned_by=? WHERE id=?", (bidder_str, item_id))
                # if group, deduct funds
                if "(group)" in bidder_str:
                    gname = bidder_str.replace(" (group)", "").lower()
                    g = db.fetchone("SELECT funds FROM investor_groups WHERE name=?", (gname,))
                    if g:
                        newfunds = max(0, g["funds"] - amount)
                        db.query("UPDATE investor_groups SET funds=? WHERE name=?", (newfunds, gname))
                        log_audit(f"Deducted {amount} from group {gname} after signing duelist")
                if channel:
                    await channel.send(f"🏁 Duelist auction ended. {duelist['username']} signed to **{bidder_str}** for **{amount}**. Salary: {salary}")
                log_audit(f"Duelist {duelist['username']} signed to {bidder_str} for {amount}")
    else:
        if channel:
            await channel.send("Auction ended with no bids.")
    # cleanup bids for item
    db.query("DELETE FROM bids WHERE item_type=? AND item_id=?", (item_type, str(item_id)))
    # remove active timer entry
    active_timers.pop((item_type, str(item_id)), None)

def schedule_auction_timer(item_type: str, item_id: str, channel_id: int):
    # cancel existing
    key = (item_type, str(item_id))
    task = active_timers.get(key)
    if task and not task.done():
        task.cancel()
    # schedule new timer
    loop = asyncio.get_event_loop()
    t = loop.create_task(asyncio.sleep(TIME_LIMIT))
    async def wrapper():
        try:
            await t
            await finalize_auction(item_type, item_id, channel_id)
        except asyncio.CancelledError:
            return
    task2 = loop.create_task(wrapper())
    active_timers[key] = task2

# ---------- DISCORD COMMANDS ----------
@bot.command()
@commands.has_permissions(administrator=True)
async def registerclub(ctx, name: str, base_price: int, *, slogan: str = ""):
    """
    Admin command: register a club
    !registerclub <name> <base_price> [slogan]
    """
    if db.fetchone("SELECT * FROM club WHERE name=?", (name,)):
        return await ctx.send("Club already registered.")
    db.query("INSERT INTO club (name, base_price, slogan, value) VALUES (?,?,?,?)", (name, base_price, slogan, base_price))
    db.query("INSERT INTO club_market_history (timestamp,value) VALUES (?,?)", (datetime.now().isoformat(), base_price))
    await ctx.send(f"Club **{name}** registered with base price {base_price}.")
    log_audit(f"{ctx.author} registered club {name} (base {base_price})")

@bot.command()
async def listclubs(ctx):
    rows = db.fetchall("SELECT id,name,base_price,value FROM club")
    if not rows:
        return await ctx.send("No clubs registered.")
    msg = "📋 Registered Clubs:\n"
    for r in rows:
        msg += f"- {r['id']}: {r['name']} | base {r['base_price']} | value {r['value']}\n"
    await ctx.send(msg)

@bot.command()
@commands.has_permissions(administrator=True)
async def startclubauction(ctx, club_name: str):
    """
    Admin command: start auction for a registered club by name
    """
    club = db.fetchone("SELECT * FROM club WHERE name=?", (club_name,))
    if not club:
        return await ctx.send("No such registered club.")
    # clear bids for this club and announce
    db.query("DELETE FROM bids WHERE item_type='club' AND item_id=?", (str(club["id"]),))
    await ctx.send(f"🔔 Auction started for club **{club_name}**! Starting price: {club['base_price']}\nUse `!placebid <amount> club {club['id']}` to bid.")
    log_audit(f"{ctx.author} started auction for club {club_name}")
    schedule_auction_timer("club", str(club["id"]), ctx.channel.id)

@bot.command()
async def clubinfo(ctx, club_id: int = None):
    if club_id is None:
        row = db.fetchone("SELECT * FROM club WHERE id=1")
    else:
        row = db.fetchone("SELECT * FROM club WHERE id=?", (club_id,))
    if not row:
        return await ctx.send("No such club.")
    current = get_current_bid("club", row["id"])
    embed = discord.Embed(title=f"{row['name']}", description=row["slogan"] or "")
    embed.add_field(name="Base price", value=str(row["base_price"]))
    embed.add_field(name="Current bid", value=str(current))
    embed.add_field(name="Market value", value=str(row["value"]))
    await ctx.send(embed=embed)

# Duelist registration & auction
@bot.command()
async def registerduelist(ctx, username: str, base_price: int, expected_salary: int):
    """
    Duelist registers themselves (or admins can do this)
    !registerduelist <username> <base_price> <expected_salary>
    """
    avatar = ctx.author.avatar.url if ctx.author.avatar else ""
    db.query("INSERT INTO duelists (discord_user_id, username, avatar_url, base_price, expected_salary, registered_at) VALUES (?,?,?,?,?,?)",
             (str(ctx.author.id), username, avatar, base_price, expected_salary, datetime.now().isoformat()))
    d = db.fetchone("SELECT id FROM duelists WHERE discord_user_id=? ORDER BY id DESC", (str(ctx.author.id),))
    await ctx.send(f"Duelist **{username}** registered with ID **{d['id']}** (base {base_price}, salary {expected_salary}).")
    log_audit(f"{ctx.author} registered duelist {username} id={d['id']}")

@bot.command()
@commands.has_permissions(administrator=True)
async def startduelistauction(ctx, duelist_id: int):
    d = db.fetchone("SELECT * FROM duelists WHERE id=?", (duelist_id,))
    if not d:
        return await ctx.send("No such duelist ID.")
    db.query("DELETE FROM bids WHERE item_type='duelist' AND item_id=?", (str(duelist_id),))
    await ctx.send(f"🔔 Auction started for duelist **{d['username']}** (ID {duelist_id}). Base price: {d['base_price']}\nUse `!placebid <amount> duelist {duelist_id}` to bid.")
    log_audit(f"{ctx.author} started duelist auction id={duelist_id}")
    schedule_auction_timer("duelist", str(duelist_id), ctx.channel.id)

@bot.command()
async def listduelists(ctx):
    rows = db.fetchall("SELECT id, username, base_price, expected_salary, owned_by FROM duelists")
    if not rows:
        return await ctx.send("No duelists registered.")
    msg = "📜 Duelists:\n"
    for r in rows:
        owned = r["owned_by"] or "Free Agent"
        msg += f"- ID {r['id']}: {r['username']} | base {r['base_price']} | salary {r['expected_salary']} | {owned}\n"
    await ctx.send(msg)

# Generic bidding commands (personal and group)
@bot.command()
async def placebid(ctx, amount: int, item_type: str = "club", item_id: int = None):
    if bidding_frozen:
        return await ctx.send("Bidding is currently frozen by an admin.")
    if item_type not in ("club", "duelist"):
        return await ctx.send("item_type must be 'club' or 'duelist'.")
    if item_id is None:
        return await ctx.send("Provide the item_id (club id or duelist id).")
    # check min
    current = get_current_bid(item_type, str(item_id))
    min_req = min_required_bid(current)
    if amount < min_req:
        return await ctx.send(f"Minimum required bid is {min_req} (current {current}, +{MIN_INCREMENT_PERCENT}%).")
    db.query("INSERT INTO bids (bidder, amount, item_type, item_id) VALUES (?, ?, ?, ?)", (str(ctx.author), amount, item_type, str(item_id)))
    db.query("INSERT INTO audit_logs (entry) VALUES (?)", (f"{ctx.author} bid {amount} on {item_type} {item_id}",))
    await ctx.send(f"✅ New bid of **{amount}** on {item_type} {item_id} by {ctx.author.mention}")
    schedule_auction_timer(item_type, str(item_id), ctx.channel.id)

@bot.command()
async def groupbid(ctx, group_name: str, amount: int, item_type: str = "club", item_id: int = None):
    if bidding_frozen:
        return await ctx.send("Bidding is currently frozen.")
    if item_type not in ("club", "duelist"):
        return await ctx.send("item_type must be 'club' or 'duelist'.")
    if item_id is None:
        return await ctx.send("Provide the item_id.")
    g = db.fetchone("SELECT * FROM investor_groups WHERE name=?", (group_name.lower(),))
    if not g:
        return await ctx.send("No such group.")
    mem = db.fetchone("SELECT * FROM groups_members WHERE group_name=? AND user_id=?", (group_name.lower(), str(ctx.author.id)))
    if not mem:
        return await ctx.send("You are not in that group.")
    if amount > g["funds"]:
        return await ctx.send(f"Group lacks funds (available {g['funds']}).")
    current = get_current_bid(item_type, str(item_id))
    min_req = min_required_bid(current)
    if amount < min_req:
        return await ctx.send(f"Minimum required bid is {min_req}.")
    db.query("INSERT INTO bids (bidder, amount, item_type, item_id) VALUES (?, ?, ?, ?)", (group_name.lower() + " (group)", amount, item_type, str(item_id)))
    db.query("INSERT INTO audit_logs (entry) VALUES (?)", (f"Group {group_name} bid {amount} on {item_type} {item_id}",))
    await ctx.send(f"✅ Group **{group_name}** placed a bid of **{amount}** on {item_type} {item_id}.")
    # DM notify group members
    members = db.fetchall("SELECT user_id FROM groups_members WHERE group_name=?", (group_name.lower(),))
    for m in members:
        try:
            user = await bot.fetch_user(int(m["user_id"]))
            await user.send(f"📢 Your group **{group_name}** placed a bid of **{amount}** on {item_type} {item_id}.")
        except:
            pass
    schedule_auction_timer(item_type, str(item_id), ctx.channel.id)

# ---------- GROUP / WALLET / PROFILE / ADMIN COMMANDS ----------
@bot.command()
async def creategroup(ctx, name: str, starting_funds: int = 0):
    name = name.lower()
    if db.fetchone("SELECT * FROM investor_groups WHERE name=?", (name,)):
        return await ctx.send("Group already exists.")
    db.query("INSERT INTO investor_groups (name, funds) VALUES (?, ?)", (name, starting_funds))
    db.query("INSERT INTO groups_members (group_name, user_id) VALUES (?, ?)", (name, str(ctx.author.id)))
    log_audit(f"{ctx.author} created group {name} with starting {starting_funds}")
    await ctx.send(f"Group **{name}** created with funds **{starting_funds}** and you were added as a member.")

@bot.command()
async def joingroup(ctx, name: str):
    name = name.lower()
    g = db.fetchone("SELECT * FROM investor_groups WHERE name=?", (name,))
    if not g:
        return await ctx.send("No such group.")
    if db.fetchone("SELECT * FROM groups_members WHERE group_name=? AND user_id=?", (name, str(ctx.author.id))):
        return await ctx.send("You are already in this group.")
    db.query("INSERT INTO groups_members (group_name, user_id) VALUES (?, ?)", (name, str(ctx.author.id)))
    log_audit(f"{ctx.author} joined group {name}")
    await ctx.send(f"{ctx.author.mention} joined **{name}**.")

@bot.command()
async def leavegroup(ctx, name: str):
    name = name.lower()
    g = db.fetchone("SELECT * FROM investor_groups WHERE name=?", (name,))
    if not g:
        return await ctx.send("No such group.")
    if not db.fetchone("SELECT * FROM groups_members WHERE group_name=? AND user_id=?", (name, str(ctx.author.id))):
        return await ctx.send("You are not in this group.")
    # apply penalty on group's funds
    penalty = g["funds"] * LEAVE_PENALTY_PERCENT // 100
    new = max(0, g["funds"] - penalty)
    db.query("UPDATE investor_groups SET funds=? WHERE name=?", (new, name))
    db.query("DELETE FROM groups_members WHERE group_name=? AND user_id=?", (name, str(ctx.author.id)))
    log_audit(f"{ctx.author} left group {name}, penalty {penalty}")
    await ctx.send(f"{ctx.author.mention} left **{name}**. Penalty applied to group funds: **{penalty}**.")

@bot.command()
async def deposit(ctx, group_name: str, amount: int):
    g = db.fetchone("SELECT * FROM investor_groups WHERE name=?", (group_name.lower(),))
    if not g:
        return await ctx.send("No such group.")
    new = g["funds"] + amount
    db.query("UPDATE investor_groups SET funds=? WHERE name=?", (new, group_name.lower()))
    db.query("INSERT INTO audit_logs (entry) VALUES (?)", (f"{ctx.author} deposited {amount} to {group_name}",))
    await ctx.send(f"Deposited **{amount}** to **{group_name}**. New funds: {new}")

@bot.command()
async def withdraw(ctx, group_name: str, amount: int):
    g = db.fetchone("SELECT * FROM investor_groups WHERE name=?", (group_name.lower(),))
    if not g:
        return await ctx.send("No such group.")
    if amount > g["funds"]:
        return await ctx.send("Not enough group funds.")
    new = g["funds"] - amount
    db.query("UPDATE investor_groups SET funds=? WHERE name=?", (new, group_name.lower()))
    db.query("INSERT INTO audit_logs (entry) VALUES (?)", (f"{ctx.author} withdrew {amount} from {group_name}",))
    await ctx.send(f"Withdrew **{amount}** from **{group_name}**. New funds: {new}")

# personal wallet
@bot.command()
async def wallet(ctx):
    uid = str(ctx.author.id)
    row = db.fetchone("SELECT balance FROM personal_wallets WHERE user_id=?", (uid,))
    bal = int(row["balance"]) if row else 0
    await ctx.send(f"{ctx.author.mention} wallet balance: **{bal}**")

@bot.command()
async def depositwallet(ctx, amount: int):
    uid = str(ctx.author.id)
    row = db.fetchone("SELECT balance FROM personal_wallets WHERE user_id=?", (uid,))
    bal = int(row["balance"]) if row else 0
    new = bal + amount
    if row:
        db.query("UPDATE personal_wallets SET balance=? WHERE user_id=?", (new, uid))
    else:
        db.query("INSERT INTO personal_wallets (user_id, balance) VALUES (?, ?)", (uid, new))
    db.query("INSERT INTO wallet_transactions (user_id, amount, type) VALUES (?,?,?)", (uid, amount, "deposit"))
    log_audit(f"{ctx.author} deposited {amount} to personal wallet")
    await ctx.send(f"{ctx.author.mention} deposited **{amount}** to personal wallet. New balance: **{new}**")

@bot.command()
async def withdrawwallet(ctx, amount: int):
    uid = str(ctx.author.id)
    row = db.fetchone("SELECT balance FROM personal_wallets WHERE user_id=?", (uid,))
    bal = int(row["balance"]) if row else 0
    if amount > bal:
        return await ctx.send("Not enough funds.")
    new = bal - amount
    db.query("UPDATE personal_wallets SET balance=? WHERE user_id=?", (new, uid))
    db.query("INSERT INTO wallet_transactions (user_id, amount, type) VALUES (?,?,?)", (uid, amount, "withdraw"))
    log_audit(f"{ctx.author} withdrew {amount} from personal wallet")
    await ctx.send(f"{ctx.author.mention} withdrew **{amount}** from personal wallet. New balance: **{new}**")

# profile
@bot.command()
async def profile(ctx, member: discord.Member = None):
    member = member or ctx.author
    uid = str(member.id)
    prof = db.fetchone("SELECT * FROM user_profiles WHERE user_id=?", (uid,))
    bal = db.fetchone("SELECT balance FROM personal_wallets WHERE user_id=?", (uid,))
    groups = db.fetchall("SELECT group_name FROM groups_members WHERE user_id=?", (uid,))
    bids = db.fetchall("SELECT * FROM bids WHERE bidder LIKE ? ORDER BY id DESC LIMIT 10", (f"%{member}%",))
    embed = discord.Embed(title=f"Profile: {member}", color=0x00ff99)
    try:
        if member.avatar:
            embed.set_thumbnail(url=member.avatar.url)
    except:
        pass
    embed.add_field(name="Wallet", value=str(bal["balance"]) if bal else "0")
    embed.add_field(name="Groups", value=", ".join([g["group_name"] for g in groups]) if groups else "None", inline=False)
    embed.add_field(name="Recent Bids", value="\n".join([f"{b['bidder']} - {b['amount']}" for b in bids]) if bids else "No recent bids", inline=False)
    await ctx.send(embed=embed)

# manager & duelists list
@bot.command()
@commands.has_permissions(administrator=True)
async def setclubmanager(ctx, club_name: str, member: discord.Member):
    club = db.fetchone("SELECT * FROM club WHERE name=?", (club_name,))
    if not club:
        return await ctx.send("No such club.")
    db.query("UPDATE club SET manager_id=? WHERE name=?", (str(member.id), club_name))
    log_audit(f"{ctx.author} set {member} as manager for {club_name}")
    await ctx.send(f"{member.mention} set as manager for {club_name}.")

@bot.command()
async def clubmanager(ctx, club_name: str):
    club = db.fetchone("SELECT * FROM club WHERE name=?", (club_name,))
    if not club:
        return await ctx.send("No such club.")
    if not club["manager_id"]:
        return await ctx.send("No manager assigned.")
    try:
        user = await bot.fetch_user(int(club["manager_id"]))
        await ctx.send(f"Manager for {club_name}: {user.mention}")
    except:
        await ctx.send("Manager set but user not found.")

@bot.command()
async def clubduelists(ctx, club_name: str):
    club = db.fetchone("SELECT * FROM club WHERE name=?", (club_name,))
    if not club:
        return await ctx.send("No such club.")
    duelists = db.fetchall("SELECT * FROM duelists WHERE owned_by LIKE ?", (f"%{club_name}%",))
    if not duelists:
        return await ctx.send("No duelists signed to this club.")
    msg = f"📜 Duelists for {club_name}:\n"
    for d in duelists:
        msg += f"- {d['username']} (ID {d['id']}) | Salary: {d['expected_salary']} | Owned by: {d['owned_by']}\n"
    await ctx.send(msg)

# apply salary deduction when a duelist misses a match
@bot.command()
async def deductsalary(ctx, duelist_id: int, apply: str = "yes"):
    d = db.fetchone("SELECT * FROM duelists WHERE id=?", (duelist_id,))
    if not d:
        return await ctx.send("No such duelist.")
    contract = db.fetchone("SELECT * FROM duelist_contracts WHERE duelist_id=? ORDER BY id DESC LIMIT 1", (duelist_id,))
    if not contract:
        return await ctx.send("Duelist not contracted.")
    club_owner = contract["club_owner"]
    invoker_id = str(ctx.author.id)
    allowed = False
    # if group owner: allow members of group
    if "(group)" in club_owner:
        gname = club_owner.replace(" (group)", "").lower()
        if db.fetchone("SELECT * FROM groups_members WHERE group_name=? AND user_id=?", (gname, invoker_id)):
            allowed = True
    else:
        # compare invoker string to stored owner string OR allow server admins
        if str(ctx.author) == club_owner or ctx.author.guild_permissions.administrator:
            allowed = True
    if not allowed:
        return await ctx.send("You are not authorized to apply salary deduction for this duelist.")
    if apply.lower() not in ("yes", "no", "y", "n"):
        return await ctx.send("apply must be 'yes' or 'no'")
    if apply.lower() in ("no", "n"):
        return await ctx.send("Salary deduction skipped by club decision.")
    # apply deduction
    penalty = contract["salary"] * DUELIST_MISS_PENALTY_PERCENT // 100
    # deduct from group funds if group owned
    if "(group)" in club_owner:
        gname = club_owner.replace(" (group)", "").lower()
        g = db.fetchone("SELECT funds FROM investor_groups WHERE name=?", (gname,))
        if g:
            new = max(0, g["funds"] - penalty)
            db.query("UPDATE investor_groups SET funds=? WHERE name=?", (new, gname))
    log_audit(f"{ctx.author} applied salary deduction {penalty} for duelist {d['username']} (id {duelist_id})")
    await ctx.send(f"Salary deduction applied: {penalty} (15%) for duelist {d['username']}.")

# admin adjust club/group balance
@bot.command()
@commands.has_permissions(administrator=True)
async def adjustgroupfunds(ctx, group_name: str, amount: int):
    g = db.fetchone("SELECT * FROM investor_groups WHERE name=?", (group_name.lower(),))
    if not g:
        return await ctx.send("No such group.")
    new = max(0, g["funds"] + amount)
    db.query("UPDATE investor_groups SET funds=? WHERE name=?", (new, group_name.lower()))
    log_audit(f"{ctx.author} adjusted funds of {group_name} by {amount}. New funds {new}")
    await ctx.send(f"Adjusted funds of {group_name} by {amount}. New funds: {new}")

# owner/admin overrides
@bot.command()
@commands.is_owner()
async def forcewinner(ctx, item_type: str, item_id: int, winner_str: str, amount: int):
    if item_type not in ("club", "duelist"):
        return await ctx.send("item_type must be club or duelist.")
    if item_type == "club":
        db.query("INSERT INTO club_history (winner, amount, timestamp, market_value_at_sale) VALUES (?,?,datetime('now'),?)",
                 (winner_str, amount, (db.fetchone("SELECT value FROM club WHERE id=1")["value"] if db.fetchone("SELECT value FROM club WHERE id=1") else None)))
        log_audit(f"Owner forced winner {winner_str} for club {item_id} at {amount}")
        await ctx.send(f"Owner forced {winner_str} as winner for club {item_id} at {amount}")
    else:
        salary = db.fetchone("SELECT expected_salary FROM duelists WHERE id=?", (item_id,))
        salary_val = salary["expected_salary"] if salary else 0
        db.query("INSERT INTO duelist_contracts (duelist_id, club_owner, purchase_price, salary, signed_at) VALUES (?,?,?,?,datetime('now'))",
                 (item_id, winner_str, amount, salary_val))
        db.query("UPDATE duelists SET owned_by=? WHERE id=?", (winner_str, item_id))
        log_audit(f"Owner forced winner {winner_str} for duelist {item_id} at {amount}")
        await ctx.send(f"Owner forced {winner_str} as winner for duelist {item_id} at {amount}")

@bot.command()
@commands.is_owner()
async def freezeauction(ctx):
    global bidding_frozen
    bidding_frozen = True
    log_audit(f"{ctx.author} froze auctions")
    await ctx.send("All auctions frozen (owner).")

@bot.command()
@commands.is_owner()
async def unfreezeauction(ctx):
    global bidding_frozen
    bidding_frozen = False
    log_audit(f"{ctx.author} unfroze auctions")
    await ctx.send("Auctions unfrozen (owner).")

@bot.command()
@commands.is_owner()
async def auditlog(ctx, lines: int = 50):
    rows = db.fetchall("SELECT entry, timestamp FROM audit_logs ORDER BY id DESC LIMIT ?", (lines,))
    if not rows:
        return await ctx.send("No audit logs.")
    text = "\n".join([f"[{r['timestamp']}] {r['entry']}" for r in rows])
    for chunk in [text[i:i+1900] for i in range(0, len(text), 1900)]:
        await ctx.send(f"```{chunk}```")

@bot.command()
@commands.is_owner()
async def resetauction(ctx):
    db.query("DELETE FROM bids")
    db.query("INSERT INTO audit_logs (entry) VALUES (?)", (f"{ctx.author} reset auctions",))
    await ctx.send("All bids cleared and auctions reset.")

@bot.command()
@commands.is_owner()
async def transferclub(ctx, old_group: str, new_group: str):
    # sets latest club_history winner to new_group (quick admin override)
    latest = db.fetchone("SELECT id FROM club_history ORDER BY id DESC LIMIT 1")
    if not latest:
        return await ctx.send("No sale to transfer.")
    db.query("UPDATE club_history SET winner=? WHERE id=?", (new_group + " (group)", latest["id"]))
    log_audit(f"{ctx.author} transferred last sale from {old_group} to {new_group}")
    await ctx.send(f"Transferred club ownership from {old_group} to {new_group} (admin override).")

# simple help command override (shows many commands grouped)
@bot.command()
async def helpme(ctx):
    txt = """
**Club Auction Bot - Commands (summary)**
General:
!helpme - this help

Club admin/registration:
!registerclub <name> <base_price> [slogan]  (admin)
!listclubs
!startclubauction <club_name>  (admin)
!clubinfo <club_id>

Duelists:
!registerduelist <username> <base_price> <expected_salary>
!listduelists
!startduelistauction <duelist_id>  (admin)

Bids:
!placebid <amount> <item_type> <item_id>
!groupbid <group_name> <amount> <item_type> <item_id>

Groups/Wallets:
!creategroup <name> <starting_funds>
!joingroup <name>
!leavegroup <name>
!deposit <group> <amount>
!withdraw <group> <amount>
!wallet / !depositwallet / !withdrawwallet

Managers & Salary:
!setclubmanager <club_name> <@member>  (admin)
!clubmanager <club_name>
!clubduelists <club_name>
!deductsalary <duelist_id> <yes|no>

Admin/Owner:
!freezeauction / !unfreezeauction (owner)
!forcewinner (owner)
!auditlog (owner)
!resetauction (owner)
"""
    await ctx.send(txt)

# ---------- DASHBOARD (Optional FastAPI) ----------
if START_DASHBOARD:
    try:
        from fastapi import FastAPI, Request
        from fastapi.staticfiles import StaticFiles
        from fastapi.templating import Jinja2Templates
        import uvicorn
        import pathlib

        app = FastAPI()
        BASE = pathlib.Path(__file__).parent.joinpath("backend")
        templates_dir = BASE.joinpath("templates")
        static_dir = BASE.joinpath("static")
        # create minimal folders if not exists
        templates_dir.mkdir(parents=True, exist_ok=True)
        static_dir.mkdir(parents=True, exist_ok=True)
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
        templates = Jinja2Templates(directory=str(templates_dir))

        def get_db_conn():
            conn = sqlite3.connect(DB_FILE)
            conn.row_factory = sqlite3.Row
            return conn

        @app.get("/")
        def index(request: Request):
            conn = get_db_conn()
            club = conn.execute("SELECT * FROM club WHERE id=1").fetchone()
            conn.close()
            return templates.TemplateResponse("index.html", {"request": request, "club": club})

        def run_dashboard():
            uvicorn.run(app, host=DASHBOARD_HOST, port=DASHBOARD_PORT)

        t = threading.Thread(target=run_dashboard, daemon=True)
        t.start()
        print(f"[dashboard] started at http://{DASHBOARD_HOST}:{DASHBOARD_PORT}/")
    except Exception as e:
        print("Failed to start dashboard:", e)
        START_DASHBOARD = False

# ---------- START BACKGROUND TASKS AFTER READY ----------
@bot.event
async def on_ready():
    print("Bot started as", bot.user)
    bot.loop.create_task(market_simulation_task())
    bot.loop.create_task(weekly_report_scheduler())

# ---------- RUN ----------
if __name__ == "__main__":
    if DISCORD_TOKEN == "PASTE_YOUR_TOKEN_HERE" or not DISCORD_TOKEN:
        print("ERROR: Please set your DISCORD_TOKEN environment variable OR paste your token into DISCORD_TOKEN in this file.")
    else:
        bot.run(discord_token)
