# MalixAris Bot

A merged `discord.py` bot based on the four supplied bot projects, branded **MalixAris Bot**, made by **abubakarmlikgul**. The four features run under one Discord bot account and one shared client.

## Features

- **Staff applications:** `/apply panel` offers **Staff** and **Dropper** applications using the original private interview and review flow.
- **Support tickets:** `/ticket panel` posts a separate dropdown for **General, Claim, Reward Claim, Support, Partnership, and Other**. Tickets use the existing configured ticket category, staff roles, and transcript log; the application panel stays separate.
- **Render backups:** `/backup status`, `/backup now`, and `/backup restore`; tracks runtime-state changes and stores selectable ZIP snapshots in the configured mod channel.
- **Guess-number and race events:** `/gtn ...`
- **Vouch/reputation system:** `/vouch ...`
- **AI:** `/ai ...`, backed by OpenRouter; no local model is downloaded.
- One shared health endpoint at `/health` (also `/`).
- Original event JSON, vouch SQLite, and AI JSON state formats are retained.

The complete list of **78 slash-command handlers** is in [`COMMANDS.md`](COMMANDS.md).

## Important: Quest Completer file

The newly supplied Quest Completer source was **not added**. It asks users for Discord account tokens and automates actions as those users. This merged bot will not collect, store, validate, forward, or use Discord user-account tokens. A legitimate manual quest/reward tracking feature would need to be scoped separately.

## Requirements

- Python 3.11 or newer
- One Discord application/bot token
- An OpenRouter API key for AI features (optional)
- No persistent disk is required; snapshots are stored as ZIP attachments in the configured private Discord channel

## Configure and run

1. Put this project on the server and enter its directory.
2. Fill in the blank credentials in `.env`.
3. Check the ticket category, staff role, transcript-log, and backup-channel IDs in `.env`.
4. Install dependencies and launch:

   ```bash
   python -m venv .venv
   . .venv/bin/activate
   python -m pip install -r requirements.txt
   python main.py
   ```

On Windows PowerShell, use `py -3.11 -m venv .venv`, then `.\.venv\Scripts\Activate.ps1` (from the project folder) to activate it.

The provided `.env` has blank credential fields. **The bot will not connect until `DISCORD_TOKEN` is filled in.** AI commands need `OPENROUTER_API_KEY`; other features can still run without it. Do not commit the filled `.env` file to Git or share it. `.env` is ignored by `.gitignore`.

Set `GUILD_ID` to your test server ID to sync commands there quickly during testing. After confirming the command tree, set `GUILD_ID=0` to sync globally. Global command changes can take time to appear in every server.

## Discord setup

Enable **Message Content Intent** and **Server Members Intent** for the chosen bot application. Invite one bot account with the `applications.commands` scope and only the permissions it needs. Depending on enabled features, these include View Channels, Send Messages, Read Message History, Embed Links, Attach Files, Manage Channels, Manage Roles, and Manage Messages. Configure role hierarchy if the bot needs to assign the accepted role.

Changing `BOT_NAME` updates embeds, help, and presence text. Discord's actual bot username must also be renamed in the Discord Developer Portal; a bot cannot rename its own account through this code.

## Deploying on Render

Create a Render **Web Service** for this project with build command `pip install -r requirements.txt`, start command `python main.py`, and health-check path `/health`. The app binds to `0.0.0.0` and uses Render's `PORT` value when one is supplied.

Do not commit `.env`. Set credentials in Render's Environment settings or upload the filled file as a Secret File named `.env`. This setup intentionally uses **no persistent disk**: local JSON/SQLite files are temporary working state, with event-triggered ZIP snapshots stored in the private Discord backup channel. The backup is imported only when an administrator selects it with `/backup restore`—it is not automatically restored after restart.

For Render Free keep-alive, use an external cron/HTTP monitor to request `https://YOUR-SERVICE.onrender.com/health` every 10 minutes. The app exposes `/health`; it does not run an internal self-ping loop. Update `YOUR-SERVICE` to the public Render hostname. This ping only keeps the web service warm; it does not restore bot data.

## Render Discord state backups

`BACKUP_CHANNEL_ID` is set to the mod-channel ID provided for this deployment. Keep that channel private to moderators and the bot. The bot needs View Channel, Send Messages, Read Message History, and Attach Files there.

There is **no 5-minute polling** and **no automatic restore**. When an event session, AI settings file, or vouch database is successfully saved, the bot queues a ZIP backup after a short 8-second debounce so multiple writes are combined. A first baseline file is posted if the channel has no backups, and a final changed-state snapshot is attempted on graceful shutdown. It retains the newest 25 bot-authored snapshots so each fits in Discord's selection dropdown. `/backup now` creates one immediately; `/backup restore` lets an administrator select and import a snapshot manually, then posts the restored state as the newest recovery file; `/backup status` reports the mode and latest file.

Archives include the event JSON, vouch SQLite database (copied with SQLite's online backup API), AI JSON state, and reviewed non-secret configuration values; common token-shaped strings are redacted in the archive copies. They **never include `.env`, the Discord bot token, OpenRouter/API keys, or source code**. Put credentials in Render's Environment settings/Secret Files. Existing Render environment values stay authoritative; a restore only fills safe configuration values that are missing. Render dashboard variable changes are not file-write events, so run `/backup now` after changing them if you want a new snapshot. Backups cannot recover a lost secret.

## Data and backups

Runtime files default to:

- `data/events.json` — number/race sessions
- `data/vouch_bot.db` — vouch records and configuration
- `data/metal_ai_data.json` — AI configuration, limits, and saved state

These are local working files only; Render Free can discard them. No persistent disk is mounted. After a restart, run `/backup restore` and select the ZIP to import when you want the saved state back.

Back up existing data before first deployment. The uploaded files contained source code, not live database/state files, so copy any old-host data into `data/` before launching. SQLite may also create `-wal` and `-shm` files while running; stop the service or use SQLite's backup API before copying a live database.

Staff application and support-ticket metadata are stored in Discord channel topics. Support-ticket transcripts are sent to the configured `LOG_CHANNEL_ID` before the ticket channel is deleted. Transcript text redacts common token-shaped strings and lists attachment filenames without adding download URLs; users should still never post passwords or account tokens. If the log channel is missing or transcript upload fails, the support ticket is left open rather than deleted without a transcript.

## Low-resource settings

The `.env` uses conservative defaults for a **512 MB RAM** host: one concurrent AI request, one media request, one attachment capped at 2 MB, four context messages, a message cache of 80, and at most 150 in-memory conversations. AI calls are remote through OpenRouter; no model weights are loaded locally. No persistent Render disk is used. The 25-snapshot Discord retention limit bounds remote backup clutter.

## Discord command-group limit

Discord allows at most 25 direct options in a command group. `/ai` has 23 direct commands plus the `server` and `admin` subgroups, exactly 25. Some AI paths therefore have three components, for example `/ai server setrules` and `/ai admin setmodel`; the original command names remain the final component.

## Tests

Run these from the project directory:

```bash
python -m unittest discover -s tests -v
```

The tests check all 78 registered paths, the six root namespaces, the `/ai` option limit, Discord command serialization, persistent views, application/ticket choices, token-shaped transcript redaction, backup secret exclusion and state restore, and that multiple message/ready listeners coexist. They do not connect to Discord or call OpenRouter.
