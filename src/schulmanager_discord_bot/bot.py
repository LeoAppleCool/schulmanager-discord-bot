from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import hashlib
import io
import logging
import time
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

from schulmanager_discord_bot.config import Settings
from schulmanager_discord_bot.api_client import ApiClientError, LoginResponse, SchulmanagerApiClient
from schulmanager_discord_bot.embeds import (
    RenderedEmbed,
    _clip,
    _fingerprint,
    compact_rendered,
    render_absences,
    render_events,
    render_grade_stats,
    render_grades,
    render_homework,
    render_homework_item,
    render_letters,
    render_messages,
    render_schedule_feed,
    render_schedule_week,
    resolve_timezone,
)
from schulmanager_discord_bot.models import ReminderRule, UpsertResult, UserWorkspaceState
from schulmanager_discord_bot.storage import DiscordStateStore

LOGGER = logging.getLogger(__name__)

CHANNEL_SPECS: list[tuple[str, str]] = [
    ("status", "00-status"),
    ("schedule_feed", "01-schedule-feed"),
    ("schedule_week", "02-schedule-week"),
    ("homework", "03-homework"),
    ("grades", "04-grades"),
    ("events", "05-events"),
    ("webhooks", "06-webhooks"),
    ("absences", "07-absences"),
    ("messages", "08-messages"),
    ("letters", "09-letters"),
]

CHANNEL_KIND_TO_FIELD: dict[str, str] = {
    "status": "status_channel_id",
    "schedule_feed": "schedule_feed_channel_id",
    "schedule_week": "schedule_week_channel_id",
    "homework": "homework_channel_id",
    "grades": "grades_channel_id",
    "events": "events_channel_id",
    "webhooks": "webhooks_channel_id",
    "absences": "absences_channel_id",
    "messages": "messages_channel_id",
    "letters": "letters_channel_id",
}

EVENTS_PANEL_KIND = "events_panel"
EVENTS_PANEL_KEY = "__next_event_panel__"
STATUS_SYNC_BUTTON_CUSTOM_ID = "sm:status:sync"
HOMEWORK_ITEM_KIND = "homework_item"
GRADE_STATS_KIND = "grade_stats"
GRADE_STATS_KEY = "__grade_stats__"

DONE_EMOJI = "✅"


