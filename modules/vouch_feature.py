"""
=====================================================================
  VOUCH BOT — COMPLETE PRODUCTION BUILD
  Python 3.11+ | discord.py 2.4+ | aiosqlite | aiohttp
  Optimized for Render Free Tier (<512MB RAM)
=====================================================================
"""

import datetime
import logging
import os
import re
import secrets
import string
import time

from aiohttp import web
import aiosqlite
import discord
from discord import app_commands, ui
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

# =====================================================================
# CONFIG
# =====================================================================

TOKEN = os.getenv("DISCORD_TOKEN")
PORT = int(os.getenv("PORT", 8080))
DB_PATH = os.getenv("DB_PATH", "data/vouch_bot.db")
BOT_NAME = os.getenv("BOT_NAME", "MalixAris Bot").strip() or "MalixAris Bot"
BOT_CREATOR_NAME = os.getenv("BOT_CREATOR_NAME", "abubakarmlikgul").strip() or "abubakarmlikgul"
GUILD_ID = os.getenv("GUILD_ID")

COLOR_POSITIVE = 0x2ECC71
COLOR_NEGATIVE = 0xE74C3C
COLOR_NEUTRAL = 0xF1C40F
COLOR_DARK = 0x2B2D31
COLOR_INFO = 0x5865F2
COLOR_WARN = 0xE67E22

RANK_TIERS = [
    (0, 5, "🆕 New Seller"),
    (5, 20, "✅ Trusted Seller"),
    (20, 50, "⭐ Veteran Seller"),
    (50, 100, "👑 Elite Seller"),
    (100, float("inf"), "💎 Legendary Seller"),
]

TYPE_MAP = {
    1: ("Positive", COLOR_POSITIVE, "✅"),
    -1: ("Negative", COLOR_NEGATIVE, "⚠️"),
    0: ("Neutral", COLOR_NEUTRAL, "➖"),
}

ID_ALPHABET = string.ascii_uppercase + string.digits

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("vouch_bot")


# =====================================================================
# DATABASE LAYER
# =====================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS vouches (
    id TEXT PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    target_id INTEGER NOT NULL,
    author_id INTEGER NOT NULL,
    type INTEGER NOT NULL,
    comment TEXT,
    timestamp INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vouches_guild_target ON vouches(guild_id, target_id);
CREATE INDEX IF NOT EXISTS idx_vouches_guild_type ON vouches(guild_id, type);
CREATE INDEX IF NOT EXISTS idx_vouches_id_prefix ON vouches(guild_id, id);
CREATE INDEX IF NOT EXISTS idx_vouches_guild_author ON vouches(guild_id, author_id);

CREATE TABLE IF NOT EXISTS guild_config (
    guild_id INTEGER PRIMARY KEY,
    vouch_channel_id INTEGER,
    log_channel_id INTEGER,
    admin_role_id INTEGER
);

CREATE TABLE IF NOT EXISTS blacklists (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    reason TEXT,
    moderator_id INTEGER,
    timestamp INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);
