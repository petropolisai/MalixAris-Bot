"""Safe Render state backups stored as bot-authored Discord channel attachments.

Only allow-listed runtime state files and non-secret settings are archived.
The .env file, Discord bot token, OpenRouter/API keys, and source code are
never included.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import sqlite3
import tempfile
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Optional

import discord
from discord import app_commands, ui
from discord.ext import commands

logger = logging.getLogger("render_backups")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FORMAT_NAME = "malixaris-bot-state-backup"
FORMAT_VERSION = 1
BACKUP_PREFIX = "malixaris-backup-"
MAX_ARCHIVE_BYTES = 24 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_HISTORY_SCAN = 500
BACKUP_RETENTION_COUNT = 25
BACKUP_DEBOUNCE_SECONDS = 8
_TOKEN_SHAPE = re.compile(
    r"(?i)(?:mfa\.[A-Za-z0-9_-]{70,}|[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_=-]{20,})"
)

# Explicit allow-list: adding a new key requires reviewing it for secrets.
# Credential variables, .env paths, and upload URLs are deliberately excluded.
SAFE_SETTINGS_KEYS = (
    "BOT_NAME",
    "BOT_CREATOR_NAME",
    "BOT_CREATOR_ID",
    "BOT_OWNER_IDS",
    "SERVER_NAME",
    "TICKET_CATEGORY_ID",
    "STAFF_ROLE_IDS",
    "LOG_CHANNEL_ID",
    "ACCEPTED_ROLE_ID",
    "ANSWER_TIMEOUT_MINUTES",
    "DEFAULT_PING_POLICY",
    "AI_MODEL_NAME",
    "VISION_MODEL_NAME",
    "API_KEY_COOLDOWN_SECS",
    "API_TIMEOUT_SECS",
    "MAX_OUTPUT_TOKENS",
    "THINKING_BUDGET",
    "MAX_CONTEXT_MESSAGES",
    "FREE_TIER_DAILY_LIMIT",
    "MAX_MEDIA_ATTACHMENTS",
    "MAX_MEDIA_SIZE_MB",
    "MAX_CONCURRENT_AI_CALLS",
    "MAX_CONCURRENT_MEDIA",
    "QUOTA_UTC_OFFSET_HOURS",
    "MESSAGE_CACHE_SIZE",
    "MAX_CONVERSATIONS",
    "LOG_LEVEL",
)
_SAFE_SETTINGS = frozenset(SAFE_SETTINGS_KEYS)


@dataclass(frozen=True)
class BackupArtifact:
    data: bytes
    filename: str
    fingerprint: str
    created_at: str
    files: tuple[str, ...]
    settings_count: int


def _env_int(name: str, default: int = 0) -> int:
    try:
        return int(os.getenv(name, str(default)).strip() or str(default))
    except (TypeError, ValueError):
        return default


def _backup_channel_id() -> int:
    return _env_int("BACKUP_CHANNEL_ID", 0)


def _resolve_path(raw: str, fallback: str) -> Path:
    value = Path((raw or fallback).strip() or fallback).expanduser()
    if not value.is_absolute():
        value = (PROJECT_ROOT / value).resolve()
    return value.resolve()


def _data_root() -> Path:
    return _resolve_path(os.getenv("DATA_DIR", "data"), "data")


def _configured_data_paths() -> dict[str, tuple[Path, str]]:
    """Map safe paths under DATA_DIR to their data kind; never archive arbitrary files."""
    root = _data_root()
    raw_paths = (
        ("STATE_PATH", "data/events.json", "json"),
        ("DB_PATH", "data/vouch_bot.db", "sqlite"),
        ("DATA_FILE", "data/metal_ai_data.json", "json"),
    )
    result: dict[str, tuple[Path, str]] = {}
    for env_name, fallback, kind in raw_paths:
        path = _resolve_path(os.getenv(env_name, fallback), fallback)
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            logger.warning("Skipping %s outside DATA_DIR; backups only include files under DATA_DIR", env_name)
            continue
        if relative in result:
            logger.warning("Skipping duplicate backup data path %s", relative)
            continue
        result[relative] = (path, kind)
    return result


def _safe_settings() -> dict[str, str]:
    result: dict[str, str] = {}
    for key in SAFE_SETTINGS_KEYS:
        value = os.getenv(key, "").strip()
        if not value or value.lower().startswith(("your_", "replace_me", "changeme")):
            continue
        value = _TOKEN_SHAPE.sub("[redacted token-like string]", value)
        if len(value) <= 4096 and "\x00" not in value:
            result[key] = value
    return result


def _sqlite_snapshot(path: Path) -> bytes:
    """Use SQLite's online backup API so committed WAL data is included safely."""
    with tempfile.TemporaryDirectory(prefix="malixaris-sqlite-backup-") as temp_dir:
        destination = Path(temp_dir) / "snapshot.sqlite3"
        source_conn = sqlite3.connect(str(path), timeout=10)
        destination_conn = sqlite3.connect(str(destination), timeout=10)
        try:
            source_conn.backup(destination_conn)
            _redact_sqlite_text(destination_conn)
            check = destination_conn.execute("PRAGMA quick_check").fetchone()
            if not check or check[0] != "ok":
                raise ValueError(f"SQLite quick_check failed for {path.name}")
        finally:
            destination_conn.close()
            source_conn.close()
        return destination.read_bytes()


