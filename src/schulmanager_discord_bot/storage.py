from __future__ import annotations

import time
from pathlib import Path

import aiosqlite

from schulmanager_discord_bot.models import EmbedRecord, ReminderRule, UserWorkspaceState


class DiscordStateStore:
    def __init__(self, db_path: str) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

    async def initialize(self) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS discord_users (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    email TEXT NOT NULL,
                    password TEXT,
                    student_id TEXT NOT NULL,
                    student_name TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    access_token TEXT NOT NULL,
                    refresh_token TEXT NOT NULL,
                    access_expires_at INTEGER NOT NULL,
                    refresh_expires_at INTEGER NOT NULL,
                    category_id INTEGER,
                    status_channel_id INTEGER,
                    schedule_feed_channel_id INTEGER,
                    schedule_week_channel_id INTEGER,
                    homework_channel_id INTEGER,
                    grades_channel_id INTEGER,
                    events_channel_id INTEGER,
                    webhooks_channel_id INTEGER,
                    absences_channel_id INTEGER,
                    messages_channel_id INTEGER,
                    letters_channel_id INTEGER,
                    active INTEGER NOT NULL DEFAULT 1,
                    last_sync_ts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    last_digest_date TEXT,
                    PRIMARY KEY (guild_id, user_id)
                )
                """
            )
            for col, ddl in [
                ("password", "TEXT"),
                ("absences_channel_id", "INTEGER"),
                ("messages_channel_id", "INTEGER"),
                ("letters_channel_id", "INTEGER"),
                ("last_digest_date", "TEXT"),
            ]:
                await self._ensure_user_column(db, col, ddl)

            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS discord_embed_messages (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    channel_kind TEXT NOT NULL,
                    item_key TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    fingerprint TEXT NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, user_id, channel_kind, item_key)
                )
                """
            )

            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS reminder_rules (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    reminder_type TEXT NOT NULL,
                    hours_before INTEGER NOT NULL DEFAULT 24,
                    PRIMARY KEY (guild_id, user_id, reminder_type)
                )
                """
            )

            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS reminder_sent (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    item_id TEXT NOT NULL,
                    reminder_type TEXT NOT NULL,
                    sent_at INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, user_id, item_id, reminder_type)
                )
                """
            )

            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS homework_done (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    homework_id TEXT NOT NULL,
                    done INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, user_id, homework_id)
                )
                """
            )

            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS notification_prefs (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    pref_key TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY (guild_id, user_id, pref_key)
                )
                """
            )

            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS schedule_change_seen (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    change_key TEXT NOT NULL,
                    seen_at INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, user_id, change_key)
                )
                """
            )

            await db.commit()

    # ─── Users ────────────────────────────────────────────────────────────────

    async def upsert_user(self, state: UserWorkspaceState) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO discord_users (
                    guild_id, user_id, email, password, student_id, student_name, account_id,
                    access_token, refresh_token, access_expires_at, refresh_expires_at,
                    category_id, status_channel_id, schedule_feed_channel_id,
                    schedule_week_channel_id, homework_channel_id, grades_channel_id,
                    events_channel_id, webhooks_channel_id, absences_channel_id,
                    messages_channel_id, letters_channel_id, active, last_sync_ts, last_error, last_digest_date
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET
                    email=excluded.email,
                    password=excluded.password,
                    student_id=excluded.student_id,
                    student_name=excluded.student_name,
                    account_id=excluded.account_id,
                    access_token=excluded.access_token,
                    refresh_token=excluded.refresh_token,
                    access_expires_at=excluded.access_expires_at,
                    refresh_expires_at=excluded.refresh_expires_at,
                    category_id=excluded.category_id,
                    status_channel_id=excluded.status_channel_id,
                    schedule_feed_channel_id=excluded.schedule_feed_channel_id,
                    schedule_week_channel_id=excluded.schedule_week_channel_id,
                    homework_channel_id=excluded.homework_channel_id,
                    grades_channel_id=excluded.grades_channel_id,
                    events_channel_id=excluded.events_channel_id,
                    webhooks_channel_id=excluded.webhooks_channel_id,
                    absences_channel_id=excluded.absences_channel_id,
                    messages_channel_id=excluded.messages_channel_id,
                    letters_channel_id=excluded.letters_channel_id,
                    active=excluded.active,
                    last_sync_ts=excluded.last_sync_ts,
                    last_error=excluded.last_error,
                    last_digest_date=excluded.last_digest_date
                """,
                (
                    state.guild_id,
                    state.user_id,
                    state.email,
                    state.password,
                    state.student_id,
                    state.student_name,
                    state.account_id,
                    state.access_token,
                    state.refresh_token,
                    state.access_expires_at,
                    state.refresh_expires_at,
                    state.category_id,
                    state.status_channel_id,
                    state.schedule_feed_channel_id,
                    state.schedule_week_channel_id,
                    state.homework_channel_id,
                    state.grades_channel_id,
                    state.events_channel_id,
                    state.webhooks_channel_id,
                    state.absences_channel_id,
                    state.messages_channel_id,
                    state.letters_channel_id,
                    1 if state.active else 0,
                    state.last_sync_ts,
                    state.last_error,
                    state.last_digest_date,
                ),
            )
            await db.commit()

    async def get_user(self, guild_id: int, user_id: int) -> UserWorkspaceState | None:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM discord_users WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            row = await cursor.fetchone()
            return self._row_to_user(row) if row else None

    async def list_active_users(self) -> list[UserWorkspaceState]:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM discord_users WHERE active = 1")
            rows = await cursor.fetchall()
            return [self._row_to_user(row) for row in rows]

    async def list_users_for_guild(self, guild_id: int) -> list[UserWorkspaceState]:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM discord_users WHERE guild_id = ? ORDER BY user_id",
                (guild_id,),
            )
            rows = await cursor.fetchall()
            return [self._row_to_user(row) for row in rows]

    async def update_tokens(
        self,
        guild_id: int,
        user_id: int,
        access_token: str,
        refresh_token: str,
        access_expires_at: int,
        refresh_expires_at: int,
    ) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                UPDATE discord_users
                SET access_token = ?, refresh_token = ?, access_expires_at = ?, refresh_expires_at = ?
                WHERE guild_id = ? AND user_id = ?
                """,
                (access_token, refresh_token, access_expires_at, refresh_expires_at, guild_id, user_id),
            )
            await db.commit()

    async def update_sync_status(
        self,
        guild_id: int,
        user_id: int,
        *,
        last_sync_ts: int,
        last_error: str | None,
    ) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                UPDATE discord_users
                SET last_sync_ts = ?, last_error = ?
                WHERE guild_id = ? AND user_id = ?
                """,
                (last_sync_ts, last_error, guild_id, user_id),
            )
            await db.commit()

    async def update_digest_date(self, guild_id: int, user_id: int, digest_date: str) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE discord_users SET last_digest_date = ? WHERE guild_id = ? AND user_id = ?",
                (digest_date, guild_id, user_id),
            )
            await db.commit()

    async def set_active(self, guild_id: int, user_id: int, active: bool) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE discord_users SET active = ? WHERE guild_id = ? AND user_id = ?",
                (1 if active else 0, guild_id, user_id),
            )
            await db.commit()

    async def delete_user(self, guild_id: int, user_id: int) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "DELETE FROM discord_embed_messages WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            await db.execute(
                "DELETE FROM discord_users WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            await db.execute(
                "DELETE FROM reminder_rules WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            await db.execute(
                "DELETE FROM reminder_sent WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            await db.execute(
                "DELETE FROM homework_done WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            await db.execute(
                "DELETE FROM notification_prefs WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            await db.execute(
                "DELETE FROM schedule_change_seen WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            await db.commit()

    # ─── Embed records ────────────────────────────────────────────────────────

    async def get_embed_record(
        self,
        guild_id: int,
        user_id: int,
        channel_kind: str,
        item_key: str,
    ) -> EmbedRecord | None:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM discord_embed_messages
                WHERE guild_id = ? AND user_id = ? AND channel_kind = ? AND item_key = ?
                """,
                (guild_id, user_id, channel_kind, item_key),
            )
            row = await cursor.fetchone()
            return self._row_to_embed(row) if row else None

    async def get_embed_record_by_message_id(
        self,
        guild_id: int,
        channel_kind: str,
        message_id: int,
    ) -> EmbedRecord | None:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM discord_embed_messages
                WHERE guild_id = ? AND channel_kind = ? AND message_id = ?
                LIMIT 1
                """,
                (guild_id, channel_kind, message_id),
            )
            row = await cursor.fetchone()
            return self._row_to_embed(row) if row else None

    async def list_embed_records(
        self,
        guild_id: int,
        user_id: int,
        channel_kind: str,
    ) -> list[EmbedRecord]:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM discord_embed_messages
                WHERE guild_id = ? AND user_id = ? AND channel_kind = ?
                """,
                (guild_id, user_id, channel_kind),
            )
            rows = await cursor.fetchall()
            return [self._row_to_embed(row) for row in rows]

    async def upsert_embed_record(
        self,
        guild_id: int,
        user_id: int,
        channel_kind: str,
        item_key: str,
        message_id: int,
        fingerprint: str,
    ) -> None:
        now = int(time.time())
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO discord_embed_messages (
                    guild_id, user_id, channel_kind, item_key, message_id, fingerprint, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id, channel_kind, item_key) DO UPDATE SET
                    message_id = excluded.message_id,
                    fingerprint = excluded.fingerprint,
                    updated_at = excluded.updated_at
                """,
                (guild_id, user_id, channel_kind, item_key, message_id, fingerprint, now),
            )
            await db.commit()

    async def delete_embed_record(
        self,
        guild_id: int,
        user_id: int,
        channel_kind: str,
        item_key: str,
    ) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                DELETE FROM discord_embed_messages
                WHERE guild_id = ? AND user_id = ? AND channel_kind = ? AND item_key = ?
                """,
                (guild_id, user_id, channel_kind, item_key),
            )
            await db.commit()

    # ─── Reminder rules ───────────────────────────────────────────────────────

    async def upsert_reminder_rule(self, rule: ReminderRule) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO reminder_rules (guild_id, user_id, reminder_type, hours_before)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id, reminder_type) DO UPDATE SET
                    hours_before = excluded.hours_before
                """,
                (rule.guild_id, rule.user_id, rule.reminder_type, rule.hours_before),
            )
            await db.commit()

    async def get_reminder_rule(
        self, guild_id: int, user_id: int, reminder_type: str
    ) -> ReminderRule | None:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM reminder_rules WHERE guild_id = ? AND user_id = ? AND reminder_type = ?",
                (guild_id, user_id, reminder_type),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            return ReminderRule(
                guild_id=row["guild_id"],
                user_id=row["user_id"],
                reminder_type=row["reminder_type"],
                hours_before=row["hours_before"],
            )

    async def list_all_reminder_rules(self) -> list[ReminderRule]:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM reminder_rules")
            rows = await cursor.fetchall()
            return [
                ReminderRule(
                    guild_id=row["guild_id"],
                    user_id=row["user_id"],
                    reminder_type=row["reminder_type"],
                    hours_before=row["hours_before"],
                )
                for row in rows
            ]

    async def has_sent_reminder(
        self, guild_id: int, user_id: int, item_id: str, reminder_type: str
    ) -> bool:
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                "SELECT 1 FROM reminder_sent WHERE guild_id = ? AND user_id = ? AND item_id = ? AND reminder_type = ?",
                (guild_id, user_id, item_id, reminder_type),
            )
            row = await cursor.fetchone()
            return row is not None

    async def mark_reminder_sent(
        self, guild_id: int, user_id: int, item_id: str, reminder_type: str
    ) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO reminder_sent (guild_id, user_id, item_id, reminder_type, sent_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (guild_id, user_id, item_id, reminder_type, int(time.time())),
            )
            await db.commit()

    # ─── Homework done state ──────────────────────────────────────────────────

    async def get_homework_done(self, guild_id: int, user_id: int, homework_id: str) -> bool:
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                "SELECT done FROM homework_done WHERE guild_id = ? AND user_id = ? AND homework_id = ?",
                (guild_id, user_id, homework_id),
            )
            row = await cursor.fetchone()
            return bool(row[0]) if row else False

    async def set_homework_done(
        self, guild_id: int, user_id: int, homework_id: str, done: bool
    ) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO homework_done (guild_id, user_id, homework_id, done, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id, homework_id) DO UPDATE SET
                    done = excluded.done,
                    updated_at = excluded.updated_at
                """,
                (guild_id, user_id, homework_id, 1 if done else 0, int(time.time())),
            )
            await db.commit()

    async def get_all_homework_done(self, guild_id: int, user_id: int) -> dict[str, bool]:
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                "SELECT homework_id, done FROM homework_done WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            rows = await cursor.fetchall()
            return {row[0]: bool(row[1]) for row in rows}

    # ─── Reminder rules (delete) ──────────────────────────────────────────────

    async def delete_reminder_rule(self, guild_id: int, user_id: int, reminder_type: str) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "DELETE FROM reminder_rules WHERE guild_id = ? AND user_id = ? AND reminder_type = ?",
                (guild_id, user_id, reminder_type),
            )
            await db.commit()

    # ─── Notification prefs ───────────────────────────────────────────────────

    async def get_notification_pref(self, guild_id: int, user_id: int, pref_key: str, default: bool = True) -> bool:
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                "SELECT enabled FROM notification_prefs WHERE guild_id = ? AND user_id = ? AND pref_key = ?",
                (guild_id, user_id, pref_key),
            )
            row = await cursor.fetchone()
            return bool(row[0]) if row is not None else default

    async def set_notification_pref(self, guild_id: int, user_id: int, pref_key: str, enabled: bool) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO notification_prefs (guild_id, user_id, pref_key, enabled)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id, pref_key) DO UPDATE SET enabled = excluded.enabled
                """,
                (guild_id, user_id, pref_key, 1 if enabled else 0),
            )
            await db.commit()

    async def list_notification_prefs(self, guild_id: int, user_id: int) -> dict[str, bool]:
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                "SELECT pref_key, enabled FROM notification_prefs WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            rows = await cursor.fetchall()
            return {row[0]: bool(row[1]) for row in rows}

    # ─── Schedule change dedup ────────────────────────────────────────────────

    async def has_seen_schedule_change(self, guild_id: int, user_id: int, change_key: str) -> bool:
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                "SELECT 1 FROM schedule_change_seen WHERE guild_id = ? AND user_id = ? AND change_key = ?",
                (guild_id, user_id, change_key),
            )
            row = await cursor.fetchone()
            return row is not None

    async def mark_schedule_change_seen(self, guild_id: int, user_id: int, change_key: str) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO schedule_change_seen (guild_id, user_id, change_key, seen_at)
                VALUES (?, ?, ?, ?)
                """,
                (guild_id, user_id, change_key, int(time.time())),
            )
            await db.commit()

    async def purge_old_dedup(self, retention_days: int = 90) -> None:
        """Drop dedup rows older than the retention window so these tables don't grow forever."""
        cutoff = int(time.time()) - max(retention_days, 1) * 86400
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("DELETE FROM schedule_change_seen WHERE seen_at < ?", (cutoff,))
            await db.execute("DELETE FROM reminder_sent WHERE sent_at < ?", (cutoff,))
            await db.commit()

    # ─── Row converters ───────────────────────────────────────────────────────

    @staticmethod
    def _row_to_user(row: aiosqlite.Row) -> UserWorkspaceState:
        def _opt_int(key: str) -> int | None:
            try:
                val = row[key]
                return int(val) if val is not None else None
            except (KeyError, TypeError):
                return None

        def _opt_str(key: str) -> str | None:
            try:
                val = row[key]
                return str(val) if val is not None else None
            except (KeyError, TypeError):
                return None

        return UserWorkspaceState(
            guild_id=row["guild_id"],
            user_id=row["user_id"],
            email=row["email"],
            password=_opt_str("password"),
            student_id=row["student_id"],
            student_name=row["student_name"],
            account_id=row["account_id"],
            access_token=row["access_token"],
            refresh_token=row["refresh_token"],
            access_expires_at=row["access_expires_at"],
            refresh_expires_at=row["refresh_expires_at"],
            category_id=_opt_int("category_id"),
            status_channel_id=_opt_int("status_channel_id"),
            schedule_feed_channel_id=_opt_int("schedule_feed_channel_id"),
            schedule_week_channel_id=_opt_int("schedule_week_channel_id"),
            homework_channel_id=_opt_int("homework_channel_id"),
            grades_channel_id=_opt_int("grades_channel_id"),
            events_channel_id=_opt_int("events_channel_id"),
            webhooks_channel_id=_opt_int("webhooks_channel_id"),
            absences_channel_id=_opt_int("absences_channel_id"),
            messages_channel_id=_opt_int("messages_channel_id"),
            letters_channel_id=_opt_int("letters_channel_id"),
            active=bool(row["active"]),
            last_sync_ts=row["last_sync_ts"],
            last_error=_opt_str("last_error"),
            last_digest_date=_opt_str("last_digest_date"),
        )

    @staticmethod
    async def _ensure_user_column(db: aiosqlite.Connection, column_name: str, ddl: str) -> None:
        cursor = await db.execute("PRAGMA table_info(discord_users)")
        rows = await cursor.fetchall()
        existing = {str(row[1]) for row in rows}
        if column_name in existing:
            return
        await db.execute(f"ALTER TABLE discord_users ADD COLUMN {column_name} {ddl}")

    @staticmethod
    def _row_to_embed(row: aiosqlite.Row) -> EmbedRecord:
        return EmbedRecord(
            guild_id=row["guild_id"],
            user_id=row["user_id"],
            channel_kind=row["channel_kind"],
            item_key=row["item_key"],
            message_id=row["message_id"],
            fingerprint=row["fingerprint"],
            updated_at=row["updated_at"],
        )