"""


class Database:

  def __init__(self, path: str):
    self.path = path
    self._conn: aiosqlite.Connection | None = None

  async def connect(self):
    self._conn = await aiosqlite.connect(self.path)
    self._conn.row_factory = aiosqlite.Row
    await self._conn.execute("PRAGMA journal_mode=WAL;")
    await self._conn.execute("PRAGMA synchronous=NORMAL;")
    await self._conn.execute("PRAGMA temp_store=MEMORY;")
    await self._conn.execute("PRAGMA cache_size=-2000;")
    await self._conn.executescript(SCHEMA)
    await self._conn.commit()
    logger.info("Database ready at %s", self.path)

  async def close(self):
    if self._conn:
      await self._conn.close()

  async def _commit(self):
    await self._conn.commit()
    try:
      from modules.backup_feature import notify_state_changed
      notify_state_changed("Vouch database changed")
    except Exception:
      logger.debug("Could not queue a state backup after vouch database commit", exc_info=True)

  async def _generate_id(self) -> str:
    for _ in range(15):
      candidate = "".join(secrets.choice(ID_ALPHABET) for _ in range(6))
      cur = await self._conn.execute(
          "SELECT 1 FROM vouches WHERE id = ?", (candidate,)
      )
      row = await cur.fetchone()
      await cur.close()
      if not row:
        return candidate
    raise RuntimeError("Failed to generate a unique vouch ID.")

  async def add_vouch(
      self,
      guild_id: int,
      target_id: int,
      author_id: int,
      vtype: int,
      comment: str,
  ):
    vouch_id = await self._generate_id()
    ts = int(time.time())
    await self._conn.execute(
        "INSERT INTO vouches (id, guild_id, target_id, author_id, type,"
        " comment, timestamp) VALUES (?,?,?,?,?,?,?)",
        (vouch_id, guild_id, target_id, author_id, vtype, comment, ts),
    )
    await self._commit()
    return vouch_id, ts

  async def add_bulk_vouches(
      self, guild_id: int, target_id: int, author_id: int, vouches_data: list
  ):
    """Executes atomic insertions for high-count vouch imports."""
    ts = int(time.time())
    to_insert = []
    for vtype, comment in vouches_data:
      vouch_id = await self._generate_id()
      to_insert.append(
          (vouch_id, guild_id, target_id, author_id, vtype, comment, ts)
      )

    await self._conn.executemany(
        "INSERT INTO vouches (id, guild_id, target_id, author_id, type,"
        " comment, timestamp) VALUES (?,?,?,?,?,?,?)",
        to_insert,
    )
    await self._commit()
    return len(to_insert)

  async def get_vouch(self, vouch_id: str):
    cur = await self._conn.execute(
        "SELECT * FROM vouches WHERE id = ?", (vouch_id.upper(),)
    )
    row = await cur.fetchone()
    await cur.close()
    return row

  async def update_vouch_comment(self, vouch_id: str, new_comment: str) -> bool:
    cur = await self._conn.execute(
        "UPDATE vouches SET comment=? WHERE id=?",
        (new_comment, vouch_id.upper()),
    )
    await self._commit()
    return cur.rowcount > 0

  async def search_vouch_ids(self, guild_id: int, prefix: str, limit=25):
    cur = await self._conn.execute(
        "SELECT * FROM vouches WHERE guild_id=? AND id LIKE ? ORDER BY"
        " timestamp DESC LIMIT ?",
        (guild_id, f"{prefix.upper()}%", limit),
    )
    rows = await cur.fetchall()
    await cur.close()
    return rows

  async def remove_vouch(self, vouch_id: str):
    row = await self.get_vouch(vouch_id)
    if not row:
      return None
    await self._conn.execute(
        "DELETE FROM vouches WHERE id = ?", (vouch_id.upper(),)
    )
    await self._commit()
    return row

  async def get_counts(
      self, guild_id: int, author_id: int, target_id: int
  ) -> dict:
    cur = await self._conn.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM vouches WHERE guild_id=? AND author_id=?)
            AS given,
          (SELECT COUNT(*) FROM vouches WHERE guild_id=? AND target_id=?)
            AS received,
          (SELECT COUNT(*) FROM vouches WHERE guild_id=?) AS server_total
        """,
        (guild_id, author_id, guild_id, target_id, guild_id),
    )
    row = await cur.fetchone()
    await cur.close()
    return {
        "given": row["given"] or 0,
        "received": row["received"] or 0,
        "server_total": row["server_total"] or 0,
    }

  async def get_stats(self, guild_id: int, user_id: int) -> dict:
    cur = await self._conn.execute(
        "SELECT type, COUNT(*) as cnt FROM vouches WHERE guild_id=? AND"
        " target_id=? GROUP BY type",
        (guild_id, user_id),
    )
    rows = await cur.fetchall()
    await cur.close()

    stats = {1: 0, -1: 0, 0: 0, "total": 0}
    for r in rows:
      stats[r["type"]] = r["cnt"]
      stats["total"] += r["cnt"]
    return stats

  async def get_latest_vouches(
      self, guild_id: int, user_id: int, limit: int = 5
  ):
    cur = await self._conn.execute(
        "SELECT * FROM vouches WHERE guild_id=? AND target_id=? ORDER BY"
        " timestamp DESC LIMIT ?",
        (guild_id, user_id, limit),
    )
    rows = await cur.fetchall()
    await cur.close()
    return rows

  async def get_leaderboard(self, guild_id: int, limit: int = 10):
    cur = await self._conn.execute(
        """
        SELECT target_id,
               SUM(CASE WHEN type = 1 THEN 1 ELSE 0 END) AS positive,
               SUM(CASE WHEN type = -1 THEN 1 ELSE 0 END) AS negative,
               SUM(CASE WHEN type = 0 THEN 1 ELSE 0 END) AS neutral,
               COUNT(*) AS total
        FROM vouches 
        WHERE guild_id = ?
        GROUP BY target_id 
        ORDER BY total DESC, positive DESC 
        LIMIT ?
        """,
        (guild_id, limit),
    )
    rows = await cur.fetchall()
    await cur.close()
    return rows

  async def get_guild_summary(self, guild_id: int):
    cur = await self._conn.execute(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN type=1 THEN 1 ELSE 0 END) AS positive,
               SUM(CASE WHEN type=-1 THEN 1 ELSE 0 END) AS negative,
               SUM(CASE WHEN type=0 THEN 1 ELSE 0 END) AS neutral,
               COUNT(DISTINCT target_id) AS sellers
        FROM vouches WHERE guild_id=?
        """,
        (guild_id,),
    )
    row = await cur.fetchone()
    await cur.close()

    cur2 = await self._conn.execute(
        "SELECT COUNT(*) as cnt FROM blacklists WHERE guild_id=?", (guild_id,)
    )
    bl = await cur2.fetchone()
    await cur2.close()
    return row, (bl["cnt"] if bl else 0)

  async def get_config(self, guild_id: int):
    cur = await self._conn.execute(
        "SELECT * FROM guild_config WHERE guild_id=?", (guild_id,)
    )
    row = await cur.fetchone()
    await cur.close()
    return row

  async def upsert_config(self, guild_id: int, **kwargs):
    existing = await self.get_config(guild_id)
    if existing:
      fields = ", ".join(f"{k}=?" for k in kwargs)
      values = list(kwargs.values()) + [guild_id]
      await self._conn.execute(
          f"UPDATE guild_config SET {fields} WHERE guild_id=?", values
      )
    else:
      keys = ["guild_id"] + list(kwargs.keys())
      placeholders = ",".join("?" for _ in keys)
      values = [guild_id] + list(kwargs.values())
      await self._conn.execute(
          f"INSERT INTO guild_config ({','.join(keys)}) VALUES ({placeholders})",
          values,
      )
    await self._commit()

  async def is_blacklisted(self, guild_id: int, user_id: int) -> bool:
    cur = await self._conn.execute(
        "SELECT 1 FROM blacklists WHERE guild_id=? AND user_id=?",
        (guild_id, user_id),
    )
    row = await cur.fetchone()
    await cur.close()
    return row is not None

  async def add_blacklist(
      self, guild_id: int, user_id: int, reason: str, moderator_id: int
  ):
    ts = int(time.time())
    await self._conn.execute(
        "INSERT OR REPLACE INTO blacklists (guild_id, user_id, reason,"
        " moderator_id, timestamp) VALUES (?,?,?,?,?)",
        (guild_id, user_id, reason, moderator_id, ts),
    )
    await self._commit()

  async def remove_blacklist(self, guild_id: int, user_id: int) -> bool:
    cur = await self._conn.execute(
        "DELETE FROM blacklists WHERE guild_id=? AND user_id=?",
        (guild_id, user_id),
    )
    await self._commit()
    return cur.rowcount > 0


db = Database(DB_PATH)


# =====================================================================
# HELPERS
# =====================================================================


def get_rank(positive_count: int) -> str:
  for low, high, name in RANK_TIERS:
    if low <= positive_count < high:
      return name
  return RANK_TIERS[-1][2]


def progress_bar(percent: float, length: int = 10) -> str:
  filled = max(0, min(length, round((percent / 100) * length)))
  return "🟩" * filled + "⬛" * (length - filled)


async def resolve_member(guild: discord.Guild | None, user_id: int):
  if guild is None:
    return None
  member = guild.get_member(user_id)
  if member:
    return member
  try:
    return await guild.fetch_member(user_id)
  except (discord.NotFound, discord.HTTPException):
    return None


def is_staff():

  async def predicate(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
      raise app_commands.CheckFailure(
          "This command can only be used in a server."
      )
    if interaction.user.guild_permissions.administrator:
      return True
    config = await db.get_config(interaction.guild_id)
    if config and config["admin_role_id"]:
      role = interaction.guild.get_role(config["admin_role_id"])
      if role and role in interaction.user.roles:
        return True
    raise app_commands.CheckFailure(
        "❌ You need **Administrator** permission or the configured staff role"
        " to use this command."
    )

  return app_commands.check(predicate)


async def vouch_id_autocomplete(interaction: discord.Interaction, current: str):
  if not interaction.guild_id:
    return []
  rows = await db.search_vouch_ids(interaction.guild_id, current)
  choices = []
  for r in rows:
    emoji = TYPE_MAP.get(r["type"], TYPE_MAP[0])[2]
    snippet = (r["comment"] or "No reason")[:35]
    choices.append(
        app_commands.Choice(
            name=f"{emoji} {r['id']} — {snippet}", value=r["id"]
        )
    )
  return choices[:25]


async def safe_send(channel: discord.abc.Messageable, **kwargs):
  try:
    return await channel.send(**kwargs)
  except discord.Forbidden:
    logger.warning(
        "Missing permission to send in channel %s", getattr(channel, "id", "?")
    )
    return None
  except discord.HTTPException as e:
    logger.warning("Failed to send message: %s", e)
    return None


# =====================================================================
# EMBEDS
# =====================================================================


def vouch_embed(
    vouch_id: str,
    author: discord.abc.User,
    target: discord.abc.User,
    vtype: int,
    comment: str,
    counts: dict | None = None,
) -> discord.Embed:
  label, color, emoji = TYPE_MAP.get(vtype, TYPE_MAP[0])
  embed = discord.Embed(
      title=f"{emoji} {label} Vouch",
      color=color,
      timestamp=discord.utils.utcnow(),
  )
  embed.description = (
      f"**Vouch by** {author.mention}\n"
      f"**Vouch to** {target.mention}\n"
      f"**Reason** {comment or '*No reason provided*'}"
  )
  if counts:
    embed.add_field(
        name="Totals",
        value=(
            f"Vouches given by {author.display_name}:"
            f" **{counts['given']}**\n"
            f"Vouches received by {target.display_name}:"
            f" **{counts['received']}**\n"
            f"Total vouches in server: **{counts['server_total']}**"
        ),
        inline=False,
    )
  embed.set_thumbnail(url=target.display_avatar.url)
  embed.set_footer(text=f"Vouch ID: {vouch_id} • {BOT_NAME}")
  return embed


def vouch_detail_embed(row, author_user, target_user) -> discord.Embed:
  vtype = row["type"]
  label, color, emoji = TYPE_MAP.get(vtype, TYPE_MAP[0])
  embed = discord.Embed(title=f"{emoji} Vouch Details", color=color)
  embed.add_field(name="🆔 ID", value=f"`{row['id']}`", inline=True)
  embed.add_field(name="📌 Type", value=label, inline=True)
  embed.add_field(name="\u200b", value="\u200b", inline=True)
  embed.add_field(
      name="👤 From",
      value=(
          author_user.mention
          if author_user
          else f"<@{row['author_id']}> *(Left Server)*"
      ),
      inline=True,
  )
  embed.add_field(
      name="🎯 To",
      value=(
          target_user.mention
          if target_user
          else f"<@{row['target_id']}> *(Left Server)*"
      ),
      inline=True,
  )
  embed.add_field(name="\u200b", value="\u200b", inline=True)
  embed.add_field(
      name="💬 Reason", value=row["comment"] or "*No reason provided*", inline=False
  )
  embed.timestamp = datetime.datetime.fromtimestamp(
      row["timestamp"], tz=datetime.timezone.utc
  )
  embed.set_footer(text=f"Vouch ID: {row['id']} • {BOT_NAME}")
  return embed


def profile_embed(
    user: discord.abc.User, stats: dict, latest_vouches
) -> discord.Embed:
  positive = stats.get(1, 0)
  negative = stats.get(-1, 0)
  neutral = stats.get(0, 0)
  total = stats.get("total", (positive + negative + neutral))
  trust = (positive / total * 100) if total > 0 else 0.0
  net = positive - negative
  rank = get_rank(positive)

  embed = discord.Embed(
      title="📊 Seller Reputation Profile", color=COLOR_DARK
  )
  embed.set_author(name=str(user), icon_url=user.display_avatar.url)
  embed.set_thumbnail(url=user.display_avatar.url)
  embed.add_field(name="📦 Total Vouches", value=f"**{total:,}**", inline=True)
  embed.add_field(name="✅ Positive", value=f"**{positive:,}**", inline=True)
  embed.add_field(name="⚠️ Negative", value=f"**{negative:,}**", inline=True)
  embed.add_field(name="➖ Neutral", value=f"**{neutral:,}**", inline=True)
  embed.add_field(name="⚖️ Net Score", value=f"**{net:+d}**", inline=True)
  embed.add_field(name="🏆 Current Rank", value=f"**{rank}**", inline=True)
  embed.add_field(
      name=f"📈 Trust Rating — {trust:.1f}%",
      value=f"{progress_bar(trust)}",
      inline=False,
  )

  if latest_vouches:
    lines = []
    for v in latest_vouches:
      _, _, emoji = TYPE_MAP.get(v["type"], TYPE_MAP[0])
      snippet = (v["comment"] or "No comment")[:50]
      lines.append(f"{emoji} `{v['id']}` — {snippet} (<t:{v['timestamp']}:R>)")
    embed.add_field(
        name="🕒 Latest Reviews", value="\n".join(lines), inline=False
    )
  else:
    embed.add_field(
        name="🕒 Latest Reviews", value="*No reviews recorded yet.*", inline=False
    )

  embed.set_footer(text=f"Official Reputation Database • {BOT_NAME}")
  embed.timestamp = discord.utils.utcnow()
  return embed


def leaderboard_embed(guild: discord.Guild, rows, name_resolver) -> discord.Embed:
  embed = discord.Embed(
      title=f"🏆 Top Reputed Sellers — {guild.name}", color=COLOR_DARK
  )
  if guild.icon:
    embed.set_thumbnail(url=guild.icon.url)
  if not rows:
    embed.description = "No vouches logged yet."
    return embed

  medals = ["🥇", "🥈", "🥉"]
  lines = []
  for i, row in enumerate(rows):
    prefix = medals[i] if i < 3 else f"`#{i + 1:02d}`"
    name = name_resolver(row["target_id"])
    lines.append(
        f"{prefix} **{name}**\n   📦 **{row['total']:,} Total** (✅"
        f" {row['positive']:,} | ⚠️ {row['negative']:,})\n"
    )
  embed.description = "\n".join(lines)
  embed.set_footer(text=f"Sorted by Total Confirmed Vouches • {BOT_NAME}")
  embed.timestamp = discord.utils.utcnow()
  return embed


def stats_embed(
    guild: discord.Guild, summary_row, blacklist_count: int
) -> discord.Embed:
  total = summary_row["total"] or 0
  positive = summary_row["positive"] or 0
  negative = summary_row["negative"] or 0
  neutral = summary_row["neutral"] or 0
  sellers = summary_row["sellers"] or 0
  ratio = (positive / total * 100) if total else 0.0

  embed = discord.Embed(title="📈 Overall Server Vouch Metrics", color=COLOR_INFO)
  if guild.icon:
    embed.set_thumbnail(url=guild.icon.url)
  embed.add_field(name="📦 All Vouches", value=f"**{total:,}**", inline=True)
  embed.add_field(name="✅ Positive", value=f"**{positive:,}**", inline=True)
  embed.add_field(name="⚠️ Negative", value=f"**{negative:,}**", inline=True)
  embed.add_field(name="➖ Neutral", value=f"**{neutral:,}**", inline=True)
  embed.add_field(name="👥 Active Sellers", value=f"**{sellers:,}**", inline=True)
  embed.add_field(
      name="🚫 Blacklisted Users", value=f"**{blacklist_count}**", inline=True
  )
  embed.add_field(
      name="📊 Positivity Rate", value=f"**{ratio:.1f}%**", inline=False
  )
  embed.set_footer(text=f"{guild.name} • Reputation Hub • {BOT_NAME}")
  embed.timestamp = discord.utils.utcnow()
  return embed


def help_embed(section: str = "home") -> discord.Embed:
  if section == "vouch":
    embed = discord.Embed(
        title="📥 Vouch Commands",
        color=COLOR_INFO,
        description=(
            "• `/vouch add` — Log a vouch for a trader\n"
            "• `/vouch view` — Inspect a specific vouch by ID\n"
            "• `/vouch edit` — Modify your submitted reason\n"
            "• `/vouch remove` — *(Staff)* Delete a fraudulent vouch\n"
            "• `/vouch import` — *(Staff)* Import bulk records\n\n"
            "**Fast Chat Shortcuts:**\n"
            "`+vouch @user [reason]` — Submit instant positive review\n"
            "`-vouch @user [reason]` — Submit instant negative review"
        ),
    )
  elif section == "profile":
    embed = discord.Embed(
        title="📊 Reputation Commands",
        color=COLOR_INFO,
        description=(
            "• `/vouch profile [user]` — Check full vouch stats and ratings\n"
            "• `/vouch leaderboard` — View the top ranked sellers\n"
            "• `/vouch stats` — Total server metrics and vouch counts"
        ),
    )
  elif section == "admin":
    embed = discord.Embed(
        title="⚙️ Administration Tools",
        color=COLOR_INFO,
        description=(
            "• `/vouch setup` — Bind feed & moderation audit channels\n"
            "• `/vouch blacklist add` — Revoke user vouch privileges\n"
            "• `/vouch blacklist remove` — Restore user vouch privileges\n"
            "• `/vouch import` — Bulk insert thousands of vouches"
        ),
    )
  else:
    embed = discord.Embed(
        title=f"🛡️ {BOT_NAME} • Vouch Help",
        color=COLOR_DARK,
        description=(
            "Welcome to the server reputation system.\n\n"
            "**Key Quick Start:**\n"
            "1. Run `/vouch setup` to initialize channel destinations.\n"
            "2. Trade and submit vouches using `+vouch @user trade note`.\n"
            "3. Query live trust profiles with `/vouch profile`.\n\n"
            "Use the menu below to view specific category guides."
        ),
    )
  embed.set_footer(text=f"Select category below • Made by {BOT_CREATOR_NAME}")
  return embed


# =====================================================================
# UI COMPONENTS
# =====================================================================


class ReportVouchButton(
    ui.DynamicItem[ui.Button],
    template=r"report_vouch:(?P<vouch_id>[A-Z0-9]{6})",
):

  def __init__(self, vouch_id: str):
    super().__init__(
        ui.Button(
            label="Report",
            style=discord.ButtonStyle.secondary,
            emoji="🚩",
            custom_id=f"report_vouch:{vouch_id}",
        )
    )
    self.vouch_id = vouch_id

  @classmethod
  async def from_custom_id(cls, interaction, item, match: re.Match):
    return cls(match["vouch_id"])

  async def callback(self, interaction: discord.Interaction):
    await interaction.response.send_message(
        f"🚩 Report for vouch `{self.vouch_id}` forwarded to staff.",
        ephemeral=True,
    )
    if not interaction.guild:
      return
    row = await db.get_vouch(self.vouch_id)
    if not row:
      return
    config = await db.get_config(interaction.guild.id)
    if config and config["log_channel_id"]:
      channel = interaction.guild.get_channel(config["log_channel_id"])
      if channel:
        embed = discord.Embed(
            title="🚩 Vouch Under Review",
            description=(
                f"Vouch `{self.vouch_id}` flagged by {interaction.user.mention}."
            ),
            color=COLOR_WARN,
        )
        embed.add_field(
            name="Target", value=f"<@{row['target_id']}>", inline=True
        )
        embed.add_field(
            name="Author", value=f"<@{row['author_id']}>", inline=True
        )
        embed.add_field(
            name="Reason",
            value=row["comment"] or "None provided",
            inline=False,
        )
        embed.timestamp = discord.utils.utcnow()
        await safe_send(channel, embed=embed)


class VouchView(ui.View):

  def __init__(self, vouch_id: str):
    super().__init__(timeout=None)
    self.add_item(ReportVouchButton(vouch_id))


class HelpSelect(ui.Select):

  def __init__(self):
    options = [
        discord.SelectOption(label="Home", value="home", emoji="🏠"),
        discord.SelectOption(
            label="Vouch Handling", value="vouch", emoji="📥"
        ),
        discord.SelectOption(
            label="Profiles & Leaderboards", value="profile", emoji="📊"
        ),
        discord.SelectOption(
            label="Staff Administration", value="admin", emoji="⚙️"
        ),
    ]
    super().__init__(
        placeholder="Select a category to view...", options=options
    )

  async def callback(self, interaction: discord.Interaction):
    await interaction.response.edit_message(
        embed=help_embed(self.values[0]), view=self.view
    )


class HelpView(ui.View):

  def __init__(self):
    super().__init__(timeout=180)
    self.add_item(HelpSelect())


# =====================================================================
# BOT CLIENT CONFIGURATION
# =====================================================================

INTENTS = discord.Intents.default()
INTENTS.message_content = True


class VouchBot(commands.Bot):

  def __init__(self):
    super().__init__(
        command_prefix=commands.when_mentioned,
        intents=INTENTS,
        max_messages=None,
    )
    self.db = db
    self.web_runner: web.AppRunner | None = None

  async def setup_hook(self):
    await self.db.connect()
    self.add_dynamic_items(ReportVouchButton)
    self.tree.add_command(vouch_group)
    self.tree.add_command(blacklist_group)

    self.web_runner = await start_web_server(PORT)

    if GUILD_ID:
      guild_obj = discord.Object(id=int(GUILD_ID))
      self.tree.copy_global_to(guild=guild_obj)
      await self.tree.sync(guild=guild_obj)
      logger.info("Synced commands instantly to Guild ID: %s", GUILD_ID)
    else:
      await self.tree.sync()
      logger.info("Synced application command tree globally.")


  async def close(self):
    await self.db.close()
    if self.web_runner:
      await self.web_runner.cleanup()
    await super().close()


bot = VouchBot()


# =====================================================================
# HEALTH ENDPOINT (for standalone feature use)
# =====================================================================


async def _health(request):
  return web.Response(text="Bot operational", status=200)


async def start_web_server(port: int) -> web.AppRunner:
  app = web.Application()
  app.router.add_get("/", _health)
  app.router.add_get("/health", _health)
  runner = web.AppRunner(app)
  await runner.setup()
  site = web.TCPSite(runner, "0.0.0.0", port)
  await site.start()
  logger.info("Web service healthcheck listening on 0.0.0.0:%s", port)
  return runner


# =====================================================================
# COMMAND GROUPS & LOGIC
# =====================================================================

vouch_group = app_commands.Group(name="vouch", description="Vouch management")
blacklist_group = app_commands.Group(
    name="blacklist", description="Vouch moderation"
)

VOUCH_TYPE_CHOICES = [
    app_commands.Choice(name="Positive (+1)", value=1),
    app_commands.Choice(name="Negative (-1)", value=-1),
    app_commands.Choice(name="Neutral (0)", value=0),
]


async def create_vouch(guild, author, target, vtype, comment):
  if target.id == author.id:
    return None, "❌ You cannot vouch for yourself."
  if target.bot:
    return None, "❌ You cannot vouch for a bot."
  if await db.is_blacklisted(guild.id, author.id):
    return None, "❌ You are blacklisted from submitting vouches."
  if await db.is_blacklisted(guild.id, target.id):
    return None, "❌ That user is blacklisted and cannot receive vouches."

  vouch_id, _ = await db.add_vouch(
      guild.id, target.id, author.id, vtype, comment
  )
  counts = await db.get_counts(guild.id, author.id, target.id)
  embed = vouch_embed(vouch_id, author, target, vtype, comment, counts)
  view = VouchView(vouch_id)

  config = await db.get_config(guild.id)
  target_channel = None
  if config and config["vouch_channel_id"]:
    target_channel = guild.get_channel(config["vouch_channel_id"])

  return (vouch_id, embed, view, config, target_channel), None


@vouch_group.command(name="add", description="Submit an official vouch")
@app_commands.describe(
    user="Seller or buyer to review",
    type="Transaction rating type",
    reason="Transaction description or proof note",
)
@app_commands.choices(type=VOUCH_TYPE_CHOICES)
@app_commands.guild_only()
async def vouch_add(
    interaction: discord.Interaction,
    user: discord.Member,
    type: app_commands.Choice[int],
    reason: str,
):
  await interaction.response.defer(thinking=True)
  result, error = await create_vouch(
      interaction.guild, interaction.user, user, type.value, reason
  )
  if error:
    return await interaction.followup.send(error, ephemeral=True)

  vouch_id, embed, view, config, target_channel = result
  send_target = target_channel or interaction.channel
  sent = await safe_send(send_target, embed=embed, view=view)

  if sent is None:
    return await interaction.followup.send(
        "⚠️ Vouch saved to database, but missing post permissions in the feed"
        " channel.",
        ephemeral=True,
    )

  if send_target.id != interaction.channel_id:
    await interaction.followup.send(
        f"✅ Vouch `{vouch_id}` posted to {send_target.mention}."
    )
  else:
    await interaction.followup.send(f"✅ Vouch `{vouch_id}` registered.")

  if config and config["log_channel_id"]:
    log_ch = interaction.guild.get_channel(config["log_channel_id"])
    if log_ch and log_ch.id != send_target.id:
      await safe_send(log_ch, embed=embed)


@vouch_group.command(name="view", description="Retrieve detailed vouch record")
@app_commands.describe(vouch_id="The unique 6-character vouch ID")
@app_commands.autocomplete(vouch_id=vouch_id_autocomplete)
@app_commands.guild_only()
async def vouch_view(interaction: discord.Interaction, vouch_id: str):
  await interaction.response.defer()
  row = await db.get_vouch(vouch_id)
  if not row or row["guild_id"] != interaction.guild_id:
    return await interaction.followup.send(
        "❌ Vouch record not found.", ephemeral=True
    )
  author_user = await resolve_member(interaction.guild, row["author_id"])
  target_user = await resolve_member(interaction.guild, row["target_id"])
  await interaction.followup.send(
      embed=vouch_detail_embed(row, author_user, target_user)
  )


@vouch_group.command(name="edit", description="Update a submitted vouch reason")
@app_commands.describe(
    vouch_id="Target ID to modify", new_reason="Updated comment text"
)
@app_commands.autocomplete(vouch_id=vouch_id_autocomplete)
@app_commands.guild_only()
async def vouch_edit(
    interaction: discord.Interaction, vouch_id: str, new_reason: str
):
  await interaction.response.defer(ephemeral=True)
  row = await db.get_vouch(vouch_id)
  if not row or row["guild_id"] != interaction.guild_id:
    return await interaction.followup.send(
        "❌ Vouch record not found.", ephemeral=True
    )

  if (
      row["author_id"] != interaction.user.id
      and not interaction.user.guild_permissions.administrator
  ):
    return await interaction.followup.send(
        "❌ Only the author or administrators can modify this entry.",
        ephemeral=True,
    )

  await db.update_vouch_comment(vouch_id, new_reason)
  await interaction.followup.send(
      f"✅ Vouch `{vouch_id.upper()}` comment updated successfully."
  )


@vouch_group.command(
    name="remove", description="[Staff] Delete a vouch from the records"
)
@app_commands.describe(vouch_id="Vouch ID to delete", reason="Removal motive")
@app_commands.autocomplete(vouch_id=vouch_id_autocomplete)
@app_commands.guild_only()
@is_staff()
async def vouch_remove(
    interaction: discord.Interaction, vouch_id: str, reason: str
):
  await interaction.response.defer(ephemeral=True)
  row = await db.remove_vouch(vouch_id)
  if not row or row["guild_id"] != interaction.guild_id:
    return await interaction.followup.send(
        "❌ Record could not be found.", ephemeral=True
    )

  await interaction.followup.send(f"🗑️ Vouch `{vouch_id}` removed from index.")

  config = await db.get_config(interaction.guild_id)
  if config and config["log_channel_id"]:
    ch = interaction.guild.get_channel(config["log_channel_id"])
    if ch:
      embed = discord.Embed(
          title="🗑️ Vouch Purged",
          color=0x95A5A6,
          description=(
              f"Vouch `{vouch_id}` deleted by {interaction.user.mention}"
          ),
      )
      embed.add_field(name="Reason", value=reason, inline=False)
      embed.add_field(
          name="Seller", value=f"<@{row['target_id']}>", inline=True
      )
      embed.add_field(
          name="Author", value=f"<@{row['author_id']}>", inline=True
      )
      embed.timestamp = discord.utils.utcnow()
      await safe_send(ch, embed=embed)


@vouch_group.command(
    name="import", description="[Staff] Bulk import historical counts"
)
@app_commands.describe(
    user="Recipient member",
    positive="Positive entries to create (up to 5,000)",
    negative="Negative entries to create (up to 5,000)",
    neutral="Neutral entries to create (up to 5,000)",
    note="Origin reference or batch note",
)
@app_commands.guild_only()
@is_staff()
async def vouch_import(
    interaction: discord.Interaction,
    user: discord.Member,
    positive: app_commands.Range[int, 0, 5000] = 0,
    negative: app_commands.Range[int, 0, 5000] = 0,
    neutral: app_commands.Range[int, 0, 5000] = 0,
    note: str = "Imported Historical Entry",
):
  await interaction.response.defer()

  if positive == 0 and negative == 0 and neutral == 0:
    return await interaction.followup.send(
        "❌ You must specify at least one positive, negative, or neutral"
        " quantity to import.",
        ephemeral=True,
    )

  batch_comment = f"[Imported by {interaction.user.display_name}] {note}"
  vouches_data = []

  for _ in range(positive):
    vouches_data.append((1, batch_comment))
  for _ in range(negative):
    vouches_data.append((-1, batch_comment))
  for _ in range(neutral):
    vouches_data.append((0, batch_comment))

  try:
    total_added = await db.add_bulk_vouches(
        interaction.guild_id, user.id, interaction.user.id, vouches_data
    )
  except Exception:
    logger.exception("Bulk import failed execution")
    return await interaction.followup.send(
        "❌ Failed to process database insertion transaction.", ephemeral=True
    )

  embed = discord.Embed(
      title="📥 Import Completed",
      color=COLOR_INFO,
      description=f"Added **{total_added:,}** vouches to {user.mention}",
  )
  embed.set_thumbnail(url=user.display_avatar.url)
  embed.add_field(name="✅ Positive", value=f"{positive:,}", inline=True)
  embed.add_field(name="⚠️ Negative", value=f"{negative:,}", inline=True)
  embed.add_field(name="➖ Neutral", value=f"{neutral:,}", inline=True)
  embed.add_field(name="📝 Reference", value=note, inline=False)
  embed.set_footer(text=f"Import executed by {interaction.user.display_name} • {BOT_NAME}")
  embed.timestamp = discord.utils.utcnow()

  await interaction.followup.send(embed=embed)


@blacklist_group.command(
    name="add", description="[Staff] Block member from vouch system"
)
@app_commands.describe(user="User target", reason="Justification")
@app_commands.guild_only()
@is_staff()
async def blacklist_add(
    interaction: discord.Interaction, user: discord.Member, reason: str
):
  await interaction.response.defer()
  await db.add_blacklist(
      interaction.guild_id, user.id, reason, interaction.user.id
  )
  embed = discord.Embed(
      title="🚫 User Blacklisted",
      description=f"{user.mention} is now barred from interacting with vouches.",
      color=COLOR_NEGATIVE,
  )
  embed.add_field(name="Reason", value=reason, inline=True)
  embed.add_field(
      name="Moderator", value=interaction.user.mention, inline=True
  )
  embed.timestamp = discord.utils.utcnow()
  await interaction.followup.send(embed=embed)


@blacklist_group.command(
    name="remove", description="[Staff] Restore member vouch permissions"
)
@app_commands.describe(user="User target")
@app_commands.guild_only()
@is_staff()
async def blacklist_remove(
    interaction: discord.Interaction, user: discord.Member
):
  await interaction.response.defer()
  removed = await db.remove_blacklist(interaction.guild_id, user.id)
  msg = (
      f"✅ {user.mention} blacklist entry removed."
      if removed
      else f"ℹ️ {user.mention} was not on the blacklist."
  )
  await interaction.followup.send(msg)


# =====================================================================
# GLOBAL COMMANDS
# =====================================================================


@bot.tree.command(
    name="profile", description="Check total vouches and rating for a user"
)
@app_commands.describe(user="Select member (defaults to your profile)")
@app_commands.guild_only()
async def profile(
    interaction: discord.Interaction, user: discord.Member | None = None
):
  await interaction.response.defer()
  user = user or interaction.user
  stats = await db.get_stats(interaction.guild_id, user.id)
  latest = await db.get_latest_vouches(interaction.guild_id, user.id, limit=4)
  await interaction.followup.send(embed=profile_embed(user, stats, latest))


@bot.tree.command(
    name="leaderboard", description="Display highest rated sellers in server"
)
@app_commands.guild_only()
async def leaderboard(interaction: discord.Interaction):
  await interaction.response.defer()
  rows = await db.get_leaderboard(interaction.guild_id, limit=10)
  names = {}
  for row in rows:
    member = await resolve_member(interaction.guild, row["target_id"])
    names[row["target_id"]] = member.display_name if member else "Former Member"
  embed = leaderboard_embed(
      interaction.guild, rows, lambda uid: names.get(uid, f"<@{uid}>")
  )
  await interaction.followup.send(embed=embed)


@bot.tree.command(name="stats", description="Display total server vouch tallies")
@app_commands.guild_only()
async def stats(interaction: discord.Interaction):
  await interaction.response.defer()
  summary, blacklist_count = await db.get_guild_summary(interaction.guild_id)
  await interaction.followup.send(
      embed=stats_embed(interaction.guild, summary, blacklist_count)
  )


@bot.tree.command(
    name="setup", description="[Staff] Initialize vouch channels and permissions"
)
@app_commands.describe(
    vouch_channel="Channel for verified vouches",
    log_channel="Channel for internal audit and reports",
    admin_role="Role designated to manage vouchers",
)
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def setup_cmd(
    interaction: discord.Interaction,
    vouch_channel: discord.TextChannel,
    log_channel: discord.TextChannel,
    admin_role: discord.Role | None = None,
):
  await interaction.response.defer()
  await db.upsert_config(
      interaction.guild_id,
      vouch_channel_id=vouch_channel.id,
      log_channel_id=log_channel.id,
      admin_role_id=admin_role.id if admin_role else None,
  )
  embed = discord.Embed(
      title="⚙️ Configuration Saved", color=discord.Color.green()
  )
  embed.add_field(
      name="Vouch Feed Channel", value=vouch_channel.mention, inline=True
  )
  embed.add_field(
      name="Moderation Log", value=log_channel.mention, inline=True
  )
  embed.add_field(
      name="Manager Role",
      value=admin_role.mention if admin_role else "Administrators Only",
      inline=True,
  )
  await interaction.followup.send(embed=embed)


@bot.tree.command(name="help", description="Command registry and guide")
async def help_cmd(interaction: discord.Interaction):
  await interaction.response.send_message(
      embed=help_embed("home"), view=HelpView(), ephemeral=True
  )


# =====================================================================
# FAST PREFIX HANDLER (+vouch / -vouch)
# =====================================================================

MENTION_RE = re.compile(r"<@!?\d+>")


async def handle_prefix_vouch(message: discord.Message):
  content = message.content.strip()
  is_positive = content.lower().startswith("+vouch")
  vtype = 1 if is_positive else -1
  cmd_name = "+vouch" if is_positive else "-vouch"

  if not message.mentions:
    return await message.reply(
        f"❌ Missing target: `{cmd_name} @user [transaction details]`"
    )

  target = message.mentions[0]
  author = message.author
  guild = message.guild

  parts = content.split(maxsplit=1)
  raw_reason = parts[1] if len(parts) > 1 else ""
  reason = MENTION_RE.sub("", raw_reason).strip()

  if not reason:
    return await message.reply(
        f"❌ Please include a reason/proof. Example: `{cmd_name} @user Fast"
        " deal delivered!`"
    )

  result, error = await create_vouch(guild, author, target, vtype, reason)
  if error:
    return await message.reply(error)

  vouch_id, embed, view, config, target_channel = result
  send_target = target_channel or message.channel
  sent = await safe_send(send_target, embed=embed, view=view)

  if sent is None:
    return await message.reply("⚠️ Vouch saved, but unable to post into feed.")

  if send_target.id != message.channel.id:
    await message.reply(
        f"✅ Vouch `{vouch_id}` recorded in {send_target.mention}."
    )
  else:
    try:
      await message.add_reaction("✅")
    except discord.HTTPException:
      pass

  if config and config["log_channel_id"]:
    log_ch = guild.get_channel(config["log_channel_id"])
    if log_ch and log_ch.id != send_target.id:
      await safe_send(log_ch, embed=embed)


# =====================================================================
# EVENT LISTENERS
# =====================================================================


@bot.event
async def on_ready():
  logger.info("Bot authenticated as %s (UID: %s)", bot.user, bot.user.id)
  # Unified presence is configured once by the merged bot.


@bot.event
async def on_message(message: discord.Message):
  if message.author.bot or not message.guild:
    return
  content = message.content.strip().lower()
  if content.startswith("+vouch") or content.startswith("-vouch"):
    await handle_prefix_vouch(message)


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
):
  if isinstance(
      error, (app_commands.CheckFailure, app_commands.MissingPermissions)
  ):
    msg = str(error)
  else:
    logger.exception("Slash command execution failure", exc_info=error)
    msg = "❌ An unexpected error occurred while executing the command."

  try:
    if interaction.response.is_done():
      await interaction.followup.send(msg, ephemeral=True)
    else:
      await interaction.response.send_message(msg, ephemeral=True)
  except discord.HTTPException:
    pass


# =====================================================================
# MAIN RUNTIME
# =====================================================================


def main():
  if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN environment variable is undefined.")
  bot.run(TOKEN, log_handler=None)
