"""Separate support-ticket panel, distinct from the staff application panel.

Tickets use Discord channel topics as their source of truth, matching the
existing application-ticket approach. No account credentials are requested.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands, ui
from discord.ext import commands

logger = logging.getLogger("support_tickets")
bot: Optional[commands.Bot] = None

TICKET_TYPES = {
    "general": ("General", "General questions or community help", "💬"),
    "claim": ("Claim", "Questions about a claim", "📨"),
    "reward_claim": ("Reward Claim", "Help claiming a reward", "🎁"),
    "support": ("Support", "Technical or account-related support", "🛠️"),
    "partnership": ("Partnership", "Partnership and collaboration enquiries", "🤝"),
    "other": ("Other", "Anything that does not fit another category", "📌"),
}

# Redact token-shaped text in support transcripts rather than forwarding it to
# staff logs. This is a defensive shape match, not token validation.
_DISCORD_TOKEN_SHAPE = re.compile(
    r"(?i)(?:mfa\.[A-Za-z0-9_-]{70,}|[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_=-]{20,})"
)


def _redact_token_like_text(value: str) -> str:
    return _DISCORD_TOKEN_SHAPE.sub("[redacted token-like string]", value)


def _staff_role_ids() -> list[int]:
    raw = os.getenv("STAFF_ROLE_IDS", "")
    ids: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit() and int(part) > 0:
            ids.append(int(part))
    return ids


def _category_id() -> int:
    try:
        return int(os.getenv("TICKET_CATEGORY_ID", "0").strip() or "0")
    except ValueError:
        return 0


def _log_channel_id() -> int:
    try:
        return int(os.getenv("LOG_CHANNEL_ID", "0").strip() or "0")
    except ValueError:
        return 0


def _parse_ticket_topic(topic: Optional[str]) -> Optional[dict[str, str]]:
    if not topic or not topic.startswith("support-ticket|"):
        return None
    fields: dict[str, str] = {}
    for part in topic.split("|")[1:]:
        key, separator, value = part.partition(":")
        if separator:
            fields[key.strip()] = value.strip()
    if not fields.get("owner") or not fields.get("type"):
        return None
    return fields


def _slug(value: str) -> str:
    clean = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    return (clean or "member")[:48]


def _is_staff(member: discord.Member) -> bool:
    if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
        return True
    ids = set(_staff_role_ids())
    return any(role.id in ids for role in member.roles)


def _find_existing_ticket(guild: discord.Guild, owner_id: int, ticket_type: str):
    category = guild.get_channel(_category_id())
    if not isinstance(category, discord.CategoryChannel):
        return None
    for channel in category.channels:
        if not isinstance(channel, discord.TextChannel):
            continue
        info = _parse_ticket_topic(channel.topic)
        if (
            info
            and info.get("owner") == str(owner_id)
            and info.get("type") == ticket_type
            and info.get("status", "open") != "closed"
        ):
            return channel
    return None


async def _create_ticket(interaction: discord.Interaction, ticket_type: str) -> None:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "Support tickets can only be opened inside the server.", ephemeral=True
        )
        return
    if bot is None:
        await interaction.response.send_message(
            "The ticket feature is not ready yet. Please try again shortly.", ephemeral=True
        )
        return

    category = interaction.guild.get_channel(_category_id())
    if not isinstance(category, discord.CategoryChannel):
        await interaction.response.send_message(
            "The ticket category is not configured. Ask a server administrator to check `TICKET_CATEGORY_ID` in `.env`.",
            ephemeral=True,
        )
        return

    existing = _find_existing_ticket(interaction.guild, interaction.user.id, ticket_type)
    if existing is not None:
        await interaction.response.send_message(
            f"You already have an open {TICKET_TYPES[ticket_type][0]} ticket: {existing.mention}",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    label, description, emoji = TICKET_TYPES[ticket_type]
    base_name = f"ticket-{_slug(ticket_type)}-{_slug(interaction.user.name)}"
    name = base_name[:95]

    overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
        interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            embed_links=True,
        ),
    }
    bot_member = interaction.guild.me
    if bot_member is not None:
        overwrites[bot_member] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_channels=True,
            manage_messages=True,
            attach_files=True,
            embed_links=True,
        )
    staff_mentions: list[str] = []
    for role_id in _staff_role_ids():
        role = interaction.guild.get_role(role_id)
        if role is None:
            continue
        staff_mentions.append(role.mention)
        overwrites[role] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_messages=True,
            attach_files=True,
            embed_links=True,
        )

    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    topic = f"support-ticket|owner:{interaction.user.id}|type:{ticket_type}|created:{created_at}|status:open"
    try:
        channel = await interaction.guild.create_text_channel(
            name=name,
            category=category,
            overwrites=overwrites,
            topic=topic,
            reason=f"{label} ticket opened by {interaction.user} ({interaction.user.id})",
        )
    except (discord.Forbidden, discord.HTTPException):
        logger.exception("Failed to create %s ticket for user %s", ticket_type, interaction.user.id)
        await interaction.followup.send(
            "I couldn't create the ticket. Please check the bot's channel permissions and try again.",
            ephemeral=True,
        )
        return

    brand = os.getenv("BOT_NAME", "MalixAris Bot").strip() or "MalixAris Bot"
    creator = os.getenv("BOT_CREATOR_NAME", "abubakarmlikgul").strip() or "abubakarmlikgul"
    embed = discord.Embed(
        title=f"{emoji} {label} Ticket",
        description=(
            f"Welcome, {interaction.user.mention}. Please describe what you need help with. "
            "A staff member will reply here.\n\n"
            "Do not post passwords, account tokens, or other login credentials."
        ),
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Category", value=label, inline=True)
    embed.add_field(name="Opened by", value=interaction.user.mention, inline=True)
    embed.set_footer(text=f"{brand} • made by {creator}")
    try:
        await channel.send(
            content=" ".join([interaction.user.mention, *staff_mentions]),
            embed=embed,
            view=SupportTicketControls(),
            allowed_mentions=discord.AllowedMentions(users=True, roles=True),
        )
    except discord.HTTPException:
        logger.exception("Ticket channel %s was created but its welcome message failed", channel.id)
        await interaction.followup.send(
            f"Your ticket was created: {channel.mention}. The welcome message could not be posted; please send your request there.",
            ephemeral=True,
        )
        return

    await interaction.followup.send(f"Your ticket is ready: {channel.mention}", ephemeral=True)


class TicketTypeSelect(ui.Select):
    def __init__(self) -> None:
        options = [
            discord.SelectOption(label=label, value=key, description=description, emoji=emoji)
            for key, (label, description, emoji) in TICKET_TYPES.items()
        ]
        super().__init__(
            placeholder="Choose a ticket category…",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="malixaris:support_ticket:type",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        ticket_type = self.values[0]
        if ticket_type not in TICKET_TYPES:
            await interaction.response.send_message("That ticket category is invalid.", ephemeral=True)
            return
        await _create_ticket(interaction, ticket_type)


class TicketPanelView(ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(TicketTypeSelect())


async def _build_transcript(channel: discord.TextChannel) -> bytes:
    lines = [
        f"Support ticket transcript: #{channel.name}",
        f"Channel ID: {channel.id}",
        f"Topic: {channel.topic or '(none)'}",
        "=" * 72,
    ]
    async for message in channel.history(limit=300, oldest_first=True):
        stamp = message.created_at.replace(tzinfo=timezone.utc).isoformat(timespec="seconds")
        content = (message.content or "").replace("\r", "").strip()
        if message.embeds:
            embed_bits = [e.title or "" for e in message.embeds if e.title]
            if embed_bits:
                content = (content + " " + " | ".join(embed_bits)).strip()
        attachment_names = ", ".join(a.filename for a in message.attachments)
        if attachment_names:
            content = (content + "\nAttachments (files not linked): " + attachment_names).strip()
        content = _redact_token_like_text(content)
        lines.append(f"[{stamp}] {message.author} ({message.author.id}): {content or '[no text]'}")
    return "\n".join(lines).encode("utf-8", errors="replace")


class SupportTicketControls(ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @ui.button(
        label="Close ticket",
        style=discord.ButtonStyle.danger,
        emoji="🔒",
        custom_id="malixaris:support_ticket:close",
    )
    async def close_ticket(self, interaction: discord.Interaction, button: ui.Button) -> None:
        if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("This ticket channel is not available.", ephemeral=True)
            return
        info = _parse_ticket_topic(interaction.channel.topic)
        if info is None:
            await interaction.response.send_message("This is not an active support ticket.", ephemeral=True)
            return

        member = interaction.user
        owner_ok = str(member.id) == info.get("owner")
        staff_ok = isinstance(member, discord.Member) and _is_staff(member)
        if not (owner_ok or staff_ok):
            await interaction.response.send_message(
                "Only the ticket opener or staff can close this ticket.", ephemeral=True
            )
            return

        channel = interaction.channel
        log_id = _log_channel_id()
        log_channel = interaction.guild.get_channel(log_id) if log_id else None
        if not isinstance(log_channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message(
                "The transcript log channel is not configured or could not be found. This ticket was left open; ask staff to configure `LOG_CHANNEL_ID`.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            transcript = await _build_transcript(channel)
            category = TICKET_TYPES.get(info.get("type", "other"), TICKET_TYPES["other"])[0]
            upload = discord.File(
                io.BytesIO(transcript),
                filename=f"ticket-{_slug(category)}-{channel.id}.txt",
            )
            await log_channel.send(
                content=f"Transcript • {category} • opened by <@{info['owner']}> • closed by {interaction.user.mention} • channel `{channel.id}`",
                file=upload,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.HTTPException, OSError):
            logger.exception("Could not log transcript for support ticket %s", channel.id)
            await channel.send("I couldn't save the transcript, so I left this ticket open. Please ask staff to check the log-channel permissions.")
            await interaction.followup.send("Transcript upload failed; this ticket remains open.", ephemeral=True)
            return

        button.disabled = True
        if interaction.message is not None:
            try:
                await interaction.message.edit(view=self)
            except discord.HTTPException:
                logger.warning("Could not disable the close button on ticket %s", channel.id)
        try:
            await channel.edit(topic=f"{channel.topic}|status:closed", reason="Support ticket transcript saved")
        except discord.HTTPException:
            logger.warning("Could not mark support ticket %s closed", channel.id)
        await channel.send(f"Ticket closed by {interaction.user.mention}. Transcript saved; this channel will be removed shortly.")
        await interaction.followup.send("Transcript saved. Closing the ticket…", ephemeral=True)
        await asyncio.sleep(3)
        try:
            await channel.delete(reason=f"Support ticket closed by {interaction.user} ({interaction.user.id})")
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Transcript saved but could not delete support ticket %s", channel.id)


class TicketPanelCommandGroup(app_commands.Group):
    def __init__(self) -> None:
        super().__init__(name="ticket", description="Support-ticket panel and ticket tools", guild_only=True)

    @app_commands.command(name="panel", description="Post the support-ticket dropdown panel")
    @app_commands.checks.has_permissions(administrator=True)
    async def panel(self, interaction: discord.Interaction) -> None:
        brand = os.getenv("BOT_NAME", "MalixAris Bot").strip() or "MalixAris Bot"
        creator = os.getenv("BOT_CREATOR_NAME", "abubakarmlikgul").strip() or "abubakarmlikgul"
        embed = discord.Embed(
            title=f"{brand} • Support Centre",
            description=(
                "Choose the option that best matches your request. A private ticket will be opened "
                "for you and the staff team. Please do not include passwords or account tokens."
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Available categories",
            value="💬 General • 📨 Claim • 🎁 Reward Claim • 🛠️ Support • 🤝 Partnership • 📌 Other",
            inline=False,
        )
        embed.set_footer(text=f"Made by {creator}")
        await interaction.response.defer(ephemeral=True, thinking=True)
        if interaction.channel is None:
            await interaction.followup.send("I couldn't find this channel.", ephemeral=True)
            return
        try:
            await interaction.channel.send(embed=embed, view=TicketPanelView())
        except discord.HTTPException:
            logger.exception("Could not post support-ticket panel")
            await interaction.followup.send("I couldn't post the panel here. Check my channel permissions.", ephemeral=True)
            return
        await interaction.followup.send("Support-ticket dropdown posted.", ephemeral=True)


ticket_group = TicketPanelCommandGroup()
