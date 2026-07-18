"""Tests for the forum section config, rendering, and forum storage."""
from __future__ import annotations

import asyncio

from schulmanager_discord_bot.forum import SECTION_KEYS, render_section, section_thread_name
from schulmanager_discord_bot.models import UserWorkspaceState
from schulmanager_discord_bot.storage import DiscordStateStore


def test_render_section_payments() -> None:
    data = {"payments": [{"id": "p1", "title": "Fahrt", "amount": 10.0, "paid": False}]}
    embeds, fp = render_section("payments", data, "Europe/Berlin")
    assert embeds and fp
    assert "Fahrt" in (embeds[0].description or "")


def test_render_section_empty_placeholder() -> None:
    embeds, fp = render_section("absences", {"absences": []}, "Europe/Berlin")
    assert len(embeds) == 1
    assert "Keine" in (embeds[0].description or "")


def test_render_section_caps_at_10_embeds() -> None:
    # 20 messages -> render_messages makes one embed each; section must cap at 10.
    data = {"messages": [
        {"id": f"m{i}", "sender": "A", "subject": f"S{i}", "body_preview": "", "date": "2026-07-10T10:00:00", "read": False, "unread_count": 1}
        for i in range(20)
    ]}
    embeds, _ = render_section("messages", data, "Europe/Berlin")
    assert len(embeds) <= 10


def test_section_thread_name() -> None:
    name = section_thread_name("schedule")
    assert "Stundenplan" in name


def _make_state(**over) -> UserWorkspaceState:
    base = dict(
        guild_id=1, user_id=2, email="a@b.de", password="pw", student_id="s1", student_name="Max",
        account_id="acc", access_token="at", refresh_token="rt", access_expires_at=0, refresh_expires_at=0,
        category_id=None, status_channel_id=None, schedule_feed_channel_id=None, schedule_week_channel_id=None,
        homework_channel_id=None, grades_channel_id=None, events_channel_id=None, webhooks_channel_id=None,
        absences_channel_id=None, messages_channel_id=None, letters_channel_id=None,
        active=True, last_sync_ts=0, last_error=None, last_digest_date=None,
    )
    base.update(over)
    return UserWorkspaceState(**base)


def test_forum_storage_roundtrip(tmp_path) -> None:
    async def run() -> None:
        store = DiscordStateStore(str(tmp_path / "f.sqlite3"))
        await store.initialize()
        await store.upsert_user(_make_state())

        await store.set_forum(1, 2, forum_channel_id=555, dashboard_thread_id=777)
        user = await store.get_user(1, 2)
        assert user is not None and user.forum_channel_id == 555 and user.dashboard_thread_id == 777

        # dashboard-thread reverse lookup
        by_dash = await store.get_user_by_dashboard_thread(1, 777)
        assert by_dash is not None and by_dash.user_id == 2

        # sections default to on; toggling off persists
        await store.upsert_forum_section(1, 2, "schedule", thread_id=1000, enabled=True, fingerprint="fp1")
        rec = await store.get_forum_section(1, 2, "schedule")
        assert rec is not None and rec.thread_id == 1000 and rec.enabled is True

        await store.set_forum_section_enabled(1, 2, "grades", False)
        sections = {s.section: s for s in await store.list_forum_sections(1, 2)}
        assert sections["grades"].enabled is False

    asyncio.run(run())


def test_all_sections_have_titles() -> None:
    for key in SECTION_KEYS:
        assert section_thread_name(key).strip()
