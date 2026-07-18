from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import io
import logging
import time
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

from schulmanager_discord_bot.api_client import ApiClientError, SchulmanagerApiClient
from schulmanager_discord_bot.config import Settings
from schulmanager_discord_bot.embeds import _clip, resolve_timezone
from schulmanager_discord_bot.forum import (
    SECTION_KEYS,
    SECTION_TITLES,
    ForumDashboardView,
    SectionManageView,
    render_section,
    section_thread_name,
)
from schulmanager_discord_bot.models import ReminderRule, UserWorkspaceState
from schulmanager_discord_bot.storage import DiscordStateStore

LOGGER = logging.getLogger(__name__)

DASHBOARD_NAME = "📊 Dashboard"


class SchulmanagerCog(commands.Cog):
    def __init__(self, bot: commands.Bot, settings: Settings, store: DiscordStateStore, api: SchulmanagerApiClient) -> None:
        self.bot = bot
        self.settings = settings
        self.store = store
        self.api = api
        self._user_locks: dict[tuple[int, int], asyncio.Lock] = {}
        self.sync_loop.change_interval(seconds=max(self.settings.discord_sync_interval_seconds, 30))

    async def cog_load(self) -> None:
        self.bot.add_view(ForumDashboardView(self))
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

    @app_commands.command(name="login", description="Login für Schulmanager und privates Forum erstellen")
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
            await interaction.followup.send(
                f"Student nicht gefunden. Verfügbar: {', '.join(login_response.student_ids)}",
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
            LOGGER.warning("Students endpoint failed after login for guild=%s user=%s: %s", interaction.guild.id, interaction.user.id, exc)

        existing = await self.store.get_user(interaction.guild.id, interaction.user.id)

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
            category_id=None,
            status_channel_id=None,
            schedule_feed_channel_id=None,
            schedule_week_channel_id=None,
            homework_channel_id=None,
            grades_channel_id=None,
            events_channel_id=None,
            webhooks_channel_id=None,
            absences_channel_id=None,
            messages_channel_id=None,
            letters_channel_id=None,
            active=True,
            last_sync_ts=0,
            last_error=None,
            last_digest_date=None,
            forum_channel_id=existing.forum_channel_id if existing else None,
            dashboard_thread_id=existing.dashboard_thread_id if existing else None,
        )
        await self.store.upsert_user(state)
        await self._manage_logged_in_role(interaction.guild, interaction.user.id, add=True)

        try:
            await self._sync_user(state, reason="initial", force_refresh=True)
        except Exception as exc:
            await interaction.followup.send(f"Login ok, aber Einrichtung fehlgeschlagen: {exc}", ephemeral=True)
            return

        fresh = await self.store.get_user(interaction.guild.id, interaction.user.id)
        if fresh is None or fresh.forum_channel_id is None:
            await interaction.followup.send(
                "Login ok, aber das Forum konnte nicht erstellt werden. Hat der Bot die Rechte "
                "**Kanäle verwalten** und **Öffentliche Threads erstellen**?",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"✅ Login erfolgreich! Dein privates Forum: <#{fresh.forum_channel_id}> — oben ist das 📊 Dashboard angepinnt.",
            ephemeral=True,
        )

    @app_commands.command(name="logout", description="Bot-Zugang entfernen")
    async def logout(self, interaction: discord.Interaction, delete_forum: bool = False) -> None:
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

        if delete_forum and state.forum_channel_id:
            forum = interaction.guild.get_channel(state.forum_channel_id)
            if forum is not None:
                try:
                    await forum.delete(reason="Schulmanager logout")
                except discord.HTTPException:
                    pass

        await self.store.delete_user(interaction.guild.id, interaction.user.id)
        await self._manage_logged_in_role(interaction.guild, interaction.user.id, add=False)
        await interaction.followup.send("✅ Abgemeldet.", ephemeral=True)

    @app_commands.command(name="sync", description="Manuelle Aktualisierung auslösen")
    async def sync_now(self, interaction: discord.Interaction) -> None:
        state = await self._require_state(interaction)
        if state is None:
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
        state = await self._require_state(interaction, require_active=False)
        if state is None:
            return
        last_sync = "Nie" if state.last_sync_ts <= 0 else f"<t:{state.last_sync_ts}:R>"
        color = discord.Color.green() if state.active and not state.last_error else (discord.Color.orange() if state.last_error else discord.Color.red())
        embed = discord.Embed(title="📡 Bot-Status", color=color, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Schüler", value=f"{state.student_name} (`{state.student_id}`)", inline=False)
        if state.forum_channel_id:
            embed.add_field(name="Forum", value=f"<#{state.forum_channel_id}>", inline=True)
        embed.add_field(name="Letzter Sync", value=last_sync, inline=True)
        embed.add_field(name="Aktiv", value="✅ Ja" if state.active else "❌ Nein", inline=True)
        if state.last_error:
            embed.add_field(name="⚠️ Letzter Fehler", value=state.last_error[:200], inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="calendar", description="ICS-Kalender als DM senden")
    async def calendar(self, interaction: discord.Interaction) -> None:
        state = await self._require_state(interaction)
        if state is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._send_calendar_dm(interaction, state)

    @app_commands.command(name="digest", description="Tages-Zusammenfassung jetzt posten")
    async def digest_now(self, interaction: discord.Interaction) -> None:
        state = await self._require_state(interaction)
        if state is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            state = await self._ensure_valid_tokens(state)
            guild = interaction.guild
            assert guild is not None
            await self._send_digest(guild, state)
        except Exception as exc:
            await interaction.followup.send(f"Digest fehlgeschlagen: {exc}", ephemeral=True)
            return
        await interaction.followup.send("Tages-Zusammenfassung wurde gepostet.", ephemeral=True)

    @app_commands.command(name="threads", description="Zeigt/verwaltet deine Forum-Threads")
    async def threads_cmd(self, interaction: discord.Interaction) -> None:
        state = await self._require_state(interaction, require_active=False)
        if state is None:
            return
        sections = {s.section: s for s in await self.store.list_forum_sections(state.guild_id, state.user_id)}
        enabled_map = {key: (sections[key].enabled if key in sections else True) for key in SECTION_KEYS}
        lines = [f"Forum: <#{state.forum_channel_id}>" if state.forum_channel_id else "Forum: -"]
        for key in SECTION_KEYS:
            lines.append(f"{'✅' if enabled_map[key] else '⬜'} {SECTION_TITLES[key]}")
        await interaction.response.send_message(
            "\n".join(lines) + "\n\nMit den Buttons unten schaltest du Threads an/aus:",
            view=SectionManageView(self, enabled_map),
            ephemeral=True,
        )

    # /remind group
    remind_group = app_commands.Group(name="remind", description="Erinnerungen konfigurieren")

    @remind_group.command(name="exams", description="Prüfungs-Erinnerung X Stunden vorher aktivieren")
    async def remind_exams(self, interaction: discord.Interaction, hours_before: int) -> None:
        await self._set_reminder(interaction, "exam", hours_before)

    @remind_group.command(name="homework", description="Hausaufgaben-Erinnerung X Stunden vorher aktivieren")
    async def remind_homework(self, interaction: discord.Interaction, hours_before: int) -> None:
        await self._set_reminder(interaction, "homework", hours_before)

    @remind_group.command(name="off", description="Erinnerung deaktivieren")
    @app_commands.choices(reminder_type=[
        app_commands.Choice(name="Klausuren", value="exam"),
        app_commands.Choice(name="Hausaufgaben", value="homework"),
    ])
    async def remind_off(self, interaction: discord.Interaction, reminder_type: str) -> None:
        state = await self._require_state(interaction, require_active=False)
        if state is None:
            return
        await self.store.delete_reminder_rule(state.guild_id, state.user_id, reminder_type)
        label = "Prüfungs-Erinnerung" if reminder_type == "exam" else "Hausaufgaben-Erinnerung"
        await interaction.response.send_message(f"✅ {label} deaktiviert.", ephemeral=True)

    async def _set_reminder(self, interaction: discord.Interaction, reminder_type: str, hours_before: int) -> None:
        state = await self._require_state(interaction, require_active=False)
        if state is None:
            return
        if hours_before < 1 or hours_before > 168:
            await interaction.response.send_message("Stunden müssen zwischen 1 und 168 liegen.", ephemeral=True)
            return
        await self.store.upsert_reminder_rule(ReminderRule(state.guild_id, state.user_id, reminder_type, hours_before))
        type_label = "Prüfung" if reminder_type == "exam" else "Hausaufgabe"
        await interaction.response.send_message(f"Erinnerung gesetzt: **{type_label}** — {hours_before} Stunden vorher als DM.", ephemeral=True)

    # /notify group
    notify_group = app_commands.Group(name="notify", description="Benachrichtigungen konfigurieren")

    @notify_group.command(name="schedule-changes", description="DM bei Stundenplanänderungen (Ausfall/Vertretung)")
    @app_commands.choices(enabled=[app_commands.Choice(name="An", value=1), app_commands.Choice(name="Aus", value=0)])
    async def notify_schedule_changes(self, interaction: discord.Interaction, enabled: int) -> None:
        await self._set_notification_pref(interaction, "schedule_changes", bool(enabled))

    @notify_group.command(name="digest", description="Tägliche Zusammenfassung im Dashboard")
    @app_commands.choices(enabled=[app_commands.Choice(name="An", value=1), app_commands.Choice(name="Aus", value=0)])
    async def notify_digest(self, interaction: discord.Interaction, enabled: int) -> None:
        await self._set_notification_pref(interaction, "digest", bool(enabled))

    @notify_group.command(name="letters", description="DM bei neuen bestätigungspflichtigen Elternbriefen")
    @app_commands.choices(enabled=[app_commands.Choice(name="An", value=1), app_commands.Choice(name="Aus", value=0)])
    async def notify_letters(self, interaction: discord.Interaction, enabled: int) -> None:
        await self._set_notification_pref(interaction, "letters", bool(enabled))

    @notify_group.command(name="status", description="Aktuelle Benachrichtigungs-Einstellungen anzeigen")
    async def notify_status(self, interaction: discord.Interaction) -> None:
        state = await self._require_state(interaction, require_active=False)
        if state is None:
            return
        prefs = await self.store.list_notification_prefs(state.guild_id, state.user_id)
        embed = discord.Embed(title="🔔 Benachrichtigungen", color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))
        defaults = {"schedule_changes": True, "digest": True, "letters": True}
        labels = {"schedule_changes": "Stundenplan-Änderungen", "digest": "Tages-Digest", "letters": "Elternbriefe (Bestätigung)"}
        for key, default in defaults.items():
            embed.add_field(name=labels[key], value="✅ An" if prefs.get(key, default) else "❌ Aus", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _set_notification_pref(self, interaction: discord.Interaction, pref_key: str, enabled: bool) -> None:
        state = await self._require_state(interaction, require_active=False)
        if state is None:
            return
        await self.store.set_notification_pref(state.guild_id, state.user_id, pref_key, enabled)
        await interaction.response.send_message(f"✅ **{pref_key}** {'aktiviert' if enabled else 'deaktiviert'}.", ephemeral=True)

    @app_commands.command(name="info", description="Zeigt allgemeine Bot-Informationen")
    async def info(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or interaction.user is None:
            await interaction.response.send_message("Bitte in einem Server ausführen.", ephemeral=True)
            return
        state = await self.store.get_user(interaction.guild.id, interaction.user.id)
        next_sync = self.sync_loop.next_iteration
        next_sync_text = f"<t:{int(next_sync.timestamp())}:R>" if isinstance(next_sync, datetime) else "unbekannt"
        embed = discord.Embed(title="ℹ️ Schulmanager Bot", color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))
        embed.add_field(name="API", value=self.settings.discord_api_base_url, inline=False)
        embed.add_field(name="Sync-Intervall", value=f"{self.settings.discord_sync_interval_seconds}s", inline=True)
        embed.add_field(name="Nächster Auto-Sync", value=next_sync_text, inline=True)
        if state is not None:
            embed.add_field(name="Eingeloggt als", value=f"{state.student_name} (`{state.student_id}`)", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="debug-state", description="Debug-Infos für deinen Account")
    async def debug_state(self, interaction: discord.Interaction) -> None:
        state = await self._require_state(interaction, require_active=False)
        if state is None:
            return
        now_ts = int(time.time())
        lines = [
            f"account_id={state.account_id}", f"student_id={state.student_id}", f"active={state.active}",
            f"forum={state.forum_channel_id}", f"dashboard={state.dashboard_thread_id}",
            f"access_exp_in={state.access_expires_at - now_ts}s", f"refresh_exp_in={state.refresh_expires_at - now_ts}s",
            f"last_error={state.last_error or '-'}",
        ]
        try:
            me = await self.api.get_me(state.access_token)
            lines.append(f"/auth/me ok: {me.get('account_id', '-')}")
        except ApiClientError as exc:
            lines.append(f"/auth/me failed: {exc}")
        await interaction.response.send_message("```\n" + "\n".join(lines) + "\n```", ephemeral=True)

    # ─── Admin commands ───────────────────────────────────────────────────────

    @app_commands.command(name="admin-users", description="(Admin) Zeigt alle Bot-User im Server")
    async def admin_users(self, interaction: discord.Interaction) -> None:
        if not await self._admin_guard(interaction):
            return
        assert interaction.guild is not None
        users = await self.store.list_users_for_guild(interaction.guild.id)
        if not users:
            await interaction.response.send_message("Keine Bot-User gespeichert.", ephemeral=True)
            return
        lines = []
        for row in users[:25]:
            member = interaction.guild.get_member(row.user_id)
            display = member.display_name if member else str(row.user_id)
            lines.append(f"- {display} (`{row.user_id}`) | {'✅' if row.active else '❌'} | `{row.student_id}`")
        embed = discord.Embed(title="👥 Bot-Nutzer", color=discord.Color.dark_teal(), description="\n".join(lines))
        embed.set_footer(text=f"{len(users)} Nutzer")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="admin-sync-all", description="(Admin) Sync für alle aktiven Bot-User")
    async def admin_sync_all(self, interaction: discord.Interaction) -> None:
        if not await self._admin_guard(interaction):
            return
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        users = [u for u in await self.store.list_users_for_guild(interaction.guild.id) if u.active]
        ok = failed = 0
        errors: list[str] = []
        for row in users:
            try:
                await self._sync_user(row, reason="admin-bulk", force_refresh=True)
                ok += 1
            except Exception as exc:
                failed += 1
                errors.append(f"{row.user_id}: {exc}")
        embed = discord.Embed(title="🔄 Bulk-Sync", color=discord.Color.green() if failed == 0 else discord.Color.orange())
        embed.add_field(name="Aktiv", value=str(len(users)), inline=True)
        embed.add_field(name="✅", value=str(ok), inline=True)
        embed.add_field(name="❌", value=str(failed), inline=True)
        if errors:
            embed.add_field(name="Fehler", value="\n".join(f"• {e}" for e in errors[:5]), inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="admin-user-active", description="(Admin) Aktiv/Inaktiv für einen Bot-User")
    async def admin_user_active(self, interaction: discord.Interaction, user: discord.Member, active: bool) -> None:
        if not await self._admin_guard(interaction):
            return
        assert interaction.guild is not None
        state = await self.store.get_user(interaction.guild.id, user.id)
        if state is None:
            await interaction.response.send_message("Kein Bot-Login für diesen User.", ephemeral=True)
            return
        await self.store.set_active(interaction.guild.id, user.id, active)
        await interaction.response.send_message(f"{user.display_name}: active={active}", ephemeral=True)

    @app_commands.command(name="admin-errors", description="(Admin) Letzte Sync-Fehler aller Bot-User")
    async def admin_errors(self, interaction: discord.Interaction) -> None:
        if not await self._admin_guard(interaction):
            return
        assert interaction.guild is not None
        users = [u for u in await self.store.list_users_for_guild(interaction.guild.id) if u.last_error]
        if not users:
            await interaction.response.send_message("Keine Sync-Fehler.", ephemeral=True)
            return
        lines = []
        for u in users[:10]:
            member = interaction.guild.get_member(u.user_id)
            lines.append(f"**{member.display_name if member else u.user_id}**: {(u.last_error or '')[:80]}")
        embed = discord.Embed(title="⚠️ Sync-Fehler", color=discord.Color.red(), description="\n".join(lines))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="admin-stats", description="(Admin) Bot-Statistiken")
    async def admin_stats(self, interaction: discord.Interaction) -> None:
        if not await self._admin_guard(interaction):
            return
        assert interaction.guild is not None
        users = await self.store.list_users_for_guild(interaction.guild.id)
        embed = discord.Embed(title="Bot-Statistiken", color=discord.Color.dark_teal())
        embed.add_field(name="Registriert", value=str(len(users)), inline=True)
        embed.add_field(name="Aktiv", value=str(sum(1 for u in users if u.active)), inline=True)
        embed.add_field(name="Mit Fehlern", value=str(sum(1 for u in users if u.last_error)), inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="admin-purge", description="(Admin) User-Forum und Daten vollständig entfernen")
    async def admin_purge(self, interaction: discord.Interaction, user: discord.Member) -> None:
        if not await self._admin_guard(interaction):
            return
        assert interaction.guild is not None
        state = await self.store.get_user(interaction.guild.id, user.id)
        if state is None:
            await interaction.response.send_message("Kein Bot-Eintrag.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if state.forum_channel_id:
            forum = interaction.guild.get_channel(state.forum_channel_id)
            if forum is not None:
                try:
                    await forum.delete(reason=f"Admin purge by {interaction.user}")
                except discord.HTTPException:
                    pass
        await self.store.delete_user(interaction.guild.id, user.id)
        await interaction.followup.send(f"User **{user.display_name}** entfernt.", ephemeral=True)

    @app_commands.command(name="admin-flush-cache", description="(Admin) API-Cache leeren")
    async def admin_flush_cache(self, interaction: discord.Interaction) -> None:
        if not await self._admin_guard(interaction):
            return
        assert interaction.guild is not None
        state = await self.store.get_user(interaction.guild.id, interaction.user.id)
        if state is None:
            await interaction.response.send_message("Bitte zuerst /login.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            state = await self._ensure_valid_tokens(state)
            await self.api.flush_cache(state.access_token)
            await interaction.followup.send("✅ API-Cache geleert.", ephemeral=True)
        except ApiClientError as exc:
            await interaction.followup.send(f"Fehler: {exc}", ephemeral=True)

    # ─── Forum dashboard button handlers ──────────────────────────────────────

    async def _forum_dashboard_sync(self, interaction: discord.Interaction) -> None:
        state = await self._state_from_dashboard(interaction)
        if state is None:
            await interaction.response.send_message("Dieses Dashboard gehört nicht zu deinem Account.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self._sync_user(state, reason="button", force_refresh=True)
        except Exception as exc:
            await interaction.followup.send(f"Sync fehlgeschlagen: {exc}", ephemeral=True)
            return
        await interaction.followup.send("✅ Sync abgeschlossen.", ephemeral=True)

    async def _forum_dashboard_calendar(self, interaction: discord.Interaction) -> None:
        state = await self._state_from_dashboard(interaction)
        if state is None:
            await interaction.response.send_message("Nicht dein Dashboard.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._send_calendar_dm(interaction, state)

    async def _forum_dashboard_manage(self, interaction: discord.Interaction) -> None:
        state = await self._state_from_dashboard(interaction)
        if state is None:
            await interaction.response.send_message("Nicht dein Dashboard.", ephemeral=True)
            return
        sections = {s.section: s for s in await self.store.list_forum_sections(state.guild_id, state.user_id)}
        enabled_map = {key: (sections[key].enabled if key in sections else True) for key in SECTION_KEYS}
        await interaction.response.send_message(
            "Threads an/aus schalten (grün = an):",
            view=SectionManageView(self, enabled_map),
            ephemeral=True,
        )

    async def _forum_toggle_section(self, interaction: discord.Interaction, section: str) -> None:
        state = await self._state_from_dashboard(interaction)
        if state is None:
            await interaction.response.send_message("Nicht dein Dashboard.", ephemeral=True)
            return

        rec = await self.store.get_forum_section(state.guild_id, state.user_id, section)
        currently_enabled = rec.enabled if rec else True
        new_enabled = not currently_enabled
        guild = interaction.guild

        if new_enabled:
            await self.store.set_forum_section_enabled(state.guild_id, state.user_id, section, True)
        else:
            if rec and rec.thread_id and guild is not None:
                thread = await self._get_thread(guild, rec.thread_id)
                if thread is not None:
                    try:
                        await thread.delete()
                    except discord.HTTPException:
                        pass
            await self.store.upsert_forum_section(state.guild_id, state.user_id, section, thread_id=None, enabled=False, fingerprint=None)

        sections = {s.section: s for s in await self.store.list_forum_sections(state.guild_id, state.user_id)}
        enabled_map = {key: (sections[key].enabled if key in sections else True) for key in SECTION_KEYS}
        try:
            await interaction.response.edit_message(view=SectionManageView(self, enabled_map))
        except discord.HTTPException:
            pass

        if new_enabled:
            asyncio.create_task(self._safe_background_sync(state))

    async def _safe_background_sync(self, state: UserWorkspaceState) -> None:
        try:
            await self._sync_user(state, reason="toggle", force_refresh=False)
        except Exception as exc:  # pragma: no cover
            LOGGER.warning("Background sync after toggle failed for user=%s: %s", state.user_id, exc)

    # ─── Background loops ─────────────────────────────────────────────────────

    @tasks.loop(seconds=120)
    async def sync_loop(self) -> None:
        for state in await self.store.list_active_users():
            try:
                await self._sync_user(state, reason="auto", force_refresh=False)
            except Exception as exc:  # pragma: no cover
                LOGGER.exception("Auto sync failed for guild=%s user=%s: %s", state.guild_id, state.user_id, exc)

    @sync_loop.before_loop
    async def before_sync_loop(self) -> None:
        await self.bot.wait_until_ready()
        await self._relogin_all_users_on_startup()

    async def _relogin_all_users_on_startup(self) -> None:
        users = await self.store.list_active_users()
        if not users:
            return
        LOGGER.info("Startup relogin: %d active users", len(users))
        for state in users:
            if not state.password:
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
            except ApiClientError as exc:
                LOGGER.warning("Startup relogin failed for guild=%s user=%s: %s", state.guild_id, state.user_id, exc)

    @tasks.loop(minutes=5)
    async def reminder_loop(self) -> None:
        for rule in await self.store.list_all_reminder_rules():
            try:
                await self._process_reminder(rule)
            except Exception as exc:
                LOGGER.warning("Reminder failed for guild=%s user=%s: %s", rule.guild_id, rule.user_id, exc)

    @reminder_loop.before_loop
    async def before_reminder_loop(self) -> None:
        await self.bot.wait_until_ready()
        try:
            await self.store.purge_old_dedup(90)
        except Exception as exc:  # pragma: no cover
            LOGGER.warning("Dedup purge failed: %s", exc)

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
        if (now.hour, now.minute) < (digest_hour, digest_minute):
            return
        today_str = now.date().isoformat()
        for state in await self.store.list_active_users():
            if state.last_digest_date == today_str:
                continue
            if not await self.store.get_notification_pref(state.guild_id, state.user_id, "digest", default=True):
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

    # ─── DMs & reminders ──────────────────────────────────────────────────────

    async def _process_schedule_change_dms(self, state: UserWorkspaceState, schedule: list[dict[str, Any]]) -> None:
        if not await self.store.get_notification_pref(state.guild_id, state.user_id, "schedule_changes", default=True):
            return
        change_types = {"cancellation", "substitution", "room_change"}
        tz = resolve_timezone(self.settings.discord_timezone)
        for day_raw in schedule:
            if not isinstance(day_raw, dict):
                continue
            day_date_str = str(day_raw.get("date") or "")
            for lesson in (day_raw.get("lessons") or []):
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
                icon = {"cancellation": "❌", "substitution": "🔄", "room_change": "🏫"}.get(ct, "⚠️")
                label = {"cancellation": "Ausfall", "substitution": "Vertretung", "room_change": "Raumwechsel"}.get(ct, "Änderung")
                desc = [f"Fach: **{subject}**", f"Zeit: **{start_time}**", f"Datum: **{day_date_str}**"]
                for field in ("teacher", "room", "note"):
                    val = str(lesson.get(field) or "").strip()
                    if val:
                        desc.append(f"{field.capitalize()}: {val}")
                embed = discord.Embed(title=f"{icon} Stundenplan: {label}", color=discord.Color.orange(), description="\n".join(desc), timestamp=datetime.now(tz))
                embed.set_footer(text=state.student_name)
                await self._dm_user(state.user_id, embed)

    async def _process_letter_notifications(self, state: UserWorkspaceState, letters: list[dict[str, Any]]) -> None:
        if not await self.store.get_notification_pref(state.guild_id, state.user_id, "letters", default=True):
            return
        tz = resolve_timezone(self.settings.discord_timezone)
        for letter in letters:
            if not isinstance(letter, dict) or letter.get("read") or not letter.get("requires_confirmation"):
                continue
            letter_id = str(letter.get("id") or "")
            if not letter_id or await self.store.has_sent_reminder(state.guild_id, state.user_id, letter_id, "letter"):
                continue
            title = str(letter.get("title") or "Elternbrief")
            sender = str(letter.get("sender") or "").strip()
            desc = f"**{title}**" + (f"\nvon {sender}" if sender else "") + "\n\n⚠️ Bestätigung erforderlich — bitte im Schulmanager bestätigen."
            embed = discord.Embed(title="✉️ Neuer Elternbrief", color=discord.Color.gold(), description=desc, timestamp=datetime.now(tz))
            embed.set_footer(text=state.student_name)
            if await self._dm_user(state.user_id, embed):
                await self.store.mark_reminder_sent(state.guild_id, state.user_id, letter_id, "letter")

    async def _process_reminder(self, rule: ReminderRule) -> None:
        state = await self.store.get_user(rule.guild_id, rule.user_id)
        if state is None or not state.active:
            return
        state = await self._ensure_valid_tokens(state)
        now = datetime.now(timezone.utc)
        threshold = now + timedelta(hours=rule.hours_before)
        tz = resolve_timezone(self.settings.discord_timezone)

        if rule.reminder_type == "exam":
            items = await self.api.get_exams(state.access_token, state.student_id, force_refresh=False)
            date_field, title, color, unit = "date", "📝 Prüfungs-Erinnerung", discord.Color.orange(), "Prüfung"
        else:
            items = await self.api.get_homework(state.access_token, state.student_id, open_only=True, force_refresh=False)
            date_field, title, color, unit = "due_date", "📚 Hausaufgaben-Erinnerung", discord.Color.yellow(), "Hausaufgabe"

        for item in items:
            item_date_str = str(item.get(date_field) or "")
            if not item_date_str:
                continue
            try:
                item_date = date.fromisoformat(item_date_str[:10])
            except ValueError:
                continue
            item_dt = datetime(item_date.year, item_date.month, item_date.day, 8, 0, tzinfo=tz)
            if not (now < item_dt <= threshold):
                continue
            subject = str(item.get("subject") or "Fach")
            item_id = str(item.get("id") or f"{item_date_str}:{subject}")
            if await self.store.has_sent_reminder(rule.guild_id, rule.user_id, item_id, rule.reminder_type):
                continue
            delta_hours = int((item_dt - now).total_seconds() / 3600)
            detail = str(item.get("topic") or item.get("text") or "")
            embed = discord.Embed(
                title=f"{title}: {subject}", color=color,
                description=f"In **{delta_hours} Stunden**!\n\nFach: **{subject}**\n{unit}: {detail[:200] or '-'}\nWann: <t:{int(item_dt.timestamp())}:D>",
                timestamp=datetime.now(timezone.utc),
            )
            if await self._dm_user(rule.user_id, embed):
                await self.store.mark_reminder_sent(rule.guild_id, rule.user_id, item_id, rule.reminder_type)

    async def _send_digest(self, guild: discord.Guild, state: UserWorkspaceState) -> None:
        thread = await self._get_thread(guild, state.dashboard_thread_id)
        if thread is None:
            return
        tz = resolve_timezone(self.settings.discord_timezone)
        today = datetime.now(tz).date()
        today_str = today.isoformat()
        to_date = today + timedelta(days=21)

        schedule = await self.api.get_schedule(state.access_token, state.student_id, today, to_date, force_refresh=False)
        homework = await self.api.get_homework(state.access_token, state.student_id, open_only=False, force_refresh=False)
        exams = await self.api.get_exams(state.access_token, state.student_id, force_refresh=False)
        letters = await self.api.get_letters(state.access_token, state.student_id, force_refresh=False)

        today_lessons = next((d.get("lessons", []) for d in schedule if d.get("date") == today_str), [])
        today_hw = [h for h in homework if h.get("due_date") == today_str and not h.get("done")]
        next_7 = (today + timedelta(days=7)).isoformat()
        upcoming_exams = [e for e in exams if today_str <= str(e.get("date") or "") <= next_7]
        unread_letters = [l for l in letters if isinstance(l, dict) and not l.get("read")]

        embed = discord.Embed(title=f"☀️ Tages-Digest — {today.strftime('%A, %d.%m.%Y')}", color=discord.Color.blurple(), timestamp=datetime.now(tz))
        embed.set_author(name=state.student_name)
        if today_lessons:
            lines = []
            for lesson in today_lessons[:6]:
                ct = str(lesson.get("change_type") or "")
                icon = "❌" if ct == "cancellation" else ("🔄" if ct in ("substitution", "room_change") else "📖")
                lines.append(f"{icon} {lesson.get('start_time', '')} **{lesson.get('subject', 'Fach')}**")
            embed.add_field(name=f"📅 Heute ({len(today_lessons)})", value=_clip("\n".join(lines), 1024), inline=False)
        else:
            embed.add_field(name="📅 Heute", value="Keine Stunden", inline=False)
        if today_hw:
            embed.add_field(name=f"📚 Heute fällig ({len(today_hw)})", value=_clip("\n".join(f"• **{h.get('subject', 'Fach')}**: {str(h.get('text', ''))[:80]}" for h in today_hw[:5]), 1024), inline=False)
        if upcoming_exams:
            embed.add_field(name=f"📝 Klausuren (7 Tage, {len(upcoming_exams)})", value=_clip("\n".join(f"• {e.get('date', '')} **{e.get('subject', 'Fach')}**" for e in upcoming_exams[:5]), 1024), inline=False)
        if unread_letters:
            embed.add_field(name=f"✉️ Ungelesene Elternbriefe ({len(unread_letters)})", value=_clip("\n".join(f"• {'⚠️ ' if l.get('requires_confirmation') else ''}{str(l.get('title', ''))[:80]}" for l in unread_letters[:5]), 1024), inline=False)
        if not embed.fields:
            embed.description = "Heute keine besonderen Ereignisse."
        try:
            await thread.send(embed=embed)
        except discord.HTTPException:
            pass

    # ─── Sync internals ───────────────────────────────────────────────────────

    async def _sync_user(self, state: UserWorkspaceState, *, reason: str, force_refresh: bool) -> None:
        key = (state.guild_id, state.user_id)
        lock = self._user_locks.setdefault(key, asyncio.Lock())
        async with lock:
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

            forum, dashboard = await self._ensure_forum(guild, member, state)
            if forum is None:
                await self.store.update_sync_status(state.guild_id, state.user_id, last_sync_ts=int(time.time()),
                                                    last_error="Forum konnte nicht erstellt werden (Bot-Rechte?).")
                return
            state = replace(state, forum_channel_id=forum.id, dashboard_thread_id=dashboard.id if dashboard else None)

            try:
                await self._run_sync_iteration(guild=guild, state=state, reason=reason, force_refresh=force_refresh)
            except Exception as exc:
                requires_relogin = self._requires_relogin(exc)
                error_text = str(exc)
                if requires_relogin:
                    if await self._attempt_auto_relogin(guild=guild, state=state, reason=reason, force_refresh=force_refresh):
                        return
                    error_text = self._session_notice_text(exc)
                    await self.store.set_active(state.guild_id, state.user_id, False)
                await self.store.update_sync_status(state.guild_id, state.user_id, last_sync_ts=int(time.time()), last_error=error_text)
                if requires_relogin and reason == "auto":
                    return
                raise

    async def _run_sync_iteration(self, *, guild: discord.Guild, state: UserWorkspaceState, reason: str, force_refresh: bool) -> UserWorkspaceState:
        state = await self._ensure_valid_tokens(state)
        await self.store.upsert_user(state)
        data = await self._fetch_payloads(state, force_refresh=force_refresh)
        await self._process_schedule_change_dms(state, data["schedule"])
        await self._process_letter_notifications(state, data.get("letters", []))
        await self._publish_forum(guild, state, data)
        await self._publish_dashboard(guild, state, data, reason=reason)
        await self.store.update_sync_status(state.guild_id, state.user_id, last_sync_ts=int(time.time()), last_error=None)
        return state

    async def _attempt_auto_relogin(self, *, guild: discord.Guild, state: UserWorkspaceState, reason: str, force_refresh: bool) -> bool:
        if not state.password:
            return False
        LOGGER.info("Auto re-login for guild=%s user=%s", state.guild_id, state.user_id)
        try:
            login_response = await self.api.login(email=state.email, password=state.password)
        except ApiClientError as exc:
            LOGGER.warning("Auto re-login (login) failed: %s", exc)
            return False
        selected_student_id = state.student_id
        if selected_student_id not in login_response.student_ids:
            if not login_response.student_ids:
                return False
            selected_student_id = login_response.student_ids[0]
        now_ts = int(time.time())
        refreshed = replace(
            state, student_id=selected_student_id, account_id=login_response.account_id,
            access_token=login_response.access_token, refresh_token=login_response.refresh_token,
            access_expires_at=now_ts + max(login_response.expires_in, 1),
            refresh_expires_at=now_ts + max(login_response.refresh_expires_in, 1),
            active=True, last_error=None,
        )
        await self.store.upsert_user(refreshed)
        try:
            await self._run_sync_iteration(guild=guild, state=refreshed, reason=f"{reason}-relogin", force_refresh=True)
        except Exception as exc:
            LOGGER.warning("Auto re-login (sync) failed: %s", exc)
            return False
        return True

    async def _ensure_valid_tokens(self, state: UserWorkspaceState) -> UserWorkspaceState:
        now = int(time.time())
        if state.access_expires_at - now > 90:
            return state
        refreshed = await self.api.refresh(state.refresh_token)
        updated = replace(
            state, access_token=refreshed.access_token, refresh_token=refreshed.refresh_token,
            access_expires_at=now + max(refreshed.expires_in, 1), refresh_expires_at=now + max(refreshed.refresh_expires_in, 1),
        )
        await self.store.update_tokens(state.guild_id, state.user_id, updated.access_token, updated.refresh_token, updated.access_expires_at, updated.refresh_expires_at)
        return updated

    @staticmethod
    def _data_looks_empty(data: dict[str, Any]) -> bool:
        return not any(data.get(k) for k in ("schedule", "grades", "events", "homework", "absences", "messages", "letters"))

    async def _fetch_payloads(self, state: UserWorkspaceState, *, force_refresh: bool) -> dict[str, Any]:
        tz = resolve_timezone(self.settings.discord_timezone)
        today = datetime.now(tz).date()
        to_date = today + timedelta(days=21)
        sid = state.student_id

        async def fetch_once(token: str) -> dict[str, Any]:
            coros = {
                "schedule": self.api.get_schedule(token, sid, from_date=today, to_date=to_date, force_refresh=force_refresh),
                "homework": self.api.get_homework(token, sid, open_only=False, force_refresh=force_refresh),
                "grades": self.api.get_grades(token, sid, force_refresh=force_refresh),
                "grade_stats": self.api.get_grade_stats(token, sid, force_refresh=force_refresh),
                "exams": self.api.get_exams(token, sid, force_refresh=force_refresh),
                "events": self.api.get_events(token, sid, force_refresh=force_refresh),
                "absences": self.api.get_absences(token, sid, force_refresh=force_refresh),
                "messages": self.api.get_messages(token, sid, force_refresh=force_refresh),
                "letters": self.api.get_letters(token, sid, force_refresh=force_refresh),
                "payments": self.api.get_payments(token, sid, force_refresh=force_refresh),
                "learning": self.api.get_learning(token, sid, force_refresh=force_refresh),
            }
            labels = list(coros.keys())
            results = await asyncio.gather(*coros.values(), return_exceptions=True)
            out: dict[str, Any] = {}
            for label, result in zip(labels, results):
                if isinstance(result, ApiClientError) and result.status_code == 401:
                    raise result
                if isinstance(result, Exception):
                    LOGGER.warning("Endpoint '%s' failed for user=%s: %s", label, state.user_id, result)
                    out[label] = {} if label == "grade_stats" else []
                else:
                    out[label] = result
            return out

        try:
            data = await fetch_once(state.access_token)
        except ApiClientError as exc:
            if exc.status_code != 401:
                raise
            refreshed = await self._ensure_valid_tokens(replace(state, access_expires_at=0))
            await self.store.upsert_user(refreshed)
            data = await fetch_once(refreshed.access_token)

        if self._data_looks_empty(data) and state.password:
            try:
                login_response = await self.api.login(email=state.email, password=state.password)
                now_ts = int(time.time())
                refreshed = replace(
                    state, account_id=login_response.account_id, access_token=login_response.access_token,
                    refresh_token=login_response.refresh_token,
                    access_expires_at=now_ts + max(login_response.expires_in, 1),
                    refresh_expires_at=now_ts + max(login_response.refresh_expires_in, 1),
                )
                await self.store.upsert_user(refreshed)
                data = await fetch_once(refreshed.access_token)
            except ApiClientError as exc:
                LOGGER.warning("Re-login after empty data failed for user=%s: %s", state.user_id, exc)
                raise
        return data

    # ─── Forum presentation ───────────────────────────────────────────────────

    async def _ensure_forum(self, guild: discord.Guild, member: discord.Member, state: UserWorkspaceState) -> tuple[discord.ForumChannel | None, discord.Thread | None]:
        forum: discord.ForumChannel | None = None
        if state.forum_channel_id:
            ch = guild.get_channel(state.forum_channel_id)
            if isinstance(ch, discord.ForumChannel):
                forum = ch

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(view_channel=True, read_message_history=True, send_messages_in_threads=True),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, manage_channels=True, manage_threads=True,
                send_messages=True, send_messages_in_threads=True, create_public_threads=True, read_message_history=True,
            ),
        }

        if forum is None:
            name = f"{self.settings.discord_category_prefix}-{member.display_name}".lower().replace(" ", "-")[:95]
            try:
                forum = await guild.create_forum(name=name, overwrites=overwrites)
            except (discord.Forbidden, discord.HTTPException) as exc:
                LOGGER.warning("Forum creation failed for guild=%s user=%s: %s", guild.id, member.id, exc)
                return None, None
        else:
            try:
                await forum.edit(overwrites=overwrites)
            except (discord.Forbidden, discord.HTTPException):
                pass

        dashboard = await self._ensure_dashboard_thread(guild, forum, state)
        await self.store.set_forum(state.guild_id, state.user_id, forum.id, dashboard.id if dashboard else None)
        return forum, dashboard

    async def _ensure_dashboard_thread(self, guild: discord.Guild, forum: discord.ForumChannel, state: UserWorkspaceState) -> discord.Thread | None:
        if state.dashboard_thread_id:
            existing = await self._get_thread(guild, state.dashboard_thread_id)
            if existing is not None:
                return existing
        embed = discord.Embed(title=DASHBOARD_NAME, description="Wird beim ersten Sync gefüllt …", color=discord.Color.dark_teal())
        try:
            created = await forum.create_thread(name=DASHBOARD_NAME, embed=embed, view=ForumDashboardView(self))
        except discord.HTTPException as exc:
            LOGGER.warning("Dashboard thread creation failed: %s", exc)
            return None
        thread = created.thread
        try:
            await thread.edit(pinned=True)
        except (discord.HTTPException, TypeError):
            pass
        return thread

    async def _publish_forum(self, guild: discord.Guild, state: UserWorkspaceState, data: dict[str, Any]) -> None:
        forum = guild.get_channel(state.forum_channel_id) if state.forum_channel_id else None
        if not isinstance(forum, discord.ForumChannel):
            return
        sections = {s.section: s for s in await self.store.list_forum_sections(state.guild_id, state.user_id)}

        for key in SECTION_KEYS:
            rec = sections.get(key)
            enabled = rec.enabled if rec else True
            if not enabled:
                continue

            embeds, fingerprint = render_section(key, data, self.settings.discord_timezone)
            thread = await self._get_thread(guild, rec.thread_id) if (rec and rec.thread_id) else None

            if thread is None:
                try:
                    created = await forum.create_thread(name=section_thread_name(key), embeds=embeds)
                except discord.HTTPException as exc:
                    LOGGER.warning("Section thread create failed (%s): %s", key, exc)
                    continue
                await self.store.upsert_forum_section(state.guild_id, state.user_id, key, thread_id=created.thread.id, enabled=True, fingerprint=fingerprint)
            else:
                if rec and rec.fingerprint == fingerprint:
                    continue
                try:
                    await thread.get_partial_message(thread.id).edit(embeds=embeds)
                    await self.store.upsert_forum_section(state.guild_id, state.user_id, key, thread_id=thread.id, enabled=True, fingerprint=fingerprint)
                except discord.NotFound:
                    try:
                        created = await forum.create_thread(name=section_thread_name(key), embeds=embeds)
                        await self.store.upsert_forum_section(state.guild_id, state.user_id, key, thread_id=created.thread.id, enabled=True, fingerprint=fingerprint)
                    except discord.HTTPException:
                        pass
                except discord.HTTPException as exc:
                    LOGGER.warning("Section thread edit failed (%s): %s", key, exc)
            await asyncio.sleep(0.4)

    async def _publish_dashboard(self, guild: discord.Guild, state: UserWorkspaceState, data: dict[str, Any], *, reason: str) -> None:
        thread = await self._get_thread(guild, state.dashboard_thread_id)
        if thread is None:
            return
        embed = self._build_dashboard_embed(state, data, reason)
        try:
            await thread.get_partial_message(thread.id).edit(embed=embed, view=ForumDashboardView(self))
        except discord.HTTPException:
            pass

    def _build_dashboard_embed(self, state: UserWorkspaceState, data: dict[str, Any], reason: str) -> discord.Embed:
        tz = resolve_timezone(self.settings.discord_timezone)
        now_epoch = int(time.time())
        today_str = datetime.now(tz).date().isoformat()

        schedule = data.get("schedule") or []
        homework = data.get("homework") or []
        messages = data.get("messages") or []
        letters = data.get("letters") or []
        payments = data.get("payments") or []
        learning = data.get("learning") or []

        today_hw = [h for h in homework if h.get("due_date") == today_str and not h.get("done")]
        unread_msgs = sum(1 for m in messages if isinstance(m, dict) and not m.get("read"))
        unread_letters = sum(1 for l in letters if isinstance(l, dict) and not l.get("read"))
        open_pay = [p for p in payments if isinstance(p, dict) and not p.get("paid")]
        open_pay_sum = sum(p.get("amount") or 0 for p in open_pay if isinstance(p.get("amount"), (int, float)))
        open_learning = sum(1 for u in learning if isinstance(u, dict) and not u.get("done"))

        embed = discord.Embed(title="📊 Dashboard", color=discord.Color.dark_teal(), timestamp=datetime.now(timezone.utc))
        embed.set_author(name=state.student_name)
        embed.description = f"Letzter Sync: <t:{now_epoch}:R> • `{reason}`\n🔄 Sync · 📅 Kalender · ⚙️ Threads verwalten"

        embed.add_field(name="📅 Nächste Stunde", value=self._next_lesson_label(schedule, tz) or "—", inline=False)
        embed.add_field(name="📚 Heute fällig", value=str(len(today_hw)), inline=True)
        embed.add_field(name="📊 Noten", value=str(len(data.get("grades") or [])), inline=True)
        embed.add_field(name="📝 Klausuren", value=str(len(data.get("exams") or [])), inline=True)
        embed.add_field(name="📋 Fehlzeiten", value=str(len(data.get("absences") or [])), inline=True)
        embed.add_field(name="📬 Nachrichten", value=f"{unread_msgs} ungelesen" if unread_msgs else "0", inline=True)
        embed.add_field(name="✉️ Elternbriefe", value=f"{unread_letters} ungelesen" if unread_letters else "0", inline=True)
        embed.add_field(name="💶 Offene Zahlungen", value=(f"{len(open_pay)} ({open_pay_sum:.2f} €)" if open_pay else "0"), inline=True)
        embed.add_field(name="📓 Lernen offen", value=str(open_learning), inline=True)
        embed.add_field(name="🗓️ Termine", value=str(len(data.get("events") or [])), inline=True)
        embed.set_footer(text="Schulmanager • Threads unten in diesem Forum")
        return embed

    @staticmethod
    def _next_lesson_label(schedule: list[dict[str, Any]], tz) -> str | None:  # type: ignore[no-untyped-def]
        now = datetime.now(tz)
        best: datetime | None = None
        best_subject = ""
        for day in schedule:
            if not isinstance(day, dict):
                continue
            try:
                day_date = date.fromisoformat(str(day.get("date") or "")[:10])
            except ValueError:
                continue
            for lesson in (day.get("lessons") or []):
                if not isinstance(lesson, dict) or str(lesson.get("change_type") or "") == "cancellation":
                    continue
                start = str(lesson.get("start_time") or "")
                if ":" not in start:
                    continue
                try:
                    hh, mm = int(start.split(":")[0]), int(start.split(":")[1])
                except ValueError:
                    continue
                dt = datetime(day_date.year, day_date.month, day_date.day, hh, mm, tzinfo=tz)
                if dt >= now and (best is None or dt < best):
                    best, best_subject = dt, str(lesson.get("subject") or "Fach")
        if best is None:
            return None
        return f"**{best_subject}** — <t:{int(best.timestamp())}:F> (<t:{int(best.timestamp())}:R>)"

    async def _send_calendar_dm(self, interaction: discord.Interaction, state: UserWorkspaceState) -> None:
        try:
            state = await self._ensure_valid_tokens(state)
            ics_bytes = await self.api.get_calendar_ics(state.access_token, state.student_id)
        except ApiClientError as exc:
            await interaction.followup.send(f"Kalender-Export fehlgeschlagen: {exc}", ephemeral=True)
            return
        try:
            dm = await interaction.user.create_dm()
            await dm.send(
                f"Dein Schulmanager-Kalender für **{state.student_name}**.",
                file=discord.File(io.BytesIO(ics_bytes), filename=f"{state.student_id}.ics"),
            )
            await interaction.followup.send("Kalender wurde als DM gesendet.", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send("Konnte keine DM senden. Bitte DMs erlauben.", ephemeral=True)

    # ─── Small utilities ──────────────────────────────────────────────────────

    async def _require_state(self, interaction: discord.Interaction, *, require_active: bool = True) -> UserWorkspaceState | None:
        if interaction.guild is None or interaction.user is None:
            await interaction.response.send_message("Bitte in einem Server ausführen.", ephemeral=True)
            return None
        state = await self.store.get_user(interaction.guild.id, interaction.user.id)
        if state is None:
            await interaction.response.send_message("Bitte zuerst /login verwenden.", ephemeral=True)
            return None
        if require_active and not state.active:
            await interaction.response.send_message("Session nicht mehr aktiv. Bitte /login erneut ausführen.", ephemeral=True)
            return None
        return state

    async def _state_from_dashboard(self, interaction: discord.Interaction) -> UserWorkspaceState | None:
        if interaction.guild is None or interaction.user is None or interaction.channel is None:
            return None
        state = await self.store.get_user_by_dashboard_thread(interaction.guild.id, interaction.channel.id)
        if state is None:
            state = await self.store.get_user(interaction.guild.id, interaction.user.id)
        if state is None or state.user_id != interaction.user.id:
            return None
        return state

    async def _get_thread(self, guild: discord.Guild, thread_id: int | None) -> discord.Thread | None:
        if not thread_id:
            return None
        thread = guild.get_thread(thread_id)
        if thread is not None:
            return thread
        try:
            fetched = await guild.fetch_channel(thread_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
        return fetched if isinstance(fetched, discord.Thread) else None

    async def _dm_user(self, user_id: int, embed: discord.Embed) -> bool:
        user = self.bot.get_user(user_id)
        if user is None:
            try:
                user = await self.bot.fetch_user(user_id)
            except discord.NotFound:
                return False
        try:
            await user.send(embed=embed)
            return True
        except discord.Forbidden:
            return False

    async def _admin_guard(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or interaction.user is None:
            await interaction.response.send_message("Bitte in einem Server ausführen.", ephemeral=True)
            return False
        if not self._is_admin_interaction(interaction):
            await interaction.response.send_message("Nur für Server-Admins.", ephemeral=True)
            return False
        return True

    def _select_student(self, students: list[dict[str, Any]], student_id: str) -> dict[str, Any] | None:
        return next((s for s in students if str(s.get("id") or "") == student_id), None)

    @staticmethod
    def _student_display_name(student: dict[str, Any]) -> str:
        full = f"{str(student.get('first_name') or '').strip()} {str(student.get('last_name') or '').strip()}".strip()
        return full or str(student.get("id") or "student")

    async def _manage_logged_in_role(self, guild: discord.Guild, user_id: int, *, add: bool) -> None:
        role_id = self.settings.discord_logged_in_role_id
        if not role_id:
            return
        role = guild.get_role(role_id)
        if role is None:
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
            LOGGER.warning("Missing permission to manage role %s in guild %s", role_id, guild.id)

    @staticmethod
    def _is_admin_interaction(interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member):
            return False
        perms = interaction.user.guild_permissions
        return bool(perms.administrator or perms.manage_guild)

    @staticmethod
    def _requires_relogin(exc: Exception) -> bool:
        return isinstance(exc, ApiClientError) and exc.status_code == 401

    @staticmethod
    def _session_notice_text(exc: Exception) -> str:
        return f"Sitzung nicht mehr gültig. Bitte /login erneut ausführen. ({exc})"


class SchulmanagerDiscordBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.guild_messages = True
        super().__init__(command_prefix="!", intents=intents)
        self.settings = settings
        self.store = DiscordStateStore(settings.discord_db_path)
        self.api = SchulmanagerApiClient(settings.discord_api_base_url)

    async def setup_hook(self) -> None:
        await self.store.initialize()
        await self.add_cog(SchulmanagerCog(self, self.settings, self.store, self.api))
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
