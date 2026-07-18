from datetime import date, datetime, timedelta, timezone

from schulmanager_discord_bot.embeds import (
    render_events,
    render_exams,
    render_grades,
    render_homework,
    render_learning,
    render_letters,
    render_messages,
    render_payments,
    render_schedule_feed,
    render_schedule_week,
)


def test_render_schedule_items() -> None:
    schedule = [
        {
            "date": date.today().isoformat(),
            "lessons": [
                {
                    "start_time": "08:00",
                    "end_time": "08:45",
                    "subject": "Mathe",
                    "teacher": "Herr A",
                    "room": "101",
                }
            ],
        }
    ]
    feed = render_schedule_feed(schedule, "Europe/Berlin")
    week = render_schedule_week(schedule, "Europe/Berlin")

    assert len(feed) == 1
    assert len(week) == 1
    assert week[0].key == date.today().isoformat()


def test_schedule_deduplicates_identical_lessons() -> None:
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    duplicate_lesson = {
        "start_time": "08:00",
        "end_time": "08:45",
        "subject": "Mathe",
        "teacher": "Herr A",
        "room": "101",
    }
    schedule = [
        {
            "date": tomorrow,
            "lessons": [duplicate_lesson, dict(duplicate_lesson)],
        }
    ]

    feed = render_schedule_feed(schedule, "Europe/Berlin")
    week = render_schedule_week(schedule, "Europe/Berlin")

    assert feed[0].embed.description.count("**Mathe**") == 1
    assert week[0].embed.description.count("**Mathe**") == 1


def test_schedule_merges_parallel_variants_into_single_subject_line() -> None:
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    schedule = [
        {
            "date": tomorrow,
            "lessons": [
                {
                    "start_time": "08:00",
                    "end_time": "08:45",
                    "subject": "Uebung",
                    "teacher": "Herr A",
                    "room": "130",
                },
                {
                    "start_time": "08:00",
                    "end_time": "08:45",
                    "subject": "Uebung",
                    "teacher": "Herr B",
                    "room": "132",
                },
            ],
        }
    ]

    feed = render_schedule_feed(schedule, "Europe/Berlin")
    week = render_schedule_week(schedule, "Europe/Berlin")
    feed_text = feed[0].embed.description or ""
    week_text = week[0].embed.description or ""

    assert feed_text.count("**Uebung**") == 1
    assert week_text.count("**Uebung**") == 1
    assert "Herr A" in week_text and "Herr B" in week_text
    assert "130" in week_text and "132" in week_text


def test_schedule_collapses_same_subject_blocks_with_multiple_intervals() -> None:
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    schedule = [
        {
            "date": tomorrow,
            "lessons": [
                {
                    "start_time": "08:00",
                    "end_time": "08:45",
                    "subject": "Uebung",
                    "teacher": "Ra",
                    "room": "130",
                },
                {
                    "start_time": "08:00",
                    "end_time": "08:45",
                    "subject": "Uebung",
                    "teacher": "Ws",
                    "room": "132",
                },
                {
                    "start_time": "08:45",
                    "end_time": "09:30",
                    "subject": "Uebung",
                    "teacher": "Ws",
                    "room": "132",
                },
                {
                    "start_time": "08:45",
                    "end_time": "09:30",
                    "subject": "Uebung",
                    "teacher": "Ra",
                    "room": "130",
                },
            ],
        }
    ]

    week = render_schedule_week(schedule, "Europe/Berlin")
    week_text = week[0].embed.description or ""
    assert week_text.count("**Uebung**") == 1
    assert "Ra" in week_text and "Ws" in week_text
    assert "130" in week_text and "132" in week_text
    assert "2 Blöcke" in week_text


def test_schedule_shows_single_and_double_badges() -> None:
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    schedule = [
        {
            "date": tomorrow,
            "lessons": [
                {
                    "start_time": "08:00",
                    "end_time": "08:45",
                    "subject": "Mathe",
                    "teacher": "A",
                    "room": "101",
                },
                {
                    "start_time": "10:00",
                    "end_time": "11:30",
                    "subject": "Deutsch",
                    "teacher": "B",
                    "room": "102",
                },
            ],
        }
    ]

    week = render_schedule_week(schedule, "Europe/Berlin")
    text = week[0].embed.description or ""
    assert "1️⃣" in text
    assert "2️⃣" in text


def test_schedule_feed_only_shows_next_active_day() -> None:
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    day_after = (date.today() + timedelta(days=2)).isoformat()
    schedule = [
        {
            "date": tomorrow,
            "lessons": [
                {
                    "start_time": "08:00",
                    "end_time": "08:45",
                    "subject": "Mathe",
                    "teacher": "A",
                    "room": "101",
                }
            ],
        },
        {
            "date": day_after,
            "lessons": [
                {
                    "start_time": "08:00",
                    "end_time": "08:45",
                    "subject": "Deutsch",
                    "teacher": "B",
                    "room": "102",
                }
            ],
        },
    ]

    feed = render_schedule_feed(schedule, "Europe/Berlin")
    text = feed[0].embed.description or ""
    assert "**Mathe**" in text
    assert "**Deutsch**" not in text


