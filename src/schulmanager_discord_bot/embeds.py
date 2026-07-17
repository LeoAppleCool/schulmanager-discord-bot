from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from functools import lru_cache
import hashlib
import json
import logging
import re
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class RenderedEmbed:
    key: str
    embed: discord.Embed
    fingerprint: str
    sort_epoch: int | None = None
    button_url: str | None = None
    button_label: str | None = None


@lru_cache(maxsize=16)
def resolve_timezone(timezone_name: str) -> tzinfo:
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        LOGGER.warning(
            "Timezone '%s' nicht gefunden. Fallback auf UTC. Installiere Paket 'tzdata' fuer IANA-Zeitzonen.",
            timezone_name,
        )
        return timezone.utc


def render_schedule_feed(
    schedule_days: list[dict[str, Any]],
    timezone_name: str,
    *,
    max_items: int = 18,
) -> list[RenderedEmbed]:
    tz = resolve_timezone(timezone_name)
    now = datetime.now(tz)
    lessons = _flatten_lessons(schedule_days, tz)
    upcoming = [entry for entry in lessons if entry["start"] >= now]
    upcoming.sort(key=lambda item: item["start"])
    upcoming = _drop_exact_duplicate_lessons(upcoming)
    upcoming = _merge_parallel_variants(upcoming)
    upcoming = _restrict_to_next_active_day(upcoming)
    upcoming = _merge_consecutive_blocks(upcoming)

    embed = discord.Embed(
        title="📅 Nächste Stunden",
        color=discord.Color.blue(),
        timestamp=datetime.now(tz),
    )

    if not upcoming:
        embed.description = "Keine kommenden Stunden gefunden."
        return [RenderedEmbed(key="upcoming", embed=embed, fingerprint=_fingerprint({"entries": []}))]

    lines: list[str] = []
    for entry in upcoming[:max_items]:
        start_epoch = int(entry["start"].timestamp())
        end_epoch = int(entry["end"].timestamp())
        badge = _lesson_duration_badge(entry["start"], entry["end"])
        icon = _change_icon(entry.get("change_type"))
        subject = _clean_subject(entry.get("subject"), entry.get("change_type"), entry.get("note"))
        teacher = _canonical_list_text(entry.get("teacher"))
        room = _canonical_list_text(entry.get("room"))
        details: list[str] = []
        if teacher:
            details.append(teacher)
        if room:
            details.append(f"Raum {room}")
        tail = f" | {' | '.join(details)}" if details else ""
        icon_prefix = f"{icon} " if icon else ""
        lines.append(
            f"{badge} {icon_prefix}**{subject}**\n<t:{start_epoch}:t>-<t:{end_epoch}:t> • <t:{start_epoch}:R>{tail}"
        )

    embed.description = "\n\n".join(lines)
    if upcoming:
        active_day = upcoming[0].get("date")
        if isinstance(active_day, date):
            embed.set_footer(
                text=f"{min(len(upcoming), max_items)} von {len(upcoming)} Einträge • {active_day.strftime('%d.%m.%Y')}"
            )
        else:
            embed.set_footer(text=f"{min(len(upcoming), max_items)} von {len(upcoming)} Einträge")
    return [RenderedEmbed(key="upcoming", embed=embed, fingerprint=_fingerprint(lines))]


