# Schulmanager Discord Bot

> Discord-Bot für die [Schulmanager API](https://github.com/leoapplecool/schulmanager-api) — synchronisiert Schuldaten automatisch in ein privates Forum pro Nutzer (ein Thread je Bereich + gepinntes Dashboard).

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

- **Privates Forum pro Nutzer** — beim `/login` wird ein privater **Forum-Kanal** mit deinem Namen erstellt (nur du siehst ihn). Jeder Bereich (Stundenplan, Hausaufgaben, Noten, Klausuren, Termine, Fehlzeiten, Nachrichten, Elternbriefe, Zahlungen, Lernen) ist ein eigener **Thread**.
- **Angepinntes Dashboard** — ganz oben ein gepinnter Thread mit Live-Übersicht (nächste Stunde, offene Aufgaben, ungelesene Nachrichten/Briefe, offene Zahlungen …) und **Buttons**: 🔄 Sync · 📅 Kalender · ⚙️ Threads verwalten.
- **Threads an/aus** — über „Threads verwalten" (oder `/threads`) schaltest du einzelne Bereiche aus (Thread wird gelöscht) oder wieder an (wird neu erstellt).
- **Zahlungen & Lernen** — eigene Threads für offene/bezahlte Rechnungen und für Aufgaben/Material (seen/done).
- **Elternbriefe** — eigener Thread mit Lesestatus + DM-Hinweis bei bestätigungspflichtigen Briefen.
- **Automatischer Sync** im konfigurierbaren Intervall (Standard: 120 s); alle Datenquellen werden **parallel** abgerufen; ein einzelner fehlerhafter Endpoint blockiert den Sync nicht.
- **Tages-Digest** — morgens eine Zusammenfassung im Dashboard-Thread (holt auch bei Ausfallzeit nach).
- **Erinnerungen** — DM-Erinnerungen vor Klausuren und Hausaufgaben (in Schul-Zeitzone).
- **Stundenplan-Änderungs-DMs** — sofortige Benachrichtigung bei Ausfall/Vertretung.
- **Login-Rolle** & **Admin-Befehle** — optionale Rolle beim Login; Nutzerverwaltung/Sync/Cache aus Discord.

> **Bot-Rechte:** Der Bot braucht **Kanäle verwalten**, **Threads verwalten** und **Öffentliche Threads erstellen**, um die privaten Foren anzulegen.

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
| `SM_DISCORD_CATEGORY_PREFIX` | Präfix für den privaten Forum-Namen pro Nutzer | `schulmanager` |
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
| `/login email password [student_id]` | Schulmanager-Login und privates Forum anlegen |
| `/logout [delete_forum]` | Bot-Zugang entfernen (optional: Forum löschen) |
| `/sync` | Manuellen Sync auslösen |
| `/status` | Bot-Status und letzten Sync-Zeitpunkt anzeigen |
| `/calendar` | ICS-Kalender als DM senden |
| `/digest` | Tages-Digest sofort posten |
| `/info` | Allgemeine Bot-Informationen |
| `/threads` | Forum-Threads anzeigen & an/aus schalten |
| `/remind exams <hours>` | Klausur-Erinnerung X Stunden vorher aktivieren |
| `/remind homework <hours>` | Hausaufgaben-Erinnerung X Stunden vorher aktivieren |
| `/remind off <type>` | Erinnerung deaktivieren |
| `/notify schedule-changes <on/off>` | DM bei Stundenplan-Änderungen |
| `/notify digest <on/off>` | Tages-Digest aktivieren/deaktivieren |
| `/notify letters <on/off>` | DM bei neuen bestätigungspflichtigen Elternbriefen |
| `/notify status` | Benachrichtigungs-Einstellungen anzeigen |
| `/debug-state` | Debug-Infos für den eigenen Account |

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

## Forum-Layout

Pro Nutzer wird ein privater **Forum-Kanal** erstellt. Der gepinnte **📊 Dashboard**-Thread bündelt
die Übersicht + Buttons; darunter liegt pro Bereich ein Thread (per `/threads` an/aus schaltbar):

| Thread | Inhalt |
|---|---|
| 📊 **Dashboard** (gepinnt) | Live-Übersicht + Buttons (Sync, Kalender, Threads verwalten) + Tages-Digest |
| 📅 Stundenplan | Nächste Stunden + Wochenübersicht |
| 📚 Hausaufgaben | Nach Fälligkeitstag gruppiert |
| 📊 Noten | Notenstatistik mit Trend + Noten je Fach |
| 📝 Klausuren | Anstehende Klausuren |
| 🗓️ Termine | Schultermine |
| 📋 Fehlzeiten | Fehlzeiten-Übersicht (entschuldigt/unentschuldigt) |
| 📬 Nachrichten | Messenger-Konversationen (mit Ungelesen-Zähler) |
| ✉️ Elternbriefe | Elternbriefe mit Lesestatus & Bestätigungshinweis |
| 💶 Zahlungen | Offene/bezahlte Rechnungen |
| 📓 Lernen | Aufgaben & Material (seen/done) |

---

## Architektur

```
src/schulmanager_discord_bot/
├── __main__.py      # Einstiegspunkt: lädt Settings und startet den Bot
├── config.py        # Standalone Settings (pydantic-settings, SM_ prefix)
├── bot.py           # Discord-Cog: Slash-Befehle, Sync-Loop, Forum-Präsentation
├── forum.py         # Forum-Sektionen, Rendering-Dispatch, Dashboard-/Verwaltungs-Views
├── api_client.py    # Async HTTP-Client für die Schulmanager API (httpx)
├── embeds.py        # Embed-Rendering mit Fingerprint-basierter Deduplizierung
├── storage.py       # SQLite-Persistenz (UserWorkspaceState, ForumSectionRecord, ...)
└── models.py        # Datenmodelle (UserWorkspaceState, ForumSectionRecord, ...)
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