def test_schedule_feed_merges_consecutive_to_double_lesson() -> None:
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    schedule = [
        {
            "date": tomorrow,
            "lessons": [
                {
                    "start_time": "08:00",
                    "end_time": "08:45",
                    "subject": "Uebung",
                    "teacher": "Ra",
                    "room": "130",
                },
                {
                    "start_time": "08:45",
                    "end_time": "09:30",
                    "subject": "Uebung",
                    "teacher": "Ra",
                    "room": "130",
                },
            ],
        }
    ]

    feed = render_schedule_feed(schedule, "Europe/Berlin")
    text = feed[0].embed.description or ""
    assert text.count("**Uebung**") == 1
    assert "2️⃣" in text


def test_render_homework_groups_per_day() -> None:
    today = date.today().isoformat()
    homework = [
        {"id": "1", "subject": "Mathe", "text": "Aufgabe", "due_date": today, "done": False},
        {"id": "2", "subject": "Mathe", "text": "Zweite", "due_date": today, "done": True},
    ]
    schedule = [
        {
            "date": today,
            "lessons": [{"start_time": "10:00", "end_time": "10:45", "subject": "Mathe"}],
        }
    ]

    rendered = render_homework(homework, schedule, "Europe/Berlin")
    assert len(rendered) == 1
    assert rendered[0].key == today


def test_render_grades_per_subject() -> None:
    grades = [
        {"subject": "Mathe", "grade": "1", "date": "2026-01-01", "comment": "Test"},
        {"subject": "Deutsch", "grade": "2", "date": "2026-01-02", "comment": "Ex"},
    ]

    rendered = render_grades(grades, "Europe/Berlin")
    keys = {item.key for item in rendered}
    assert "mathe" in keys
    assert "deutsch" in keys


def test_render_events() -> None:
    now = datetime.now(timezone.utc)
    later = now + timedelta(hours=1)
    events = [
        {
            "id": "ev1",
            "title": "Elternabend",
            "start": now.isoformat(),
            "end": later.isoformat(),
            "location": "Aula",
        }
    ]

    rendered = render_events(events, "Europe/Berlin")
    assert len(rendered) == 1
    assert rendered[0].key == "ev1"


def test_render_letters() -> None:
    now = datetime.now(timezone.utc)
    letters = [
        {
            "id": "l1",
            "title": "Klassenfahrt",
            "date": now.isoformat(),
            "read": False,
            "sender": "Klassenleitung",
            "requires_confirmation": True,
            "attachment_count": 2,
        },
        {
            "id": "l2",
            "title": "Info",
            "date": (now - timedelta(days=3)).isoformat(),
            "read": True,
            "requires_confirmation": False,
            "attachment_count": 0,
        },
    ]
    rendered = render_letters(letters, "Europe/Berlin")
    assert {item.key for item in rendered} == {"l1", "l2"}
    # Newest first
    assert rendered[0].key == "l1"
    unread = next(item for item in rendered if item.key == "l1")
    assert "Bestätigung erforderlich" in (unread.embed.description or "")


def test_render_letters_empty() -> None:
    assert render_letters([], "Europe/Berlin") == []


def test_render_payments() -> None:
    payments = [
        {"id": "p1", "title": "Klassenfahrt", "amount": 120.0, "paid": False, "due_date": "2026-08-01"},
        {"id": "p2", "title": "Kopiergeld", "amount": 15.0, "paid": True, "due_date": "2026-05-01"},
    ]
    rendered = render_payments(payments, "Europe/Berlin")
    assert len(rendered) == 1
    text = rendered[0].embed.description or ""
    assert "Klassenfahrt" in text and "🔴" in text  # unpaid marker


def test_render_learning() -> None:
    units = [
        {"id": "u1", "subject": "DB", "title": "Arbeitsblatt", "published": "2026-07-10T09:00:00", "done": False, "seen": False},
    ]
    rendered = render_learning(units, "Europe/Berlin")
    assert len(rendered) == 1
    assert "Arbeitsblatt" in (rendered[0].embed.description or "")


def test_render_exams_empty() -> None:
    rendered = render_exams([], "Europe/Berlin")
    assert rendered and "Keine" in (rendered[0].embed.description or "")


def test_render_messages_shows_unread_count() -> None:
    now = datetime.now(timezone.utc)
    messages = [
        {
            "id": "sub1",
            "sender": "Frau Adler",
            "subject": "Ausflug",
            "body_preview": "",
            "date": now.isoformat(),
            "read": False,
            "unread_count": 3,
        }
    ]
    rendered = render_messages(messages, "Europe/Berlin")
    assert len(rendered) == 1
    assert "(3 neu)" in (rendered[0].embed.title or "")
