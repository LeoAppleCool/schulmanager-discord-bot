from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class UserWorkspaceState:
    guild_id: int
    user_id: int
    email: str
    password: str | None
    student_id: str
    student_name: str
    account_id: str
    access_token: str
    refresh_token: str
    access_expires_at: int
    refresh_expires_at: int
    category_id: int | None
    status_channel_id: int | None
    schedule_feed_channel_id: int | None
    schedule_week_channel_id: int | None
    homework_channel_id: int | None
    grades_channel_id: int | None
    events_channel_id: int | None
    webhooks_channel_id: int | None
    absences_channel_id: int | None
    messages_channel_id: int | None
    letters_channel_id: int | None
    active: bool
    last_sync_ts: int
    last_error: str | None
    last_digest_date: str | None  # ISO date string


@dataclass(slots=True)
class EmbedRecord:
    guild_id: int
    user_id: int
    channel_kind: str
    item_key: str
    message_id: int
    fingerprint: str
    updated_at: int


@dataclass(slots=True)
class UpsertResult:
    action: str
    message_id: int
    changed: bool


@dataclass(slots=True)
class ReminderRule:
    guild_id: int
    user_id: int
    reminder_type: str  # "exam" | "homework"
    hours_before: int