def render_schedule_week(
    schedule_days: list[dict[str, Any]],
    timezone_name: str,
) -> list[RenderedEmbed]:
    tz = resolve_timezone(timezone_name)
    rendered: list[RenderedEmbed] = []

    for day in sorted(schedule_days, key=lambda row: str(row.get("date") or "")):
        day_date = _parse_date(day.get("date"))
        if day_date is None:
            continue

        lessons = day.get("lessons") or []
        title_date = day_date.strftime("%d.%m.%Y")
        title_weekday = day_date.strftime("%A")

        embed = discord.Embed(
            title=f"🗓️ {title_weekday}, {title_date}",
            color=discord.Color.teal(),
            timestamp=datetime.now(tz),
        )

        if not isinstance(lessons, list) or not lessons:
            embed.description = "Keine Einträge."
            rendered.append(
                RenderedEmbed(
                    key=day_date.isoformat(),
                    embed=embed,
                    fingerprint=_fingerprint({"date": day_date.isoformat(), "lessons": []}),
                )
            )
            continue

        day_lessons: list[dict[str, Any]] = []
        for lesson in lessons:
            if not isinstance(lesson, dict):
                continue
            start_dt = _combine(day_date, lesson.get("start_time"), tz)
            end_dt = _combine(day_date, lesson.get("end_time"), tz)
            day_lessons.append(
                {
                    "date": day_date,
                    "start": start_dt,
                    "end": end_dt,
                    "subject": str(lesson.get("subject") or "Fach"),
                    "teacher": lesson.get("teacher"),
                    "room": lesson.get("room"),
                    "change_type": lesson.get("change_type"),
                    "note": lesson.get("note"),
                }
            )

        day_lessons.sort(key=lambda entry: entry["start"])
        day_lessons = _drop_exact_duplicate_lessons(day_lessons)
        day_lessons = _merge_parallel_variants(day_lessons)
        day_lessons = _collapse_same_subject_blocks(day_lessons)

        lesson_lines: list[str] = []
        for lesson in day_lessons:
            intervals = lesson.get("intervals")
            if not isinstance(intervals, list) or not intervals:
                intervals = [(lesson["start"], lesson["end"])]

            first_start = intervals[0][0]
            start_epoch = int(first_start.timestamp())
            icon = _change_icon(lesson.get("change_type"))
            subject = _clean_subject(lesson.get("subject"), lesson.get("change_type"), lesson.get("note"))
            teacher = _canonical_list_text(lesson.get("teacher")) or ""
            room = _canonical_list_text(lesson.get("room")) or ""
            note = str(lesson.get("note") or "").strip()

            detail_parts = []
            if teacher:
                detail_parts.append(teacher)
            if room:
                detail_parts.append(f"Raum {room}")
            details = " | ".join(detail_parts)
            intervals_text = ", ".join(
                f"{_lesson_duration_badge(start, end)} <t:{int(start.timestamp())}:t>-<t:{int(end.timestamp())}:t>"
                for start, end in intervals
                if isinstance(start, datetime) and isinstance(end, datetime)
            )
            blocks_suffix = f" | {len(intervals)} Blöcke" if len(intervals) > 1 else ""
            icon_prefix = f"{icon} " if icon else ""
            line = f"{icon_prefix}**{subject}**"
            line += f"\nZeit: {intervals_text} • <t:{start_epoch}:R>{blocks_suffix}"
            if details:
                line += f" | {details}"
            if note:
                line += f"\nHinweis: {_clip(note, 180)}"
            lesson_lines.append(line)

        embed.description = "\n\n".join(lesson_lines) if lesson_lines else "Keine Einträge."
        rendered.append(
            RenderedEmbed(
                key=day_date.isoformat(),
                embed=embed,
                fingerprint=_fingerprint({"date": day_date.isoformat(), "lines": lesson_lines}),
            )
        )

    return rendered


def render_homework(
    homework_items: list[dict[str, Any]],
    schedule_days: list[dict[str, Any]],
    timezone_name: str,
) -> list[RenderedEmbed]:
    tz = resolve_timezone(timezone_name)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in homework_items:
        if not isinstance(item, dict):
            continue
        due = str(item.get("due_date") or "")
        if not due:
            continue
        grouped.setdefault(due, []).append(item)

    upcoming_lessons = _flatten_lessons(schedule_days, tz)
    upcoming_lessons.sort(key=lambda entry: entry["start"])

    rendered: list[RenderedEmbed] = []
    for due_key in sorted(grouped):
        due_date = _parse_date(due_key)
        if due_date is None:
            continue

        embed = discord.Embed(
            title=f"📚 Hausaufgaben – {due_date.strftime('%d.%m.%Y')}",
            color=discord.Color.gold(),
            timestamp=datetime.now(tz),
        )

        lines: list[str] = []
        for item in grouped[due_key]:
            subject = _clean_subject(item.get("subject"), None, None)
            text = str(item.get("text") or "").strip()
            done = bool(item.get("done"))
            marker = "✅" if done else "📝"

            next_lesson = _next_lesson_for_subject(subject, upcoming_lessons)
            if next_lesson is None:
                deadline_text = "nächste Stunde: unbekannt"
            else:
                deadline_text = f"nächste Stunde: <t:{int(next_lesson.timestamp())}:F> (<t:{int(next_lesson.timestamp())}:R>)"

            lines.append(f"{marker} **{subject}**\n{_clip(text, 180)}\n{deadline_text}")

        embed.description = "\n\n".join(lines) if lines else "Keine Hausaufgaben."
        rendered.append(
            RenderedEmbed(
                key=due_date.isoformat(),
                embed=embed,
                fingerprint=_fingerprint({"due": due_date.isoformat(), "lines": lines}),
            )
        )

    return rendered


