"""Forum-based presentation: one private forum channel per user, one thread per section.

Each content section renders to up to 10 embeds in its thread's starter message (edited on
sync via a fingerprint). A pinned Dashboard thread carries a rich summary + action buttons.
"""
from __future__ import annotations

from typing import Any, Callable

import discord

from schulmanager_discord_bot.embeds import (
    RenderedEmbed,
    _fingerprint,
    render_absences,
    render_events,
    render_exams,
    render_grade_stats,
    render_grades,
    render_homework,
    render_learning,
    render_letters,
    render_messages,
    render_payments,
    render_schedule_feed,
    render_schedule_week,
)

# (key, title, emoji) — order defines the thread order in the forum.
SECTIONS: list[tuple[str, str, str]] = [
    ("schedule", "Stundenplan", "📅"),
    ("homework", "Hausaufgaben", "📚"),
    ("grades", "Noten", "📊"),
    ("exams", "Klausuren", "📝"),
    ("events", "Termine", "🗓️"),
    ("absences", "Fehlzeiten", "📋"),
    ("messages", "Nachrichten", "📬"),
    ("letters", "Elternbriefe", "✉️"),
    ("payments", "Zahlungen", "💶"),
    ("learning", "Lernen", "📓"),
]

SECTION_TITLES: dict[str, str] = {key: title for key, title, _ in SECTIONS}
SECTION_EMOJI: dict[str, str] = {key: emoji for key, _, emoji in SECTIONS}
SECTION_KEYS: list[str] = [key for key, _, _ in SECTIONS]


def section_thread_name(section: str) -> str:
    emoji = SECTION_EMOJI.get(section, "")
    title = SECTION_TITLES.get(section, section)
    return f"{emoji} {title}".strip()


# ── Per-section renderers (each takes the full payload dict + tz name) ──────────

def _sec_schedule(data: dict[str, Any], tz: str) -> list[RenderedEmbed]:
    schedule = data.get("schedule") or []
    return render_schedule_feed(schedule, tz) + render_schedule_week(schedule, tz)


def _sec_homework(data: dict[str, Any], tz: str) -> list[RenderedEmbed]:
    return render_homework(data.get("homework") or [], data.get("schedule") or [], tz)


def _sec_grades(data: dict[str, Any], tz: str) -> list[RenderedEmbed]:
    return render_grade_stats(data.get("grade_stats") or {}, tz) + render_grades(data.get("grades") or [], tz)


_SECTION_RENDERERS: dict[str, Callable[[dict[str, Any], str], list[RenderedEmbed]]] = {
    "schedule": _sec_schedule,
    "homework": _sec_homework,
    "grades": _sec_grades,
    "exams": lambda data, tz: render_exams(data.get("exams") or [], tz),
    "events": lambda data, tz: render_events(data.get("events") or [], tz),
    "absences": lambda data, tz: render_absences(data.get("absences") or [], tz),
    "messages": lambda data, tz: render_messages(data.get("messages") or [], tz),
    "letters": lambda data, tz: render_letters(data.get("letters") or [], tz),
    "payments": lambda data, tz: render_payments(data.get("payments") or [], tz),
    "learning": lambda data, tz: render_learning(data.get("learning") or [], tz),
}


def render_section(section: str, data: dict[str, Any], tz: str) -> tuple[list[discord.Embed], str]:
    """Return up to 10 embeds for a section plus a fingerprint of their content."""
    renderer = _SECTION_RENDERERS.get(section)
    rendered = renderer(data, tz) if renderer else []
    embeds = [item.embed for item in rendered][:10]
    fingerprint = _fingerprint([item.fingerprint for item in rendered[:10]])
    if not embeds:
        placeholder = discord.Embed(
            title=section_thread_name(section),
            description="Keine Einträge.",
            color=discord.Color.light_grey(),
        )
        embeds = [placeholder]
        fingerprint = _fingerprint({"empty": section})
    return embeds, fingerprint


# ── Views ──────────────────────────────────────────────────────────────────────

class ForumDashboardView(discord.ui.View):
    """Persistent buttons on the pinned Dashboard thread."""

    def __init__(self, cog: Any) -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Jetzt synchronisieren", emoji="🔄", style=discord.ButtonStyle.primary, custom_id="sm:forum:sync")
    async def sync_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog._forum_dashboard_sync(interaction)

    @discord.ui.button(label="Kalender (DM)", emoji="📅", style=discord.ButtonStyle.secondary, custom_id="sm:forum:calendar")
    async def calendar_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog._forum_dashboard_calendar(interaction)

    @discord.ui.button(label="Threads verwalten", emoji="⚙️", style=discord.ButtonStyle.secondary, custom_id="sm:forum:manage")
    async def manage_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog._forum_dashboard_manage(interaction)


class _SectionToggleButton(discord.ui.Button):
    def __init__(self, cog: Any, section: str, enabled: bool, row: int) -> None:
        super().__init__(
            label=SECTION_TITLES.get(section, section),
            emoji=SECTION_EMOJI.get(section),
            style=discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary,
            row=row,
        )
        self.cog = cog
        self.section = section

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        await self.cog._forum_toggle_section(interaction, self.section)


class SectionManageView(discord.ui.View):
    """Ephemeral view: one green/grey toggle button per section."""

    def __init__(self, cog: Any, enabled_map: dict[str, bool]) -> None:
        super().__init__(timeout=180)
        for index, (key, _title, _emoji) in enumerate(SECTIONS):
            self.add_item(_SectionToggleButton(cog, key, enabled_map.get(key, True), row=index // 5))
