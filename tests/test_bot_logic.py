"""Tests for pure bot logic and the storage letters migration."""
from __future__ import annotations

import asyncio

import httpx

from schulmanager_discord_bot.api_client import ApiClientError, SchulmanagerApiClient
from schulmanager_discord_bot.bot import SchulmanagerCog
from schulmanager_discord_bot.models import UserWorkspaceState
from schulmanager_discord_bot.storage import DiscordStateStore


def test_connection_error_becomes_api_client_error() -> None:
    """A transport failure (e.g. bad SM_DISCORD_API_BASE_URL) must surface as a clean
    ApiClientError so the bot shows a friendly message instead of crashing."""
    client = SchulmanagerApiClient("http://unreachable.invalid:9999")

    class _FakeHttpx:
        async def request(self, *a, **k):
            raise httpx.ConnectError("Name or service not known")

    client._client = _FakeHttpx()  # type: ignore[assignment]

    async def run() -> None:
        try:
            await client.get_students("token")
        except ApiClientError as exc:
            assert exc.status_code is None
            assert "nicht erreichbar" in str(exc)
        else:
            raise AssertionError("expected ApiClientError")

    asyncio.run(run())


def test_data_looks_empty_requires_all_empty() -> None:
    empty = {k: [] for k in ("schedule", "grades", "events", "homework", "absences", "messages", "letters")}
    assert SchulmanagerCog._data_looks_empty(empty) is True
    # A schedule alone means the session is alive.
    assert SchulmanagerCog._data_looks_empty({**empty, "schedule": [{"date": "2026-01-01"}]}) is False


def test_requires_relogin_only_on_401() -> None:
    assert SchulmanagerCog._requires_relogin(ApiClientError("egal", status_code=401)) is True
    assert SchulmanagerCog._requires_relogin(ApiClientError("boom", status_code=502)) is False
    assert SchulmanagerCog._requires_relogin(ValueError("x")) is False


def _make_state(**over) -> UserWorkspaceState:
    base = dict(
        guild_id=1, user_id=2, email="a@b.de", password="pw", student_id="s1",
        student_name="Max", account_id="acc", access_token="at", refresh_token="rt",
        access_expires_at=0, refresh_expires_at=0, category_id=10,
        status_channel_id=11, schedule_feed_channel_id=12, schedule_week_channel_id=13,
        homework_channel_id=14, grades_channel_id=15, events_channel_id=16,
        webhooks_channel_id=17, absences_channel_id=18, messages_channel_id=19,
        letters_channel_id=20, active=True, last_sync_ts=0, last_error=None, last_digest_date=None,
    )
    base.update(over)
    return UserWorkspaceState(**base)


def test_storage_roundtrips_letters_channel(tmp_path) -> None:
    async def run() -> None:
        store = DiscordStateStore(str(tmp_path / "bot.sqlite3"))
        await store.initialize()
        await store.upsert_user(_make_state(letters_channel_id=999))
        got = await store.get_user(1, 2)
        assert got is not None
        assert got.letters_channel_id == 999
        # Purge helper runs without error on an empty dedup set.
        await store.purge_old_dedup(90)

    asyncio.run(run())
