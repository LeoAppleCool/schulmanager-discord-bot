# Schulmanager Discord Bot

> Discord-Bot für die [Schulmanager API](https://github.com/leoapplecool/schulmanager-api) — synchronisiert Schuldaten automatisch in private Discord-Kanäle pro Nutzer.

![Python](https://img.shields.io/badge/Python-3.11%2B-blue?logo=python)
![Discord.py](https://img.shields.io/badge/discord.py-2.x-5865F2?logo=discord)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker)
![License](https://img.shields.io/badge/Lizenz-MIT-green)

---

## Voraussetzung: Schulmanager API

**Dieser Bot funktioniert nicht eigenständig.** Er benötigt eine laufende Instanz der Schulmanager API als Backend.

> API-Repository: **[schulmanager-api →](https://github.com/leoapplecool/schulmanager-api)**

Die API muss gestartet und erreichbar sein, bevor der Bot verbunden werden kann. Die URL wird über `SM_DISCORD_API_BASE_URL` konfiguriert (Standard: `http://127.0.0.1:8000`).

---

## Features

- **Private Kanäle pro Nutzer** — automatisch erstellte Kategorie mit 9 Kanälen (Stundenplan, Hausaufgaben, Noten, Termine, Fehlzeiten, Nachrichten, Webhook-Log, ...)
- **Automatischer Sync** im konfigurierbaren Intervall (Standard: alle 120 Sekunden)
- **Hausaufgaben-Reaktionen** — ✅ auf eine Nachricht reagieren markiert die Aufgabe als erledigt (wird an die API übermittelt)
- **Tages-Digest** — morgens eine Zusammenfassung im Status-Kanal
- **Erinnerungen** — DM-Erinnerungen vor Klausuren und Hausaufgaben-Abgaben
- **Stundenplan-Änderungs-DMs** — sofortige Benachrichtigung bei Ausfall oder Vertretung
- **Login-Rolle** — optionale Discord-Rolle wird beim Login automatisch vergeben und beim Logout wieder entfernt
- **Admin-Befehle** — Nutzerverwaltung, Sync-Übersicht und Cache-Steuerung direkt aus Discord

---

## Quick Start

### 1. Schulmanager API starten

Zuerst die API zum Laufen bringen:

```bash
git clone https://github.com/leoapplecool/schulmanager-api.git
cd schulmanager-api
cp .env.example .env
# SM_JWT_SECRET setzen
docker compose up --build
```

### 2. Discord Bot starten

```bash
git clone https://github.com/leoapplecool/schulmanager-discord-bot.git
cd schulmanager-discord-bot

cp .env.example .env
# SM_DISCORD_BOT_TOKEN und SM_DISCORD_API_BASE_URL setzen

docker compose up --build
```

### Lokale Entwicklung

```bash
pip install -e ".[dev]"
python -m schulmanager_discord_bot
```

---

## Umgebungsvariablen

| Variable | Beschreibung | Standard |
|---|---|---|
| `SM_DISCORD_BOT_TOKEN` | Discord-Bot-Token (aus dem Developer Portal) | *(erforderlich)* |
| `SM_DISCORD_API_BASE_URL` | URL der laufenden Schulmanager API | `http://127.0.0.1:8000` |
| `SM_DISCORD_GUILD_ID` | Discord-Server-ID (für schnelleres Slash-Command-Sync) | *(leer)* |
| `SM_DISCORD_SYNC_INTERVAL_SECONDS` | Sync-Intervall in Sekunden | `120` |
| `SM_DISCORD_DB_PATH` | Pfad zur SQLite-Datenbank des Bots | `data/discord_bot.sqlite3` |
| `SM_DISCORD_TIMEZONE` | Zeitzone für alle Zeitanzeigen | `Europe/Berlin` |
| `SM_DISCORD_CATEGORY_PREFIX` | Präfix für private Nutzer-Kategorien | `schulmanager` |
| `SM_DISCORD_DIGEST_TIME` | Uhrzeit für den Tages-Digest (HH:MM) | `07:00` |
| `SM_DISCORD_DIGEST_ENABLED` | Tages-Digest aktivieren | `true` |
| `SM_DISCORD_LOGGED_IN_ROLE_ID` | Rolle die bei Login vergeben / bei Logout entfernt wird | *(leer)* |
| `SM_LOG_LEVEL` | Log-Level (`INFO`, `DEBUG`, ...) | `INFO` |

Vollständige Liste: `.env.example`

---

## Slash-Befehle

### Nutzer

| Befehl | Beschreibung |
|---|---|
| `/login email password [student_id]` | Schulmanager-Login und private Kanäle anlegen |
| `/logout [delete_category]` | Bot-Zugang entfernen (optional: Kanäle löschen) |
| `/sync` | Manuellen Sync auslösen |
| `/status` | Bot-Status und letzten Sync-Zeitpunkt anzeigen |
| `/calendar` | ICS-Kalender als DM senden |
| `/digest` | Tages-Digest sofort anzeigen |
| `/info` | Allgemeine Bot-Informationen |
| `/channels` | Eigene Schulmanager-Kanäle anzeigen |
| `/remind exams <hours>` | Klausur-Erinnerung X Stunden vorher aktivieren |
| `/remind homework <hours>` | Hausaufgaben-Erinnerung X Stunden vorher aktivieren |
| `/remind off <type>` | Erinnerung deaktivieren |
| `/notify schedule-changes <on/off>` | DM bei Stundenplan-Änderungen |
| `/notify digest <on/off>` | Tages-Digest aktivieren/deaktivieren |
| `/notify status` | Benachrichtigungs-Einstellungen anzeigen |
| `/debug-state` | Debug-Infos für den eigenen Account |
| `/debug-webhook` | Test-Nachricht in den Webhook-Kanal senden |

### Admin

| Befehl | Beschreibung |
|---|---|
| `/admin-users` | Alle Bot-Nutzer im Server auflisten |
| `/admin-sync-all` | Sync für alle aktiven Nutzer auslösen |
| `/admin-user-active` | Nutzer aktiv/inaktiv setzen |
| `/admin-errors` | Letzte Sync-Fehler aller Nutzer anzeigen |
| `/admin-stats` | Bot-Statistiken (aktive Nutzer, Sync-Count, ...) |
| `/admin-purge` | Nutzer-Workspace vollständig löschen |
| `/admin-flush-cache` | API-Cache leeren |

---

## Kanal-Layout

Pro Nutzer wird automatisch eine private Kategorie mit folgenden Kanälen erstellt:

| Kanal | Inhalt |
|---|---|
| `00-status` | Sync-Status-Embed + Tages-Digest + 🔄-Sync-Button |
| `01-schedule-feed` | Nächste Stunden (automatisch aktualisiert) |
| `02-schedule-week` | Wochenübersicht (ein Embed pro Tag) |
| `03-homework` | Eine Nachricht pro Hausaufgabe + ✅-Reaktion zum Abhaken |
| `04-grades` | Noten je Fach + Notenstatistik mit Trend |
| `05-events` | Schultermine + „Nächstes Event"-Panel |
| `06-webhooks` | Änderungslog nach jedem Sync |
| `07-absences` | Fehlzeiten-Übersicht |
| `08-messages` | Schulnachrichten / Posteingang |

---

## Architektur

```
src/schulmanager_discord_bot/
├── __main__.py      # Einstiegspunkt: lädt Settings und startet den Bot
├── config.py        # Standalone Settings (pydantic-settings, SM_ prefix)
├── bot.py           # Discord-Cog: Slash-Befehle, Sync-Loop, Kanal-Management
├── api_client.py    # Async HTTP-Client für die Schulmanager API (httpx)
├── embeds.py        # Embed-Rendering mit Fingerprint-basierter Deduplizierung
├── storage.py       # SQLite-Persistenz (UserWorkspaceState, EmbedRecord, ...)
└── models.py        # Datenmodelle (UserWorkspaceState, ReminderRule, ...)
```

**Tech-Stack:** Python 3.11+, discord.py 2.x, httpx, pydantic-settings, aiosqlite, Docker Compose

---

## Tests

```bash
pip install -e ".[dev]"
pytest
```

---

## Lizenz

MIT