def render_grades(grades: list[dict[str, Any]], timezone_name: str) -> list[RenderedEmbed]:
    tz = resolve_timezone(timezone_name)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in grades:
        if not isinstance(item, dict):
            continue
        subject = str(item.get("subject") or "Fach")
        grouped.setdefault(subject, []).append(item)

    rendered: list[RenderedEmbed] = []
    for subject in sorted(grouped):
        entries = grouped[subject]
        entries.sort(key=lambda row: str(row.get("date") or ""))
        subject = _clean_subject(subject, None, None)

        embed = discord.Embed(
            title=f"📊 Noten – {subject}",
            color=discord.Color.green(),
            timestamp=datetime.now(tz),
        )

        lines: list[str] = []
        for entry in entries:
            grade = str(entry.get("grade") or "?")
            grade_date = str(entry.get("date") or "-")
            weight = entry.get("weight")
            comment = str(entry.get("comment") or "").strip()
            suffix = f" (Gewichtung {weight})" if weight not in (None, "") else ""
            line = f"- {grade_date} | **{grade}**{suffix}"
            if comment:
                line += f"\n  {_clip(comment, 120)}"
            lines.append(line)

        embed.description = "\n".join(lines) if lines else "Keine Noten."
        embed.set_footer(text=f"{len(entries)} Noten")
        rendered.append(
            RenderedEmbed(
                key=_slug(subject),
                embed=embed,
                fingerprint=_fingerprint({"subject": subject, "lines": lines}),
            )
        )

    return rendered


def render_events(events: list[dict[str, Any]], timezone_name: str) -> list[RenderedEmbed]:
    tz = resolve_timezone(timezone_name)
    rendered: list[RenderedEmbed] = []

    normalized: list[dict[str, Any]] = []
    for item in events:
        if not isinstance(item, dict):
            continue
        start = _parse_datetime(item.get("start"), tz)
        end = _parse_datetime(item.get("end"), tz)
        if start is None or end is None:
            continue
        normalized.append({"raw": item, "start": start, "end": end})

    normalized.sort(key=lambda row: row["start"])

    for entry in normalized:
        item = entry["raw"]
        start = entry["start"]
        end = entry["end"]

        title = _clip(str(item.get("title") or "Event"), 120)
        location = str(item.get("location") or "").strip()
        description = str(item.get("description") or "").strip()

        start_epoch = int(start.timestamp())
        end_epoch = int(end.timestamp())

        embed = discord.Embed(
            title=title,
            color=discord.Color.purple(),
            timestamp=datetime.now(tz),
        )
        lines = [
            f"Wann: <t:{start_epoch}:F> bis <t:{end_epoch}:t>",
            f"Noch: <t:{start_epoch}:R>",
            f"Dauer: {_format_duration_label(start, end)}",
        ]
        if location:
            lines.append(f"Ort: {location}")
        if description:
            lines.append(f"Info: {_clip(description, 220)}")
        embed.description = "\n".join(lines)

        key = str(item.get("id") or f"{start_epoch}-{_slug(title)}")
        rendered.append(
            RenderedEmbed(
                key=key,
                embed=embed,
                fingerprint=_fingerprint({"key": key, "lines": lines}),
                sort_epoch=start_epoch,
            )
        )

    return rendered


