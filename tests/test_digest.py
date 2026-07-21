"""Tests for the daily digest: one message per day, edited in place instead of reposted."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import discord

from schulmanager_discord_bot.bot import DIGEST_KIND, SchulmanagerCog, _digest_fingerprint
from schulmanager_discord_bot.models import UserWorkspaceState
from schulmanager_discord_bot.storage import DiscordStateStore


def _make_state(**over) -> UserWorkspaceState:
    base = dict(
        guild_id=1, user_id=2, email="a@b.de", password="pw", student_id="s1", student_name="Max",
        account_id="acc", access_token="at", refresh_token="rt", access_expires_at=0, refresh_expires_at=0,
        category_id=None, status_channel_id=None, schedule_feed_channel_id=None, schedule_week_channel_id=None,
        homework_channel_id=None, grades_channel_id=None, events_channel_id=None, webhooks_channel_id=None,
        absences_channel_id=None, messages_channel_id=None, letters_channel_id=None,
        active=True, last_sync_ts=0, last_error=None, last_digest_date=None, dashboard_thread_id=555,
    )
    base.update(over)
    return UserWorkspaceState(**base)


# ─── Root cause: upsert_user must not clobber last_digest_date ────────────────


def test_upsert_user_does_not_clobber_digest_date(tmp_path) -> None:
    """The bug behind the hourly repost.

    A sync reads the user row, spends a long time on network I/O, then writes the whole
    row back. If that write echoed the stale last_digest_date, a digest posted meanwhile
    was forgotten and posted again on the next tick -- over and over, all day.
    """
    async def run() -> None:
        store = DiscordStateStore(str(tmp_path / "bot.sqlite3"))
        await store.initialize()
        stale = _make_state(last_digest_date=None)
        await store.upsert_user(stale)

        # digest_loop marks today as done...
        await store.update_digest_date(1, 2, "2026-07-21")
        # ...while a sync that started earlier writes back its stale snapshot.
        await store.upsert_user(replace(stale, access_token="rotated"))

        got = await store.get_user(1, 2)
        assert got is not None
        assert got.access_token == "rotated"  # the sync's own fields still land
        assert got.last_digest_date == "2026-07-21"  # but the digest date survives

    asyncio.run(run())


def test_update_digest_date_is_the_only_writer(tmp_path) -> None:
    """Fresh logins still start with no digest date, and update_digest_date sets it."""
    async def run() -> None:
        store = DiscordStateStore(str(tmp_path / "bot.sqlite3"))
        await store.initialize()
        await store.upsert_user(_make_state())
        assert (await store.get_user(1, 2)).last_digest_date is None
        await store.update_digest_date(1, 2, "2026-07-21")
        assert (await store.get_user(1, 2)).last_digest_date == "2026-07-21"

    asyncio.run(run())


# ─── Fingerprint gating ───────────────────────────────────────────────────────


def test_digest_fingerprint_is_stable_and_content_sensitive() -> None:
    lessons = [{"start_time": "08:00", "subject": "Mathe", "change_type": ""}]
    hw = [{"subject": "Mathe", "text": "S. 42"}]
    args = ("2026-07-21", "Max", lessons, hw, [], [])

    assert _digest_fingerprint(*args) == _digest_fingerprint(*args)  # stable
    # A cancellation appearing must change the fingerprint so the message gets edited.
    changed = [{"start_time": "08:00", "subject": "Mathe", "change_type": "cancellation"}]
    assert _digest_fingerprint("2026-07-21", "Max", changed, hw, [], []) != _digest_fingerprint(*args)
    # A new day is a different message.
    assert _digest_fingerprint("2026-07-22", "Max", lessons, hw, [], []) != _digest_fingerprint(*args)
    # The student name is rendered in the embed author line, so it must be covered too.
    assert _digest_fingerprint("2026-07-21", "Moritz", lessons, hw, [], []) != _digest_fingerprint(*args)


def test_digest_survives_null_lessons(tmp_path) -> None:
    """A schedule day can carry "lessons": null -- that must not crash the digest."""
    async def run() -> None:
        store = DiscordStateStore(str(tmp_path / "bot.sqlite3"))
        await store.initialize()
        state = _make_state()
        await store.upsert_user(state)
        thread = _FakeThread()
        cog = _make_cog(store, thread)

        data = {"schedule": [{"date": _today(), "lessons": None}], "homework": [], "exams": [], "letters": []}
        assert await cog._send_digest(object(), state, data=data) is True
        assert len(thread.sends) == 1

    asyncio.run(run())


# ─── Edit-in-place behaviour ──────────────────────────────────────────────────


class _FakeMessage:
    def __init__(self, message_id: int, thread: "_FakeThread") -> None:
        self.id = message_id
        self._thread = thread

    async def edit(self, **kwargs) -> None:
        self._thread.edits.append(kwargs.get("embed"))


class _FakeThread:
    """Records sends vs edits so a test can tell reposting from updating."""

    def __init__(self, thread_id: int = 555) -> None:
        self.id = thread_id
        self.sends: list[discord.Embed] = []
        self.edits: list[discord.Embed] = []
        self._next_id = 1000

    async def send(self, *, embed: discord.Embed) -> _FakeMessage:
        self.sends.append(embed)
        self._next_id += 1
        return _FakeMessage(self._next_id, self)

    def get_partial_message(self, message_id: int) -> _FakeMessage:
        return _FakeMessage(message_id, self)


def _make_cog(store: DiscordStateStore, thread: _FakeThread) -> SchulmanagerCog:
    cog = SchulmanagerCog.__new__(SchulmanagerCog)  # bypass __init__ (needs a live bot)
    cog.store = store
    cog.settings = SimpleNamespace(discord_timezone="Europe/Berlin", discord_digest_enabled=True)

    async def _get_thread(guild, thread_id):  # noqa: ANN001
        return thread if thread_id else None

    cog._get_thread = _get_thread  # type: ignore[method-assign]
    return cog


def _payload(hw_text: str = "S. 42") -> dict:
    return {
        "schedule": [{"date": _today(), "lessons": [{"start_time": "08:00", "subject": "Mathe"}]}],
        "homework": [{"subject": "Mathe", "text": hw_text, "due_date": _today(), "done": False}],
        "exams": [],
        "letters": [],
    }


def _today() -> str:
    from datetime import datetime

    from schulmanager_discord_bot.embeds import resolve_timezone

    return datetime.now(resolve_timezone("Europe/Berlin")).date().isoformat()


def test_digest_posts_once_then_edits(tmp_path) -> None:
    """The core fix: repeated digest runs must edit one message, never repost."""
    async def run() -> None:
        store = DiscordStateStore(str(tmp_path / "bot.sqlite3"))
        await store.initialize()
        state = _make_state()
        await store.upsert_user(state)
        thread = _FakeThread()
        cog = _make_cog(store, thread)

        # First run of the day posts.
        assert await cog._send_digest(object(), state, data=_payload()) is True
        assert len(thread.sends) == 1 and len(thread.edits) == 0

        # Same content again -> no Discord call at all.
        assert await cog._send_digest(object(), state, data=_payload()) is True
        assert len(thread.sends) == 1 and len(thread.edits) == 0

        # Changed content -> edits the same message, still no repost.
        assert await cog._send_digest(object(), state, data=_payload("S. 43")) is True
        assert len(thread.sends) == 1 and len(thread.edits) == 1

        rec = await store.get_embed_record(1, 2, DIGEST_KIND, _today())
        assert rec is not None and rec.message_id == thread._next_id

    asyncio.run(run())


def test_digest_refresh_does_not_create(tmp_path) -> None:
    """The sync-time refresh must never post a digest before the digest time."""
    async def run() -> None:
        store = DiscordStateStore(str(tmp_path / "bot.sqlite3"))
        await store.initialize()
        state = _make_state()
        await store.upsert_user(state)
        thread = _FakeThread()
        cog = _make_cog(store, thread)

        assert await cog._send_digest(object(), state, data=_payload(), create=False) is False
        assert thread.sends == [] and thread.edits == []

        # Once the loop has created it, refreshes update that message.
        await cog._send_digest(object(), state, data=_payload())
        await cog._send_digest(object(), state, data=_payload("S. 44"), create=False)
        assert len(thread.sends) == 1 and len(thread.edits) == 1

    asyncio.run(run())


def test_digest_reposts_if_message_was_deleted(tmp_path) -> None:
    """If the user deletes the digest message, the next tick posts a fresh one."""
    async def run() -> None:
        store = DiscordStateStore(str(tmp_path / "bot.sqlite3"))
        await store.initialize()
        state = _make_state()
        await store.upsert_user(state)
        thread = _FakeThread()
        cog = _make_cog(store, thread)

        await cog._send_digest(object(), state, data=_payload())
        assert len(thread.sends) == 1

        class _GoneMessage(_FakeMessage):
            async def edit(self, **kwargs) -> None:
                raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "unknown message")

        thread.get_partial_message = lambda mid: _GoneMessage(mid, thread)  # type: ignore[assignment]
        assert await cog._send_digest(object(), state, data=_payload("S. 45")) is True
        assert len(thread.sends) == 2  # re-posted, and only because it was gone

    asyncio.run(run())


def test_refresh_never_resurrects_a_deleted_digest(tmp_path) -> None:
    """If the user deletes the digest, the 120s sync loop must not post it back.

    Only an explicit /digest (create=True) may repair it -- otherwise deleting the
    message would just make it reappear a couple of minutes later.
    """
    async def run() -> None:
        store = DiscordStateStore(str(tmp_path / "bot.sqlite3"))
        await store.initialize()
        state = _make_state()
        await store.upsert_user(state)
        thread = _FakeThread()
        cog = _make_cog(store, thread)

        await cog._send_digest(object(), state, data=_payload())
        assert len(thread.sends) == 1

        class _GoneMessage(_FakeMessage):
            async def edit(self, **kwargs) -> None:
                raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "unknown message")

        thread.get_partial_message = lambda mid: _GoneMessage(mid, thread)  # type: ignore[assignment]

        assert await cog._send_digest(object(), state, data=_payload("neu"), create=False) is False
        assert len(thread.sends) == 1  # still just the original -- nothing resurrected

    asyncio.run(run())


def test_digest_loop_guards_on_digest_date_alone(tmp_path) -> None:
    """Defense in depth: a lost embed record must not re-open the create path.

    If the message was posted but its bookkeeping row failed to write, last_digest_date
    is the remaining guard -- without it the digest would be posted again every tick,
    which is the original bug.
    """
    async def run() -> None:
        store = DiscordStateStore(str(tmp_path / "bot.sqlite3"))
        await store.initialize()
        await store.upsert_user(_make_state())
        await store.update_digest_date(1, 2, _today())

        state = await store.get_user(1, 2)
        assert state is not None
        # No embed record exists (the write failed), yet the day is marked done.
        assert await store.get_embed_record(1, 2, DIGEST_KIND, _today()) is None
        assert state.last_digest_date == _today()  # -> digest_loop's first guard skips

    asyncio.run(run())


def test_digest_missing_thread_is_not_marked_done(tmp_path) -> None:
    """A missing dashboard thread must report failure so the day is not marked done."""
    async def run() -> None:
        store = DiscordStateStore(str(tmp_path / "bot.sqlite3"))
        await store.initialize()
        state = _make_state(dashboard_thread_id=None)
        await store.upsert_user(state)
        cog = _make_cog(store, _FakeThread())
        assert await cog._send_digest(object(), state, data=_payload()) is False

    asyncio.run(run())


def test_stale_digest_records_are_purged(tmp_path) -> None:
    """Yesterday's bookkeeping row is dropped when today's digest is posted."""
    async def run() -> None:
        store = DiscordStateStore(str(tmp_path / "bot.sqlite3"))
        await store.initialize()
        state = _make_state()
        await store.upsert_user(state)
        await store.upsert_embed_record(1, 2, DIGEST_KIND, "2020-01-01", 42, "old")
        cog = _make_cog(store, _FakeThread())

        await cog._send_digest(object(), state, data=_payload())

        keys = {r.item_key for r in await store.list_embed_records(1, 2, DIGEST_KIND)}
        assert keys == {_today()}

    asyncio.run(run())