def _json_bytes_are_valid(data: bytes, name: str) -> bool:
    try:
        json.loads(data.decode("utf-8"))
        return True
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"JSON state file {name} is not valid JSON") from exc


def _redact_json_value(value: Any) -> Any:
    if isinstance(value, str):
        return _TOKEN_SHAPE.sub("[redacted token-like string]", value)
    if isinstance(value, list):
        return [_redact_json_value(item) for item in value]
    if isinstance(value, dict):
        return {
            (_TOKEN_SHAPE.sub("[redacted key]", key) if isinstance(key, str) else key): _redact_json_value(item)
            for key, item in value.items()
        }
    return value


def _redact_sqlite_text(connection: sqlite3.Connection) -> None:
    """Redact common token-shaped strings from text columns in the backup copy."""
    tables = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()

    def quote(identifier: str) -> str:
        return '"' + identifier.replace('"', '""') + '"'

    for (table_name,) in tables:
        table_sql = quote(str(table_name))
        for column in connection.execute(f"PRAGMA table_info({table_sql})").fetchall():
            column_name = str(column[1])
            declared_type = str(column[2] or "").upper()
            if not any(tag in declared_type for tag in ("TEXT", "CHAR", "CLOB")):
                continue
            column_sql = quote(column_name)
            last_row_id: int | None = None
            while True:
                try:
                    if last_row_id is None:
                        query = (
                            f"SELECT rowid, {column_sql} FROM {table_sql} "
                            f"WHERE {column_sql} IS NOT NULL ORDER BY rowid LIMIT 500"
                        )
                        rows = connection.execute(query).fetchall()
                    else:
                        query = (
                            f"SELECT rowid, {column_sql} FROM {table_sql} "
                            f"WHERE rowid>? AND {column_sql} IS NOT NULL ORDER BY rowid LIMIT 500"
                        )
                        rows = connection.execute(query, (last_row_id,)).fetchall()
                except sqlite3.Error:
                    break
                if not rows:
                    break
                updates: list[tuple[str, int]] = []
                for row_id, value in rows:
                    last_row_id = int(row_id)
                    if isinstance(value, str):
                        cleaned = _TOKEN_SHAPE.sub("[redacted token-like string]", value)
                        if cleaned != value:
                            updates.append((cleaned, int(row_id)))
                if updates:
                    connection.executemany(
                        f"UPDATE {table_sql} SET {column_sql}=? WHERE rowid=?",
                        updates,
                    )
    connection.commit()


