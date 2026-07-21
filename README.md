# Schulmanager Discord Bot

> Discord bot for the [Schulmanager API](https://github.com/leoapplecool/schulmanager-api) — automatically syncs school data into a private per-user forum (one thread per section + a pinned dashboard).

![Python](https://img.shields.io/badge/Python-3.11%2B-blue?logo=python)
![Discord.py](https://img.shields.io/badge/discord.py-2.x-5865F2?logo=discord)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker)
![License](https://img.shields.io/badge/License-MIT-green)

---

## Prerequisite: Schulmanager API

**This bot does not work on its own.** It requires a running instance of the Schulmanager API as its backend.

> API repository: **[schulmanager-api →](https://github.com/leoapplecool/schulmanager-api)**

The API must be started and reachable before the bot can connect. The URL is configured via `SM_DISCORD_API_BASE_URL` (default: `http://127.0.0.1:8000`).

---

## Features

- **Private forum per user** — on `/login`, a private **forum channel** with your name is created (only you can see it). Each section (timetable, homework, grades, exams, events, absences, messages, parent letters, payments, learning) is its own **thread**.
- **Pinned dashboard** — at the very top, a pinned thread with a live overview (next lesson, open tasks, unread messages/letters, open payments …) and **buttons**: 🔄 Sync · 📅 Calendar · ⚙️ Manage threads.
- **Threads on/off** — via "Manage threads" (or `/threads`) you can turn individual sections off (the thread is deleted) or back on (it is recreated).
- **Payments & learning** — dedicated threads for open/paid invoices and for tasks/materials (seen/done).
- **Parent letters** — a dedicated thread with read status + DM notice for letters that require confirmation.
- **Automatic sync** at a configurable interval (default: 120 s); all data sources are fetched **in parallel**; a single failing endpoint does not block the sync.
- **Daily digest** — one summary message in the dashboard thread each morning, **edited in place** for the rest of the day instead of being posted again (also catches up after downtime).
- **Reminders** — DM reminders before exams and homework (in the school time zone).
- **Timetable change DMs** — instant notification for cancellations/substitutions.
- **Login role** & **admin commands** — optional role on login; user management/sync/cache from Discord.

> **Bot permissions:** The bot needs **Manage Channels**, **Manage Threads** and **Create Public Threads** in order to create the private forums.

---

## Quick Start

### 1. Start the Schulmanager API

First get the API running:

```bash
git clone https://github.com/leoapplecool/schulmanager-api.git
cd schulmanager-api
cp .env.example .env
# set SM_JWT_SECRET
docker compose up --build
```

### 2. Start the Discord bot

```bash
git clone https://github.com/leoapplecool/schulmanager-discord-bot.git
cd schulmanager-discord-bot

cp .env.example .env
# set SM_DISCORD_BOT_TOKEN and SM_DISCORD_API_BASE_URL

docker compose up --build
```

### Local development

```bash
pip install -e ".[dev]"
python -m schulmanager_discord_bot
```

---

## Environment variables

| Variable | Description | Default |
|---|---|---|
| `SM_DISCORD_BOT_TOKEN` | Discord bot token (from the Developer Portal) | *(required)* |
| `SM_DISCORD_API_BASE_URL` | URL of the running Schulmanager API | `http://127.0.0.1:8000` |
| `SM_DISCORD_GUILD_ID` | Discord server ID (for faster slash-command sync) | *(empty)* |
| `SM_DISCORD_SYNC_INTERVAL_SECONDS` | Sync interval in seconds | `120` |
| `SM_DISCORD_DB_PATH` | Path to the bot's SQLite database | `data/discord_bot.sqlite3` |
| `SM_DISCORD_TIMEZONE` | Time zone for all time displays | `Europe/Berlin` |
| `SM_DISCORD_CATEGORY_PREFIX` | Prefix for the private per-user forum name | `schulmanager` |
| `SM_DISCORD_DIGEST_TIME` | Time of day for the daily digest (HH:MM) | `07:00` |
| `SM_DISCORD_DIGEST_ENABLED` | Enable the daily digest | `true` |
| `SM_DISCORD_LOGGED_IN_ROLE_ID` | Role granted on login / removed on logout | *(empty)* |
| `SM_LOG_LEVEL` | Log level (`INFO`, `DEBUG`, ...) | `INFO` |

Full list: `.env.example`

---

## Slash commands

### User

| Command | Description |
|---|---|
| `/login email password [student_id]` | Log in to Schulmanager and create the private forum |
| `/logout [delete_forum]` | Remove bot access (optionally: delete the forum) |
| `/sync` | Trigger a manual sync |
| `/status` | Show bot status and the last sync time |
| `/calendar` | Send the ICS calendar as a DM |
| `/digest` | Post today's digest immediately, or refresh it if it already exists |
| `/info` | General bot information |
| `/threads` | Show forum threads & toggle them on/off |
| `/remind exams <hours>` | Enable an exam reminder X hours in advance |
| `/remind homework <hours>` | Enable a homework reminder X hours in advance |
| `/remind off <type>` | Disable a reminder |
| `/notify schedule-changes <on/off>` | DM on timetable changes |
| `/notify digest <on/off>` | Enable/disable the daily digest |
| `/notify letters <on/off>` | DM for new parent letters that require confirmation |
| `/notify status` | Show notification settings |
| `/debug-state` | Debug info for your own account |

### Admin

| Command | Description |
|---|---|
| `/admin-users` | List all bot users on the server |
| `/admin-sync-all` | Trigger a sync for all active users |
| `/admin-user-active` | Set a user active/inactive |
| `/admin-errors` | Show the latest sync errors for all users |
| `/admin-stats` | Bot statistics (active users, sync count, ...) |
| `/admin-purge` | Fully delete a user's workspace |
| `/admin-flush-cache` | Clear the API cache |

---

## Forum layout

A private **forum channel** is created per user. The pinned **📊 Dashboard** thread bundles
the overview + buttons; below it sits one thread per section (toggle on/off via `/threads`):

| Thread | Content |
|---|---|
| 📊 **Dashboard** (pinned) | Live overview + buttons (Sync, Calendar, Manage threads) + daily digest |
| 📅 Stundenplan | Next lessons + weekly overview |
| 📚 Hausaufgaben | Grouped by due date |
| 📊 Noten | Grade statistics with trend + grades per subject |
| 📝 Klausuren | Upcoming exams |
| 🗓️ Termine | School events |
| 📋 Fehlzeiten | Absences overview (excused/unexcused) |
| 📬 Nachrichten | Messenger conversations (with unread counter) |
| ✉️ Elternbriefe | Parent letters with read status & confirmation notice |
| 💶 Zahlungen | Open/paid invoices |
| 📓 Lernen | Tasks & materials (seen/done) |

---

## Architecture

```
src/schulmanager_discord_bot/
├── __main__.py      # Entry point: loads settings and starts the bot
├── config.py        # Standalone settings (pydantic-settings, SM_ prefix)
├── bot.py           # Discord cog: slash commands, sync loop, forum presentation
├── forum.py         # Forum sections, rendering dispatch, dashboard/manage views
├── api_client.py    # Async HTTP client for the Schulmanager API (httpx)
├── embeds.py        # Embed rendering with fingerprint-based deduplication
├── storage.py       # SQLite persistence (UserWorkspaceState, ForumSectionRecord, ...)
└── models.py        # Data models (UserWorkspaceState, ForumSectionRecord, ...)
```

**Tech stack:** Python 3.11+, discord.py 2.x, httpx, pydantic-settings, aiosqlite, Docker Compose

---

## Tests

```bash
pip install -e ".[dev]"
pytest
```

---

## License

MIT