def _flatten_lessons(schedule_days: list[dict[str, Any]], tz: tzinfo) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for day in schedule_days:
        if not isinstance(day, dict):
            continue
        day_date = _parse_date(day.get("date"))
        if day_date is None:
            continue
        lessons = day.get("lessons")
        if not isinstance(lessons, list):
            continue
        for lesson in lessons:
            if not isinstance(lesson, dict):
                continue
            start = _combine(day_date, lesson.get("start_time"), tz)
            end = _combine(day_date, lesson.get("end_time"), tz)
            rows.append(
                {
                    "date": day_date,
                    "start": start,
                    "end": end,
                    "subject": str(lesson.get("subject") or "Fach"),
                    "teacher": lesson.get("teacher"),
                    "room": lesson.get("room"),
                    "change_type": lesson.get("change_type"),
                    "note": lesson.get("note"),
                }
            )
    return rows


def _next_lesson_for_subject(subject: str, upcoming_lessons: list[dict[str, Any]]) -> datetime | None:
    target = _normalize(subject)
    if not target:
        return None
    for lesson in upcoming_lessons:
        lesson_subject = _normalize(str(lesson.get("subject") or ""))
        if lesson_subject == target or target in lesson_subject or lesson_subject in target:
            return lesson["start"]
    return None


def _parse_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _parse_datetime(value: Any, tz: tzinfo) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(tz) if value.tzinfo else value.replace(tzinfo=tz)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def _combine(day_date: date, hhmm: Any, tz: tzinfo) -> datetime:
    text = str(hhmm or "00:00").strip()
    try:
        match = re.match(r"^\s*(\d{1,2})[:.](\d{2})", text)
        if match:
            hour = int(match.group(1))
            minute = int(match.group(2))
        else:
            digits = re.sub(r"\D", "", text)
            hour = int(digits[:2])
            minute = int(digits[2:4])
        value_time = time(hour=hour, minute=minute)
    except Exception:
        value_time = time(hour=0, minute=0)
    return datetime.combine(day_date, value_time, tzinfo=tz)