def _fingerprint(file_hashes: dict[str, str], settings: dict[str, str]) -> str:
    body = json.dumps(
        {"files": file_hashes, "settings": settings},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def build_backup_artifact() -> BackupArtifact:
    """Build a compressed archive containing only approved runtime data."""
    root = _data_root()
    root.mkdir(parents=True, exist_ok=True)
    included: dict[str, bytes] = {}
    file_hashes: dict[str, str] = {}

    for relative, (path, kind) in _configured_data_paths().items():
        if not path.is_file() or path.stat().st_size == 0:
            continue
        if kind == "sqlite":
            contents = _sqlite_snapshot(path)
        else:
            contents = path.read_bytes()
            _json_bytes_are_valid(contents, relative)
            parsed = json.loads(contents.decode("utf-8"))
            contents = json.dumps(
                _redact_json_value(parsed),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        archive_name = f"data/{relative}"
        included[archive_name] = contents
        file_hashes[archive_name] = hashlib.sha256(contents).hexdigest()

    settings = _safe_settings()
    fingerprint = _fingerprint(file_hashes, settings)
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    manifest = {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "created_at": created_at,
        "files": {
            name: {"sha256": file_hashes[name], "size": len(contents)}
            for name, contents in sorted(included.items())
        },
        "settings_sha256": hashlib.sha256(
            json.dumps(settings, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "fingerprint": fingerprint,
    }

    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, indent=2).encode("utf-8"))
        archive.writestr("settings.json", json.dumps(settings, indent=2).encode("utf-8"))
        for name, contents in sorted(included.items()):
            archive.writestr(name, contents)
    data = archive_buffer.getvalue()
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ValueError(
            f"Compressed backup is {len(data) // (1024 * 1024)} MiB, above the safety limit."
        )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return BackupArtifact(
        data=data,
        filename=f"{BACKUP_PREFIX}{stamp}.zip",
        fingerprint=fingerprint,
        created_at=created_at,
        files=tuple(sorted(included)),
        settings_count=len(settings),
    )


def _validate_sqlite_bytes(data: bytes, name: str) -> None:
    with tempfile.NamedTemporaryFile(prefix="malixaris-restore-check-", suffix=".sqlite3") as temp:
        temp.write(data)
        temp.flush()
        connection = sqlite3.connect(temp.name, timeout=5)
        try:
            row = connection.execute("PRAGMA quick_check").fetchone()
            if not row or row[0] != "ok":
                raise ValueError(f"SQLite backup {name} failed its integrity check")
        finally:
            connection.close()


def _is_usable_local_file(path: Path, kind: str) -> bool:
    if not path.is_file() or path.stat().st_size == 0:
        return False
    try:
        if kind == "sqlite":
            _sqlite_snapshot(path)
        else:
            _json_bytes_are_valid(path.read_bytes(), path.name)
        return True
    except (OSError, sqlite3.Error, ValueError):
        return False


def _atomic_write(path: Path, contents: bytes, kind: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "sqlite":
        for suffix in ("-wal", "-shm"):
            try:
                Path(str(path) + suffix).unlink()
            except FileNotFoundError:
                pass
    with tempfile.NamedTemporaryFile(prefix=f".{path.name}.", suffix=".restore", dir=path.parent, delete=False) as temp:
        temp.write(contents)
        temp.flush()
        os.fsync(temp.fileno())
        temporary = Path(temp.name)
    os.replace(temporary, path)


def _apply_settings(settings: dict[str, str], force: bool) -> list[str]:
    applied: list[str] = []
    for key, value in settings.items():
        if key not in _SAFE_SETTINGS or not isinstance(value, str) or len(value) > 4096:
            continue
        current = os.getenv(key, "").strip()
        if force or not current:
            os.environ[key] = value
            applied.append(key)
    return applied


def restore_backup_archive(
    archive_bytes: bytes,
    *,
    missing_only: bool = True,
    force_settings: bool = False,
) -> dict[str, Any]:
    """Validate and restore a backup without ever extracting arbitrary ZIP paths."""
    if len(archive_bytes) > MAX_ARCHIVE_BYTES:
        raise ValueError("Backup attachment is larger than the allowed limit.")
    configured = _configured_data_paths()
    allowed_archive_paths = {f"data/{name}": (path, kind) for name, (path, kind) in configured.items()}
    data_by_name: dict[str, bytes] = {}

    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes), "r") as archive:
            names = archive.namelist()
            if len(names) > 16 or len(set(names)) != len(names):
                raise ValueError("Backup has an unexpected number of entries.")
            if "manifest.json" not in names or "settings.json" not in names:
                raise ValueError("Backup is missing its manifest or settings file.")
            total_size = sum(item.file_size for item in archive.infolist())
            if total_size > MAX_UNCOMPRESSED_BYTES:
                raise ValueError("Backup expands beyond the allowed safety limit.")
            for name in names:
                pure = PurePosixPath(name)
                if pure.is_absolute() or ".." in pure.parts or "\\" in name:
                    raise ValueError("Backup contains an unsafe file path.")
                if name not in {"manifest.json", "settings.json"} and name not in allowed_archive_paths:
                    raise ValueError(f"Backup contains an unrecognized file: {name}")
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            settings = json.loads(archive.read("settings.json").decode("utf-8"))
            if not isinstance(manifest, dict) or manifest.get("format") != FORMAT_NAME or manifest.get("version") != FORMAT_VERSION:
                raise ValueError("Backup format/version is not supported.")
            if not isinstance(settings, dict) or any(key not in _SAFE_SETTINGS for key in settings):
                raise ValueError("Backup settings did not pass the allow-list check.")
            if any(not isinstance(value, str) or len(value) > 4096 for value in settings.values()):
                raise ValueError("Backup contains malformed settings.")
            expected_settings_hash = hashlib.sha256(
                json.dumps(settings, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            if manifest.get("settings_sha256") != expected_settings_hash:
                raise ValueError("Backup settings checksum does not match.")
            file_entries = manifest.get("files")
            if not isinstance(file_entries, dict):
                raise ValueError("Backup file manifest is invalid.")
            expected_names = set(file_entries)
            actual_data_names = set(names) - {"manifest.json", "settings.json"}
            if expected_names != actual_data_names:
                raise ValueError("Backup files do not match the manifest.")
            file_hashes: dict[str, str] = {}
            for name in sorted(expected_names):
                entry = file_entries[name]
                if not isinstance(entry, dict) or not isinstance(entry.get("sha256"), str):
                    raise ValueError(f"Backup checksum entry is invalid for {name}.")
                contents = archive.read(name)
                if len(contents) != entry.get("size"):
                    raise ValueError(f"Backup size does not match for {name}.")
                digest = hashlib.sha256(contents).hexdigest()
                if digest != entry["sha256"]:
                    raise ValueError(f"Backup checksum failed for {name}.")
                path, kind = allowed_archive_paths[name]
                if kind == "json":
                    _json_bytes_are_valid(contents, name)
                else:
                    _validate_sqlite_bytes(contents, name)
                data_by_name[name] = contents
                file_hashes[name] = digest
            fingerprint = _fingerprint(file_hashes, settings)
            if manifest.get("fingerprint") != fingerprint:
                raise ValueError("Backup fingerprint does not match.")
            created_at = str(manifest.get("created_at", "unknown"))
    except (zipfile.BadZipFile, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Backup ZIP/JSON could not be read.") from exc

    restored: list[str] = []
    skipped: list[str] = []
    for archive_name, contents in data_by_name.items():
        path, kind = allowed_archive_paths[archive_name]
        if missing_only and _is_usable_local_file(path, kind):
            skipped.append(archive_name)
            continue
        _atomic_write(path, contents, kind)
        restored.append(archive_name)

    applied_settings = _apply_settings(settings, force=force_settings)
    return {
        "created_at": created_at,
        "fingerprint": fingerprint,
        "restored_files": restored,
        "skipped_files": skipped,
        "settings_applied": applied_settings,
        "settings_count": len(settings),
    }


async def _get_channel(bot: commands.Bot) -> discord.TextChannel | discord.Thread | None:
    channel_id = _backup_channel_id()
    if channel_id <= 0:
        return None
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            logger.exception("Could not fetch backup channel %s", channel_id)
            return None
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        logger.error("BACKUP_CHANNEL_ID %s is not a text channel or thread", channel_id)
        return None
    return channel


def _is_backup_message(message: discord.Message, bot_id: int) -> bool:
    return message.author.id == bot_id and any(
        item.filename.startswith(BACKUP_PREFIX) and item.filename.endswith(".zip")
        for item in message.attachments
    )


class BackupSelect(ui.Select):
    def __init__(self, messages: list[discord.Message], service: "BackupService") -> None:
        self.service = service
        options: list[discord.SelectOption] = []
        for message in messages[:25]:
            attachment = next(
                item for item in message.attachments
                if item.filename.startswith(BACKUP_PREFIX) and item.filename.endswith(".zip")
            )
            stamp = message.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            label = f"{stamp} • {attachment.filename}"
            options.append(discord.SelectOption(
                label=label[:100],
                value=str(message.id),
                description=f"{attachment.size // 1024} KiB • click to restore this snapshot"[:100],
            ))
        super().__init__(
            placeholder="Select a backup snapshot to restore…",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="malixaris:backup:restore-select",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Only server administrators can restore backups.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await self.service.restore_message(int(self.values[0]))
            restored = ", ".join(result.get("restored_files", [])) or "no files needed replacement"
            promotion_error = result.get("promotion_error")
            promotion_note = (
                f" Latest backup now: `{result['promoted_filename']}`."
                if result.get("promoted_filename")
                else f" Warning: latest-backup promotion failed ({promotion_error}); the selection is not yet saved as the newest channel backup."
            )
            await interaction.followup.send(
                "✅ Restore finished for the running bot. "
                f"Files restored: {restored}. Missing non-secret config values filled: "
                f"{len(result.get('settings_applied', []))}; existing Render environment values were preserved."
                f"{promotion_note} Secrets were not part of the archive.",
                ephemeral=True,
            )
        except Exception as exc:
            logger.exception("Manual backup restore failed")
            await interaction.followup.send(f"❌ Restore failed safely: {exc}", ephemeral=True)
        if self.view is not None:
            self.view.stop()


class BackupSelectView(ui.View):
    def __init__(self, messages: list[discord.Message], service: "BackupService") -> None:
        super().__init__(timeout=180)
        self.add_item(BackupSelect(messages, service))


class BackupService:
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.started = False
        self._closing = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._change_task: asyncio.Task | None = None
        self._change_generation = 0
        self._pending_change_reason: str | None = None
        self.last_fingerprint: str | None = None
        self.last_backup_at: datetime | None = None
        self.last_backup_filename: str | None = None
        self.startup_summary = "not started"
        self._lock = asyncio.Lock()
        self._error_notice_at = 0.0

    async def start(self) -> None:
        if self.started:
            return
        channel = await _get_channel(self.bot)
        if channel is None:
            logger.warning("Backups are not running; set BACKUP_CHANNEL_ID and check channel permissions.")
            return
        try:
            previous = await self._backup_messages(channel)
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Cannot read backup-channel history; grant Read Message History to the bot")
            return

        self._loop = asyncio.get_running_loop()
        self.started = True
        pending_change_reason = self._pending_change_reason
        self._pending_change_reason = None
        if previous:
            latest = previous[0]
            attachment = next(
                (item for item in latest.attachments
                 if item.filename.startswith(BACKUP_PREFIX) and item.filename.endswith(".zip")),
                None,
            )
            self.last_backup_filename = attachment.filename if attachment else None
            self.last_backup_at = latest.created_at
            self.startup_summary = (
                f"Found {len(previous)} saved snapshot(s). Nothing was restored automatically; "
                "admins can select a file with `/backup restore`."
            )
            # Seed the event detector from the current local state so an
            # unchanged shutdown does not create a duplicate snapshot. If a
            # write was reported before startup completed, leave the baseline
            # unset so the queued event produces a backup.
            if pending_change_reason is None:
                try:
                    current = await asyncio.to_thread(build_backup_artifact)
                    self.last_fingerprint = current.fingerprint
                except Exception:
                    logger.exception("Could not fingerprint current state at backup-service startup")
        else:
            try:
                await self.create_backup(force=True, reason="initial baseline")
                self.startup_summary = "No prior snapshots existed, so an initial backup file was created."
            except Exception as exc:
                self.startup_summary = f"No prior snapshots existed; the initial upload failed ({type(exc).__name__})."
                logger.exception("Could not create the initial backup snapshot")

        try:
            await channel.send(
                f"🟢 **{os.getenv('BOT_NAME', 'MalixAris Bot')} backup service online.**\n"
                f"{self.startup_summary}\n"
                f"Backups are event-driven after saved state changes (coalesced for {BACKUP_DEBOUNCE_SECONDS}s); "
                f"the latest {BACKUP_RETENTION_COUNT} files are retained.\n"
                "Restore is manual with `/backup restore`; `.env` and credentials are never included.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Could not post backup startup status")
        logger.info("Event-driven backup service started; no periodic state polling or automatic restore is enabled")
        if pending_change_reason:
            self.notify_state_changed(pending_change_reason)

    async def close(self) -> None:
        self._closing = True
        self.started = False
        if self._change_task and not self._change_task.done():
            self._change_task.cancel()
            try:
                await self._change_task
            except asyncio.CancelledError:
                pass

    def notify_state_changed(self, reason: str = "saved state changed") -> None:
        if self._closing:
            return
        if not self.started:
            self._pending_change_reason = self._pending_change_reason or reason
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            if self._loop is not None and self._loop.is_running():
                self._loop.call_soon_threadsafe(self.notify_state_changed, reason)
            return
        self._change_generation += 1
        if self._change_task is None or self._change_task.done():
            self._change_task = loop.create_task(
                self._debounced_backup(reason),
                name="event-driven-discord-backup",
            )

    async def _debounced_backup(self, reason: str) -> None:
        processed_generation = -1
        try:
            while self.started and not self._closing:
                generation = self._change_generation
                await asyncio.sleep(BACKUP_DEBOUNCE_SECONDS)
                if generation != self._change_generation:
                    continue
                processed_generation = generation
                await self.create_backup(force=False, reason=reason)
                if generation == self._change_generation:
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Event-triggered backup failed")
            await self._notice_error_once(str(exc))
        finally:
            self._change_task = None
            if (
                self.started
                and not self._closing
                and self._change_generation != processed_generation
            ):
                loop = asyncio.get_running_loop()
                self._change_task = loop.create_task(
                    self._debounced_backup("another state change occurred during backup"),
                    name="event-driven-discord-backup",
                )

    async def _notice_error_once(self, detail: str) -> None:
        now = time.monotonic()
        if now - self._error_notice_at < 1800:
            return
        self._error_notice_at = now
        channel = await _get_channel(self.bot)
        if channel is None:
            return
        try:
            await channel.send(
                f"⚠️ Event-triggered backup failed: `{detail[:500]}`. Check Render logs and backup-channel permissions.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logger.exception("Could not post event-driven backup error notice")

    async def _backup_messages(self, channel: discord.TextChannel | discord.Thread) -> list[discord.Message]:
        if self.bot.user is None:
            return []
        messages: list[discord.Message] = []
        async for message in channel.history(limit=MAX_HISTORY_SCAN):
            if _is_backup_message(message, self.bot.user.id):
                messages.append(message)
        messages.sort(key=lambda item: item.created_at, reverse=True)
        return messages

    async def _prune_old_backups(self, channel: discord.TextChannel | discord.Thread) -> None:
        messages = await self._backup_messages(channel)
        for old_message in messages[BACKUP_RETENTION_COUNT:]:
            try:
                await old_message.delete(reason="Rotate old automatic state backup")
            except (discord.Forbidden, discord.HTTPException):
                logger.warning("Could not prune old backup message %s", old_message.id)

    async def create_backup(self, *, force: bool, reason: str) -> dict[str, Any]:
        async with self._lock:
            artifact = await asyncio.to_thread(build_backup_artifact)
            if not force and artifact.fingerprint == self.last_fingerprint:
                return {"created": False, "artifact": artifact}
            channel = await _get_channel(self.bot)
            if channel is None:
                raise RuntimeError("Backup channel is unavailable; verify BACKUP_CHANNEL_ID.")
            file_limit = getattr(channel.guild, "filesize_limit", MAX_ARCHIVE_BYTES)
            if len(artifact.data) > min(MAX_ARCHIVE_BYTES, int(file_limit)):
                raise ValueError(
                    f"Backup ZIP is {len(artifact.data) // 1024} KiB, but the channel upload limit is {int(file_limit) // 1024} KiB."
                )
            description = ", ".join(artifact.files) or "no local state files yet"
            message = await channel.send(
                content=(
                    f"📦 **MalixAris Bot state backup** • `{artifact.created_at}`\n"
                    f"Reason: {reason}. Included: `{description}`. "
                    f"Saved non-secret setting values: {artifact.settings_count}."
                ),
                file=discord.File(io.BytesIO(artifact.data), filename=artifact.filename),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            self.last_fingerprint = artifact.fingerprint
            self.last_backup_at = datetime.now(timezone.utc)
            self.last_backup_filename = artifact.filename
            try:
                await self._prune_old_backups(channel)
            except (discord.Forbidden, discord.HTTPException):
                logger.warning("Backup uploaded, but old snapshots could not be pruned")
            return {"created": True, "artifact": artifact, "message": message}

    async def list_backups(self) -> list[discord.Message]:
        channel = await _get_channel(self.bot)
        if channel is None:
            raise RuntimeError("Backup channel is unavailable; verify BACKUP_CHANNEL_ID.")
        return await self._backup_messages(channel)

    async def restore_message(self, message_id: int) -> dict[str, Any]:
        messages = await self.list_backups()
        selected = next((item for item in messages if item.id == message_id), None)
        if selected is None:
            raise ValueError("That snapshot is no longer available or was not posted by this bot.")
        attachment = next(
            (item for item in selected.attachments
             if item.filename.startswith(BACKUP_PREFIX) and item.filename.endswith(".zip")),
            None,
        )
        if attachment is None or attachment.size > MAX_ARCHIVE_BYTES:
            raise ValueError("The selected backup attachment is missing or too large.")
        archive_bytes = await attachment.read()

        # Close SQLite before atomically replacing its file, then reload the
        # in-memory feature state so the admin sees the selected snapshot now.
        from modules import ai_feature, apply_feature, events_feature, vouch_feature

        was_db_open = vouch_feature.db._conn is not None
        if was_db_open:
            await vouch_feature.db.close()
        try:
            result = await asyncio.to_thread(
                restore_backup_archive,
                archive_bytes,
                missing_only=False,
                force_settings=False,
            )
        finally:
            if was_db_open:
                await vouch_feature.db.connect()

        events_feature.store.sessions.clear()
        events_feature.store.load()
        ai_feature.reload_runtime_settings_from_env()
        ai_feature.reload_saved_state_from_disk()
        apply_feature.reload_runtime_configuration()

        name = os.getenv("BOT_NAME", "MalixAris Bot").strip() or "MalixAris Bot"
        creator = os.getenv("BOT_CREATOR_NAME", "abubakarmlikgul").strip() or "abubakarmlikgul"
        events_feature.BOT_NAME = name
        vouch_feature.BOT_NAME = name
        vouch_feature.BOT_CREATOR_NAME = creator
        ai_feature.BOT_NAME_PLAIN = name
        ai_feature.BOT_NAME = name
        ai_feature.BOT_CREATOR_NAME = creator
        try:
            import main
            main.BOT_NAME = name
            main.BOT_CREATOR_NAME = creator
            main.ai_feature.BOT_NAME_PLAIN = name
            main.ai_feature.BOT_NAME = name
            main.ai_feature.BOT_CREATOR_NAME = creator
            await self.bot.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.watching,
                    name=f"{name} • /ai chat • /gtn • /vouch • /apply • /ticket panel • /backup",
                )
            )
        except (ImportError, discord.HTTPException):
            logger.warning("Restored state, but could not refresh bot presence", exc_info=True)
        result["filename"] = attachment.filename
        result["source_message_id"] = message_id
        try:
            promoted = await self.create_backup(
                force=True,
                reason=f"restored from {attachment.filename}",
            )
            result["promoted_filename"] = promoted["artifact"].filename
            result["promotion_error"] = None
        except Exception as exc:
            logger.exception("Restore applied, but selected snapshot could not be promoted as the latest backup")
            result["promotion_filename"] = None
            result["promotion_error"] = str(exc)
        return result

    def status_text(self) -> str:
        channel_id = _backup_channel_id()
        last = self.last_backup_at.isoformat(timespec="seconds") if self.last_backup_at else "not uploaded in this process yet"
        return (
            f"Backup channel: `{channel_id or 'not configured'}`\n"
            f"Mode: event-driven; debounce {BACKUP_DEBOUNCE_SECONDS}s; no periodic polling\n"
            f"Restore: manual only with `/backup restore`; startup never imports a file\n"
            f"Retention: latest {BACKUP_RETENTION_COUNT} bot-authored backup files\n"
            f"Last backup: `{last}` ({self.last_backup_filename or 'none'})\n"
            f"Startup: {self.startup_summary}\n"
            "`.env`, bot/API credentials, and source code are excluded from archives."
        )


service: BackupService | None = None
backup_group = app_commands.Group(name="backup", description="Render state backups and manual restore tools", guild_only=True)


def bind_service(bot: commands.Bot) -> BackupService:
    global service
    service = BackupService(bot)
    return service


def notify_state_changed(reason: str = "saved state changed") -> None:
    """Request a debounced snapshot immediately after a persistent state write."""
    if service is not None:
        service.notify_state_changed(reason)


@backup_group.command(name="status", description="Show event-driven backup and manual-import status")
@app_commands.checks.has_permissions(administrator=True)
async def backup_status(interaction: discord.Interaction) -> None:
    if service is None:
        await interaction.response.send_message("Backup service is not initialized yet.", ephemeral=True)
        return
    await interaction.response.send_message(service.status_text(), ephemeral=True)


@backup_group.command(name="now", description="Create a backup snapshot immediately")
@app_commands.checks.has_permissions(administrator=True)
async def backup_now(interaction: discord.Interaction) -> None:
    if service is None:
        await interaction.response.send_message("Backup service is not initialized yet.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        result = await service.create_backup(force=True, reason=f"manual by {interaction.user}")
        artifact: BackupArtifact = result["artifact"]
        await interaction.followup.send(
            f"✅ Backup uploaded as `{artifact.filename}` ({len(artifact.files)} state file(s), "
            f"{artifact.settings_count} non-secret setting value(s)).",
            ephemeral=True,
        )
    except Exception as exc:
        logger.exception("Manual backup upload failed")
        await interaction.followup.send(f"❌ Backup failed: {exc}", ephemeral=True)


@backup_group.command(name="restore", description="Choose a previous backup snapshot to restore")
@app_commands.checks.has_permissions(administrator=True)
async def backup_restore(interaction: discord.Interaction) -> None:
    if service is None:
        await interaction.response.send_message("Backup service is not initialized yet.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        messages = await service.list_backups()
        if not messages:
            await interaction.followup.send("No bot-authored backups were found in the configured channel.", ephemeral=True)
            return
        await interaction.followup.send(
            "Select a snapshot to restore. This replaces the current runtime data but never restores credentials or `.env`.",
            view=BackupSelectView(messages, service),
            ephemeral=True,
        )
    except Exception as exc:
        logger.exception("Could not list backup snapshots")
        await interaction.followup.send(f"❌ Could not load backup files: {exc}", ephemeral=True)
