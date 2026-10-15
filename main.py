"""Unified Discord bot launcher for the four supplied bot projects.

The original feature handlers are kept in modules/ and attached to one shared
Discord client. Configure the deployment in .env before running this file.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# Resolve persistent paths against this project directory, not the shell's cwd.
def _normalise_path_env(name: str, default: str) -> Path:
    raw = os.getenv(name, default).strip() or default
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (BASE_DIR / path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.environ[name] = str(path)
    return path


DATA_DIR = Path(os.getenv("DATA_DIR", "data").strip() or "data").expanduser()
if not DATA_DIR.is_absolute():
    DATA_DIR = (BASE_DIR / DATA_DIR).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
os.environ["DATA_DIR"] = str(DATA_DIR)
_normalise_path_env("STATE_PATH", str(DATA_DIR / "events.json"))
_normalise_path_env("DB_PATH", str(DATA_DIR / "vouch_bot.db"))
_normalise_path_env("DATA_FILE", str(DATA_DIR / "metal_ai_data.json"))

import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("merged_bot")


def _int_env(name: str, default: int = 0) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw or default)
    except ValueError:
        logger.warning("Invalid %s=%r; using %s", name, raw, default)
        return default


PORT = _int_env("PORT", 8080)
GUILD_ID = _int_env("GUILD_ID", 0)
DISCORD_TOKEN = (os.getenv("DISCORD_TOKEN") or os.getenv("DISCORD_BOT_TOKEN") or "").strip()
BOT_NAME = os.getenv("BOT_NAME", "MalixAris Bot").strip() or "MalixAris Bot"
BOT_CREATOR_NAME = os.getenv("BOT_CREATOR_NAME", "abubakarmlikgul").strip() or "abubakarmlikgul"

# Import backup controls before assembling commands. Backups are triggered by
# successful state writes; restore is always an explicit administrator action.
from modules import backup_feature

# Import each source file as its own module so the original helper functions,
# state, views, and command callbacks retain separate Python namespaces.
from modules import apply_feature, events_feature, vouch_feature, ai_feature, ticket_feature

# Apply shared public branding to the AI module, whose source historically had
# its own display name and creator defaults.
ai_feature.BOT_NAME_PLAIN = BOT_NAME
ai_feature.BOT_NAME = BOT_NAME
ai_feature.BOT_CREATOR_NAME = BOT_CREATOR_NAME


# Use the union of the privileged intents required by the original bots.
intents = discord.Intents.default()
intents.guilds = True
intents.guild_messages = True
intents.message_content = True
intents.members = True
intents.presences = False


class MergedBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(
            command_prefix=commands.when_mentioned_or("!"),
            intents=intents,
            help_command=None,
            max_messages=max(50, _int_env("MESSAGE_CACHE_SIZE", 80)),
            member_cache_flags=discord.MemberCacheFlags.from_intents(intents),
            chunk_guild_at_startup=False,
        )
        self.health_runner: web.AppRunner | None = None

    async def setup_hook(self) -> None:
        # Bind the event-driven backup service before commands become available.
        backup_feature.bind_service(self)

        # Load each feature's local working state; Discord snapshots are never
        # imported here (backup restore is an explicit administrator action).
        events_feature.store.load()
        await vouch_feature.db.connect()

        # Restore persistent Discord UI views from the original bots.
        self.add_view(apply_feature.PanelView())
        self.add_view(apply_feature.ReviewView())
        self.add_view(apply_feature.CloseTicketView())
        self.add_view(events_feature.NumberControlView())
        self.add_view(events_feature.RaceControlView())
        self.add_dynamic_items(vouch_feature.ReportVouchButton)
        self.add_view(ticket_feature.TicketPanelView())
        self.add_view(ticket_feature.SupportTicketControls())

        await self._start_health_server()

        # One command tree and one sync. Set GUILD_ID in .env for fast testing;
        # leave it at 0 to publish the command tree globally.
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            logger.info("Synced %d merged root command groups to test guild %s", len(synced), GUILD_ID)
        else:
            synced = await self.tree.sync()
            logger.info("Synced %d merged root command groups globally", len(synced))

    async def _start_health_server(self) -> None:
        app = web.Application()

        async def health(_: web.Request) -> web.Response:
            return web.json_response({
                "status": "ok",
                "bot_ready": self.is_ready(),
                "bot_user": str(self.user) if self.user else None,
                "bot_name": BOT_NAME,
                "features": ["apply", "gtn", "vouch", "ai", "ticket", "backup"],
                "ai_api_key_configured": bool(ai_feature.AI_API_KEYS),
            })

        app.router.add_get("/", health)
        app.router.add_get("/health", health)
        self.health_runner = web.AppRunner(app, access_log=None)
        await self.health_runner.setup()
        site = web.TCPSite(self.health_runner, "0.0.0.0", PORT)
        await site.start()
        logger.info("Unified health endpoint listening on 0.0.0.0:%s", PORT)

    async def close(self) -> None:
        if backup_feature.service is not None:
            try:
                await backup_feature.service.close()
            except Exception:
                logger.exception("Could not stop the backup service cleanly")

        # Stop the AI background loops; Render pings are provided externally by cron.
        for loop in (
            ai_feature.cleanup,
            ai_feature.health_check,
            ai_feature.log_shipper,
        ):
            try:
                if loop.is_running():
                    loop.cancel()
            except Exception:
                logger.exception("Could not stop background loop %s", getattr(loop, "coro", loop))

        for task in list(apply_feature.interview_tasks.values()):
            if not task.done():
                task.cancel()

        try:
            events_feature.store.save()
        except Exception:
            logger.exception("Could not save event state during shutdown")
        try:
            await ai_feature.save_data()
        except Exception:
            logger.exception("Could not save AI state during shutdown")
        try:
            await vouch_feature.db.close()
        except Exception:
            logger.exception("Could not close vouch database cleanly")
        # Best-effort last snapshot after feature state and SQLite are flushed,
        # before the Discord HTTP client is closed on a graceful Render stop.
        if backup_feature.service is not None and self.is_ready():
            try:
                await asyncio.wait_for(
                    backup_feature.service.create_backup(force=False, reason="graceful shutdown"),
                    timeout=12,
                )
            except Exception:
                logger.exception("Could not upload a final graceful-shutdown snapshot")
        if self.health_runner is not None:
            try:
                await self.health_runner.cleanup()
            except Exception:
                logger.exception("Could not stop health server")
        await super().close()


bot = MergedBot()
ticket_feature.bot = bot


# ---------------------------------------------------------------------------
# Command-tree assembly
# ---------------------------------------------------------------------------
# The old command objects are collected from their isolated registration bots,
# then reparented under the new feature namespaces. Their original callback
# functions (and therefore business logic, checks, autocomplete, and options)
# are retained.


def _commands_from_old_tree(module) -> tuple[object, list[app_commands.Command | app_commands.Group]]:
    old_bot = module.bot
    registered = list(old_bot.tree.get_commands())
    module.bot = bot  # callbacks resolve this module global at execution time
    return old_bot, registered


old_bots: list[object] = []
apply_old, apply_commands = _commands_from_old_tree(apply_feature)
old_bots.append(apply_old)
events_old, events_commands = _commands_from_old_tree(events_feature)
old_bots.append(events_old)
vouch_old, vouch_commands = _commands_from_old_tree(vouch_feature)
old_bots.append(vouch_old)
ai_old, ai_commands = _commands_from_old_tree(ai_feature)
old_bots.append(ai_old)


apply_group = app_commands.Group(name="apply", description="Staff applications and application tickets", guild_only=True)
for command in apply_commands:
    if isinstance(command, app_commands.Group):
        raise RuntimeError(f"Unexpected nested application group: {command.name}")
    apply_group.add_command(command)


gtn_group = app_commands.Group(name="gtn", description="Guess-the-number and race events")
gtn_group.add_command(events_feature.number_group)
gtn_group.add_command(events_feature.race_group)
for command in events_commands:
    if not isinstance(command, app_commands.Group):
        gtn_group.add_command(command)


vouch_group = app_commands.Group(name="vouch", description="Vouches, profiles, and vouch administration", guild_only=True)
# The original /vouch group becomes this namespace, so its leaf commands stay
# /vouch add, /vouch view, etc. The existing blacklist subgroup remains nested.
for command in events_feature.number_group.commands:  # no-op validation of retained event group objects
    if command.name not in {"start", "status", "cancel", "reveal"}:
        raise RuntimeError(f"Unexpected number command: {command.name}")
for command in vouch_feature.vouch_group.commands:
    vouch_group.add_command(command)
vouch_group.add_command(vouch_feature.blacklist_group)
for command in vouch_commands:
    if not isinstance(command, app_commands.Group):
        vouch_group.add_command(command)


ai_group = app_commands.Group(name="ai", description="MalixAris Bot AI chat, server tools, and administration")
ai_server_group = app_commands.Group(name="server", description="AI server rules, channels, and ping settings")
ai_admin_group = app_commands.Group(name="admin", description="AI bot administration and operations")
AI_DIRECT = {
    "chat", "ask", "reset", "roast", "vibe", "mode", "whoami", "about",
    "describe", "testvision", "serverinfo", "summarize", "rules", "askrules",
    "notify", "pingsettings", "ping", "help", "stats", "persona",
    "resetpersona", "models", "dashboard",
}
AI_SERVER = {
    "setrules", "addrule", "removerule", "clearrules", "setaichannel",
    "removeaichannel", "listaichannels", "pingpolicy", "pingprotect",
    "pingunprotect", "setdailylimit", "setlogchannel", "removelogchannel",
}
AI_ADMIN = {
    "addowner", "removeowner", "owners", "clear", "addpremium", "removepremium",
    "blacklist", "unblacklist", "userinfo", "setmodel", "resetmodel",
    "diagnose", "apistatus", "maintenance", "broadcast",
}

seen_ai: set[str] = set()
for command in ai_commands:
    if isinstance(command, app_commands.Group):
        raise RuntimeError(f"Unexpected AI group before assembly: {command.name}")
    if command.name in AI_DIRECT:
        ai_group.add_command(command)
    elif command.name in AI_SERVER:
        ai_server_group.add_command(command)
    elif command.name in AI_ADMIN:
        ai_admin_group.add_command(command)
    else:
        raise RuntimeError(f"AI command has no namespace assignment: {command.name}")
    seen_ai.add(command.name)

if seen_ai != AI_DIRECT | AI_SERVER | AI_ADMIN:
    missing = sorted((AI_DIRECT | AI_SERVER | AI_ADMIN) - seen_ai)
    unexpected = sorted(seen_ai - (AI_DIRECT | AI_SERVER | AI_ADMIN))
    raise RuntimeError(f"AI command map mismatch; missing={missing}, unexpected={unexpected}")
ai_group.add_command(ai_server_group)
ai_group.add_command(ai_admin_group)
if len(ai_group.commands) > 25:
    raise RuntimeError("/ai exceeds Discord's 25 direct child option limit")

ticket_group = ticket_feature.ticket_group
backup_group = backup_feature.backup_group
for root in (apply_group, gtn_group, vouch_group, ai_group, ticket_group, backup_group):
    bot.tree.add_command(root)

# Registration bots are now unnecessary. The command callbacks and feature
# modules use the single shared `bot` object from here on.
old_bots.clear()
del apply_old, events_old, vouch_old, ai_old, old_bots
# Drop temporary registrations; the shared tree now owns every command.
del apply_commands, events_commands, vouch_commands, ai_commands, seen_ai


# ---------------------------------------------------------------------------
# Event listeners from the four source bots
# ---------------------------------------------------------------------------
# Use add_listener rather than assigning one on_message/on_ready function, so
# each original feature continues to receive Discord events.

for module, event_names in (
    (apply_feature, ("on_ready", "on_guild_channel_delete", "on_member_remove")),
    (events_feature, ("on_message",)),
    (vouch_feature, ("on_ready", "on_message")),
    (ai_feature, ("on_message", "on_guild_join", "on_guild_remove", "on_ready")),
):
    for event_name in event_names:
        listener = getattr(module, event_name, None)
        if listener is not None:
            bot.add_listener(listener, event_name)


async def _merged_presence() -> None:
    try:
        await bot.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name=f"{BOT_NAME} • /ai chat • /gtn • /vouch • /apply • /ticket • /backup",
            )
        )
    except discord.HTTPException:
        logger.exception("Could not set merged bot presence")
    if backup_feature.service is not None:
        try:
            await backup_feature.service.start()
        except Exception:
            logger.exception("Could not start the event-driven backup service")


bot.add_listener(_merged_presence, "on_ready")


# One error handler for all four command namespaces.
@bot.tree.error
async def merged_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    command = interaction.command
    command_name = getattr(command, "qualified_name", None) or getattr(command, "name", "unknown")
    logger.error("Application command /%s failed: %s", command_name, error, exc_info=error)

    if isinstance(error, (app_commands.MissingPermissions, app_commands.CheckFailure)):
        message = "You do not have permission to use that command."
    elif isinstance(error, app_commands.NoPrivateMessage):
        message = "This command can only be used inside a server."
    else:
        message = "Something went wrong while running that command. Please try again."

    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        logger.debug("Could not send slash-command error response", exc_info=True)


# Keep a module-level list for health/status checks and convenient testing.
ROOT_COMMANDS = tuple(command.name for command in bot.tree.get_commands())


def _check_configuration() -> None:
    if not DISCORD_TOKEN:
        raise SystemExit(
            "DISCORD_TOKEN is empty. Add your single merged bot token to the project's .env file."
        )
    if not ai_feature.AI_API_KEYS:
        logger.warning("OPENROUTER_API_KEY is empty; non-AI features will still run, but AI requests will not.")
    if not apply_feature.TICKET_CATEGORY_ID:
        logger.warning("TICKET_CATEGORY_ID is 0; configure staff application settings in .env before using /apply.")
    if not apply_feature.STAFF_ROLE_IDS:
        logger.warning("STAFF_ROLE_IDS is empty; configure a staff role before using application or support tickets.")
    if not _int_env("LOG_CHANNEL_ID", 0):
        logger.warning("LOG_CHANNEL_ID is 0; support-ticket transcripts will not have a configured destination.")
    if not _int_env("BACKUP_CHANNEL_ID", 0):
        logger.warning("BACKUP_CHANNEL_ID is 0; event-driven backups and manual restore are disabled.")


if __name__ == "__main__":
    _check_configuration()
    try:
        bot.run(DISCORD_TOKEN, log_handler=None)
    except discord.LoginFailure:
        logger.critical("Discord rejected DISCORD_TOKEN. Check the token in .env.")
        raise
    except discord.PrivilegedIntentsRequired:
        logger.critical(
            "Enable Server Members Intent and Message Content Intent in the Discord Developer Portal."
        )
        raise