class StatusSyncButtonView(discord.ui.View):
    def __init__(self, cog: SchulmanagerCog) -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Jetzt synchronisieren",
        style=discord.ButtonStyle.primary,
        emoji="🔄",
        custom_id=STATUS_SYNC_BUTTON_CUSTOM_ID,
    )
    async def sync_button(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await self.cog._handle_status_sync_button(interaction)


class SchulmanagerCog(commands.Cog):
    def __init__(self, bot: commands.Bot, settings: Settings, store: DiscordStateStore, api: SchulmanagerApiClient) -> None:
        self.bot = bot
        self.settings = settings
        self.store = store
        self.api = api
        self._user_locks: dict[tuple[int, int], asyncio.Lock] = {}
        self.sync_loop.change_interval(seconds=max(self.settings.discord_sync_interval_seconds, 30))

    async def cog_load(self) -> None:
        self.bot.add_view(StatusSyncButtonView(self))
        if not self.sync_loop.is_running():
            self.sync_loop.start()
        if not self.reminder_loop.is_running():
            self.reminder_loop.start()
        if self.settings.discord_digest_enabled and not self.digest_loop.is_running():
            self.digest_loop.start()

    async def cog_unload(self) -> None:
        for loop in (self.sync_loop, self.reminder_loop, self.digest_loop):
            if loop.is_running():
                loop.cancel()

    # ─── Slash commands ───────────────────────────────────────────────────────

    @app_commands.command(name="login", description="Login für Schulmanager und private Kategorie erstellen")
    async def login(
        self,
        interaction: discord.Interaction,
        email: str,
        password: str,
        student_id: str | None = None,
    ) -> None:
        if interaction.guild is None or interaction.user is None:
            await interaction.response.send_message("Bitte in einem Server ausführen.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            login_response = await self.api.login(email=email, password=password)
        except ApiClientError as exc:
            await interaction.followup.send(f"Login fehlgeschlagen: {exc}", ephemeral=True)
            return

        if not login_response.student_ids:
            await interaction.followup.send("Keine Schüler in diesem Account gefunden.", ephemeral=True)
            return

        selected_student_id = student_id or login_response.student_ids[0]
        if selected_student_id not in login_response.student_ids:
            student_list = ", ".join(login_response.student_ids)
            await interaction.followup.send(
                f"Student nicht gefunden. Verfügbar: {student_list}",
                ephemeral=True,
            )
            return

        selected_student: dict[str, Any] = {"id": selected_student_id}
        try:
            students = await self.api.get_students(login_response.access_token)
            from_api = self._select_student(students, selected_student_id)
            if from_api is not None:
                selected_student = from_api
        except ApiClientError as exc:
            LOGGER.warning(
                "Students endpoint failed directly after login for guild=%s user=%s: %s",
                interaction.guild.id,
                interaction.user.id,
                exc,
            )

        member = interaction.guild.get_member(interaction.user.id)
        if member is None:
            member = await interaction.guild.fetch_member(interaction.user.id)

        existing = await self.store.get_user(interaction.guild.id, interaction.user.id)
        workspace = await self._ensure_workspace(interaction.guild, member, existing)

        now_ts = int(time.time())
        state = UserWorkspaceState(
            guild_id=interaction.guild.id,
            user_id=interaction.user.id,
            email=email,
            password=password,
            student_id=selected_student_id,
            student_name=self._student_display_name(selected_student),
            account_id=login_response.account_id,
            access_token=login_response.access_token,
            refresh_token=login_response.refresh_token,
            access_expires_at=now_ts + max(login_response.expires_in, 1),
            refresh_expires_at=now_ts + max(login_response.refresh_expires_in, 1),
            category_id=workspace["category_id"],
            status_channel_id=workspace["status"],
            schedule_feed_channel_id=workspace["schedule_feed"],
            schedule_week_channel_id=workspace["schedule_week"],
            homework_channel_id=workspace["homework"],
            grades_channel_id=workspace["grades"],
            events_channel_id=workspace["events"],
            webhooks_channel_id=workspace["webhooks"],
            absences_channel_id=workspace["absences"],
            messages_channel_id=workspace["messages"],
            letters_channel_id=workspace["letters"],
            active=True,
            last_sync_ts=0,
            last_error=None,
            last_digest_date=None,
        )
        await self.store.upsert_user(state)
        await self._manage_logged_in_role(interaction.guild, interaction.user.id, add=True)
        await self._sync_user(state, reason="initial", force_refresh=True)

        category_mention = f"<#{workspace['status']}>"
        await interaction.followup.send(
            (
                "✅ Login erfolgreich. Deine private Kategorie ist bereit. "
                f"Status-Channel: {category_mention}."
            ),
            ephemeral=True,
        )

    @app_commands.command(name="logout", description="Bot-Zugang entfernen")
    async def logout(
        self,
        interaction: discord.Interaction,
        delete_category: bool = False,
    ) -> None:
        if interaction.guild is None or interaction.user is None:
            await interaction.response.send_message("Bitte in einem Server ausführen.", ephemeral=True)
            return

        state = await self.store.get_user(interaction.guild.id, interaction.user.id)
        if state is None:
            await interaction.response.send_message("Kein aktiver Login gefunden.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            await self.api.logout(state.access_token)
        except ApiClientError as exc:
            LOGGER.info("API logout failed for guild=%s user=%s: %s", state.guild_id, state.user_id, exc)

        if delete_category and state.category_id:
            category = interaction.guild.get_channel(state.category_id)
            if isinstance(category, discord.CategoryChannel):
                for channel in list(category.channels):
                    await channel.delete(reason="Schulmanager logout")
                await category.delete(reason="Schulmanager logout")

        await self.store.delete_user(interaction.guild.id, interaction.user.id)
        await self._manage_logged_in_role(interaction.guild, interaction.user.id, add=False)
        await interaction.followup.send("✅ Abgemeldet.", ephemeral=True)

    @app_commands.command(name="sync", description="Manuelle Aktualisierung auslösen")
    async def sync_now(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or interaction.user is None:
            await interaction.response.send_message("Bitte in einem Server ausführen.", ephemeral=True)
            return

        state = await self.store.get_user(interaction.guild.id, interaction.user.id)
        if state is None:
            await interaction.response.send_message("Bitte zuerst /login verwenden.", ephemeral=True)
            return
        if not state.active:
            await interaction.response.send_message("Session ist nicht mehr aktiv. Bitte /login erneut ausführen.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self._sync_user(state, reason="manual", force_refresh=True)
        except Exception as exc:
            await interaction.followup.send(f"Synchronisierung fehlgeschlagen: {exc}", ephemeral=True)
            return
        await interaction.followup.send("✅ Sync abgeschlossen.", ephemeral=True)

    @app_commands.command(name="status", description="Zeigt den Bot-Status für deinen Account")
    async def status(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or interaction.user is None:
            await interaction.response.send_message("Bitte in einem Server ausführen.", ephemeral=True)
            return

        state = await self.store.get_user(interaction.guild.id, interaction.user.id)
        if state is None:
            await interaction.response.send_message("Nicht eingeloggt.", ephemeral=True)
            return

        last_sync = "Nie" if state.last_sync_ts <= 0 else f"<t:{state.last_sync_ts}:R>"
        color = discord.Color.green() if state.active and not state.last_error else (discord.Color.orange() if state.last_error else discord.Color.red())
        embed = discord.Embed(
            title="📡 Bot-Status",
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Schüler", value=f"{state.student_name} (`{state.student_id}`)", inline=False)
        embed.add_field(name="Letzter Sync", value=last_sync, inline=True)
        embed.add_field(name="Aktiv", value="✅ Ja" if state.active else "❌ Nein", inline=True)
        if state.last_error:
            embed.add_field(name="⚠️ Letzter Fehler", value=state.last_error[:200], inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="calendar", description="ICS-Kalender als DM senden")
    async def calendar(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or interaction.user is None:
            await interaction.response.send_message("Bitte in einem Server ausführen.", ephemeral=True)
            return

        state = await self.store.get_user(interaction.guild.id, interaction.user.id)
        if state is None:
            await interaction.response.send_message("Bitte zuerst /login verwenden.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            state = await self._ensure_valid_tokens(state)
            ics_bytes = await self.api.get_calendar_ics(state.access_token, state.student_id)
        except ApiClientError as exc:
            await interaction.followup.send(f"Kalender-Export fehlgeschlagen: {exc}", ephemeral=True)
            return

        try:
            dm_channel = await interaction.user.create_dm()
            file = discord.File(io.BytesIO(ics_bytes), filename=f"{state.student_id}.ics")
            await dm_channel.send(
                f"Dein Schulmanager-Kalender für **{state.student_name}**.\n"
                "Importiere die .ics-Datei in deinen Kalender (Google, Apple, Outlook, …).",
                file=file,
            )
            await interaction.followup.send("Kalender wurde als DM gesendet.", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send("Konnte keine DM senden. Bitte erlaube DMs von Server-Mitgliedern.", ephemeral=True)

    @app_commands.command(name="digest", description="Tages-Zusammenfassung jetzt anzeigen")
    async def digest_now(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or interaction.user is None:
            await interaction.response.send_message("Bitte in einem Server ausführen.", ephemeral=True)
            return

        state = await self.store.get_user(interaction.guild.id, interaction.user.id)
        if state is None:
            await interaction.response.send_message("Bitte zuerst /login verwenden.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        guild = interaction.guild
        try:
            state = await self._ensure_valid_tokens(state)
            await self._send_digest(guild, state)
        except Exception as exc:
            await interaction.followup.send(f"Digest fehlgeschlagen: {exc}", ephemeral=True)
            return

        await interaction.followup.send("Tages-Zusammenfassung wurde gepostet.", ephemeral=True)

    # /remind group
    remind_group = app_commands.Group(name="remind", description="Erinnerungen konfigurieren")

    @remind_group.command(name="exams", description="Prüfungs-Erinnerung X Stunden vorher aktivieren")
    async def remind_exams(self, interaction: discord.Interaction, hours_before: int) -> None:
        await self._set_reminder(interaction, "exam", hours_before)

    @remind_group.command(name="homework", description="Hausaufgaben-Erinnerung X Stunden vorher aktivieren")
    async def remind_homework(self, interaction: discord.Interaction, hours_before: int) -> None:
        await self._set_reminder(interaction, "homework", hours_before)

    @remind_group.command(name="off", description="Erinnerung deaktivieren")
    @app_commands.describe(reminder_type="Typ der zu deaktivierenden Erinnerung")
    @app_commands.choices(reminder_type=[
        app_commands.Choice(name="Klausuren", value="exam"),
        app_commands.Choice(name="Hausaufgaben", value="homework"),
    ])
    async def remind_off(self, interaction: discord.Interaction, reminder_type: str) -> None:
        if interaction.guild is None or interaction.user is None:
            await interaction.response.send_message("Bitte in einem Server ausführen.", ephemeral=True)
            return
        state = await self.store.get_user(interaction.guild.id, interaction.user.id)
        if state is None:
            await interaction.response.send_message("Bitte zuerst /login verwenden.", ephemeral=True)
            return
        await self.store.delete_reminder_rule(interaction.guild.id, interaction.user.id, reminder_type)
        label = "Prüfungs-Erinnerung" if reminder_type == "exam" else "Hausaufgaben-Erinnerung"
        await interaction.response.send_message(f"✅ {label} deaktiviert.", ephemeral=True)

    async def _set_reminder(self, interaction: discord.Interaction, reminder_type: str, hours_before: int) -> None:
        if interaction.guild is None or interaction.user is None:
            await interaction.response.send_message("Bitte in einem Server ausführen.", ephemeral=True)
            return

        state = await self.store.get_user(interaction.guild.id, interaction.user.id)
        if state is None:
            await interaction.response.send_message("Bitte zuerst /login verwenden.", ephemeral=True)
            return

        if hours_before < 1 or hours_before > 168:
            await interaction.response.send_message("Stunden müssen zwischen 1 und 168 liegen.", ephemeral=True)
            return

        rule = ReminderRule(
            guild_id=interaction.guild.id,
            user_id=interaction.user.id,
            reminder_type=reminder_type,
            hours_before=hours_before,
        )
        await self.store.upsert_reminder_rule(rule)
        type_label = "Prüfung" if reminder_type == "exam" else "Hausaufgabe"
        await interaction.response.send_message(
            f"Erinnerung gesetzt: **{type_label}** — {hours_before} Stunden vorher als DM.",
            ephemeral=True,
        )

    # /notify group
    notify_group = app_commands.Group(name="notify", description="Benachrichtigungen konfigurieren")

    @notify_group.command(name="schedule-changes", description="DM bei Stundenplanänderungen (Ausfall/Vertretung)")
    @app_commands.describe(enabled="An oder Aus")
    @app_commands.choices(enabled=[
        app_commands.Choice(name="An", value=1),
        app_commands.Choice(name="Aus", value=0),
    ])
    async def notify_schedule_changes(self, interaction: discord.Interaction, enabled: int) -> None:
        await self._set_notification_pref(interaction, "schedule_changes", bool(enabled))

    @notify_group.command(name="digest", description="Tägliche Zusammenfassung im Status-Channel")
    @app_commands.describe(enabled="An oder Aus")
    @app_commands.choices(enabled=[
        app_commands.Choice(name="An", value=1),
        app_commands.Choice(name="Aus", value=0),
    ])
    async def notify_digest(self, interaction: discord.Interaction, enabled: int) -> None:
        await self._set_notification_pref(interaction, "digest", bool(enabled))

    @notify_group.command(name="letters", description="DM bei neuen Elternbriefen mit Bestätigungspflicht")
    @app_commands.describe(enabled="An oder Aus")
    @app_commands.choices(enabled=[
        app_commands.Choice(name="An", value=1),
        app_commands.Choice(name="Aus", value=0),
    ])
    async def notify_letters(self, interaction: discord.Interaction, enabled: int) -> None:
        await self._set_notification_pref(interaction, "letters", bool(enabled))

    @notify_group.command(name="status", description="Aktuelle Benachrichtigungs-Einstellungen anzeigen")
    async def notify_status(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or interaction.user is None:
            await interaction.response.send_message("Bitte in einem Server ausführen.", ephemeral=True)
            return
        state = await self.store.get_user(interaction.guild.id, interaction.user.id)
        if state is None:
            await interaction.response.send_message("Bitte zuerst /login verwenden.", ephemeral=True)
            return
        prefs = await self.store.list_notification_prefs(interaction.guild.id, interaction.user.id)
        embed = discord.Embed(title="🔔 Benachrichtigungs-Einstellungen", color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))
        defaults = {"schedule_changes": True, "digest": True, "letters": True}
        labels = {"schedule_changes": "Stundenplan-Änderungen", "digest": "Tages-Digest", "letters": "Elternbriefe (Bestätigung)"}
        for key, default in defaults.items():
            val = prefs.get(key, default)
            embed.add_field(name=labels.get(key, key), value="✅ An" if val else "❌ Aus", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _set_notification_pref(self, interaction: discord.Interaction, pref_key: str, enabled: bool) -> None:
        if interaction.guild is None or interaction.user is None:
            await interaction.response.send_message("Bitte in einem Server ausführen.", ephemeral=True)
            return
        state = await self.store.get_user(interaction.guild.id, interaction.user.id)
        if state is None:
            await interaction.response.send_message("Bitte zuerst /login verwenden.", ephemeral=True)
            return
        await self.store.set_notification_pref(interaction.guild.id, interaction.user.id, pref_key, enabled)
        status = "aktiviert" if enabled else "deaktiviert"
        await interaction.response.send_message(f"✅ **{pref_key}** {status}.", ephemeral=True)

    @app_commands.command(name="info", description="Zeigt allgemeine Bot-Informationen")
    async def info(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or interaction.user is None:
            await interaction.response.send_message("Bitte in einem Server ausführen.", ephemeral=True)
            return

        state = await self.store.get_user(interaction.guild.id, interaction.user.id)
        next_sync = self.sync_loop.next_iteration
        next_sync_text = (
            f"<t:{int(next_sync.timestamp())}:R>"
            if isinstance(next_sync, datetime)
            else "unbekannt"
        )

        embed = discord.Embed(title="ℹ️ Schulmanager Bot", color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))
        embed.add_field(name="API", value=self.settings.discord_api_base_url, inline=False)
        embed.add_field(name="Sync-Intervall", value=f"{self.settings.discord_sync_interval_seconds}s", inline=True)
        embed.add_field(name="Zeitzone", value=self.settings.discord_timezone, inline=True)
        embed.add_field(name="Nächster Auto-Sync", value=next_sync_text, inline=True)
        if state is not None:
            embed.add_field(name="Eingeloggt als", value=f"{state.student_name} (`{state.student_id}`)", inline=False)
            embed.add_field(name="Aktiv", value="✅ Ja" if state.active else "❌ Nein", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="channels", description="Zeigt deine Schulmanager-Channel")
    async def channels(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or interaction.user is None:
            await interaction.response.send_message("Bitte in einem Server ausführen.", ephemeral=True)
            return

        state = await self.store.get_user(interaction.guild.id, interaction.user.id)
        if state is None:
            await interaction.response.send_message("Bitte zuerst /login verwenden.", ephemeral=True)
            return

        channel_items = [
            ("status", state.status_channel_id),
            ("schedule-feed", state.schedule_feed_channel_id),
            ("schedule-week", state.schedule_week_channel_id),
            ("homework", state.homework_channel_id),
            ("grades", state.grades_channel_id),
            ("events", state.events_channel_id),
            ("webhooks", state.webhooks_channel_id),
            ("absences", state.absences_channel_id),
            ("messages", state.messages_channel_id),
            ("letters", state.letters_channel_id),
        ]
        lines = [f"Kategorie: <#{state.category_id}>" if state.category_id else "Kategorie: -"]
        lines.extend(f"{name}: <#{channel_id}>" if channel_id else f"{name}: -" for name, channel_id in channel_items)
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @app_commands.command(name="debug-state", description="Debug-Infos für deinen Account")
    async def debug_state(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or interaction.user is None:
            await interaction.response.send_message("Bitte in einem Server ausführen.", ephemeral=True)
            return

        state = await self.store.get_user(interaction.guild.id, interaction.user.id)
        if state is None:
            await interaction.response.send_message("Bitte zuerst /login verwenden.", ephemeral=True)
            return

        now_ts = int(time.time())
        lines = [
            f"guild_id={state.guild_id}",
            f"user_id={state.user_id}",
            f"account_id={state.account_id}",
            f"student_id={state.student_id}",
            f"active={state.active}",
            f"last_sync={state.last_sync_ts}",
            f"access_exp_in={state.access_expires_at - now_ts}s",
            f"refresh_exp_in={state.refresh_expires_at - now_ts}s",
            f"last_error={state.last_error or '-'}",
        ]
        try:
            me_payload = await self.api.get_me(state.access_token)
            lines.append(f"/auth/me ok: account_id={me_payload.get('account_id', '-')}")
        except ApiClientError as exc:
            lines.append(f"/auth/me failed: {exc}")

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @app_commands.command(name="debug-webhook", description="Testnachricht in deinen Webhook-Channel senden")
    async def debug_webhook(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or interaction.user is None:
            await interaction.response.send_message("Bitte in einem Server ausführen.", ephemeral=True)
            return

        state = await self.store.get_user(interaction.guild.id, interaction.user.id)
        if state is None:
            await interaction.response.send_message("Bitte zuerst /login verwenden.", ephemeral=True)
            return

        channel = self._get_channel(interaction.guild, state.webhooks_channel_id)
        if channel is None:
            await interaction.response.send_message("Webhook-Channel nicht gefunden.", ephemeral=True)
            return

        embed = discord.Embed(
            title="Debug-Webhooks Test",
            color=discord.Color.orange(),
            description=f"Student: {state.student_name}\nZeit: <t:{int(time.time())}:F>",
            timestamp=datetime.now(timezone.utc),
        )
        await channel.send(embed=embed)
        await interaction.response.send_message("Testnachricht gesendet.", ephemeral=True)

    @app_commands.command(name="admin-users", description="(Admin) Zeigt alle Bot-User im Server")
    async def admin_users(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or interaction.user is None:
            await interaction.response.send_message("Bitte in einem Server ausführen.", ephemeral=True)
            return
        if not self._is_admin_interaction(interaction):
            await interaction.response.send_message("Nur für Server-Admins.", ephemeral=True)
            return

        users = await self.store.list_users_for_guild(interaction.guild.id)
        if not users:
            await interaction.response.send_message("Keine Bot-User gespeichert.", ephemeral=True)
            return

        lines: list[str] = []
        for row in users[:25]:
            member = interaction.guild.get_member(row.user_id)
            display = member.display_name if member else str(row.user_id)
            marker = "✅ aktiv" if row.active else "❌ inaktiv"
            lines.append(f"- {display} (`{row.user_id}`) | {marker} | `{row.student_id}`")

        embed = discord.Embed(title="👥 Bot-Nutzer", color=discord.Color.dark_teal(), timestamp=datetime.now(timezone.utc))
        embed.description = "\n".join(lines) or "Keine Einträge."
        embed.set_footer(text=f"{len(users)} Nutzer gesamt")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="admin-sync-all", description="(Admin) Sync für alle aktiven Bot-User")
    async def admin_sync_all(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or interaction.user is None:
            await interaction.response.send_message("Bitte in einem Server ausführen.", ephemeral=True)
            return
        if not self._is_admin_interaction(interaction):
            await interaction.response.send_message("Nur für Server-Admins.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        users = await self.store.list_users_for_guild(interaction.guild.id)
        active_users = [row for row in users if row.active]

        ok = 0
        failed = 0
        errors: list[str] = []
        for row in active_users:
            try:
                await self._sync_user(row, reason="admin-bulk", force_refresh=True)
                ok += 1
            except Exception as exc:
                failed += 1
                errors.append(f"{row.user_id}: {exc}")

        status_color = discord.Color.green() if failed == 0 else discord.Color.orange()
        embed = discord.Embed(title="🔄 Bulk-Sync", color=status_color, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Aktive Nutzer", value=str(len(active_users)), inline=True)
        embed.add_field(name="✅ Erfolgreich", value=str(ok), inline=True)
        embed.add_field(name="❌ Fehlgeschlagen", value=str(failed), inline=True)
        if errors:
            embed.add_field(name="Fehler", value="\n".join(f"• {e}" for e in errors[:5]), inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="admin-user-active", description="(Admin) Aktiv/Inaktiv für einen Bot-User setzen")
    async def admin_user_active(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        active: bool,
    ) -> None:
        if interaction.guild is None or interaction.user is None:
            await interaction.response.send_message("Bitte in einem Server ausführen.", ephemeral=True)
            return
        if not self._is_admin_interaction(interaction):
            await interaction.response.send_message("Nur für Server-Admins.", ephemeral=True)
            return

        state = await self.store.get_user(interaction.guild.id, user.id)
        if state is None:
            await interaction.response.send_message("Fuer diesen User gibt es keinen Bot-Login.", ephemeral=True)
            return

        await self.store.set_active(interaction.guild.id, user.id, active)
        await interaction.response.send_message(f"{user.display_name}: active={active}", ephemeral=True)

    @app_commands.command(name="admin-errors", description="(Admin) Letzte Sync-Fehler aller Bot-User")
    async def admin_errors(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or interaction.user is None:
            await interaction.response.send_message("Bitte in einem Server ausführen.", ephemeral=True)
            return
        if not self._is_admin_interaction(interaction):
            await interaction.response.send_message("Nur für Server-Admins.", ephemeral=True)
            return

        users = await self.store.list_users_for_guild(interaction.guild.id)
        error_users = [u for u in users if u.last_error]
        if not error_users:
            await interaction.response.send_message("Keine Sync-Fehler vorhanden.", ephemeral=True)
            return

        lines: list[str] = []
        for u in error_users[:10]:
            member = interaction.guild.get_member(u.user_id)
            display = member.display_name if member else str(u.user_id)
            error_short = (u.last_error or "")[:80]
            lines.append(f"**{display}**: {error_short}")
        embed = discord.Embed(title="⚠️ Sync-Fehler", color=discord.Color.red(), timestamp=datetime.now(timezone.utc))
        embed.description = "\n".join(lines)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="admin-stats", description="(Admin) Bot-Statistiken anzeigen")
    async def admin_stats(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or interaction.user is None:
            await interaction.response.send_message("Bitte in einem Server ausführen.", ephemeral=True)
            return
        if not self._is_admin_interaction(interaction):
            await interaction.response.send_message("Nur für Server-Admins.", ephemeral=True)
            return

        users = await self.store.list_users_for_guild(interaction.guild.id)
        active_count = sum(1 for u in users if u.active)
        error_count = sum(1 for u in users if u.last_error)
        next_sync = self.sync_loop.next_iteration
        next_sync_text = (
            f"<t:{int(next_sync.timestamp())}:R>"
            if isinstance(next_sync, datetime)
            else "unbekannt"
        )

        embed = discord.Embed(
            title="Bot-Statistiken",
            color=discord.Color.dark_teal(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Registrierte User", value=str(len(users)), inline=True)
        embed.add_field(name="Aktive User", value=str(active_count), inline=True)
        embed.add_field(name="User mit Fehlern", value=str(error_count), inline=True)
        embed.add_field(name="Nächster Auto-Sync", value=next_sync_text, inline=True)
        embed.add_field(name="Sync-Intervall", value=f"{self.settings.discord_sync_interval_seconds}s", inline=True)
        embed.add_field(name="API-URL", value=self.settings.discord_api_base_url, inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="admin-purge", description="(Admin) User-Workspace und Daten vollständig entfernen")
    async def admin_purge(self, interaction: discord.Interaction, user: discord.Member) -> None:
        if interaction.guild is None or interaction.user is None:
            await interaction.response.send_message("Bitte in einem Server ausführen.", ephemeral=True)
            return
        if not self._is_admin_interaction(interaction):
            await interaction.response.send_message("Nur für Server-Admins.", ephemeral=True)
            return

        state = await self.store.get_user(interaction.guild.id, user.id)
        if state is None:
            await interaction.response.send_message("Kein Bot-Eintrag für diesen User.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        if state.category_id:
            category = interaction.guild.get_channel(state.category_id)
            if isinstance(category, discord.CategoryChannel):
                for ch in list(category.channels):
                    try:
                        await ch.delete(reason=f"Admin purge by {interaction.user}")
                    except discord.NotFound:
                        pass
                try:
                    await category.delete(reason=f"Admin purge by {interaction.user}")
                except discord.NotFound:
                    pass

        await self.store.delete_user(interaction.guild.id, user.id)
        await interaction.followup.send(f"User **{user.display_name}** vollständig entfernt.", ephemeral=True)

    @app_commands.command(name="admin-flush-cache", description="(Admin) API-Cache leeren")
    async def admin_flush_cache(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or interaction.user is None:
            await interaction.response.send_message("Bitte in einem Server ausführen.", ephemeral=True)
            return
        if not self._is_admin_interaction(interaction):
            await interaction.response.send_message("Nur für Server-Admins.", ephemeral=True)
            return

        state = await self.store.get_user(interaction.guild.id, interaction.user.id)
        if state is None:
            await interaction.response.send_message("Bitte zuerst /login verwenden.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            state = await self._ensure_valid_tokens(state)
            await self.api.flush_cache(state.access_token)
            await interaction.followup.send("✅ API-Cache wurde geleert.", ephemeral=True)
        except ApiClientError as exc:
            await interaction.followup.send(f"Fehler beim Leeren des Caches: {exc}", ephemeral=True)

    # ─── Reaction handlers ────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.user_id == self.bot.user.id:  # type: ignore[union-attr]
            return
        if str(payload.emoji) != DONE_EMOJI:
            return
        await self._handle_homework_reaction(payload, done=True)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.user_id == self.bot.user.id:  # type: ignore[union-attr]
            return
        if str(payload.emoji) != DONE_EMOJI:
            return
        await self._handle_homework_reaction(payload, done=False)

    async def _handle_homework_reaction(
        self,
        payload: discord.RawReactionActionEvent,
        *,
        done: bool,
    ) -> None:
        if payload.guild_id is None:
            return

        record = await self.store.get_embed_record_by_message_id(
            payload.guild_id, HOMEWORK_ITEM_KIND, payload.message_id
        )
        if record is None:
            return

        if payload.user_id != record.user_id:
            return

        await self.store.set_homework_done(payload.guild_id, payload.user_id, record.item_key, done)

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        state = await self.store.get_user(payload.guild_id, payload.user_id)
        if state is None:
            return

        channel = self._get_channel(guild, state.homework_channel_id)
        if channel is None:
            return

        try:
            message = await channel.fetch_message(payload.message_id)
        except discord.NotFound:
            return

        try:
            state = await self._ensure_valid_tokens(state)
            tz = resolve_timezone(self.settings.discord_timezone)
            today = datetime.now(tz).date()
            to_date = today + timedelta(days=21)
            homework_raw = await self.api.get_homework(state.access_token, state.student_id, open_only=False, force_refresh=False)
            schedule_raw = await self.api.get_schedule(state.access_token, state.student_id, today, to_date, force_refresh=False)

            item = next((hw for hw in homework_raw if str(hw.get("id")) == record.item_key), None)
            if item is None:
                return

            rendered = render_homework_item(item, schedule_raw, self.settings.discord_timezone, done=done)
            await message.edit(embed=rendered.embed)
            await self.store.upsert_embed_record(
                payload.guild_id,
                payload.user_id,
                HOMEWORK_ITEM_KIND,
                record.item_key,
                message.id,
                rendered.fingerprint,
            )
        except Exception as exc:
            LOGGER.warning(
                "Failed to update homework reaction for guild=%s user=%s item=%s: %s",
                payload.guild_id,
                payload.user_id,
                record.item_key,
                exc,
            )

    # ─── Background tasks ─────────────────────────────────────────────────────

    @tasks.loop(seconds=120)
    async def sync_loop(self) -> None:
        users = await self.store.list_active_users()
        if not users:
            return

        for state in users:
            try:
                await self._sync_user(state, reason="auto", force_refresh=False)
            except Exception as exc:  # pragma: no cover
                LOGGER.exception("Auto sync failed for guild=%s user=%s: %s", state.guild_id, state.user_id, exc)

    @sync_loop.before_loop
    async def before_sync_loop(self) -> None:
        await self.bot.wait_until_ready()
        await self._relogin_all_users_on_startup()

    async def _relogin_all_users_on_startup(self) -> None:
        """Beim Start alle aktiven User neu in die API einloggen um die Selenium-Session wiederherzustellen."""
        users = await self.store.list_active_users()
        if not users:
            return

        LOGGER.info("Startup relogin: %d aktive User werden neu eingeloggt", len(users))
        for state in users:
            if not state.password:
                LOGGER.warning(
                    "Startup relogin: kein Passwort gespeichert für guild=%s user=%s – übersprungen",
                    state.guild_id, state.user_id,
                )
                continue
            try:
                login_response = await self.api.login(email=state.email, password=state.password)
                now_ts = int(time.time())
                updated = replace(
                    state,
                    account_id=login_response.account_id,
                    access_token=login_response.access_token,
                    refresh_token=login_response.refresh_token,
                    access_expires_at=now_ts + max(login_response.expires_in, 1),
                    refresh_expires_at=now_ts + max(login_response.refresh_expires_in, 1),
                    active=True,
                    last_error=None,
                )
                await self.store.upsert_user(updated)
                guild = self.bot.get_guild(state.guild_id)
                if guild:
                    await self._manage_logged_in_role(guild, state.user_id, add=True)
                LOGGER.info(
                    "Startup relogin erfolgreich für guild=%s user=%s",
                    state.guild_id, state.user_id,
                )
            except ApiClientError as exc:
                LOGGER.warning(
                    "Startup relogin fehlgeschlagen für guild=%s user=%s: %s",
                    state.guild_id, state.user_id, exc,
                )

    @tasks.loop(minutes=5)
    async def reminder_loop(self) -> None:
        rules = await self.store.list_all_reminder_rules()
        if not rules:
            return

        for rule in rules:
            try:
                await self._process_reminder(rule)
            except Exception as exc:
                LOGGER.warning(
                    "Reminder processing failed for guild=%s user=%s type=%s: %s",
                    rule.guild_id,
                    rule.user_id,
                    rule.reminder_type,
                    exc,
                )

    @reminder_loop.before_loop
    async def before_reminder_loop(self) -> None:
        await self.bot.wait_until_ready()
        try:
            await self.store.purge_old_dedup(90)
        except Exception as exc:  # pragma: no cover - best-effort maintenance
            LOGGER.warning("Dedup purge on startup failed: %s", exc)

    @tasks.loop(minutes=30)
    async def digest_loop(self) -> None:
        if not self.settings.discord_digest_enabled:
            return

        tz = resolve_timezone(self.settings.discord_timezone)
        now = datetime.now(tz)
        try:
            digest_hour, digest_minute = map(int, self.settings.discord_digest_time.split(":")[:2])
        except (ValueError, AttributeError):
            digest_hour, digest_minute = 7, 0

        # Fire once per day, at or after the configured time. The per-user last_digest_date guard
        # below prevents duplicates and lets a missed window (bot downtime) catch up the same day.
        if (now.hour, now.minute) < (digest_hour, digest_minute):
            return

        today_str = now.date().isoformat()
        users = await self.store.list_active_users()
        for state in users:
            if state.last_digest_date == today_str:
                continue
            digest_pref = await self.store.get_notification_pref(state.guild_id, state.user_id, "digest", default=True)
            if not digest_pref:
                continue
            guild = self.bot.get_guild(state.guild_id)
            if guild is None:
                continue
            try:
                state = await self._ensure_valid_tokens(state)
                await self._send_digest(guild, state)
                await self.store.update_digest_date(state.guild_id, state.user_id, today_str)
            except Exception as exc:
                LOGGER.warning("Digest failed for guild=%s user=%s: %s", state.guild_id, state.user_id, exc)

    @digest_loop.before_loop
    async def before_digest_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _process_schedule_change_dms(
        self, state: UserWorkspaceState, schedule: list[dict[str, Any]]
    ) -> None:
        pref_enabled = await self.store.get_notification_pref(
            state.guild_id, state.user_id, "schedule_changes", default=True
        )
        if not pref_enabled:
            return

        change_types = {"cancellation", "substitution", "room_change"}
        tz = resolve_timezone(self.settings.discord_timezone)

        for day_raw in schedule:
            if not isinstance(day_raw, dict):
                continue
            day_date_str = str(day_raw.get("date") or "")
            lessons = day_raw.get("lessons") or []
            for lesson in lessons:
                if not isinstance(lesson, dict):
                    continue
                ct = str(lesson.get("change_type") or "")
                if ct not in change_types:
                    continue

                start_time = str(lesson.get("start_time") or "")
                subject = str(lesson.get("subject") or "Fach")
                change_key = f"{day_date_str}:{start_time}:{subject}:{ct}"

                if await self.store.has_seen_schedule_change(state.guild_id, state.user_id, change_key):
                    continue

                await self.store.mark_schedule_change_seen(state.guild_id, state.user_id, change_key)

                icon_map = {"cancellation": "❌", "substitution": "🔄", "room_change": "🏫"}
                icon = icon_map.get(ct, "⚠️")
                label_map = {"cancellation": "Ausfall", "substitution": "Vertretung", "room_change": "Raumwechsel"}
                label = label_map.get(ct, "Änderung")

                note = str(lesson.get("note") or "").strip()
                room = str(lesson.get("room") or "").strip()
                teacher = str(lesson.get("teacher") or "").strip()

                desc_lines = [
                    f"Fach: **{subject}**",
                    f"Zeit: **{start_time}** Uhr",
                    f"Datum: **{day_date_str}**",
                ]
                if teacher:
                    desc_lines.append(f"Lehrer: {teacher}")
                if room:
                    desc_lines.append(f"Raum: {room}")
                if note:
                    desc_lines.append(f"Hinweis: {note}")

                embed = discord.Embed(
                    title=f"{icon} Stundenplan-Änderung: {label}",
                    color=discord.Color.orange(),
                    description="\n".join(desc_lines),
                    timestamp=datetime.now(tz),
                )
                embed.set_footer(text=state.student_name)

                user = self.bot.get_user(state.user_id)
                if user is None:
                    try:
                        user = await self.bot.fetch_user(state.user_id)
                    except discord.NotFound:
                        continue

                try:
                    await user.send(embed=embed)
                except discord.Forbidden:
                    pass

    async def _process_letter_notifications(
        self, state: UserWorkspaceState, letters: list[dict[str, Any]]
    ) -> None:
        """DM the user about new unread Elternbriefe that require a confirmation (Lesebestätigung)."""
        pref_enabled = await self.store.get_notification_pref(
            state.guild_id, state.user_id, "letters", default=True
        )
        if not pref_enabled:
            return

        tz = resolve_timezone(self.settings.discord_timezone)
        for letter in letters:
            if not isinstance(letter, dict):
                continue
            if letter.get("read") or not letter.get("requires_confirmation"):
                continue
            letter_id = str(letter.get("id") or "")
            if not letter_id:
                continue
            # Reuse the reminder_sent dedup table (reminder_type="letter").
            if await self.store.has_sent_reminder(state.guild_id, state.user_id, letter_id, "letter"):
                continue

            title = str(letter.get("title") or "Elternbrief")
            sender = str(letter.get("sender") or "").strip()
            desc = f"**{title}**"
            if sender:
                desc += f"\nvon {sender}"
            desc += "\n\n⚠️ Dieser Elternbrief erfordert eine Bestätigung. Bitte im Schulmanager bestätigen."

            embed = discord.Embed(
                title="✉️ Neuer Elternbrief",
                color=discord.Color.gold(),
                description=desc,
                timestamp=datetime.now(tz),
            )
            embed.set_footer(text=state.student_name)

            user = self.bot.get_user(state.user_id)
            if user is None:
                try:
                    user = await self.bot.fetch_user(state.user_id)
                except discord.NotFound:
                    continue
            try:
                await user.send(embed=embed)
                await self.store.mark_reminder_sent(state.guild_id, state.user_id, letter_id, "letter")
            except discord.Forbidden:
                pass

    async def _process_reminder(self, rule: ReminderRule) -> None:
        state = await self.store.get_user(rule.guild_id, rule.user_id)
        if state is None or not state.active:
            return

        state = await self._ensure_valid_tokens(state)
        now = datetime.now(timezone.utc)
        threshold = now + timedelta(hours=rule.hours_before)

        user = self.bot.get_user(rule.user_id)
        if user is None:
            try:
                user = await self.bot.fetch_user(rule.user_id)
            except discord.NotFound:
                return

        tz = resolve_timezone(self.settings.discord_timezone)
        if rule.reminder_type == "exam":
            items = await self.api.get_exams(state.access_token, state.student_id, force_refresh=False)
            for item in items:
                item_date_str = str(item.get("date") or "")
                if not item_date_str:
                    continue
                try:
                    item_date = date.fromisoformat(item_date_str)
                except ValueError:
                    continue
                # Anchor at 08:00 school-local time (not UTC) so the reminder window is correct.
                item_dt = datetime(item_date.year, item_date.month, item_date.day, 8, 0, tzinfo=tz)
                if now < item_dt <= threshold:
                    item_id = str(item.get("id") or item_date_str)
                    already_sent = await self.store.has_sent_reminder(rule.guild_id, rule.user_id, item_id, "exam")
                    if already_sent:
                        continue
                    subject = str(item.get("subject") or "Prüfung")
                    topic = str(item.get("topic") or "")
                    delta_hours = int((item_dt - now).total_seconds() / 3600)
                    embed = discord.Embed(
                        title=f"📝 Prüfungs-Erinnerung: {subject}",
                        color=discord.Color.orange(),
                        description=(
                            f"Du hast in **{delta_hours} Stunden** eine Prüfung!\n\n"
                            f"Fach: **{subject}**\n"
                            f"Thema: {topic or '-'}\n"
                            f"Datum: <t:{int(item_dt.timestamp())}:D>"
                        ),
                        timestamp=datetime.now(timezone.utc),
                    )
                    try:
                        await user.send(embed=embed)
                        await self.store.mark_reminder_sent(rule.guild_id, rule.user_id, item_id, "exam")
                    except discord.Forbidden:
                        pass

        elif rule.reminder_type == "homework":
            items = await self.api.get_homework(state.access_token, state.student_id, open_only=True, force_refresh=False)
            for item in items:
                item_date_str = str(item.get("due_date") or "")
                if not item_date_str:
                    continue
                try:
                    item_date = date.fromisoformat(item_date_str)
                except ValueError:
                    continue
                # Anchor at 08:00 school-local time (not UTC) so the reminder window is correct.
                item_dt = datetime(item_date.year, item_date.month, item_date.day, 8, 0, tzinfo=tz)
                if now < item_dt <= threshold:
                    item_id = str(item.get("id") or item_date_str)
                    already_sent = await self.store.has_sent_reminder(rule.guild_id, rule.user_id, item_id, "homework")
                    if already_sent:
                        continue
                    subject = str(item.get("subject") or "Fach")
                    text = str(item.get("text") or "")
                    delta_hours = int((item_dt - now).total_seconds() / 3600)
                    embed = discord.Embed(
                        title=f"📚 Hausaufgaben-Erinnerung: {subject}",
                        color=discord.Color.yellow(),
                        description=(
                            f"Hausaufgaben in **{delta_hours} Stunden** fällig!\n\n"
                            f"Fach: **{subject}**\n"
                            f"Aufgabe: {text[:200] or '-'}\n"
                            f"Fällig: <t:{int(item_dt.timestamp())}:D>"
                        ),
                        timestamp=datetime.now(timezone.utc),
                    )
                    try:
                        await user.send(embed=embed)
                        await self.store.mark_reminder_sent(rule.guild_id, rule.user_id, item_id, "homework")
                    except discord.Forbidden:
                        pass

    async def _send_digest(self, guild: discord.Guild, state: UserWorkspaceState) -> None:
        channel = self._get_channel(guild, state.status_channel_id)
        if channel is None:
            return

        tz = resolve_timezone(self.settings.discord_timezone)
        today = datetime.now(tz).date()
        to_date = today + timedelta(days=21)

        schedule_raw = await self.api.get_schedule(state.access_token, state.student_id, today, to_date, force_refresh=False)
        homework_raw = await self.api.get_homework(state.access_token, state.student_id, open_only=False, force_refresh=False)
        exams_raw = await self.api.get_exams(state.access_token, state.student_id, force_refresh=False)
        grades_raw = await self.api.get_grades(state.access_token, state.student_id, force_refresh=False)
        letters_raw = await self.api.get_letters(state.access_token, state.student_id, force_refresh=False)

        today_str = today.isoformat()
        next_7 = today + timedelta(days=7)

        today_schedule = [d for d in schedule_raw if d.get("date") == today_str]
        today_lessons = today_schedule[0].get("lessons", []) if today_schedule else []
        today_hw = [hw for hw in homework_raw if hw.get("due_date") == today_str and not hw.get("done")]
        upcoming_exams = [e for e in exams_raw if today_str <= str(e.get("date") or "") <= next_7.isoformat()]
        yesterday_str = (today - timedelta(days=1)).isoformat()
        recent_grades = [g for g in grades_raw if str(g.get("date") or "") >= yesterday_str]
        unread_letters = [l for l in letters_raw if isinstance(l, dict) and not l.get("read")]

        embed = discord.Embed(
            title=f"☀️ Tages-Digest — {today.strftime('%A, %d.%m.%Y')}",
            color=discord.Color.blurple(),
            timestamp=datetime.now(tz),
        )
        embed.set_author(name=state.student_name)

        if today_lessons:
            lesson_lines: list[str] = []
            for lesson in today_lessons[:6]:
                subj = str(lesson.get("subject") or "Fach")
                start = str(lesson.get("start_time") or "")
                ct = str(lesson.get("change_type") or "")
                icon = "❌" if ct == "cancellation" else ("🔄" if ct in ("substitution", "room_change") else "📖")
                lesson_lines.append(f"{icon} {start} **{subj}**")
            embed.add_field(name=f"📅 Heute ({len(today_lessons)} Stunden)", value="\n".join(lesson_lines) or "-", inline=False)
        else:
            embed.add_field(name="📅 Heute", value="Keine Stunden", inline=False)

        if today_hw:
            hw_lines = [f"• **{hw.get('subject', 'Fach')}**: {str(hw.get('text', ''))[:80]}" for hw in today_hw[:5]]
            embed.add_field(name=f"📚 Heute fällig ({len(today_hw)})", value=_clip("\n".join(hw_lines), 1024), inline=False)

        if upcoming_exams:
            exam_lines: list[str] = []
            for exam in upcoming_exams[:5]:
                d = str(exam.get("date") or "")
                try:
                    dd = date.fromisoformat(d[:10]) if d else None
                    exam_epoch = int(datetime(dd.year, dd.month, dd.day, tzinfo=tz).timestamp()) if dd else 0
                    time_str = f"<t:{exam_epoch}:D>" if exam_epoch else d
                except ValueError:
                    time_str = d
                exam_lines.append(f"• {time_str} **{exam.get('subject', 'Fach')}** — {exam.get('topic', '-')}")
            embed.add_field(name=f"📝 Prüfungen (7 Tage, {len(upcoming_exams)})", value=_clip("\n".join(exam_lines), 1024), inline=False)

        if recent_grades:
            grade_lines = [f"• **{g.get('subject', 'Fach')}**: {g.get('grade', '?')} {g.get('comment', '')}" for g in recent_grades[:5]]
            embed.add_field(name=f"📊 Neue Noten ({len(recent_grades)})", value=_clip("\n".join(grade_lines), 1024), inline=False)

        if unread_letters:
            letter_lines = [
                f"• {'⚠️ ' if l.get('requires_confirmation') else ''}{str(l.get('title', 'Elternbrief'))[:80]}"
                for l in unread_letters[:5]
            ]
            embed.add_field(name=f"✉️ Ungelesene Elternbriefe ({len(unread_letters)})", value=_clip("\n".join(letter_lines), 1024), inline=False)

        if not embed.fields:
            embed.description = "Heute keine besonderen Ereignisse."

        # Fingerprint basiert auf Datum + tatsächlichem Inhalt damit bei Änderungen
        # tagsüber (z.B. neue Hausaufgabe) der Embed aktualisiert wird statt neu gesendet
        fp_data = {
            "date": today_str,
            "lessons": len(today_lessons),
            "hw": len(today_hw),
            "exams": len(upcoming_exams),
            "grades": len(recent_grades),
            "letters": len(unread_letters),
            "hw_texts": [str(hw.get("text", ""))[:80] for hw in today_hw[:5]],
            "exam_subjects": [str(e.get("subject", "")) for e in upcoming_exams[:5]],
            "grade_subjects": [str(g.get("subject", "")) for g in recent_grades[:5]],
            "letter_titles": [str(l.get("title", ""))[:80] for l in unread_letters[:5]],
        }
        fingerprint = "digest-v2:" + _fingerprint(fp_data)[:16]

        item = RenderedEmbed(key="digest", embed=embed, fingerprint=fingerprint)
        await self._upsert_embed(state.guild_id, state.user_id, channel, "status", item)

    # ─── Sync internals ───────────────────────────────────────────────────────

    async def _handle_status_sync_button(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or interaction.user is None:
            if interaction.response.is_done():
                await interaction.followup.send("Bitte in einem Server ausführen.", ephemeral=True)
            else:
                await interaction.response.send_message("Bitte in einem Server ausführen.", ephemeral=True)
            return

        message = interaction.message
        if message is None:
            await interaction.response.send_message("Status-Nachricht konnte nicht erkannt werden.", ephemeral=True)
            return

        record = await self.store.get_embed_record_by_message_id(
            guild_id=interaction.guild.id,
            channel_kind="status",
            message_id=message.id,
        )
        if record is None or record.item_key != "status":
            await interaction.response.send_message("Diese Status-Nachricht ist nicht mehr gültig.", ephemeral=True)
            return

        if interaction.user.id != record.user_id:
            await interaction.response.send_message("Dieser Sync-Button gehört nicht zu deinem Account.", ephemeral=True)
            return

        state = await self.store.get_user(record.guild_id, record.user_id)
        if state is None:
            await interaction.response.send_message("Bitte zuerst /login verwenden.", ephemeral=True)
            return
        if not state.active:
            await interaction.response.send_message("Session ist nicht mehr aktiv. Bitte /login erneut ausführen.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self._sync_user(state, reason="button", force_refresh=True)
        except Exception as exc:
            await interaction.followup.send(f"Synchronisierung fehlgeschlagen: {exc}", ephemeral=True)
            return
        await interaction.followup.send("✅ Sync abgeschlossen.", ephemeral=True)

    async def _sync_user(self, state: UserWorkspaceState, *, reason: str, force_refresh: bool) -> None:
        key = (state.guild_id, state.user_id)
        lock = self._user_locks.setdefault(key, asyncio.Lock())

        async with lock:
            # Re-read the authoritative state after acquiring the lock: a concurrent sync (auto
            # loop vs. manual vs. button) may have rotated the tokens while we waited, so the
            # snapshot we were called with can carry an already-consumed refresh token.
            fresh = await self.store.get_user(state.guild_id, state.user_id)
            if fresh is not None:
                state = fresh

            guild = self.bot.get_guild(state.guild_id)
            if guild is None:
                await self.store.set_active(state.guild_id, state.user_id, False)
                return

            member = guild.get_member(state.user_id)
            if member is None:
                try:
                    member = await guild.fetch_member(state.user_id)
                except discord.NotFound:
                    await self.store.set_active(state.guild_id, state.user_id, False)
                    return

            workspace = await self._ensure_workspace(guild, member, state)
            state = replace(
                state,
                category_id=workspace["category_id"],
                status_channel_id=workspace["status"],
                schedule_feed_channel_id=workspace["schedule_feed"],
                schedule_week_channel_id=workspace["schedule_week"],
                homework_channel_id=workspace["homework"],
                grades_channel_id=workspace["grades"],
                events_channel_id=workspace["events"],
                webhooks_channel_id=workspace["webhooks"],
                absences_channel_id=workspace["absences"],
                messages_channel_id=workspace["messages"],
                letters_channel_id=workspace["letters"],
            )

            try:
                state = await self._run_sync_iteration(
                    guild=guild,
                    state=state,
                    reason=reason,
                    force_refresh=force_refresh,
                )
            except Exception as exc:
                requires_relogin = self._requires_relogin(exc)
                error_text = str(exc)
                if requires_relogin:
                    if await self._attempt_auto_relogin(
                        guild=guild,
                        state=state,
                        reason=reason,
                        force_refresh=force_refresh,
                    ):
                        return
                    error_text = self._session_notice_text(exc)
                    await self.store.set_active(state.guild_id, state.user_id, False)
                    await self._publish_relogin_notice(guild, state, error_text)
                await self.store.update_sync_status(
                    state.guild_id,
                    state.user_id,
                    last_sync_ts=int(time.time()),
                    last_error=error_text,
                )
                if requires_relogin and reason == "auto":
                    return
                raise

    async def _run_sync_iteration(
        self,
        *,
        guild: discord.Guild,
        state: UserWorkspaceState,
        reason: str,
        force_refresh: bool,
    ) -> UserWorkspaceState:
        state = await self._ensure_valid_tokens(state)
        await self.store.upsert_user(state)

        data = await self._fetch_payloads(state, force_refresh=force_refresh)
        await self._process_schedule_change_dms(state, data["schedule"])
        await self._process_letter_notifications(state, data.get("letters", []))
        changes = await self._publish_payloads(guild, state, data)
        await self._publish_status(guild, state, reason=reason, data=data)
        await self._publish_webhook_notifications(guild, state, changes, reason=reason)
        await self.store.update_sync_status(
            state.guild_id,
            state.user_id,
            last_sync_ts=int(time.time()),
            last_error=None,
        )
        return state

    async def _attempt_auto_relogin(
        self,
        *,
        guild: discord.Guild,
        state: UserWorkspaceState,
        reason: str,
        force_refresh: bool,
    ) -> bool:
        if not state.password:
            return False

        LOGGER.info("Attempting automatic re-login for guild=%s user=%s", state.guild_id, state.user_id)
        try:
            login_response = await self.api.login(email=state.email, password=state.password)
        except ApiClientError as exc:
            LOGGER.warning("Automatic re-login failed (login) for guild=%s user=%s: %s", state.guild_id, state.user_id, exc)
            return False

        selected_student_id = state.student_id
        if selected_student_id not in login_response.student_ids:
            if not login_response.student_ids:
                return False
            selected_student_id = login_response.student_ids[0]

        student_name = state.student_name
        try:
            students = await self.api.get_students(login_response.access_token)
            selected_student = self._select_student(students, selected_student_id)
            if selected_student is not None:
                student_name = self._student_display_name(selected_student)
        except ApiClientError as exc:
            LOGGER.info("Automatic re-login continues without students call for guild=%s user=%s: %s", state.guild_id, state.user_id, exc)

        now_ts = int(time.time())
        refreshed_state = replace(
            state,
            student_id=selected_student_id,
            student_name=student_name,
            account_id=login_response.account_id,
            access_token=login_response.access_token,
            refresh_token=login_response.refresh_token,
            access_expires_at=now_ts + max(login_response.expires_in, 1),
            refresh_expires_at=now_ts + max(login_response.refresh_expires_in, 1),
            active=True,
            last_error=None,
        )
        await self.store.upsert_user(refreshed_state)

        try:
            await self._run_sync_iteration(
                guild=guild,
                state=refreshed_state,
                reason=f"{reason}-relogin",
                force_refresh=True if reason == "auto" else force_refresh,
            )
        except Exception as exc:
            LOGGER.warning("Automatic re-login failed (sync) for guild=%s user=%s: %s", state.guild_id, state.user_id, exc)
            return False

        return True

    async def _ensure_valid_tokens(self, state: UserWorkspaceState) -> UserWorkspaceState:
        now = int(time.time())
        if state.access_expires_at - now > 90:
            return state

        refreshed = await self.api.refresh(state.refresh_token)
        updated = replace(
            state,
            access_token=refreshed.access_token,
            refresh_token=refreshed.refresh_token,
            access_expires_at=now + max(refreshed.expires_in, 1),
            refresh_expires_at=now + max(refreshed.refresh_expires_in, 1),
        )
        await self.store.update_tokens(
            state.guild_id,
            state.user_id,
            updated.access_token,
            updated.refresh_token,
            updated.access_expires_at,
            updated.refresh_expires_at,
        )
        return updated

    @staticmethod
    def _data_looks_empty(data: dict[str, Any]) -> bool:
        """Heuristic for a dead session: ALL data sources empty.

        Includes the schedule so a legitimately quiet account (has lessons but no current
        grades/messages) is not repeatedly force-relogged. Since the API now returns explicit
        401s for expired sessions, this is only a secondary fallback.
        """
        return not any(
            data.get(key)
            for key in ("schedule", "grades", "events", "homework", "absences", "messages", "letters")
        )

    async def _fetch_payloads(self, state: UserWorkspaceState, *, force_refresh: bool) -> dict[str, Any]:
        tz = resolve_timezone(self.settings.discord_timezone)
        today = datetime.now(tz).date()
        to_date = today + timedelta(days=21)

        async def fetch_once(access_token: str) -> dict[str, Any]:
            sid = state.student_id
            # Fetch all endpoints concurrently (was 7+ sequential round-trips) for much faster syncs.
            coros = {
                "schedule": self.api.get_schedule(access_token, sid, from_date=today, to_date=to_date, force_refresh=force_refresh),
                "homework": self.api.get_homework(access_token, sid, open_only=False, force_refresh=force_refresh),
                "grades": self.api.get_grades(access_token, sid, force_refresh=force_refresh),
                "events": self.api.get_events(access_token, sid, force_refresh=force_refresh),
                "absences": self.api.get_absences(access_token, sid, force_refresh=force_refresh),
                "messages": self.api.get_messages(access_token, sid, force_refresh=force_refresh),
                "grade_stats": self.api.get_grade_stats(access_token, sid, force_refresh=force_refresh),
                "letters": self.api.get_letters(access_token, sid, force_refresh=force_refresh),
            }
            labels = list(coros.keys())
            results = await asyncio.gather(*coros.values(), return_exceptions=True)

            data: dict[str, Any] = {}
            for label, result in zip(labels, results):
                if isinstance(result, ApiClientError) and result.status_code == 401:
                    # Session expired — propagate so the caller can refresh / re-login.
                    raise result
                if isinstance(result, Exception):
                    # One flaky/disabled endpoint must not abort the whole sync.
                    LOGGER.warning(
                        "Endpoint '%s' failed for guild=%s user=%s: %s",
                        label, state.guild_id, state.user_id, result,
                    )
                    data[label] = {} if label == "grade_stats" else []
                else:
                    data[label] = result
            return data

        try:
            data = await fetch_once(state.access_token)
        except ApiClientError as exc:
            if exc.status_code != 401:
                raise
            refreshed = await self._ensure_valid_tokens(replace(state, access_expires_at=0))
            await self.store.upsert_user(refreshed)
            data = await fetch_once(refreshed.access_token)

        if self._data_looks_empty(data) and state.password:
            LOGGER.info(
                "Alle Daten leer für guild=%s user=%s – versuche erneuten Login",
                state.guild_id, state.user_id,
            )
            try:
                login_response = await self.api.login(email=state.email, password=state.password)
                now_ts = int(time.time())
                refreshed_state = replace(
                    state,
                    account_id=login_response.account_id,
                    access_token=login_response.access_token,
                    refresh_token=login_response.refresh_token,
                    access_expires_at=now_ts + max(login_response.expires_in, 1),
                    refresh_expires_at=now_ts + max(login_response.refresh_expires_in, 1),
                )
                await self.store.upsert_user(refreshed_state)
                data = await fetch_once(refreshed_state.access_token)
                LOGGER.info(
                    "Erneuter Login erfolgreich für guild=%s user=%s – Daten neu geladen",
                    state.guild_id, state.user_id,
                )
            except ApiClientError as exc:
                LOGGER.warning(
                    "Erneuter Login nach leeren Daten fehlgeschlagen für guild=%s user=%s: %s",
                    state.guild_id, state.user_id, exc,
                )
                raise

        return data

    async def _publish_payloads(self, guild: discord.Guild, state: UserWorkspaceState, payloads: dict[str, Any]) -> dict[str, list[str]]:
        changes: dict[str, list[str]] = {
            "schedule_feed": [],
            "schedule_week": [],
            "homework": [],
            "grades": [],
            "events": [],
            "absences": [],
            "messages": [],
            "letters": [],
        }

        schedule_days = payloads["schedule"]
        feed_items = compact_rendered(render_schedule_feed(schedule_days, self.settings.discord_timezone))
        week_items = compact_rendered(render_schedule_week(schedule_days, self.settings.discord_timezone))
        grades_items = compact_rendered(render_grades(payloads["grades"], self.settings.discord_timezone))
        events_items = compact_rendered(render_events(payloads["events"], self.settings.discord_timezone))
        absences_items = compact_rendered(render_absences(payloads["absences"], self.settings.discord_timezone))
        messages_items = compact_rendered(render_messages(payloads["messages"], self.settings.discord_timezone))
        letters_items = compact_rendered(render_letters(payloads.get("letters", []), self.settings.discord_timezone))
        grade_stats_items = compact_rendered(render_grade_stats(payloads["grade_stats"], self.settings.discord_timezone))

        channel_map = {
            "schedule_feed": self._get_channel(guild, state.schedule_feed_channel_id),
            "schedule_week": self._get_channel(guild, state.schedule_week_channel_id),
            "grades": self._get_channel(guild, state.grades_channel_id),
            "absences": self._get_channel(guild, state.absences_channel_id),
        }

        rendered_map = {
            "schedule_feed": feed_items,
            "schedule_week": week_items,
            "grades": grades_items,
            "absences": absences_items,
        }

        for kind, channel in channel_map.items():
            if channel is None:
                continue
            updates = await self._sync_channel_embeds(
                guild_id=state.guild_id,
                user_id=state.user_id,
                channel=channel,
                channel_kind=kind,
                items=rendered_map[kind],
            )
            changes[kind] = updates

        grades_channel = self._get_channel(guild, state.grades_channel_id)
        if grades_channel is not None and grade_stats_items:
            stats_updates = await self._sync_channel_embeds(
                guild_id=state.guild_id,
                user_id=state.user_id,
                channel=grades_channel,
                channel_kind=GRADE_STATS_KIND,
                items=grade_stats_items,
            )
            if stats_updates:
                changes["grades"].extend(stats_updates)

        homework_channel = self._get_channel(guild, state.homework_channel_id)
        if homework_channel is not None:
            hw_updates = await self._sync_homework_items_channel(
                guild_id=state.guild_id,
                user_id=state.user_id,
                channel=homework_channel,
                homework_items=payloads["homework"],
                schedule_days=schedule_days,
            )
            changes["homework"] = hw_updates

        messages_channel = self._get_channel(guild, state.messages_channel_id)
        if messages_channel is not None:
            msg_updates = await self._sync_channel_embeds(
                guild_id=state.guild_id,
                user_id=state.user_id,
                channel=messages_channel,
                channel_kind="messages",
                items=messages_items,
            )
            changes["messages"] = msg_updates

        letters_channel = self._get_channel(guild, state.letters_channel_id)
        if letters_channel is not None:
            letter_updates = await self._sync_channel_embeds(
                guild_id=state.guild_id,
                user_id=state.user_id,
                channel=letters_channel,
                channel_kind="letters",
                items=letters_items,
            )
            changes["letters"] = letter_updates

        events_channel = self._get_channel(guild, state.events_channel_id)
        if events_channel is not None:
            event_updates = await self._sync_channel_embeds(
                guild_id=state.guild_id,
                user_id=state.user_id,
                channel=events_channel,
                channel_kind="events",
                items=events_items,
            )
            changes["events"] = event_updates

            panel_item = await self._build_events_panel_item(
                guild=guild,
                guild_id=state.guild_id,
                user_id=state.user_id,
                channel=events_channel,
                event_items=events_items,
            )
            panel_update = await self._sync_events_panel(
                guild_id=state.guild_id,
                user_id=state.user_id,
                channel=events_channel,
                panel_item=panel_item,
                move_to_bottom=self._events_panel_should_move(event_updates),
            )
            if panel_update:
                changes["events"].append(panel_update)

        return changes

    async def _sync_homework_items_channel(
        self,
        *,
        guild_id: int,
        user_id: int,
        channel: discord.TextChannel,
        homework_items: list[dict[str, Any]],
        schedule_days: list[dict[str, Any]],
    ) -> list[str]:
        existing = {
            row.item_key: row
            for row in await self.store.list_embed_records(guild_id, user_id, HOMEWORK_ITEM_KIND)
        }
        done_states = await self.store.get_all_homework_done(guild_id, user_id)

        changes: list[str] = []
        incoming_keys: set[str] = set()

        for item in homework_items:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "")
            if not item_id:
                continue
            incoming_keys.add(item_id)

            done = done_states.get(item_id, bool(item.get("done")))
            rendered = render_homework_item(item, schedule_days, self.settings.discord_timezone, done=done)

            record = existing.get(item_id)
            if record and record.fingerprint == rendered.fingerprint:
                continue

            if record:
                try:
                    message = await channel.fetch_message(record.message_id)
                    await message.edit(embed=rendered.embed)
                    await self.store.upsert_embed_record(guild_id, user_id, HOMEWORK_ITEM_KIND, item_id, message.id, rendered.fingerprint)
                    changes.append(f"{item_id}:updated")
                except discord.NotFound:
                    message = await channel.send(embed=rendered.embed)
                    try:
                        await message.add_reaction(DONE_EMOJI)
                    except discord.Forbidden:
                        pass
                    await self.store.upsert_embed_record(guild_id, user_id, HOMEWORK_ITEM_KIND, item_id, message.id, rendered.fingerprint)
                    changes.append(f"{item_id}:created")
            else:
                message = await channel.send(embed=rendered.embed)
                try:
                    await message.add_reaction(DONE_EMOJI)
                except discord.Forbidden:
                    pass
                await self.store.upsert_embed_record(guild_id, user_id, HOMEWORK_ITEM_KIND, item_id, message.id, rendered.fingerprint)
                changes.append(f"{item_id}:created")

            await asyncio.sleep(0.3)

        stale_keys = [key for key in existing if key not in incoming_keys]
        for key in stale_keys:
            record = existing[key]
            try:
                message = await channel.fetch_message(record.message_id)
                await message.delete()
            except discord.NotFound:
                pass
            await self.store.delete_embed_record(guild_id, user_id, HOMEWORK_ITEM_KIND, key)
            changes.append(f"{key}:deleted")
            await asyncio.sleep(0.3)

        return changes

    @staticmethod
    def _events_panel_should_move(event_updates: list[str]) -> bool:
        for update in event_updates:
            if update.endswith(":created") or update.endswith(":deleted"):
                return True
        return False

    async def _build_events_panel_item(
        self,
        *,
        guild: discord.Guild,
        guild_id: int,
        user_id: int,
        channel: discord.TextChannel,
        event_items: list[RenderedEmbed],
    ) -> RenderedEmbed:
        tz = resolve_timezone(self.settings.discord_timezone)
        now_epoch = int(datetime.now(tz).timestamp())

        target = self._select_next_event_item(event_items, now_epoch)
        embed = discord.Embed(
            title="Nächstes Event",
            color=discord.Color.magenta(),
            timestamp=datetime.now(tz),
        )

        if target is None:
            embed.description = "Keine Events vorhanden."
            return RenderedEmbed(key=EVENTS_PANEL_KEY, embed=embed, fingerprint="none", sort_epoch=2**31 - 1)

        message_record = await self.store.get_embed_record(guild_id, user_id, "events", target.key)
        jump_url = (
            self._message_jump_url(guild.id, channel.id, message_record.message_id)
            if message_record
            else None
        )

        start_epoch = target.sort_epoch or now_epoch
        lines = [f"**{target.embed.title or target.key}**", f"Start: <t:{start_epoch}:F> • <t:{start_epoch}:R>"]
        if jump_url:
            lines.append("Mit dem Button springst du direkt zum Event-Embed.")
        embed.description = "\n".join(lines)

        return RenderedEmbed(
            key=EVENTS_PANEL_KEY,
            embed=embed,
            fingerprint=f"{target.key}:{start_epoch}:{jump_url or '-'}",
            sort_epoch=2**31 - 1,
            button_url=jump_url,
            button_label="Zum nächsten Event",
        )

    def _select_next_event_item(self, event_items: list[RenderedEmbed], now_epoch: int) -> RenderedEmbed | None:
        with_sort_epoch = [item for item in event_items if item.sort_epoch is not None]
        if not with_sort_epoch:
            return None

        upcoming = [item for item in with_sort_epoch if int(item.sort_epoch or 0) >= now_epoch]
        if upcoming:
            return min(upcoming, key=lambda item: int(item.sort_epoch or now_epoch))

        return min(with_sort_epoch, key=lambda item: abs(int(item.sort_epoch or now_epoch) - now_epoch))

    async def _sync_events_panel(
        self,
        *,
        guild_id: int,
        user_id: int,
        channel: discord.TextChannel,
        panel_item: RenderedEmbed,
        move_to_bottom: bool,
    ) -> str | None:
        if move_to_bottom:
            record = await self.store.get_embed_record(guild_id, user_id, EVENTS_PANEL_KIND, EVENTS_PANEL_KEY)
            if record is not None:
                try:
                    message = await channel.fetch_message(record.message_id)
                    await message.delete()
                except discord.NotFound:
                    pass
                await self.store.delete_embed_record(guild_id, user_id, EVENTS_PANEL_KIND, EVENTS_PANEL_KEY)

        result = await self._upsert_embed(guild_id, user_id, channel, EVENTS_PANEL_KIND, panel_item)
        if result.changed:
            return f"{panel_item.key}:{result.action}"
        return None

    @staticmethod
    def _message_jump_url(guild_id: int, channel_id: int, message_id: int) -> str:
        return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"

    async def _publish_status(self, guild: discord.Guild, state: UserWorkspaceState, *, reason: str, data: dict[str, Any]) -> None:
        channel = self._get_channel(guild, state.status_channel_id)
        if channel is None:
            return

        await self._delete_embed_if_exists(
            guild_id=state.guild_id,
            user_id=state.user_id,
            channel=channel,
            channel_kind="status",
            item_key="reauth_notice",
        )

        now_epoch = int(time.time())
        embed = discord.Embed(
            title="🔄 Schulmanager – Sync-Status",
            color=discord.Color.dark_teal(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_author(name=state.student_name)
        embed.description = f"Letzter Sync: <t:{now_epoch}:F> (<t:{now_epoch}:R>)\nSync-Typ: `{reason}`"
        embed.add_field(name="📅 Stunden", value=str(len(data["schedule"])), inline=True)
        embed.add_field(name="📚 Hausaufgaben", value=str(len(data["homework"])), inline=True)
        embed.add_field(name="📊 Noten", value=str(len(data["grades"])), inline=True)
        embed.add_field(name="🗓️ Events", value=str(len(data["events"])), inline=True)
        embed.add_field(name="📋 Fehlzeiten", value=str(len(data["absences"])), inline=True)
        embed.add_field(name="📬 Nachrichten", value=str(len(data["messages"])), inline=True)
        letters_data = data.get("letters") or []
        unread_letters = sum(1 for letter in letters_data if isinstance(letter, dict) and not letter.get("read"))
        letters_value = str(len(letters_data)) + (f" ({unread_letters} ungelesen)" if unread_letters else "")
        embed.add_field(name="✉️ Elternbriefe", value=letters_value, inline=True)

        item = RenderedEmbed(key="status", embed=embed, fingerprint=f"status-v3:{now_epoch // 60}")
        await self._upsert_embed(state.guild_id, state.user_id, channel, "status", item)

    async def _publish_relogin_notice(self, guild: discord.Guild, state: UserWorkspaceState, error_text: str) -> None:
        channel = self._get_channel(guild, state.status_channel_id)
        if channel is None:
            return

        now_epoch = int(time.time())
        embed = discord.Embed(
            title="Erneute Anmeldung erforderlich",
            color=discord.Color.red(),
            description="Die API- oder Selenium-Sitzung ist nicht mehr gültig.\nBitte führe `/login` erneut aus.",
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Zeit", value=f"<t:{now_epoch}:F>", inline=False)
        embed.add_field(name="Details", value=error_text[:1000], inline=False)
        item = RenderedEmbed(key="reauth_notice", embed=embed, fingerprint=_short_fingerprint(error_text))
        await self._upsert_embed(state.guild_id, state.user_id, channel, "status", item)

    async def _publish_webhook_notifications(
        self,
        guild: discord.Guild,
        state: UserWorkspaceState,
        changes: dict[str, list[str]],
        *,
        reason: str,
    ) -> None:
        channel = self._get_channel(guild, state.webhooks_channel_id)
        if channel is None:
            return

        summaries: list[tuple[str, str]] = []
        for kind, updates in changes.items():
            filtered_updates = [value for value in updates if not value.startswith(f"{EVENTS_PANEL_KEY}:")]
            if not filtered_updates:
                continue
            created, updated, deleted, samples = self._summarize_updates(filtered_updates)
            stat_parts: list[str] = []
            if created:
                stat_parts.append(f"+{created}")
            if updated:
                stat_parts.append(f"~{updated}")
            if deleted:
                stat_parts.append(f"-{deleted}")
            if not stat_parts:
                stat_parts.append("keine")

            value = " ".join(stat_parts)
            if reason != "initial" and samples:
                value += f" | {', '.join(samples)}"
            summaries.append((kind, value))

        if not summaries:
            if reason == "initial":
                await channel.send("Initiale Synchronisierung abgeschlossen. Keine Änderungen erkannt.")
            return

        embed = discord.Embed(
            title=f"Webhook-Event ({reason})",
            color=discord.Color.dark_blue(),
            description=(
                f"Student: {state.student_name}\n"
                "Legende: `+` erstellt, `~` aktualisiert, `-` gelöscht"
            ),
            timestamp=datetime.now(timezone.utc),
        )
        for kind, value in summaries[:8]:
            embed.add_field(name=kind, value=value, inline=False)
        await channel.send(embed=embed)

    @staticmethod
    def _summarize_updates(updates: list[str]) -> tuple[int, int, int, list[str]]:
        created = updated = deleted = 0
        samples: list[str] = []
        for item in updates:
            if ":" not in item:
                continue
            key, action = item.rsplit(":", maxsplit=1)
            action = action.strip()
            if action == "created":
                created += 1
            elif action == "updated":
                updated += 1
            elif action == "deleted":
                deleted += 1
            if len(samples) < 3:
                samples.append(key)
        return created, updated, deleted, samples

    async def _sync_channel_embeds(
        self,
        *,
        guild_id: int,
        user_id: int,
        channel: discord.TextChannel,
        channel_kind: str,
        items: list[RenderedEmbed],
    ) -> list[str]:
        existing = {
            row.item_key: row
            for row in await self.store.list_embed_records(guild_id, user_id, channel_kind)
        }

        changes: list[str] = []
        incoming_keys = {item.key for item in items}

        for item in items:
            result = await self._upsert_embed(guild_id, user_id, channel, channel_kind, item)
            if result.changed:
                changes.append(f"{item.key}:{result.action}")
                await asyncio.sleep(self._channel_update_delay(channel_kind))

        stale_keys = [key for key in existing if key not in incoming_keys]
        for key in stale_keys:
            record = existing[key]
            try:
                message = await channel.fetch_message(record.message_id)
                await message.delete()
            except discord.NotFound:
                pass
            await self.store.delete_embed_record(guild_id, user_id, channel_kind, key)
            changes.append(f"{key}:deleted")
            await asyncio.sleep(self._channel_update_delay(channel_kind))

        return changes

    async def _delete_embed_if_exists(
        self,
        *,
        guild_id: int,
        user_id: int,
        channel: discord.TextChannel,
        channel_kind: str,
        item_key: str,
    ) -> None:
        record = await self.store.get_embed_record(guild_id, user_id, channel_kind, item_key)
        if record is None:
            return
        try:
            message = await channel.fetch_message(record.message_id)
            await message.delete()
        except discord.NotFound:
            pass
        await self.store.delete_embed_record(guild_id, user_id, channel_kind, item_key)

    async def _upsert_embed(
        self,
        guild_id: int,
        user_id: int,
        channel: discord.TextChannel,
        channel_kind: str,
        item: RenderedEmbed,
    ) -> UpsertResult:
        record = await self.store.get_embed_record(guild_id, user_id, channel_kind, item.key)
        view = self._build_message_view(channel_kind, item)

        if record and record.fingerprint == item.fingerprint:
            return UpsertResult(action="unchanged", message_id=record.message_id, changed=False)

        if record:
            try:
                message = await channel.fetch_message(record.message_id)
                await message.edit(embed=item.embed, view=view)
                await self.store.upsert_embed_record(guild_id, user_id, channel_kind, item.key, message.id, item.fingerprint)
                return UpsertResult(action="updated", message_id=message.id, changed=True)
            except discord.NotFound:
                pass

        message = await channel.send(embed=item.embed, view=view)
        await self.store.upsert_embed_record(guild_id, user_id, channel_kind, item.key, message.id, item.fingerprint)
        return UpsertResult(action="created", message_id=message.id, changed=True)

    def _build_message_view(self, channel_kind: str, item: RenderedEmbed) -> discord.ui.View | None:
        if channel_kind == "status" and item.key == "status":
            return StatusSyncButtonView(self)
        if not item.button_url:
            return None
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label=item.button_label or "Oeffnen", url=item.button_url))
        return view

    async def _manage_logged_in_role(self, guild: discord.Guild, user_id: int, *, add: bool) -> None:
        role_id = self.settings.discord_logged_in_role_id
        if not role_id:
            return
        role = guild.get_role(role_id)
        if role is None:
            LOGGER.warning("Logged-in role %s not found in guild %s", role_id, guild.id)
            return
        try:
            member = guild.get_member(user_id) or await guild.fetch_member(user_id)
        except discord.NotFound:
            return
        try:
            if add:
                await member.add_roles(role, reason="Schulmanager login")
            else:
                await member.remove_roles(role, reason="Schulmanager logout")
        except discord.Forbidden:
            LOGGER.warning("Missing permission to manage logged-in role %s in guild %s", role_id, guild.id)

    async def _ensure_workspace(
        self,
        guild: discord.Guild,
        member: discord.Member,
        state: UserWorkspaceState | None,
    ) -> dict[str, int | None]:
        category = None
        if state and state.category_id:
            channel = guild.get_channel(state.category_id)
            if isinstance(channel, discord.CategoryChannel):
                category = channel

        if category is None:
            category_name = f"{self.settings.discord_category_prefix}-{member.display_name}".lower().replace(" ", "-")
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_messages=True, manage_channels=True),
            }
            category = await guild.create_category(name=category_name[:95], overwrites=overwrites)
        else:
            await category.set_permissions(guild.default_role, view_channel=False)
            await category.set_permissions(member, view_channel=True, send_messages=True, read_message_history=True)

        result: dict[str, int | None] = {"category_id": category.id}
        for kind, channel_name in CHANNEL_SPECS:
            channel_id = getattr(state, CHANNEL_KIND_TO_FIELD[kind], None) if state else None
            text_channel = guild.get_channel(channel_id) if channel_id else None
            if not isinstance(text_channel, discord.TextChannel) or text_channel.category_id != category.id:
                text_channel = await guild.create_text_channel(channel_name, category=category)
            result[kind] = text_channel.id

        return result

    def _select_student(self, students: list[dict[str, Any]], student_id: str) -> dict[str, Any] | None:
        for student in students:
            if str(student.get("id") or "") == student_id:
                return student
        return None

    @staticmethod
    def _student_display_name(student: dict[str, Any]) -> str:
        first_name = str(student.get("first_name") or "").strip()
        last_name = str(student.get("last_name") or "").strip()
        full = f"{first_name} {last_name}".strip()
        return full or str(student.get("id") or "student")

    @staticmethod
    def _get_channel(guild: discord.Guild, channel_id: int | None) -> discord.TextChannel | None:
        if not channel_id:
            return None
        channel = guild.get_channel(channel_id)
        return channel if isinstance(channel, discord.TextChannel) else None

    @staticmethod
    def _is_admin_interaction(interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member):
            return False
        perms = interaction.user.guild_permissions
        return bool(perms.administrator or perms.manage_guild)

    @staticmethod
    def _channel_update_delay(channel_kind: str) -> float:
        if channel_kind in {"schedule_week", "homework", "grades", "events", "absences", "messages", "letters"}:
            return 0.35
        return 0.2

    @staticmethod
    def _requires_relogin(exc: Exception) -> bool:
        # Any 401 that reaches here has already survived the in-flight token refresh in
        # _fetch_payloads, so the access AND refresh tokens are unusable -> a fresh /login is
        # required. Matching on German error wording was brittle (e.g. "Token ungueltig" vs the
        # "ungültig" marker never matched); rely on the status code instead.
        return isinstance(exc, ApiClientError) and exc.status_code == 401

    @staticmethod
    def _session_notice_text(exc: Exception) -> str:
        return f"Sitzung nicht mehr gültig. Bitte /login erneut ausführen. Originalfehler: {exc}"


class SchulmanagerDiscordBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.guild_messages = True
        intents.reactions = True

        super().__init__(command_prefix="!", intents=intents)
        self.settings = settings
        self.store = DiscordStateStore(settings.discord_db_path)
        self.api = SchulmanagerApiClient(settings.discord_api_base_url)

    async def setup_hook(self) -> None:
        await self.store.initialize()
        cog = SchulmanagerCog(self, self.settings, self.store, self.api)
        await self.add_cog(cog)

        guild_id = self.settings.discord_guild_id
        if guild_id:
            guild = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def close(self) -> None:
        await self.api.close()
        await super().close()


def run_discord_bot(settings: Settings) -> None:
    if not settings.discord_bot_token:
        raise RuntimeError("SM_DISCORD_BOT_TOKEN fehlt")

    logging.basicConfig(level=logging.INFO)
    bot = SchulmanagerDiscordBot(settings)
    bot.run(settings.discord_bot_token)


def _short_fingerprint(value: str) -> str:
    return hashlib.sha256(value.strip().casefold().encode("utf-8")).hexdigest()[:16]