def _slug(value: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", _normalize(value))
    return base.strip("-") or "fach"


def _normalize(value: str) -> str:
    return re.sub(r"\s+", "", value.casefold())


def _clean_subject(subject: Any, change_type: Any, note: Any) -> str:
    text = str(subject or "").strip()
    normalized = _normalize(text)
    if text and normalized not in {"unbekannt", "unknown", "-", "n/a"}:
        return text

    if str(change_type or "") == "info":
        return "Information"
    if str(note or "").strip():
        return "Information"
    return "Fach"


def _clip(value: str, max_len: int) -> str:
    text = value.strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rstrip() + "..."


def _change_icon(change_type: Any) -> str:
    mapping = {
        "cancellation": "🚫",
        "substitution": "🔄",
        "room_change": "🏫",
        "exam": "📝",
        "info": "ℹ️",
    }
    return mapping.get(str(change_type or ""), "")


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compact_rendered(items: Iterable[RenderedEmbed]) -> list[RenderedEmbed]:
    return [item for item in items]


def render_homework_item(
    item: dict[str, Any],
    schedule_days: list[dict[str, Any]],
    timezone_name: str,
    *,
    done: bool = False,
) -> RenderedEmbed:
    """Render a single homework item as its own embed (for per-item channel messages)."""
    tz = resolve_timezone(timezone_name)
    subject = _clean_subject(item.get("subject"), None, None)
    text = str(item.get("text") or "").strip()
    due_raw = str(item.get("due_date") or "")
    item_id = str(item.get("id") or "")

    due_date = _parse_date(due_raw)
    if due_date is not None:
        due_epoch = int(datetime.combine(due_date, time(0, 0), tzinfo=tz).timestamp())
    else:
        due_epoch = None

    upcoming_lessons = _flatten_lessons(schedule_days, tz)
    upcoming_lessons.sort(key=lambda entry: entry["start"])
    next_lesson = _next_lesson_for_subject(subject, upcoming_lessons)

    if done:
        title = f"~~{subject}~~"
        color = discord.Color.green()
        marker = "✅"
    else:
        title = subject
        color = discord.Color.gold()
        marker = "📝"

    embed = discord.Embed(
        title=f"{marker} {title}",
        color=color,
        timestamp=datetime.now(tz),
    )

    lines: list[str] = []
    if text:
        lines.append(_clip(text, 200))
    if due_epoch is not None:
        lines.append(f"Fällig: <t:{due_epoch}:D> (<t:{due_epoch}:R>)")
    if next_lesson is not None:
        lines.append(f"Nächste Stunde: <t:{int(next_lesson.timestamp())}:F>")
    elif due_epoch is not None:
        lines.append("Nächste Stunde: unbekannt")

    embed.description = "\n".join(lines) if lines else "Keine Details"
    embed.set_footer(text=f"Hausaufgabe • {subject}")

    fp_data = {"id": item_id, "subject": subject, "text": text, "done": done, "due": due_raw}
    return RenderedEmbed(
        key=item_id,
        embed=embed,
        fingerprint=_fingerprint(fp_data),
        sort_epoch=due_epoch,
    )


def render_absences(absences: list[dict[str, Any]], timezone_name: str) -> list[RenderedEmbed]:
    """Render absences as a single persistent embed."""
    tz = resolve_timezone(timezone_name)

    embed = discord.Embed(
        title="📋 Fehlzeiten",
        color=discord.Color.red(),
        timestamp=datetime.now(tz),
    )

    if not absences:
        embed.description = "Keine Fehlzeiten vorhanden."
        return [RenderedEmbed(key="absences", embed=embed, fingerprint=_fingerprint({"entries": []}))]

    sorted_absences = sorted(
        [a for a in absences if isinstance(a, dict)],
        key=lambda a: str(a.get("date") or ""),
        reverse=True,
    )

    lines: list[str] = []
    excused_count = 0
    unexcused_count = 0

    for absence in sorted_absences[:25]:
        abs_date = _parse_date(absence.get("date"))
        if abs_date is None:
            continue
        date_str = abs_date.strftime("%d.%m.%Y")
        periods = absence.get("periods") or []
        period_str = f"Std. {', '.join(str(p) for p in periods)}" if periods else "ganztägig"
        reason = str(absence.get("reason") or "").strip() or "kein Grund"
        excused = bool(absence.get("excused"))
        icon = "✅" if excused else "❌"
        if excused:
            excused_count += 1
        else:
            unexcused_count += 1
        lines.append(f"{icon} **{date_str}** | {period_str} | {_clip(reason, 60)}")

    embed.description = "\n".join(lines) if lines else "Keine Einträge."
    embed.add_field(name="Entschuldigt", value=str(excused_count), inline=True)
    embed.add_field(name="Unentschuldigt", value=str(unexcused_count), inline=True)
    embed.set_footer(text=f"{len(sorted_absences)} Einträge gesamt")

    return [RenderedEmbed(
        key="absences",
        embed=embed,
        fingerprint=_fingerprint({"lines": lines, "total": len(absences)}),
    )]


def render_messages(messages: list[dict[str, Any]], timezone_name: str) -> list[RenderedEmbed]:
    """Render each unread message as its own embed."""
    tz = resolve_timezone(timezone_name)
    rendered: list[RenderedEmbed] = []

    sorted_msgs = sorted(
        [m for m in messages if isinstance(m, dict)],
        key=lambda m: str(m.get("date") or ""),
        reverse=True,
    )

    for msg in sorted_msgs[:20]:
        msg_id = str(msg.get("id") or "")
        sender = str(msg.get("sender") or "Unbekannt")
        subject = str(msg.get("subject") or "(kein Betreff)")
        body = str(msg.get("body_preview") or "").strip()
        read = bool(msg.get("read"))
        unread_count = int(msg.get("unread_count") or 0)

        msg_dt = _parse_datetime(msg.get("date"), tz)

        color = discord.Color.light_grey() if read else discord.Color.blue()

        title = f"{'📭' if read else '📬'} {_clip(subject, 100)}"
        if unread_count > 0:
            title += f" ({unread_count} neu)"
        embed = discord.Embed(
            title=title,
            color=color,
            timestamp=datetime.now(tz),
        )
        embed.set_author(name=_clip(sender, 60))
        lines: list[str] = []
        if msg_dt is not None:
            lines.append(f"Am: <t:{int(msg_dt.timestamp())}:F>")
        lines.append("🔵 ungelesen" if not read else "✅ gelesen")
        if body:
            lines.append(f"\n{_clip(body, 200)}")

        embed.description = "\n".join(lines)
        embed.set_footer(text=f"Konversation • ID: {msg_id}")

        fp_data = {"id": msg_id, "subject": subject, "read": read, "sender": sender, "unread": unread_count}
        rendered.append(RenderedEmbed(
            key=msg_id,
            embed=embed,
            fingerprint=_fingerprint(fp_data),
            sort_epoch=int(msg_dt.timestamp()) if msg_dt else None,
        ))

    return rendered


def render_letters(letters: list[dict[str, Any]], timezone_name: str) -> list[RenderedEmbed]:
    """Render each Elternbrief as its own embed (newest first)."""
    tz = resolve_timezone(timezone_name)
    rendered: list[RenderedEmbed] = []

    sorted_letters = sorted(
        [letter for letter in letters if isinstance(letter, dict)],
        key=lambda letter: str(letter.get("date") or ""),
        reverse=True,
    )

    for letter in sorted_letters[:25]:
        letter_id = str(letter.get("id") or "")
        title = str(letter.get("title") or "Elternbrief")
        sender = str(letter.get("sender") or "").strip()
        read = bool(letter.get("read"))
        requires_confirmation = bool(letter.get("requires_confirmation"))
        attachments = int(letter.get("attachment_count") or 0)
        letter_dt = _parse_datetime(letter.get("date"), tz)

        icon = "📩" if read else "✉️"
        color = discord.Color.light_grey() if read else discord.Color.gold()

        embed = discord.Embed(
            title=f"{icon} {_clip(title, 100)}",
            color=color,
            timestamp=datetime.now(tz),
        )
        if sender:
            embed.set_author(name=_clip(sender, 60))

        lines: list[str] = []
        if letter_dt is not None:
            lines.append(f"Am: <t:{int(letter_dt.timestamp())}:F>")
        status_parts = ["✅ gelesen" if read else "🔵 ungelesen"]
        if requires_confirmation:
            status_parts.append("⚠️ Bestätigung erforderlich")
        if attachments:
            status_parts.append(f"📎 {attachments} Anhang" + ("änge" if attachments != 1 else ""))
        lines.append(" • ".join(status_parts))

        embed.description = "\n".join(lines)
        embed.set_footer(text=f"Elternbrief • ID: {letter_id}")

        fp_data = {
            "id": letter_id,
            "title": title,
            "read": read,
            "confirm": requires_confirmation,
            "attachments": attachments,
        }
        rendered.append(RenderedEmbed(
            key=letter_id or _slug(title),
            embed=embed,
            fingerprint=_fingerprint(fp_data),
            sort_epoch=int(letter_dt.timestamp()) if letter_dt else None,
        ))

    return rendered


def render_grade_stats(stats: dict[str, Any], timezone_name: str) -> list[RenderedEmbed]:
    """Render grade statistics summary as a persistent embed."""
    tz = resolve_timezone(timezone_name)

    embed = discord.Embed(
        title="📈 Notenstatistik",
        color=discord.Color.brand_green(),
        timestamp=datetime.now(tz),
    )

    subjects = stats.get("subjects") or []
    if not subjects:
        embed.description = "Keine Noten vorhanden."
        return [RenderedEmbed(key="grade_stats", embed=embed, fingerprint=_fingerprint({"subjects": []}))]

    overall_gpa = stats.get("overall_gpa")
    best = stats.get("best_subject")
    worst = stats.get("worst_subject")

    lines: list[str] = []
    trend_icons = {"improving": "📈", "stable": "➡️", "declining": "📉"}

    for subj in sorted(subjects, key=lambda s: s.get("average") if isinstance(s.get("average"), (int, float)) else 99):
        name = str(subj.get("subject") or "Fach")
        avg = subj.get("average")
        count = subj.get("grade_count", 0)
        trend = str(subj.get("trend") or "stable")
        icon = trend_icons.get(trend, "➡️")
        avg_str = f"{avg:.2f}" if isinstance(avg, (int, float)) else "-"
        grade_values = subj.get("grade_values") or []
        spark = _sparkline(grade_values) if len(grade_values) >= 2 else ""
        spark_part = f" `{spark}`" if spark else ""
        lines.append(f"{icon} **{name}**: Ø {avg_str} ({count} Noten){spark_part}")

    embed.description = "\n".join(lines)

    if isinstance(overall_gpa, (int, float)):
        embed.add_field(name="⭐ Gesamtschnitt", value=f"Ø {overall_gpa:.2f}", inline=True)
    if best:
        embed.add_field(name="🥇 Bestes Fach", value=best, inline=True)
    if worst and worst != best:
        embed.add_field(name="📉 Schlechtestes Fach", value=worst, inline=True)

    return [RenderedEmbed(
        key="grade_stats",
        embed=embed,
        fingerprint=_fingerprint({"lines": lines, "gpa": overall_gpa}),
    )]


def _sparkline(values: list[float], *, low: float = 1.0, high: float = 6.0) -> str:
    """Return a Unicode block-sparkline. Grade 1 (best) → tall bar; grade 6 → short bar."""
    if not values:
        return ""
    bars = "▁▂▃▄▅▆▇█"
    span = high - low
    result: list[str] = []
    for v in values:
        # Invert so better grade = taller bar
        normalized = 1.0 - (max(low, min(high, v)) - low) / span
        idx = min(7, max(0, int(normalized * 8)))
        result.append(bars[idx])
    return "".join(result)


def _merge_parallel_variants(lessons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not lessons:
        return []

    grouped: dict[tuple[Any, ...], dict[str, Any]] = {}
    order: list[tuple[Any, ...]] = []
    teachers_by_key: dict[tuple[Any, ...], list[str]] = {}
    rooms_by_key: dict[tuple[Any, ...], list[str]] = {}
    notes_by_key: dict[tuple[Any, ...], list[str]] = {}

    for lesson in lessons:
        start = lesson.get("start")
        end = lesson.get("end")
        key = (
            lesson.get("date"),
            int(start.timestamp()) if isinstance(start, datetime) else str(start),
            int(end.timestamp()) if isinstance(end, datetime) else str(end),
            _normalize(str(lesson.get("subject") or "")),
            str(lesson.get("change_type") or ""),
        )

        if key not in grouped:
            grouped[key] = dict(lesson)
            order.append(key)
            teachers_by_key[key] = []
            rooms_by_key[key] = []
            notes_by_key[key] = []

        _add_unique_text(teachers_by_key[key], lesson.get("teacher"))
        _add_unique_text(rooms_by_key[key], lesson.get("room"))
        _add_unique_text(notes_by_key[key], lesson.get("note"))

    merged: list[dict[str, Any]] = []
    for key in order:
        item = grouped[key]
        item["teacher"] = _join_values(teachers_by_key[key], ", ", sort_values=True)
        item["room"] = _join_values(rooms_by_key[key], ", ", sort_values=True)
        item["note"] = _join_values(notes_by_key[key], " | ", sort_values=False)
        merged.append(item)

    return merged


def _collapse_same_subject_blocks(lessons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not lessons:
        return []

    grouped: dict[tuple[str, ...], dict[str, Any]] = {}
    order: list[tuple[str, ...]] = []

    for lesson in lessons:
        key = (
            _normalize(str(lesson.get("subject") or "")),
            _normalize(_canonical_list_text(lesson.get("teacher")) or ""),
            _normalize(_canonical_list_text(lesson.get("room")) or ""),
            _normalize(str(lesson.get("note") or "")),
            str(lesson.get("change_type") or ""),
        )
        if key not in grouped:
            base = dict(lesson)
            base["intervals"] = []
            grouped[key] = base
            order.append(key)
        start = lesson.get("start")
        end = lesson.get("end")
        if isinstance(start, datetime) and isinstance(end, datetime):
            grouped[key]["intervals"].append((start, end))

    collapsed: list[dict[str, Any]] = []
    for key in order:
        row = grouped[key]
        intervals = row.get("intervals") or []
        intervals.sort(key=lambda pair: pair[0])
        if intervals:
            row["start"] = intervals[0][0]
            row["end"] = intervals[-1][1]
        collapsed.append(row)

    collapsed.sort(
        key=lambda row: (
            row.get("start") if isinstance(row.get("start"), datetime) else datetime.min.replace(tzinfo=timezone.utc),
            _normalize(str(row.get("subject") or "")),
        )
    )
    return collapsed


def _restrict_to_next_active_day(lessons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not lessons:
        return []
    first_day = lessons[0].get("date")
    if not isinstance(first_day, date):
        return lessons
    return [lesson for lesson in lessons if lesson.get("date") == first_day]


def _merge_consecutive_blocks(lessons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not lessons:
        return []

    merged: list[dict[str, Any]] = []
    for lesson in lessons:
        if not merged:
            merged.append(dict(lesson))
            continue

        prev = merged[-1]
        if _can_merge_consecutive(prev, lesson):
            prev["end"] = lesson.get("end")
            prev_note = str(prev.get("note") or "").strip()
            next_note = str(lesson.get("note") or "").strip()
            if next_note and next_note not in prev_note:
                prev["note"] = f"{prev_note} | {next_note}".strip(" |")
            continue

        merged.append(dict(lesson))

    return merged


def _can_merge_consecutive(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_start = left.get("start")
    left_end = left.get("end")
    right_start = right.get("start")
    if not isinstance(left_start, datetime) or not isinstance(left_end, datetime) or not isinstance(right_start, datetime):
        return False
    if left.get("date") != right.get("date"):
        return False
    if left_end != right_start:
        return False
    return (
        _normalize(str(left.get("subject") or "")) == _normalize(str(right.get("subject") or ""))
        and _normalize(_canonical_list_text(left.get("teacher")) or "") == _normalize(_canonical_list_text(right.get("teacher")) or "")
        and _normalize(_canonical_list_text(left.get("room")) or "") == _normalize(_canonical_list_text(right.get("room")) or "")
        and str(left.get("change_type") or "") == str(right.get("change_type") or "")
    )


def _drop_exact_duplicate_lessons(lessons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()

    for lesson in lessons:
        start = lesson.get("start")
        end = lesson.get("end")
        key = (
            lesson.get("date"),
            int(start.timestamp()) if isinstance(start, datetime) else str(start),
            int(end.timestamp()) if isinstance(end, datetime) else str(end),
            str(lesson.get("subject") or ""),
            str(lesson.get("teacher") or ""),
            str(lesson.get("room") or ""),
            str(lesson.get("change_type") or ""),
            str(lesson.get("note") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(lesson)

    return deduped


def _add_unique_text(target: list[str], value: Any) -> None:
    text = str(value or "").strip()
    if not text:
        return
    if text in target:
        return
    target.append(text)


def _canonical_list_text(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    parts = [part.strip() for part in re.split(r"\s*(?:,|\|)\s*", text) if part.strip()]
    if not parts:
        return None
    unique = sorted(set(parts), key=lambda item: item.casefold())
    return ", ".join(unique)


def _join_values(values: list[str], separator: str, *, sort_values: bool) -> str | None:
    if not values:
        return None
    unique: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in unique:
            continue
        unique.append(text)
    if not unique:
        return None
    if sort_values:
        unique = sorted(unique, key=lambda item: item.casefold())
    return separator.join(unique)


def _lesson_duration_badge(start: datetime, end: datetime) -> str:
    minutes = _duration_minutes(start, end)
    if minutes is None:
        return "🔹"
    if 85 <= minutes <= 95:
        return "2️⃣"
    if 40 <= minutes <= 50:
        return "1️⃣"
    return "🔹"


def _duration_minutes(start: datetime, end: datetime) -> int | None:
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        return None
    seconds = int((end - start).total_seconds())
    if seconds <= 0:
        return None
    return seconds // 60


def _format_duration_label(start: datetime, end: datetime) -> str:
    minutes = _duration_minutes(start, end)
    if minutes is None:
        return "-"
    hours, rem = divmod(minutes, 60)
    if hours and rem:
        return f"{hours}h {rem}m"
    if hours:
        return f"{hours}h"
    return f"{rem}m"
